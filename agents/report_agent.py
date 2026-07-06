"""ReportAgent -- builds the PBIR report (pages, visuals, report.json).

Role
----
Turn the schema + DAX measures into a usable Power BI report using the
Enhanced Report Format (PBIR):

* one summary page with 4-6 visuals (cards, bar/column/line charts, a table),
* each visual references real columns/measures from the model,
* page.json (PBIR 2.0.0) + per-visual visual.json (PBIR 2.7.0) are written,
* the report.json (PBIR 3.0.0) root is written.

All JSON payloads are produced by :mod:`mcp_server.pbir_generator`, which is
derived from real working sample files so they validate against Power BI
Desktop's published schemas.

MCP tools used: ``write_theme_json`` (page + visuals are written directly
because their structure is fully specified by the templates).
"""

from __future__ import annotations

from typing import Any

import uuid as _uuid_mod

from agents.base import AgentResult, BaseAgent
from agents.dax_agent import _classify_columns
from utils.explainability import log_decision
from mcp_server import pbir_generator as pb
from utils import atomic_write_json, ensure_dir, safe_join
from utils import pbip_paths as paths
from utils.layout_engine import PAGE_W, PAGE_H, compute_page_layout, split_to_pages


class ReportAgent(BaseAgent):
    """Creates the PBIR report structure: page, visuals, report.json, theme."""

    name = "ReportAgent"
    description = (
        "You are the ReportAgent. Given a data model and a set of DAX measures, "
        "design a one-page summary report with 4-6 visuals (cards, bar/column/"
        "line charts, a table). Every visual must reference real columns or "
        "measures. Lay visuals out on a non-overlapping grid. Also write the "
        "report.json root and the theme.json."
    )

    # Max visuals per page for each report style.
    _STYLE_MAX_VISUALS: dict[str, int] = {"minimal": 3, "standard": 6, "rich": 10}
    # Max pages to generate per report style.
    _STYLE_MAX_PAGES: dict[str, int] = {"minimal": 1, "standard": 1, "rich": 3}

    # Keywords in business_description that upgrade to rich style.
    _RICH_KEYWORDS = (
        "comprehensive", "all visuals", "3 page", "3pages", "more than",
        "multiple page", "multi page", "multi-page", "all chart", "every visual",
        "full dashboard", "detailed", "complete", "in-depth", "breakdown",
        "regional breakdown", "trend", "kpi", "regional", "20 visual",
        "diverse", "variety",
    )
    # Keywords in business_description that downgrade to minimal style.
    _MINIMAL_KEYWORDS = (
        "quick", "simple", "basic", "single", "one page", "overview only",
        "just", "only", "minimal",
    )

    @classmethod
    def _infer_style_from_description(cls, description: str, planner_style: str) -> str:
        """Upgrade/downgrade report_style from business_description keywords.

        The PlannerAgent runs BEFORE DataAnalyzer (no schema yet), so it may
        under-estimate the intended richness. ReportAgent re-reads the raw
        description and can override upward to 'rich' when clear signals are
        present, or downward to 'minimal' for simple requests.
        This is a one-way safety net: if the planner already said 'rich', we
        never downgrade; if it said 'standard' and the description screams
        'comprehensive + 3 pages', we upgrade.
        """
        if planner_style == "rich":
            return "rich"          # planner already decided rich — trust it
        desc_lower = description.lower()
        # Check for explicit page/visual count numbers (e.g. "3 pages", "20 visuals")
        import re as _re
        page_match = _re.search(r"\b([2-9]|[1-9]\d+)\s*page", desc_lower)
        visual_match = _re.search(r"\b(1[0-9]|[2-9]\d+)\s*visual", desc_lower)
        has_rich_keyword = any(kw in desc_lower for kw in cls._RICH_KEYWORDS)
        if page_match or visual_match or has_rich_keyword:
            return "rich"
        if planner_style == "minimal":
            return "minimal"       # planner said minimal — trust it
        # Check minimal hints only for standard→minimal downgrade
        has_minimal_keyword = any(kw in desc_lower for kw in cls._MINIMAL_KEYWORDS)
        if has_minimal_keyword:
            return "minimal"
        return planner_style       # keep planner's decision

    def _run(self) -> AgentResult:
        ctx = self.context
        if not ctx.schema:
            return AgentResult(
                agent=self.name, ok=False,
                message="No schema available; run SchemaAgent first.",
                errors=["ctx.schema is None"],
            )

        table = ctx.schema["table_name"]
        buckets = _classify_columns(ctx.schema["columns"])

        # Resolve effective style: planner's intent + description keyword override
        planner_style = ctx.extra.get("report_style", "standard")
        report_style = self._infer_style_from_description(
            ctx.business_description, planner_style
        )
        if report_style != planner_style:
            self.log.info(
                f"report_style overridden by description keywords: "
                f"{planner_style!r} → {report_style!r}"
            )

        # Explicit page-count override (e.g. generate_pbip(num_pages=N)):
        # decide the exact page count up front instead of inferring a style
        # from keywords, building, and reactively adding/deleting pages when
        # the inferred count turns out wrong. Forcing "rich" here unlocks
        # the uncapped candidate pool (see the `if report_style != "rich"`
        # truncation below) so there's enough material to actually fill the
        # requested page count — split_to_pages() already guarantees the
        # final page count never exceeds max_pages, so this can't overshoot.
        requested_pages = ctx.extra.get("requested_num_pages")
        if requested_pages:
            report_style = "rich"

        log_decision(
            agent=self.name,
            decision_type="visual_selection",
            subject="report_style",
            rationale=(
                f"Resolved report_style='{report_style}' "
                f"(planner suggested '{planner_style}'; "
                f"description keyword scan "
                + ("overrode it." if report_style != planner_style else "agreed.")
                + (f"; explicit num_pages={requested_pages} override forced 'rich'."
                   if requested_pages else "")
                + f") Max visuals/page={self._STYLE_MAX_VISUALS.get(report_style, 6)}, "
                f"max_pages={requested_pages or self._STYLE_MAX_PAGES.get(report_style, 1)}."
            ),
            alternatives=[s for s in ("minimal", "standard", "rich") if s != report_style],
            confidence=0.9,
            extra={"planner_style": planner_style, "resolved_style": report_style,
                   "requested_num_pages": requested_pages},
        )

        max_visuals_per_page = self._STYLE_MAX_VISUALS.get(report_style, 6)
        max_pages = int(requested_pages) if requested_pages else self._STYLE_MAX_PAGES.get(report_style, 1)

        # Write the RESOLVED style/cap back to ctx.extra. utils/judge.py's
        # style-consistency check reads ctx.extra["report_style"] to decide
        # whether the actual page count is "too many" — but that key is
        # ONLY ever written by PlannerAgent, BEFORE this method runs any of
        # the overrides above. Leaving it stale meant the Judge compared the
        # actual (correct) page count against the WRONG style's cap and
        # permanently flagged "style_page_count_exceeded", wasting the
        # orchestrator's entire feedback-loop retry budget every time — a
        # real, confirmed bug (every keyword-triggered "rich" upgrade hit
        # this; requested_num_pages made it unconditional, since it always
        # forces "rich" regardless of the planner's original suggestion).
        ctx.extra["report_style"] = report_style
        ctx.extra["effective_max_pages"] = max_pages

        # Produce enough candidates to fill all pages (rich: up to 30 slots)
        _outcome_measure_name = (ctx.extra.get("outcome_column") or {}).get("measure_name")
        plans = self._plan_visuals(
            table, buckets, ctx.measures,
            prioritized_kpis=ctx.extra.get("prioritized_kpis"),
            outcome_measure=_outcome_measure_name,
            visual_variety=ctx.extra.get("requested_visual_variety", ""),
        )
        log_decision(
            agent=self.name,
            decision_type="visual_selection",
            subject="visual_candidate_pool",
            rationale=(
                f"_plan_visuals produced {len(plans)} candidate visual(s) for "
                f"table '{table}' based on column bucket classification "
                f"(amount={len(buckets['amount'])}, qty={len(buckets['qty'])}, "
                f"date={len(buckets['date'])}, category={len(buckets['category'])}, "
                f"region={len(buckets['region'])}) and {len(ctx.measures)} measure(s)."
            ),
            confidence=0.9,
            extra={"candidate_count": len(plans),
                   "visual_kinds": list({p["kind"] for p in plans})},
        )

        # Only cap for non-rich styles; rich mode gets more candidates so the
        # VisualPlannerAgent can distribute them across multiple pages.
        if report_style != "rich":
            plans = plans[:max_visuals_per_page]

        # Phase 3 — delegate page/visual planning to the intent-aware
        # VisualPlannerAgent. It now runs as a proper BaseAgent (Fix 6):
        # candidates and params are passed via ctx.extra so _run() / run()
        # participates in pipeline tracing (event bus, audit log, sync_to_state).
        from agents.visual_planner_agent import VisualPlannerAgent

        ctx.extra["visual_candidates"] = plans
        ctx.extra["visual_plan_max_pages"] = max_pages
        planner = VisualPlannerAgent(ctx)
        planner.run()
        report_plan = ctx.extra.get("report_plan")  # type: ignore[assignment]

        # Fallback: if VisualPlannerAgent returned nothing (crash or empty plan), wrap candidates
        if report_plan is None or not report_plan.pages:
            from agents.schemas import PagePlan, ReportPlan, VisualPlan as VP
            fallback_visuals = [
                VP(
                    name=p["name"], kind=p["kind"],
                    measure=p.get("measure"), category=p.get("category"),
                    columns=[list(c) for c in p.get("columns", [])] if p.get("columns") else [],
                    intent_match_reasoning="fallback candidate",
                )
                for p in plans
            ]
            from agents.schemas import ReportPlan as RP
            report_plan = RP(pages=[PagePlan(id="summary-page", displayName="Summary", visuals=fallback_visuals)])

        # Re-distribute visuals across pages using minimum-size-aware packing.
        # VisualPlannerAgent may assign more visuals to one page than can fit at
        # their minimum heights.  split_to_pages() computes per-page capacity from
        # MIN_H / MIN_W and redistributes overflow to additional pages.
        _all_vis_flat = [
            {
                "name":     v.name,
                "kind":     v.kind,
                "measure":  v.measure,
                "category": v.category,
                "columns":  [list(c) for c in (v.columns or [])],
            }
            for page in report_plan.pages[:max_pages]
            for v in page.visuals
        ]
        _page_chunks = split_to_pages(_all_vis_flat, max_pages)

        from agents.schemas import (  # noqa: PLC0415  (local import avoids circular)
            PagePlan as _PagePlan,
            ReportPlan as _ReportPlan,
            VisualPlan as _VP,
        )
        _new_pages: list[_PagePlan] = []
        for _idx, _chunk in enumerate(_page_chunks):
            _chunk_vis = [
                _VP(
                    name     = p["name"],
                    kind     = p["kind"],
                    measure  = p.get("measure"),
                    category = p.get("category"),
                    columns  = p.get("columns") or [],
                    intent_match_reasoning="layout-split",
                )
                for p in _chunk
            ]
            _pid   = "summary-page" if _idx == 0 else f"page-{_idx + 1}"
            _dname = "Summary"      if _idx == 0 else f"Page {_idx + 1}"
            _new_pages.append(_PagePlan(id=_pid, displayName=_dname, visuals=_chunk_vis))

        # Enforce max_pages cap (split_to_pages already respects it, but belt+braces)
        pages_to_write = _new_pages[:max_pages]

        # 3) theme.json — write ONCE before any page (so report.json can ref it)
        from patterns.themes import get_theme
        theme_dict = get_theme(ctx.theme_preset)
        report_folder = f"{ctx.project_name}.Report"
        theme = ctx.toolbox.write_theme_json(report_folder, theme_dict)
        if not theme.ok:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"write_theme_json failed: {theme.message}",
                errors=theme.errors,
            )
        theme_name = theme.data.get("name", "PowerBI Builder Default") if theme.data else None

        # 4) report.json (PBIR 3.0.0) with custom theme reference
        report_json_path = safe_join(ctx.toolbox.root, ctx.report_definition_rel,
                                     "report.json")
        atomic_write_json(report_json_path, pb.report_json(custom_theme_name=theme_name))

        # Write all pages + their visuals
        all_ctx_pages: list[dict] = []
        total_visuals_written = 0

        for page_idx, page in enumerate(pages_to_write):
            # Unique page_id: edit mode always gets a fresh uuid so we don't
            # clobber existing pages; create mode uses the plan's id.
            if ctx.existing_page_ids:
                page_id = f"ai-{_uuid_mod.uuid4().hex[:8]}"
            else:
                page_id = page.id if page.id else f"page-{page_idx + 1}"

            page_root = safe_join(ctx.toolbox.root, ctx.report_definition_rel,
                                  "pages", page_id)
            visuals_dir = page_root / "visuals"
            ensure_dir(visuals_dir)

            # 1) page.json (PBIR 2.0.0)
            atomic_write_json(page_root / "page.json",
                              pb.page_json(page_id, page.displayName))

            # Convert VisualPlan objects -> plan dicts. For "rich" style,
            # `page.visuals` already reflects split_to_pages()'s size-aware
            # per-page packing (which deliberately keeps ALL cards on page 1
            # regardless of count, on top of a separately-computed main-
            # visual capacity) — re-applying a flat [:max_visuals_per_page]
            # cap here would silently discard whatever that packing put
            # past index N, including entire chart types. Only minimal/
            # standard (always exactly 1 page, no size-aware splitting)
            # still need this flat cap.
            _page_visuals = (
                page.visuals if report_style == "rich"
                else page.visuals[:max_visuals_per_page]
            )
            page_plan_dicts = [
                {
                    "name": v.name, "kind": v.kind,
                    "measure": v.measure, "category": v.category,
                    "columns": [list(c) for c in v.columns] if v.columns else [],
                }
                for v in _page_visuals
            ]

            # 2) one visual.json per visual (PBIR 2.7.0) — skip invalid plans
            # Precompute zone-based positions for all visuals on this page.
            page_positions = compute_page_layout(page_plan_dicts)
            valid_plans: list[dict] = []
            for i, (plan, pos_geo) in enumerate(zip(page_plan_dicts, page_positions)):
                # Merge geometry with z-order / tab-order (both = visual index)
                pos = {**pos_geo, "z": i, "tabOrder": i}
                payload = self._build_visual_payload(table, plan, pos)
                if payload is None:
                    continue
                vid = plan["name"]
                vdir = visuals_dir / vid
                ensure_dir(vdir)
                atomic_write_json(vdir / "visual.json", payload)
                valid_plans.append(plan)

            total_visuals_written += len(valid_plans)
            all_ctx_pages.append({
                "id": page_id,
                "displayName": page.displayName,
                "visuals": [{"id": p["name"], "visualType": p["kind"]}
                            for p in valid_plans],
            })

        ctx.pages = all_ctx_pages
        ctx.extra["report_json_path"] = str(report_json_path)

        num_pages = len(all_ctx_pages)
        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Built report with {total_visuals_written} visuals on "
                f"{num_pages} page(s)."
            ),
            data={
                "page_count": num_pages,
                "visual_count": total_visuals_written,
                "pages": [
                    {
                        "id": p["id"],
                        "displayName": p["displayName"],
                        "visual_count": len(p["visuals"]),
                    }
                    for p in all_ctx_pages
                ],
                "report_json": str(report_json_path),
            },
        )

    # ------------------------------------------------------------------
    # planning: pick the fields each visual binds to (no JSON shape yet)
    # ------------------------------------------------------------------

    def _plan_visuals(
        self, table: str,
        buckets: dict[str, list[dict[str, Any]]],
        measures: list[dict[str, Any]],
        prioritized_kpis: list[str] | None = None,
        outcome_measure: str | None = None,
        visual_variety: str = "",
    ) -> list[dict[str, Any]]:
        """Return a list of visual *plans* (kind + bound fields).

        Produces a rich pool of candidates so multi-page rich reports have
        enough material. The caller (ReportAgent._run) caps per the style.
        Each plan is a plain dict; the actual PBIR JSON is built later by
        :meth:`_build_visual_payload`.

        ``visual_variety="all"`` additionally includes scatter/pie/kpi
        candidates (previously only available via the separate, additive
        ``build_report`` MCP tool's own ``_plan_rich_pages`` — ported here
        so a single ``generate_pbip(..., visual_variety="all")`` call can
        decide on full variety up front instead of building once and
        calling ``build_report`` afterward to top up the variety).
        """
        # Build typed visual pools first, then interleave them round-robin.
        # This guarantees every chunk-of-N (i.e. every page in the fallback
        # planner) gets at least one representative of each visual type, even
        # when the dataset has no date/amount columns (e.g. bank marketing).
        used_names: set[str] = set()

        # typed pools
        p_cards:   list[dict] = []
        p_bars:    list[dict] = []
        p_cols:    list[dict] = []
        p_donuts:  list[dict] = []
        p_lines:   list[dict] = []
        p_matrix:  list[dict] = []
        p_slicers: list[dict] = []
        p_tables:  list[dict] = []
        p_pies:    list[dict] = []
        p_scatter: list[dict] = []
        p_kpis:    list[dict] = []

        def _mk(pool: list[dict], plan: dict) -> None:
            """Append to a typed pool if the name is unique."""
            if plan["name"] not in used_names:
                pool.append(plan)
                used_names.add(plan["name"])

        all_cat_cols = (
            buckets["category"] + buckets["region"] + buckets["other"]
        )
        primary_measure = self._amount_measure(
            measures, buckets, prioritized_kpis, outcome_measure=outcome_measure,
        ) or (measures[0]["name"] if measures else None)
        secondary_measures = [m["name"] for m in measures
                               if m["name"] != primary_measure][:3]
        mnames_all = [m["name"] for m in measures]

        # --- KPI cards for every measure ---------------------------------
        # Card candidates are built in `measures` order, but non-rich styles
        # truncate the whole candidate pool to `max_visuals_per_page` before
        # a single page is even planned (see ReportAgent._run). A guaranteed
        # measure appended late to the measures list (e.g. DAXAgent's
        # outcome-rate guarantee, which fires precisely when no other KPI
        # exists) would otherwise be silently truncated away — present in
        # the model, invisible in the report. Stable-sort the primary
        # measure's card to the front so it always survives the cap.
        _card_source = measures
        if primary_measure:
            _card_source = sorted(
                measures, key=lambda m: 0 if m["name"] == primary_measure else 1,
            )
        for m in _card_source:
            mname = m["name"]
            slug = mname.lower().replace(" ", "-").replace("_", "-")[:24]
            _mk(p_cards, {"name": f"card-{slug}", "kind": "card",
                          "measure": mname})

        # --- Bar / column / donut: one type per column (no repetition) ---
        # Each column gets ONE primary chart type (rotating through bar →
        # column → donut → bar …) and ONE secondary chart type (different).
        _PRI = ["barChart", "columnChart", "donutChart"]
        _SEC = ["columnChart", "donutChart", "barChart"]
        _POOL_MAP = {
            "barChart": p_bars,
            "columnChart": p_cols,
            "donutChart": p_donuts,
        }
        for i, col in enumerate(all_cat_cols[:9]):
            cname = col["name"]
            cslug = cname.lower().replace(" ", "-")[:16]
            ptype = _PRI[i % 3]
            stype = _SEC[i % 3]
            if primary_measure:
                _mk(_POOL_MAP[ptype],
                    {"name": f"{ptype[:3]}-{cslug}", "kind": ptype,
                     "category": cname, "measure": primary_measure})
            if secondary_measures:
                sec = secondary_measures[i % len(secondary_measures)]
                sec_slug = sec.lower().replace(" ", "-")[:12]
                _mk(_POOL_MAP[stype],
                    {"name": f"sec-{cslug}-{sec_slug}", "kind": stype,
                     "category": cname, "measure": sec})

        # --- Line charts: date cols or time-hinted categoricals ----------
        _TIME_HINTS = ("month", "day", "week", "year", "quarter", "date",
                       "period", "time", "hour")
        line_cols = list(buckets["date"])
        for col in all_cat_cols:
            if any(h in col["name"].lower() for h in _TIME_HINTS):
                line_cols.append(col)
        for dcol in line_cols[:3]:
            dname = dcol["name"]
            dslug = dname.lower().replace(" ", "-")[:16]
            if primary_measure:
                _mk(p_lines, {"name": f"line-{dslug}", "kind": "lineChart",
                              "category": dname, "measure": primary_measure})
            for sec in secondary_measures[:1]:
                sec_slug = sec.lower().replace(" ", "-")[:12]
                _mk(p_lines, {"name": f"line-{dslug}-{sec_slug}",
                              "kind": "lineChart",
                              "category": dname, "measure": sec})

        # --- Matrix: each categorical col with a different measure -------
        for j, col in enumerate(all_cat_cols[:3]):
            if not mnames_all:
                break
            _mk(p_matrix, {
                "name": f"matrix-{col['name'].lower()[:14]}",
                "kind": "matrix",
                "category": col["name"],
                "measure": mnames_all[j % len(mnames_all)],
                "columns": [(table, col["name"])],
            })

        # --- Slicers: up to 3 filterable dims ----------------------------
        for col in all_cat_cols[:3]:
            sname = col["name"]
            _mk(p_slicers, {"name": f"slicer-{sname.lower()[:16]}",
                            "kind": "slicer", "category": sname})

        # --- Tables ------------------------------------------------------
        table_cols = self._pick_table_columns(buckets)
        if table_cols:
            _mk(p_tables, {"name": "table-details", "kind": "tableEx",
                           "columns": [(table, c) for c in table_cols]})
        summary_cols = table_cols[:4] if len(table_cols) >= 4 else table_cols
        if summary_cols:
            _mk(p_tables, {"name": "table-summary", "kind": "tableEx",
                           "columns": [(table, c) for c in summary_cols]})

        # --- Rich visual types (pie / scatter / kpi) ----------------------
        # Ported from mcp_server/highlevel.py's _plan_rich_pages (previously
        # only reachable via the separate, additive build_report tool) so a
        # single generate_pbip(visual_variety="all") call gets the same
        # variety in one shot.
        if visual_variety == "all":
            # Pie: same category+measure pairing as donut, alternate columns
            # so it doesn't just duplicate the donut candidate 1:1.
            for col in all_cat_cols[:6]:
                cname = col["name"]
                cslug = cname.lower().replace(" ", "-")[:16]
                if primary_measure:
                    _mk(p_pies, {"name": f"pie-{cslug}", "kind": "pieChart",
                                 "category": cname, "measure": primary_measure})

            # Scatter: needs two distinct measures (X, Y) + an optional
            # category for point coloring/detail.
            if primary_measure and secondary_measures:
                cat_for_scatter = all_cat_cols[0]["name"] if all_cat_cols else None
                _mk(p_scatter, {
                    "name": "scatter-analysis", "kind": "scatterChart",
                    "measure": primary_measure, "measure2": secondary_measures[0],
                    "category": cat_for_scatter,
                })

            # KPI: indicator measure + optional trend column (date/time-
            # hinted categorical) + optional goal measure (a second,
            # DIFFERENT measure — never the same one reused as its own
            # target, which is meaningless).
            if primary_measure:
                trend_col = line_cols[0]["name"] if line_cols else None
                kpi_plan = {"name": "kpi-primary", "kind": "kpi",
                            "measure": primary_measure, "category": trend_col}
                if secondary_measures:
                    kpi_plan["measure2"] = secondary_measures[0]
                _mk(p_kpis, kpi_plan)

        # --- Round-robin interleave all pools ----------------------------
        # Order of pools controls the per-page visual type distribution.
        # With 6 visuals/page the first round fills page-1, second fills
        # page-2, etc. — so every page naturally gets a variety of types.
        all_pools = [p_cards, p_bars, p_cols, p_donuts, p_lines,
                     p_matrix, p_slicers, p_tables, p_pies, p_scatter, p_kpis]
        plans: list[dict[str, Any]] = []
        seen_final: set[str] = set()
        max_rounds = max(len(p) for p in all_pools) if all_pools else 1
        for round_i in range(max_rounds):
            for pool in all_pools:
                if round_i < len(pool):
                    item = pool[round_i]
                    if item["name"] not in seen_final:
                        plans.append(item)
                        seen_final.add(item["name"])

        return plans

    # ------------------------------------------------------------------
    # build the actual PBIR visual.json payload from a plan
    # ------------------------------------------------------------------

    @staticmethod
    def _build_visual_payload(table: str, plan: dict[str, Any],
                              pos: dict[str, Any]) -> dict[str, Any] | None:
        """Build PBIR visual.json from a plan. Returns None if required fields are missing."""
        name = plan["name"]
        kind = plan["kind"]
        if kind == "card":
            if not plan.get("measure"):
                return None
            return pb.build_card(name, pos, table, plan["measure"])
        if kind == "barChart":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_bar_chart(name, pos, table, plan["category"],
                                      plan["measure"])
        if kind == "columnChart":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_column_chart(name, pos, table, plan["category"],
                                         plan["measure"])
        if kind == "lineChart":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_line_chart(name, pos, table, plan["category"],
                                       plan["measure"])
        if kind == "tableEx":
            if not plan.get("columns"):
                return None
            return pb.build_table(name, pos, plan["columns"])
        if kind == "donutChart":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_donut(name, pos, table, plan["category"], plan["measure"])
        if kind == "pieChart":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_pie(name, pos, table, plan["category"], plan["measure"])
        if kind == "scatterChart":
            if not plan.get("measure"):
                return None
            # measure2 is the Y axis when supplied (a genuine second measure,
            # e.g. from the visual_variety="all" scatter candidate); fall
            # back to the single measure on both axes only for older/other
            # callers that never supplied one.
            y_measure = plan.get("measure2") or plan["measure"]
            return pb.build_scatter(name, pos, table, plan["measure"], y_measure, plan.get("category"))
        if kind == "matrix":
            if not plan.get("category") or not plan.get("measure"):
                return None
            return pb.build_matrix(name, pos, table, [plan["category"]], [plan["measure"]])
        if kind == "kpi":
            if not plan.get("measure"):
                return None
            # goal must be a DIFFERENT measure representing a target/
            # benchmark -- reusing the same measure as its own "goal" is
            # meaningless, so only pass one when the plan actually supplies
            # a distinct measure2 (e.g. from visual_variety="all").
            return pb.build_kpi(name, pos, table, plan["measure"], plan.get("category"), plan.get("measure2"))
        if kind == "slicer":
            if not plan.get("category"):
                return None
            return pb.build_slicer(name, pos, table, plan["category"], "column")
        raise ValueError(f"Unknown visual kind: {kind}")

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_table_columns(buckets: dict[str, list[dict[str, Any]]]) -> list[str]:
        chosen: list[str] = []
        for key in ("date", "region", "category", "amount", "qty"):
            for col in buckets.get(key, []):
                if col["name"] not in chosen:
                    chosen.append(col["name"])
                if len(chosen) >= 5:
                    return chosen
        return chosen

    @staticmethod
    def _first_measure(measures: list[dict[str, Any]], contains: str = "") -> dict[str, Any] | None:
        for m in measures:
            if contains.lower() in m["name"].lower():
                return m
        return None

    @staticmethod
    def _amount_measure(
        measures: list[dict[str, Any]],
        buckets: dict[str, list[dict[str, Any]]],
        prioritized_kpis: list[str] | None = None,
        outcome_measure: str | None = None,
    ) -> str | None:
        """Prefer the measure for the #1 business-priority KPI, else the
        guaranteed binary-outcome rate measure (when one was detected), else
        a Total <Amount> measure, else the first measure.

        ``prioritized_kpis`` (from ``utils.kpi_prioritizer`` via
        ``ctx.extra["prioritized_kpis"]``) is the same business-importance
        ranking DAXAgent used to generate measures — trying each one in
        order (not just the top) covers strategies that skipped the #1 KPI
        (e.g. an "operational" strategy) by falling through to the next
        priority, then to the existing heuristics.

        ``outcome_measure`` (from ``ctx.extra["outcome_column"]["measure_name"]``,
        set by ``utils.kpi_prioritizer.detect_outcome_column``) is checked
        before the "Total X" / first-measure fallbacks: when a dataset has
        no monetary amount column at all (marketing response, churn, fraud),
        this guaranteed rate measure IS the real KPI, and must win over a
        generic filler like "Order Count" that would otherwise be
        ``measures[0]``.

        Returns ``None`` when no measure exists — callers must skip the visual
        in that case. Returning a hard-coded ``"Total Sales"`` would bind a
        visual to a measure that does not exist in the model (a "ghost" ref).
        """
        for kpi in (prioritized_kpis or []):
            kpi_lower = kpi.lower()
            for m in measures:
                mname_lower = m["name"].lower()
                if mname_lower in (f"total {kpi_lower}", kpi_lower):
                    return m["name"]
        if outcome_measure:
            for m in measures:
                if m["name"] == outcome_measure:
                    return m["name"]
        for m in measures:
            if m["name"].lower().startswith("total "):
                return m["name"]
        if measures:
            return measures[0]["name"]
        return None
