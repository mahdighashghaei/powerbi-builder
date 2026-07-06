"""Regression tests for the three PBIR normalization / primary-table fixes
that made Power BI Desktop reject rich-page builds.

Root causes found + fixed:
  1. ``_normalize_query_state`` only recognized ``{"measure":{...}}`` items,
     not the ``{"kind":"measure","name":...,"table":...}`` format emitted by
     ``highlevel._simplified_select`` → rich-page visuals got empty queryState.
  2. ``read_semantic_model`` picked the table with the most columns as
     ``primary_table`` — that selected the auto Date table (17 cols) over the
     real data table (5 cols) → build_report bound all visuals to Date (wrong).
  3. ``_normalize_query_state`` did not recognize the role-based simplified
     format ``{"Rows":[{"kind":"column",...}],...}`` (no ``select`` key) used
     for matrix visuals → matrix passed through unnormalized → Desktop reject.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class TestNormalizeQueryStateKindFormat(unittest.TestCase):
    """Fix #1: the kind/name/table simplified format is normalized to projections."""

    def test_kind_measure_format_normalized(self):
        from mcp_server.server import PbipToolbox  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            qs = {"select": [{"kind": "measure", "name": "Total Amount", "table": "Sales", "role": "Values"}]}
            out = tb._normalize_query_state("card", qs, "Sales")
            self.assertIn("Values", out)
            proj = out["Values"]["projections"][0]
            self.assertIn("Measure", proj["field"])
            self.assertEqual(proj["field"]["Measure"]["Property"], "Total Amount")

    def test_kind_column_format_normalized(self):
        from mcp_server.server import PbipToolbox  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            qs = {"select": [{"kind": "column", "name": "Region", "table": "Sales", "role": "Category"}]}
            out = tb._normalize_query_state("barChart", qs, "Sales")
            self.assertIn("Category", out)
            proj = out["Category"]["projections"][0]
            self.assertIn("Column", proj["field"])
            self.assertEqual(proj["field"]["Column"]["Property"], "Region")

    def test_legacy_measure_dict_format_still_works(self):
        """The original {"measure": {"table":..,"name":..}} format still normalizes."""
        from mcp_server.server import PbipToolbox  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            qs = {"select": [{"measure": {"table": "Sales", "name": "Total"}, "role": "Values"}]}
            out = tb._normalize_query_state("card", qs, "Sales")
            self.assertIn("Values", out)
            self.assertEqual(out["Values"]["projections"][0]["field"]["Measure"]["Property"], "Total")


class TestNormalizeQueryStateRoleBasedFormat(unittest.TestCase):
    """Fix #3: role-based simplified format (no 'select' key) is normalized."""

    def test_matrix_role_format_normalized(self):
        from mcp_server.server import PbipToolbox  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            qs = {
                "Rows": [{"kind": "column", "name": "Region", "table": "Sales"}],
                "Values": [{"kind": "measure", "name": "Total Amount", "table": "Sales"}],
                "Columns": [{"kind": "column", "name": "Category", "table": "Sales"}],
            }
            out = tb._normalize_query_state("matrix", qs, "Sales")
            self.assertIn("Rows", out)
            self.assertIn("Values", out)
            self.assertIn("Columns", out)
            self.assertTrue(out["Rows"]["projections"])
            self.assertTrue(out["Values"]["projections"])
            self.assertTrue(out["Columns"]["projections"])

    def test_projection_format_passes_through(self):
        """Already-normalized projection format is not re-processed."""
        from mcp_server.server import PbipToolbox  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            tb = PbipToolbox(td)
            qs = {"Values": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total"}}, "queryRef": "Sales.Total"}]}}
            out = tb._normalize_query_state("card", qs, "Sales")
            self.assertEqual(out, qs)


class TestPrimaryTableSelection(unittest.TestCase):
    """Fix #2: primary_table is the data table with measures, not the Date table."""

    @classmethod
    def setUpClass(cls) -> None:
        import os  # noqa: E402

        os.environ["GOOGLE_API_KEY"] = ""
        cls._tmp = tempfile.TemporaryDirectory()
        from mcp_server import highlevel as hl  # noqa: E402

        cls.result = hl.generate_pbip(
            str(_ROOT / "examples" / "sample.csv"),
            "Monthly sales by region",
            output_root=str(Path(cls._tmp.name) / "out"),
        )
        cls.project = Path(cls.result["data"]["pbip_root"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_primary_table_is_data_table_not_date(self):
        from utils.tmdl_parser import read_semantic_model  # noqa: E402

        sm = next(self.project.glob("*.SemanticModel"))
        model = read_semantic_model(sm)
        self.assertEqual(model["primary_table"], "sample")
        self.assertNotEqual(model["primary_table"], "Date")

    def test_all_columns_from_data_table(self):
        from utils.tmdl_parser import read_semantic_model  # noqa: E402

        sm = next(self.project.glob("*.SemanticModel"))
        model = read_semantic_model(sm)
        col_names = [c["name"] for c in model["all_columns"]]
        self.assertIn("Amount", col_names)
        self.assertIn("Region", col_names)


class TestRichPagesNoEmptyQueryState(unittest.TestCase):
    """End-to-end: a build with rich pages has no empty/unnormalized queryState."""

    @classmethod
    def setUpClass(cls) -> None:
        import os  # noqa: E402

        os.environ["GOOGLE_API_KEY"] = ""
        cls._tmp = tempfile.TemporaryDirectory()
        from mcp_server import highlevel as hl  # noqa: E402
        from adk.tools.highlevel_tools import build_report  # noqa: E402

        res = hl.generate_pbip(
            str(_ROOT / "examples" / "sample.csv"),
            "Monthly sales by region",
            output_root=str(Path(cls._tmp.name) / "out"),
        )
        cls.project = Path(res["data"]["pbip_root"])
        build_report(str(cls.project), num_pages=3, visual_variety="all",
                      description="regional performance")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_no_empty_query_state(self):
        report = next(self.project.glob("*.Report"))
        for vj in report.rglob("visual.json"):
            d = json.loads(vj.read_text(encoding="utf-8"))
            qs = d.get("visual", {}).get("query", {}).get("queryState", {})
            self.assertTrue(qs, f"{vj.parent.name} has empty queryState")

    def test_no_unnormalized_role_lists(self):
        report = next(self.project.glob("*.Report"))
        for vj in report.rglob("visual.json"):
            d = json.loads(vj.read_text(encoding="utf-8"))
            qs = d.get("visual", {}).get("query", {}).get("queryState", {})
            for role, bucket in qs.items():
                self.assertNotIsInstance(
                    bucket, list,
                    f"{vj.parent.name}.{role} is an unnormalized list",
                )


if __name__ == "__main__":
    unittest.main()
