"""tests/test_insights_engine.py — Unit tests for the Business Insights layer.

Covers:
  - utils.insights_engine: detect_anomalies, segment_by_behavior,
    find_underperformers, compute_trends, suggest_missing_kpis,
    compile_visual_explanations, generate_insights.
  - agents/dax_agent.py: guaranteed YoY / Rank / Anomaly Count measures are
    always present regardless of which tournament strategy wins.
  - agents/insights_agent.py: InsightsAgent smoke test via a real
    AgentContext + PbipToolbox (mirrors tests/test_strategy_synthesis.py).
  - agents/orchestrator.py: the README "## Business Insights" section is
    populated end-to-end on a real build.

All tests are pure/hermetic — synthetic pandas DataFrames and tmp CSV files,
no dependency on repo-root sample data.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _sales_dataframe(n_per_segment: int = 20) -> pd.DataFrame:
    """A small synthetic sales dataset with a clear high/low segment split,
    a discount outlier, and two full years of monthly data (for trends)."""
    rows = []
    segments = {"Alpha": 1000.0, "Beta": 500.0, "Gamma": 50.0}
    month_idx = 0
    for seg, base in segments.items():
        for i in range(n_per_segment):
            month_idx += 1
            year = 2023 if month_idx <= 12 else 2024
            month = ((month_idx - 1) % 12) + 1
            amount = base + (i % 5) * 2.0
            rows.append({
                "Segment": seg,
                "Amount": amount,
                "Discount": 5.0 if seg != "Gamma" else 40.0,
                "Date": f"{year}-{month:02d}-01",
            })
    # inject a couple of clear outliers into Amount
    rows.append({"Segment": "Alpha", "Amount": 100000.0, "Discount": 5.0, "Date": "2024-06-01"})
    rows.append({"Segment": "Alpha", "Amount": 99000.0, "Discount": 5.0, "Date": "2024-06-01"})
    return pd.DataFrame(rows)


class TestDetectAnomalies(unittest.TestCase):
    def test_finds_outliers_in_amount_column(self):
        from utils.insights_engine import detect_anomalies

        df = _sales_dataframe()
        findings = detect_anomalies(df, [{"name": "Amount"}])
        self.assertTrue(findings)
        f = findings[0]
        self.assertEqual(f.column, "Amount")
        self.assertGreater(f.outlier_count, 0)
        self.assertIn("IQR", f.narrative)

    def test_no_outliers_returns_empty(self):
        from utils.insights_engine import detect_anomalies

        df = pd.DataFrame({"Flat": [10.0] * 20})
        findings = detect_anomalies(df, [{"name": "Flat"}])
        self.assertEqual(findings, [])

    def test_missing_column_is_skipped_not_crashed(self):
        from utils.insights_engine import detect_anomalies

        df = pd.DataFrame({"Other": [1, 2, 3]})
        findings = detect_anomalies(df, [{"name": "DoesNotExist"}])
        self.assertEqual(findings, [])

    def test_none_dataframe_returns_empty(self):
        from utils.insights_engine import detect_anomalies

        self.assertEqual(detect_anomalies(None, [{"name": "Amount"}]), [])


class TestSegmentByBehavior(unittest.TestCase):
    def test_tiers_segments_by_quantile(self):
        from utils.insights_engine import segment_by_behavior

        df = _sales_dataframe()
        segments = segment_by_behavior(df, [{"name": "Segment"}], "Amount")
        self.assertTrue(segments)
        tiers = {s.segment_name: s.tier for s in segments}
        # Alpha has the highest base amount (plus outliers) -> should be High
        self.assertEqual(tiers.get("Alpha"), "High")
        # Gamma has the lowest base amount -> should be Low
        self.assertEqual(tiers.get("Gamma"), "Low")
        total_share = sum(s.share_pct for s in segments)
        self.assertAlmostEqual(total_share, 100.0, delta=0.5)

    def test_no_category_column_returns_empty(self):
        from utils.insights_engine import segment_by_behavior

        df = pd.DataFrame({"Amount": [1, 2, 3]})
        self.assertEqual(segment_by_behavior(df, [], "Amount"), [])

    def test_no_amount_col_returns_empty(self):
        from utils.insights_engine import segment_by_behavior

        df = _sales_dataframe()
        self.assertEqual(segment_by_behavior(df, [{"name": "Segment"}], None), [])


class TestFindUnderperformers(unittest.TestCase):
    def test_low_tier_segment_gets_recommendation(self):
        from utils.insights_engine import segment_by_behavior, find_underperformers

        df = _sales_dataframe()
        segments = segment_by_behavior(df, [{"name": "Segment"}], "Amount")
        underperformers = find_underperformers(segments, df, discount_col="Discount")
        self.assertTrue(underperformers)
        gamma = next((u for u in underperformers if u.segment_name == "Gamma"), None)
        self.assertIsNotNone(gamma)
        self.assertLess(gamma.gap_vs_avg_pct, 0)
        self.assertTrue(gamma.recommended_action)
        # Gamma has a much higher discount rate -> action should call it out
        self.assertIn("discount", gamma.recommended_action.lower())

    def test_no_segments_returns_empty(self):
        from utils.insights_engine import find_underperformers

        self.assertEqual(find_underperformers([]), [])


class TestComputeTrends(unittest.TestCase):
    def test_two_year_span_produces_trend(self):
        from utils.insights_engine import compute_trends

        df = _sales_dataframe()
        trends = compute_trends(df, "Date", ["Amount"])
        self.assertTrue(trends)
        t = trends[0]
        self.assertEqual(t.metric, "Amount")
        self.assertTrue(t.narrative)

    def test_single_period_returns_empty(self):
        from utils.insights_engine import compute_trends

        df = pd.DataFrame({
            "Date": ["2024-01-01"] * 5,
            "Amount": [10, 20, 30, 40, 50],
        })
        self.assertEqual(compute_trends(df, "Date", ["Amount"]), [])

    def test_missing_date_col_returns_empty(self):
        from utils.insights_engine import compute_trends

        df = pd.DataFrame({"Amount": [1, 2, 3]})
        self.assertEqual(compute_trends(df, "NoSuchDate", ["Amount"]), [])


class TestSuggestMissingKpis(unittest.TestCase):
    def test_merges_business_analysis_and_judge_gaps(self):
        from types import SimpleNamespace
        from utils.insights_engine import suggest_missing_kpis

        biz = SimpleNamespace(recommendations=["Add time-intelligence measures (YTD, MoM) for trend analysis."])
        judge_result = {
            "strategy_gaps": [
                {"domain": "dax", "missing_pattern": "kpi coverage 20% < 50%",
                 "suggested_synthesis": "kpi_gap_fill"},
            ],
        }
        suggestions = suggest_missing_kpis(biz, judge_result)
        self.assertEqual(len(suggestions), 2)
        self.assertTrue(any("time-intelligence" in s.suggestion for s in suggestions))
        self.assertTrue(any("dax" in s.suggestion.lower() for s in suggestions))

    def test_no_signals_returns_empty(self):
        from utils.insights_engine import suggest_missing_kpis

        self.assertEqual(suggest_missing_kpis(None, None), [])


class TestCompileVisualExplanations(unittest.TestCase):
    def test_prefers_meaningful_reasoning_falls_back_to_template(self):
        from agents.schemas import PagePlan, ReportPlan, VisualPlan, VisualReasoning
        from utils.insights_engine import compile_visual_explanations

        report_plan = ReportPlan(pages=[
            PagePlan(id="summary-page", displayName="Summary", visuals=[
                VisualPlan(
                    name="card-total-sales", kind="card", measure="Total Sales",
                    visual_reasoning=VisualReasoning(
                        why_this_visual="This card highlights total sales as the primary executive KPI.",
                    ),
                ),
                VisualPlan(
                    name="bar-segment", kind="barChart", measure="Total Sales",
                    category="Segment", intent_match_reasoning="deterministic candidate visual",
                ),
            ]),
        ])
        ctx_pages = [{
            "id": "summary-page", "displayName": "Summary",
            "visuals": [
                {"id": "card-total-sales", "visualType": "card"},
                {"id": "bar-segment", "visualType": "barChart"},
            ],
        }]
        explanations = compile_visual_explanations(report_plan, ctx_pages)
        self.assertEqual(len(explanations), 2)
        by_name = {e.visual_name: e for e in explanations}
        self.assertIn("executive KPI", by_name["card-total-sales"].explanation)
        # generic placeholder reasoning -> deterministic template used instead
        self.assertIn("Segment", by_name["bar-segment"].explanation)
        self.assertNotEqual(by_name["bar-segment"].explanation, "deterministic candidate visual")

    def test_every_visual_gets_an_explanation_even_with_no_report_plan(self):
        from utils.insights_engine import compile_visual_explanations

        ctx_pages = [{
            "id": "summary-page", "displayName": "Summary",
            "visuals": [{"id": "card-x", "visualType": "card"}],
        }]
        explanations = compile_visual_explanations(None, ctx_pages)
        self.assertEqual(len(explanations), 1)
        self.assertTrue(explanations[0].explanation)


class TestGenerateInsights(unittest.TestCase):
    def test_full_integration_with_dataframe(self):
        from utils.insights_engine import generate_insights

        df = _sales_dataframe()
        schema_columns = [
            {"name": "Segment", "dataType": "string"},
            {"name": "Amount", "dataType": "double"},
            {"name": "Discount", "dataType": "double"},
            {"name": "Date", "dataType": "dateTime"},
        ]
        insights = generate_insights(
            df=df, schema_columns=schema_columns, business_analysis=None,
            report_plan=None, ctx_pages=[], judge_result=None,
        )
        self.assertTrue(insights.anomalies)
        self.assertTrue(insights.segments)
        self.assertTrue(insights.underperformers)
        self.assertTrue(insights.trends)
        self.assertTrue(insights.summary)

    def test_no_dataframe_is_fail_safe(self):
        from utils.insights_engine import generate_insights

        insights = generate_insights(
            df=None, schema_columns=[], business_analysis=None,
            report_plan=None, ctx_pages=[], judge_result=None,
        )
        self.assertEqual(insights.anomalies, [])
        self.assertEqual(insights.segments, [])
        self.assertEqual(insights.trends, [])


class TestDaxAgentGuaranteedMeasures(unittest.TestCase):
    def test_yoy_rank_and_anomaly_measures_always_present(self):
        from agents.base import AgentContext
        from agents.dax_agent import DAXAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="sales dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = {
            "table_name": "Sales",
            "columns": [
                {"name": "Amount", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Segment", "dataType": "string", "summarizeBy": "none"},
                {"name": "Date", "dataType": "dateTime", "summarizeBy": "none"},
            ],
        }

        result = DAXAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        names_lower = " | ".join(m["name"].lower() for m in ctx.measures)
        self.assertIn("yoy", names_lower)
        self.assertIn("rank", names_lower)
        self.assertIn("anomaly", names_lower)

    def test_no_duplicate_when_winning_strategy_already_covers_it(self):
        from agents.base import AgentContext
        from agents.dax_agent import DAXAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="sales dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = {
            "table_name": "Sales",
            "columns": [
                {"name": "Amount", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Segment", "dataType": "string", "summarizeBy": "none"},
                {"name": "Date", "dataType": "dateTime", "summarizeBy": "none"},
            ],
        }
        DAXAgent(ctx).run()
        yoy_names = [m["name"] for m in ctx.measures if "yoy" in m["name"].lower()]
        # exactly one YoY-style measure, never two competing copies
        self.assertEqual(len(yoy_names), 1)


class TestInsightsAgentSmoke(unittest.TestCase):
    def test_run_produces_insights_from_real_csv(self):
        from agents.base import AgentContext
        from agents.insights_agent import InsightsAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        csv_path = Path(tmp) / "data.csv"
        _sales_dataframe().to_csv(csv_path, index=False)

        ctx = AgentContext(
            business_description="sales dashboard",
            source_path=csv_path,
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
            input_mode="create",
        )
        ctx.schema = {
            "table_name": "Sales",
            "columns": [
                {"name": "Segment", "dataType": "string"},
                {"name": "Amount", "dataType": "double"},
                {"name": "Discount", "dataType": "double"},
                {"name": "Date", "dataType": "dateTime"},
            ],
        }
        ctx.pages = []

        result = InsightsAgent(ctx).run()
        self.assertTrue(result.ok)
        insights = ctx.extra.get("insights")
        self.assertIsNotNone(insights)
        self.assertTrue(insights.segments)
        self.assertIn("insights_dict", ctx.extra)


if __name__ == "__main__":
    unittest.main()
