"""Tests for the new agents (Phase 9): Data Analyzer, Cleaner, Planner,
Report Reviewer, Status, and the edit/SQL Server tools.

These tests exercise the ADK tool functions directly (no LLM, no Runner)
and the legacy agent classes with a synthetic AgentContext where needed.
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class TestProfileDataFile(unittest.TestCase):
    """Phase 1 — data profiling foundation."""

    def test_profile_detects_nulls_and_quality(self):
        from mcp_server.schema_inference import profile_data_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.csv"
            # column B has 50% nulls
            p.write_text("A,B,C\n1,,x\n2,,y\n3,5,z\n4,6,w\n", encoding="utf-8")
            r = profile_data_file(p)
            self.assertTrue(r["ok"] if "ok" in r else True)
            cols = r.get("quality", {}).get("columns", {})
            self.assertIn("B", cols)
            self.assertGreater(cols["B"].get("null_pct", 0), 0)
            self.assertIn("quality_score", r)

    def test_profile_detects_duplicates(self):
        from mcp_server.schema_inference import profile_data_file
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dup.csv"
            p.write_text("A\n1\n1\n2\n", encoding="utf-8")
            r = profile_data_file(p)
            self.assertGreater(r["quality"].get("duplicate_rows", 0), 0)


class TestDataAnalyzerTools(unittest.TestCase):
    """Phase 2 — Data Analyzer ADK tools."""

    def test_analyze_data_returns_profile_and_questions(self):
        from adk.tools.data_analysis_tools import analyze_data
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.csv"
            p.write_text("A,B\n1,5\n2,\n3,\n4,\n5,\n", encoding="utf-8")
            r = analyze_data(str(p))
            self.assertTrue(r["ok"])
            self.assertIn("quality_score", r)
            self.assertIn("questions", r)
            self.assertIn("answers", r)
            # B has >40% nulls → should produce a question
            qids = [q["id"] for q in r["questions"]]
            self.assertTrue(any("B" in qid for qid in qids))

    def test_verify_analysis_passes(self):
        from adk.tools.data_analysis_tools import analyze_data, verify_analysis
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.csv"
            p.write_text("A,B\n1,5\n2,6\n3,7\n", encoding="utf-8")
            r = analyze_data(str(p))
            v = verify_analysis(r, str(p))
            self.assertTrue(v["ok"])
            self.assertTrue(v["verified"])

    def test_ask_user_records_question(self):
        from adk.tools.data_analysis_tools import ask_user
        ctx = MagicMock()
        ctx.state = {}
        r = ask_user("Drop column X?", ["drop", "keep"], tool_context=ctx)
        self.assertTrue(r["ok"])
        self.assertEqual(ctx.state["pending_question"]["question"], "Drop column X?")


class TestDataCleanerTools(unittest.TestCase):
    """Phase 3 — Data Cleaner ADK tools."""

    def test_plan_cleaning_drops_high_null_column(self):
        from adk.tools.data_cleaning_tools import plan_cleaning
        profile = {
            "quality_score": 50,
            "issues": ["B has 70% nulls"],
            "quality": {"columns": {"B": {"null_pct": 70, "distinct_count": 2}}},
            "schema": {"columns": [{"name": "B", "dataType": "string"}]},
            "answers": {},
        }
        r = plan_cleaning(profile)
        self.assertTrue(r["ok"])
        actions = [s["action"] for s in r["plan"] if s["column"] == "B"]
        self.assertIn("drop_column", actions)

    def test_apply_cleaning_writes_cleaned_file(self):
        from adk.tools.data_cleaning_tools import apply_cleaning
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.csv"
            p.write_text("A,B\n1,5\n1,5\n2,6\n", encoding="utf-8")
            plan = [{"column": "*", "action": "dedupe", "params": {}}]
            r = apply_cleaning(str(p), plan, output_root=td)
            self.assertTrue(r["ok"])
            self.assertTrue(Path(r["cleaned_path"]).is_file())


class TestPlannerTools(unittest.TestCase):
    """Phase 4 — Planner ADK tools."""

    def test_create_build_plan_has_core_phases(self):
        from adk.tools.planner_tools import create_build_plan
        r = create_build_plan("sales dashboard")
        phases = [s["phase"] for s in r["plan"]]
        self.assertIn("schema", phases)
        self.assertIn("dax", phases)
        self.assertIn("report", phases)
        self.assertIn("validate", phases)

    def test_create_build_plan_adds_clean_when_quality_low(self):
        from adk.tools.planner_tools import create_build_plan
        r = create_build_plan("test", data_profile={"quality_score": 50, "issues": ["x"], "schema": {"columns": [{}]}})
        phases = [s["phase"] for s in r["plan"]]
        self.assertIn("clean", phases)

    def test_validate_plan_rejects_unknown_phase(self):
        from adk.tools.planner_tools import validate_plan
        r = validate_plan([{"phase": "bogus", "agent": "X"}])
        self.assertFalse(r["valid"])


class TestReviewTools(unittest.TestCase):
    """Phase 5 — Report Reviewer ADK tools."""

    def test_review_report_on_generated_project(self):
        from adk.tools.review_tools import review_report
        # Build a minimal PBIP in a temp dir
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Proj"
            sm = root / "Proj.SemanticModel" / "definition" / "tables"
            sm.mkdir(parents=True)
            (sm / "Sales.tmdl").write_text("table Sales\n\tcolumn Sales = 1\n\tmeasure 'Total Sales' = SUM(Sales[Sales])\n", encoding="utf-8")
            rep = root / "Proj.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
            rep.mkdir(parents=True)
            vjson = {"visualType": "card", "x": 0, "y": 0, "width": 100, "height": 100,
                     "queryState": {"query": {"measure": "Total Sales"}}}
            (rep / "visual.json").write_text(json.dumps(vjson), encoding="utf-8")
            r = review_report(str(root))
            self.assertTrue(r["ok"])
            self.assertIn("score", r)
            self.assertEqual(r["ghost_count"], 0)  # Total Sales exists

    def test_review_detects_ghost_reference(self):
        from adk.tools.review_tools import check_visual_references
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Proj"
            sm = root / "Proj.SemanticModel" / "definition" / "tables"
            sm.mkdir(parents=True)
            (sm / "Sales.tmdl").write_text("table Sales\n\tcolumn Sales = 1\n", encoding="utf-8")
            rep = root / "Proj.Report" / "definition" / "pages" / "p1" / "visuals" / "v1"
            rep.mkdir(parents=True)
            # references a measure that doesn't exist
            vjson = {"queryState": {"query": {"measure": "Ghost Measure"}}}
            (rep / "visual.json").write_text(json.dumps(vjson), encoding="utf-8")
            r = check_visual_references(str(root))
            self.assertTrue(r["ok"])
            self.assertGreater(r["ghost_count"], 0)


class TestStatusTools(unittest.TestCase):
    """Phase 6 — Status ADK tools."""

    def test_get_project_status_reads_state(self):
        from adk.tools.status_tools import get_project_status
        ctx = MagicMock()
        ctx.state = {"current_project": "MyProj", "current_project_root": "/tmp/MyProj"}
        r = get_project_status(tool_context=ctx)
        self.assertTrue(r["ok"])
        self.assertEqual(r["project"], "MyProj")

    def test_get_project_status_no_context(self):
        from adk.tools.status_tools import get_project_status
        r = get_project_status()
        self.assertFalse(r["ok"])


class TestMPartitionGenerators(unittest.TestCase):
    """Phase 7 — SQL Server / Excel / Web M partition generation."""

    def test_sql_partition_contains_sql_database(self):
        from mcp_server.server import _build_m_partition
        cols = [{"name": "Id", "dataType": "int64"}, {"name": "Name", "dataType": "string"}]
        block = _build_m_partition("Orders", "", cols, source_type="sql",
                                   connection_params={"server": "localhost", "database": "Sales", "table": "Orders"})
        self.assertIn("Sql.Database", block)
        self.assertIn("localhost", block)
        self.assertIn("Sales", block)

    def test_excel_partition_contains_excel_workbook(self):
        from mcp_server.server import _build_m_partition
        cols = [{"name": "A", "dataType": "string"}]
        block = _build_m_partition("Sheet1", "C:/data.xlsx", cols, source_type="excel",
                                   connection_params={"sheet": "Sheet1"})
        self.assertIn("Excel.Workbook", block)

    def test_web_partition_contains_web_contents(self):
        from mcp_server.server import _build_m_partition
        cols = [{"name": "A", "dataType": "string"}]
        block = _build_m_partition("WebTable", "https://example.com/data.csv", cols, source_type="web")
        self.assertIn("Web.Contents", block)

    def test_csv_partition_still_works(self):
        from mcp_server.server import _build_m_partition
        cols = [{"name": "A", "dataType": "string"}]
        block = _build_m_partition("T", "C:/data.csv", cols, source_type="csv")
        self.assertIn("Csv.Document", block)


class TestParsePartitionM(unittest.TestCase):
    """Phase 7 — parse_partition_m extracts connection info."""

    def test_sql_partition_parsed(self):
        from utils.tmdl_parser import parse_partition_m
        info = parse_partition_m('Sql.Database("localhost", "SalesDB", [Query="SELECT * FROM Orders"])')
        self.assertEqual(info["connection_type"], "sql")
        self.assertEqual(info["server"], "localhost")
        self.assertEqual(info["database"], "SalesDB")
        self.assertEqual(info["query"], "SELECT * FROM Orders")

    def test_csv_partition_parsed(self):
        from utils.tmdl_parser import parse_partition_m
        info = parse_partition_m('Csv.Document(File.Contents("C:/data.csv"))')
        self.assertEqual(info["connection_type"], "csv")
        self.assertEqual(info["path"], "C:/data.csv")


if __name__ == "__main__":
    unittest.main()
