"""Phase 5.2 tests — Naming Convention."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from validators.naming import (
    normalize_measure,
    pascal_case,
    plan_renames,
    suggest_folder,
    title_case,
)
from adk.tools.naming_tools import (
    normalize_name,
    plan_naming_for_pbip,
    suggest_display_folder,
)


class TestCaseStyles(unittest.TestCase):

    def test_pascal_basic(self):
        self.assertEqual(pascal_case("total sales"), "TotalSales")
        self.assertEqual(pascal_case("total_sales_amount"), "TotalSalesAmount")

    def test_pascal_preserves_acronyms(self):
        self.assertEqual(pascal_case("total sales py"), "TotalSalesPY")
        self.assertEqual(pascal_case("yoy growth"), "YOYGrowth")
        self.assertEqual(pascal_case("kpi count"), "KPICount")

    def test_pascal_handles_mixed(self):
        self.assertEqual(pascal_case("USDToEUR"), "USDToEUR")
        # 'ytd-revenue'
        self.assertEqual(pascal_case("ytd-revenue"), "YTDRevenue")

    def test_title_case(self):
        self.assertEqual(title_case("total sales py"), "Total Sales PY")
        self.assertEqual(title_case("total_sales"), "Total Sales")
        self.assertEqual(title_case("yoy_pct"), "YOY Pct")

    def test_normalize_measure_title_default(self):
        self.assertEqual(normalize_measure("total_sales"), "Total Sales")

    def test_normalize_measure_pascal(self):
        self.assertEqual(normalize_measure("total sales", style="pascal"), "TotalSales")


class TestFolderSuggestion(unittest.TestCase):

    def test_time_intelligence_folder(self):
        self.assertEqual(suggest_folder("Total Sales YoY"), "Measures\\Time Intelligence")
        self.assertEqual(suggest_folder("Sales YTD"), "Measures\\Time Intelligence")
        self.assertEqual(suggest_folder("Sales PY"), "Measures\\Time Intelligence")

    def test_target_folder(self):
        self.assertEqual(suggest_folder("Sales Target"), "Measures\\Targets")
        self.assertEqual(suggest_folder("Budget Q1"), "Measures\\Targets")

    def test_ratio_folder(self):
        self.assertEqual(suggest_folder("Profit Margin %"), "Measures\\Ratios")
        self.assertEqual(suggest_folder("Share Of Total"), "Measures\\Ratios")

    def test_aggregations_from_expression(self):
        # name doesn't hint, expression does
        f = suggest_folder("Order Count", base_expression="COUNTROWS('Sales')")
        self.assertEqual(f, "Measures\\Aggregations")

    def test_default_folder(self):
        self.assertEqual(suggest_folder("Total Sales"), "Measures")

    def test_custom_base_folder(self):
        self.assertEqual(
            suggest_folder("Sales YoY", default="_Metrics"),
            "_Metrics\\Time Intelligence",
        )


class TestPlanRenames(unittest.TestCase):

    def test_plan_renames_basic(self):
        model = {"all_measures": [
            {"name": "total_sales", "expression": "SUM('Sales'[Sales])",
             "displayFolder": "", "table": "Sales"},
            {"name": "total sales py", "expression": "CALCULATE(...)",
             "displayFolder": "", "table": "Sales"},
        ]}
        plan = plan_renames(model)
        self.assertEqual(plan["total_measures"], 2)
        self.assertEqual(plan["total_renames"], 2)
        names = [r["new_name"] for r in plan["renames"]]
        self.assertIn("Total Sales", names)
        self.assertIn("Total Sales PY", names)

    def test_plan_skip_unchanged_when_only_if_changed(self):
        model = {"all_measures": [
            {"name": "Total Sales", "expression": "SUM('Sales'[Sales])",
             "displayFolder": "Measures", "table": "Sales"},
        ]}
        plan = plan_renames(model, only_if_changed=True)
        # name already correct → no rename row
        self.assertEqual(plan["total_renames"], 0)

    def test_plan_emits_folder_changes(self):
        model = {"all_measures": [
            {"name": "Total Sales YoY", "expression": "...",
             "displayFolder": "", "table": "Sales"},
        ]}
        plan = plan_renames(model)
        self.assertEqual(plan["folders"][0]["new_folder"], "Measures\\Time Intelligence")

    def test_collision_detection(self):
        model = {"all_measures": [
            {"name": "total_sales", "expression": "", "displayFolder": "", "table": "Sales"},
            {"name": "Total_Sales", "expression": "", "displayFolder": "", "table": "Sales"},
        ]}
        plan = plan_renames(model)
        # both normalise to "Total Sales" → one wins, one skipped
        self.assertEqual(plan["total_renames"], 1)
        self.assertEqual(len(plan["skipped"]), 1)


class TestAdkWrappers(unittest.TestCase):

    def test_normalize_name_returns_both_styles(self):
        out = normalize_name("total_sales_py")
        self.assertEqual(out["pascal"], "TotalSalesPY")
        self.assertEqual(out["title"], "Total Sales PY")
        self.assertEqual(out["result"], "Total Sales PY")
        out2 = normalize_name("total_sales", style="pascal")
        self.assertEqual(out2["result"], "TotalSales")

    def test_suggest_display_folder_wrapper(self):
        out = suggest_display_folder("Sales YoY")
        self.assertEqual(out["folder"], "Measures\\Time Intelligence")

    def test_plan_naming_for_pbip_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            sm = Path(td) / "Q.SemanticModel"
            tables = sm / "definition" / "tables"
            tables.mkdir(parents=True, exist_ok=True)
            (tables / "Sales.tmdl").write_text(
                "table Sales\n"
                "\n"
                "\tcolumn 'Sales'\n"
                "\t\tdataType: double\n"
                "\t\tsummarizeBy: sum\n"
                "\t\tsourceColumn: Sales\n"
                "\n"
                "\tmeasure 'total_sales' = SUM('Sales'[Sales])\n"
                "\tmeasure 'total_sales_yoy' = CALCULATE([total_sales], DATEADD('Date'[Date], -1, YEAR))\n",
                encoding="utf-8",
            )
            out = plan_naming_for_pbip(str(sm))
            self.assertNotIn("error", out)
            new_names = {r["new_name"] for r in out["renames"]}
            self.assertIn("Total Sales", new_names)
            self.assertIn("Total Sales YOY", new_names)
            folders = {f["name"]: f["new_folder"] for f in out["folders"]}
            self.assertEqual(folders["Total Sales YOY"], "Measures\\Time Intelligence")
            self.assertEqual(out["model_path"], str(sm))

    def test_plan_naming_for_pbip_missing(self):
        out = plan_naming_for_pbip("/nonexistent/abc")
        self.assertFalse(out["ok"])
        self.assertTrue(out["errors"])  # non-empty error list


if __name__ == "__main__":
    unittest.main()
