"""Phase 3 tests — intent-driven measure/visual/relationship selection.

Verifies the Phase 3 pattern: **heuristic candidate generator + LLM
selector/ranker**. The key guarantee is that with a *different* business
description but the *same* schema, the selected measures/visuals genuinely
differ — while offline (no API key) the baseline is unchanged (fail-safe).

The LLM path is exercised by mocking the selector functions so no live API
calls are made; the offline path is verified with ``GOOGLE_API_KEY=""``.

Run with::

    python -m pytest tests/test_phase3_intent_driven.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _sample_schema() -> dict:
    """A schema with clear amount/region/category/date columns."""
    return {
        "table_name": "Sales",
        "columns": [
            {"name": "OrderDate", "dataType": "dateTime"},
            {"name": "Region", "dataType": "string"},
            {"name": "Product", "dataType": "string"},
            {"name": "Sales", "dataType": "double"},
            {"name": "Quantity", "dataType": "int64"},
        ],
    }


def _sample_candidates() -> list[dict]:
    """Candidate measures the DAXAgent would produce."""
    return [
        {"name": "Total Sales", "expression": "SUM(Sales[Sales])",
         "table": "Sales", "displayFolder": "Revenue", "formatString": "$ #,##0.00"},
        {"name": "Avg Sales", "expression": "AVERAGE(Sales[Sales])",
         "table": "Sales", "displayFolder": "Revenue", "formatString": "$ #,##0.00"},
        {"name": "Order Count", "expression": "COUNTROWS('Sales')",
         "table": "Sales", "displayFolder": "Orders", "formatString": "#,##0"},
        {"name": "Total Quantity", "expression": "SUM(Sales[Quantity])",
         "table": "Sales", "displayFolder": "Orders", "formatString": "#,##0"},
        {"name": "Distinct Product", "expression": "DISTINCTCOUNT(Sales[Product])",
         "table": "Sales", "displayFolder": "Orders", "formatString": "#,##0"},
        {"name": "Sales YTD", "expression": "TOTALYTD(SUM(Sales[Sales]), Sales[OrderDate])",
         "table": "Sales", "displayFolder": "Dates", "formatString": "$ #,##0.00"},
    ]


class TestMeasureSelectorOffline(unittest.TestCase):
    """Offline: MeasureSelectorAgent returns the candidate pool unchanged."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = ""

    def test_returns_all_candidates_offline(self):
        from agents.measure_selector_agent import MeasureSelectorAgent

        selector = MeasureSelectorAgent()
        ms = selector.select(_sample_candidates(), "any description", _sample_schema())
        names = [m.name for m in ms.measures]
        self.assertEqual(names, [c["name"] for c in _sample_candidates()])
        # rationale is populated even offline
        self.assertTrue(all(m.rationale for m in ms.measures))

    def test_empty_candidates_returns_empty(self):
        from agents.measure_selector_agent import MeasureSelectorAgent

        ms = MeasureSelectorAgent().select([], "x", _sample_schema())
        self.assertEqual(ms.count, 0)


class TestMeasureSelectorIntentDriven(unittest.TestCase):
    """Online: different intents → different selected measures."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = "fake-key"

    def test_finance_intent_keeps_revenue_measures(self):
        import agents.measure_selector_agent as mod
        from agents.schemas import Measure, MeasureSet

        finance = MeasureSet(measures=[
            Measure(name="Total Sales", expression="SUM(Sales[Sales])", table="Sales",
                    displayFolder="Revenue", formatString="$ #,##0.00",
                    rationale="core revenue KPI for finance"),
            Measure(name="Avg Sales", expression="AVERAGE(Sales[Sales])", table="Sales",
                    displayFolder="Revenue", formatString="$ #,##0.00",
                    rationale="avg revenue"),
            Measure(name="Sales YTD", expression="TOTALYTD(...)", table="Sales",
                    displayFolder="Dates", formatString="$ #,##0.00",
                    rationale="YTD for finance reporting"),
            Measure(name="Order Count", expression="COUNTROWS('Sales')", table="Sales",
                    displayFolder="Orders", formatString="#,##0",
                    rationale="transaction volume"),
        ])
        with patch.object(mod, "_select_with_llm", return_value=finance):
            ms = mod.MeasureSelectorAgent().select(
                _sample_candidates(), "financial P&L report", _sample_schema()
            )
        names = {m.name for m in ms.measures}
        self.assertIn("Total Sales", names)
        self.assertIn("Sales YTD", names)
        # a finance report drops the quantity measure
        self.assertNotIn("Total Quantity", names)

    def test_operations_intent_keeps_quantity_measures(self):
        import agents.measure_selector_agent as mod
        from agents.schemas import Measure, MeasureSet

        ops = MeasureSet(measures=[
            Measure(name="Order Count", expression="COUNTROWS('Sales')", table="Sales",
                    displayFolder="Orders", formatString="#,##0",
                    rationale="order volume for operations"),
            Measure(name="Total Quantity", expression="SUM(Sales[Quantity])", table="Sales",
                    displayFolder="Orders", formatString="#,##0",
                    rationale="units shipped"),
            Measure(name="Distinct Product", expression="DISTINCTCOUNT(...)", table="Sales",
                    displayFolder="Orders", formatString="#,##0",
                    rationale="product variety"),
            Measure(name="Total Sales", expression="SUM(Sales[Sales])", table="Sales",
                    displayFolder="Revenue", formatString="$ #,##0.00",
                    rationale="keep one revenue metric"),
        ])
        with patch.object(mod, "_select_with_llm", return_value=ops):
            ms = mod.MeasureSelectorAgent().select(
                _sample_candidates(), "operations logistics dashboard", _sample_schema()
            )
        names = {m.name for m in ms.measures}
        self.assertIn("Total Quantity", names)
        self.assertIn("Order Count", names)

    def test_same_schema_different_intent_different_output(self):
        """The core Phase 3 guarantee: same schema + different intent → different
        measure sets (when the LLM is available)."""
        import agents.measure_selector_agent as mod
        from agents.schemas import Measure, MeasureSet

        finance = MeasureSet(measures=[
            Measure(name="Total Sales", expression="SUM(Sales[Sales])", table="Sales",
                    displayFolder="Revenue", rationale="finance"),
            Measure(name="Sales YTD", expression="x", table="Sales",
                    displayFolder="Dates", rationale="finance ytd"),
            Measure(name="Avg Sales", expression="x", table="Sales",
                    displayFolder="Revenue", rationale="finance avg"),
            Measure(name="Order Count", expression="x", table="Sales",
                    displayFolder="Orders", rationale="volume"),
        ])
        ops = MeasureSet(measures=[
            Measure(name="Total Quantity", expression="x", table="Sales",
                    displayFolder="Orders", rationale="ops"),
            Measure(name="Order Count", expression="x", table="Sales",
                    displayFolder="Orders", rationale="ops"),
            Measure(name="Distinct Product", expression="x", table="Sales",
                    displayFolder="Orders", rationale="ops"),
            Measure(name="Total Sales", expression="x", table="Sales",
                    displayFolder="Revenue", rationale="keep one"),
        ])
        with patch.object(mod, "_select_with_llm", return_value=finance):
            fin_ms = mod.MeasureSelectorAgent().select(
                _sample_candidates(), "finance report", _sample_schema()
            )
        with patch.object(mod, "_select_with_llm", return_value=ops):
            ops_ms = mod.MeasureSelectorAgent().select(
                _sample_candidates(), "ops dashboard", _sample_schema()
            )
        fin_names = {m.name for m in fin_ms.measures}
        ops_names = {m.name for m in ops_ms.measures}
        # they must differ (the whole point of Phase 3)
        self.assertNotEqual(fin_names, ops_names)
        self.assertIn("Sales YTD", fin_names)
        self.assertIn("Total Quantity", ops_names)


class TestMeasureSelectorFailSafe(unittest.TestCase):
    """LLM validation failures fall back to the full candidate pool."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = "fake-key"

    def test_rejects_measures_referencing_ghost_columns(self):
        """If the LLM invents a column, _select_with_llm returns None → fallback."""
        import agents.measure_selector_agent as mod

        schema = _sample_schema()
        # the LLM returns a measure referencing 'Profit' which doesn't exist
        fake_text = '{"measures": [{"name": "Total Profit", "expression": "SUM(Sales[Profit])", "table": "Sales", "displayFolder": "R", "rationale": "x"}, {"name": "A", "expression": "1", "table": "Sales", "rationale": "y"}, {"name": "B", "expression": "2", "table": "Sales", "rationale": "z"}]}'
        fake_resp = type("R", (), {"text": fake_text})()
        fake_client = type("C", (), {"models": type("M", (), {
            "generate_content": staticmethod(lambda **k: fake_resp)})()})()
        import agents.measure_selector_agent as mod2
        with patch("google.genai.Client", return_value=fake_client):
            ms = mod2._select_with_llm(_sample_candidates(), "x", schema)
        # ghost column → rejected → None → caller falls back
        self.assertIsNone(ms)


class TestVisualPlannerOffline(unittest.TestCase):
    """Offline: VisualPlannerAgent returns candidates as one Summary page."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = ""

    def test_returns_single_summary_page_offline(self):
        from agents.visual_planner_agent import VisualPlannerAgent

        candidates = [
            {"name": "card-primary", "kind": "card", "measure": "Total Sales"},
            {"name": "bar-region", "kind": "barChart", "category": "Region",
             "measure": "Total Sales"},
        ]
        rp = VisualPlannerAgent().plan(
            candidates, "x", _sample_schema(), _sample_candidates()
        )
        self.assertEqual(rp.page_count, 1)
        self.assertEqual(rp.pages[0].id, "summary-page")
        self.assertEqual(rp.visual_count, 2)
        # intent_match_reasoning populated offline
        self.assertTrue(all(v.intent_match_reasoning for v in rp.pages[0].visuals))


class TestVisualPlannerIntentDriven(unittest.TestCase):
    """Online: different report styles → different visual counts."""

    def setUp(self) -> None:
        os.environ["GOOGLE_API_KEY"] = "fake-key"

    def test_minimal_style_fewer_visuals(self):
        import agents.visual_planner_agent as mod
        from agents.schemas import PagePlan, ReportPlan, VisualPlan

        minimal = ReportPlan(pages=[
            PagePlan(id="summary-page", displayName="Summary", visuals=[
                VisualPlan(name="card-primary", kind="card", measure="Total Sales",
                           intent_match_reasoning="the single KPI the user wants"),
            ]),
        ])
        with patch.object(mod, "_plan_with_llm", return_value=minimal):
            rp = mod.VisualPlannerAgent().plan(
                [{"name": "card-primary", "kind": "card", "measure": "Total Sales"},
                 {"name": "bar-region", "kind": "barChart", "category": "Region",
                  "measure": "Total Sales"}],
                "just one number", _sample_schema(), _sample_candidates(), "minimal"
            )
        self.assertEqual(rp.visual_count, 1)

    def test_rich_style_more_visuals(self):
        import agents.visual_planner_agent as mod
        from agents.schemas import PagePlan, ReportPlan, VisualPlan

        rich = ReportPlan(pages=[
            PagePlan(id="overview", displayName="Overview", visuals=[
                VisualPlan(name="card-primary", kind="card", measure="Total Sales",
                           intent_match_reasoning="KPI"),
                VisualPlan(name="bar-region", kind="barChart", category="Region",
                           measure="Total Sales", intent_match_reasoning="by region"),
                VisualPlan(name="line-date", kind="lineChart", category="OrderDate",
                           measure="Total Sales", intent_match_reasoning="trend"),
            ]),
        ])
        with patch.object(mod, "_plan_with_llm", return_value=rich):
            rp = mod.VisualPlannerAgent().plan(
                [{"name": "card-primary", "kind": "card", "measure": "Total Sales"}],
                "comprehensive dashboard", _sample_schema(), _sample_candidates(), "rich"
            )
        self.assertEqual(rp.visual_count, 3)

    def test_rejects_visuals_with_ghost_measures(self):
        """LLM plan referencing a non-existent measure → fallback."""
        import agents.visual_planner_agent as mod

        fake_text = '{"pages": [{"id": "p", "displayName": "P", "visuals": [{"name": "v", "kind": "card", "measure": "Ghost Measure", "intent_match_reasoning": "x"}]}]}'
        fake_resp = type("R", (), {"text": fake_text})()
        fake_client = type("C", (), {"models": type("M", (), {
            "generate_content": staticmethod(lambda **k: fake_resp)})()})()
        measures = [{"name": "Total Sales"}]
        with patch("google.genai.Client", return_value=fake_client):
            rp = mod._plan_with_llm(
                [{"name": "card-primary", "kind": "card", "measure": "Total Sales"}],
                "x", _sample_schema(), measures, "standard"
            )
        # ghost measure → rejected → None
        self.assertIsNone(rp)


class TestRelationshipAlwaysOnRefinement(unittest.TestCase):
    """refine_relationships is always called; offline = no-op, online = enrich."""

    def test_offline_returns_heuristic_unchanged(self):
        os.environ["GOOGLE_API_KEY"] = ""
        from utils.llm_client import refine_relationships

        heuristic = [{"from_table": "A", "from_column": "BId",
                      "to_table": "B", "to_column": "Id"}]
        out = refine_relationships([], heuristic)
        self.assertIs(out, heuristic)

    def test_online_enriches_with_confidence_and_reasoning(self):
        os.environ["GOOGLE_API_KEY"] = "fake-key"
        from utils.llm_client import refine_relationships
        from utils.model_config import LLMConfig

        heuristic = [{"from_table": "A", "from_column": "BId",
                      "to_table": "B", "to_column": "Id", "to_cardinality": "one"}]
        fake_config = LLMConfig(provider="google", model="gemini-2.5-flash",
                                 litellm_model="gemini/gemini-2.5-flash", api_key="fake-key")
        fake_text = '''[{
            "from_table": "A", "from_column": "BId", "to_table": "B",
            "to_column": "Id", "to_cardinality": "one",
            "confidence_score": 0.88, "source_reasoning": "name stem match"
        }]'''
        with patch("utils.model_config.get_llm_config", return_value=fake_config), \
             patch("utils.model_config.get_text_completion", return_value=fake_text):
            out = refine_relationships([], heuristic)
        self.assertEqual(out[0]["confidence_score"], 0.88)
        self.assertEqual(out[0]["source_reasoning"], "name stem match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
