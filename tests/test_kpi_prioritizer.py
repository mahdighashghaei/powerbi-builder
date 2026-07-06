"""tests/test_kpi_prioritizer.py — Unit tests for the Business-aware KPI
Prioritization Layer.

Covers:
  - utils.kpi_prioritizer: rank_kpi_candidates (tiers, business_analysis
    bonus, description bonus, tie-breaking, fail-safe), reorder_by_priority,
    get_primary_kpi, pick_revenue_and_cost_columns.
  - agents/dax_agent.py: the amount bucket is reordered by priority so
    Sales/Profit outrank Manufacturing Price/Discounts on a SampleData-like
    schema, and the profitability strategy never treats "Profit" as revenue.
  - agents/report_agent.py: ReportAgent._amount_measure prefers the
    prioritized KPI's measure.
  - utils/insights_engine.py: generate_insights reorders by prioritized_kpis.
  - utils/judge.py: primary_kpi / primary_kpi_covered check.

All tests are hermetic (synthetic schemas/contexts), no dependency on
repo-root sample data.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def _cols(*names: str) -> list[dict]:
    return [{"name": n, "dataType": "double"} for n in names]


class TestRankKpiCandidates(unittest.TestCase):
    def test_profit_outranks_revenue_outranks_cost_outranks_price(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        cols = _cols("Manufacturing Price", "Discounts", "COGS", "Sales", "Profit")
        ranked = rank_kpi_candidates(cols)
        self.assertEqual(ranked[0], "Profit")
        self.assertEqual(ranked[1], "Sales")
        self.assertLess(ranked.index("COGS"), ranked.index("Manufacturing Price"))
        self.assertLess(ranked.index("Manufacturing Price"), ranked.index("Discounts"))

    def test_important_measures_bonus(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        cols = _cols("Widget Count", "Price")
        biz = SimpleNamespace(important_measures=["Widget Count"], executive_metrics=[])
        ranked = rank_kpi_candidates(cols, business_analysis=biz)
        self.assertEqual(ranked[0], "Widget Count")

    def test_business_description_bonus(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        # Both are mid/low tier with no other signal; description explicitly
        # calls out "discount impact" so Discounts should be boosted enough
        # to beat a plain unit-price column, but never beat a Profit column.
        cols = _cols("Manufacturing Price", "Discounts")
        ranked = rank_kpi_candidates(
            cols, business_description="track discount impact on customers",
        )
        self.assertEqual(ranked[0], "Discounts")

    def test_ties_preserve_original_order(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        cols = _cols("Amount1", "Amount2")
        ranked = rank_kpi_candidates(cols)
        self.assertEqual(ranked, ["Amount1", "Amount2"])

    def test_empty_input_returns_empty(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        self.assertEqual(rank_kpi_candidates([]), [])

    def test_get_primary_kpi(self):
        from utils.kpi_prioritizer import get_primary_kpi

        cols = _cols("Sale Price", "Profit", "Sales")
        self.assertEqual(get_primary_kpi(cols), "Profit")


class TestReorderByPriority(unittest.TestCase):
    def test_priority_names_come_first(self):
        from utils.kpi_prioritizer import reorder_by_priority

        cols = _cols("A", "B", "C")
        reordered = reorder_by_priority(cols, ["C", "A"])
        self.assertEqual([c["name"] for c in reordered], ["C", "A", "B"])

    def test_no_priority_names_returns_original(self):
        from utils.kpi_prioritizer import reorder_by_priority

        cols = _cols("A", "B")
        self.assertEqual(reorder_by_priority(cols, None), cols)


class TestPickRevenueAndCostColumns(unittest.TestCase):
    def test_picks_cost_tier_and_avoids_profit_as_revenue(self):
        from utils.kpi_prioritizer import pick_revenue_and_cost_columns

        cols = _cols("Profit", "Sales", "COGS", "Manufacturing Price")
        rev, cost = pick_revenue_and_cost_columns(cols)
        self.assertEqual(cost["name"], "COGS")
        self.assertEqual(rev["name"], "Sales")
        self.assertNotEqual(rev["name"], "Profit")

    def test_falls_back_to_positional_when_no_cost_column(self):
        from utils.kpi_prioritizer import pick_revenue_and_cost_columns

        cols = _cols("Amount1", "Amount2")
        rev, cost = pick_revenue_and_cost_columns(cols)
        self.assertEqual(rev["name"], "Amount1")
        self.assertEqual(cost["name"], "Amount2")

    def test_empty_input(self):
        from utils.kpi_prioritizer import pick_revenue_and_cost_columns

        self.assertEqual(pick_revenue_and_cost_columns([]), (None, None))

    def test_cost_fallback_never_picks_a_profit_tier_column(self):
        """Regression: when no explicit cost-tier column exists at all, the
        positional cost fallback must still never select a profit-tier
        column — a margin needs (revenue, cost), never (revenue, profit)."""
        from utils.kpi_prioritizer import pick_revenue_and_cost_columns

        # Priority-reordered order with Profit first and NO cost-tier column.
        cols = _cols("Profit", "Gross Sales", "Sales", "Manufacturing Price",
                      "Sale Price", "Discounts")
        rev, cost = pick_revenue_and_cost_columns(cols)
        self.assertIsNotNone(cost)
        self.assertNotEqual(cost["name"], "Profit")
        self.assertNotEqual(rev["name"], "Profit")


class TestDaxAgentUsesPrioritization(unittest.TestCase):
    def _sample_data_like_schema(self) -> dict:
        return {
            "table_name": "Sales",
            "columns": [
                {"name": "Manufacturing Price", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Sale Price", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Gross Sales", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Discounts", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Sales", "dataType": "double", "summarizeBy": "sum"},
                {"name": "COGS", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Profit", "dataType": "double", "summarizeBy": "sum"},
                {"name": "Segment", "dataType": "string", "summarizeBy": "none"},
                {"name": "Date", "dataType": "dateTime", "summarizeBy": "none"},
            ],
        }

    def test_guaranteed_measures_anchor_on_sales_or_profit_not_manufacturing_price(self):
        from agents.base import AgentContext
        from agents.dax_agent import DAXAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="executive sales performance dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = self._sample_data_like_schema()

        result = DAXAgent(ctx).run()
        self.assertTrue(result.ok, result.message)

        yoy = next((m for m in ctx.measures if "yoy" in m["name"].lower()), None)
        anomaly = next((m for m in ctx.measures if "anomaly" in m["name"].lower()), None)
        self.assertIsNotNone(yoy)
        self.assertIsNotNone(anomaly)
        self.assertNotIn("Manufacturing Price", yoy["name"])
        self.assertNotIn("Manufacturing Price", anomaly["name"])
        self.assertTrue(
            "Sales" in yoy["name"] or "Profit" in yoy["name"],
            f"expected Sales/Profit anchored measure, got: {yoy['name']}",
        )

    def test_orchestrator_computed_ranking_is_honored_over_local_fallback(self):
        """When ctx.extra['prioritized_kpis'] is pre-set (as the orchestrator
        would), DAXAgent must defer to it rather than recomputing locally."""
        from agents.base import AgentContext
        from agents.dax_agent import DAXAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = self._sample_data_like_schema()
        # Force a deliberately "wrong" (but valid) order to prove it's honored.
        ctx.extra["prioritized_kpis"] = ["Discounts", "Sales", "Profit"]

        result = DAXAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        anomaly = next((m for m in ctx.measures if "anomaly" in m["name"].lower()), None)
        self.assertIsNotNone(anomaly)
        self.assertIn("Discounts", anomaly["name"])


class TestProfitabilityBuilderNeverMisusesProfit(unittest.TestCase):
    def test_gross_margin_uses_gross_sales_and_cogs_not_profit(self):
        """End-to-end regression for the exact SampleData.csv column set:
        Profit ranks #1 by priority, but the profitability strategy's
        margin calc must anchor on (Gross Sales, COGS), never on Profit."""
        from agents.dax_agent import DAXAgent, _classify_columns
        from utils.kpi_prioritizer import rank_kpi_candidates, reorder_by_priority

        cols = [
            {"name": "Manufacturing Price", "dataType": "double"},
            {"name": "Sale Price", "dataType": "double"},
            {"name": "Gross Sales", "dataType": "double"},
            {"name": "Discounts", "dataType": "double"},
            {"name": "Sales", "dataType": "double"},
            {"name": "COGS", "dataType": "double"},
            {"name": "Profit", "dataType": "double"},
            {"name": "Segment", "dataType": "string"},
        ]
        buckets = _classify_columns(cols)
        self.assertIn("COGS", [c["name"] for c in buckets["amount"]])

        order = rank_kpi_candidates(buckets["amount"])
        buckets["amount"] = reorder_by_priority(buckets["amount"], order)
        self.assertEqual(order[0], "Profit")

        agent = DAXAgent.__new__(DAXAgent)
        measures = agent._build_measures_profitability("Sales", buckets)
        margin = next(m for m in measures if m["name"] == "Gross Margin %")
        self.assertIn("Gross Sales", margin["expression"])
        self.assertIn("COGS", margin["expression"])
        self.assertNotIn("[Profit]", margin["expression"])


class TestReportAgentAmountMeasure(unittest.TestCase):
    def test_prefers_prioritized_kpi_measure(self):
        from agents.report_agent import ReportAgent

        measures = [
            {"name": "Total Manufacturing Price"},
            {"name": "Total Sales"},
        ]
        chosen = ReportAgent._amount_measure(measures, {}, prioritized_kpis=["Sales"])
        self.assertEqual(chosen, "Total Sales")

    def test_falls_through_priority_list_when_first_has_no_measure(self):
        from agents.report_agent import ReportAgent

        measures = [{"name": "Total Manufacturing Price"}]
        chosen = ReportAgent._amount_measure(
            measures, {}, prioritized_kpis=["Sales", "Manufacturing Price"],
        )
        self.assertEqual(chosen, "Total Manufacturing Price")

    def test_no_prioritized_kpis_keeps_old_behavior(self):
        from agents.report_agent import ReportAgent

        measures = [{"name": "Order Count"}, {"name": "Total Sales"}]
        chosen = ReportAgent._amount_measure(measures, {})
        self.assertEqual(chosen, "Total Sales")

    def test_outcome_measure_wins_over_generic_filler_when_no_amount_column(self):
        """Regression for the bank-marketing gap: when no 'Total X' measure
        exists at all (no monetary amount column in the dataset), the
        guaranteed outcome-rate measure must be chosen over the raw first
        measure in the list (a generic filler like 'Order Count')."""
        from agents.report_agent import ReportAgent

        measures = [
            {"name": "Order Count"}, {"name": "Min age"},
            {"name": "Conversion Rate %"}, {"name": "Conversion Count"},
        ]
        chosen = ReportAgent._amount_measure(
            measures, {}, outcome_measure="Conversion Rate %",
        )
        self.assertEqual(chosen, "Conversion Rate %")

    def test_outcome_measure_loses_to_prioritized_kpi(self):
        """A deliberate business-priority ranking still wins over the
        outcome-rate guarantee when both exist."""
        from agents.report_agent import ReportAgent

        measures = [{"name": "Total Sales"}, {"name": "Conversion Rate %"}]
        chosen = ReportAgent._amount_measure(
            measures, {}, prioritized_kpis=["Sales"], outcome_measure="Conversion Rate %",
        )
        self.assertEqual(chosen, "Total Sales")

    def test_outcome_measure_ignored_when_not_actually_in_measures(self):
        from agents.report_agent import ReportAgent

        measures = [{"name": "Total Sales"}]
        chosen = ReportAgent._amount_measure(
            measures, {}, outcome_measure="Conversion Rate %",
        )
        self.assertEqual(chosen, "Total Sales")


class TestReportAgentCardPoolSurfacesPrimaryMeasure(unittest.TestCase):
    def test_primary_measure_card_survives_truncation(self):
        """The guaranteed outcome-rate measure's card must sort to the front
        of the candidate pool, so the max_visuals_per_page cap (applied
        before VisualPlannerAgent ever sees the pool) can't silently drop
        it in favor of a generic filler measure's card."""
        from agents.report_agent import ReportAgent

        agent = ReportAgent.__new__(ReportAgent)
        measures = [
            {"name": "Order Count"}, {"name": "Min age"}, {"name": "Max age"},
            {"name": "Conversion Rate %"}, {"name": "Conversion Count"},
        ]
        buckets = {"category": [], "region": [], "other": [], "date": [],
                   "amount": [], "qty": []}
        plans = agent._plan_visuals(
            "T", buckets, measures, outcome_measure="Conversion Rate %",
        )
        cards = [p for p in plans if p["kind"] == "card"]
        self.assertEqual(cards[0]["measure"], "Conversion Rate %")


class TestInsightsEngineUsesPrioritization(unittest.TestCase):
    def test_generate_insights_anchors_on_prioritized_column(self):
        import pandas as pd
        from utils.insights_engine import generate_insights

        df = pd.DataFrame({
            "Segment": ["A", "A", "B", "B", "C", "C"] * 4,
            "Manufacturing Price": [500.0] * 24,
            "Sales": [10.0, 20.0, 200.0, 220.0, 5.0, 6.0] * 4,
        })
        schema_columns = [
            {"name": "Segment", "dataType": "string"},
            {"name": "Manufacturing Price", "dataType": "double"},
            {"name": "Sales", "dataType": "double"},
        ]
        insights = generate_insights(
            df=df, schema_columns=schema_columns, business_analysis=None,
            report_plan=None, ctx_pages=[], judge_result=None,
            prioritized_kpis=["Sales", "Manufacturing Price"],
        )
        self.assertTrue(insights.segments)
        self.assertEqual(insights.segments[0].primary_metric, "Sales")


class TestJudgePrimaryKpiCoverage(unittest.TestCase):
    def _ctx(self, **extra_overrides):
        ctx = SimpleNamespace()
        ctx.measures = []
        ctx.schema = {"columns": []}
        ctx.pages = []
        ctx.extra = {
            "report_plan": None,
            "business_analysis": None,
            "report_style": "standard",
            "bi_reasoning": None,
        }
        ctx.extra.update(extra_overrides)
        return ctx

    def test_uncovered_primary_kpi_flagged(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx(prioritized_kpis=["Sales", "Profit"])
        ctx.measures = [{"name": "Total Manufacturing Price", "expression": "SUM(x)"}]
        result = JudgeLayer().evaluate(ctx)
        self.assertEqual(result["primary_kpi"], "Sales")
        self.assertFalse(result["primary_kpi_covered"])
        self.assertTrue(any(
            oa.get("reason") == "primary_kpi_uncovered" for oa in result["override_actions"]
        ))
        self.assertTrue(any(
            g.get("suggested_synthesis") == "kpi_gap_fill" for g in result["strategy_gaps"]
        ))

    def test_covered_primary_kpi_not_flagged(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx(prioritized_kpis=["Sales", "Profit"])
        ctx.measures = [{"name": "Total Sales", "expression": "SUM(x)"}]
        result = JudgeLayer().evaluate(ctx)
        self.assertEqual(result["primary_kpi"], "Sales")
        self.assertTrue(result["primary_kpi_covered"])

    def test_no_prioritized_kpis_defaults_to_covered(self):
        from utils.judge import JudgeLayer

        result = JudgeLayer().evaluate(self._ctx())
        self.assertIsNone(result["primary_kpi"])
        self.assertTrue(result["primary_kpi_covered"])


class TestStrategySynthesizerPrimaryKpiTieIn(unittest.TestCase):
    def test_primary_kpi_uncovered_triggers_kpi_gap_fill(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {
            "override_actions": [{
                "action": "rerun_agent", "agent": "DAXAgent",
                "reason": "primary_kpi_uncovered",
                "detail": {"primary_kpi": "Sales"},
                "severity": "warning",
            }],
        }
        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=[], judge_signals=judge_signals,
            low_performing_clusters=[], current_strategy_pool=[],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["generation_rule"], "kpi_gap_fill")
        self.assertIn("sales", spec["parameters"]["target_keywords"])


class TestSemanticTieBreak(unittest.TestCase):
    def test_semantic_model_breaks_tie_toward_net_over_gross(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        cols = _cols("Gross Sales", "Sales")  # same "sales" tier -> tied
        semantic_model = {"canonical_metrics": {
            "gross_revenue": "Gross Sales", "net_revenue": "Sales",
        }}
        ranked = rank_kpi_candidates(cols, semantic_model=semantic_model)
        self.assertEqual(ranked[0], "Sales")

    def test_without_semantic_model_falls_back_to_column_order(self):
        from utils.kpi_prioritizer import rank_kpi_candidates

        cols = _cols("Gross Sales", "Sales")
        ranked = rank_kpi_candidates(cols)
        self.assertEqual(ranked[0], "Gross Sales")  # documented last-resort behavior


class TestDeriveCandidateKpis(unittest.TestCase):
    def test_uses_semantic_model_when_available(self):
        from utils.kpi_prioritizer import derive_candidate_kpis

        cols = _cols("Gross Sales", "Discounts", "Sales", "COGS", "Profit")
        semantic_model = {"canonical_metrics": {
            "gross_revenue": "Gross Sales", "net_revenue": "Sales",
            "deduction": "Discounts", "profit": "Profit", "cost": "COGS",
        }}
        candidates = derive_candidate_kpis(semantic_model, cols)
        by_concept = {c["concept"]: c for c in candidates}
        self.assertEqual(by_concept["margin"]["numerator"], "Profit")
        self.assertEqual(by_concept["margin"]["denominator"], "Sales")
        self.assertEqual(by_concept["discount"]["numerator"], "Discounts")
        self.assertEqual(by_concept["discount"]["denominator"], "Gross Sales")
        self.assertEqual(by_concept["cost"]["numerator"], "COGS")
        self.assertEqual(by_concept["cost"]["denominator"], "Sales")
        self.assertTrue(all(c["source"] == "semantic_model" for c in candidates))

    def test_falls_back_to_tier_keywords_without_semantic_model(self):
        from utils.kpi_prioritizer import derive_candidate_kpis

        cols = _cols("Manufacturing Price", "Discounts", "Sales", "COGS", "Profit")
        candidates = derive_candidate_kpis(None, cols)
        by_concept = {c["concept"]: c for c in candidates}
        self.assertIn("margin", by_concept)
        self.assertEqual(by_concept["margin"]["numerator"], "Profit")
        self.assertTrue(all(c["source"] == "tier_fallback" for c in candidates))

    def test_no_usable_columns_returns_empty(self):
        from utils.kpi_prioritizer import derive_candidate_kpis

        self.assertEqual(derive_candidate_kpis(None, []), [])


class TestIsRateColumn(unittest.TestCase):
    def test_rate_columns_detected(self):
        from utils.kpi_prioritizer import is_rate_column

        self.assertTrue(is_rate_column("Manufacturing Price"))
        self.assertTrue(is_rate_column("Sale Price"))
        self.assertTrue(is_rate_column("Interest Rate"))

    def test_flow_columns_not_detected(self):
        from utils.kpi_prioritizer import is_rate_column

        self.assertFalse(is_rate_column("Sales"))
        self.assertFalse(is_rate_column("Profit"))
        self.assertFalse(is_rate_column("Units Sold"))

    def test_unconventional_rate_naming_generalizes(self):
        """Part 2 robustness finding: 'price'/'rate' alone missed real
        per-unit metrics named with generic BI-convention prefixes/suffixes
        rather than the words "price"/"rate" themselves."""
        from utils.kpi_prioritizer import is_rate_column

        self.assertTrue(is_rate_column("per_unit_cost"))
        self.assertTrue(is_rate_column("avg_ticket"))
        self.assertTrue(is_rate_column("unit_margin"))
        self.assertTrue(is_rate_column("yield_pct"))

    def test_domain_specific_rate_terms_are_a_disclosed_limitation(self):
        """These are genuinely rate-shaped in marketing/ops domains, but
        enumerating every domain's rate vocabulary doesn't generalize --
        deliberately left uncaught rather than growing an ever-expanding,
        still-incomplete keyword list."""
        from utils.kpi_prioritizer import is_rate_column

        self.assertFalse(is_rate_column("conversion"))
        self.assertFalse(is_rate_column("click_through"))


class TestDaxAgentConceptCoverageAndSanitization(unittest.TestCase):
    def _agent(self, extra=None):
        from agents.dax_agent import DAXAgent
        from types import SimpleNamespace

        agent = DAXAgent.__new__(DAXAgent)
        agent.log = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        agent.context = SimpleNamespace(extra=extra or {})
        return agent

    def test_ensure_concept_coverage_measures_fills_gap(self):
        agent = self._agent(extra={
            "business_concepts": ["margin", "discount"],
            "derived_kpi_candidates": [
                {"concept": "margin", "name": "Profit Margin %",
                 "numerator": "Profit", "denominator": "Sales"},
                {"concept": "discount", "name": "Discount Rate %",
                 "numerator": "Discounts", "denominator": "Gross Sales"},
            ],
        })
        measures = [{"name": "Total Sales", "expression": "SUM('T'[Sales])",
                     "displayFolder": "KPI", "description": "", "formatString": ""}]
        out = agent._ensure_concept_coverage_measures(measures, "T")
        names = {m["name"] for m in out}
        self.assertIn("Profit Margin %", names)
        self.assertIn("Discount Rate %", names)

    def test_ensure_concept_coverage_measures_skips_when_already_covered(self):
        agent = self._agent(extra={
            "business_concepts": ["margin"],
            "derived_kpi_candidates": [
                {"concept": "margin", "name": "Profit Margin %",
                 "numerator": "Profit", "denominator": "Sales"},
            ],
        })
        measures = [{"name": "Profit Margin %", "expression": "DIVIDE(SUM('T'[Profit]),SUM('T'[Sales]),0)",
                     "displayFolder": "KPI", "description": "", "formatString": ""}]
        out = agent._ensure_concept_coverage_measures(measures, "T")
        self.assertEqual(len(out), 1)  # not duplicated

    def test_ensure_concept_coverage_measures_noop_without_concepts(self):
        agent = self._agent(extra={})
        measures = [{"name": "Total Sales"}]
        out = agent._ensure_concept_coverage_measures(measures, "T")
        self.assertEqual(out, measures)

    def test_sanitize_rate_aggregations_rewrites_sum_to_average(self):
        agent = self._agent()
        measures = [
            {"name": "Total Manufacturing Price", "expression": "SUM('T'[Manufacturing Price])",
             "displayFolder": "KPI", "description": "x", "formatString": "$"},
            {"name": "Total Sales", "expression": "SUM('T'[Sales])",
             "displayFolder": "KPI", "description": "y", "formatString": "$"},
        ]
        out = agent._sanitize_rate_aggregations(measures)
        by_name = {m["name"]: m for m in out}
        self.assertIn("Avg Manufacturing Price", by_name)
        self.assertEqual(by_name["Avg Manufacturing Price"]["expression"],
                          "AVERAGE('T'[Manufacturing Price])")
        self.assertEqual(by_name["Total Sales"]["expression"], "SUM('T'[Sales])")

    def test_sanitize_rate_aggregations_leaves_non_sum_measures_untouched(self):
        agent = self._agent()
        measures = [{"name": "Profit Margin %", "expression": "DIVIDE(SUM('T'[Profit]),SUM('T'[Sales]),0)",
                     "displayFolder": "KPI", "description": "", "formatString": ""}]
        out = agent._sanitize_rate_aggregations(measures)
        self.assertEqual(out[0]["expression"], measures[0]["expression"])

    def test_dedupe_measures_by_name_drops_later_duplicate(self):
        """Regression, confirmed live: Power BI Desktop rejected a
        generated project outright on open -- "Failed to add a
        deserialized Measure object into the model - name: 'Avg Contacts
        per Client', detailed error: Item 'Avg Contacts per Client'
        already exists in the collection." write_tmdl_measures only
        dedupes against measures already on disk from a PRIOR call; it
        has no visibility into a collision within the SAME incoming
        batch, so both copies got written and Power BI's TMDL
        deserializer rejected the duplicate name."""
        agent = self._agent()
        measures = [
            {"name": "Avg Contacts per Client", "expression": "AVERAGE('T'[Contacts])",
             "displayFolder": "Insights", "description": "first", "formatString": ""},
            {"name": "Total Sales", "expression": "SUM('T'[Sales])",
             "displayFolder": "KPI", "description": "", "formatString": "$"},
            {"name": "Avg Contacts per Client", "expression": "AVERAGE('T'[Contacts])",
             "displayFolder": "Stats", "description": "second (duplicate)", "formatString": ""},
        ]
        out = agent._dedupe_measures_by_name(measures)
        names = [m["name"] for m in out]
        self.assertEqual(names.count("Avg Contacts per Client"), 1)
        self.assertEqual(len(out), 2)
        # first occurrence wins
        self.assertEqual(
            [m for m in out if m["name"] == "Avg Contacts per Client"][0]["description"],
            "first",
        )

    def test_dedupe_measures_by_name_noop_when_no_collision(self):
        agent = self._agent()
        measures = [
            {"name": "Total Sales", "expression": "SUM('T'[Sales])"},
            {"name": "Total Profit", "expression": "SUM('T'[Profit])"},
        ]
        out = agent._dedupe_measures_by_name(measures)
        self.assertEqual(out, measures)

    def test_sanitize_then_dedupe_catches_rename_collision(self):
        """The exact live scenario end-to-end: _sanitize_rate_aggregations
        renames "Total Manufacturing Price" to "Avg Manufacturing Price"
        (a rate column summed is meaningless), landing on a name a
        DIFFERENT measure in the same batch already has -- the dedupe
        pass immediately after must catch this renamed collision too,
        not just literal duplicates present before sanitization."""
        agent = self._agent()
        measures = [
            {"name": "Avg Manufacturing Price", "expression": "AVERAGE('T'[Manufacturing Price])",
             "displayFolder": "Insights", "description": "original avg", "formatString": "$"},
            {"name": "Total Manufacturing Price", "expression": "SUM('T'[Manufacturing Price])",
             "displayFolder": "Stats", "description": "will be renamed", "formatString": "$"},
        ]
        sanitized = agent._sanitize_rate_aggregations(measures)
        deduped = agent._dedupe_measures_by_name(sanitized)
        names = [m["name"] for m in deduped]
        self.assertEqual(names.count("Avg Manufacturing Price"), 1)

    def test_ensure_outcome_rate_measure_noop_without_outcome_column(self):
        agent = self._agent(extra={})
        measures = [{"name": "Order Count"}]
        out = agent._ensure_outcome_rate_measure(measures, "T")
        self.assertEqual(out, measures)

    def test_ensure_outcome_rate_measure_generic_name_and_dax(self):
        agent = self._agent(extra={
            "outcome_column": {
                "column": "y", "positive_value": "yes",
                "measure_name": "Conversion Rate %",
            },
        })
        out = agent._ensure_outcome_rate_measure([], "T")
        names = {m["name"] for m in out}
        self.assertIn("Conversion Rate %", names)
        self.assertIn("Conversion Count", names)

        rate = next(m for m in out if m["name"] == "Conversion Rate %")
        self.assertIn("DIVIDE(COUNTROWS(", rate["expression"])
        self.assertIn('[y] = "yes"', rate["expression"])

        count = next(m for m in out if m["name"] == "Conversion Count")
        self.assertIn("COUNTROWS(FILTER(", count["expression"])
        self.assertIn('[y] = "yes"', count["expression"])

    def test_ensure_outcome_rate_measure_column_specific_name(self):
        agent = self._agent(extra={
            "outcome_column": {
                "column": "subscribed", "positive_value": "True",
                "measure_name": "Subscribed Rate %",
            },
        })
        out = agent._ensure_outcome_rate_measure([], "T")
        names = {m["name"] for m in out}
        self.assertIn("Subscribed Rate %", names)
        self.assertIn("Subscribed Count", names)

    def test_ensure_outcome_rate_measure_dedups_when_already_present(self):
        agent = self._agent(extra={
            "outcome_column": {
                "column": "y", "positive_value": "yes",
                "measure_name": "Conversion Rate %",
            },
        })
        measures = [{"name": "Conversion Rate %", "expression": "already there"}]
        out = agent._ensure_outcome_rate_measure(measures, "T")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["expression"], "already there")

    def test_ensure_outcome_rate_measure_escapes_embedded_quote_in_value(self):
        agent = self._agent(extra={
            "outcome_column": {
                "column": "status", "positive_value": 'Say "yes"',
                "measure_name": "Status Rate %",
            },
        })
        out = agent._ensure_outcome_rate_measure([], "T")
        rate = next(m for m in out if m["name"] == "Status Rate %")
        self.assertIn('""yes""', rate["expression"])


class TestJudgeEvaluationLayerFix(unittest.TestCase):
    def _ctx(self, **extra_overrides):
        from types import SimpleNamespace
        ctx = SimpleNamespace()
        ctx.measures = []
        ctx.schema = {"columns": []}
        ctx.pages = []
        ctx.extra = {
            "report_plan": None, "business_analysis": None,
            "report_style": "standard", "bi_reasoning": None,
        }
        ctx.extra.update(extra_overrides)
        return ctx

    def test_new_score_keys_present_and_neutral_by_default(self):
        from utils.judge import JudgeLayer

        result = JudgeLayer().evaluate(self._ctx())
        self.assertEqual(result["concept_coverage_score"], 1.0)
        self.assertEqual(result["semantic_correctness_score"], 1.0)
        self.assertEqual(result["kpi_appropriateness_score"], 1.0)

    def test_kpi_appropriateness_penalizes_summed_rate_column(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx()
        ctx.measures = [{"name": "Total Manufacturing Price",
                          "expression": "SUM([Manufacturing Price])"}]
        result = JudgeLayer().evaluate(ctx)
        self.assertLess(result["kpi_appropriateness_score"], 1.0)

    def test_concept_coverage_gap_surfaces_conflict(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx(business_concepts=["discount"])
        ctx.measures = [{"name": "Total Sales", "expression": "SUM([Sales])"}]
        result = JudgeLayer().evaluate(ctx)
        self.assertLess(result["concept_coverage_score"], 1.0)
        self.assertTrue(any(
            oa.get("reason") == "concept_coverage_incomplete" for oa in result["override_actions"]
        ))

    def test_semantic_correctness_penalizes_wrong_pair(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx(
            semantic_model={"canonical_metrics": {"profit": "Profit", "net_revenue": "Sales"}},
            derived_kpi_candidates=[{
                "concept": "margin", "numerator": "Profit", "denominator": "Sales",
                "source": "semantic_model",
            }],
        )
        # Uses the WRONG pair (Profit / Gross Sales instead of Profit / Sales)
        ctx.measures = [{"name": "Bad Margin %",
                          "expression": "DIVIDE(SUM([Profit]),SUM([Gross Sales]),0)"}]
        result = JudgeLayer().evaluate(ctx)
        self.assertLess(result["semantic_correctness_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
