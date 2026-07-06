"""BIReasoningAgent -- business intelligence reasoning before visual planning.

Role
----
Runs AFTER ``PlannerAgent`` and BEFORE ``DataAnalyzerAgent``. Its job is to
answer *business questions*, not generate reports:

  * What is the user actually trying to analyse?
  * What KPIs are most valuable for their domain?
  * Which analytical perspectives should the dashboard include?
  * Which pages should exist and what should each highlight?
  * Who is the target audience?

It outputs a :class:`BIReasoningResult` that flows into ``VisualPlannerAgent``
so visuals are chosen based on business intent, not just schema shape.

Design
------
* **LLM path** (when ``GOOGLE_API_KEY`` is set): asks Gemini to reason about
  the business description + schema and return a structured JSON matching
  ``BIReasoningResult``.
* **Deterministic fallback** (no key, or any LLM failure): infers domain and
  KPIs from column name hints — same pattern as ``DAXAgent._classify_columns``.
  The fallback always produces a valid ``BIReasoningResult`` so downstream
  agents are never blocked.
* **Fail-safe**: any error returns a minimal valid result with ``ok=True`` so
  the pipeline continues.  The reasoning is advisory — it never blocks.
"""
from __future__ import annotations

import json
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import (
    BIReasoningResult,
    KPIRecommendation,
    PageRecommendation,
)
from utils import AuditLogger
from utils.explainability import log_decision

_log = AuditLogger.get("agent.bi_reasoning")

# ---------------------------------------------------------------------------
# Domain keyword tables (used by the deterministic fallback)
# ---------------------------------------------------------------------------

_FINANCE_HINTS = ("revenue", "sales", "profit", "cost", "income", "spend",
                  "budget", "margin", "price", "discount", "fee", "charge")
_HR_HINTS = ("employee", "staff", "salary", "headcount", "department",
             "hire", "attrition", "performance", "leave", "absence")
_MARKETING_HINTS = ("campaign", "conversion", "lead", "click", "impression",
                    "ctr", "roas", "channel", "acquisition", "funnel")
_LOGISTICS_HINTS = ("shipment", "delivery", "order", "inventory", "stock",
                    "warehouse", "supplier", "freight", "transit", "route")
_BANKING_HINTS = ("loan", "deposit", "account", "transaction", "balance",
                  "credit", "debit", "interest", "euribor", "default", "bank")
_RETAIL_HINTS = ("product", "category", "sku", "store", "basket",
                 "promotion", "shelf", "replenishment", "units")

_AUDIENCE_EXECUTIVE = ("executive", "ceo", "cfo", "board", "summary",
                       "overview", "kpi", "scorecard")
_AUDIENCE_ANALYST = ("analyst", "detail", "breakdown", "drill", "explore",
                     "segment", "cohort", "trend")

_DASHBOARD_TYPES = {
    "executive": "executive",
    "operational": "operational",
    "analytical": "analytical",
    "storytelling": "storytelling",
}


def _infer_domain(description: str, schema: dict[str, Any]) -> str:
    """Guess the business domain from description + column names."""
    text = description.lower()
    col_names = " ".join(c["name"].lower() for c in schema.get("columns", []))
    combined = text + " " + col_names

    scores: dict[str, int] = {
        "finance": sum(1 for h in _FINANCE_HINTS if h in combined),
        "hr": sum(1 for h in _HR_HINTS if h in combined),
        "marketing": sum(1 for h in _MARKETING_HINTS if h in combined),
        "logistics": sum(1 for h in _LOGISTICS_HINTS if h in combined),
        "banking": sum(1 for h in _BANKING_HINTS if h in combined),
        "retail": sum(1 for h in _RETAIL_HINTS if h in combined),
    }
    return max(scores, key=lambda k: scores[k]) if max(scores.values()) > 0 else "general"


def _infer_audience(description: str) -> str:
    """Infer target audience from description keywords."""
    desc_lower = description.lower()
    if any(h in desc_lower for h in _AUDIENCE_EXECUTIVE):
        return "executive"
    if any(h in desc_lower for h in _AUDIENCE_ANALYST):
        return "analyst"
    return "analyst"  # safe default


def _infer_dashboard_type(description: str, audience: str) -> str:
    """Infer dashboard type from description + audience."""
    desc_lower = description.lower()
    if audience == "executive" or any(k in desc_lower for k in ("kpi", "scorecard", "overview")):
        return "executive"
    if any(k in desc_lower for k in ("operation", "daily", "real-time", "monitor")):
        return "operational"
    if any(k in desc_lower for k in ("story", "narrative", "presentation", "insight")):
        return "storytelling"
    return "analytical"  # safe default


def _deterministic_reasoning(
    description: str,
    schema: dict[str, Any],
    report_style: str,
    clarifications: dict[str, str],
) -> BIReasoningResult:
    """Rule-based fallback — always produces a valid BIReasoningResult."""
    from agents.dax_agent import _classify_columns  # reuse existing classification

    columns = schema.get("columns", []) if schema else []
    table = schema.get("table_name", "Table") if schema else "Table"
    buckets = _classify_columns(columns)

    domain = _infer_domain(description, schema or {})
    # Clarifications override inference
    audience = clarifications.get("audience") or _infer_audience(description)
    dashboard_type = clarifications.get("dashboard_type") or _infer_dashboard_type(description, audience)

    # Build KPI recommendations from amount + qty columns
    kpis: list[KPIRecommendation] = []
    for col in buckets.get("amount", [])[:3]:
        name = col["name"]
        kpis.append(KPIRecommendation(
            name=f"Total {name}",
            why=f"'{name}' is a monetary/amount column — a primary business KPI.",
            measure_hint=f"SUM('{table}'[{name}])",
            priority=1,
        ))
        log_decision(
            agent="BIReasoningAgent",
            decision_type="kpi_recommendation",
            subject=f"Total {name}",
            rationale=f"Amount column '{name}' → primary revenue KPI (domain: {domain}).",
            confidence=0.85,
        )
    for col in buckets.get("qty", [])[:2]:
        name = col["name"]
        kpis.append(KPIRecommendation(
            name=f"Total {name}",
            why=f"'{name}' is a quantity column — useful for volume tracking.",
            measure_hint=f"SUM('{table}'[{name}])",
            priority=2,
        ))

    # Build page recommendations based on available column types and style
    pages: list[PageRecommendation] = []
    max_pages = {"minimal": 1, "standard": 1, "rich": 3}.get(report_style, 1)
    clarified_pages = clarifications.get("num_pages")
    if clarified_pages == "2-3":
        max_pages = min(3, max(max_pages, 2))
    elif clarified_pages == "as_many":
        max_pages = 3

    pages.append(PageRecommendation(
        id="overview", name="Overview",
        purpose="High-level KPIs and executive summary.",
        priority=1,
    ))
    log_decision(
        agent="BIReasoningAgent",
        decision_type="page_creation",
        subject="Overview",
        rationale="Every dashboard needs a high-level summary page.",
        confidence=1.0,
    )
    if max_pages >= 2 and (buckets.get("category") or buckets.get("region")):
        pages.append(PageRecommendation(
            id="breakdown", name="Breakdown",
            purpose="Detailed breakdowns by category, region, or segment.",
            priority=2,
        ))
        log_decision(
            agent="BIReasoningAgent",
            decision_type="page_creation",
            subject="Breakdown",
            rationale="Category/region columns present — a breakdown page adds depth.",
            confidence=0.8,
        )
    if max_pages >= 3 and buckets.get("date"):
        pages.append(PageRecommendation(
            id="trends", name="Trends",
            purpose="Time-series trends and seasonal patterns.",
            priority=3,
        ))
        log_decision(
            agent="BIReasoningAgent",
            decision_type="page_creation",
            subject="Trends",
            rationale="Date column present — a trends page enables temporal analysis.",
            confidence=0.8,
        )

    # Analytical perspectives
    perspectives: list[str] = []
    if buckets.get("amount"):
        perspectives.append("Revenue and financial performance")
    if buckets.get("date"):
        perspectives.append("Time-series trend analysis")
    if buckets.get("region"):
        perspectives.append("Geographic distribution")
    if buckets.get("category"):
        perspectives.append("Category and segment breakdown")
    if not perspectives:
        perspectives = ["General data exploration"]

    # Suggested analysis
    suggested: list[str] = []
    if kpis:
        suggested.append(f"Track {kpis[0].name} as the primary executive metric.")
    if buckets.get("date"):
        suggested.append("Add time-intelligence comparisons (YTD, MoM, YoY).")
    if buckets.get("region"):
        suggested.append("Compare performance across geographic regions.")
    if buckets.get("category"):
        suggested.append("Identify top-performing categories and outliers.")

    goal = f"Build a {dashboard_type} dashboard for {audience}s analysing {domain} data."
    reasoning = (
        f"Domain detected: {domain}. Audience: {audience}. "
        f"Dashboard type: {dashboard_type}. "
        f"Found {len(buckets.get('amount', []))} amount column(s), "
        f"{len(buckets.get('date', []))} date column(s), "
        f"{len(buckets.get('category', []))} category column(s). "
        f"Recommended {len(pages)} page(s) and {len(kpis)} KPI(s)."
    )

    return BIReasoningResult(
        dashboard_goal=goal,
        target_audience=audience,
        dashboard_type=dashboard_type,
        recommended_pages=pages,
        recommended_kpis=kpis,
        suggested_analysis=suggested,
        analytical_perspectives=perspectives,
        reasoning=reasoning,
        confidence=0.75,
        source="deterministic",
    )


def _llm_reasoning(
    description: str,
    schema: dict[str, Any],
    report_style: str,
    clarifications: dict[str, str],
) -> BIReasoningResult | None:
    """Ask the LLM to reason about business intent and return a BIReasoningResult.

    Returns ``None`` on any failure so the caller falls back to deterministic.
    """
    try:
        from utils.model_config import MissingAPIKeyError, get_llm_config
        from utils.retry import retry_sync
    except Exception:
        return None

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError as exc:
        _log.error(f"LLM provider misconfigured, falling back to deterministic: {exc}")
        return None
    if llm_config is None:
        return None

    table = schema.get("table_name", "Table") if schema else "Table"
    cols = [{"name": c["name"], "dataType": c["dataType"]}
            for c in (schema.get("columns", []) if schema else [])[:25]]

    clarif_text = ""
    if clarifications:
        clarif_text = f"\nUSER_CLARIFICATIONS:\n{json.dumps(clarifications)}\n"

    prompt = (
        "You are an expert Business Intelligence consultant. Given the user's "
        "business description, dataset schema, and any clarifications, reason "
        "about the business intent and produce a structured plan.\n\n"
        "Output ONLY valid JSON matching this schema (no prose, no code fences):\n"
        "{\n"
        '  "dashboard_goal": "string",\n'
        '  "target_audience": "executive|analyst|operational",\n'
        '  "dashboard_type": "executive|operational|analytical|storytelling",\n'
        '  "recommended_pages": [\n'
        '    {"id": "string", "name": "string", "purpose": "string", "priority": 1}\n'
        "  ],\n"
        '  "recommended_kpis": [\n'
        '    {"name": "string", "why": "string", "measure_hint": "string", "priority": 1}\n'
        "  ],\n"
        '  "suggested_analysis": ["string"],\n'
        '  "analytical_perspectives": ["string"],\n'
        '  "reasoning": "string",\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        "Rules:\n"
        "- dashboard_goal: one clear sentence describing the business purpose.\n"
        "- recommended_pages: 1-3 pages max, ordered by priority (1=highest).\n"
        "- recommended_kpis: 3-7 KPIs directly relevant to the business goal.\n"
        "- measure_hint: a partial DAX expression. Use whatever shape actually "
        "fits the data — a sum for a monetary amount (e.g. 'SUM(Table[Column])'), "
        "but a count/rate/ratio for non-financial domains that have no amount "
        "column at all (e.g. 'DIVIDE(COUNTROWS(FILTER(Table, Table[Outcome]=\"Yes\")), "
        "COUNTROWS(Table))' for a conversion/response rate, or 'COUNTROWS(Table)' "
        "for a plain count). Do not force a SUM onto a categorical outcome column.\n"
        "  Only reference columns that EXIST in the schema below.\n"
        "- analytical_perspectives: 2-5 distinct analytical angles to cover.\n"
        "- confidence: your confidence in this plan (0.0–1.0).\n"
        f"- Report style is '{report_style}' "
        f"(minimal=1 page, standard=1 page, rich=up to 3 pages).\n\n"
        f"BUSINESS_DESCRIPTION:\n{description}\n\n"
        f"TABLE: {table}\nSCHEMA_COLUMNS:\n{json.dumps(cols)}\n"
        f"{clarif_text}"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config)

    try:
        text = retry_sync(_call_once, retries=2, base_delay=1.0, max_delay=8.0)
    except Exception as exc:
        _log.warning(f"LLM call failed after retries, falling back to deterministic: {exc}")
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        _log.warning(
            "LLM response contained no JSON object, falling back to deterministic "
            f"(response preview: {text[:200]!r})"
        )
        return None

    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        _log.warning(f"LLM response failed JSON parsing, falling back to deterministic: {exc}")
        return None

    try:
        result = BIReasoningResult(
            dashboard_goal=str(raw.get("dashboard_goal", "")),
            target_audience=str(raw.get("target_audience", "analyst")),
            dashboard_type=str(raw.get("dashboard_type", "analytical")),
            recommended_pages=[
                PageRecommendation(
                    id=str(p.get("id", f"page-{i+1}")),
                    name=str(p.get("name", f"Page {i+1}")),
                    purpose=str(p.get("purpose", "")),
                    priority=int(p.get("priority", i + 1)),
                )
                for i, p in enumerate(raw.get("recommended_pages", []))
            ],
            recommended_kpis=[
                KPIRecommendation(
                    name=str(k.get("name", "")),
                    why=str(k.get("why", "")),
                    measure_hint=str(k.get("measure_hint", "")),
                    priority=int(k.get("priority", i + 1)),
                )
                for i, k in enumerate(raw.get("recommended_kpis", []))
                if k.get("name")
            ],
            suggested_analysis=[str(s) for s in raw.get("suggested_analysis", [])],
            analytical_perspectives=[str(p) for p in raw.get("analytical_perspectives", [])],
            reasoning=str(raw.get("reasoning", "")),
            confidence=float(raw.get("confidence", 0.8)),
            source="llm",
        )
    except Exception as exc:
        _log.warning(f"LLM response failed schema validation, falling back to deterministic: {exc}")
        return None

    # Validate: must have at least 1 page and 1 KPI
    if not result.recommended_pages or not result.recommended_kpis:
        _log.warning(
            "LLM response had valid JSON/schema but 0 pages or 0 KPIs, falling back to "
            f"deterministic (pages={len(result.recommended_pages)}, "
            f"kpis={len(result.recommended_kpis)}) — if this domain has no monetary "
            "amount column, the LLM may need the broadened measure_hint guidance to "
            "recommend a rate/count-based KPI instead of giving up."
        )
        return None

    # Log LLM decisions
    for kpi in result.recommended_kpis:
        log_decision(
            agent="BIReasoningAgent",
            decision_type="kpi_recommendation",
            subject=kpi.name,
            rationale=kpi.why,
            confidence=result.confidence,
            extra={"source": "llm"},
        )
    for page in result.recommended_pages:
        log_decision(
            agent="BIReasoningAgent",
            decision_type="page_creation",
            subject=page.name,
            rationale=page.purpose,
            confidence=result.confidence,
            extra={"source": "llm"},
        )

    return result


class BIReasoningAgent(BaseAgent):
    """Analyses business intent and produces a structured reasoning plan.

    Runs between ``PlannerAgent`` and ``DataAnalyzerAgent``. Its output is
    stored in ``ctx.extra["bi_reasoning"]`` and consumed by
    ``VisualPlannerAgent`` to make smarter page/visual decisions.

    This agent is **advisory only** — it never blocks the pipeline.
    """

    name = "BIReasoningAgent"
    description = (
        "You are the BIReasoningAgent. Analyse the user's business description "
        "and dataset schema to understand the business intent BEFORE any report "
        "is generated. Identify the target audience, dashboard type, recommended "
        "pages, most valuable KPIs, and key analytical perspectives. Produce a "
        "structured reasoning plan. This is business analysis, not visual planning. "
        "Fall back to deterministic heuristics when no LLM is available."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        schema = ctx.schema  # may be None at this point (before SchemaAgent)
        # Use data_profile schema if ctx.schema not yet set
        if schema is None:
            profile = ctx.extra.get("data_profile") or {}
            schema = profile.get("schema")

        report_style = ctx.extra.get("report_style", "standard")
        clarifications: dict[str, str] = ctx.extra.get("clarifications", {})

        # Try LLM first, fall back to deterministic
        result = _llm_reasoning(
            ctx.business_description, schema or {}, report_style, clarifications
        )
        source = "llm" if result is not None else "deterministic"
        if result is None:
            result = _deterministic_reasoning(
                ctx.business_description, schema or {}, report_style, clarifications
            )

        ctx.extra["bi_reasoning"] = result

        self.log.info(
            f"bi_reasoning: source={source}, audience={result.target_audience}, "
            f"type={result.dashboard_type}, pages={len(result.recommended_pages)}, "
            f"kpis={len(result.recommended_kpis)}, confidence={result.confidence:.2f}"
        )

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"BI reasoning complete ({source}): {result.dashboard_type} dashboard "
                f"for {result.target_audience}s — "
                f"{len(result.recommended_pages)} page(s), "
                f"{len(result.recommended_kpis)} KPI(s)."
            ),
            data={
                "dashboard_goal": result.dashboard_goal,
                "dashboard_type": result.dashboard_type,
                "target_audience": result.target_audience,
                "page_count": len(result.recommended_pages),
                "kpi_count": len(result.recommended_kpis),
                "confidence": result.confidence,
                "source": source,
            },
        )
