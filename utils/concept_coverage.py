"""utils/concept_coverage.py — Concept Coverage Enforcement.

Extracts the business concepts a user explicitly names in their request
(revenue, profit, margin, discount, growth, cost) and checks whether each
one actually got a measure (and, transitively, a visual — ``ReportAgent``
already generates a card for every measure — and an insight) in the
delivered build.

This module only *detects* coverage; it does not generate anything. The
"system MUST generate missing KPIs automatically" requirement is satisfied
by feeding the gaps this module finds into the guaranteed-measure mechanism
already built in ``agents/dax_agent.py`` (``_ensure_guaranteed_insight_measures``),
and residual gaps (concepts whose underlying columns don't exist at all, so
nothing could be generated) are surfaced through the existing
``JudgeLayer``/``strategy_gaps``/``suggest_missing_kpis`` channels — never a
new UI surface.

Fail-safe contract: every public function is exception-safe and returns a
neutral empty result on any internal error.
"""
from __future__ import annotations

from typing import Any

# Canonical concept keys -> phrases that count as the user "naming" them.
_CONCEPT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "revenue":  ("revenue", "sales", "turnover", "top line", "top-line"),
    "profit":   ("profit", "net income", "bottom line", "bottom-line"),
    "margin":   ("margin",),
    "discount": ("discount", "rebate"),
    "growth":   ("growth", "yoy", "y/y", "mom", "m/m", "trend"),
    "cost":     ("cost", "cogs", "expense", "expenditure"),
    # Binary-Outcome KPI Synthesis: the non-financial-domain counterpart to
    # the concepts above — datasets with no monetary "amount" column at all
    # (marketing response, churn, fraud) still get a guaranteed rate measure
    # via utils.kpi_prioritizer.detect_outcome_column, independent of this
    # concept dict. This entry exists so Judge's concept_coverage_score
    # correctly reflects that request, not to gate the measure's creation.
    "conversion": ("conversion", "convert", "subscription", "response rate",
                   "success rate", "churn", "retention"),
}

# Concept -> measure-name substrings that would satisfy it. Deliberately
# distinct from _CONCEPT_KEYWORDS: a *description* naming "sales" is looking
# for the word "sales"/"revenue" in the ask, but a *measure* satisfying
# "growth" looks like "... YoY %" / "... MoM %", not the literal word
# "growth" (DAXAgent never names a measure "Growth").
#
# "discount"/"cost" deliberately require the RATIO-specific phrase, not just
# any measure containing the word: a plain "Total Discounts" dollar figure
# does not answer "what is our discount *impact*" (a relative question), so
# it must not be allowed to silently satisfy this concept and suppress the
# guaranteed "Discount Rate %" measure that actually answers it.
_CONCEPT_MEASURE_MARKERS: dict[str, tuple[str, ...]] = {
    "revenue":  ("sales", "revenue"),
    "profit":   ("profit",),
    "margin":   ("margin",),
    "discount": ("discount rate", "discount %"),
    "growth":   ("yoy", "mom", "qtd", "ytd", "growth"),
    "cost":     ("cost ratio", "cost %", "cogs ratio"),
    # Matches the GENERIC outcome-rate measure name ("Conversion Rate %") --
    # the common case for placeholder-named target columns (y/target/label).
    # A column-specific name (e.g. "Subscribed Rate %") won't match this
    # marker; disclosed limitation, not a functional gap — the measure is
    # still generated unconditionally by
    # DAXAgent._ensure_outcome_rate_measure whenever an outcome column is
    # detected, independent of this concept-coverage check.
    "conversion": ("conversion rate", "conversion %", "outcome rate"),
}


def extract_concepts(business_description: str) -> list[str]:
    """Return the canonical concept keys explicitly named in *business_description*."""
    try:
        blob = (business_description or "").lower()
        if not blob:
            return []
        return [
            concept for concept, keywords in _CONCEPT_KEYWORDS.items()
            if any(kw in blob for kw in keywords)
        ]
    except Exception:  # noqa: BLE001
        return []


def _insight_text(insights: Any | None) -> str:
    if insights is None:
        return ""
    parts: list[str] = []
    try:
        for t in getattr(insights, "trends", None) or []:
            parts.append(str(getattr(t, "narrative", "") or getattr(t, "metric", "")))
        for s in getattr(insights, "segments", None) or []:
            parts.append(str(getattr(s, "primary_metric", "")))
        for u in getattr(insights, "underperformers", None) or []:
            parts.append(str(getattr(u, "recommended_action", "")))
        for k in getattr(insights, "kpi_gap_suggestions", None) or []:
            parts.append(str(getattr(k, "suggestion", "")))
    except Exception:  # noqa: BLE001
        pass
    return " ".join(parts).lower()


def check_concept_coverage(
    concepts: list[str],
    measures: list[dict[str, Any]] | None,
    insights: Any | None = None,
) -> dict[str, dict[str, bool]]:
    """Per named concept: does at least one measure (→ visual) / insight cover it?

    A measure is the hard requirement — ``ReportAgent`` already generates a
    card visual for every measure automatically, so ``has_measure`` implies
    ``has_visual`` under current, unchanged visual-generation behavior (no
    new visual logic is introduced here).
    """
    try:
        if not concepts:
            return {}
        measure_names_lower = [
            str(m.get("name", "")).lower() for m in (measures or []) if isinstance(m, dict)
        ]
        insight_blob = _insight_text(insights)

        coverage: dict[str, dict[str, bool]] = {}
        for concept in concepts:
            markers = _CONCEPT_MEASURE_MARKERS.get(concept, (concept,))
            has_measure = any(
                any(mk in mn for mk in markers) for mn in measure_names_lower
            )
            has_insight = bool(insight_blob) and any(mk in insight_blob for mk in markers)
            coverage[concept] = {
                "has_measure": has_measure,
                "has_visual": has_measure,
                "has_insight": has_insight,
                "covered": has_measure,
            }
        return coverage
    except Exception:  # noqa: BLE001
        return {}


def missing_concepts(coverage: dict[str, dict[str, bool]] | None) -> list[str]:
    """Return the concept keys not covered, in a stable, deterministic order."""
    try:
        return sorted(
            c for c, info in (coverage or {}).items() if not info.get("covered", False)
        )
    except Exception:  # noqa: BLE001
        return []


def concept_coverage_score(coverage: dict[str, dict[str, bool]] | None) -> float:
    """Fraction of concepts covered, in ``[0, 1]``. Neutral ``1.0`` when no
    concepts were named at all — a description without explicit business
    terms should not be penalised for "missing" concepts it never asked for."""
    try:
        if not coverage:
            return 1.0
        covered = sum(1 for info in coverage.values() if info.get("covered"))
        return covered / len(coverage)
    except Exception:  # noqa: BLE001
        return 1.0


__all__ = [
    "extract_concepts",
    "check_concept_coverage",
    "missing_concepts",
    "concept_coverage_score",
]
