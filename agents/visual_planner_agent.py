"""VisualPlannerAgent -- intent-aware report page/visual planning (Phase 3).

Fixes V1-V8:
  V1: LLM prompt now lists all 10+ visual types (donut, scatter, matrix, kpi, slicer).
  V2: Schema columns + measures are passed to the LLM prompt.
  V3: Ghost reference validation — invalid visuals are pruned, NOT a full rejection.
  V4: report_style influences visual type diversity and page count in the prompt.
  V5: Layout validation note added to prompt.
  V6: max_pages param flows through so ReportAgent controls the cap; fallback
      distributes candidates across multiple pages for rich style.
  V7: BIReasoningResult consumed from bi_reasoning context — enriches LLM prompt
      with dashboard type, recommended pages, KPIs. VisualReasoning populated on
      every visual (LLM and deterministic fallback paths).
  V8: Expanded offline path from 3 → 5 arrangement candidates (executive, analytical,
      comprehensive, narrative, operational). Switched selection to tournament_select
      (multi-stage bracket ranking). business_description + schema_columns forwarded
      to score_visual_candidate for 60/40 semantic+heuristic blending.
"""
from __future__ import annotations

import json
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import PagePlan, ReportPlan, VisualPlan, VisualReasoning
from utils import AuditLogger
from utils.scoring import DEFAULT_WEIGHTS, score_visual_candidate, tournament_select

_log = AuditLogger.get("agent.visual_planner")

# Default page names for rich multi-page fallback
_RICH_PAGE_NAMES = ["Overview", "Details", "Trends"]

# Visual kinds that represent executive / KPI dashboards
_EXEC_VISUAL_KINDS: frozenset[str] = frozenset({"card", "kpi"})
# Visual kinds that represent analytical / trend charts
_ANALYTIC_VISUAL_KINDS: frozenset[str] = frozenset(
    {"barChart", "columnChart", "lineChart", "donutChart", "scatterChart"}
)
# Visual kinds that lead the narrative (time-story) arrangement
_NARRATIVE_VISUAL_KINDS: frozenset[str] = frozenset({"lineChart", "card", "kpi"})
# Visual kinds that lead the operational dense-grid arrangement
_OPERATIONAL_VISUAL_KINDS: frozenset[str] = frozenset({"matrix", "tableEx", "slicer"})

# ---------------------------------------------------------------------------
# Visual-type → task_type + heuristic reasoning (deterministic fallback)
# ---------------------------------------------------------------------------

_KIND_REASONING: dict[str, dict[str, str]] = {
    "card": {
        "task_type": "executive_kpi",
        "why_this_visual": "Card visuals surface a single KPI at a glance — ideal for executive overviews.",
        "why_this_layout": "Cards are placed in the top zone for maximum visibility.",
    },
    "kpi": {
        "task_type": "executive_kpi",
        "why_this_visual": "KPI visual shows value vs target — core executive metric format.",
        "why_this_layout": "KPIs anchor the top row of an executive page.",
    },
    "barChart": {
        "task_type": "comparison",
        "why_this_visual": "Horizontal bars enable easy comparison across categories.",
        "why_this_layout": "Bars are sized to fill the mid-section of the page.",
    },
    "columnChart": {
        "task_type": "ranking",
        "why_this_visual": "Vertical columns rank items by measure value.",
        "why_this_layout": "Column charts occupy the centre zone for prominence.",
    },
    "lineChart": {
        "task_type": "trend",
        "why_this_visual": "Line charts reveal trends and patterns over time.",
        "why_this_layout": "Line charts span full width on the trends page.",
    },
    "donutChart": {
        "task_type": "composition",
        "why_this_visual": "Donut charts show part-to-whole composition of a measure.",
        "why_this_layout": "Donut charts are compact — placed beside bar charts.",
    },
    "scatterChart": {
        "task_type": "distribution",
        "why_this_visual": "Scatter charts show distribution and correlation between measures.",
        "why_this_layout": "Scatter charts occupy the lower section of analytical pages.",
    },
    "matrix": {
        "task_type": "comparison",
        "why_this_visual": "Matrix tables compare multiple measures across dimensions simultaneously.",
        "why_this_layout": "Matrix visuals span full width for readability.",
    },
    "tableEx": {
        "task_type": "distribution",
        "why_this_visual": "Tables provide detailed row-level data for drill-down analysis.",
        "why_this_layout": "Tables anchor the bottom of the page for detailed data.",
    },
    "slicer": {
        "task_type": "composition",
        "why_this_visual": "Slicers let users filter the entire page by a dimension.",
        "why_this_layout": "Slicers are placed on the left panel or top for easy access.",
    },
}

_DEFAULT_REASONING = {
    "task_type": "comparison",
    "why_this_visual": "Visual selected to display the measure across a dimension.",
    "why_this_layout": "Placed based on available canvas zones.",
}


def _infer_visual_reasoning(
    kind: str,
    category: str | None,
    measure: str | None,
    page_name: str = "",
) -> VisualReasoning:
    """Build a VisualReasoning object using heuristics (no LLM needed)."""
    template = _KIND_REASONING.get(kind, _DEFAULT_REASONING)

    why_dims = (
        f"Dimension '{category}' chosen to slice the measure by a meaningful business segment."
        if category else "No category dimension — single-value KPI visual."
    )
    why_meas = (
        f"Measure '{measure}' selected as the primary analytical value for this visual."
        if measure else "No single measure — table/matrix shows multiple values."
    )
    why_page = (
        f"Placed on '{page_name}' page because its task type ({template['task_type']}) "
        "fits this page's analytical theme."
        if page_name else "Placed on the summary page."
    )

    return VisualReasoning(
        why_this_visual=template["why_this_visual"],
        why_this_page=why_page,
        why_these_dimensions=why_dims,
        why_these_measures=why_meas,
        why_this_layout=template["why_this_layout"],
        task_type=template["task_type"],
    )


def _build_bi_context_section(bi_reasoning: Any | None) -> str:
    """Format BIReasoningResult into a prompt section for the LLM."""
    if bi_reasoning is None:
        return ""
    lines = ["\nBI_REASONING_CONTEXT:"]
    lines.append(f"- Dashboard goal: {bi_reasoning.dashboard_goal}")
    lines.append(f"- Dashboard type: {bi_reasoning.dashboard_type}")
    lines.append(f"- Target audience: {bi_reasoning.target_audience}")
    if bi_reasoning.recommended_pages:
        pages_str = ", ".join(
            f"'{p.name}' ({p.purpose})"
            for p in bi_reasoning.recommended_pages
        )
        lines.append(f"- Recommended pages: {pages_str}")
    if bi_reasoning.recommended_kpis:
        kpis_str = ", ".join(
            f"'{k.name}'" for k in bi_reasoning.recommended_kpis[:5]
        )
        lines.append(f"- Priority KPIs: {kpis_str}")
    if bi_reasoning.analytical_perspectives:
        lines.append(
            f"- Analytical perspectives: {'; '.join(bi_reasoning.analytical_perspectives[:3])}"
        )
    return "\n".join(lines) + "\n"


def _plan_with_llm(
    candidates: list[dict[str, Any]],
    description: str,
    schema: dict[str, Any],
    measures: list[dict[str, Any]],
    report_style: str,
    max_pages: int = 1,
    bi_reasoning: Any | None = None,
) -> ReportPlan | None:
    """Ask the LLM to plan pages/visuals based on the intent + report style.

    V3 fix: instead of rejecting the whole plan on a ghost ref, we prune the
    offending visual and keep valid ones. A plan with at least 1 valid visual
    on at least 1 page is accepted.
    V7: bi_reasoning context enriches the prompt when available.
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

    table = schema.get("table_name", "Table")
    cols = [{"name": c["name"], "dataType": c["dataType"]} for c in schema.get("columns", [])]
    measure_names = [m["name"] for m in measures]

    cand_brief = [{"name": c["name"], "kind": c["kind"],
                   "measure": c.get("measure"), "category": c.get("category"),
                   "columns": c.get("columns", [])} for c in candidates]

    # V4 + V6: style_guidance includes actual max_pages
    _style_guidance: dict[str, str] = {
        "minimal": "1 page, 1-3 visuals. Only the single most relevant visual.",
        "standard": "1 page, 4-6 visuals (the default). Mix cards, charts, and a table.",
        "rich": (
            f"UP TO {max_pages} pages, up to 10 visuals per page. "
            "Distribute visuals thematically: page 1 = KPIs/overview cards, "
            "page 2 = detailed breakdowns (bar/column/donut), "
            "page 3 = trends + table. Use diverse visual types."
        ),
    }
    style_guidance = _style_guidance.get(report_style, "1 page, 4-6 visuals.")

    # V7: inject BI reasoning context
    bi_context = _build_bi_context_section(bi_reasoning)

    prompt = (
        "You are a Power BI report planner. Given the user's business "
        "description, schema, measures, and a pool of candidate visuals, plan "
        "the report pages and visuals. Output ONLY JSON matching:\n"
        "{\n"
        '  "pages": [\n'
        '    {"id": "string", "displayName": "string", "visuals": [\n'
        '      {"name": "string", "kind": "card|barChart|columnChart|lineChart|tableEx|donutChart|scatterChart|matrix|kpi|slicer", '
        '"measure": "string|null", "category": "string|null", '
        '"columns": [["table","col"],...], "intent_match_reasoning": "string"}\n'
        "    ]}\n"
        "  ]\n"
        "}\n\n"
        f"STYLE GUIDANCE: {style_guidance}\n\n"
        "Rules:\n"
        "- Every visual's measure/category/columns MUST reference real measures "
        "or schema columns (from the lists below). Never invent names.\n"
        "- If you cannot find a real measure/column, set that field to null.\n"
        "- Prefer candidate visuals that match the user's intent.\n"
        "- Fill intent_match_reasoning for every visual (one short sentence).\n"
        f"- For '{report_style}' style, use diverse visual types, not just cards.\n"
        "- Ensure visuals don't overlap (1280x720 canvas, zone-based layout).\n"
        "- Prioritise the KPIs listed in BI_REASONING_CONTEXT if provided.\n\n"
        f"BUSINESS_DESCRIPTION:\n{description}\n\n"
        f"TABLE: {table}\nCOLUMNS:\n{json.dumps(cols)}\n\n"
        f"MEASURES:\n{json.dumps(measure_names)}\n\n"
        f"CANDIDATE_VISUALS:\n{json.dumps(cand_brief)}\n"
        f"{bi_context}"
    )

    def _call_once() -> str:
        from utils.model_config import get_text_completion
        return get_text_completion(prompt, llm_config)

    try:
        text = retry_sync(_call_once, retries=2, base_delay=1.0, max_delay=8.0)
    except Exception:
        return None

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    # Build initial ReportPlan — tolerate extra/unknown visual fields
    try:
        raw_pages = []
        for p in raw.get("pages", []):
            raw_visuals = []
            for v in p.get("visuals", []):
                try:
                    coerced = _coerce_visual(v)
                    vp = VisualPlan(**coerced)
                    # Attach heuristic VisualReasoning (LLM path)
                    vp.visual_reasoning = _infer_visual_reasoning(
                        vp.kind, vp.category, vp.measure,
                        p.get("displayName", "")
                    )
                    raw_visuals.append(vp)
                except Exception:
                    pass  # skip malformed visual, keep the rest
            raw_pages.append(PagePlan(
                id=p.get("id", f"page-{len(raw_pages)+1}"),
                displayName=p.get("displayName", f"Page {len(raw_pages)+1}"),
                visuals=raw_visuals,
            ))
        rp = ReportPlan(pages=raw_pages)
    except Exception:
        return None

    # V3 fix: prune ghost refs instead of rejecting the whole plan
    col_names = {c["name"] for c in schema.get("columns", [])}
    measure_names_set = set(measure_names)
    clean_pages: list[PagePlan] = []
    for page in rp.pages:
        valid_visuals: list[VisualPlan] = []
        for v in page.visuals:
            ghost = False
            if v.measure and v.measure not in measure_names_set:
                ghost = True
            if v.category and v.category not in col_names:
                ghost = True
            for pair in v.columns:
                if len(pair) >= 2 and pair[1] not in col_names:
                    ghost = True
                    break
            if not ghost:
                valid_visuals.append(v)
        if valid_visuals:
            clean_pages.append(PagePlan(id=page.id, displayName=page.displayName,
                                        visuals=valid_visuals))

    if not clean_pages:
        return None
    return ReportPlan(pages=clean_pages)


def _fallback_reportplan(
    candidates: list[dict[str, Any]],
    report_style: str = "standard",
    max_pages: int = 1,
    bi_reasoning: Any | None = None,
) -> ReportPlan:
    """Wrap the candidate visual plans in a ReportPlan (no LLM).

    V6 fix: for rich style distribute candidates across up to max_pages pages
    (6 visuals per page) so offline runs also produce multi-page reports.
    V7: use recommended page names from bi_reasoning when available.
    """
    # Determine page names from bi_reasoning if available
    page_names = _RICH_PAGE_NAMES[:]
    if bi_reasoning is not None and bi_reasoning.recommended_pages:
        page_names = [p.name for p in bi_reasoning.recommended_pages] + _RICH_PAGE_NAMES

    all_visuals: list[VisualPlan] = []
    for c in candidates:
        c2 = dict(c)
        c2.setdefault("intent_match_reasoning", "deterministic candidate visual")
        cols = c2.get("columns") or []
        c2["columns"] = [list(col) for col in cols] if cols else []
        vp = VisualPlan(**_coerce_visual(c2))
        # V7: attach heuristic VisualReasoning
        vp.visual_reasoning = _infer_visual_reasoning(vp.kind, vp.category, vp.measure)
        all_visuals.append(vp)

    if report_style != "rich" or max_pages <= 1:
        return ReportPlan(pages=[
            PagePlan(id="summary-page", displayName="Summary", visuals=all_visuals),
        ])

    # Rich style: split into pages of 6 visuals each
    visuals_per_page = 6
    pages: list[PagePlan] = []
    for i in range(0, len(all_visuals), visuals_per_page):
        chunk = all_visuals[i:i + visuals_per_page]
        if not chunk:
            break
        page_num = len(pages)
        if page_num >= max_pages:
            break
        # Update VisualReasoning with page name now that we know it
        pname = page_names[page_num] if page_num < len(page_names) else f"Page {page_num + 1}"
        for vp in chunk:
            if vp.visual_reasoning is not None:
                vp.visual_reasoning.why_this_page = (
                    f"Placed on '{pname}' page based on visual type '{vp.kind}' "
                    "fitting this page's analytical theme."
                )
        page_id = pname.lower().replace(" ", "-")
        pages.append(PagePlan(id=page_id, displayName=pname, visuals=chunk))

    if not pages:
        pages = [PagePlan(id="summary-page", displayName="Summary", visuals=all_visuals)]
    return ReportPlan(pages=pages)


def _coerce_visual(d: dict[str, Any]) -> dict[str, Any]:
    """Ensure a candidate dict has the keys VisualPlan requires."""
    return {
        "name": d["name"],
        "kind": d["kind"],
        "measure": d.get("measure"),
        "category": d.get("category"),
        "columns": d.get("columns", []),
        "intent_match_reasoning": d.get("intent_match_reasoning", ""),
        # visual_reasoning is not included here — set separately after construction
    }


class VisualPlannerAgent(BaseAgent):
    """Plans report pages/visuals based on user intent + report style.

    ``context`` is optional: when omitted, only :meth:`plan` is usable
    (the public API for direct callers and unit tests). When a context is
    supplied, :meth:`run` / :meth:`_run` are also available and participate
    in full pipeline tracing (event bus, audit log, sync_to_state).
    """

    name = "VisualPlannerAgent"
    description = (
        "Plan the report pages and visual mix based on the user's business "
        "intent and the report style from the planner. Validate every visual "
        "references real measures/columns (prune ghost refs, don't reject the plan). "
        "Fall back to the candidate plan when no LLM is available; for rich style "
        "distribute candidates across multiple pages. Consume BIReasoningResult "
        "when available to align pages with business intent."
    )

    def __init__(self, context=None) -> None:  # type: ignore[override]
        if context is not None:
            super().__init__(context)
        else:
            # Context-free mode: only plan() is callable; run()/_run() must not
            # be called. We still set up the audit logger so plan() can log.
            from utils import AuditLogger
            self.context = None  # type: ignore[assignment]
            self.log = AuditLogger.get(f"agent.{self.name.lower()}")

    def _run(self) -> AgentResult:
        """Execute visual planning from AgentContext.

        Reads ``ctx.extra["visual_candidates"]`` and planning parameters set by
        ReportAgent, calls :meth:`plan`, and stores the result in
        ``ctx.extra["report_plan"]``.
        """
        ctx = self.context
        candidates: list[dict[str, Any]] = ctx.extra.get("visual_candidates", [])  # type: ignore[assignment]
        report_style: str = ctx.extra.get("report_style", "standard")  # type: ignore[assignment]
        max_pages: int = ctx.extra.get("visual_plan_max_pages", 1)  # type: ignore[assignment]
        bi_reasoning: Any | None = ctx.extra.get("bi_reasoning")

        scoring_weights = dict(ctx.extra.get("scoring_weights") or DEFAULT_WEIGHTS)  # type: ignore[arg-type]
        _adaptive_bias: float = float(ctx.extra.get("adaptive_bias", 0.0))
        _candidate_count: int = int(ctx.extra.get("candidate_count", 5))
        _synth_specs: list[dict[str, Any]] = list(
            ctx.extra.get("synthesized_strategies", {}).get("visual") or []
        )
        rp = self.plan(
            candidates, ctx.business_description, ctx.schema or {},
            ctx.measures, report_style, max_pages=max_pages,
            bi_reasoning=bi_reasoning, weights=scoring_weights,
            adaptive_bias=_adaptive_bias, candidate_count=_candidate_count,
            synthesized_specs=_synth_specs,
        )
        ctx.extra["report_plan"] = rp
        ctx.extra["visual_plan_candidates"] = getattr(self, "_last_candidate_scores", [])  # type: ignore[assignment]

        total_visuals = sum(len(p.visuals) for p in rp.pages)
        self.log.info(
            f"planned {total_visuals} visual(s) on {len(rp.pages)} page(s); "
            f"style={report_style}, max_pages={max_pages}"
        )
        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Planned {total_visuals} visual(s) on "
                f"{len(rp.pages)} page(s) [{report_style}]."
            ),
            data={
                "page_count": len(rp.pages),
                "visual_count": total_visuals,
                "pages": [
                    {"id": p.id, "displayName": p.displayName,
                     "visual_count": len(p.visuals)}
                    for p in rp.pages
                ],
            },
        )

    def plan(
        self,
        candidates: list[dict[str, Any]],
        description: str,
        schema: dict[str, Any],
        measures: list[dict[str, Any]],
        report_style: str = "standard",
        max_pages: int = 1,
        bi_reasoning: Any | None = None,
        weights: dict[str, float] | None = None,
        adaptive_bias: float = 0.0,
        candidate_count: int = 5,
        synthesized_specs: list[dict[str, Any]] | None = None,
    ) -> ReportPlan:
        """Return a ReportPlan of pages/visuals with intent reasoning.

        When the LLM is available, delegates to ``_plan_with_llm``.  In the
        offline (no API key) path, generates **five arrangement candidates**
        (executive, analytical, comprehensive, narrative, operational), scores
        each with the global semantic+heuristic utility model (60/40 blend) via
        ``tournament_select``, and returns the tournament winner.

        Candidate strategies
        --------------------
        executive    : KPI/card visuals first — optimised for C-suite audiences.
        analytical   : Chart visuals first — optimised for analysts.
        comprehensive: Original pool order — balanced baseline.
        narrative    : Time-story layout (lineChart → card → bar → table) —
                       optimised for finance/ops storytelling with temporal flow.
        operational  : Dense indicator grid (matrix/table/slicer first) —
                       optimised for ops teams that need row-level drill-down.

        Args:
            bi_reasoning: Optional ``BIReasoningResult`` from ``BIReasoningAgent``.
                          When provided, the LLM prompt is enriched with dashboard
                          type, recommended page names, and priority KPIs.
            weights:      Scoring weights from ``ctx.extra["scoring_weights"]``.
                          Defaults to ``DEFAULT_WEIGHTS`` when not provided.
        """
        if not candidates:
            return ReportPlan(pages=[])

        rp = _plan_with_llm(candidates, description, schema, measures,
                             report_style, max_pages=max_pages,
                             bi_reasoning=bi_reasoning)
        if rp is not None:
            return rp

        # --- Offline path: multi-arrangement candidate scoring (V8) ------
        # Five meaningfully-different arrangements of the same candidate pool,
        # scored by the global semantic+heuristic utility model (60/40 blend).
        # Total visual count is identical for all five — only the ordering
        # (and therefore page composition and KPI prominence) differs.
        #
        # executive    — KPI/card visuals surface first; optimised for C-suite.
        # analytical   — Chart visuals first; optimised for analysts.
        # comprehensive— Original pool order; balanced baseline.
        # narrative    — Time-story layout: lineChart → card/kpi → bar → table.
        #                Leads with trends, adds context, then detail.
        # operational  — Dense indicator grid: matrix/table/slicer first.
        #                Optimised for ops teams that need row-level drill-down.
        cands_exec = sorted(
            candidates,
            key=lambda c: (0 if c.get("kind") in _EXEC_VISUAL_KINDS else 1),
        )
        cands_analytic = sorted(
            candidates,
            key=lambda c: (0 if c.get("kind") in _ANALYTIC_VISUAL_KINDS else 1),
        )
        cands_comp = list(candidates)  # unchanged — balanced baseline

        # narrative: lineChart > card/kpi > everything else (score 0/1/2)
        def _narrative_key(c: dict) -> int:
            k = c.get("kind", "")
            if k == "lineChart":
                return 0
            if k in _EXEC_VISUAL_KINDS:
                return 1
            return 2

        cands_narrative = sorted(candidates, key=_narrative_key)

        # operational: matrix/tableEx/slicer > kpi/card > charts (score 0/1/2)
        def _operational_key(c: dict) -> int:
            k = c.get("kind", "")
            if k in _OPERATIONAL_VISUAL_KINDS:
                return 0
            if k in _EXEC_VISUAL_KINDS:
                return 1
            return 2

        cands_operational = sorted(candidates, key=_operational_key)

        plan_exec        = _fallback_reportplan(cands_exec,        report_style, max_pages, bi_reasoning)
        plan_analytic    = _fallback_reportplan(cands_analytic,    report_style, max_pages, bi_reasoning)
        plan_comp        = _fallback_reportplan(cands_comp,        report_style, max_pages, bi_reasoning)
        plan_narrative   = _fallback_reportplan(cands_narrative,   report_style, max_pages, bi_reasoning)
        plan_operational = _fallback_reportplan(cands_operational, report_style, max_pages, bi_reasoning)

        all_plans = [plan_exec, plan_analytic, plan_comp, plan_narrative, plan_operational]
        all_plan_ids = ["executive", "analytical", "comprehensive", "narrative", "operational"]

        # Expanded arrangements for medium/high-complexity inputs
        if candidate_count > 5:
            # kpi_grid: KPI/card first, then prefer visuals with a category
            # dimension — creates a multi-KPI grid that maximises semantic
            # kpi_alignment scores on high-KPI-density inputs.
            cands_kpi_grid = sorted(
                candidates,
                key=lambda c: (
                    0 if c.get("kind") in _EXEC_VISUAL_KINDS else 1,
                    0 if c.get("category") else 1,
                ),
            )
            all_plans.append(_fallback_reportplan(cands_kpi_grid, report_style, max_pages, bi_reasoning))
            all_plan_ids.append("kpi_grid")

        if candidate_count > 6:
            # mixed_density: alternate executive and analytical visuals so each
            # page has both KPI cards and detailed charts — balances breadth and depth.
            exec_visuals  = [c for c in candidates if c.get("kind") in _EXEC_VISUAL_KINDS]
            other_visuals = [c for c in candidates if c.get("kind") not in _EXEC_VISUAL_KINDS]
            cands_mixed: list[dict[str, Any]] = []
            for pair in zip(exec_visuals, other_visuals):
                cands_mixed.extend(pair)
            # append any remainder
            cands_mixed.extend(
                c for c in candidates if c not in cands_mixed
            )
            all_plans.append(_fallback_reportplan(cands_mixed, report_style, max_pages, bi_reasoning))
            all_plan_ids.append("mixed_density")

        # Strategy Synthesis Layer (additive) — inject any visual arrangement
        # strategies the orchestrator synthesized from failure/judge signals
        # (empty by default, so behaviour is byte-identical when no gap was
        # detected).
        for _spec in (synthesized_specs or []):
            try:
                from utils.strategy_synthesizer import apply_visual_strategy_spec
                synth_cands = apply_visual_strategy_spec(_spec, candidates)
                if synth_cands:
                    all_plans.append(
                        _fallback_reportplan(synth_cands, report_style, max_pages, bi_reasoning)
                    )
                    all_plan_ids.append(_spec["strategy_id"])
            except Exception as _synth_exc:  # noqa: BLE001 — fail-safe
                self.log.warning(
                    f"synthesized visual strategy '{_spec.get('strategy_id')}' skipped: {_synth_exc}"
                )

        w = weights or DEFAULT_WEIGHTS
        schema_cols: list[dict] = list(schema.get("columns", []))
        vis_scores = [
            score_visual_candidate(
                pid, plan, measures, bi_reasoning, w,
                business_description=description,
                schema_columns=schema_cols,
                adaptive_bias=adaptive_bias,
            )
            for pid, plan in zip(all_plan_ids, all_plans)
        ]

        _kpi_score_map_v: dict[str, float] = {
            s.candidate_id: s.semantic.kpi_semantic_alignment
            for s in vis_scores
        }
        best_v_idx, best_v_score, rejected_v = tournament_select(
            all_plans, vis_scores,
            context_aware=(candidate_count > 5),
            kpi_scores=_kpi_score_map_v,
        )

        # Persist scores on self so _run() can store them in ctx.extra without
        # needing ctx access inside plan().
        self._last_candidate_scores = [s.as_dict() for s in vis_scores]  # type: ignore[attr-defined]
        return all_plans[best_v_idx]


__all__ = ["VisualPlannerAgent"]
