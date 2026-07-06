"""Phase 7 tests — high-level MCP tools (generate_pbip / edit_pbip / add_*).

These tests cover the new orchestration helpers in
``mcp_server/highlevel.py``. The fabric deploy tool is exercised via a
subprocess mock so no fab CLI is required to run them.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server import highlevel as hl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(p: Path, rows: list[list]) -> None:
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for row in rows:
            w.writerow(row)


def _sample_csv(p: Path) -> Path:
    _write_csv(p, [
        ["Date",        "Region",    "Product",    "Sales"],
        ["2024-01-01", "North",     "Widget",     "100.50"],
        ["2024-01-02", "South",     "Gadget",     "200.75"],
        ["2024-02-01", "North",     "Widget",     "150.25"],
        ["2024-02-02", "East",      "Sprocket",   "300.00"],
        ["2024-03-15", "West",      "Widget",     "175.00"],
    ])
    return p


def _make_pbip(root: Path, name: str = "Demo") -> Path:
    """Create a minimal valid PBIP project on disk for add_* tests."""
    sm  = root / f"{name}.SemanticModel"
    rep = root / f"{name}.Report"
    (sm / "definition" / "tables").mkdir(parents=True, exist_ok=True)
    (rep / "definition" / "pages").mkdir(parents=True, exist_ok=True)
    (sm / "definition.pbism").write_text("{}", encoding="utf-8")
    (rep / "definition.pbir").write_text("{}", encoding="utf-8")
    # Write a tiny table file so write_tmdl_measures can find a target table
    (sm / "definition" / "tables" / "Sales.tmdl").write_text(
        "table Sales\n\tlineageTag: abc\n\n\tcolumn Amount\n\t\tdataType: double\n",
        encoding="utf-8",
    )
    return root


def _make_pbip_with_page(root: Path, name: str = "Demo",
                         page_id: str = "page1") -> Path:
    _make_pbip(root, name)
    rep_def = root / f"{name}.Report" / "definition"
    pdir = rep_def / "pages" / page_id
    (pdir / "visuals").mkdir(parents=True, exist_ok=True)
    (pdir / "page.json").write_text(json.dumps({
        "$schema": "x", "name": page_id, "displayName": page_id,
        "width": 1280, "height": 720,
    }), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# generate_pbip
# ---------------------------------------------------------------------------

class TestGeneratePbip(unittest.TestCase):

    def test_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "data.parquet"
            src.write_bytes(b"\x00")
            res = hl.generate_pbip(str(src), "build", output_root=str(tdp))
            self.assertFalse(res["ok"])
            self.assertIn("Unsupported", res["message"])

    def test_missing_source(self):
        with tempfile.TemporaryDirectory() as td:
            res = hl.generate_pbip(str(Path(td) / "nope.csv"), "build",
                                   output_root=td)
            self.assertFalse(res["ok"])
            self.assertIn("not found", res["message"].lower())

    def test_orchestrator_failure_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            fake_report = MagicMock(
                ok=False, project_name="Bad",
                pbip_root=str(tdp / "Bad"),
                validation=None, error="kaboom",
                steps=[{"agent": "X", "ok": False, "message": "kaboom",
                        "errors": ["kaboom"]}],
            )
            with patch("agents.orchestrator.OrchestratorAgent") as Orch:
                Orch.return_value.run.return_value = fake_report
                res = hl.generate_pbip(str(src), "build", output_root=str(tdp))
            self.assertFalse(res["ok"])
            self.assertIn("Build failed", res["message"])

    def test_orchestrator_success(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            fake_report = MagicMock(
                ok=True, project_name="Sales",
                pbip_root=str(tdp / "Sales"),
                validation={"ok": True, "errors": []},
                error=None,
                steps=[{"agent": "Schema", "ok": True,
                        "message": "ok", "errors": []}],
            )
            with patch("agents.orchestrator.OrchestratorAgent") as Orch:
                Orch.return_value.run.return_value = fake_report
                res = hl.generate_pbip(str(src), "Sales by region",
                                       output_root=str(tdp))
            self.assertTrue(res["ok"])
            self.assertEqual(res["data"]["project_name"], "Sales")
            self.assertEqual(res["data"]["validation"]["ok"], True)

    def test_metadata_summarizes_per_agent_data(self):
        """generate_pbip's data["metadata"] pulls insights/KPI/count fields
        out of each step's own AgentResult.data -- previously computed by
        the pipeline but dropped before reaching the caller (only
        agent/ok/message survived)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            fake_report = MagicMock(
                ok=True, project_name="Sales",
                pbip_root=str(tdp / "Sales"),
                validation={"ok": True, "errors": []},
                error=None,
                steps=[
                    {"agent": "SchemaAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {"column_count": 5}},
                    {"agent": "DataAnalyzerAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {"quality_score": 92.0, "potential_kpi_count": 3}},
                    {"agent": "DAXAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {"count": 7, "folders": ["KPI"]}},
                    {"agent": "ReportAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {"page_count": 2, "visual_count": 10}},
                    {"agent": "ReportReviewerAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {"score": 88}},
                    {"agent": "InsightsAgent", "ok": True, "message": "ok",
                     "errors": [], "data": {
                         "anomaly_count": 1, "segment_count": 2,
                         "underperformer_count": 0, "trend_count": 1,
                         "kpi_suggestion_count": 3,
                     }},
                ],
            )
            with patch("agents.orchestrator.OrchestratorAgent") as Orch:
                Orch.return_value.run.return_value = fake_report
                res = hl.generate_pbip(str(src), "Sales by region",
                                       output_root=str(tdp))
            meta = res["data"]["metadata"]
            self.assertEqual(meta["table_count"], 1)
            self.assertEqual(meta["column_count"], 5)
            self.assertEqual(meta["quality_score"], 92.0)
            self.assertEqual(meta["potential_kpi_count"], 3)
            self.assertEqual(meta["measure_count"], 7)
            self.assertEqual(meta["measure_folders"], ["KPI"])
            self.assertEqual(meta["page_count"], 2)
            self.assertEqual(meta["visual_count"], 10)
            self.assertEqual(meta["review_score"], 88)
            self.assertEqual(meta["anomaly_count"], 1)
            self.assertEqual(meta["kpi_suggestion_count"], 3)


# ---------------------------------------------------------------------------
# edit_pbip
# ---------------------------------------------------------------------------

class TestEditPbip(unittest.TestCase):

    def test_missing_pbip_dir(self):
        with tempfile.TemporaryDirectory() as td:
            res = hl.edit_pbip(str(Path(td) / "missing"), "do thing")
            self.assertFalse(res["ok"])
            self.assertIn("not found", res["message"].lower())

    def test_not_a_pbip(self):
        with tempfile.TemporaryDirectory() as td:
            # has no .SemanticModel inside
            res = hl.edit_pbip(td, "do thing")
            self.assertFalse(res["ok"])

    def test_runs_orchestrator(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Acme")
            fake_report = MagicMock(
                ok=True, project_name="Acme",
                pbip_root=str(tdp / "Acme"),
                validation={"ok": True},
                error=None,
                steps=[{"agent": "Edit", "ok": True, "message": "ok",
                        "errors": []}],
            )
            with patch("agents.orchestrator.OrchestratorAgent") as Orch:
                Orch.return_value.run.return_value = fake_report
                res = hl.edit_pbip(str(tdp), "add YoY measure")
            self.assertTrue(res["ok"])
            Orch.return_value.run.assert_called_once()
            kwargs = Orch.return_value.run.call_args.kwargs
            self.assertEqual(kwargs["input_mode"], "edit_pbip")


# ---------------------------------------------------------------------------
# add_measure
# ---------------------------------------------------------------------------

class TestAddMeasure(unittest.TestCase):

    def test_appends_to_table(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Demo")
            res = hl.add_measure(
                str(tdp), "Total Sales",
                "SUM('Sales'[Amount])",
                table="Sales",
                format_string="#,0.00",
                display_folder="Aggregations",
            )
            self.assertTrue(res["ok"], res)
            txt = (tdp / "Demo.SemanticModel" / "definition" /
                   "tables" / "Sales.tmdl").read_text(encoding="utf-8")
            self.assertIn("measure 'Total Sales'", txt)
            self.assertIn("formatString: #,0.00", txt)

    def test_missing_pbip(self):
        with tempfile.TemporaryDirectory() as td:
            res = hl.add_measure(str(Path(td) / "nope"), "M", "1")
            self.assertFalse(res["ok"])

    def test_description_ignored_silently(self):
        """description kwarg is accepted but not written (no TMDL field)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Demo")
            res = hl.add_measure(
                str(tdp), "X", "1", table="Sales",
                description="should be dropped, not crash",
            )
            self.assertTrue(res["ok"], res)


# ---------------------------------------------------------------------------
# add_page
# ---------------------------------------------------------------------------

class TestAddPage(unittest.TestCase):

    def test_adds_empty_page(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Demo")
            res = hl.add_page(str(tdp), "Overview")
            self.assertTrue(res["ok"], res)
            pid = res["data"]["page_id"]
            self.assertTrue((tdp / "Demo.Report" / "definition" /
                             "pages" / pid / "page.json").is_file())
            pmeta = json.loads((tdp / "Demo.Report" / "definition" /
                                "pages" / "pages.json").read_text(encoding="utf-8"))
            self.assertIn(pid, pmeta.get("pageOrder", []))

    def test_appends_to_existing_pages_index(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip_with_page(tdp, "Demo", "first")
            # seed pages.json with one entry
            from mcp_server import pbir_generator as pb
            from utils import atomic_write_json
            atomic_write_json(
                tdp / "Demo.Report" / "definition" / "pages" / "pages.json",
                pb.pages_metadata(["first"]),
            )
            res = hl.add_page(str(tdp), "Second", page_id="second")
            self.assertTrue(res["ok"], res)
            order = res["data"]["page_order"]
            self.assertEqual(order, ["first", "second"])


# ---------------------------------------------------------------------------
# add_visual
# ---------------------------------------------------------------------------

class TestAddVisual(unittest.TestCase):

    def test_writes_visual_json(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip_with_page(tdp, "Demo", "page1")
            res = hl.add_visual(
                str(tdp),
                page_id="page1",
                visual_type="card",
                query_state={"select": [
                    {"measure": {"table": "Sales", "name": "Total"}},
                ]},
                title="KPI",
            )
            self.assertTrue(res["ok"], res)
            vpath = Path(res["data"]["visual_path"])
            self.assertTrue(vpath.is_file())
            payload = json.loads(vpath.read_text(encoding="utf-8"))
            self.assertEqual(payload["visual"]["visualType"], "card")

    def test_missing_page(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Demo")  # no page
            res = hl.add_visual(
                str(tdp), page_id="bogus",
                visual_type="card", query_state={"select": []},
            )
            self.assertFalse(res["ok"])
            self.assertIn("not found", res["message"].lower())


# ---------------------------------------------------------------------------
# deploy_to_fabric  (forwards to fabric.deploy.deploy)
# ---------------------------------------------------------------------------

class TestDeployToFabric(unittest.TestCase):

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Demo")
            res = hl.deploy_to_fabric(str(tdp), "WS-A", mode="auto",
                                      dry_run=True)
            self.assertTrue(res["ok"], res)
            self.assertTrue(res["data"]["dry_run"])
            actions = res["data"]["actions"]
            kinds = [a["kind"] for a in actions]
            self.assertIn("SemanticModel", kinds)

    def test_propagates_error(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res = hl.deploy_to_fabric(str(tdp), "WS-A", dry_run=True)
            self.assertFalse(res["ok"])
            self.assertTrue(res["errors"])


# ---------------------------------------------------------------------------
# suggest_measures
# ---------------------------------------------------------------------------

class TestSuggestMeasures(unittest.TestCase):

    def test_auto_mode_from_csv(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            res = hl.suggest_measures(str(src))
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["mode"], "auto")
            measures = res["data"]["measures"]
            self.assertGreater(len(measures), 0)
            self.assertTrue(any("Total" in m["name"] for m in measures))

    def test_pattern_mode_requires_base_name(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            res = hl.suggest_measures(str(src), pattern_types=["ytd"])
            self.assertFalse(res["ok"])
            self.assertIn("base_name", res["message"])

    def test_pattern_mode_generates_ytd(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = _sample_csv(tdp / "data.csv")
            res = hl.suggest_measures(
                str(src),
                pattern_types=["ytd", "yoy_pct"],
                base_name="Total Sales",
                base_expr="[Total Sales]",
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["data"]["mode"], "pattern")
            names = [m["name"] for m in res["data"]["measures"]]
            self.assertTrue(any("YTD" in n for n in names))

    def test_unsupported_source(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "binary.bin"
            src.write_bytes(b"\x00")
            res = hl.suggest_measures(str(src))
            self.assertFalse(res["ok"])

    def test_missing_source(self):
        res = hl.suggest_measures("/no/such/path.csv")
        self.assertFalse(res["ok"])


# ---------------------------------------------------------------------------
# _resolve_project / _find_single_project utility
# ---------------------------------------------------------------------------

class TestResolveProject(unittest.TestCase):

    def test_resolves_root(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "MyProj")
            name, root = hl._resolve_project(str(tdp))
            self.assertEqual(name, "MyProj")
            self.assertEqual(root, tdp.resolve())

    def test_resolves_when_pointed_at_semantic_model(self):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _make_pbip(tdp, "Acme")
            name, root = hl._resolve_project(str(tdp / "Acme.SemanticModel"))
            self.assertEqual(name, "Acme")
            self.assertEqual(root, tdp.resolve())

    def test_no_pbip(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                hl._resolve_project(td)

    def test_missing_path(self):
        with self.assertRaises(ValueError):
            hl._resolve_project("/no/such/dir")


if __name__ == "__main__":
    unittest.main()
