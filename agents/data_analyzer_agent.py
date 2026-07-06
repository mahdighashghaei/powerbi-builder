"""DataAnalyzerAgent -- profiles data quality and flags issues + questions.

Role
----
Runs BEFORE SchemaAgent (in create / edit_excel mode) to analyse the raw data
file for quality problems: nulls, outliers, duplicates, single-value columns,
type mismatches. It produces a structured profile stored in
``ctx.extra["data_profile"]`` that downstream agents (Cleaner, Planner) read.

Key behaviours:
  * **Self-verification**: re-reads a small sample and cross-checks null /
    duplicate counts. Flags a ``verify_warning`` if the second read disagrees.
  * **Questions**: for ambiguous issues (e.g. "column X has 50% nulls — drop
    or impute?") it builds a list of questions. In interactive CLI mode the
    orchestrator asks the user; otherwise a best-effort decision is recorded
    in ``ctx.extra["answers"]``.
  * **Blocking issues**: critical problems (e.g. empty file, all-null key
    column) are reported as ``blocking_issues``. With no interaction the run
    is marked failed so the user sees a clear, actionable report.

MCP tools used: ``read_csv_schema`` (for schema), ``profile_data_file`` (direct
import) for quality stats.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import ColumnRoleGuess, SemanticInterpretation
from utils import AuditLogger

_log = AuditLogger.get("agent.data_analyzer")


# Thresholds for issue classification (percentages)
_NULL_DROP_THRESHOLD = 60.0     # >60% nulls → suggest drop
_NULL_IMPUTE_THRESHOLD = 40.0   # 40-60% nulls → suggest impute (ambiguous)
_LOW_CARDINALITY = 1            # distinct <= 1 → single-value column

_ALLOWED_COLUMN_ROLES = frozenset({
    "fact_measure", "dimension_key", "foreign_key_candidate",
    "date_dimension", "descriptive_text", "unknown",
})


def _get_semantic_interpretation_llm(
    data_profile: dict[str, Any], business_description: str,
) -> SemanticInterpretation | None:
    """Optional, one-shot LLM interpretation of the already-computed data
    profile: a business-domain guess + a per-column role guess. Advisor-
    only — this NEVER changes the raw statistical profile (quality score,
    issues, nulls, etc.), it only adds an interpretive signal alongside it
    for later stages (e.g. PlannerAgent/BIReasoningAgent) to optionally
    consume.

    Returns ``None`` on ANY failure (flag off, no key, call failure/
    timeout, malformed response, or confidence below the configured
    threshold) — same fail-safe contract as every other optional-LLM
    agent in this codebase.
    """
    try:
        from config import DEFAULT_SETTINGS
        if not DEFAULT_SETTINGS.semantic_llm_assist_enabled:
            return None
        from utils.model_config import MissingAPIKeyError, get_llm_config
        from utils.retry import retry_sync
    except Exception:
        return None

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError as exc:
        _log.error(f"LLM provider misconfigured, skipping semantic interpretation: {exc}")
        return None
    if llm_config is None:
        return None

    quality = data_profile.get("quality") or {}
    cols_by_name = quality.get("columns") or {}
    schema = data_profile.get("schema") or {}
    col_summaries = []
    for col in schema.get("columns", [])[:40]:
        name = col.get("name")
        cp = cols_by_name.get(name, {})
        col_summaries.append({
            "name": name,
            "dataType": col.get("dataType"),
            "null_pct": cp.get("null_pct", 0),
            "distinct_count": cp.get("distinct_count", 0),
            "unique_pct": cp.get("unique_pct", 0),
            "sample_values": (cp.get("distinct_values") or [])[:10],
        })

    prompt = (
        "You are a data analyst. Given ONLY the pre-computed statistical "
        "profile below (never the raw dataset), guess the business domain "
        "and each column's likely analytical role.\n\n"
        "Output ONLY valid JSON matching this schema (no prose, no code fences):\n"
        "{\n"
        '  "business_domain_guess": "string, e.g. \'retail sales\', \'HR/attendance\', \'logistics\'",\n'
        '  "column_roles": [\n'
        '    {"column_name": "string", "likely_role": '
        '"fact_measure|dimension_key|foreign_key_candidate|date_dimension|descriptive_text|unknown"}\n'
        "  ],\n"
        '  "confidence": 0.0,\n'
        '  "reasoning": "string"\n'
        "}\n\n"
        "Rules:\n"
        "- fact_measure: a numeric column meant to be aggregated (amount, "
        "price, quantity, a rate/ratio).\n"
        "- dimension_key: a categorical column used to slice/group data.\n"
        "- foreign_key_candidate: an identifier-shaped column that likely "
        "references another table/entity.\n"
        "- date_dimension: a date/time column.\n"
        "- descriptive_text: free text with no clear analytical role.\n"
        "- confidence: your confidence in this interpretation (0.0-1.0).\n\n"
        f"BUSINESS_CONTEXT: {business_description}\n"
        f"COLUMN_PROFILE: {json.dumps(col_summaries)}\n"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config, timeout=15)

    try:
        text = retry_sync(_call_once, retries=1, base_delay=0.5, max_delay=2.0)
    except Exception as exc:
        _log.warning(f"semantic-interpretation LLM call failed, skipping: {exc}")
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        _log.warning("semantic-interpretation LLM response had no JSON object, skipping")
        return None

    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        _log.warning(f"semantic-interpretation LLM response failed JSON parsing: {exc}")
        return None

    try:
        roles = []
        for r in raw.get("column_roles", []):
            role = str(r.get("likely_role", "unknown"))
            if role not in _ALLOWED_COLUMN_ROLES:
                role = "unknown"
            cname = r.get("column_name")
            if not cname:
                continue
            roles.append(ColumnRoleGuess(column_name=str(cname), likely_role=role))

        result = SemanticInterpretation(
            business_domain_guess=str(raw.get("business_domain_guess", "")),
            column_roles=roles,
            confidence=float(raw.get("confidence", 0.0)),
            reasoning=str(raw.get("reasoning", "")),
            source="llm",
        )
    except Exception as exc:
        _log.warning(f"semantic-interpretation LLM response failed schema validation: {exc}")
        return None

    if result.confidence < DEFAULT_SETTINGS.semantic_llm_confidence_threshold:
        _log.info(
            f"semantic-interpretation LLM confidence {result.confidence} below "
            f"threshold {DEFAULT_SETTINGS.semantic_llm_confidence_threshold} -- discarding"
        )
        return None

    return result


class DataAnalyzerAgent(BaseAgent):
    """Analyses raw data quality before the schema is built."""

    name = "DataAnalyzerAgent"
    description = (
        "You are the DataAnalyzerAgent. Inspect the user's data file for "
        "quality issues — nulls, outliers, duplicates, single-value columns, "
        "type mismatches. Produce a structured profile with a quality score, "
        "a list of issues, and questions for the user when a decision is "
        "ambiguous. Self-verify your analysis by re-reading a sample. Never "
        "guess silently: if something is wrong, surface it."
    )

    def _run(self) -> AgentResult:
        ctx = self.context

        # Only profile raw data files (create / edit_excel). In edit_pbip /
        # edit_pbix there is no raw data to profile — the TMDL already encodes
        # the schema; we record a minimal profile from the model instead.
        if ctx.input_mode in ("edit_pbip", "edit_pbix"):
            return self._profile_from_model(ctx)

        source = ctx.source_path
        if not source or not source.is_file():
            return AgentResult(
                agent=self.name, ok=True,
                message="No raw data file to profile — skipping analysis.",
                data={"skipped": True},
            )

        # 1) full profile (schema + quality)
        from mcp_server.schema_inference import profile_data_file
        try:
            result = profile_data_file(source)
        except Exception as exc:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Data analysis failed: {exc}",
                errors=[str(exc)],
            )

        # 2) self-verification: re-read a tiny sample and cross-check nulls
        verify_warning = self._self_verify(source, result)

        # 3) build questions for ambiguous issues
        questions = self._build_questions(result)
        answers = self._best_effort_answers(questions)
        blocking = self._blocking_issues(result)

        profile = {
            "quality_score": result.get("quality_score", 100),
            "quality": result.get("quality", {}),
            "issues": result.get("issues", []),
            "questions": questions,
            "answers": answers,
            "blocking_issues": blocking,
            "verified": verify_warning is None,
            "verify_warning": verify_warning,
            "schema": result.get("schema"),
        }
        ctx.extra["data_profile"] = profile
        # pre-seed answers so Cleaner can run non-interactively
        ctx.extra["answers"] = answers

        # 4) business-oriented analysis (structured recommendations for downstream agents)
        schema_for_biz = result.get("schema") or {}
        biz_analysis = self._business_analysis(schema_for_biz)
        ctx.extra["business_analysis"] = biz_analysis

        # 5) optional one-shot LLM semantic interpretation (advisor-only —
        # additional signal for later stages; never changes the profile
        # above). No-op, zero LLM calls, key absent entirely unless
        # ENABLE_SEMANTIC_LLM_ASSIST is on.
        try:
            from config import DEFAULT_SETTINGS
            if DEFAULT_SETTINGS.semantic_llm_assist_enabled:
                interpretation = _get_semantic_interpretation_llm(
                    profile, ctx.business_description or "",
                )
                if interpretation is not None:
                    ctx.extra["semantic_interpretation"] = interpretation.model_dump()
        except Exception as exc:  # noqa: BLE001 — must never block analysis
            self.log.warning(f"semantic interpretation skipped: {exc}")

        self.log.info(
            f"data analysis: score={profile['quality_score']}, "
            f"issues={len(profile['issues'])}, questions={len(questions)}, "
            f"blocking={len(blocking)}, verified={profile['verified']}, "
            f"kpis_identified={len(biz_analysis.potential_kpis)}"
        )

        ok = not blocking or ctx.extra.get("interactive", False)
        return AgentResult(
            agent=self.name,
            ok=ok,
            message=(
                f"Analysed data: quality score {profile['quality_score']}/100, "
                f"{len(profile['issues'])} issue(s)"
                + (f", {len(blocking)} blocking" if blocking else "")
                + (f", {len(questions)} question(s) for user" if questions else "")
            ),
            data={
                "quality_score": profile["quality_score"],
                "issue_count": len(profile["issues"]),
                "question_count": len(questions),
                "blocking_count": len(blocking),
                "verified": profile["verified"],
                "potential_kpi_count": len(biz_analysis.potential_kpis),
            },
            errors=blocking,
        )

    # ------------------------------------------------------------------
    # Business-oriented analysis
    # ------------------------------------------------------------------

    def _business_analysis(self, schema: dict) -> "BusinessAnalysis":  # type: ignore[name-defined]
        """Produce business-oriented analysis from the inferred schema.

        Identifies KPI candidates, trend indicators, seasonal markers, and
        executive metrics so downstream agents (BIReasoningAgent,
        VisualPlannerAgent) can make more informed decisions.
        """
        from agents.schemas import BusinessAnalysis
        from agents.dax_agent import _classify_columns
        from utils.explainability import log_decision

        columns = schema.get("columns", [])
        table = schema.get("table_name", "Table")
        if not columns:
            return BusinessAnalysis()

        buckets = _classify_columns(columns)

        important_measures: list[str] = []
        potential_kpis: list[str] = []
        trend_indicators: list[str] = []
        seasonal_indicators: list[str] = []
        category_dominant: list[str] = []
        executive_metrics: list[str] = []
        recommendations: list[str] = []

        # Amount columns → important measures + potential KPIs
        for col in buckets.get("amount", []):
            name = col["name"]
            important_measures.append(name)
            potential_kpis.append(f"Total {name}")
            executive_metrics.append(f"Total {name}")
            log_decision(
                agent="DataAnalyzerAgent",
                decision_type="business_analysis",
                subject=name,
                rationale=f"Amount column '{name}' identified as primary KPI candidate.",
                confidence=0.85,
            )

        # Qty columns → volume KPIs
        for col in buckets.get("qty", []):
            name = col["name"]
            important_measures.append(name)
            potential_kpis.append(f"Total {name}")

        # Date columns → trend and seasonal indicators
        for col in buckets.get("date", []):
            name = col["name"]
            trend_indicators.append(name)
            name_lower = name.lower()
            if any(h in name_lower for h in ("month", "quarter", "season", "week")):
                seasonal_indicators.append(name)
            log_decision(
                agent="DataAnalyzerAgent",
                decision_type="business_analysis",
                subject=name,
                rationale=f"Date column '{name}' enables time-series trend analysis.",
                confidence=0.9,
            )

        # Category columns → categorical dominance check (high cardinality useful for drill-down)
        for col in buckets.get("category", []) + buckets.get("region", []):
            name = col["name"]
            category_dominant.append(name)

        # Executive metrics: top amount + count
        if executive_metrics:
            recommendations.append(
                f"Highlight '{executive_metrics[0]}' as the primary executive KPI on the overview page."
            )
        if buckets.get("date"):
            recommendations.append("Add time-intelligence measures (YTD, MoM) for trend analysis.")
        if buckets.get("category"):
            recommendations.append("Include a category breakdown visual for segment comparison.")
        if len(buckets.get("amount", [])) > 1:
            recommendations.append("Consider a matrix visual to compare multiple measures side-by-side.")

        return BusinessAnalysis(
            important_measures=important_measures,
            potential_kpis=potential_kpis,
            trend_indicators=trend_indicators,
            seasonal_indicators=seasonal_indicators,
            category_dominant_cols=category_dominant,
            executive_metrics=executive_metrics,
            recommendations=recommendations,
        )

    # ------------------------------------------------------------------
    # edit-mode: derive a minimal profile from the TMDL model
    # ------------------------------------------------------------------

    def _profile_from_model(self, ctx) -> AgentResult:
        """When editing an existing PBIP/PBIX, build a profile from the model."""
        schema = ctx.schema
        if not schema:
            return AgentResult(
                agent=self.name, ok=True,
                message="No schema yet — skipping model profile.",
                data={"skipped": True},
            )
        issues: list[str] = []
        all_tables = schema.get("all_tables", [])
        for t in all_tables:
            conn = t.get("connection_type", "other")
            if conn == "other" and t.get("partition_source"):
                issues.append(f"Table '{t['table_name']}' has an unrecognised data source")
            for c in t.get("columns", []):
                if c.get("isCalculated"):
                    issues.append(
                        f"Table '{t['table_name']}' column '{c['name']}' is calculated "
                        f"(BPA: avoid calculated columns on import tables)"
                    )
        profile = {
            "quality_score": 90.0 if not issues else 70.0,
            "quality": {},
            "issues": issues,
            "questions": [],
            "answers": {},
            "blocking_issues": [],
            "verified": True,
            "verify_warning": None,
            "schema": schema,
            "source": "tmdl_model",
        }
        ctx.extra["data_profile"] = profile
        self.log.info(f"model-based profile: {len(issues)} issue(s), score={profile['quality_score']}")
        return AgentResult(
            agent=self.name, ok=True,
            message=f"Model profile: {len(issues)} issue(s), score {profile['quality_score']}/100",
            data={"quality_score": profile["quality_score"], "issue_count": len(issues)},
        )

    # ------------------------------------------------------------------
    # self-verification
    # ------------------------------------------------------------------

    def _self_verify(self, source: Path, result: dict) -> str | None:
        """Re-read a small sample and cross-check null counts.

        Returns a warning string if the second read disagrees, else None.
        """
        try:
            from mcp_server.schema_inference import profile_data_file
            second = profile_data_file(source, sample_rows=200)
            q1 = result.get("quality", {}).get("columns", {})
            q2 = second.get("quality", {}).get("columns", {})
            mismatches: list[str] = []
            for name, cp1 in q1.items():
                cp2 = q2.get(name, {})
                # null_pct should be in the same ballpark (within 10pp)
                if abs(cp1.get("null_pct", 0) - cp2.get("null_pct", 0)) > 10:
                    mismatches.append(
                        f"{name}: null_pct {cp1.get('null_pct')} vs {cp2.get('null_pct')}"
                    )
            if mismatches:
                return f"verification mismatch: {'; '.join(mismatches[:3])}"
        except Exception as exc:
            return f"verification error: {exc}"
        return None

    # ------------------------------------------------------------------
    # question + answer generation
    # ------------------------------------------------------------------

    def _build_questions(self, result: dict) -> list[dict[str, Any]]:
        """Build user-facing questions for ambiguous issues."""
        questions: list[dict[str, Any]] = []
        quality = result.get("quality", {})
        cols = quality.get("columns", {})
        schema = result.get("schema", {})
        for col in schema.get("columns", []):
            name = col["name"]
            cp = cols.get(name, {})
            null_pct = cp.get("null_pct", 0)
            if null_pct > _NULL_IMPUTE_THRESHOLD and null_pct <= _NULL_DROP_THRESHOLD:
                questions.append({
                    "id": f"nulls_{name}",
                    "question": (
                        f"Column '{name}' has {null_pct}% null values. "
                        f"Should I drop the column, or impute missing values?"
                    ),
                    "options": ["drop", "impute_median", "impute_mean", "impute_mode", "keep"],
                    "default": "impute_median",
                })
            distinct = cp.get("distinct_count", 0)
            if distinct <= _LOW_CARDINALITY and null_pct < 100:
                questions.append({
                    "id": f"single_value_{name}",
                    "question": (
                        f"Column '{name}' has only {distinct} distinct value(s). "
                        f"It adds no analytical value — drop it?"
                    ),
                    "options": ["drop", "keep"],
                    "default": "drop",
                })
            outliers = cp.get("outlier_count", 0)
            if outliers > 5 and col["dataType"] in {"int64", "double", "decimal"}:
                questions.append({
                    "id": f"outliers_{name}",
                    "question": (
                        f"Column '{name}' has {outliers} outlier(s). "
                        f"Cap them, remove them, or keep them?"
                    ),
                    "options": ["keep", "cap", "remove"],
                    "default": "cap",
                })
        return questions

    def _best_effort_answers(self, questions: list[dict]) -> dict[str, str]:
        """Pick a sensible default answer for each question (non-interactive)."""
        return {q["id"]: q.get("default", "keep") for q in questions}

    def _blocking_issues(self, result: dict) -> list[str]:
        """Issues severe enough to halt the pipeline without interaction."""
        blocking: list[str] = []
        schema = result.get("schema", {})
        if not schema.get("columns"):
            blocking.append("Data file has no columns — cannot build a model.")
        quality = result.get("quality", {})
        cols = quality.get("columns", {})
        for col in schema.get("columns", []):
            cp = cols.get(col["name"], {})
            if cp.get("null_pct", 0) >= 100:
                blocking.append(f"Column '{col['name']}' is 100% null — unusable.")
        return blocking
