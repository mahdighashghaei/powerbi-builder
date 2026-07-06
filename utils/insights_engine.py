"""utils/insights_engine.py — Business Insights narrative layer.

Produces the narrative counterpart to the guaranteed DAX measures
(``"{amt} YoY %"``, ``"{cat} {amt} Rank"``, ``"{amt} Anomaly Count"`` — see
``agents/dax_agent.py``): anomaly findings, aggregate behavior segmentation,
underperformer action recommendations, trend commentary, missing-KPI
suggestions, and natural-language visual explanations.

Honesty note: ``segment_by_behavior`` is deterministic aggregate/quantile
tiering (group-by + sum + tercile split) — NOT per-row ML clustering. No ML
library (scikit-learn, etc.) is a project dependency, so this is the
honest, fully-offline mechanism available. This is stated in the generated
README so the user knows exactly what produced the segmentation.

Fail-safe contract: every public function is exception-safe and returns an
empty list / neutral default on any internal error, matching the convention
already used throughout ``utils/`` (``adaptive_learning.py``,
``learning_memory.py``, ``strategy_synthesizer.py``).
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Anomaly detection (IQR — mirrors mcp_server/schema_inference.py:75-82)
# ---------------------------------------------------------------------------


def detect_anomalies(df: Any, amount_columns: list[dict[str, Any]]) -> list[Any]:
    """Return one ``AnomalyFinding`` per numeric column with IQR outliers.

    Args:
        df:              A pandas DataFrame (row-level data).
        amount_columns:  Column dicts (``{"name": ...}``) considered
                          business-meaningful amounts (from ``_classify_columns``).
    """
    from agents.schemas import AnomalyFinding

    findings: list[AnomalyFinding] = []
    try:
        if df is None or not len(df):
            return []
        n_rows = len(df)
        for col in amount_columns:
            name = col.get("name")
            if not name or name not in df.columns:
                continue
            try:
                series = df[name].dropna()
                if len(series) < 5:
                    continue
                q1 = series.quantile(0.25)
                q3 = series.quantile(0.75)
                iqr = q3 - q1
                if iqr <= 0:
                    continue
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                outliers = series[(series < lower) | (series > upper)]
                count = int(len(outliers))
                if count == 0:
                    continue
                pct = round(count / n_rows * 100, 2)
                findings.append(AnomalyFinding(
                    column=name,
                    outlier_count=count,
                    outlier_pct=pct,
                    method="iqr",
                    narrative=(
                        f"'{name}' has {count} outlier value(s) ({pct}% of rows), "
                        f"detected via the IQR method (values outside "
                        f"[{lower:,.2f}, {upper:,.2f}])."
                    ),
                ))
            except Exception:  # noqa: BLE001 — one bad column must not drop the rest
                continue
        return findings
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Behavior segmentation (deterministic aggregate/quantile tiering)
# ---------------------------------------------------------------------------

_MIN_SEGMENT_CARDINALITY = 2
_MAX_SEGMENT_CARDINALITY = 20


def _pick_segmentation_column(
    df: Any, category_columns: list[dict[str, Any]],
) -> str | None:
    """Pick the categorical column with the most usable cardinality."""
    best_name: str | None = None
    best_card = -1
    for col in category_columns:
        name = col.get("name")
        if not name or name not in df.columns:
            continue
        card = df[name].nunique(dropna=True)
        if _MIN_SEGMENT_CARDINALITY <= card <= _MAX_SEGMENT_CARDINALITY and card > best_card:
            best_card = card
            best_name = name
    return best_name


def segment_by_behavior(
    df: Any,
    category_columns: list[dict[str, Any]],
    amount_col: str | None,
) -> list[Any]:
    """Group by the best categorical dimension, sum ``amount_col``, tier by tercile.

    Returns an empty list when no usable categorical column or amount column
    is available — never fabricates a segmentation.
    """
    from agents.schemas import SegmentProfile

    try:
        if df is None or not len(df) or not amount_col or amount_col not in df.columns:
            return []
        dim = _pick_segmentation_column(df, category_columns)
        if not dim:
            return []

        grouped = df.groupby(dim)[amount_col].sum().sort_values(ascending=False)
        total = float(grouped.sum())
        if total == 0 or len(grouped) == 0:
            return []

        n = len(grouped)
        high_cut = max(1, round(n / 3))
        low_cut = max(high_cut, n - max(1, round(n / 3)))

        profiles: list[SegmentProfile] = []
        for rank, (seg_name, value) in enumerate(grouped.items()):
            if rank < high_cut:
                tier = "High"
            elif rank >= low_cut:
                tier = "Low"
            else:
                tier = "Medium"
            share = round(float(value) / total * 100, 2)
            profiles.append(SegmentProfile(
                dimension=dim,
                segment_name=str(seg_name),
                primary_metric=amount_col,
                metric_value=round(float(value), 2),
                share_pct=share,
                tier=tier,
                narrative=(
                    f"'{seg_name}' ({dim}) contributes {share}% of total {amount_col} "
                    f"— {tier} tier."
                ),
            ))
        return profiles
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Underperformer detection + recommended actions
# ---------------------------------------------------------------------------


def find_underperformers(
    segments: list[Any],
    df: Any = None,
    discount_col: str | None = None,
) -> list[Any]:
    """Return an ``UnderperformerInsight`` per Low-tier segment with a
    template-derived recommended action.

    When ``discount_col`` is provided and present in ``df``, the action text
    distinguishes "high discount exposure" from a generic "low volume"
    underperformance.
    """
    from agents.schemas import UnderperformerInsight

    try:
        if not segments:
            return []
        values = [s.metric_value for s in segments]
        avg = sum(values) / len(values) if values else 0.0
        if avg == 0:
            return []

        high_discount_segments: set[str] = set()
        if df is not None and discount_col and discount_col in getattr(df, "columns", []):
            try:
                dim = segments[0].dimension
                if dim in df.columns:
                    disc_by_seg = df.groupby(dim)[discount_col].mean()
                    overall_disc = float(df[discount_col].mean())
                    if overall_disc > 0:
                        high_discount_segments = {
                            str(k) for k, v in disc_by_seg.items()
                            if v > overall_disc * 1.25
                        }
            except Exception:  # noqa: BLE001
                pass

        results: list[UnderperformerInsight] = []
        for seg in segments:
            if seg.tier != "Low":
                continue
            gap_pct = round((seg.metric_value - avg) / avg * 100, 2)
            if seg.segment_name in high_discount_segments:
                action = (
                    f"'{seg.segment_name}' shows above-average {discount_col} alongside "
                    f"below-average {seg.primary_metric} — review discount/pricing policy "
                    f"for this {seg.dimension} before increasing promotional spend."
                )
            else:
                action = (
                    f"'{seg.segment_name}' is {abs(gap_pct):.1f}% below the average "
                    f"{seg.primary_metric} across {seg.dimension} — consider targeted "
                    f"marketing, bundling, or a pricing review to lift this segment."
                )
            results.append(UnderperformerInsight(
                dimension=seg.dimension,
                segment_name=seg.segment_name,
                primary_metric=seg.primary_metric,
                metric_value=seg.metric_value,
                gap_vs_avg_pct=gap_pct,
                recommended_action=action,
            ))
        return results
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Trend commentary (current vs prior period)
# ---------------------------------------------------------------------------


def compute_trends(
    df: Any,
    date_col: str | None,
    amount_columns: list[str],
) -> list[Any]:
    """Compare the last two periods (year, or month when <2 years span) for
    each amount column. Returns an empty list when fewer than 2 periods of
    data are available — never fabricates a trend from a single point.
    """
    from agents.schemas import TrendInsight

    try:
        if df is None or not date_col or date_col not in getattr(df, "columns", []):
            return []
        import pandas as pd  # local import — pandas is a hard project dependency

        dates = pd.to_datetime(df[date_col], errors="coerce")
        valid = dates.notna()
        if valid.sum() < 2:
            return []
        span_days = (dates[valid].max() - dates[valid].min()).days
        freq_label, period_key = (
            ("year", dates.dt.year) if span_days > 400 else ("month", dates.dt.to_period("M"))
        )

        trends: list[TrendInsight] = []
        for amt in amount_columns:
            if amt not in df.columns:
                continue
            try:
                grouped = df.loc[valid].groupby(period_key[valid])[amt].sum().sort_index()
                if len(grouped) < 2:
                    continue
                prior_period, current_period = grouped.index[-2], grouped.index[-1]
                prior_val = float(grouped.iloc[-2])
                current_val = float(grouped.iloc[-1])
                change_pct = (
                    round((current_val - prior_val) / prior_val * 100, 2)
                    if prior_val != 0 else 0.0
                )
                direction = "up" if change_pct >= 0 else "down"
                trends.append(TrendInsight(
                    metric=amt,
                    period_label=f"{current_period} vs {prior_period} ({freq_label})",
                    current_value=round(current_val, 2),
                    prior_value=round(prior_val, 2),
                    change_pct=change_pct,
                    narrative=(
                        f"{amt} is {direction} {abs(change_pct):.1f}% in {current_period} "
                        f"({current_val:,.2f}) vs {prior_period} ({prior_val:,.2f})."
                    ),
                ))
            except Exception:  # noqa: BLE001 — one bad column must not drop the rest
                continue
        return trends
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Missing-KPI suggestions
# ---------------------------------------------------------------------------


def suggest_missing_kpis(
    business_analysis: Any | None,
    judge_result: dict[str, Any] | None,
) -> list[Any]:
    """Surface BusinessAnalysis.recommendations + judge strategy_gaps as
    plain-English suggestions. Both signals already exist elsewhere in the
    pipeline but were never shown to the user before this layer."""
    from agents.schemas import KpiGapSuggestion

    try:
        suggestions: list[KpiGapSuggestion] = []
        seen: set[str] = set()

        if business_analysis is not None:
            for rec in (getattr(business_analysis, "recommendations", None) or []):
                if rec not in seen:
                    seen.add(rec)
                    suggestions.append(KpiGapSuggestion(
                        suggestion=rec, reason="identified during data analysis",
                    ))

        for gap in ((judge_result or {}).get("strategy_gaps") or []):
            if not isinstance(gap, dict):
                continue
            text = (
                f"Address {gap.get('domain', 'model')} gap: "
                f"{gap.get('missing_pattern', 'coverage gap detected')}"
            )
            if text not in seen:
                seen.add(text)
                suggestions.append(KpiGapSuggestion(
                    suggestion=text,
                    reason=(
                        f"Judge detected this gap; suggested synthesis: "
                        f"{gap.get('suggested_synthesis', 'n/a')}"
                    ),
                ))
        return suggestions
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Visual explanations (natural language, every visual, LLM or deterministic)
# ---------------------------------------------------------------------------

_GENERIC_REASONING_PLACEHOLDERS = {
    "", "layout-split", "fallback candidate", "deterministic candidate visual",
}

_KIND_TEMPLATES: dict[str, str] = {
    "card":         "Shows the total {measure} as a single headline number.",
    "kpi":          "Highlights {measure} as a key performance indicator.",
    "barChart":     "Compares {measure} across {category}, making it easy to spot the highest and lowest performers.",
    "columnChart":  "Compares {measure} across {category} side-by-side.",
    "donutChart":   "Shows the proportion of {measure} contributed by each {category}.",
    "lineChart":    "Shows how {measure} trends over {category}.",
    "scatterChart": "Shows the relationship between measures across {category}.",
    "matrix":       "Breaks down {measure} across {category} in a cross-tab for detailed drill-down.",
    "tableEx":      "Lists the underlying records for row-level detail.",
    "slicer":       "Lets you filter the whole report by {category}.",
}


def _is_meaningful_reasoning(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    return stripped.lower() not in _GENERIC_REASONING_PLACEHOLDERS and len(stripped) > 15


def _template_explanation(kind: str, measure: str | None, category: str | None) -> str:
    template = _KIND_TEMPLATES.get(kind, "Displays {measure}.")
    return template.format(
        measure=measure or "the selected measure",
        category=category or "category",
    )


def compile_visual_explanations(
    report_plan: Any | None,
    ctx_pages: list[dict[str, Any]] | None,
) -> list[Any]:
    """Explain every visual actually written to the report.

    ``report_plan`` (``ctx.extra["report_plan"]``) carries per-visual
    ``measure``/``category``/``intent_match_reasoning``/``visual_reasoning``
    but its page grouping may differ from what was finally written (layout
    redistribution in ``ReportAgent``). ``ctx_pages`` (``ctx.pages``) is the
    authoritative final page/visual listing but only has ``id``/``visualType``.
    This joins the two by visual name so every delivered visual gets an
    explanation using its real measure/category context.
    """
    from agents.schemas import VisualExplanation

    try:
        if not ctx_pages:
            return []

        context_by_name: dict[str, dict[str, Any]] = {}
        for page in (getattr(report_plan, "pages", None) or []):
            for v in getattr(page, "visuals", None) or []:
                reasoning = ""
                vr = getattr(v, "visual_reasoning", None)
                if vr is not None:
                    reasoning = " ".join(filter(None, [
                        getattr(vr, "why_this_visual", ""),
                        getattr(vr, "why_these_measures", ""),
                        getattr(vr, "why_these_dimensions", ""),
                    ])).strip()
                if not _is_meaningful_reasoning(reasoning):
                    reasoning = getattr(v, "intent_match_reasoning", "") or ""
                context_by_name[v.name] = {
                    "measure": getattr(v, "measure", None),
                    "category": getattr(v, "category", None),
                    "reasoning": reasoning,
                }

        explanations: list[VisualExplanation] = []
        for page in ctx_pages:
            page_name = page.get("displayName", page.get("id", "Page"))
            for vis in page.get("visuals", []):
                name = vis.get("id", "")
                kind = vis.get("visualType", "")
                ctx_info = context_by_name.get(name, {})
                reasoning = ctx_info.get("reasoning", "")
                if _is_meaningful_reasoning(reasoning):
                    explanation = reasoning
                else:
                    explanation = _template_explanation(
                        kind, ctx_info.get("measure"), ctx_info.get("category"),
                    )
                explanations.append(VisualExplanation(
                    page=page_name, visual_name=name, kind=kind, explanation=explanation,
                ))
        return explanations
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def _build_summary(
    anomalies: list[Any], segments: list[Any],
    underperformers: list[Any], trends: list[Any],
) -> str:
    parts: list[str] = []
    if trends:
        parts.append(f"{len(trends)} trend(s) analysed")
    if segments:
        parts.append(f"{len(segments)} segment(s) profiled")
    if underperformers:
        parts.append(f"{len(underperformers)} underperforming segment(s) flagged")
    if anomalies:
        parts.append(f"{len(anomalies)} column(s) with statistical anomalies")
    return "; ".join(parts) if parts else "No data-driven insights available for this run."


def generate_insights(
    df: Any | None,
    schema_columns: list[dict[str, Any]],
    business_analysis: Any | None,
    report_plan: Any | None,
    ctx_pages: list[dict[str, Any]] | None,
    judge_result: dict[str, Any] | None,
    prioritized_kpis: list[str] | None = None,
) -> Any:
    """Compute the full ``BusinessInsights`` object for one run. Never raises.

    ``prioritized_kpis`` (from ``utils.kpi_prioritizer`` via
    ``ctx.extra["prioritized_kpis"]``) is the same business-importance
    ranking DAXAgent/ReportAgent use — when provided, the amount columns are
    reordered so segmentation/trend analysis anchors on the same #1 KPI
    rather than the first amount column in raw schema order.
    """
    from agents.schemas import BusinessInsights

    try:
        from agents.dax_agent import _classify_columns
        from utils.kpi_prioritizer import reorder_by_priority

        buckets = _classify_columns(schema_columns or [])
        amount_cols = buckets.get("amount", [])
        if prioritized_kpis:
            amount_cols = reorder_by_priority(amount_cols, prioritized_kpis)
        category_cols = buckets.get("category", []) + buckets.get("region", [])
        date_cols = buckets.get("date", [])

        anomalies: list[Any] = []
        segments: list[Any] = []
        underperformers: list[Any] = []
        trends: list[Any] = []

        if df is not None:
            anomalies = detect_anomalies(df, amount_cols)
            primary_amt = amount_cols[0]["name"] if amount_cols else None
            segments = segment_by_behavior(df, category_cols, primary_amt)
            discount_col = next(
                (c["name"] for c in schema_columns or []
                 if "discount" in c.get("name", "").lower()),
                None,
            )
            underperformers = find_underperformers(segments, df, discount_col)
            if date_cols and amount_cols:
                trends = compute_trends(
                    df, date_cols[0]["name"], [c["name"] for c in amount_cols[:3]],
                )

        visual_explanations = compile_visual_explanations(report_plan, ctx_pages)
        kpi_gap_suggestions = suggest_missing_kpis(business_analysis, judge_result)
        summary = _build_summary(anomalies, segments, underperformers, trends)

        return BusinessInsights(
            anomalies=anomalies,
            segments=segments,
            underperformers=underperformers,
            trends=trends,
            visual_explanations=visual_explanations,
            kpi_gap_suggestions=kpi_gap_suggestions,
            summary=summary,
        )
    except Exception:  # noqa: BLE001
        return BusinessInsights()


__all__ = [
    "detect_anomalies",
    "segment_by_behavior",
    "find_underperformers",
    "compute_trends",
    "suggest_missing_kpis",
    "compile_visual_explanations",
    "generate_insights",
]
