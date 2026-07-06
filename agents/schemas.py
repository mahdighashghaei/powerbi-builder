"""Pydantic schemas for agent outputs (Phase 1).

These models give every agent a *formal contract* for its output instead of a
free-form dict. They are used in two ways:

1. As ``output_schema`` on ADK ``Agent`` / ``LlmAgent`` definitions so the LLM's
   response is validated against the schema (and retried on failure).
2. As the typed shape that flows through ``session.state`` (the ADK state store)
   so downstream agents — and the feedback loop in Phase 4 — can rely on a
   stable contract rather than duck-typing a dict.

Design notes
------------
* Every model is **optional-friendly**: fields that an agent may not always
  populate default to ``None`` / empty list, so a deterministic (offline) agent
  that only fills part of the shape still validates.
* ``rationale`` / ``source_reasoning`` / ``intent_match_reasoning`` are new
  free-text fields added so later phases (the feedback loop, the planner) can
  understand *why* a decision was made — not just *what* was decided.
* The models mirror the existing dict shapes produced by the deterministic
  agents (``ctx.schema``, ``ctx.measures``, ``ctx.validation`` …) so adopting
  them is a validation layer, not a rewrite of the generators.

These are intentionally lightweight (no custom validators beyond type
coercion) so they stay cheap to construct on every agent run.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data profiling / cleaning
# ---------------------------------------------------------------------------


class ColumnProfile(BaseModel):
    """Quality profile of a single column (nulls, distinct, outliers)."""

    name: str
    null_pct: float = 0.0
    distinct_count: int = 0
    outlier_count: int = 0
    data_type: str = "string"


class DataProfile(BaseModel):
    """Output of ``DataAnalyzerAgent`` — the data quality profile."""

    quality_score: float = 100.0
    issues: list[str] = Field(default_factory=list)
    questions: list[dict[str, Any]] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    blocking_issues: list[str] = Field(default_factory=list)
    verified: bool = True
    verify_warning: Optional[str] = None
    column_profiles: list[ColumnProfile] = Field(default_factory=list)


class CleaningAction(BaseModel):
    """One cleaning step applied to the data."""

    column: str
    action: str  # drop | impute_median | impute_mean | impute_mode | cap | keep
    detail: str = ""


class CleaningPlan(BaseModel):
    """Planned cleaning steps before they are applied."""

    actions: list[CleaningAction] = Field(default_factory=list)
    rationale: str = ""


class CleaningReport(BaseModel):
    """Result of applying a cleaning plan (output of ``DataCleanerAgent``)."""

    applied: list[CleaningAction] = Field(default_factory=list)
    quality_before: float = 100.0
    quality_after: float = 100.0
    improved: bool = False
    cleaned_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Schema / relationships
# ---------------------------------------------------------------------------


class ColumnSpec(BaseModel):
    """A single column in a table schema."""

    name: str
    dataType: str = "string"
    summarizeBy: str = "none"
    sourceColumn: Optional[str] = None


class TableSpec(BaseModel):
    """A table in the inferred schema."""

    table_name: str
    columns: list[ColumnSpec] = Field(default_factory=list)
    connection_type: str = "csv"


class SchemaResult(BaseModel):
    """Output of ``SchemaAgent`` — the inferred data model."""

    table_name: str
    columns: list[ColumnSpec] = Field(default_factory=list)
    all_tables: list[TableSpec] = Field(default_factory=list)


class Relationship(BaseModel):
    """One detected foreign-key relationship."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    to_cardinality: str = "one"
    # Phase 3 fields — populated by the LLM refinement step.
    confidence_score: float = 1.0
    source_reasoning: str = ""


class RelationshipSet(BaseModel):
    """Output of ``RelationshipAgent`` — the full set of relationships."""

    relationships: list[Relationship] = Field(default_factory=list)
    table_count: int = 0


# ---------------------------------------------------------------------------
# DAX measures
# ---------------------------------------------------------------------------


class Measure(BaseModel):
    """A single DAX measure."""

    name: str
    expression: str
    table: str = ""
    displayFolder: str = ""
    description: str = ""
    formatString: str = ""
    # Phase 3 field — why this measure was selected/authored.
    rationale: str = ""


class MeasureSet(BaseModel):
    """Output of ``DAXAgent`` / ``MeasureSelectorAgent``."""

    measures: list[Measure] = Field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.measures)


# ---------------------------------------------------------------------------
# Business Intelligence Reasoning (Phase BI)
# ---------------------------------------------------------------------------


class PageRecommendation(BaseModel):
    """One recommended dashboard page from BIReasoningAgent."""

    id: str
    name: str
    purpose: str = ""
    priority: int = 1  # 1 = highest


class KPIRecommendation(BaseModel):
    """One recommended KPI from BIReasoningAgent."""

    name: str
    why: str = ""
    measure_hint: str = ""  # suggested DAX expression hint
    priority: int = 1       # 1 = highest


class BIReasoningResult(BaseModel):
    """Structured output of ``BIReasoningAgent`` — business intelligence analysis.

    Contains the agent's understanding of business intent before any visuals
    are planned. ``VisualPlannerAgent`` consumes this to make smarter decisions.
    """

    dashboard_goal: str = ""
    target_audience: str = "analyst"    # executive | analyst | operational
    dashboard_type: str = "analytical"  # executive | operational | analytical | storytelling
    recommended_pages: list[PageRecommendation] = Field(default_factory=list)
    recommended_kpis: list[KPIRecommendation] = Field(default_factory=list)
    suggested_analysis: list[str] = Field(default_factory=list)
    analytical_perspectives: list[str] = Field(default_factory=list)
    reasoning: str = ""
    confidence: float = 1.0   # 0.0–1.0
    source: str = "deterministic"  # "llm" | "deterministic"


# ---------------------------------------------------------------------------
# Business Analysis (extended DataAnalyzerAgent output)
# ---------------------------------------------------------------------------


class BusinessAnalysis(BaseModel):
    """Business-oriented analysis produced by ``DataAnalyzerAgent`` in addition
    to its standard quality profile.

    Identifies KPI candidates, trends, and executive metrics from the schema
    so downstream agents can make more informed planning decisions.
    """

    important_measures: list[str] = Field(default_factory=list)
    potential_kpis: list[str] = Field(default_factory=list)
    trend_indicators: list[str] = Field(default_factory=list)
    seasonal_indicators: list[str] = Field(default_factory=list)
    category_dominant_cols: list[str] = Field(default_factory=list)
    executive_metrics: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Report plan
# ---------------------------------------------------------------------------


class VisualReasoning(BaseModel):
    """Structured reasoning for a single visual choice (Phase BI-Reasoning).

    Every ``VisualPlan`` optionally carries explicit rationale so decisions
    can be debugged, explained to users, and fed into future planning loops.
    """

    why_this_visual: str = ""
    why_this_page: str = ""
    why_these_dimensions: str = ""
    why_these_measures: str = ""
    why_this_layout: str = ""
    task_type: str = ""
    """One of: comparison, trend, ranking, distribution, composition, executive_kpi."""


class VisualPlan(BaseModel):
    """One visual on a page (kind + bound fields)."""

    name: str
    kind: str  # card | barChart | columnChart | lineChart | tableEx | …
    measure: Optional[str] = None
    category: Optional[str] = None
    columns: list[list[str]] = Field(default_factory=list)
    # Phase 3 field — why this visual matches the user intent.
    intent_match_reasoning: str = ""
    # Phase BI-Reasoning — structured per-visual rationale (optional).
    visual_reasoning: Optional[VisualReasoning] = None


class PagePlan(BaseModel):
    """One report page with its visuals."""

    id: str
    displayName: str
    visuals: list[VisualPlan] = Field(default_factory=list)


class ReportPlan(BaseModel):
    """Output of ``ReportAgent`` / ``VisualPlannerAgent``."""

    pages: list[PagePlan] = Field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def visual_count(self) -> int:
        return sum(len(p.visuals) for p in self.pages)


# ---------------------------------------------------------------------------
# Validation / feedback loop (Phase 4)
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    """A single issue found by the validator, with routing metadata."""

    severity: str = "warning"  # error | warning
    message: str
    # Phase 4 — which agent should retry to fix this issue.
    agent_responsible: str = ""
    suggested_fix: str = ""


class ValidationResult(BaseModel):
    """Output of ``ValidatorAgent``."""

    ok: bool = True
    tables: int = 0
    measures: int = 0
    pages: int = 0
    visuals: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    fixes_applied: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Build plan (Phase 2 — Planner output)
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    """One step in the build plan."""

    phase: str  # clean | analyze | relationships | dax | report | …
    agent: str
    action: str = ""
    rationale: str = ""


class BuildPlan(BaseModel):
    """Output of ``PlannerAgent`` — drives the orchestrator's agent selection."""

    steps: list[PlanStep] = Field(default_factory=list)
    needs_cleaning: bool = False
    report_style: str = "standard"  # standard | minimal | rich
    # why the plan was shaped this way (from the LLM, or the deterministic note)
    planner_reasoning: str = ""

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def agents(self) -> list[str]:
        return [s.agent for s in self.steps]


# ---------------------------------------------------------------------------
# Business Insights (narrative layer — anomalies, segments, trends, actions)
# ---------------------------------------------------------------------------
#
# Produced by ``InsightsAgent`` (advisory; never blocks a build) from the
# already-computed schema, measures, report plan, business analysis, and
# judge result. Surfaced to the user via the README "Business Insights"
# section — this is the narrative counterpart to the guaranteed DAX
# measures (YoY %, Rank, Anomaly Count) that DAXAgent always includes.


class AnomalyFinding(BaseModel):
    """One column's IQR-based outlier finding."""

    column: str
    outlier_count: int = 0
    outlier_pct: float = 0.0
    method: str = "iqr"
    narrative: str = ""


class SegmentProfile(BaseModel):
    """Aggregate performance of one segment (categorical value) — NOT
    per-row ML clustering. Computed via group-by + quantile tiering."""

    dimension: str
    segment_name: str
    primary_metric: str
    metric_value: float = 0.0
    share_pct: float = 0.0
    tier: str = "Medium"  # High | Medium | Low
    narrative: str = ""


class UnderperformerInsight(BaseModel):
    """A Low-tier segment with a template-derived recommended action."""

    dimension: str
    segment_name: str
    primary_metric: str
    metric_value: float = 0.0
    gap_vs_avg_pct: float = 0.0
    recommended_action: str = ""


class TrendInsight(BaseModel):
    """Current-vs-prior period comparison for one metric."""

    metric: str
    period_label: str = ""
    current_value: float = 0.0
    prior_value: float = 0.0
    change_pct: float = 0.0
    narrative: str = ""


class VisualExplanation(BaseModel):
    """Natural-language explanation of one visual in the delivered report."""

    page: str
    visual_name: str
    kind: str
    explanation: str = ""


class KpiGapSuggestion(BaseModel):
    """One suggested additional KPI/measure, with why it's suggested."""

    suggestion: str
    reason: str = ""


class BusinessInsights(BaseModel):
    """Output of ``InsightsAgent`` — the full narrative layer for one run."""

    anomalies: list[AnomalyFinding] = Field(default_factory=list)
    segments: list[SegmentProfile] = Field(default_factory=list)
    underperformers: list[UnderperformerInsight] = Field(default_factory=list)
    trends: list[TrendInsight] = Field(default_factory=list)
    visual_explanations: list[VisualExplanation] = Field(default_factory=list)
    kpi_gap_suggestions: list[KpiGapSuggestion] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Build specification — the enduring, versioned asset (Spec-Driven Development)
# ---------------------------------------------------------------------------
#
# The generated PBIP code/files are *disposable* (regenerable), but the
# specification of *what was built and why* is an enduring asset that should be
# versioned alongside the build. Each orchestrator run writes a `build.spec.json`
# at the PBIP root; it captures the source, inferred schema, measures, pages,
# validation outcome, and the agent trajectory. This lets a later agent (or a
# human) reproduce or audit the build without re-running it.


class BuildSpec(BaseModel):
    """Versioned specification of a single build run.

    Written as ``build.spec.json`` next to ``README.md`` in the PBIP root. The
    ``schema_version`` field lets future readers migrate older specs. All
    payload fields are optional-friendly so a partial/failed run still produces
    a valid (if incomplete) spec.

    Note: the inferred-model field is named ``data_schema`` (not ``schema``) to
    avoid shadowing ``pydantic.BaseModel.schema`` (deprecated in v2 but still
    present). The JSON key stays ``schema`` via ``serialization_alias``.
    """

    schema_version: str = "1.0"
    project_name: str
    source: dict[str, Any] = Field(default_factory=dict)
    data_schema: dict[str, Any] = Field(default_factory=dict, serialization_alias="schema")
    measures: list[dict[str, Any]] = Field(default_factory=list)
    pages: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    insights: dict[str, Any] = Field(default_factory=dict)
    business_description: str = ""
    started_at: str = ""
    finished_at: str = ""
    ok: bool = False
    builder_version: str = "powerbi-builder"


# ---------------------------------------------------------------------------
# Semantic LLM-assist layer (DataAnalyzerAgent / DataCleanerAgent)
# ---------------------------------------------------------------------------
# Advisor-only outputs: the LLM never touches raw data or executes cleaning
# itself; it only proposes a decision that the existing deterministic
# executors (plan_cleaning/apply_cleaning) carry out unchanged. See
# ``source`` on BIReasoningResult for the same "llm"|"deterministic"
# provenance-marker convention reused here.


class CleaningStrategyDecision(BaseModel):
    """LLM's proposed cleaning strategy for one ambiguous column."""

    column: str
    # identifier|categorical|numeric_measure|numeric_code|date|free_text|ambiguous
    semantic_role: str = "ambiguous"
    # drop_column|fill_mean|fill_mode|fill_median|fill_sentinel|leave_as_is
    null_handling_strategy: str = "leave_as_is"
    confidence: float = 0.0
    reasoning: str = ""
    source: str = "deterministic"  # "llm" | "deterministic"


class ColumnRoleGuess(BaseModel):
    """One column's likely business role, guessed by the semantic-interpretation LLM."""

    column_name: str
    # fact_measure|dimension_key|foreign_key_candidate|date_dimension|descriptive_text|unknown
    likely_role: str = "unknown"


class SemanticInterpretation(BaseModel):
    """Output of DataAnalyzerAgent's optional one-shot LLM interpretation pass."""

    business_domain_guess: str = ""
    column_roles: list[ColumnRoleGuess] = Field(default_factory=list)
    confidence: float = 0.0
    reasoning: str = ""
    source: str = "deterministic"  # "llm" | "deterministic"


__all__ = [
    "ColumnProfile",
    "DataProfile",
    "CleaningAction",
    "CleaningPlan",
    "CleaningReport",
    "ColumnSpec",
    "TableSpec",
    "SchemaResult",
    "Relationship",
    "RelationshipSet",
    "Measure",
    "MeasureSet",
    # BI Reasoning
    "PageRecommendation",
    "KPIRecommendation",
    "BIReasoningResult",
    "BusinessAnalysis",
    # Report plan
    "VisualReasoning",
    "VisualPlan",
    "PagePlan",
    "ReportPlan",
    # Business Insights
    "AnomalyFinding",
    "SegmentProfile",
    "UnderperformerInsight",
    "TrendInsight",
    "VisualExplanation",
    "KpiGapSuggestion",
    "BusinessInsights",
    "ValidationIssue",
    "ValidationResult",
    "PlanStep",
    "BuildPlan",
    "BuildSpec",
    # Semantic LLM-assist layer
    "CleaningStrategyDecision",
    "ColumnRoleGuess",
    "SemanticInterpretation",
]
