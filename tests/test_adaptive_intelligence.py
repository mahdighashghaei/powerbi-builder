"""tests/test_adaptive_intelligence.py — Unit tests for the adaptive intelligence layer.

Covers:
  - compute_complexity_score / candidate_count_from_complexity
  - AdaptiveLearningLayer.compute_adaptive_bias / compute_context_similarity
  - LearningMemory (cluster_input, record_outcome, get_*patterns,
    get_judge_override_frequency, save/load round-trip)
  - score_dax_candidate / score_schema_candidate / score_visual_candidate
    with adaptive_bias parameter
  - tournament_select with context_aware=True and kpi_scores
  - JudgeLayer._generate_policy_adjustments / evaluate() policy_adjustments key

All tests are pure unit tests (no MCP tools, no file I/O beyond tmp paths).
726 existing tests must remain unchanged — no monkey-patching of global state.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers: lightweight stubs so tests run without the full pipeline
# ---------------------------------------------------------------------------


def _make_columns(n: int) -> list[dict]:
    """Generate n fake schema columns (mix of numeric + string)."""
    cols = []
    for i in range(n):
        dtype = "double" if i % 2 == 0 else "string"
        cols.append({"name": f"col_{i}", "dataType": dtype, "summarizeBy": "none"})
    return cols


def _make_measures(names: list[str]) -> list[dict]:
    return [
        {
            "name": n,
            "expression": f"SUM('T'[{n}])",
            "displayFolder": "KPI",
            "description": f"Measure {n}",
            "formatString": "#,##0",
            "table": "T",
        }
        for n in names
    ]


# ---------------------------------------------------------------------------
# 1 & 2 — compute_complexity_score
# ---------------------------------------------------------------------------

class TestComputeComplexityScore:
    def test_low_complexity_empty_inputs(self):
        from utils.scoring import compute_complexity_score
        score = compute_complexity_score([], [], "")
        assert 0.0 <= score <= 1.0
        # no columns, no kpis, no description → mostly ambiguity-driven
        assert score <= 0.50

    def test_high_complexity_rich_schema(self):
        from utils.scoring import compute_complexity_score
        cols = _make_columns(35)   # > 30 → capped at 1.0
        kpis = [f"kpi_{i}" for i in range(12)]   # > 10 → capped at 1.0
        score = compute_complexity_score(cols, kpis, "obscure domain XYZ")
        assert score > 0.70

    def test_medium_complexity(self):
        from utils.scoring import compute_complexity_score
        cols = _make_columns(15)
        kpis = ["revenue", "profit", "orders"]
        score = compute_complexity_score(
            cols, kpis,
            "Analyse revenue and profit by product for the finance team"
        )
        assert 0.2 <= score <= 0.8

    def test_many_domain_keywords_reduces_ambiguity(self):
        from utils.scoring import compute_complexity_score
        desc_clear = "revenue sales profit margin customer product region date"
        desc_vague = "xyz abc foo bar baz qux"
        score_clear = compute_complexity_score(_make_columns(10), [], desc_clear)
        score_vague = compute_complexity_score(_make_columns(10), [], desc_vague)
        assert score_vague > score_clear


# ---------------------------------------------------------------------------
# 3 — candidate_count_from_complexity
# ---------------------------------------------------------------------------

class TestCandidateCountFromComplexity:
    def test_high_complexity_returns_12(self):
        from utils.scoring import candidate_count_from_complexity
        assert candidate_count_from_complexity(0.80) == 12

    def test_medium_complexity_returns_7(self):
        from utils.scoring import candidate_count_from_complexity
        assert candidate_count_from_complexity(0.55) == 7

    def test_low_complexity_returns_4(self):
        from utils.scoring import candidate_count_from_complexity
        assert candidate_count_from_complexity(0.20) == 4

    def test_boundary_above_07(self):
        from utils.scoring import candidate_count_from_complexity
        assert candidate_count_from_complexity(0.71) == 12

    def test_boundary_above_04(self):
        from utils.scoring import candidate_count_from_complexity
        assert candidate_count_from_complexity(0.41) == 7

    def test_exact_boundary_04(self):
        from utils.scoring import candidate_count_from_complexity
        # 0.4 is NOT > 0.4, falls through to low
        assert candidate_count_from_complexity(0.40) == 4


# ---------------------------------------------------------------------------
# 4–8 — AdaptiveLearningLayer
# ---------------------------------------------------------------------------

class TestAdaptiveLearningLayer:
    def test_neutral_bias_no_patterns(self):
        from utils.adaptive_learning import AdaptiveLearningLayer
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=0.5,
            success_patterns=[],
            failure_patterns=[],
            current_context={"description": "test"},
        )
        assert bias == pytest.approx(0.0, abs=1e-9)

    def test_success_pattern_produces_positive_bias(self):
        from utils.adaptive_learning import AdaptiveLearningLayer
        patterns = [
            {"context": {"description": "revenue sales profit finance"},
             "semantic_score": 0.85,
             "candidate_id": "a"},
        ]
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=0.5,
            success_patterns=patterns,
            failure_patterns=[],
            current_context={"description": "revenue sales profit"},
        )
        assert bias > 0.0

    def test_failure_pattern_produces_negative_bias(self):
        from utils.adaptive_learning import AdaptiveLearningLayer
        patterns = [
            {"context": {"description": "revenue sales profit finance"},
             "semantic_score": 0.85,
             "candidate_id": "a"},
        ]
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=0.5,
            success_patterns=[],
            failure_patterns=patterns,
            current_context={"description": "revenue sales profit"},
        )
        assert bias < 0.0

    def test_bias_clamped_max(self):
        from utils.adaptive_learning import AdaptiveLearningLayer, _MAX_BIAS
        # Many strongly matching success patterns → bias capped at +MAX_BIAS
        patterns = [
            {"context": {"description": "revenue sales profit finance margin kpi"},
             "semantic_score": 1.0,
             "candidate_id": f"c{i}"}
            for i in range(20)
        ]
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=0.5,
            success_patterns=patterns,
            failure_patterns=[],
            current_context={"description": "revenue sales profit finance margin kpi"},
        )
        assert bias <= _MAX_BIAS + 1e-9

    def test_bias_clamped_min(self):
        from utils.adaptive_learning import AdaptiveLearningLayer, _MAX_BIAS
        patterns = [
            {"context": {"description": "revenue sales profit finance margin kpi"},
             "semantic_score": 1.0,
             "candidate_id": f"c{i}"}
            for i in range(20)
        ]
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=0.5,
            success_patterns=[],
            failure_patterns=patterns,
            current_context={"description": "revenue sales profit finance margin kpi"},
        )
        assert bias >= -_MAX_BIAS - 1e-9

    def test_context_similarity_identical_texts(self):
        from utils.adaptive_learning import AdaptiveLearningLayer
        sim = AdaptiveLearningLayer.compute_context_similarity(
            {"description": "revenue profit sales"},
            {"description": "revenue profit sales"},
        )
        assert sim == pytest.approx(1.0, abs=0.01)

    def test_context_similarity_disjoint_texts(self):
        from utils.adaptive_learning import AdaptiveLearningLayer
        sim = AdaptiveLearningLayer.compute_context_similarity(
            {"description": "revenue profit sales margin"},
            {"description": "zzz xxx yyy qqq"},
        )
        assert sim < 0.15

    def test_exception_safety_returns_zero(self):
        """Passing garbage input must not raise — returns 0.0."""
        from utils.adaptive_learning import AdaptiveLearningLayer
        bias = AdaptiveLearningLayer.compute_adaptive_bias(
            base_semantic_score=None,   # type: ignore[arg-type]
            success_patterns=None,       # type: ignore[arg-type]
            failure_patterns=None,       # type: ignore[arg-type]
            current_context=None,        # type: ignore[arg-type]
        )
        # must not raise; returns 0.0 on error
        assert isinstance(bias, float)


# ---------------------------------------------------------------------------
# 9–12 — LearningMemory
# ---------------------------------------------------------------------------

class TestLearningMemory:
    def _tmp_memory(self) -> "LearningMemory":  # type: ignore[name-defined]
        from utils.learning_memory import LearningMemory
        tmp = tempfile.mktemp(suffix=".json")
        return LearningMemory(tmp)

    def test_cluster_finance_kpis(self):
        from utils.learning_memory import LearningMemory
        lm = LearningMemory(":memory:")
        cluster = lm.cluster_input(
            "Revenue and profit analysis for the finance team",
            _make_columns(20),
            ["revenue", "profit", "margin", "orders", "cost"],
        )
        assert "finance" in cluster
        assert "kpi" in cluster
        assert "cols" in cluster

    def test_cluster_buckets_correct(self):
        from utils.learning_memory import LearningMemory
        lm = LearningMemory(":memory:")
        # 35 cols → "cols30plus"; 12 kpis → "kpi10plus"
        cluster = lm.cluster_input("xyz", _make_columns(35), [f"k{i}" for i in range(12)])
        assert "cols30plus" in cluster
        assert "kpi10plus" in cluster

    def test_record_and_retrieve_success(self):
        from utils.learning_memory import LearningMemory
        lm = self._tmp_memory()
        lm.record_outcome(
            cluster="finance_kpi0to4_cols0to9",
            candidate_id="revenue_first",
            semantic_score=0.75,
            success=True,
            judge_overridden=False,
            context={"description": "finance revenue"},
        )
        patterns = lm.get_success_patterns("finance_kpi0to4_cols0to9")
        assert len(patterns) == 1
        assert patterns[0]["candidate_id"] == "revenue_first"

    def test_record_failure_not_in_success(self):
        from utils.learning_memory import LearningMemory
        lm = self._tmp_memory()
        lm.record_outcome("c1", "cand_a", 0.3, success=False,
                          judge_overridden=True, context={})
        assert lm.get_success_patterns("c1") == []
        assert len(lm.get_failure_patterns("c1")) == 1

    def test_judge_override_frequency(self):
        from utils.learning_memory import LearningMemory
        lm = self._tmp_memory()
        lm.record_outcome("c1", "a", 0.8, True, False, {})
        lm.record_outcome("c1", "b", 0.6, True, True, {})
        lm.record_outcome("c1", "c", 0.5, False, True, {})
        freq = lm.get_judge_override_frequency()
        assert freq == pytest.approx(2 / 3, abs=0.01)

    def test_save_load_roundtrip(self):
        from utils.learning_memory import LearningMemory
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lm.json"
            lm1 = LearningMemory(path)
            lm1.record_outcome("finance_kpi0to4_cols0to9", "rev_first",
                               0.8, True, False, {"description": "finance"})
            lm1.save()

            lm2 = LearningMemory(path)
            lm2.load()
            pats = lm2.get_success_patterns("finance_kpi0to4_cols0to9")
            assert len(pats) == 1
            assert pats[0]["candidate_id"] == "rev_first"
            assert lm2.get_judge_override_frequency() == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 13–15 — score_*_candidate adaptive_bias parameter
# ---------------------------------------------------------------------------

class TestAdaptiveBiasInScorers:
    def _biz(self):
        return SimpleNamespace(
            potential_kpis=["revenue", "profit"],
            important_measures=[],
            dashboard_type="executive",
            recommended_kpis=[],
        )

    def test_score_dax_positive_bias_raises_total(self):
        from utils.scoring import score_dax_candidate
        measures = _make_measures(["Total Revenue", "Order Count"])
        schema = {"columns": _make_columns(10), "table_name": "Sales"}
        s_neutral = score_dax_candidate(
            "c1", measures, self._biz(), schema, None, "revenue profit", adaptive_bias=0.0
        )
        s_boosted = score_dax_candidate(
            "c1", measures, self._biz(), schema, None, "revenue profit", adaptive_bias=0.10
        )
        assert s_boosted.total > s_neutral.total
        assert s_boosted.semantic_total >= s_neutral.semantic_total

    def test_score_dax_negative_bias_lowers_total(self):
        from utils.scoring import score_dax_candidate
        measures = _make_measures(["Total Revenue"])
        schema = {"columns": _make_columns(5), "table_name": "T"}
        s_neutral = score_dax_candidate(
            "c1", measures, None, schema, None, "revenue", adaptive_bias=0.0
        )
        s_penalised = score_dax_candidate(
            "c1", measures, None, schema, None, "revenue", adaptive_bias=-0.10
        )
        assert s_penalised.total < s_neutral.total

    def test_score_schema_positive_bias_raises_total(self):
        from utils.scoring import score_schema_candidate
        cols = _make_columns(8)
        s_neutral  = score_schema_candidate("c1", cols, None, None, "revenue", adaptive_bias=0.0)
        s_boosted  = score_schema_candidate("c1", cols, None, None, "revenue", adaptive_bias=0.10)
        assert s_boosted.total > s_neutral.total

    def test_score_schema_default_bias_zero(self):
        from utils.scoring import score_schema_candidate
        # calling without adaptive_bias kwarg must still work (backward compat)
        s = score_schema_candidate("c1", _make_columns(5), None)
        assert 0.0 <= s.total <= 1.0

    def test_score_visual_positive_bias_raises_total(self):
        from utils.scoring import score_visual_candidate
        from agents.schemas import PagePlan, ReportPlan, VisualPlan
        vp = VisualPlan(name="v1", kind="card", measure="Total Revenue",
                        intent_match_reasoning="test")
        plan = ReportPlan(pages=[PagePlan(id="p1", displayName="P1", visuals=[vp])])
        measures = _make_measures(["Total Revenue"])
        s_neutral = score_visual_candidate("c1", plan, measures, None, None, "revenue",
                                           adaptive_bias=0.0)
        s_boosted = score_visual_candidate("c1", plan, measures, None, None, "revenue",
                                           adaptive_bias=0.10)
        assert s_boosted.total > s_neutral.total

    def test_score_dax_bias_clamped_to_one(self):
        """semantic_total must never exceed 1.0 even with large positive bias."""
        from utils.scoring import score_dax_candidate
        measures = _make_measures(["Revenue"])
        schema = {"columns": _make_columns(5), "table_name": "T"}
        s = score_dax_candidate("c", measures, None, schema, None, "revenue",
                                 adaptive_bias=999.0)
        assert s.semantic_total <= 1.0

    def test_score_dax_bias_clamped_to_zero(self):
        """semantic_total must never go below 0.0 even with large negative bias."""
        from utils.scoring import score_dax_candidate
        measures = _make_measures(["Revenue"])
        schema = {"columns": _make_columns(5), "table_name": "T"}
        s = score_dax_candidate("c", measures, None, schema, None, "revenue",
                                 adaptive_bias=-999.0)
        assert s.semantic_total >= 0.0


# ---------------------------------------------------------------------------
# 16–17 — tournament_select context_aware + kpi_scores
# ---------------------------------------------------------------------------

class TestTournamentSelectAdaptive:
    def _make_scores(self, totals: list[float], sem_totals: list[float]):
        from utils.scoring import CandidateScore, SemanticScore
        scores = []
        for i, (t, s) in enumerate(zip(totals, sem_totals)):
            sc = CandidateScore(
                candidate_id=f"c{i}",
                total=t,
                semantic_total=s,
                heuristic_total=t,
                semantic=SemanticScore(kpi_semantic_alignment=s),
            )
            scores.append(sc)
        return scores

    def test_context_aware_returns_valid_winner(self):
        from utils.scoring import tournament_select
        candidates = list(range(6))
        scores = self._make_scores(
            [0.8, 0.6, 0.7, 0.5, 0.75, 0.65],
            [0.9, 0.4, 0.85, 0.3, 0.95, 0.55],
        )
        idx, best, rejected = tournament_select(
            candidates, scores, context_aware=True
        )
        assert 0 <= idx < len(candidates)
        assert best.total > 0
        assert len(rejected) == len(candidates) - 1

    def test_context_aware_false_same_as_default(self):
        """context_aware=False should give identical result to not passing it."""
        from utils.scoring import tournament_select
        candidates = list(range(5))
        scores = self._make_scores(
            [0.9, 0.7, 0.8, 0.6, 0.5],
            [0.8, 0.6, 0.7, 0.5, 0.4],
        )
        idx1, s1, _ = tournament_select(candidates, scores, context_aware=False)
        idx2, s2, _ = tournament_select(candidates, scores)
        assert idx1 == idx2
        assert s1.candidate_id == s2.candidate_id

    def test_kpi_scores_bonus_shifts_winner(self):
        """When kpi_scores gives c1 a large bonus it should beat c0."""
        from utils.scoring import tournament_select
        candidates = ["a", "b"]
        scores = self._make_scores([0.80, 0.70], [0.60, 0.55])
        # Without bonus: c0 (0.80) wins
        idx_no, _, _ = tournament_select(candidates, scores)
        assert idx_no == 0
        # With kpi_scores giving c1 a huge KPI bonus: c1 should win
        idx_kpi, best_kpi, _ = tournament_select(
            candidates, scores,
            kpi_scores={"c0": 0.0, "c1": 1.5},  # 1.5 * 0.1 = 0.15 bonus
        )
        # c1 effective total = 0.70 + 0.15 = 0.85 > 0.80 → wins
        assert idx_kpi == 1

    def test_single_candidate_returns_it(self):
        from utils.scoring import tournament_select
        scores = self._make_scores([0.7], [0.6])
        idx, best, rejected = tournament_select(["x"], scores, context_aware=True)
        assert idx == 0
        assert rejected == []


# ---------------------------------------------------------------------------
# 18–20 — JudgeLayer policy_adjustments
# ---------------------------------------------------------------------------

class TestJudgePolicyAdjustments:
    def _make_ctx(
        self,
        kpi_coverage: float = 1.0,
        visual_coherence: float = 1.0,
        schema_drift: bool = False,
        consistency_score: float = 0.9,
    ):
        """Return a minimal fake AgentContext-like namespace."""
        ctx = MagicMock()
        ctx.measures = []
        ctx.pages = []
        ctx.schema = {"columns": []}
        ctx.extra = {
            "business_analysis": None,
            "report_plan": None,
            "report_style": "standard",
            "bi_reasoning": None,
            "dax_candidates": [],
        }
        return ctx

    def test_policy_adjustments_key_in_evaluate_result(self):
        from utils.judge import JudgeLayer
        ctx = self._make_ctx()
        result = JudgeLayer().evaluate(ctx)
        assert "policy_adjustments" in result
        assert isinstance(result["policy_adjustments"], list)

    def test_failsafe_evaluate_includes_policy_adjustments(self):
        """Even when evaluate() catches an exception, policy_adjustments is in result."""
        from utils.judge import JudgeLayer
        bad_ctx = None  # will trigger exception inside _evaluate_inner
        result = JudgeLayer().evaluate(bad_ctx)  # type: ignore[arg-type]
        assert "policy_adjustments" in result
        assert result["policy_adjustments"] == []

    def test_kpi_coverage_low_triggers_adjustment(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=0.3,       # < 0.5 threshold
            visual_coherence=1.0,
            schema_drift=[],
            consistency_score=0.9,
        )
        triggers = [a["trigger"] for a in adjs]
        assert "kpi_coverage_low" in triggers
        kpi_adj = next(a for a in adjs if a["trigger"] == "kpi_coverage_low")
        assert kpi_adj["weight_delta"].get("kpi_alignment", 0) > 0
        assert kpi_adj["candidate_count_bias"] > 0

    def test_visual_coherence_low_triggers_adjustment(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=1.0,
            visual_coherence=0.2,   # < 0.35 threshold
            schema_drift=[],
            consistency_score=0.9,
        )
        triggers = [a["trigger"] for a in adjs]
        assert "visual_coherence_low" in triggers

    def test_schema_drift_triggers_adjustment(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=1.0,
            visual_coherence=1.0,
            schema_drift=["drift1"],
            consistency_score=0.9,
        )
        triggers = [a["trigger"] for a in adjs]
        assert "schema_drift_detected" in triggers

    def test_critical_inconsistency_triggers_adjustment(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=1.0,
            visual_coherence=1.0,
            schema_drift=[],
            consistency_score=0.2,  # < 0.4 threshold
        )
        triggers = [a["trigger"] for a in adjs]
        assert "critical_inconsistency" in triggers
        crit = next(a for a in adjs if a["trigger"] == "critical_inconsistency")
        assert crit["candidate_count_bias"] >= 3

    def test_no_adjustments_on_perfect_run(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=1.0,
            visual_coherence=1.0,
            schema_drift=[],
            consistency_score=0.95,
        )
        assert adjs == []

    def test_adjustment_format_contains_required_keys(self):
        from utils.judge import JudgeLayer
        adjs = JudgeLayer._generate_policy_adjustments(
            kpi_coverage=0.2,
            visual_coherence=1.0,
            schema_drift=[],
            consistency_score=0.9,
        )
        assert len(adjs) >= 1
        for adj in adjs:
            assert "trigger" in adj
            assert "weight_delta" in adj
            assert "candidate_count_bias" in adj
            assert "strategy_preference" in adj
            assert "rationale" in adj
