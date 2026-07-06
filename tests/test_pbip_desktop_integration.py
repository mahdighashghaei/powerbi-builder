"""Integration test: full pipeline -> Power BI Desktop-openable PBIP.

This test runs the *real* OrchestratorAgent (no mocks, no LLM -- the
deterministic offline path) and verifies that:

  1. The generated PBIP folder is structurally valid and Power BI Desktop
     would accept it (valid TMDL, valid PBIR, correct JSON manifests, no
     ghost column references).

  2. The Optimization-Mode pipeline actually ran: decisions.log.json
     contains evidence of multi-candidate scoring for both the DAX and
     schema strategy selection steps.

  3. All Production Hardening guarantees hold: structured review issues,
     explainability decisions written, build.spec.json present and valid.

Run standalone::

    python -m pytest tests/test_pbip_desktop_integration.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Force the deterministic offline path (no LLM) so the test is reproducible.
os.environ["GOOGLE_API_KEY"] = ""


# ---------------------------------------------------------------------------
# Fixture CSV: amount, qty, date, region and product columns so DAXAgent
# exercises revenue/qty/time-intel branches across all 3 candidate strategies.
# ---------------------------------------------------------------------------
_CSV_CONTENT = (
    "OrderDate,Region,Product,Quantity,Revenue,Cost\n"
    "2024-01-05,North,Widget,10,2505.00,1200.00\n"
    "2024-01-07,South,Gadget,5,999.90,500.00\n"
    "2024-02-10,East,Widget,8,2000.00,960.00\n"
    "2024-02-15,West,Gadget,12,2398.80,1100.00\n"
    "2024-03-01,North,Gadget,3,899.70,420.00\n"
    "2024-03-12,South,Widget,15,3750.00,1800.00\n"
    "2024-04-02,East,Gadget,7,1399.86,680.00\n"
    "2024-04-18,West,Widget,20,5000.00,2400.00\n"
)
_DESCRIPTION = (
    "Monthly sales performance by region: revenue, cost, and quantity trends "
    "with year-over-year comparison"
)


class TestPbipDesktopIntegration(unittest.TestCase):
    """Full pipeline => valid Power BI Desktop PBIP with Optimization Mode."""

    # ------------------------------------------------------------------
    # One-time setup: run the pipeline once, share result across all tests
    # ------------------------------------------------------------------

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        out = Path(cls._tmp.name) / "out"
        csv = Path(cls._tmp.name) / "sales_integration.csv"
        csv.write_text(_CSV_CONTENT, encoding="utf-8")

        from agents.orchestrator import OrchestratorAgent

        orchestrator = OrchestratorAgent(output_root=out)
        cls.report = orchestrator.run(
            source_path=csv,
            business_description=_DESCRIPTION,
            project_name="sales_integration",
        )

        candidates = [
            p for p in out.iterdir()
            if p.is_dir() and any(p.glob("*.SemanticModel"))
        ]
        cls.project = candidates[0] if candidates else out
        cls.pbip_root = cls.project

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # ==================================================================
    # Group 1 -- build completed
    # ==================================================================

    def test_01_build_ok(self) -> None:
        """Pipeline must finish without a hard error."""
        self.assertTrue(
            self.report.ok,
            "OrchestratorAgent returned ok=False. error={!r}\nsteps: {}".format(
                self.report.error,
                [s["message"] for s in self.report.steps if not s["ok"]],
            ),
        )

    def test_02_validation_passed(self) -> None:
        """Validator must not report hard failures."""
        val = self.report.validation or {}
        self.assertTrue(
            val.get("ok", False),
            "ValidatorAgent failures: {}".format(val),
        )

    # ==================================================================
    # Group 2 -- Power BI Desktop structural requirements
    # ==================================================================

    def test_03_pbip_entry_file_exists(self) -> None:
        """.pbip manifest must be present (Desktop opening entrypoint)."""
        pbip = next(self.project.glob("*.pbip"), None)
        self.assertIsNotNone(pbip, "No .pbip entry file found")

    def test_04_pbip_manifest_is_valid_json(self) -> None:
        """The .pbip file must be parseable JSON."""
        pbip = next(self.project.glob("*.pbip"))
        data = json.loads(pbip.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict, ".pbip is not a JSON object")

    def test_05_semantic_model_folder_present(self) -> None:
        """*.SemanticModel directory must exist."""
        sm = next(self.project.glob("*.SemanticModel"), None)
        self.assertIsNotNone(sm, "No .SemanticModel folder found")

    def test_06_semantic_model_core_tmdl_files(self) -> None:
        """database.tmdl and model.tmdl must both be present."""
        sm = next(self.project.glob("*.SemanticModel"))
        defn = sm / "definition"
        self.assertTrue((defn / "database.tmdl").is_file(), "database.tmdl missing")
        self.assertTrue((defn / "model.tmdl").is_file(), "model.tmdl missing")

    def test_07_at_least_one_table_tmdl(self) -> None:
        """At least one table .tmdl file must exist in tables/."""
        sm = next(self.project.glob("*.SemanticModel"))
        tables_dir = sm / "definition" / "tables"
        tmdl_files = list(tables_dir.glob("*.tmdl")) if tables_dir.is_dir() else []
        self.assertGreater(len(tmdl_files), 0, "No TMDL table files found")

    def test_08_measures_generated_in_tmdl(self) -> None:
        """TMDL must contain measure definitions (at least 3)."""
        from utils.tmdl_parser import read_semantic_model

        sm = next(self.project.glob("*.SemanticModel"))
        model = read_semantic_model(sm)
        measure_names = model.get("measure_names", [])
        self.assertGreaterEqual(
            len(measure_names), 3,
            "Expected >=3 measures, got: {}".format(measure_names),
        )

    def test_09_measures_use_display_folders(self) -> None:
        """Optimization Mode ensures measures are grouped into named folders."""
        sm = next(self.project.glob("*.SemanticModel"))
        tables_dir = sm / "definition" / "tables"
        combined = "\n".join(
            f.read_text(encoding="utf-8") for f in tables_dir.glob("*.tmdl")
        )
        self.assertIn(
            "displayFolder", combined,
            "No displayFolder in any TMDL -- measure grouping missing",
        )

    def test_10_report_folder_present(self) -> None:
        """*.Report directory must exist."""
        rpt = next(self.project.glob("*.Report"), None)
        self.assertIsNotNone(rpt, "No .Report folder found")

    def test_11_report_json_valid(self) -> None:
        """report.json must be valid JSON with a themeCollection key."""
        rpt = next(self.project.glob("*.Report"))
        rjson = rpt / "definition" / "report.json"
        self.assertTrue(rjson.is_file(), "report.json missing")
        data = json.loads(rjson.read_text(encoding="utf-8"))
        self.assertIn("themeCollection", data, "report.json missing themeCollection")

    def test_12_pages_json_valid(self) -> None:
        """pages.json must list at least one page."""
        rpt = next(self.project.glob("*.Report"))
        pages_json = rpt / "definition" / "pages" / "pages.json"
        self.assertTrue(pages_json.is_file(), "pages.json missing")
        data = json.loads(pages_json.read_text(encoding="utf-8"))
        self.assertIn("pageOrder", data, "pages.json missing pageOrder")
        self.assertGreater(len(data["pageOrder"]), 0, "pages.json lists no pages")

    def test_13_each_page_has_at_least_one_visual(self) -> None:
        """Every page directory must contain at least one visual."""
        rpt = next(self.project.glob("*.Report"))
        pages_dir = rpt / "definition" / "pages"
        page_folders = [p for p in pages_dir.iterdir() if p.is_dir()]
        self.assertGreater(len(page_folders), 0, "No page folders found")
        for page in page_folders:
            visuals = list((page / "visuals").glob("*"))
            self.assertGreater(
                len(visuals), 0,
                "Page {!r} has no visuals -- Desktop would show blank page".format(
                    page.name
                ),
            )

    def test_14_no_ghost_column_references(self) -> None:
        """Ghost refs cause Desktop import errors -- must be zero."""
        from adk.tools.review_tools import check_visual_references

        result = check_visual_references(str(self.project))
        self.assertTrue(
            result["ok"], "check_visual_references failed: {}".format(result)
        )
        ghost_refs = result.get("ghost_refs", [])
        self.assertEqual(
            len(ghost_refs), 0,
            "Ghost references detected (would break Desktop): {}".format(ghost_refs),
        )

    # ==================================================================
    # Group 3 -- Optimization Mode evidence (multi-candidate scoring)
    # ==================================================================

    def test_15_decisions_log_written(self) -> None:
        """decisions.log.json must be present -- explainability is mandatory."""
        log_file = self.pbip_root / "decisions.log.json"
        self.assertTrue(log_file.is_file(), "decisions.log.json not written")

    def test_16_decisions_log_is_valid_json(self) -> None:
        """decisions.log.json must parse and contain at least one decision."""
        log_file = self.pbip_root / "decisions.log.json"
        data = json.loads(log_file.read_text(encoding="utf-8"))
        self.assertIn("decisions", data)
        self.assertGreater(len(data["decisions"]), 0, "decisions list is empty")

    def test_17_dax_candidate_selection_logged(self) -> None:
        """DAXAgent must log which of 3 strategies won the scoring round.

        The 'candidate_selection' subject is written when it picks among
        revenue_first / operational / time_intelligence candidates.
        """
        data = json.loads(
            (self.pbip_root / "decisions.log.json").read_text(encoding="utf-8")
        )
        dax_sel = [
            d for d in data["decisions"]
            if d.get("agent") in ("DAXAgent", "dax_agent")
            and d.get("subject") == "candidate_selection"
        ]
        self.assertGreater(
            len(dax_sel), 0,
            "No DAX candidate_selection in decisions.log.json -- "
            "multi-hypothesis DAX scoring did not run",
        )
        extra = dax_sel[0].get("extra", {})
        self.assertIn("selected", extra, "DAX selection missing 'selected'")
        self.assertIn("rejected", extra, "DAX selection missing 'rejected'")
        self.assertGreaterEqual(
            len(extra["rejected"]), 2,
            "Expected >=2 rejected DAX candidates, got: {}".format(extra["rejected"]),
        )

    def test_18_schema_strategy_selection_logged(self) -> None:
        """SchemaAgent must log which of 3 column strategies won.

        The 'schema_strategy_selection' subject is written when it picks among
        conservative / analytical / categorical strategies.
        """
        data = json.loads(
            (self.pbip_root / "decisions.log.json").read_text(encoding="utf-8")
        )
        schema_sel = [
            d for d in data["decisions"]
            if d.get("agent") in ("SchemaAgent", "schema_agent")
            and d.get("subject") == "schema_strategy_selection"
        ]
        self.assertGreater(
            len(schema_sel), 0,
            "No schema_strategy_selection in decisions.log.json -- "
            "multi-hypothesis schema scoring did not run",
        )
        extra = schema_sel[0].get("extra", {})
        self.assertIn("selected", extra, "Schema selection missing 'selected'")
        self.assertIn("rejected", extra, "Schema selection missing 'rejected'")

    # ==================================================================
    # Group 4 -- Production Hardening artifacts
    # ==================================================================

    def test_19_readme_written(self) -> None:
        """README.md must be present in the project directory."""
        self.assertTrue(
            (self.pbip_root / "README.md").is_file(), "README.md missing"
        )

    def test_20_build_spec_written_and_valid(self) -> None:
        """build.spec.json must be present with required fields."""
        spec_file = self.pbip_root / "build.spec.json"
        self.assertTrue(spec_file.is_file(), "build.spec.json missing")
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        self.assertEqual(spec.get("schema_version"), "1.0", "Wrong schema_version")
        self.assertTrue(spec.get("project_name"), "project_name empty in spec")
        self.assertIsInstance(spec.get("measures"), list, "measures not a list in spec")
        self.assertGreater(len(spec.get("trajectory", [])), 0, "trajectory empty")

    def test_21_build_spec_has_dax_and_schema_agents(self) -> None:
        """build.spec.json trajectory must show DAXAgent and SchemaAgent ran."""
        spec = json.loads(
            (self.pbip_root / "build.spec.json").read_text(encoding="utf-8")
        )
        agents = {s.get("agent", "") for s in spec.get("trajectory", [])}
        self.assertIn("DAXAgent", agents, "DAXAgent missing from trajectory")
        self.assertIn("SchemaAgent", agents, "SchemaAgent missing from trajectory")

    def test_22_review_issues_are_structured_dicts(self) -> None:
        """ReportReviewerAgent issues must carry the full Fix-1 schema.

        Every issue dict must have: severity, agent_responsible, message, context.
        """
        reviewer_steps = [
            s for s in self.report.steps
            if s.get("agent") == "ReportReviewerAgent"
        ]
        if not reviewer_steps:
            self.skipTest("ReportReviewerAgent step not in report")
        issues = reviewer_steps[0].get("data", {}).get("issues", []) or []
        for issue in issues:
            self.assertIsInstance(issue, dict, "Issue not a dict: {!r}".format(issue))
            for key in ("severity", "agent_responsible", "message", "context"):
                self.assertIn(
                    key, issue,
                    "Issue missing key {!r}: {!r}".format(key, issue),
                )

    def test_23_visual_planner_agent_ran(self) -> None:
        """VisualPlannerAgent (now a BaseAgent subclass) must have produced output.

        VisualPlannerAgent runs as a sub-step inside ReportAgent (not a
        top-level orchestrator step), so we verify it ran by checking that
        ReportAgent reports a non-zero visual_count -- that count comes
        directly from the plan VisualPlannerAgent wrote into ctx.extra.
        """
        report_steps = [
            s for s in self.report.steps if s.get("agent") == "ReportAgent"
        ]
        self.assertGreater(len(report_steps), 0, "ReportAgent step not found")
        data = report_steps[0].get("data", {})
        visual_count = data.get("visual_count", 0)
        self.assertGreater(
            visual_count, 0,
            "ReportAgent.data['visual_count'] == 0 -- VisualPlannerAgent "
            "produced no plan or its output was ignored. data={}".format(data),
        )

    def test_24_validate_pbip_structure_passes(self) -> None:
        """Built-in PbipToolbox validator must clear for Desktop compatibility."""
        from mcp_server.server import PbipToolbox

        tb = PbipToolbox(str(self.pbip_root.parent))
        res = tb.validate_pbip_structure(str(self.project))
        self.assertTrue(
            res.ok, "validate_pbip_structure failed: {}".format(res.errors)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
