"""tests/test_report_agent_page_override.py — "decide first, not build-then-fix".

Covers a real pattern found by forensically replaying live `adk web`
sessions: the user states an exact page count / visual variety up front,
but the agent used to build a generic report, discover the count was
wrong, and reactively add/delete pages to correct it.

- ``ctx.extra["requested_num_pages"]`` lets ReportAgent honor an EXACT
  page count in one pass (agents/report_agent.py::_run), instead of
  forcing a separate additive `build_report` follow-up call.
- ``ctx.extra["requested_visual_variety"] == "all"`` adds scatter/pie/kpi
  candidates directly in `_plan_visuals` (previously only reachable via
  that same separate `build_report` tool).
- `OrchestratorAgent.run(num_pages=..., visual_variety=...)` threads both
  through end-to-end from a single `generate_pbip` call.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _rich_schema() -> dict:
    """Enough category columns + measures that split_to_pages needs more
    than one page to hold everything, so a num_pages override is
    observable (not just capped by lack of content)."""
    return {
        "table_name": "Sales",
        "columns": [
            {"name": "Segment", "dataType": "string", "summarizeBy": "none"},
            {"name": "Country", "dataType": "string", "summarizeBy": "none"},
            {"name": "Product", "dataType": "string", "summarizeBy": "none"},
            {"name": "Discount Band", "dataType": "string", "summarizeBy": "none"},
            {"name": "Date", "dataType": "dateTime", "summarizeBy": "none"},
            {"name": "Sales", "dataType": "double", "summarizeBy": "sum"},
            {"name": "Profit", "dataType": "double", "summarizeBy": "sum"},
            {"name": "COGS", "dataType": "double", "summarizeBy": "sum"},
            {"name": "Units Sold", "dataType": "double", "summarizeBy": "sum"},
        ],
    }


def _rich_measures() -> list[dict]:
    return [
        {"name": "Total Sales", "expression": "SUM('Sales'[Sales])"},
        {"name": "Total Profit", "expression": "SUM('Sales'[Profit])"},
        {"name": "Total COGS", "expression": "SUM('Sales'[COGS])"},
        {"name": "Total Units Sold", "expression": "SUM('Sales'[Units Sold])"},
    ]


class TestPlanVisualsVariety(unittest.TestCase):
    """agents/report_agent.py::ReportAgent._plan_visuals"""

    def _agent(self):
        from agents.report_agent import ReportAgent
        return ReportAgent.__new__(ReportAgent)

    def _buckets(self):
        from agents.dax_agent import _classify_columns
        return _classify_columns(_rich_schema()["columns"])

    def test_default_variety_has_no_scatter_pie_kpi(self):
        agent = self._agent()
        plans = agent._plan_visuals("Sales", self._buckets(), _rich_measures())
        kinds = {p["kind"] for p in plans}
        self.assertNotIn("scatterChart", kinds)
        self.assertNotIn("pieChart", kinds)
        self.assertNotIn("kpi", kinds)

    def test_all_variety_adds_scatter_pie_kpi(self):
        agent = self._agent()
        plans = agent._plan_visuals(
            "Sales", self._buckets(), _rich_measures(), visual_variety="all",
        )
        kinds = {p["kind"] for p in plans}
        self.assertIn("scatterChart", kinds)
        self.assertIn("pieChart", kinds)
        self.assertIn("kpi", kinds)

    def test_scatter_candidate_has_two_distinct_measures(self):
        agent = self._agent()
        plans = agent._plan_visuals(
            "Sales", self._buckets(), _rich_measures(), visual_variety="all",
        )
        scatter = next(p for p in plans if p["kind"] == "scatterChart")
        self.assertIsNotNone(scatter.get("measure2"))
        self.assertNotEqual(scatter["measure"], scatter["measure2"])


class TestBuildVisualPayloadRichTypes(unittest.TestCase):
    """agents/report_agent.py::ReportAgent._build_visual_payload"""

    def _payload(self, plan: dict):
        from agents.report_agent import ReportAgent
        return ReportAgent._build_visual_payload("Sales", plan, {"x": 0, "y": 0, "width": 100, "height": 100})

    def test_pie_chart_payload(self):
        payload = self._payload({"name": "pie-1", "kind": "pieChart",
                                  "category": "Segment", "measure": "Total Sales"})
        self.assertEqual(payload["visual"]["visualType"], "pieChart")

    def test_scatter_uses_distinct_x_y_when_measure2_present(self):
        payload = self._payload({
            "name": "scatter-1", "kind": "scatterChart",
            "measure": "Total Sales", "measure2": "Total Profit",
        })
        qs = payload["visual"]["query"]["queryState"]
        x_ref = qs["X"]["projections"][0]["field"]["Measure"]["Property"]
        y_ref = qs["Y"]["projections"][0]["field"]["Measure"]["Property"]
        self.assertEqual(x_ref, "Total Sales")
        self.assertEqual(y_ref, "Total Profit")

    def test_scatter_falls_back_to_single_measure_without_measure2(self):
        """Backward compat: a plan with no measure2 (e.g. from some other,
        older caller) still builds a valid (if degenerate) scatter chart."""
        payload = self._payload({"name": "scatter-2", "kind": "scatterChart",
                                  "measure": "Total Sales"})
        qs = payload["visual"]["query"]["queryState"]
        x_ref = qs["X"]["projections"][0]["field"]["Measure"]["Property"]
        y_ref = qs["Y"]["projections"][0]["field"]["Measure"]["Property"]
        self.assertEqual(x_ref, "Total Sales")
        self.assertEqual(y_ref, "Total Sales")

    def test_kpi_omits_goal_without_measure2(self):
        """Regression: a KPI must never reuse its own indicator measure as
        a fake 'goal' -- that was meaningless. No measure2 -> no Goal key
        at all, rather than a self-referential one."""
        payload = self._payload({"name": "kpi-1", "kind": "kpi", "measure": "Total Sales"})
        qs = payload["visual"]["query"]["queryState"]
        self.assertNotIn("Goal", qs)

    def test_kpi_includes_distinct_goal_when_measure2_present(self):
        payload = self._payload({
            "name": "kpi-2", "kind": "kpi",
            "measure": "Total Sales", "measure2": "Total Profit",
        })
        qs = payload["visual"]["query"]["queryState"]
        goal_ref = qs["Goal"]["projections"][0]["field"]["Measure"]["Property"]
        self.assertEqual(goal_ref, "Total Profit")


class TestReportAgentNumPagesOverride(unittest.TestCase):
    """Full ReportAgent._run() pass: ctx.extra["requested_num_pages"]."""

    def _ctx(self, tmp: str, *, requested_num_pages=None, description="dashboard"):
        from agents.base import AgentContext
        from mcp_server.server import PbipToolbox

        ctx = AgentContext(
            business_description=description,
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = _rich_schema()
        ctx.measures = _rich_measures()
        if requested_num_pages:
            ctx.extra["requested_num_pages"] = requested_num_pages
        return ctx

    def test_no_override_generic_description_stays_one_page(self):
        from agents.report_agent import ReportAgent

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, description="a sales dashboard")
            result = ReportAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(ctx.pages), 1)

    def test_explicit_num_pages_produces_exactly_that_many(self):
        from agents.report_agent import ReportAgent

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp, requested_num_pages=2, description="a sales dashboard")
            result = ReportAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(ctx.pages), 2)

    def test_explicit_num_pages_caps_even_when_keywords_suggest_more(self):
        """A description that would infer 'rich' (3 pages) on its own must
        still be capped to the EXPLICIT override -- explicit beats
        inferred, so an override always wins, never just raises the ceiling."""
        from agents.report_agent import ReportAgent

        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(
                tmp, requested_num_pages=1,
                description="a comprehensive rich dashboard with 3 pages and every visual type",
            )
            result = ReportAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(ctx.pages), 1)


class TestOrchestratorThreadsPageOverride(unittest.TestCase):
    """agents/orchestrator.py::OrchestratorAgent.run(num_pages=..., visual_variety=...)"""

    _CSV = (
        "Date,Region,Product,Segment,Sales,Profit\n"
        "2024-01-05,North,Widget,A,2505.00,500.00\n"
        "2024-01-07,South,Gadget,B,999.90,200.00\n"
        "2024-02-10,East,Widget,A,2000.00,450.00\n"
        "2024-02-15,West,Gadget,B,2398.80,480.00\n"
        "2024-03-01,North,Gadget,A,899.70,190.00\n"
        "2024-03-12,South,Widget,B,3750.00,700.00\n"
    )

    def test_num_pages_and_visual_variety_thread_through_and_cap_pages(self):
        from agents.orchestrator import OrchestratorAgent

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "sales.csv"
            csv_path.write_text(self._CSV, encoding="utf-8")

            with patch("utils.model_config.get_llm_config", return_value=None):
                orchestrator = OrchestratorAgent(output_root=root / "out")
                report = orchestrator.run(
                    source_path=csv_path,
                    business_description="sales dashboard",
                    project_name="ThreadTest",
                    num_pages=1,
                    visual_variety="all",
                )
            self.assertTrue(report.ok, report.error)
            self.assertLessEqual(report.validation["pages"], 1)

    def test_omitting_both_params_leaves_extra_keys_absent(self):
        """Backward compat: omitting num_pages/visual_variety must not add
        any new ctx.extra footprint (zero behavior change by default)."""
        from agents.orchestrator import OrchestratorAgent

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            csv_path = root / "sales.csv"
            csv_path.write_text(self._CSV, encoding="utf-8")

            captured_ctx = {}
            orig_run = None

            from agents.report_agent import ReportAgent
            orig_run = ReportAgent._run

            def _spy(self):
                captured_ctx["extra"] = dict(self.context.extra)
                return orig_run(self)

            with patch("utils.model_config.get_llm_config", return_value=None), \
                 patch.object(ReportAgent, "_run", _spy):
                orchestrator = OrchestratorAgent(output_root=root / "out2")
                report = orchestrator.run(
                    source_path=csv_path,
                    business_description="sales dashboard",
                    project_name="NoOverrideTest",
                )
            self.assertTrue(report.ok, report.error)
            self.assertNotIn("requested_num_pages", captured_ctx["extra"])
            self.assertNotIn("requested_visual_variety", captured_ctx["extra"])


class TestJudgeHonorsEffectiveMaxPages(unittest.TestCase):
    """Regression: utils/judge.py's style-consistency check compared the
    actual page count against ctx.extra["report_style"] — a key ONLY ever
    written by PlannerAgent, BEFORE ReportAgent resolves its own effective
    style/page-count overrides (keyword-triggered "rich" bump, or the new
    requested_num_pages). Whenever ReportAgent's resolved style differed
    from the planner's stale suggestion, the Judge compared the correct
    page count against the WRONG cap and permanently flagged
    "style_page_count_exceeded" -- an override_action that reruns
    ReportAgent, which then produces the SAME (correct) page count again,
    so the check can never pass. This wasted the orchestrator's entire
    3-attempt feedback-loop budget on every affected build (confirmed live
    via a real adk web session that appeared to hang)."""

    def _ctx(self, *, report_style, actual_pages, effective_max_pages=None):
        from types import SimpleNamespace
        ctx = SimpleNamespace()
        ctx.measures = [{"name": "Total Sales", "expression": "SUM(x)"}]
        ctx.schema = {"columns": [{"name": "Sales"}]}
        ctx.pages = [{"id": f"p{i}"} for i in range(actual_pages)]
        ctx.extra = {
            "report_plan": None, "business_analysis": None,
            "report_style": report_style, "bi_reasoning": None,
        }
        if effective_max_pages is not None:
            ctx.extra["effective_max_pages"] = effective_max_pages
        return ctx

    def _style_actions(self, result):
        return [oa for oa in result["override_actions"]
                if oa.get("reason") == "style_page_count_exceeded"]

    def test_stale_style_no_longer_false_positives_when_max_pages_matches(self):
        from utils.judge import JudgeLayer

        # Stale planner style says "standard" (max 1), but ReportAgent
        # correctly resolved 2 pages via requested_num_pages and reported
        # its actual cap via effective_max_pages.
        ctx = self._ctx(report_style="standard", actual_pages=2, effective_max_pages=2)
        result = JudgeLayer().evaluate(ctx)
        self.assertEqual(self._style_actions(result), [])

    def test_genuine_overflow_still_flagged(self):
        """The check must still catch a REAL violation -- effective_max_pages
        itself exceeded -- so this isn't just disabling the check."""
        from utils.judge import JudgeLayer

        ctx = self._ctx(report_style="rich", actual_pages=5, effective_max_pages=2)
        result = JudgeLayer().evaluate(ctx)
        actions = self._style_actions(result)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["detail"]["max"], 2)

    def test_backward_compat_falls_back_to_style_lookup_when_absent(self):
        """Older/other callers that never set effective_max_pages keep the
        exact previous behavior (style-name lookup)."""
        from utils.judge import JudgeLayer

        ctx = self._ctx(report_style="standard", actual_pages=2)  # no effective_max_pages
        result = JudgeLayer().evaluate(ctx)
        actions = self._style_actions(result)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["detail"]["max"], 1)

    def test_full_report_agent_then_judge_no_spurious_rerun(self):
        """End-to-end: run the real ReportAgent with requested_num_pages,
        then feed its ctx straight into JudgeLayer -- confirms the write-back
        actually happens in practice, not just in a hand-built ctx."""
        from agents.base import AgentContext
        from agents.report_agent import ReportAgent
        from mcp_server.server import PbipToolbox
        from utils.judge import JudgeLayer

        with tempfile.TemporaryDirectory() as tmp:
            ctx = AgentContext(
                business_description="a sales dashboard",  # no "rich" keywords
                source_path=Path(tmp) / "data.csv",
                toolbox=PbipToolbox(tmp),
                project_name="TestProject",
                pbip_root=Path(tmp),
            )
            ctx.schema = _rich_schema()
            ctx.measures = _rich_measures()
            ctx.extra["requested_num_pages"] = 2

            result = ReportAgent(ctx).run()
            self.assertTrue(result.ok, result.message)
            self.assertEqual(len(ctx.pages), 2)

            judge_result = JudgeLayer().evaluate(ctx)
            style_actions = [
                oa for oa in judge_result["override_actions"]
                if oa.get("reason") == "style_page_count_exceeded"
            ]
            self.assertEqual(style_actions, [])


if __name__ == "__main__":
    unittest.main()
