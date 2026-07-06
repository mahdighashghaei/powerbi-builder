"""tests/test_strategy_synthesis.py — Unit tests for the Strategy Synthesis Layer.

Covers:
  - StrategySynthesizer.synthesize_dax_strategy / synthesize_schema_strategy /
    synthesize_visual_strategy — None when healthy, well-formed spec when a
    failure signal is present.
  - StrategySynthesizer.synthesize_new_strategies — aggregates all three domains.
  - apply_dax_strategy / apply_schema_strategy_spec / apply_visual_strategy_spec
    — spec -> candidate interpreters, fail-safe on unknown rules.
  - LearningMemory strategy tracking: record_strategy_outcome ->
    get_strategy_success_rate -> decay_weak_strategies -> prune_strategies.
  - JudgeLayer.evaluate() "strategy_gaps" key.
  - End-to-end smoke: a seeded synthesized DAX strategy shows up in
    ctx.extra["dax_candidates"] after DAXAgent._run().

All tests are pure unit tests (no LLM calls; MCP file I/O only via tmp paths
for the DAXAgent smoke test, mirroring tests/test_phase1_schemas_state.py).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# 1 — StrategySynthesizer.synthesize_dax_strategy
# ---------------------------------------------------------------------------

class TestSynthesizeDaxStrategy(unittest.TestCase):
    def test_no_signal_returns_none(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=[], judge_signals={}, low_performing_clusters=[],
            current_strategy_pool=[],
        )
        self.assertIsNone(spec)

    def test_kpi_coverage_low_override_action_yields_kpi_gap_fill(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {
            "kpi_coverage": 0.2,
            "override_actions": [{
                "action": "rerun_agent", "agent": "DAXAgent",
                "reason": "kpi_coverage_low",
                "detail": {"covered": 1, "total": 5, "missing": ["Revenue", "Margin"]},
                "severity": "warning",
            }],
        }
        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=[], judge_signals=judge_signals,
            low_performing_clusters=[], current_strategy_pool=["revenue_first"],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["strategy_type"], "dax")
        self.assertEqual(spec["generation_rule"], "kpi_gap_fill")
        self.assertIn("revenue", spec["parameters"]["target_keywords"])
        self.assertTrue(spec["strategy_id"].startswith("synth_dax_kpi_gap_fill_"))
        self.assertGreaterEqual(spec["expected_improvement_target"], 0.2)

    def test_schema_drift_yields_schema_safe_measures(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {
            "schema_measure_drift": [
                "measure='Total X' references unknown column(s): ['GhostCol']",
            ],
        }
        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=[], judge_signals=judge_signals,
            low_performing_clusters=[], current_strategy_pool=[],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["generation_rule"], "schema_safe_measures")
        self.assertIn("GhostCol", spec["parameters"]["exclude_hint_patterns"])

    def test_generic_failure_pattern_volume_triggers_kpi_gap_fill(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        failure_patterns = [
            {"candidate_id": "revenue_first", "semantic_score": 0.3,
             "context": {"description": "sales revenue dashboard"}},
            {"candidate_id": "operational", "semantic_score": 0.35,
             "context": {"description": "sales revenue dashboard"}},
        ]
        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=failure_patterns, judge_signals={},
            low_performing_clusters=[], current_strategy_pool=[],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["generation_rule"], "kpi_gap_fill")

    def test_strategy_id_avoids_collision_with_existing_pool(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {"kpi_coverage": 0.1}
        pool = ["synth_dax_kpi_gap_fill_1"]
        spec = StrategySynthesizer().synthesize_dax_strategy(
            failure_patterns=[], judge_signals=judge_signals,
            low_performing_clusters=[], current_strategy_pool=pool,
        )
        self.assertEqual(spec["strategy_id"], "synth_dax_kpi_gap_fill_2")


# ---------------------------------------------------------------------------
# 2 — synthesize_schema_strategy / synthesize_visual_strategy
# ---------------------------------------------------------------------------

class TestSynthesizeSchemaAndVisualStrategy(unittest.TestCase):
    def test_schema_no_signal_returns_none(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        spec = StrategySynthesizer().synthesize_schema_strategy(
            failure_patterns=[], judge_signals={}, low_performing_clusters=[],
        )
        self.assertIsNone(spec)

    def test_schema_kpi_gap_yields_targeted_kpi_boost(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {"kpi_coverage": 0.3}
        spec = StrategySynthesizer().synthesize_schema_strategy(
            failure_patterns=[], judge_signals=judge_signals, low_performing_clusters=[],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["strategy_type"], "schema")
        self.assertEqual(spec["generation_rule"], "targeted_kpi_boost")

    def test_visual_no_signal_returns_none(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        spec = StrategySynthesizer().synthesize_visual_strategy(
            failure_patterns=[], judge_signals={}, low_performing_clusters=[],
        )
        self.assertIsNone(spec)

    def test_visual_coherence_low_yields_coherence_gap_fill(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_signals = {
            "visual_coherence": 0.1,
            "override_actions": [{
                "action": "rerun_agent", "agent": "ReportAgent",
                "reason": "visual_semantic_inconsistency",
                "detail": {
                    "dashboard_type": "executive",
                    "actual_kinds": ["barChart"],
                    "expected_kinds": ["card", "kpi", "columnChart", "lineChart"],
                    "coherence_score": 0.1,
                },
                "severity": "warning",
            }],
        }
        spec = StrategySynthesizer().synthesize_visual_strategy(
            failure_patterns=[], judge_signals=judge_signals, low_performing_clusters=[],
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec["generation_rule"], "coherence_gap_fill")
        self.assertTrue(set(spec["parameters"]["missing_kinds"]).issubset(
            {"card", "kpi", "columnChart", "lineChart"}
        ))


# ---------------------------------------------------------------------------
# 3 — synthesize_new_strategies aggregation
# ---------------------------------------------------------------------------

class TestSynthesizeNewStrategies(unittest.TestCase):
    def test_aggregates_across_domains(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        judge_result = {
            "kpi_coverage": 0.2,
            "visual_coherence": 0.1,
            "override_actions": [],
            "strategy_gaps": [
                {"domain": "dax", "missing_pattern": "kpi gap", "suggested_synthesis": "kpi_gap_fill"},
                {"domain": "visual", "missing_pattern": "coherence gap", "suggested_synthesis": "coherence_gap_fill"},
            ],
        }
        result = StrategySynthesizer().synthesize_new_strategies(
            domain_pool_ids={"dax": [], "schema": [], "visual": []},
            judge_result=judge_result,
        )
        self.assertEqual(set(result.keys()), {"dax", "schema", "visual"})
        self.assertGreaterEqual(len(result["dax"]), 1)
        self.assertGreaterEqual(len(result["schema"]), 1)
        # visual only has a generic strategy_gaps entry (no override_action detail)
        # -> still triggers because domain == "visual" is present in strategy_gaps
        self.assertGreaterEqual(len(result["visual"]), 1)

    def test_healthy_signals_yield_empty_lists(self):
        from utils.strategy_synthesizer import StrategySynthesizer

        result = StrategySynthesizer().synthesize_new_strategies(
            domain_pool_ids={"dax": [], "schema": [], "visual": []},
            judge_result={"kpi_coverage": 1.0, "visual_coherence": 1.0},
        )
        self.assertEqual(result, {"dax": [], "schema": [], "visual": []})


# ---------------------------------------------------------------------------
# 4 — apply_* interpreters
# ---------------------------------------------------------------------------

class TestApplyInterpreters(unittest.TestCase):
    def _buckets(self):
        return {
            "amount": [{"name": "Revenue", "dataType": "double"},
                       {"name": "Cost", "dataType": "double"}],
            "qty": [{"name": "Units", "dataType": "int64"}],
            "date": [], "region": [], "category": [], "other_numeric": [], "other": [],
        }

    def test_apply_dax_kpi_gap_fill_targets_matching_columns(self):
        from utils.strategy_synthesizer import apply_dax_strategy

        spec = {
            "strategy_id": "synth_dax_kpi_gap_fill_1", "strategy_type": "dax",
            "generation_rule": "kpi_gap_fill",
            "parameters": {"target_keywords": ["revenue"]},
            "expected_improvement_target": 0.6,
        }
        measures = apply_dax_strategy(spec, "Sales", self._buckets(), biz_analysis=None)
        self.assertTrue(measures)
        names = [m["name"] for m in measures]
        self.assertTrue(any("Revenue" in n for n in names))
        for m in measures:
            self.assertIn("displayFolder", m)
            self.assertIn("expression", m)

    def test_apply_dax_schema_safe_measures_excludes_drifted_column(self):
        from utils.strategy_synthesizer import apply_dax_strategy

        spec = {
            "strategy_id": "synth_dax_schema_safe_measures_1", "strategy_type": "dax",
            "generation_rule": "schema_safe_measures",
            "parameters": {"exclude_hint_patterns": ["Revenue"]},
            "expected_improvement_target": 0.6,
        }
        measures = apply_dax_strategy(spec, "Sales", self._buckets(), biz_analysis=None)
        names = [m["name"] for m in measures]
        self.assertFalse(any("Revenue" in n for n in names))
        self.assertTrue(any("Cost" in n for n in names))

    def test_apply_dax_unknown_rule_returns_empty(self):
        from utils.strategy_synthesizer import apply_dax_strategy

        spec = {"strategy_id": "x", "generation_rule": "nonexistent_rule", "parameters": {}}
        self.assertEqual(apply_dax_strategy(spec, "Sales", self._buckets()), [])

    def test_apply_schema_targeted_kpi_boost(self):
        from utils.strategy_synthesizer import apply_schema_strategy_spec

        base_cols = [
            {"name": "Revenue", "dataType": "double", "summarizeBy": "none", "sourceColumn": "Revenue"},
            {"name": "Id", "dataType": "int64", "summarizeBy": "none", "sourceColumn": "Id"},
        ]
        spec = {
            "strategy_id": "synth_schema_targeted_kpi_boost_1",
            "generation_rule": "targeted_kpi_boost",
            "parameters": {"target_keywords": ["revenue"]},
        }
        cols = apply_schema_strategy_spec(spec, base_cols, set(), set(), set())
        by_name = {c["name"]: c for c in cols}
        self.assertEqual(by_name["Revenue"]["summarizeBy"], "sum")

    def test_apply_schema_unknown_rule_returns_empty(self):
        from utils.strategy_synthesizer import apply_schema_strategy_spec

        spec = {"strategy_id": "x", "generation_rule": "nonexistent_rule", "parameters": {}}
        self.assertEqual(apply_schema_strategy_spec(spec, [], set(), set(), set()), [])

    def test_apply_visual_coherence_gap_fill_reorders(self):
        from utils.strategy_synthesizer import apply_visual_strategy_spec

        candidates = [
            {"name": "v1", "kind": "table"},
            {"name": "v2", "kind": "card"},
            {"name": "v3", "kind": "barChart"},
        ]
        spec = {
            "strategy_id": "synth_visual_coherence_gap_fill_1",
            "generation_rule": "coherence_gap_fill",
            "parameters": {"missing_kinds": ["card"]},
        }
        reordered = apply_visual_strategy_spec(spec, candidates)
        self.assertEqual(reordered[0]["kind"], "card")

    def test_apply_visual_unknown_rule_returns_empty(self):
        from utils.strategy_synthesizer import apply_visual_strategy_spec

        spec = {"strategy_id": "x", "generation_rule": "nonexistent_rule", "parameters": {}}
        self.assertEqual(apply_visual_strategy_spec(spec, [{"kind": "card"}]), [])


# ---------------------------------------------------------------------------
# 5 — LearningMemory strategy tracking + decay + pruning
# ---------------------------------------------------------------------------

class TestLearningMemoryStrategyTracking(unittest.TestCase):
    def _memory(self):
        from utils.learning_memory import LearningMemory

        tmp = tempfile.mkdtemp()
        return LearningMemory(Path(tmp) / "learning_memory.json")

    def test_record_and_success_rate(self):
        lm = self._memory()
        lm.record_strategy_outcome("synth_dax_kpi_gap_fill_1", success=True)
        lm.record_strategy_outcome("synth_dax_kpi_gap_fill_1", success=False)
        rate = lm.get_strategy_success_rate("synth_dax_kpi_gap_fill_1")
        self.assertAlmostEqual(rate, 0.5)

    def test_unknown_strategy_returns_neutral_rate(self):
        lm = self._memory()
        self.assertAlmostEqual(lm.get_strategy_success_rate("never_seen"), 0.5)

    def test_is_synthesized_auto_detected_from_prefix(self):
        lm = self._memory()
        lm.record_strategy_outcome("synth_dax_kpi_gap_fill_1", success=True)
        lm.record_strategy_outcome("revenue_first", success=True)
        strategies = lm._data["strategies"]  # noqa: SLF001 — white-box check
        self.assertTrue(strategies["synth_dax_kpi_gap_fill_1"]["is_synthesized"])
        self.assertFalse(strategies["revenue_first"]["is_synthesized"])

    def test_decay_and_prune_removes_persistently_weak_strategy(self):
        lm = self._memory()
        # 1 success out of 5 uses -> well below the 0.3 weak threshold
        for success in (True, False, False, False, False):
            lm.record_strategy_outcome("synth_dax_weak_1", success=success)
        # Strong strategy: 4/5 successes -> should survive
        for success in (True, True, True, True, False):
            lm.record_strategy_outcome("synth_dax_strong_1", success=success)

        # Repeated decay compounds strength down for the weak strategy only
        for _ in range(30):
            lm.decay_weak_strategies()

        pruned = lm.prune_strategies()
        self.assertIn("synth_dax_weak_1", pruned)
        self.assertNotIn("synth_dax_strong_1", pruned)

    def test_save_load_roundtrip_preserves_strategies(self):
        lm = self._memory()
        lm.record_strategy_outcome("synth_dax_kpi_gap_fill_1", success=True)
        lm.save()

        from utils.learning_memory import LearningMemory
        lm2 = LearningMemory(lm._path)  # noqa: SLF001
        lm2.load()
        self.assertAlmostEqual(lm2.get_strategy_success_rate("synth_dax_kpi_gap_fill_1"), 1.0)


# ---------------------------------------------------------------------------
# 6 — JudgeLayer.evaluate() "strategy_gaps"
# ---------------------------------------------------------------------------

class TestJudgeStrategyGaps(unittest.TestCase):
    def _ctx(self, **extra_overrides):
        from types import SimpleNamespace
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

    def test_healthy_ctx_has_empty_strategy_gaps(self):
        from utils.judge import JudgeLayer

        result = JudgeLayer().evaluate(self._ctx())
        self.assertIn("strategy_gaps", result)
        self.assertEqual(result["strategy_gaps"], [])

    def test_kpi_coverage_low_produces_dax_strategy_gap(self):
        from types import SimpleNamespace
        from utils.judge import JudgeLayer

        biz = SimpleNamespace(potential_kpis=["Revenue", "Margin", "Orders"])
        ctx = self._ctx(business_analysis=biz)
        ctx.measures = [{"name": "Something Else", "expression": "SUM(x)"}]
        result = JudgeLayer().evaluate(ctx)
        domains = {g["domain"] for g in result["strategy_gaps"]}
        self.assertIn("dax", domains)
        dax_gap = next(g for g in result["strategy_gaps"] if g["domain"] == "dax")
        self.assertEqual(dax_gap["suggested_synthesis"], "kpi_gap_fill")

    def test_schema_drift_produces_schema_strategy_gap(self):
        from utils.judge import JudgeLayer

        ctx = self._ctx()
        ctx.schema = {"columns": [{"name": "Revenue"}]}
        ctx.measures = [{"name": "Bad", "expression": "SUM([GhostColumn])"}]
        result = JudgeLayer().evaluate(ctx)
        domains = {g["domain"] for g in result["strategy_gaps"]}
        self.assertIn("schema", domains)


# ---------------------------------------------------------------------------
# 7 — End-to-end smoke: seeded synthesized DAX strategy flows into
#     ctx.extra["dax_candidates"] via a real DAXAgent._run() call.
# ---------------------------------------------------------------------------

class TestDaxAgentConsumesSynthesizedStrategy(unittest.TestCase):
    def test_seeded_dax_spec_appears_in_dax_candidates(self):
        from agents.base import AgentContext
        from agents.dax_agent import DAXAgent
        from mcp_server.server import PbipToolbox

        tmp = tempfile.mkdtemp()
        ctx = AgentContext(
            business_description="revenue dashboard",
            source_path=Path(tmp) / "data.csv",
            toolbox=PbipToolbox(tmp),
            project_name="TestProject",
            pbip_root=Path(tmp),
        )
        ctx.schema = {
            "table_name": "Sales",
            "columns": [
                {"name": "Revenue", "dataType": "double", "summarizeBy": "sum"},
                {"name": "OrderId", "dataType": "int64", "summarizeBy": "none"},
            ],
        }
        ctx.extra["synthesized_strategies"] = {
            "dax": [{
                "strategy_id": "synth_dax_kpi_gap_fill_1",
                "strategy_type": "dax",
                "generation_rule": "kpi_gap_fill",
                "parameters": {"target_keywords": ["revenue"]},
                "expected_improvement_target": 0.6,
            }],
            "schema": [], "visual": [],
        }

        result = DAXAgent(ctx).run()
        self.assertTrue(result.ok, result.message)
        candidate_ids = {c["candidate_id"] for c in ctx.extra.get("dax_candidates", [])}
        self.assertIn("synth_dax_kpi_gap_fill_1", candidate_ids)


if __name__ == "__main__":
    unittest.main()
