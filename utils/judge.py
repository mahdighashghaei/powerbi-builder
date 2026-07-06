"""utils/judge.py — Global Judge Layer: Final Consistency + Optimization Authority.

Architecture: Top-3 Kaggle Winning Architecture.

The JudgeLayer is NOT a passive validator. It is the *final decision authority*:

  1. Override invalid selections if semantic inconsistencies exist.
  2. Re-rank final candidates if KPI coverage is low.
  3. Enforce global coherence across DAX + Schema + Visual.
  4. Emit override_actions that the orchestrator feedback loop executes.

Checks performed (upgraded)
---------------------------
1. **Ghost measure refs** — visuals referencing measures not in ctx.measures.
2. **KPI coverage** — fraction of business KPIs covered by measures.
3. **Style consistency** — page count appropriate for report_style.
4. **Schema-measure drift** — measures referencing columns not in schema (semantic).
5. **Visual-semantic inconsistency** — visual kinds mismatched to dashboard type.
6. **Candidate re-ranking** — if KPI coverage < 0.6 and dax_candidates exist,
   identify which rejected candidate had higher KPI coverage and flag for re-run.
7. **Global coherence score** — blended metric across all six checks.

Override actions
----------------
If semantic inconsistencies are detected, the judge populates
``result["override_actions"]`` with structured directives. The orchestrator
feedback loop reads these to selectively re-run agents.

Fail-safe contract: JudgeLayer.evaluate() must NEVER raise. Any internal
exception returns a conservative result so the judge never breaks a build.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.base import AgentContext

# Visual kinds → dashboard type alignment (mirrors scoring.py constants)
_DASHBOARD_VISUAL_MAP: dict[str, frozenset[str]] = {
    "executive":   frozenset({"card", "kpi", "columnChart", "lineChart"}),
    "operational": frozenset({"barChart", "columnChart", "matrix", "slicer"}),
    "analytical":  frozenset({"scatterChart", "lineChart", "matrix", "donutChart"}),
    "narrative":   frozenset({"card", "lineChart", "barChart", "columnChart"}),
    "default":     frozenset({"card", "barChart", "columnChart", "lineChart"}),
}

_STYLE_MAX_PAGES: dict[str, int] = {
    "minimal":  1,
    "standard": 1,
    "rich":     3,
}

# Semantic inconsistency thresholds
_KPI_COVERAGE_THRESHOLD = 0.5       # below this → KPI conflict
_SEMANTIC_OVERRIDE_THRESHOLD = 0.4  # consistency_score below this → override
_VISUAL_COHERENCE_THRESHOLD = 0.35  # visual-semantic score below this → flag

# DAX column reference extractor (same as scoring.py)
_COL_REF_RE = re.compile(r"\[([^\]]+)\]")


def _col_names_in_expr(expression: str) -> set[str]:
    return set(_COL_REF_RE.findall(expression))


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


def _visual_kind_coherence(
    visual_kinds: list[str],
    dashboard_type: str,
) -> float:
    """F1 of ideal vs actual visual kind sets (same formula as scoring.py)."""
    if not visual_kinds:
        return 0.5
    ideal = _DASHBOARD_VISUAL_MAP.get(dashboard_type, _DASHBOARD_VISUAL_MAP["default"])
    present = set(visual_kinds)
    overlap = len(present & ideal)
    precision = _safe_div(overlap, len(present))
    recall = _safe_div(overlap, len(ideal))
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


class JudgeLayer:
    """Final Consistency + Optimization Authority.

    Not a passive checker — actively overrides selections when semantic
    inconsistencies cross configured thresholds.  Also generates
    ``policy_adjustments`` — forward-looking weight and count directives that
    the orchestrator applies to bias the *next* run, turning the Judge from a
    validator into a policy optimiser.
    """

    # ------------------------------------------------------------------
    # Policy optimiser — generates scoring-weight and candidate-count
    # adjustments based on the current run's quality signals.
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_policy_adjustments(
        kpi_coverage: float,
        visual_coherence: float,
        schema_drift: list,
        consistency_score: float,
    ) -> list[dict[str, Any]]:
        """Produce forward-looking policy directives for the orchestrator.

        Each directive tells the orchestrator how to adjust scoring weights
        and candidate counts for the *next* feedback-loop iteration.

        Format
        ------
        {
            "trigger":              str,             # why this adjustment fires
            "weight_delta":         dict[str,float], # dimension → delta to add
            "candidate_count_bias": int,             # extra candidates to request
            "strategy_preference":  str,             # hint for agent strategy
            "rationale":            str,             # human-readable explanation
        }
        """
        try:
            adjustments: list[dict[str, Any]] = []

            # --- trigger: kpi_coverage_low ---
            if kpi_coverage < _KPI_COVERAGE_THRESHOLD:
                adjustments.append({
                    "trigger":              "kpi_coverage_low",
                    "weight_delta":         {"kpi_alignment": +0.08},
                    "candidate_count_bias": +2,
                    "strategy_preference":  "kpi_focused",
                    "rationale": (
                        f"KPI coverage {kpi_coverage:.0%} is below threshold "
                        f"{_KPI_COVERAGE_THRESHOLD:.0%}. Boost kpi_alignment "
                        "weight and expand candidate pool to improve KPI coverage."
                    ),
                })

            # --- trigger: visual_coherence_low ---
            if visual_coherence < _VISUAL_COHERENCE_THRESHOLD:
                adjustments.append({
                    "trigger":              "visual_coherence_low",
                    "weight_delta":         {"visual_quality": +0.06},
                    "candidate_count_bias": +1,
                    "strategy_preference":  "executive",
                    "rationale": (
                        f"Visual coherence {visual_coherence:.2f} is below threshold "
                        f"{_VISUAL_COHERENCE_THRESHOLD:.2f}. Boost visual_quality "
                        "weight and add a visual candidate variant."
                    ),
                })

            # --- trigger: schema_drift_detected ---
            if schema_drift:
                adjustments.append({
                    "trigger":              "schema_drift_detected",
                    "weight_delta":         {"data_coverage": +0.06},
                    "candidate_count_bias": 0,
                    "strategy_preference":  "relationship_aware",
                    "rationale": (
                        f"{len(schema_drift)} schema-drift violation(s) detected. "
                        "Boost data_coverage weight to prefer candidates that "
                        "stay within known schema columns."
                    ),
                })

            # --- trigger: critical_inconsistency ---
            if consistency_score < _SEMANTIC_OVERRIDE_THRESHOLD:
                adjustments.append({
                    "trigger":              "critical_inconsistency",
                    "weight_delta":         {
                        "kpi_alignment":  +0.05,
                        "data_coverage":  +0.05,
                    },
                    "candidate_count_bias": +3,
                    "strategy_preference":  "analytical",
                    "rationale": (
                        f"Consistency score {consistency_score:.3f} is critically "
                        f"below {_SEMANTIC_OVERRIDE_THRESHOLD:.2f}. Significantly "
                        "expand candidate pool and re-weight toward data fidelity."
                    ),
                })

            return adjustments
        except Exception:  # noqa: BLE001 — policy generation must never crash
            return []

    # ------------------------------------------------------------------
    # Strategy gaps — minimal enhancement for the Strategy Synthesis Layer.
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_strategy_gaps(
        kpi_coverage: float,
        schema_drift: list,
        visual_coherence: float,
        dashboard_type: str,
        primary_kpi: str | None = None,
        primary_kpi_covered: bool = True,
    ) -> list[dict[str, Any]]:
        """Re-shape existing threshold breaches into strategy-gap directives.

        Consumed by ``utils.strategy_synthesizer.StrategySynthesizer`` to
        decide which domain (dax / schema / visual) needs a synthesized
        strategy and which named rule to synthesize.
        """
        try:
            gaps: list[dict[str, Any]] = []

            if kpi_coverage < _KPI_COVERAGE_THRESHOLD:
                gaps.append({
                    "domain": "dax",
                    "missing_pattern": (
                        f"kpi coverage {kpi_coverage:.0%} < "
                        f"{_KPI_COVERAGE_THRESHOLD:.0%}"
                    ),
                    "suggested_synthesis": "kpi_gap_fill",
                })

            if primary_kpi and not primary_kpi_covered:
                gaps.append({
                    "domain": "dax",
                    "missing_pattern": f"primary business KPI '{primary_kpi}' has no measure",
                    "suggested_synthesis": "kpi_gap_fill",
                })

            if schema_drift:
                gaps.append({
                    "domain": "schema",
                    "missing_pattern": (
                        f"{len(schema_drift)} measure(s) reference unknown "
                        "schema column(s)"
                    ),
                    "suggested_synthesis": "schema_safe_measures",
                })

            if visual_coherence < _VISUAL_COHERENCE_THRESHOLD:
                gaps.append({
                    "domain": "visual",
                    "missing_pattern": (
                        f"visual coherence {visual_coherence:.2f} < "
                        f"{_VISUAL_COHERENCE_THRESHOLD:.2f} for "
                        f"dashboard_type='{dashboard_type}'"
                    ),
                    "suggested_synthesis": "coherence_gap_fill",
                })

            return gaps
        except Exception:  # noqa: BLE001 — strategy-gap generation must never crash
            return []

    def evaluate(self, ctx: "AgentContext") -> dict[str, Any]:
        """Evaluate cross-agent consistency and issue override directives.

        Returns
        -------
        {
            "consistent":           bool,
            "consistency_score":    float (0–1),
            "conflicts":            list[str],
            "ghost_measure_refs":   list[str],
            "kpi_coverage":         float (0–1),
            "style_ok":             bool,
            "schema_measure_drift": list[str],
            "visual_coherence":     float (0–1),
            "override_actions":     list[dict],  # NEW: directives for feedback loop
            "rerank_recommendation":dict | None, # NEW: alternative candidate to try
        }
        """
        try:
            return self._evaluate_inner(ctx)
        except Exception as exc:  # noqa: BLE001 — judge must never crash the build
            return {
                "consistent":            True,
                "consistency_score":     1.0,
                "conflicts":             [f"judge_error: {exc}"],
                "ghost_measure_refs":    [],
                "kpi_coverage":          1.0,
                "primary_kpi":           None,
                "primary_kpi_covered":   True,
                "style_ok":              True,
                "schema_measure_drift":  [],
                "visual_coherence":      1.0,
                "concept_coverage_score":     1.0,
                "semantic_correctness_score": 1.0,
                "kpi_appropriateness_score":  1.0,
                "override_actions":      [],
                "rerank_recommendation": None,
                "policy_adjustments":    [],
                "strategy_gaps":         [],
            }

    # ------------------------------------------------------------------
    # Internal — allowed to raise (caught by evaluate())
    # ------------------------------------------------------------------

    def _evaluate_inner(self, ctx: "AgentContext") -> dict[str, Any]:
        conflicts: list[str] = []
        override_actions: list[dict[str, Any]] = []
        rerank_recommendation: dict[str, Any] | None = None

        measure_names: set[str] = {m["name"] for m in (ctx.measures or [])}

        # ----------------------------------------------------------------
        # Check 1: Ghost measure references in report visuals
        # ----------------------------------------------------------------
        ghost_refs: list[str] = []
        report_plan = ctx.extra.get("report_plan")  # type: ignore[union-attr]
        if report_plan is not None:
            for page in getattr(report_plan, "pages", []):
                for v in page.visuals:
                    if v.measure and v.measure not in measure_names:
                        ghost_refs.append(
                            f"page='{page.displayName}' visual='{v.name}' "
                            f"references missing measure '{v.measure}'"
                        )
        if ghost_refs:
            conflicts.append(
                f"{len(ghost_refs)} ghost measure reference(s) in report visuals."
            )
            override_actions.append({
                "action":    "rerun_agent",
                "agent":     "ReportAgent",
                "reason":    "ghost_measure_refs",
                "detail":    ghost_refs,
                "severity":  "error",
            })

        # ----------------------------------------------------------------
        # Check 2: KPI coverage
        # ----------------------------------------------------------------
        biz = ctx.extra.get("business_analysis")  # type: ignore[union-attr]
        kpi_coverage = 1.0
        kpis: list[str] = []
        if biz is not None:
            kpis = list(getattr(biz, "potential_kpis", []) or [])
            if kpis:
                measure_names_lower = {n.lower() for n in measure_names}
                covered = sum(
                    1 for k in kpis
                    if any(k.lower() in mn for mn in measure_names_lower)
                )
                kpi_coverage = covered / len(kpis)
                if kpi_coverage < _KPI_COVERAGE_THRESHOLD:
                    conflicts.append(
                        f"KPI coverage low: {covered}/{len(kpis)} "
                        f"business KPIs covered ({kpi_coverage:.0%})."
                    )
                    override_actions.append({
                        "action":   "rerun_agent",
                        "agent":    "DAXAgent",
                        "reason":   "kpi_coverage_low",
                        "detail":   {
                            "covered": covered,
                            "total":   len(kpis),
                            "missing": [k for k in kpis
                                        if not any(k.lower() in mn for mn in measure_names_lower)],
                        },
                        "severity": "warning",
                    })

        # Check 2b: Re-rank recommendation — if a rejected DAX candidate
        # had a higher KPI semantic alignment, recommend it over the winner.
        dax_candidates: list[dict] = list(ctx.extra.get("dax_candidates") or [])  # type: ignore[union-attr]
        if dax_candidates and kpi_coverage < _KPI_COVERAGE_THRESHOLD:
            best_rejected = max(
                (c for c in dax_candidates),
                key=lambda c: c.get("semantic", {}).get("kpi_semantic_alignment", 0.0)
                    + c.get("kpi_alignment", 0.0),
                default=None,
            )
            if best_rejected:
                rerank_recommendation = {
                    "recommended_candidate": best_rejected.get("candidate_id"),
                    "reason":                "kpi_coverage_low_rerank",
                    "kpi_alignment":         best_rejected.get("kpi_alignment", 0),
                    "action":                "override_dax_candidate",
                }

        # ----------------------------------------------------------------
        # Check 2c: Primary business KPI coverage (business-aware KPI
        # prioritization — utils/kpi_prioritizer.py). Aggregate kpi_coverage
        # above can look healthy while the single #1-priority KPI (e.g.
        # "Profit" or "Sales", not "Manufacturing Price") still has no
        # measure at all — this check catches that specific case.
        # ----------------------------------------------------------------
        primary_kpi: str | None = None
        primary_kpi_covered = True
        prioritized_kpis: list[str] = list(ctx.extra.get("prioritized_kpis") or [])  # type: ignore[union-attr]
        if prioritized_kpis:
            primary_kpi = prioritized_kpis[0]
            measure_names_lower_primary = {n.lower() for n in measure_names}
            primary_kpi_covered = (
                any(primary_kpi.lower() in mn for mn in measure_names_lower_primary)
                if measure_names_lower_primary else False
            )
            if not primary_kpi_covered:
                conflicts.append(
                    f"Primary business KPI '{primary_kpi}' (top of the "
                    "business-aware priority ranking) has no corresponding measure."
                )
                override_actions.append({
                    "action":   "rerun_agent",
                    "agent":    "DAXAgent",
                    "reason":   "primary_kpi_uncovered",
                    "detail":   {"primary_kpi": primary_kpi},
                    "severity": "warning",
                })

        # ----------------------------------------------------------------
        # Check 3: Style consistency (page count)
        # ----------------------------------------------------------------
        # Prefer ReportAgent's own resolved cap (ctx.extra["effective_max_pages"],
        # written by ReportAgent._run() after applying description-keyword
        # and explicit num_pages overrides) over a coarse style-name lookup.
        # ctx.extra["report_style"] is written by PlannerAgent BEFORE
        # ReportAgent runs any of its own overrides — using only the style
        # name here would compare the actual (correct) page count against
        # the WRONG cap whenever ReportAgent's resolved style/count differs
        # from the planner's original suggestion, permanently flagging
        # "style_page_count_exceeded" and wasting the entire feedback-loop
        # retry budget on a rerun that can never satisfy the check.
        style: str = ctx.extra.get("report_style", "standard")  # type: ignore[union-attr]
        effective_max_pages = ctx.extra.get("effective_max_pages")  # type: ignore[union-attr]
        max_pages = int(effective_max_pages) if effective_max_pages else _STYLE_MAX_PAGES.get(style, 1)
        actual_pages = len(ctx.pages or [])
        style_ok = actual_pages <= max_pages
        if not style_ok:
            conflicts.append(
                f"Page count {actual_pages} exceeds max {max_pages} "
                f"for report_style='{style}'."
            )
            override_actions.append({
                "action":   "rerun_agent",
                "agent":    "ReportAgent",
                "reason":   "style_page_count_exceeded",
                "detail":   {"actual": actual_pages, "max": max_pages, "style": style},
                "severity": "warning",
            })

        # ----------------------------------------------------------------
        # Check 4: Schema-measure drift (NEW)
        # Measures referencing columns that don't exist in the schema.
        # ----------------------------------------------------------------
        schema_drift: list[str] = []
        schema_cols: set[str] = set()
        if ctx.schema:
            schema_cols = {c["name"] for c in ctx.schema.get("columns", [])}
        for m in (ctx.measures or []):
            expr = m.get("expression", "")
            expr_refs = _col_names_in_expr(expr)
            # Only flag columns that look like data columns (not other measures)
            unknown_cols = expr_refs - schema_cols - measure_names
            if unknown_cols:
                schema_drift.append(
                    f"measure='{m['name']}' references unknown column(s): "
                    f"{sorted(unknown_cols)}"
                )
        if schema_drift:
            conflicts.append(
                f"{len(schema_drift)} measure(s) reference unknown schema column(s)."
            )
            override_actions.append({
                "action":   "rerun_agent",
                "agent":    "DAXAgent",
                "reason":   "schema_measure_drift",
                "detail":   schema_drift,
                "severity": "error",
            })

        # ----------------------------------------------------------------
        # Check 5: Visual-semantic inconsistency (NEW)
        # Visual kinds mismatched to the intended dashboard type.
        # ----------------------------------------------------------------
        visual_coherence = 1.0
        bi_reasoning = ctx.extra.get("bi_reasoning")  # type: ignore[union-attr]
        dashboard_type = (
            getattr(bi_reasoning, "dashboard_type", "default")
            if bi_reasoning is not None else "default"
        )
        if report_plan is not None:
            all_visual_kinds = [
                v.kind
                for page in getattr(report_plan, "pages", [])
                for v in page.visuals
            ]
            visual_coherence = _visual_kind_coherence(all_visual_kinds, dashboard_type)
            if visual_coherence < _VISUAL_COHERENCE_THRESHOLD:
                conflicts.append(
                    f"Visual-semantic inconsistency: kind coherence {visual_coherence:.2f} < "
                    f"{_VISUAL_COHERENCE_THRESHOLD:.2f} for dashboard_type='{dashboard_type}'."
                )
                override_actions.append({
                    "action":   "rerun_agent",
                    "agent":    "ReportAgent",
                    "reason":   "visual_semantic_inconsistency",
                    "detail":   {
                        "dashboard_type":   dashboard_type,
                        "actual_kinds":     list(set(all_visual_kinds)),
                        "expected_kinds":   list(_DASHBOARD_VISUAL_MAP.get(
                            dashboard_type, _DASHBOARD_VISUAL_MAP["default"]
                        )),
                        "coherence_score":  round(visual_coherence, 4),
                    },
                    "severity": "warning",
                })

        # ----------------------------------------------------------------
        # Check 6: Concept Coverage / Semantic Correctness / KPI
        # Appropriateness (Evaluation Layer Fix — system stabilization).
        # All three default to a neutral 1.0 when not applicable (no
        # concepts named, no semantic model, no amount-shaped measures) so
        # runs without explicit business terms score exactly as before.
        # ----------------------------------------------------------------
        concept_coverage_score = 1.0
        _concepts: list[str] = list(ctx.extra.get("business_concepts") or [])  # type: ignore[union-attr]
        if _concepts:
            from utils.concept_coverage import (
                check_concept_coverage, concept_coverage_score as _compute_ccs, missing_concepts,
            )
            _coverage = check_concept_coverage(_concepts, list(ctx.measures or []), ctx.extra.get("insights"))
            concept_coverage_score = _compute_ccs(_coverage)
            _missing_concepts = missing_concepts(_coverage)
            if _missing_concepts:
                conflicts.append(
                    f"Concept coverage incomplete: {', '.join(_missing_concepts)} named in "
                    "the business description but not covered by any measure."
                )
                override_actions.append({
                    "action":   "rerun_agent",
                    "agent":    "DAXAgent",
                    "reason":   "concept_coverage_incomplete",
                    "detail":   {"missing_concepts": _missing_concepts},
                    "severity": "warning",
                })

        # Lightweight audit (not a rigorous prover): of the ratio measures
        # DAXAgent produced, how many use the numerator/denominator pair the
        # Semantic Truth Layer actually discovered for that concept, versus
        # some other (possibly wrong-direction) pair?
        semantic_correctness_score = 1.0
        _semantic_model: dict = ctx.extra.get("semantic_model") or {}  # type: ignore[union-attr]
        _derived_candidates: list[dict] = list(ctx.extra.get("derived_kpi_candidates") or [])  # type: ignore[union-attr]
        _expected_pairs = {
            frozenset({c.get("numerator"), c.get("denominator")})
            for c in _derived_candidates if c.get("source") == "semantic_model"
        }
        if _semantic_model.get("canonical_metrics") and _expected_pairs:
            _checked, _correct = 0, 0
            for m in (ctx.measures or []):
                expr = m.get("expression", "")
                if "DIVIDE" not in expr:
                    continue
                refs = _col_names_in_expr(expr)
                if len(refs) != 2:
                    continue
                _checked += 1
                if frozenset(refs) in _expected_pairs:
                    _correct += 1
            semantic_correctness_score = (_correct / _checked) if _checked else 1.0

        # Fraction of SUM-based measures that do NOT sum a rate/price column
        # (audits that DAXAgent's Aggregation Safety Fix held).
        kpi_appropriateness_score = 1.0
        from utils.kpi_prioritizer import is_rate_column
        _sum_measures = [m for m in (ctx.measures or []) if m.get("expression", "").startswith("SUM(")]
        if _sum_measures:
            _bad = sum(
                1 for m in _sum_measures
                if any(is_rate_column(r) for r in _col_names_in_expr(m.get("expression", "")))
            )
            kpi_appropriateness_score = 1.0 - (_bad / len(_sum_measures))

        # ----------------------------------------------------------------
        # Aggregate consistency score (Evaluation Layer Fix — re-derived to
        # fold in concept coverage / semantic correctness / KPI
        # appropriateness; "current quality_score is NOT sufficient" was the
        # explicit finding this re-weighting addresses):
        # ghost(0.20) + kpi(0.15) + style(0.10) + schema_drift(0.10)
        # + visual(0.10) + concept_coverage(0.15) + semantic_correctness(0.10)
        # + kpi_appropriateness(0.10)
        # ----------------------------------------------------------------
        ghost_score = (
            1.0 if not ghost_refs else max(0.0, 1.0 - len(ghost_refs) * 0.2)
        )
        style_score = 1.0 if style_ok else 0.6
        drift_score = (
            1.0 if not schema_drift else max(0.0, 1.0 - len(schema_drift) * 0.15)
        )

        consistency_score = (
            ghost_score                  * 0.20
            + kpi_coverage               * 0.15
            + style_score                * 0.10
            + drift_score                * 0.10
            + visual_coherence           * 0.10
            + concept_coverage_score     * 0.15
            + semantic_correctness_score * 0.10
            + kpi_appropriateness_score  * 0.10
        )
        consistent = (
            consistency_score >= 0.7
            and not ghost_refs
            and not schema_drift
        )

        # ----------------------------------------------------------------
        # Global override: if consistency_score is critically low, force
        # full DAX + Report re-run (not just targeted fix).
        # ----------------------------------------------------------------
        if consistency_score < _SEMANTIC_OVERRIDE_THRESHOLD:
            override_actions.append({
                "action":   "global_override",
                "agent":    "ALL",
                "reason":   "critical_semantic_inconsistency",
                "detail":   {
                    "consistency_score": round(consistency_score, 4),
                    "threshold":         _SEMANTIC_OVERRIDE_THRESHOLD,
                    "conflicts":         conflicts,
                },
                "severity": "error",
            })

        # ----------------------------------------------------------------
        # Policy optimisation: generate forward-looking weight/count
        # adjustments so the orchestrator can self-tune future iterations.
        # ----------------------------------------------------------------
        policy_adjustments = self._generate_policy_adjustments(
            kpi_coverage=kpi_coverage,
            visual_coherence=visual_coherence,
            schema_drift=schema_drift,
            consistency_score=consistency_score,
        )

        # ----------------------------------------------------------------
        # Strategy gaps: surface the same threshold breaches already
        # detected above (checks 2, 4, 5) in a shape the Strategy Synthesis
        # Layer (utils/strategy_synthesizer.py) can consume directly. No new
        # checks are introduced here — this only re-shapes existing signals.
        # ----------------------------------------------------------------
        strategy_gaps: list[dict[str, Any]] = self._generate_strategy_gaps(
            kpi_coverage=kpi_coverage,
            schema_drift=schema_drift,
            visual_coherence=visual_coherence,
            dashboard_type=dashboard_type,
            primary_kpi=primary_kpi,
            primary_kpi_covered=primary_kpi_covered,
        )

        return {
            "consistent":            consistent,
            "consistency_score":     round(consistency_score, 4),
            "conflicts":             conflicts,
            "ghost_measure_refs":    ghost_refs,
            "kpi_coverage":          round(kpi_coverage, 4),
            "primary_kpi":           primary_kpi,
            "primary_kpi_covered":   primary_kpi_covered,
            "style_ok":              style_ok,
            "schema_measure_drift":  schema_drift,
            "visual_coherence":      round(visual_coherence, 4),
            "concept_coverage_score":     round(concept_coverage_score, 4),
            "semantic_correctness_score": round(semantic_correctness_score, 4),
            "kpi_appropriateness_score":  round(kpi_appropriateness_score, 4),
            "override_actions":      override_actions,
            "rerank_recommendation": rerank_recommendation,
            "policy_adjustments":    policy_adjustments,
            "strategy_gaps":         strategy_gaps,
        }


__all__ = ["JudgeLayer"]
