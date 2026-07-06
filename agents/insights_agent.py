"""InsightsAgent -- Business Insights narrative layer (advisory).

Role
----
Runs AFTER the feedback loop, once the model, measures, and report plan are
in their final state. Produces the narrative counterpart to the guaranteed
DAX measures DAXAgent always includes (YoY %, Rank, Anomaly Count):

  * Anomaly findings (IQR-based, same method as the guaranteed DAX measure).
  * Aggregate behavior segmentation (group-by + quantile tiering — no ML
    library is a project dependency, so this is deterministic, not per-row
    clustering; the README states this plainly).
  * Underperformer detection with a template-derived recommended action.
  * Trend commentary (current vs. prior period).
  * Missing-KPI suggestions (surfaces ``BusinessAnalysis.recommendations``
    and Judge ``strategy_gaps`` — both already computed elsewhere in the
    pipeline but never shown to the user before this agent existed).
  * A natural-language explanation for every visual in the delivered report.

Design
------
All computation is delegated to ``utils.insights_engine`` (pure, fail-safe
functions). This agent's only job is to gather the already-available
context (``ctx.schema``, ``ctx.measures``, ``ctx.extra["report_plan"]``,
``ctx.extra["business_analysis"]``, ``ctx.extra["judge_result"]``, and —
when a raw/cleaned CSV is available — the row-level data via pandas) and
store the result in ``ctx.extra["insights"]``.

**Fail-safe**: this agent is purely advisory. It never blocks the build —
any internal error still returns ``ok=True`` with an empty insights object,
exactly like ``BIReasoningAgent``.
"""
from __future__ import annotations

from typing import Any

from agents.base import AgentResult, BaseAgent


class InsightsAgent(BaseAgent):
    """Computes the Business Insights narrative layer for the finished build."""

    name = "InsightsAgent"
    description = (
        "You are the InsightsAgent. Given the finished data model, measures, "
        "report plan, and judge result, produce anomaly findings, behavior "
        "segmentation, underperformer recommendations, trend commentary, "
        "missing-KPI suggestions, and a natural-language explanation for "
        "every visual. You are advisory only — never block the build."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        from agents.schemas import BusinessInsights
        from utils.insights_engine import generate_insights

        try:
            df = self._load_dataframe(ctx)
            schema_columns = (ctx.schema or {}).get("columns", [])
            business_analysis = ctx.extra.get("business_analysis")
            report_plan = ctx.extra.get("report_plan")
            judge_result = ctx.extra.get("judge_result")

            insights = generate_insights(
                df=df,
                schema_columns=schema_columns,
                business_analysis=business_analysis,
                report_plan=report_plan,
                ctx_pages=ctx.pages or [],
                judge_result=judge_result,
                prioritized_kpis=ctx.extra.get("prioritized_kpis"),
            )
        except Exception as exc:  # noqa: BLE001 — advisory agent must never crash the build
            self.log.warning(f"insights generation failed, using empty insights: {exc}")
            insights = BusinessInsights()

        ctx.extra["insights"] = insights
        ctx.extra["insights_dict"] = insights.model_dump()

        self.log.info(
            f"insights: {len(insights.anomalies)} anomaly finding(s), "
            f"{len(insights.segments)} segment(s), "
            f"{len(insights.underperformers)} underperformer(s), "
            f"{len(insights.trends)} trend(s), "
            f"{len(insights.visual_explanations)} visual explanation(s), "
            f"{len(insights.kpi_gap_suggestions)} KPI suggestion(s)"
        )

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Business insights: {insights.summary}"
                if insights.summary else "Business insights: nothing notable to report."
            ),
            data={
                "anomaly_count": len(insights.anomalies),
                "segment_count": len(insights.segments),
                "underperformer_count": len(insights.underperformers),
                "trend_count": len(insights.trends),
                "visual_explanation_count": len(insights.visual_explanations),
                "kpi_suggestion_count": len(insights.kpi_gap_suggestions),
            },
        )

    # ------------------------------------------------------------------
    # data access
    # ------------------------------------------------------------------

    def _load_dataframe(self, ctx: Any) -> Any | None:
        """Load the row-level data for data-driven insights, if available.

        In create / edit_excel mode ``ctx.source_path`` points at the
        (already-cleaned, if applicable — see ``DataCleanerAgent``) CSV. In
        edit_pbip / edit_pbix mode there is no raw data file, so data-driven
        insights (anomalies/segments/trends) are skipped gracefully — the
        schema/measure/visual-based insights still run.
        """
        if ctx.input_mode not in ("create", "edit_excel"):
            return None
        source = ctx.source_path
        if not source or not source.is_file() or source.suffix.lower() != ".csv":
            return None
        try:
            import pandas as pd
            return pd.read_csv(source)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"insights: could not read source data for analysis: {exc}")
            return None


__all__ = ["InsightsAgent"]
