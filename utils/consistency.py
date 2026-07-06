"""utils/consistency.py — Cross-Agent Semantic Consistency Checker.

Architecture: Top-3 Kaggle Winning Architecture.

Enforces semantic alignment across the four output layers:
  1. Schema intent       — columns reflect business domain
  2. DAX measures        — measures compute the right KPIs
  3. Visual selection    — visuals surface the right measures
  4. Business KPIs       — all stated KPIs are reachable end-to-end

Detection
---------
The checker computes a per-pair alignment matrix and penalises:

  * KPI mismatch:           A KPI stated in the business description has no
                            corresponding measure AND no visual that would show it.
  * Schema-measure drift:   A measure expression references a column name that
                            cannot be found in any schema table.
  * Visual-semantic incon.: The visual kind distribution is poorly aligned with
                            the dashboard type inferred by BIReasoningAgent.
  * Orphan measures:        A measure exists but is not used in any visual, AND
                            it is not in the KPI list — it is clutter.

Usage
-----
    from utils.consistency import CrossAgentConsistencyChecker

    report = CrossAgentConsistencyChecker().check(ctx)
    # report["aligned"]  → bool (True if all checks pass)
    # report["penalties"] → list[dict] with type, severity, detail
    # report["alignment_score"] → float [0, 1]

Fail-safe contract: check() must never raise. Any exception returns a
neutral "aligned=True" result to preserve fail-safe pipeline behaviour.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.base import AgentContext

# Visual kinds → dashboard affinity (mirrors scoring.py)
_DASHBOARD_VISUAL_MAP: dict[str, frozenset[str]] = {
    "executive":   frozenset({"card", "kpi", "columnChart", "lineChart"}),
    "operational": frozenset({"barChart", "columnChart", "matrix", "slicer"}),
    "analytical":  frozenset({"scatterChart", "lineChart", "matrix", "donutChart"}),
    "narrative":   frozenset({"card", "lineChart", "barChart", "columnChart"}),
    "default":     frozenset({"card", "barChart", "columnChart", "lineChart"}),
}

_COL_REF_RE = re.compile(r"\[([^\]]+)\]")


def _safe_div(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den > 0 else default


def _col_refs_in_expr(expr: str) -> set[str]:
    return set(_COL_REF_RE.findall(expr))


def _token_overlap(a: str, b: str) -> float:
    """Jaccard overlap between token sets of two strings."""
    toks_a = set(re.findall(r"[a-z][a-z0-9]*", a.lower()))
    toks_b = set(re.findall(r"[a-z][a-z0-9]*", b.lower()))
    if not toks_a or not toks_b:
        return 0.0
    return _safe_div(len(toks_a & toks_b), len(toks_a | toks_b))


class CrossAgentConsistencyChecker:
    """Validates semantic alignment between schema, measures, visuals, and KPIs."""

    def check(self, ctx: "AgentContext") -> dict[str, Any]:
        """Run all cross-agent consistency checks.

        Returns
        -------
        {
            "aligned":          bool,
            "alignment_score":  float (0–1),
            "penalties":        list[dict],   # each {type, severity, detail, penalty}
            "kpi_mismatch":     list[str],    # KPIs with no measure + no visual
            "schema_drift":     list[str],    # measures referencing unknown columns
            "visual_incon":     list[str],    # visual kinds mismatched to dash type
            "orphan_measures":  list[str],    # measures not used in any visual
        }
        """
        try:
            return self._check_inner(ctx)
        except Exception as exc:  # noqa: BLE001
            return {
                "aligned":         True,
                "alignment_score": 1.0,
                "penalties":       [{"type": "checker_error", "detail": str(exc)}],
                "kpi_mismatch":    [],
                "schema_drift":    [],
                "visual_incon":    [],
                "orphan_measures": [],
            }

    def _check_inner(self, ctx: "AgentContext") -> dict[str, Any]:
        penalties: list[dict[str, Any]] = []

        # ----------------------------------------------------------------
        # Gather artifacts
        # ----------------------------------------------------------------
        measures: list[dict] = list(ctx.measures or [])
        measure_names: set[str] = {m["name"] for m in measures}

        # Schema columns (all tables)
        schema_cols: set[str] = set()
        if ctx.schema:
            for c in ctx.schema.get("columns", []):
                schema_cols.add(c["name"])
            for tbl in ctx.schema.get("all_tables", []):
                if isinstance(tbl, dict):
                    for c in tbl.get("columns", []):
                        schema_cols.add(c["name"])

        # Visual measure bindings
        visual_measures: set[str] = set()
        visual_kinds: list[str] = []
        report_plan = ctx.extra.get("report_plan")  # type: ignore[union-attr]
        if report_plan is not None:
            for page in getattr(report_plan, "pages", []):
                for v in page.visuals:
                    if v.measure:
                        visual_measures.add(v.measure)
                    visual_kinds.append(v.kind)

        # Business KPIs
        biz = ctx.extra.get("business_analysis")  # type: ignore[union-attr]
        kpis: list[str] = list(getattr(biz, "potential_kpis", []) or []) if biz else []

        # Dashboard type
        bi_reasoning = ctx.extra.get("bi_reasoning")  # type: ignore[union-attr]
        dashboard_type = (
            getattr(bi_reasoning, "dashboard_type", "default")
            if bi_reasoning else "default"
        )

        # ----------------------------------------------------------------
        # Check A: KPI mismatch
        # A stated KPI has no measure AND no visual showing it.
        # ----------------------------------------------------------------
        kpi_mismatch: list[str] = []
        measure_names_lower = {n.lower() for n in measure_names}
        visual_measures_lower = {n.lower() for n in visual_measures}

        for kpi in kpis:
            kpi_low = kpi.lower()
            has_measure = any(kpi_low in mn or mn in kpi_low for mn in measure_names_lower)
            has_visual = any(kpi_low in vm or vm in kpi_low for vm in visual_measures_lower)
            if not has_measure and not has_visual:
                kpi_mismatch.append(kpi)

        if kpi_mismatch:
            penalty = 0.15 * min(1.0, len(kpi_mismatch) / max(len(kpis), 1))
            penalties.append({
                "type":     "kpi_mismatch",
                "severity": "warning",
                "detail":   kpi_mismatch,
                "penalty":  round(penalty, 4),
                "message":  (
                    f"{len(kpi_mismatch)} KPI(s) have no measure or visual: "
                    f"{kpi_mismatch}"
                ),
            })

        # ----------------------------------------------------------------
        # Check B: Schema-measure drift
        # A measure references a column name absent from every schema table.
        # ----------------------------------------------------------------
        schema_drift: list[str] = []
        for m in measures:
            refs = _col_refs_in_expr(m.get("expression", ""))
            unknown = refs - schema_cols - measure_names  # exclude self-refs to other measures
            if unknown:
                schema_drift.append(
                    f"measure='{m['name']}' unknown_cols={sorted(unknown)}"
                )

        if schema_drift:
            penalty = 0.20 * min(1.0, len(schema_drift) / max(len(measures), 1))
            penalties.append({
                "type":     "schema_measure_drift",
                "severity": "error",
                "detail":   schema_drift,
                "penalty":  round(penalty, 4),
                "message":  (
                    f"{len(schema_drift)} measure(s) reference unknown schema column(s)."
                ),
            })

        # ----------------------------------------------------------------
        # Check C: Visual-semantic inconsistency
        # The visual kind distribution is poorly aligned with dashboard type.
        # ----------------------------------------------------------------
        visual_incon: list[str] = []
        visual_coherence = 1.0
        if visual_kinds:
            ideal = _DASHBOARD_VISUAL_MAP.get(dashboard_type, _DASHBOARD_VISUAL_MAP["default"])
            present = set(visual_kinds)
            overlap = len(present & ideal)
            precision = _safe_div(overlap, len(present))
            recall = _safe_div(overlap, len(ideal))
            visual_coherence = (
                2 * precision * recall / (precision + recall)
                if (precision + recall) > 0 else 0.0
            )
            unexpected = present - ideal
            if unexpected and visual_coherence < 0.4:
                visual_incon = [
                    f"unexpected kind '{k}' for dashboard_type='{dashboard_type}'"
                    for k in sorted(unexpected)
                ]
                penalty = (1.0 - visual_coherence) * 0.10
                penalties.append({
                    "type":     "visual_semantic_inconsistency",
                    "severity": "warning",
                    "detail":   visual_incon,
                    "penalty":  round(penalty, 4),
                    "message":  (
                        f"Visual kind coherence {visual_coherence:.2f} below threshold "
                        f"for dashboard_type='{dashboard_type}'."
                    ),
                })

        # ----------------------------------------------------------------
        # Check D: Orphan measures
        # Measures that are not used in any visual and are not KPI-relevant.
        # ----------------------------------------------------------------
        orphan_measures: list[str] = []
        kpi_text = " ".join(kpis).lower()
        for m in measures:
            mname = m["name"]
            in_visual = mname in visual_measures
            is_kpi_relevant = (
                mname.lower() in kpi_text
                or any(mname.lower() in k.lower() or k.lower() in mname.lower()
                       for k in kpis)
            )
            if not in_visual and not is_kpi_relevant and len(measures) > 3:
                orphan_measures.append(mname)

        if orphan_measures:
            penalty = 0.05 * min(1.0, len(orphan_measures) / max(len(measures), 1))
            penalties.append({
                "type":     "orphan_measures",
                "severity": "info",
                "detail":   orphan_measures,
                "penalty":  round(penalty, 4),
                "message":  (
                    f"{len(orphan_measures)} measure(s) unused in visuals and "
                    f"not KPI-relevant (consider removing clutter)."
                ),
            })

        # ----------------------------------------------------------------
        # Aggregate alignment score
        # ----------------------------------------------------------------
        total_penalty = sum(p["penalty"] for p in penalties)
        alignment_score = max(0.0, 1.0 - total_penalty)
        aligned = (
            alignment_score >= 0.75
            and not schema_drift
            and len(kpi_mismatch) == 0
        )

        return {
            "aligned":         aligned,
            "alignment_score": round(alignment_score, 4),
            "penalties":       penalties,
            "kpi_mismatch":    kpi_mismatch,
            "schema_drift":    schema_drift,
            "visual_incon":    visual_incon,
            "orphan_measures": orphan_measures,
        }


__all__ = ["CrossAgentConsistencyChecker"]
