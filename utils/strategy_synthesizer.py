"""utils/strategy_synthesizer.py — Strategy Synthesis Layer.

Architecture: Top-3 Kaggle Winning Architecture — Adaptive Strategy Evolution.

Closes the last gap in the multi-candidate tournament architecture: every
strategy in every candidate pool (DAX measure sets, schema summarize-by
mappings, visual arrangements) used to be a fixed, hand-written recipe. This
module lets the system *invent* a new, parameterised strategy when the
existing pool keeps failing the same way — derived from judge signals,
learning-memory failure patterns, and repeated low-performing candidate
clusters. The synthesized strategy is then injected into the normal
candidate pool and competes in the exact same scoring + tournament pipeline
as every hand-written strategy (see ``utils/scoring.py``).

Non-negotiable constraint: nothing here is a hardcoded strategy. The
``generation_rule`` names are a small, generic vocabulary (the same pattern
already used for named strategies like ``"kpi_focused"`` in
``agents/schema_agent.py``) — the *parameters* that make each rule concrete
(which keywords, which columns, which visual kinds) are always derived at
call time from failure evidence, never literal.

Fail-safe contract: every public function/method is exception-safe and
returns a neutral default (``None`` / ``[]`` / unchanged input) on any
internal error, so the synthesis layer can never block or break a build.
"""
from __future__ import annotations

import re
from typing import Any

from utils.identifiers import quote_dax_column, quote_dax_table

# ---------------------------------------------------------------------------
# Shared vocabulary (mirrors utils/scoring.py + utils/adaptive_learning.py)
# ---------------------------------------------------------------------------

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "revenue":    ["revenue", "sales", "income", "profit", "margin", "price", "amount"],
    "operations": ["count", "volume", "quantity", "orders", "units", "rate", "duration"],
    "customer":   ["customer", "client", "user", "segment", "account", "contact"],
    "time":       ["date", "year", "month", "quarter", "period", "ytd", "yoy", "mom"],
    "geography":  ["region", "country", "city", "market", "territory", "zone"],
    "product":    ["product", "category", "brand", "segment", "sku", "item", "type"],
}

# Minimum accumulated failure evidence (failure_patterns + low_performing_clusters)
# before a generic (non-judge-driven) kpi_gap_fill signal is allowed to fire.
_MIN_EVIDENCE_FOR_GENERIC_TRIGGER = 2

_COL_LIST_RE = re.compile(r"\[(.*?)\]")


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9]*", (text or "").lower()))


def _rationale_tokens_from_evidence(evidence: list[dict[str, Any]]) -> list[str]:
    """Extract domain keyword hits from failure/low-performer context descriptions."""
    blob = " ".join(
        str((e.get("context") or {}).get("description", ""))
        for e in evidence if isinstance(e, dict)
    )
    toks = _tokenize(blob)
    hits: list[str] = []
    for kws in _DOMAIN_KEYWORDS.values():
        hits.extend(k for k in kws if k in toks)
    return hits


def _dedupe_lower(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        item = str(raw).strip().lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# StrategySynthesizer
# ---------------------------------------------------------------------------


class StrategySynthesizer:
    """Derives new candidate-generation strategies from failure evidence.

    Every ``synthesize_*_strategy`` method inspects the same four evidence
    sources — failure patterns, judge signals, low-performing candidate
    clusters, and the current strategy pool — and returns either a single
    strategy spec dict or ``None`` when no actionable gap is found.
    """

    # ------------------------------------------------------------------
    # Domain-specific synthesis
    # ------------------------------------------------------------------

    def synthesize_dax_strategy(
        self,
        failure_patterns: list[dict[str, Any]] | None = None,
        judge_signals: dict[str, Any] | None = None,
        low_performing_clusters: list[dict[str, Any]] | None = None,
        current_strategy_pool: list[str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            judge_signals = judge_signals or {}
            evidence = list(failure_patterns or []) + list(low_performing_clusters or [])
            pool = current_strategy_pool or []

            # Rule: kpi_gap_fill — measures that directly target the KPI
            # keywords the judge (or historical failures) flagged as missing.
            keywords, kpi_coverage = self._kpi_gap_signal(judge_signals, evidence)
            if keywords is not None:
                target = min(1.0, (kpi_coverage if kpi_coverage is not None else 0.4) + 0.15)
                return {
                    "strategy_id": self._unique_id("dax", "kpi_gap_fill", pool),
                    "strategy_type": "dax",
                    "generation_rule": "kpi_gap_fill",
                    "parameters": {"target_keywords": keywords},
                    "expected_improvement_target": round(target, 4),
                }

            # Rule: schema_safe_measures — measures that deliberately avoid
            # the specific columns that caused schema-measure drift.
            drift_cols = self._drift_signal(judge_signals)
            if drift_cols:
                target = min(1.0, 0.55 + 0.05 * len(drift_cols))
                return {
                    "strategy_id": self._unique_id("dax", "schema_safe_measures", pool),
                    "strategy_type": "dax",
                    "generation_rule": "schema_safe_measures",
                    "parameters": {"exclude_hint_patterns": drift_cols},
                    "expected_improvement_target": round(target, 4),
                }

            return None
        except Exception:  # noqa: BLE001 — synthesis must never crash the build
            return None

    def synthesize_schema_strategy(
        self,
        failure_patterns: list[dict[str, Any]] | None = None,
        judge_signals: dict[str, Any] | None = None,
        low_performing_clusters: list[dict[str, Any]] | None = None,
        current_strategy_pool: list[str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            judge_signals = judge_signals or {}
            evidence = list(failure_patterns or []) + list(low_performing_clusters or [])
            pool = current_strategy_pool or []

            # Rule: targeted_kpi_boost — force summarizeBy=sum on exactly the
            # columns matching the KPI keywords the judge/history flagged.
            keywords, kpi_coverage = self._kpi_gap_signal(judge_signals, evidence)
            if keywords is not None:
                target = min(1.0, (kpi_coverage if kpi_coverage is not None else 0.4) + 0.15)
                return {
                    "strategy_id": self._unique_id("schema", "targeted_kpi_boost", pool),
                    "strategy_type": "schema",
                    "generation_rule": "targeted_kpi_boost",
                    "parameters": {"target_keywords": keywords},
                    "expected_improvement_target": round(target, 4),
                }

            return None
        except Exception:  # noqa: BLE001
            return None

    def synthesize_visual_strategy(
        self,
        failure_patterns: list[dict[str, Any]] | None = None,
        judge_signals: dict[str, Any] | None = None,
        low_performing_clusters: list[dict[str, Any]] | None = None,
        current_strategy_pool: list[str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            judge_signals = judge_signals or {}
            pool = current_strategy_pool or []

            missing_kinds, coherence = self._visual_gap_signal(judge_signals)
            if missing_kinds is not None:
                target = min(1.0, (coherence if coherence is not None else 0.3) + 0.15)
                return {
                    "strategy_id": self._unique_id("visual", "coherence_gap_fill", pool),
                    "strategy_type": "visual",
                    "generation_rule": "coherence_gap_fill",
                    "parameters": {"missing_kinds": missing_kinds},
                    "expected_improvement_target": round(target, 4),
                }

            return None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Orchestration entry point
    # ------------------------------------------------------------------

    def synthesize_new_strategies(
        self,
        domain_pool_ids: dict[str, list[str]] | None = None,
        judge_result: dict[str, Any] | None = None,
        failure_patterns: list[dict[str, Any]] | None = None,
        low_performing_clusters: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Run all three domain synthesizers and collect their specs.

        Returns ``{"dax": [...], "schema": [...], "visual": [...]}`` — each
        list holds zero or one spec (a domain contributes nothing when no
        actionable gap is found). Never raises.
        """
        try:
            domain_pool_ids = domain_pool_ids or {}
            result: dict[str, list[dict[str, Any]]] = {"dax": [], "schema": [], "visual": []}

            dax_spec = self.synthesize_dax_strategy(
                failure_patterns, judge_result, low_performing_clusters,
                domain_pool_ids.get("dax"),
            )
            if dax_spec:
                result["dax"].append(dax_spec)

            schema_spec = self.synthesize_schema_strategy(
                failure_patterns, judge_result, low_performing_clusters,
                domain_pool_ids.get("schema"),
            )
            if schema_spec:
                result["schema"].append(schema_spec)

            visual_spec = self.synthesize_visual_strategy(
                failure_patterns, judge_result, low_performing_clusters,
                domain_pool_ids.get("visual"),
            )
            if visual_spec:
                result["visual"].append(visual_spec)

            return result
        except Exception:  # noqa: BLE001
            return {"dax": [], "schema": [], "visual": []}

    # ------------------------------------------------------------------
    # Signal extraction helpers (internal)
    # ------------------------------------------------------------------

    @staticmethod
    def _kpi_gap_signal(
        judge_signals: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> tuple[list[str] | None, float | None]:
        """Return ``(target_keywords, kpi_coverage)`` or ``(None, None)``.

        Structured signals (judge override_actions / policy_adjustments /
        strategy_gaps) take priority; falls back to keyword-mining historical
        failure/low-performer context descriptions when no structured signal
        is present but evidence volume is high enough to act on.
        """
        kpi_coverage = judge_signals.get("kpi_coverage")
        triggered = False
        keywords: list[str] = []

        for oa in (judge_signals.get("override_actions") or []):
            if not isinstance(oa, dict):
                continue
            if oa.get("reason") == "kpi_coverage_low":
                triggered = True
                detail = oa.get("detail") or {}
                keywords.extend(str(m) for m in (detail.get("missing") or []))
            elif oa.get("reason") == "primary_kpi_uncovered":
                # Business-aware KPI Prioritization Layer (utils/kpi_prioritizer.py):
                # the Judge flagged the #1-priority KPI specifically as
                # uncovered — target it directly rather than the generic
                # aggregate-coverage gap list.
                triggered = True
                detail = oa.get("detail") or {}
                if detail.get("primary_kpi"):
                    keywords.append(str(detail["primary_kpi"]))

        for pa in (judge_signals.get("policy_adjustments") or []):
            if isinstance(pa, dict) and pa.get("trigger") == "kpi_coverage_low":
                triggered = True

        for sg in (judge_signals.get("strategy_gaps") or []):
            if isinstance(sg, dict) and sg.get("suggested_synthesis") in (
                "kpi_gap_fill", "targeted_kpi_boost",
            ):
                triggered = True

        if not triggered and kpi_coverage is not None and kpi_coverage < 0.5:
            triggered = True

        if not triggered and len(evidence) >= _MIN_EVIDENCE_FOR_GENERIC_TRIGGER:
            triggered = True
            keywords.extend(_rationale_tokens_from_evidence(evidence))

        if not triggered:
            return None, None

        return _dedupe_lower(keywords), kpi_coverage

    @staticmethod
    def _drift_signal(judge_signals: dict[str, Any]) -> list[str]:
        """Return the list of column names implicated in schema-measure drift."""
        cols: list[str] = []
        drift_msgs = list(judge_signals.get("schema_measure_drift") or [])
        has_gap_signal = any(
            isinstance(sg, dict) and sg.get("suggested_synthesis") == "schema_safe_measures"
            for sg in (judge_signals.get("strategy_gaps") or [])
        )
        if not drift_msgs and not has_gap_signal:
            return []
        for msg in drift_msgs:
            m = _COL_LIST_RE.search(str(msg))
            if m:
                cols.extend(
                    n.strip().strip("'\"") for n in m.group(1).split(",") if n.strip()
                )
        seen: set[str] = set()
        uniq: list[str] = []
        for c in cols:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    @staticmethod
    def _visual_gap_signal(
        judge_signals: dict[str, Any],
    ) -> tuple[list[str] | None, float | None]:
        """Return ``(missing_kinds, visual_coherence)`` or ``(None, None)``."""
        coherence = judge_signals.get("visual_coherence")
        triggered = False
        missing: list[str] = []

        for oa in (judge_signals.get("override_actions") or []):
            if isinstance(oa, dict) and oa.get("reason") == "visual_semantic_inconsistency":
                triggered = True
                detail = oa.get("detail") or {}
                actual = set(detail.get("actual_kinds") or [])
                expected = set(detail.get("expected_kinds") or [])
                missing.extend(sorted(expected - actual))

        for sg in (judge_signals.get("strategy_gaps") or []):
            if isinstance(sg, dict) and sg.get("domain") == "visual":
                triggered = True

        if not triggered and coherence is not None and coherence < 0.35:
            triggered = True

        if not triggered:
            return None, None

        return missing, coherence

    @staticmethod
    def _unique_id(domain: str, rule: str, pool: list[str] | None) -> str:
        base = f"synth_{domain}_{rule}"
        existing = {p for p in (pool or []) if isinstance(p, str) and p.startswith(base)}
        n = 1
        candidate = f"{base}_{n}"
        while candidate in existing:
            n += 1
            candidate = f"{base}_{n}"
        return candidate


# ---------------------------------------------------------------------------
# Spec interpreters — turn a synthesized spec into an actual candidate using
# the exact same data shapes the base strategies already use, so the result
# flows straight into the existing score_*_candidate / tournament_select
# machinery with zero changes to scoring code.
# ---------------------------------------------------------------------------


def apply_dax_strategy(
    spec: dict[str, Any],
    table: str,
    buckets: dict[str, list[dict[str, Any]]],
    biz_analysis: Any | None = None,
) -> list[dict[str, Any]]:
    """Interpret a synthesized DAX strategy spec into a measure list."""
    try:
        rule = spec.get("generation_rule")
        params = spec.get("parameters") or {}
        if rule == "kpi_gap_fill":
            return _apply_kpi_gap_fill_dax(table, buckets, params.get("target_keywords") or [])
        if rule == "schema_safe_measures":
            return _apply_schema_safe_measures_dax(
                table, buckets, params.get("exclude_hint_patterns") or [],
            )
        return []
    except Exception:  # noqa: BLE001
        return []


def _apply_kpi_gap_fill_dax(
    table: str, buckets: dict[str, list[dict[str, Any]]], target_keywords: list[str],
) -> list[dict[str, Any]]:
    qtable = quote_dax_table(table)

    def ref(col: str) -> str:
        return quote_dax_column(table, col)

    all_numeric = (
        buckets.get("amount", []) + buckets.get("qty", []) + buckets.get("other_numeric", [])
    )
    kw_lower = [k.lower() for k in target_keywords]
    matched = [
        c for c in all_numeric
        if kw_lower and any(kw in c["name"].lower() or c["name"].lower() in kw for kw in kw_lower)
    ]
    if not matched:
        matched = buckets.get("amount", [])[:5] or all_numeric[:5]

    measures: list[dict[str, Any]] = []
    for col in matched[:6]:
        cname = col["name"]
        measures.append({
            "name": f"Synth Total {cname}",
            "expression": f"SUM({ref(cname)})",
            "displayFolder": "SynthesizedKPI",
            "description": f"Synthesized KPI-gap-fill total of {cname}.",
            "formatString": "$ #,##0.00",
        })
    measures.append({
        "name": "Synth Order Count",
        "expression": f"COUNTROWS({qtable})",
        "displayFolder": "SynthesizedKPI",
        "description": "Synthesized row-count anchor for KPI-gap-fill strategy.",
        "formatString": "#,##0",
    })
    return measures


def _apply_schema_safe_measures_dax(
    table: str, buckets: dict[str, list[dict[str, Any]]], exclude_patterns: list[str],
) -> list[dict[str, Any]]:
    qtable = quote_dax_table(table)

    def ref(col: str) -> str:
        return quote_dax_column(table, col)

    excl_lower = {e.lower() for e in exclude_patterns}
    safe_numeric = [
        c for c in (
            buckets.get("amount", []) + buckets.get("qty", []) + buckets.get("other_numeric", [])
        )
        if c["name"].lower() not in excl_lower
    ]

    measures: list[dict[str, Any]] = []
    for col in safe_numeric[:5]:
        cname = col["name"]
        measures.append({
            "name": f"Synth Safe Total {cname}",
            "expression": f"SUM({ref(cname)})",
            "displayFolder": "SynthesizedSafe",
            "description": f"Drift-safe total of {cname} (excludes previously-drifted columns).",
            "formatString": "#,##0.00",
        })
    measures.append({
        "name": "Synth Safe Row Count",
        "expression": f"COUNTROWS({qtable})",
        "displayFolder": "SynthesizedSafe",
        "description": "Drift-safe row-count anchor.",
        "formatString": "#,##0",
    })
    return measures


def apply_schema_strategy_spec(
    spec: dict[str, Any],
    base_cols: list[dict[str, Any]],
    amount_names: set[str],
    qty_names: set[str],
    other_numeric_names: set[str],
) -> list[dict[str, Any]]:
    """Interpret a synthesized schema strategy spec into a column list."""
    try:
        rule = spec.get("generation_rule")
        params = spec.get("parameters") or {}
        if rule == "targeted_kpi_boost":
            return _apply_targeted_kpi_boost_schema(
                base_cols, params.get("target_keywords") or [],
                amount_names, other_numeric_names,
            )
        return []
    except Exception:  # noqa: BLE001
        return []


def _apply_targeted_kpi_boost_schema(
    base_cols: list[dict[str, Any]],
    target_keywords: list[str],
    amount_names: set[str],
    other_numeric_names: set[str],
) -> list[dict[str, Any]]:
    kw_lower = [k.lower() for k in target_keywords]
    result: list[dict[str, Any]] = []
    for c in base_cols:
        col = dict(c)
        lname = col["name"].lower()
        if kw_lower and any(kw in lname or lname in kw for kw in kw_lower):
            col["summarizeBy"] = "sum"
        elif col["name"] in amount_names:
            col["summarizeBy"] = "sum"
        elif col["name"] in other_numeric_names:
            col["summarizeBy"] = "none"
        result.append(col)
    return result


def apply_visual_strategy_spec(
    spec: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Interpret a synthesized visual strategy spec into a candidate ordering."""
    try:
        rule = spec.get("generation_rule")
        params = spec.get("parameters") or {}
        if rule == "coherence_gap_fill":
            missing_kinds = set(params.get("missing_kinds") or [])
            if not missing_kinds:
                return []
            return sorted(candidates, key=lambda c: 0 if c.get("kind") in missing_kinds else 1)
        return []
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "StrategySynthesizer",
    "apply_dax_strategy",
    "apply_schema_strategy_spec",
    "apply_visual_strategy_spec",
]
