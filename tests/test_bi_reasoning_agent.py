"""tests/test_bi_reasoning_agent.py — Tests for BIReasoningAgent + related new features.

Covers:
  * agents/bi_reasoning_agent.py  (domain inference, audience, dashboard type,
    deterministic fallback, LLM path mocked, BIReasoningAgent.run())
  * utils/explainability.py       (DecisionLog, ExplainabilityTracker, log_decision)
  * agents/schemas.py             (BIReasoningResult, VisualReasoning, BusinessAnalysis,
                                   PageRecommendation, KPIRecommendation)
  * agents/data_analyzer_agent.py (_business_analysis extension)
  * agents/visual_planner_agent.py (VisualReasoning attached to visuals)
  * orchestrator integration      (_write_decisions_log, tracker.reset)

Run with::

    python -m pytest tests/test_bi_reasoning_agent.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# ExplainabilityTracker tests
# ---------------------------------------------------------------------------


class TestExplainabilityTracker(unittest.TestCase):
    """Thread-safe per-run accumulator of DecisionLog entries."""

    def setUp(self) -> None:
        from utils.explainability import ExplainabilityTracker
        self.tracker = ExplainabilityTracker()  # fresh instance per test

    def test_record_and_retrieve(self):
        self.tracker.record(
            agent="TestAgent",
            decision_type="visual_selection",
            subject="Bar Chart",
            rationale="Best for category comparison.",
            confidence=0.9,
        )
        decisions = self.tracker.all_decisions()
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d.agent, "TestAgent")
        self.assertEqual(d.decision_type, "visual_selection")
        self.assertEqual(d.subject, "Bar Chart")
        self.assertAlmostEqual(d.confidence, 0.9)

    def test_reset_clears_decisions(self):
        self.tracker.record("A", "t", "s", "r")
        self.tracker.record("B", "t", "s", "r")
        self.assertEqual(len(self.tracker), 2)
        self.tracker.reset()
        self.assertEqual(len(self.tracker), 0)

    def test_as_dicts_returns_serialisable_list(self):
        self.tracker.record("A", "kpi_recommendation", "Revenue", "Amount column.", confidence=0.8)
        dicts = self.tracker.as_dicts()
        self.assertIsInstance(dicts, list)
        self.assertEqual(len(dicts), 1)
        d = dicts[0]
        self.assertIn("agent", d)
        self.assertIn("rationale", d)
        self.assertIn("confidence", d)

    def test_record_never_raises_on_bad_input(self):
        """Telemetry must never crash the pipeline."""
        # passing None for required fields — should silently swallow
        try:
            self.tracker.record(None, None, None, None)  # type: ignore[arg-type]
        except Exception as exc:  # pragma: no cover
            self.fail(f"record() raised unexpectedly: {exc}")

    def test_thread_safety(self):
        """Concurrent record calls must not raise or lose entries."""
        errors: list[str] = []

        def worker(n: int) -> None:
            try:
                for i in range(10):
                    self.tracker.record(f"Agent{n}", "t", f"sub{i}", "r")
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.tracker), 50)


class TestLogDecisionHelper(unittest.TestCase):
    """Global log_decision convenience wrapper."""

    def setUp(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def tearDown(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def test_log_decision_adds_entry(self):
        from utils.explainability import get_tracker, log_decision
        log_decision("BIReasoningAgent", "kpi_recommendation", "Total Revenue", "Amount col.")
        self.assertEqual(len(get_tracker()), 1)

    def test_log_decision_with_all_params(self):
        from utils.explainability import get_tracker, log_decision
        log_decision(
            agent="DataAnalyzerAgent",
            decision_type="business_analysis",
            subject="SalesAmount",
            rationale="High-value amount column.",
            alternatives=["Revenue", "Profit"],
            confidence=0.85,
            extra={"bucket": "amount"},
        )
        decisions = get_tracker().all_decisions()
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d.alternatives, ["Revenue", "Profit"])
        self.assertEqual(d.extra["bucket"], "amount")


class TestDecisionLogAsDict(unittest.TestCase):
    def test_as_dict_keys(self):
        from utils.explainability import DecisionLog
        dl = DecisionLog(
            agent="A", decision_type="t", subject="s", rationale="r",
            alternatives=["x"], confidence=0.7, extra={"k": "v"}
        )
        d = dl.as_dict()
        self.assertEqual(set(d.keys()), {"agent", "decision_type", "subject",
                                         "rationale", "alternatives", "confidence", "extra"})
        self.assertEqual(d["agent"], "A")
        self.assertEqual(d["extra"]["k"], "v")


# ---------------------------------------------------------------------------
# Pydantic schema tests
# ---------------------------------------------------------------------------


class TestBIReasoningResultSchema(unittest.TestCase):
    def test_default_construction(self):
        from agents.schemas import BIReasoningResult
        r = BIReasoningResult()
        self.assertEqual(r.target_audience, "analyst")
        self.assertEqual(r.source, "deterministic")
        self.assertIsInstance(r.recommended_pages, list)
        self.assertIsInstance(r.recommended_kpis, list)

    def test_full_construction(self):
        from agents.schemas import BIReasoningResult, KPIRecommendation, PageRecommendation
        r = BIReasoningResult(
            dashboard_goal="Track sales",
            target_audience="executive",
            dashboard_type="executive",
            recommended_pages=[PageRecommendation(id="p1", name="Overview", purpose="KPIs")],
            recommended_kpis=[KPIRecommendation(name="Total Sales", why="Revenue", measure_hint="SUM(Table[Sales])")],
            confidence=0.9,
            source="llm",
        )
        self.assertEqual(r.dashboard_goal, "Track sales")
        self.assertEqual(len(r.recommended_pages), 1)
        self.assertEqual(len(r.recommended_kpis), 1)
        self.assertEqual(r.source, "llm")

    def test_page_recommendation_defaults(self):
        from agents.schemas import PageRecommendation
        p = PageRecommendation(id="x", name="X")
        self.assertEqual(p.priority, 1)
        self.assertEqual(p.purpose, "")

    def test_kpi_recommendation_defaults(self):
        from agents.schemas import KPIRecommendation
        k = KPIRecommendation(name="Revenue")
        self.assertEqual(k.priority, 1)
        self.assertEqual(k.why, "")
        self.assertEqual(k.measure_hint, "")


class TestVisualReasoningSchema(unittest.TestCase):
    def test_default_construction(self):
        from agents.schemas import VisualReasoning
        vr = VisualReasoning()
        self.assertEqual(vr.why_this_visual, "")
        self.assertEqual(vr.task_type, "")

    def test_full_construction(self):
        from agents.schemas import VisualReasoning
        vr = VisualReasoning(
            why_this_visual="Bar chart is best for comparison",
            why_this_page="Overview page needs KPIs",
            why_these_dimensions="Region is the primary segmentation",
            why_these_measures="Revenue is the key metric",
            why_this_layout="High priority for executive attention",
            task_type="comparison",
        )
        self.assertEqual(vr.task_type, "comparison")
        self.assertEqual(vr.why_this_visual, "Bar chart is best for comparison")


class TestBusinessAnalysisSchema(unittest.TestCase):
    def test_default_construction(self):
        from agents.schemas import BusinessAnalysis
        ba = BusinessAnalysis()
        self.assertIsInstance(ba.important_measures, list)
        self.assertIsInstance(ba.potential_kpis, list)
        self.assertIsInstance(ba.trend_indicators, list)
        self.assertIsInstance(ba.executive_metrics, list)

    def test_with_data(self):
        from agents.schemas import BusinessAnalysis
        ba = BusinessAnalysis(
            important_measures=["Revenue", "Profit"],
            potential_kpis=["Total Revenue"],
            executive_metrics=["YTD Revenue"],
            recommendations=["Add time-intelligence"],
        )
        self.assertEqual(len(ba.important_measures), 2)
        self.assertEqual(len(ba.recommendations), 1)


class TestVisualPlanWithReasoning(unittest.TestCase):
    """VisualPlan now has an optional visual_reasoning field."""

    def test_visual_plan_without_reasoning(self):
        from agents.schemas import VisualPlan
        vp = VisualPlan(name="RevenueByRegion", kind="barChart",
                        category="Region")
        self.assertIsNone(vp.visual_reasoning)

    def test_visual_plan_with_reasoning(self):
        from agents.schemas import VisualPlan, VisualReasoning
        vr = VisualReasoning(why_this_visual="Best for ranking", task_type="ranking")
        vp = VisualPlan(name="RevenueByRegion", kind="barChart",
                        category="Region", visual_reasoning=vr)
        self.assertIsNotNone(vp.visual_reasoning)
        self.assertEqual(vp.visual_reasoning.task_type, "ranking")


# ---------------------------------------------------------------------------
# Domain / audience / dashboard-type inference tests
# ---------------------------------------------------------------------------


class TestDomainInference(unittest.TestCase):
    def _infer(self, desc, cols=None):
        from agents.bi_reasoning_agent import _infer_domain
        schema = {"columns": [{"name": c} for c in (cols or [])]}
        return _infer_domain(desc, schema)

    def test_finance_description(self):
        self.assertEqual(self._infer("track revenue and profit margins"), "finance")

    def test_hr_columns(self):
        self.assertEqual(self._infer("workforce data", ["employee_id", "salary", "department"]), "hr")

    def test_marketing_description(self):
        self.assertEqual(self._infer("campaign conversion and ROAS analysis"), "marketing")

    def test_logistics_columns(self):
        self.assertEqual(self._infer("supply chain", ["shipment_id", "delivery_date", "inventory"]), "logistics")

    def test_banking_columns(self):
        self.assertEqual(self._infer("loan portfolio", ["loan_id", "balance", "euribor_rate"]), "banking")

    def test_retail_columns(self):
        self.assertEqual(self._infer("product performance", ["sku", "store", "units_sold"]), "retail")

    def test_unknown_returns_general(self):
        self.assertEqual(self._infer("some random data", ["col_a", "col_b"]), "general")


class TestAudienceInference(unittest.TestCase):
    def _infer(self, desc):
        from agents.bi_reasoning_agent import _infer_audience
        return _infer_audience(desc)

    def test_executive_keywords(self):
        self.assertEqual(self._infer("board-level KPI overview for the CEO"), "executive")

    def test_analyst_keywords(self):
        self.assertEqual(self._infer("drill-down segment analysis for analysts"), "analyst")

    def test_default_is_analyst(self):
        self.assertEqual(self._infer("show me the data"), "analyst")


class TestDashboardTypeInference(unittest.TestCase):
    def _infer(self, desc, audience="analyst"):
        from agents.bi_reasoning_agent import _infer_dashboard_type
        return _infer_dashboard_type(desc, audience)

    def test_executive_audience_overrides(self):
        self.assertEqual(self._infer("some dashboard", audience="executive"), "executive")

    def test_kpi_keyword(self):
        self.assertEqual(self._infer("KPI scorecard overview"), "executive")

    def test_operational_keywords(self):
        self.assertEqual(self._infer("real-time operational monitor"), "operational")

    def test_storytelling_keywords(self):
        self.assertEqual(self._infer("narrative presentation of insights"), "storytelling")

    def test_default_is_analytical(self):
        self.assertEqual(self._infer("analyse the data"), "analytical")


# ---------------------------------------------------------------------------
# Deterministic reasoning tests
# ---------------------------------------------------------------------------


_FINANCE_SCHEMA = {
    "table_name": "Sales",
    "columns": [
        {"name": "SalesAmount", "dataType": "decimal"},
        {"name": "Profit", "dataType": "decimal"},
        {"name": "OrderDate", "dataType": "dateTime"},
        {"name": "Region", "dataType": "text"},
        {"name": "Category", "dataType": "text"},
    ],
}

_EMPTY_SCHEMA: dict = {"table_name": "Data", "columns": []}


class TestDeterministicReasoning(unittest.TestCase):
    def setUp(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def tearDown(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def _run(self, desc="Sales dashboard", schema=None, style="standard", clarifs=None):
        from agents.bi_reasoning_agent import _deterministic_reasoning
        return _deterministic_reasoning(desc, schema or _FINANCE_SCHEMA, style, clarifs or {})

    def test_returns_valid_bi_reasoning_result(self):
        from agents.schemas import BIReasoningResult
        result = self._run()
        self.assertIsInstance(result, BIReasoningResult)
        self.assertEqual(result.source, "deterministic")

    def test_always_has_at_least_one_page(self):
        result = self._run()
        self.assertGreaterEqual(len(result.recommended_pages), 1)
        self.assertEqual(result.recommended_pages[0].id, "overview")

    def test_amount_columns_become_kpis(self):
        result = self._run()
        kpi_names = [k.name for k in result.recommended_kpis]
        # SalesAmount and Profit are amount columns
        self.assertTrue(any("SalesAmount" in n or "Profit" in n for n in kpi_names))

    def test_rich_style_can_produce_multiple_pages(self):
        result = self._run(style="rich")
        # with category + date columns → up to 3 pages
        self.assertGreaterEqual(len(result.recommended_pages), 2)

    def test_minimal_style_produces_one_page(self):
        result = self._run(style="minimal")
        self.assertEqual(len(result.recommended_pages), 1)

    def test_date_column_triggers_trends_page_on_rich(self):
        result = self._run(style="rich")
        page_ids = [p.id for p in result.recommended_pages]
        self.assertIn("trends", page_ids)

    def test_empty_schema_does_not_raise(self):
        result = self._run(schema=_EMPTY_SCHEMA)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result.recommended_pages), 1)

    def test_clarification_overrides_audience(self):
        result = self._run(clarifs={"audience": "executive"})
        self.assertEqual(result.target_audience, "executive")

    def test_clarification_num_pages_2_3(self):
        result = self._run(style="standard", clarifs={"num_pages": "2-3"})
        # category column present → breakdown page added
        self.assertGreaterEqual(len(result.recommended_pages), 2)

    def test_suggested_analysis_not_empty(self):
        result = self._run()
        self.assertGreater(len(result.suggested_analysis), 0)

    def test_analytical_perspectives_not_empty(self):
        result = self._run()
        self.assertGreater(len(result.analytical_perspectives), 0)

    def test_decisions_logged_to_tracker(self):
        from utils.explainability import get_tracker
        self._run()
        # At least overview + KPI decisions logged
        self.assertGreater(len(get_tracker()), 0)


# ---------------------------------------------------------------------------
# BIReasoningAgent.run() integration tests
# ---------------------------------------------------------------------------


def _make_ctx(desc="Analyse sales data", schema=None, extra=None):
    """Build a minimal AgentContext for testing."""
    from agents.base import AgentContext
    from unittest.mock import MagicMock

    toolbox = MagicMock()
    ctx = AgentContext(
        business_description=desc,
        source_path=Path("/tmp/fake.xlsx"),
        toolbox=toolbox,
        project_name="test_project",
        pbip_root="/tmp/test_out",
        input_mode="create",
        extra=extra or {},
    )
    ctx.schema = schema
    return ctx


class TestBIReasoningAgentRun(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.setdefault("GOOGLE_API_KEY", "")
        from utils.explainability import get_tracker
        get_tracker().reset()

    def tearDown(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def test_run_always_returns_ok_true(self):
        """BIReasoningAgent is advisory — never blocks the pipeline."""
        from agents.bi_reasoning_agent import BIReasoningAgent
        ctx = _make_ctx()
        result = BIReasoningAgent(ctx).run()
        self.assertTrue(result.ok)

    def test_run_stores_bi_reasoning_in_ctx_extra(self):
        from agents.bi_reasoning_agent import BIReasoningAgent
        from agents.schemas import BIReasoningResult
        ctx = _make_ctx()
        BIReasoningAgent(ctx).run()
        self.assertIn("bi_reasoning", ctx.extra)
        self.assertIsInstance(ctx.extra["bi_reasoning"], BIReasoningResult)

    def test_run_result_data_contains_expected_keys(self):
        from agents.bi_reasoning_agent import BIReasoningAgent
        ctx = _make_ctx()
        result = BIReasoningAgent(ctx).run()
        for key in ("dashboard_goal", "dashboard_type", "target_audience",
                    "page_count", "kpi_count", "confidence", "source"):
            self.assertIn(key, result.data)

    def test_run_with_explicit_schema(self):
        from agents.bi_reasoning_agent import BIReasoningAgent
        ctx = _make_ctx(schema=_FINANCE_SCHEMA)
        result = BIReasoningAgent(ctx).run()
        self.assertTrue(result.ok)
        bi = ctx.extra["bi_reasoning"]
        self.assertGreaterEqual(len(bi.recommended_kpis), 1)

    def test_run_without_api_key_uses_deterministic(self):
        """With no API key the deterministic path is used."""
        from agents.bi_reasoning_agent import BIReasoningAgent
        with patch.dict(os.environ, {"GOOGLE_API_KEY": ""}):
            ctx = _make_ctx(schema=_FINANCE_SCHEMA)
            result = BIReasoningAgent(ctx).run()
        self.assertEqual(result.data["source"], "deterministic")

    def test_run_with_clarifications_in_extra(self):
        from agents.bi_reasoning_agent import BIReasoningAgent
        ctx = _make_ctx(
            schema=_FINANCE_SCHEMA,
            extra={"clarifications": {"audience": "executive", "num_pages": "2-3"}},
        )
        result = BIReasoningAgent(ctx).run()
        bi = ctx.extra["bi_reasoning"]
        self.assertEqual(bi.target_audience, "executive")

    def test_run_llm_path_mocked(self):
        """Mocked LLM path returns the structured result and is tagged 'llm'."""
        import agents.bi_reasoning_agent as brmod
        from agents.schemas import BIReasoningResult, KPIRecommendation, PageRecommendation

        fake_result = BIReasoningResult(
            dashboard_goal="Track revenue",
            target_audience="executive",
            dashboard_type="executive",
            recommended_pages=[PageRecommendation(id="p1", name="Overview", purpose="KPIs")],
            recommended_kpis=[KPIRecommendation(name="Revenue", why="Primary KPI")],
            confidence=0.95,
            source="llm",
        )
        with patch.object(brmod, "_llm_reasoning", return_value=fake_result):
            ctx = _make_ctx(schema=_FINANCE_SCHEMA)
            from agents.bi_reasoning_agent import BIReasoningAgent
            result = BIReasoningAgent(ctx).run()

        self.assertEqual(result.data["source"], "llm")
        self.assertAlmostEqual(result.data["confidence"], 0.95)

    def test_run_llm_failure_falls_back_to_deterministic(self):
        """When LLM returns None, deterministic fallback is used seamlessly."""
        import agents.bi_reasoning_agent as brmod
        with patch.object(brmod, "_llm_reasoning", return_value=None):
            ctx = _make_ctx(schema=_FINANCE_SCHEMA)
            from agents.bi_reasoning_agent import BIReasoningAgent
            result = BIReasoningAgent(ctx).run()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["source"], "deterministic")


# ---------------------------------------------------------------------------
# DataAnalyzerAgent._business_analysis() tests
# ---------------------------------------------------------------------------


class TestDataAnalyzerBusinessAnalysis(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.setdefault("GOOGLE_API_KEY", "")
        from utils.explainability import get_tracker
        get_tracker().reset()

    def tearDown(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def _run_business_analysis(self, columns):
        from agents.base import AgentContext
        from agents.data_analyzer_agent import DataAnalyzerAgent
        from unittest.mock import MagicMock
        toolbox = MagicMock()
        ctx = AgentContext(
            business_description="Sales data",
            source_path=Path("/tmp/fake.xlsx"),
            toolbox=toolbox,
            project_name="test",
            pbip_root="/tmp/out",
            input_mode="create",
            extra={},
        )
        agent = DataAnalyzerAgent(ctx)
        schema = {"table_name": "Sales", "columns": columns}
        return agent._business_analysis(schema)

    def test_returns_business_analysis_instance(self):
        from agents.schemas import BusinessAnalysis
        cols = [
            {"name": "SalesAmount", "dataType": "decimal"},
            {"name": "OrderDate", "dataType": "dateTime"},
        ]
        result = self._run_business_analysis(cols)
        self.assertIsInstance(result, BusinessAnalysis)

    def test_amount_column_produces_kpi_candidate(self):
        cols = [{"name": "Revenue", "dataType": "decimal"}]
        result = self._run_business_analysis(cols)
        self.assertTrue(len(result.potential_kpis) > 0 or len(result.important_measures) > 0)

    def test_date_column_produces_trend_indicator(self):
        cols = [
            {"name": "SalesAmount", "dataType": "decimal"},
            {"name": "OrderDate", "dataType": "dateTime"},
        ]
        result = self._run_business_analysis(cols)
        self.assertGreater(len(result.trend_indicators), 0)

    def test_empty_schema_does_not_raise(self):
        result = self._run_business_analysis([])
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# VisualPlannerAgent — VisualReasoning attached to each visual
# ---------------------------------------------------------------------------


class TestVisualPlannerAgentReasoning(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.setdefault("GOOGLE_API_KEY", "")

    def _make_plans(self):
        """Return a minimal list of candidate dicts (as VisualPlannerAgent expects)."""
        return [
            {"name": "SalesAmount", "kind": "barChart",
             "measure": "SalesAmount", "category": "Region"},
        ]

    def test_fallback_attaches_visual_reasoning(self):
        """The deterministic fallback attaches VisualReasoning to each visual."""
        import agents.visual_planner_agent as vpmod
        from agents.schemas import VisualReasoning

        with patch.object(vpmod, "_plan_with_llm", return_value=None):
            agent = vpmod.VisualPlannerAgent()
            plan = agent.plan(
                candidates=self._make_plans(),
                description="Sales by region",
                schema=_FINANCE_SCHEMA,
                measures=["SalesAmount"],
                report_style="standard",
            )

        # At least one visual should have reasoning attached
        visuals_with_reasoning = [
            v for p in plan.pages for v in p.visuals
            if v.visual_reasoning is not None
        ]
        self.assertGreater(len(visuals_with_reasoning), 0)

    def test_visual_reasoning_task_type_is_set(self):
        """task_type must be a non-empty string (one of the known kinds)."""
        import agents.visual_planner_agent as vpmod

        with patch.object(vpmod, "_plan_with_llm", return_value=None):
            agent = vpmod.VisualPlannerAgent()
            plan = agent.plan(
                candidates=self._make_plans(),
                description="Sales by region",
                schema=_FINANCE_SCHEMA,
                measures=["SalesAmount"],
                report_style="standard",
            )

        for page in plan.pages:
            for visual in page.visuals:
                if visual.visual_reasoning is not None:
                    self.assertNotEqual(visual.visual_reasoning.task_type, "")


# ---------------------------------------------------------------------------
# Orchestrator _write_decisions_log() tests
# ---------------------------------------------------------------------------


class TestOrchestratorWriteDecisionsLog(unittest.TestCase):
    def setUp(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def tearDown(self) -> None:
        from utils.explainability import get_tracker
        get_tracker().reset()

    def _make_orchestrator(self, tmp_path):
        import tempfile
        from agents.orchestrator import OrchestratorAgent
        return OrchestratorAgent(output_root=str(tmp_path))

    def _make_ctx(self, tmp_path):
        from agents.base import AgentContext
        from unittest.mock import MagicMock
        toolbox = MagicMock()
        ctx = AgentContext(
            business_description="Test",
            source_path=Path("/tmp/fake.xlsx"),
            toolbox=toolbox,
            project_name="test_project",
            pbip_root=str(tmp_path),
            input_mode="create",
            extra={},
        )
        return ctx

    def test_write_decisions_log_creates_file(self):
        import json
        import tempfile
        from utils.explainability import log_decision
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = self._make_orchestrator(tmp_path)
            ctx = self._make_ctx(tmp_path)

            # Record a decision so the file gets written
            log_decision("TestAgent", "visual_selection", "Bar Chart", "Best for comparison")

            orch._write_decisions_log(ctx)

            log_file = tmp_path / "decisions.log.json"
            self.assertTrue(log_file.exists(), "decisions.log.json should be created")
            with open(log_file) as f:
                data = json.load(f)
            self.assertEqual(data["project_name"], "test_project")
            self.assertEqual(data["decision_count"], 1)
            self.assertEqual(len(data["decisions"]), 1)

    def test_write_decisions_log_skips_when_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = self._make_orchestrator(tmp_path)
            ctx = self._make_ctx(tmp_path)

            # No decisions recorded
            orch._write_decisions_log(ctx)

            log_file = tmp_path / "decisions.log.json"
            self.assertFalse(log_file.exists(), "Should not write empty decisions log")

    def test_write_decisions_log_is_fail_safe(self):
        """Even if atomic_write_text raises, the method must not propagate."""
        import tempfile
        from utils.explainability import log_decision
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            orch = self._make_orchestrator(tmp_path)
            ctx = self._make_ctx(tmp_path)
            log_decision("A", "t", "s", "r")
            with patch("agents.orchestrator.atomic_write_text", side_effect=OSError("disk full")):
                try:
                    orch._write_decisions_log(ctx)
                except Exception as exc:  # pragma: no cover
                    self.fail(f"_write_decisions_log raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# Regression guard: baseline schemas still present
# ---------------------------------------------------------------------------


class TestSchemasBackwardsCompatibility(unittest.TestCase):
    """Ensure new additions to schemas.py didn't break existing exports."""

    def test_existing_exports_still_present(self):
        from agents import schemas
        for name in ("BuildPlan", "PlanStep", "VisualPlan", "ReportPlan",
                      "PagePlan", "SchemaResult", "BuildSpec",
                      "ValidationResult"):
            self.assertTrue(hasattr(schemas, name), f"schemas.{name} missing")

    def test_new_exports_present(self):
        from agents import schemas
        for name in ("BIReasoningResult", "VisualReasoning", "BusinessAnalysis",
                      "PageRecommendation", "KPIRecommendation"):
            self.assertTrue(hasattr(schemas, name), f"schemas.{name} missing")

    def test_visual_plan_accepts_visual_reasoning_as_optional(self):
        from agents.schemas import VisualPlan
        # No visual_reasoning → should still work
        vp = VisualPlan(name="TestVisual", kind="barChart", category="c")
        self.assertIsNone(vp.visual_reasoning)


if __name__ == "__main__":
    unittest.main()
