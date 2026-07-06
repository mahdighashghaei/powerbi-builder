"""DAXAgent -- generates well-formed DAX measures from the schema + brief.

Role
----
Given the schema produced by SchemaAgent and the user's business description,
produce 5-10 useful DAX measures following Power BI best practices:

* every measure has ``displayFolder``, ``description``, ``formatString``;
* measures are grouped into sensible display folders (Revenue, Orders, etc.);
* format strings match the underlying data type (currency, %, int, date).

The agent uses deterministic heuristics that inspect the schema columns, so it
works fully offline. If an LLM is configured (env API key) it can optionally
enrich the measure set, but the deterministic path is always the baseline.

MCP tools used: ``write_tmdl_measures``.
"""

from __future__ import annotations

import re
from typing import Any

from agents.base import AgentResult, BaseAgent
from utils.explainability import log_decision
from utils.identifiers import quote_dax_column, quote_dax_table


# ---------------------------------------------------------------------------
# Column classification helpers
# ---------------------------------------------------------------------------

_AMOUNT_HINTS = ("amount", "revenue", "sales", "price", "cost", "cogs", "value", "total", "profit", "discount", "income", "spend", "fee", "charge")
_QTY_HINTS = ("quantity", "qty", "count", "units", "volume", "orders")
_DATE_HINTS = ("date", "month", "year", "period", "time", "timestamp")
_REGION_HINTS = ("region", "country", "state", "city", "market", "territory", "location", "area", "zone")
_CATEGORY_HINTS = ("product", "category", "segment", "type", "brand", "department", "group", "class", "tier")
# Columns that look numeric but are NOT monetary / quantity amounts:
# rates, indices, durations, IDs, codes, campaign numbers, economic indicators.
_NON_AMOUNT_HINTS = (
    "duration", "campaign", "euribor", "rate", "index", "emp.var",
    "nr.employed", "cons.price", "cons.conf", "pdays", "previous",
    "id", "code", "seq", "num", "score", "ratio", "pct",
)


def _classify_columns(columns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket columns by likely business role using name + type hints.

    Negative exclusion list (_NON_AMOUNT_HINTS) prevents economic indicators,
    rates, durations, and IDs from being mis-classified as revenue amounts,
    which would produce nonsensical DAX measures like 'Total euribor3m'.
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "amount": [], "qty": [], "date": [],
        "region": [], "category": [], "other_numeric": [], "other": [],
    }
    for c in columns:
        lname = c["name"].lower().replace(".", "")  # normalise dots (emp.var → empvar)
        dtype = c["dataType"]
        is_numeric = dtype in {"double", "decimal", "int64"}

        # Check negative list first — these are never revenue amounts
        if any(h.replace(".", "") in lname for h in _NON_AMOUNT_HINTS):
            if is_numeric:
                buckets["other_numeric"].append(c)
            else:
                buckets["other"].append(c)
            continue

        if any(h in lname for h in _AMOUNT_HINTS) and is_numeric:
            buckets["amount"].append(c)
        elif any(h in lname for h in _QTY_HINTS) and is_numeric:
            buckets["qty"].append(c)
        elif any(h in lname for h in _DATE_HINTS) or dtype in {"dateTime", "date"}:
            buckets["date"].append(c)
        elif any(h in lname for h in _REGION_HINTS):
            buckets["region"].append(c)
        elif any(h in lname for h in _CATEGORY_HINTS):
            buckets["category"].append(c)
        elif is_numeric:
            buckets["other_numeric"].append(c)
        else:
            buckets["other"].append(c)
    return buckets


# ---------------------------------------------------------------------------
# Measure builders (deterministic, best-practice)
# ---------------------------------------------------------------------------

_CURRENCY_FMT = "$ #,##0.00"
_INT_FMT = "#,##0"
_PCT_FMT = "0.00%"


class DAXAgent(BaseAgent):
    """Generates and writes DAX measures based on the inferred schema."""

    name = "DAXAgent"
    description = (
        "You are the DAXAgent. Given a data model and a business description, "
        "author 5-10 useful DAX measures. Always set displayFolder, description, "
        "and formatString on every measure. Group related measures into folders "
        "(Revenue, Orders, Dates, etc.). Prefer explicit, readable DAX."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        if not ctx.schema:
            return AgentResult(
                agent=self.name, ok=False,
                message="No schema available; run SchemaAgent first.",
                errors=["ctx.schema is None"],
            )

        table = ctx.schema["table_name"]
        columns = ctx.schema["columns"]
        buckets = _classify_columns(columns)

        # Fix 2: consume business_analysis to prioritise measure generation.
        # DAXAgent MUST use potential_kpis / executive_metrics / important_measures
        # from the upstream DataAnalyzerAgent rather than relying on schema alone.
        biz = ctx.extra.get("business_analysis")

        # Business-aware KPI Prioritization Layer (additive): reorder the
        # amount bucket by business importance (semantic BI-convention tiers +
        # business_analysis + business description), not just a boolean
        # "important or not" partition. Prefers the cross-agent ranking the
        # orchestrator already computed (ctx.extra["prioritized_kpis"]) so
        # DAXAgent, ReportAgent, InsightsAgent, and JudgeLayer all agree on
        # the same order; falls back to computing it locally when absent
        # (e.g. direct/unit-test usage without the orchestrator).
        from utils.kpi_prioritizer import rank_kpi_candidates, reorder_by_priority

        prioritized_kpis: list[str] = list(ctx.extra.get("prioritized_kpis") or [])
        if not prioritized_kpis:
            prioritized_kpis = rank_kpi_candidates(
                buckets["amount"], biz, ctx.business_description or "",
            )
        buckets["amount"] = reorder_by_priority(buckets["amount"], prioritized_kpis)

        if biz is not None:
            important = set(getattr(biz, "important_measures", []))
            exec_metrics = list(getattr(biz, "executive_metrics", []))
            potential_kpis = list(getattr(biz, "potential_kpis", []))
            log_decision(
                agent=self.name,
                decision_type="measure_rationale",
                subject="business_analysis_intake",
                rationale=(
                    f"Consumed business_analysis: {len(important)} important_measure(s), "
                    f"{len(exec_metrics)} executive_metric(s), "
                    f"{len(potential_kpis)} potential_kpi(s). "
                    "Amount bucket re-ordered by business-aware KPI priority "
                    f"(primary='{prioritized_kpis[0] if prioritized_kpis else 'n/a'}')."
                ),
                confidence=1.0,
                extra={
                    "important_measures": sorted(important),
                    "executive_metrics": exec_metrics,
                    "potential_kpis": potential_kpis,
                    "prioritized_kpis": prioritized_kpis,
                },
            )
        else:
            log_decision(
                agent=self.name,
                decision_type="measure_rationale",
                subject="business_analysis_intake",
                rationale=(
                    "No business_analysis found in ctx.extra; falling back to "
                    "semantic BI-convention tiers only for KPI prioritization "
                    f"(primary='{prioritized_kpis[0] if prioritized_kpis else 'n/a'}')."
                ),
                confidence=0.6,
                extra={"prioritized_kpis": prioritized_kpis},
            )

        # --- Adaptive candidate count + bias (injected by orchestrator) ---
        # candidate_count: how many strategies to generate (driven by input
        #   complexity; default 5 preserves prior behaviour exactly).
        # adaptive_bias:   semantic score shift from learning memory
        #   (default 0.0 preserves prior behaviour exactly).
        candidate_count: int = int(ctx.extra.get("candidate_count", 5))
        adaptive_bias: float = float(ctx.extra.get("adaptive_bias", 0.0))

        # --- Multi-candidate generation + global scoring ---
        # Base 5 strategies are always generated.  Extra strategies are added
        # when the orchestrator signals higher complexity / more exploration.
        from utils.scoring import score_dax_candidate, tournament_select, DEFAULT_WEIGHTS

        def _fill(m_list: list) -> list:
            """Apply filler logic to a candidate to ensure ≥5 measures."""
            if len(m_list) >= 5:
                return m_list
            existing_names = {m["name"] for m in m_list}
            for extra in self._filler_measures(table, buckets):
                if extra["name"] not in existing_names and len(m_list) < 5:
                    m_list.append(extra)
                    existing_names.add(extra["name"])
            return m_list

        candidate_a = _fill(self._build_measures(table, buckets))[:10]
        candidate_b = _fill(self._build_measures_operational(table, buckets))[:10]
        candidate_c = _fill(self._build_measures_time_focused(table, buckets))[:10]
        candidate_d = _fill(self._build_measures_profitability(table, buckets))[:10]
        candidate_e = _fill(self._build_measures_statistical(table, buckets))[:10]

        all_candidates = [candidate_a, candidate_b, candidate_c, candidate_d, candidate_e]
        candidate_ids  = [
            "revenue_first", "operational", "time_intelligence",
            "profitability", "statistical",
        ]

        # Expanded candidate pool when complexity demands more exploration
        if candidate_count > 5:
            # kpi_targeted: force SUM on every column matching a KPI keyword
            _kpi_list: list[str] = list(getattr(biz, "potential_kpis", []) or []) if biz else []
            candidate_f = _fill(
                self._build_measures_kpi_targeted(table, buckets, _kpi_list)
            )[:10]
            all_candidates.append(candidate_f)
            candidate_ids.append("kpi_targeted")

        if candidate_count > 6:
            # executive_summary: card-ready measures only (totals + KPI card set)
            candidate_g = _fill(
                self._build_measures_executive_summary(table, buckets)
            )[:10]
            all_candidates.append(candidate_g)
            candidate_ids.append("executive_summary")

        # Strategy Synthesis Layer (additive) — inject any DAX strategies the
        # orchestrator synthesized from failure/judge signals (empty by
        # default, so behaviour is byte-identical when no gap was detected).
        for _spec in (ctx.extra.get("synthesized_strategies", {}).get("dax") or []):
            try:
                from utils.strategy_synthesizer import apply_dax_strategy
                synth_measures = _fill(apply_dax_strategy(_spec, table, buckets, biz))[:10]
                if synth_measures:
                    all_candidates.append(synth_measures)
                    candidate_ids.append(_spec["strategy_id"])
            except Exception as _synth_exc:  # noqa: BLE001 — fail-safe
                self.log.warning(
                    f"synthesized dax strategy '{_spec.get('strategy_id')}' skipped: {_synth_exc}"
                )

        weights = dict(ctx.extra.get("scoring_weights") or DEFAULT_WEIGHTS)  # type: ignore[arg-type]
        biz_desc = ctx.business_description or ""
        scores = [
            score_dax_candidate(
                cid, cand, biz, ctx.schema, weights, biz_desc,
                adaptive_bias=adaptive_bias,
            )
            for cid, cand in zip(candidate_ids, all_candidates)
        ]

        # Context-aware tournament: interleave diverse groups when pool is large
        _kpi_score_map: dict[str, float] = {
            s.candidate_id: s.semantic.kpi_semantic_alignment
            for s in scores
        }
        best_idx, best_score, rejected = tournament_select(
            all_candidates, scores,
            context_aware=(candidate_count > 5),
            kpi_scores=_kpi_score_map,
        )
        measures = all_candidates[best_idx]

        log_decision(
            agent=self.name,
            decision_type="measure_rationale",
            subject="candidate_selection",
            rationale=(
                f"Tournament-selected strategy '{best_score.candidate_id}' "
                f"(final={best_score.total:.3f}, "
                f"semantic={best_score.semantic_total:.3f}, "
                f"heuristic={best_score.heuristic_total:.3f}) over "
                + ", ".join(f"'{r['candidate_id']}'={r['score']:.3f}" for r in rejected)
                + f". Weights: bv={weights['business_value']:.2f}, "
                f"ka={weights['kpi_alignment']:.2f}, dc={weights['data_coverage']:.2f}."
            ),
            confidence=best_score.total,
            extra={
                "selected":  best_score.as_dict(),
                "rejected":  rejected,
                "strategy":  f"tournament_{len(all_candidates)}_candidates",
            },
        )
        ctx.extra["dax_candidates"] = [s.as_dict() for s in scores]

        # Phase 3 — delegate selection to the intent-aware MeasureSelectorAgent.
        # Offline (no API key) it returns the candidates unchanged, so the
        # baseline is byte-identical. Online it may prune/add measures with a
        # rationale per measure.
        from agents.measure_selector_agent import MeasureSelectorAgent

        selector = MeasureSelectorAgent()
        measure_set = selector.select(measures, ctx.business_description, ctx.schema)
        # convert the typed MeasureSet back to the dict shape write_tmdl_measures
        # expects (keeping the rationale for the feedback loop).
        measures = [m.model_dump() for m in measure_set.measures]

        # Business Insights Layer (additive): guarantee a small set of
        # insight-bearing measures regardless of which tournament strategy
        # won — historical trend comparison, category ranking, and
        # threshold-free anomaly detection must always be available, not
        # just when the strategy that happens to generate them wins.
        measures = self._ensure_guaranteed_insight_measures(measures, table, buckets)

        # Concept Coverage Enforcement (system stabilization phase): every
        # business concept the user explicitly named (revenue, margin,
        # discount, cost, ...) MUST have a measure — generate the missing
        # derived ratio measures automatically rather than silently letting
        # the winning strategy skip them.
        measures = self._ensure_concept_coverage_measures(measures, table)

        # Binary-Outcome KPI Synthesis: many real datasets (marketing
        # response, churn, fraud, conversion) have no monetary "amount"
        # column at all — their real KPI is a RATE derived from a binary
        # categorical outcome column (e.g. "y" = yes/no). Guarantee a
        # conversion/outcome-rate measure the same way Margin %/Discount
        # Rate % are guaranteed for financial datasets.
        measures = self._ensure_outcome_rate_measure(measures, table)

        # Aggregation Safety Fix: a single centralized pass that rewrites
        # any "Total <rate column>" measure (e.g. "Total Manufacturing
        # Price") to an AVERAGE — catches the output of every strategy,
        # guaranteed measures included, regardless of which one won.
        measures = self._sanitize_rate_aggregations(measures)

        # Duplicate-Name Safety Fix: the tournament winner plus the three
        # additive "ensure" layers above (guaranteed insight / concept
        # coverage / outcome-rate) can independently compute the same
        # display name for the same underlying column (e.g. two of them
        # both deriving "Avg Contacts per Client"). write_tmdl_measures
        # only dedupes against what's already ON DISK from a PRIOR call —
        # it has no way to know two entries in the SAME incoming batch
        # collide, so both get written and Power BI Desktop rejects the
        # project outright on open ("Item '...' already exists in the
        # collection"). Confirmed live. Keep the first occurrence (the
        # tournament winner's own measure takes priority over an
        # additive layer computing the same name).
        measures = self._dedupe_measures_by_name(measures)

        # In edit mode: skip measures that already exist in the model
        if ctx.existing_measures:
            before = len(measures)
            measures = [m for m in measures if m["name"] not in ctx.existing_measures]
            skipped = before - len(measures)
            if skipped:
                self.log.info(f"skipped {skipped} measures already in model")

        if not measures:
            ctx.measures = []
            return AgentResult(
                agent=self.name, ok=True,
                message="All generated measures already exist in the model; nothing added.",
                data={"count": 0, "skipped": True},
            )

        # attach the target table to each measure for write_tmdl_measures
        for m in measures:
            m["table"] = table

        # write via MCP tool
        write = ctx.toolbox.write_tmdl_measures(ctx.sm_definition_rel, measures)
        if not write.ok:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"write_tmdl_measures failed: {write.message}",
                errors=write.errors,
            )

        ctx.measures = measures
        self.log.info(f"generated {len(measures)} measures across folders: "
                      f"{sorted({m['displayFolder'] for m in measures})}")

        return AgentResult(
            agent=self.name,
            ok=True,
            message=f"Generated {len(measures)} DAX measures "
                    f"({len({m['displayFolder'] for m in measures})} folders).",
            data={
                "count": len(measures),
                "folders": sorted({m["displayFolder"] for m in measures}),
                "measures": [
                    {"name": m["name"], "folder": m["displayFolder"],
                     "format": m["formatString"]}
                    for m in measures
                ],
            },
        )

    # ------------------------------------------------------------------
    # measure generation
    # ------------------------------------------------------------------

    def _build_measures(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)  # safe single-quoted table name

        # helper: fully-qualified, safely-quoted column reference
        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        # --- Revenue folder: totals / averages on amount columns ---------
        for col in buckets["amount"][:2]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Total {cname}",
                expression=f"SUM({ref(cname)})",
                folder="Revenue",
                description=f"Sum of {cname}.",
                format=_CURRENCY_FMT,
            ))
            measures.append(self._m(
                name=f"Avg {cname}",
                expression=f"AVERAGE({ref(cname)})",
                folder="Revenue",
                description=f"Average of {cname} per row.",
                format=_CURRENCY_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"Total {cname} / Avg {cname}",
                rationale=(
                    f"'{cname}' classified as amount column; "
                    "generated SUM and AVERAGE measures in Revenue folder."
                ),
                alternatives=["COUNT", "MAX", "MIN"],
                confidence=0.9,
            )

        # --- Orders folder: counts + quantity totals --------------------
        for col in buckets["qty"][:1]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Total {cname}",
                expression=f"SUM({ref(cname)})",
                folder="Orders",
                description=f"Total {cname} across all rows.",
                format=_INT_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"Total {cname}",
                rationale=(
                    f"'{cname}' classified as quantity column; "
                    "generated SUM measure in Orders folder."
                ),
                confidence=0.85,
            )

        # row count is almost always useful
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="Orders",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))
        log_decision(
            agent="DAXAgent",
            decision_type="measure_rationale",
            subject="Order Count",
            rationale="COUNTROWS added unconditionally; fundamental grain measure for every model.",
            confidence=1.0,
        )

        # distinct count of a category column, if present
        if buckets["category"]:
            cat = buckets["category"][0]["name"]
            measures.append(self._m(
                name=f"Distinct {cat}",
                expression=f"DISTINCTCOUNT({ref(cat)})",
                folder="Orders",
                description=f"Number of unique {cat}.",
                format=_INT_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"Distinct {cat}",
                rationale=(
                    f"'{cat}' classified as category column; "
                    "DISTINCTCOUNT enables segment-level cardinality analysis."
                ),
                confidence=0.8,
            )

        # --- derived ratio: revenue per order (if both exist) -----------
        if buckets["amount"] and (self._has(measures, "Order Count") or buckets["qty"]):
            amt = buckets["amount"][0]["name"]
            measures.append(self._m(
                name=f"Avg {amt} per Order",
                expression=(
                    f"DIVIDE(SUM({ref(amt)}), COUNTROWS({qtable}), 0)"
                ),
                folder="Revenue",
                description=f"Average {amt} contributed per order.",
                format=_CURRENCY_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"Avg {amt} per Order",
                rationale=(
                    f"Amount column '{amt}' and row count both present; "
                    "DIVIDE ratio measure generated for per-order benchmarking."
                ),
                alternatives=["skip ratio"],
                confidence=0.8,
            )

        # --- Dates folder: time intelligence on amount + first/last date -
        if buckets["amount"] and buckets["date"]:
            amt = buckets["amount"][0]["name"]
            dcol = buckets["date"][0]["name"]
            measures.append(self._m(
                name=f"{amt} YTD",
                expression=(
                    f"TOTALYTD(SUM({ref(amt)}), {ref(dcol)})"
                ),
                folder="Dates",
                description=f"Year-to-date total of {amt}.",
                format=_CURRENCY_FMT,
            ))
            measures.append(self._m(
                name=f"{amt} PY",
                expression=(
                    f"CALCULATE(SUM({ref(amt)}), "
                    f"DATEADD({ref(dcol)}, -1, YEAR))"
                ),
                folder="Dates",
                description=f"Prior-year {amt} for YoY comparison.",
                format=_CURRENCY_FMT,
            ))
            # YoY growth %
            measures.append(self._m(
                name=f"{amt} YoY %",
                expression=(
                    f"DIVIDE(SUM({ref(amt)}) - "
                    f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), "
                    f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), 0)"
                ),
                folder="Dates",
                description=f"Year-over-year growth % of {amt}.",
                format=_PCT_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"{amt} YTD / {amt} PY / {amt} YoY %",
                rationale=(
                    f"Date column '{dcol}' and amount column '{amt}' both present; "
                    "generated TOTALYTD, DATEADD prior-year, and YoY % time-intelligence suite."
                ),
                alternatives=["MTD only", "skip time intelligence"],
                confidence=0.9,
                extra={"date_column": dcol, "amount_column": amt},
            )

        return measures

    def _build_measures_operational(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Operational strategy: emphasise counts, distinct counts, and averages.

        Candidate B in the multi-hypothesis scoring flow. Prioritises KPIs
        useful for operational monitoring (volume, variety, efficiency) rather
        than pure revenue aggregation.
        """
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        # Row-count (always first — fundamental operational metric)
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="Operations",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))

        # Distinct count for every category + region column (up to 4)
        for col in (buckets["category"] + buckets["region"])[:4]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Distinct {cname}",
                expression=f"DISTINCTCOUNT({ref(cname)})",
                folder="Operations",
                description=f"Number of unique {cname} values.",
                format=_INT_FMT,
            ))

        # Average of every qty/amount column (up to 3)
        for col in (buckets["qty"] + buckets["amount"])[:3]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Avg {cname}",
                expression=f"AVERAGE({ref(cname)})",
                folder="Performance",
                description=f"Average {cname} per record.",
                format=_CURRENCY_FMT if col in buckets["amount"] else _INT_FMT,
            ))

        # Total of first qty column (volume KPI)
        for col in buckets["qty"][:1]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Total {cname}",
                expression=f"SUM({ref(cname)})",
                folder="Operations",
                description=f"Total {cname} across all records.",
                format=_INT_FMT,
            ))

        # Total rows ignoring filters (operational denominator)
        measures.append(self._m(
            name="Records All",
            expression=f"COUNTROWS(ALL({qtable}))",
            folder="Operations",
            description="Total rows ignoring all filters.",
            format=_INT_FMT,
        ))

        return measures

    def _build_measures_time_focused(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Time-intelligence strategy: emphasise YTD, QTD, MoM, and PY variants.

        Candidate C in the multi-hypothesis scoring flow. Generates a rich set
        of time-intelligence measures for every (amount × date) pair. Falls
        back to operational strategy when no date columns are present.
        """
        # Can't do time intelligence without date columns
        if not buckets["date"] or not buckets["amount"]:
            return self._build_measures_operational(table, buckets)

        measures: list[dict[str, Any]] = []

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        # Generate YTD / QTD / PY / YoY% for up to 2 amount × 2 date combos
        for amt_col in buckets["amount"][:2]:
            amt = amt_col["name"]
            # Base SUM (needed for the time-intelligence expressions)
            measures.append(self._m(
                name=f"Total {amt}",
                expression=f"SUM({ref(amt)})",
                folder="Revenue",
                description=f"Total sum of {amt}.",
                format=_CURRENCY_FMT,
            ))
            for d_col in buckets["date"][:2]:
                dcol = d_col["name"]
                measures.append(self._m(
                    name=f"{amt} YTD",
                    expression=f"TOTALYTD(SUM({ref(amt)}), {ref(dcol)})",
                    folder="Dates",
                    description=f"Year-to-date total of {amt}.",
                    format=_CURRENCY_FMT,
                ))
                measures.append(self._m(
                    name=f"{amt} QTD",
                    expression=f"TOTALQTD(SUM({ref(amt)}), {ref(dcol)})",
                    folder="Dates",
                    description=f"Quarter-to-date total of {amt}.",
                    format=_CURRENCY_FMT,
                ))
                measures.append(self._m(
                    name=f"{amt} PY",
                    expression=(
                        f"CALCULATE(SUM({ref(amt)}), "
                        f"DATEADD({ref(dcol)}, -1, YEAR))"
                    ),
                    folder="Dates",
                    description=f"Prior-year total of {amt} for YoY comparison.",
                    format=_CURRENCY_FMT,
                ))
                measures.append(self._m(
                    name=f"{amt} YoY %",
                    expression=(
                        f"DIVIDE(SUM({ref(amt)}) - "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), 0)"
                    ),
                    folder="Dates",
                    description=f"Year-over-year growth % of {amt}.",
                    format=_PCT_FMT,
                ))
                measures.append(self._m(
                    name=f"{amt} MoM %",
                    expression=(
                        f"DIVIDE(SUM({ref(amt)}) - "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, MONTH)), "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, MONTH)), 0)"
                    ),
                    folder="Dates",
                    description=f"Month-over-month growth % of {amt}.",
                    format=_PCT_FMT,
                ))

        return measures

    def _build_measures_profitability(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Profitability strategy: margin analysis, ranking, and TOPN patterns.

        Candidate D in the 5-hypothesis tournament. Generates measures oriented
        toward profitability KPIs: margin %, top-N contributors, and RANKX
        expressions for ranking dimensions by value.
        """
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        # Base revenue SUM (anchor for margin calculations). Revenue/cost are
        # picked by semantic tier (utils.kpi_prioritizer), not raw position —
        # a "Profit" column must never be treated as "revenue" in the margin
        # formula below. Falls back to the first two amount columns when no
        # clear revenue/cost pair is found (byte-identical to prior behavior
        # for datasets with no cost-tier column).
        from utils.kpi_prioritizer import pick_revenue_and_cost_columns

        rev_col_dict, cost_col_dict = pick_revenue_and_cost_columns(buckets["amount"])
        amt_cols = [c for c in (rev_col_dict, cost_col_dict) if c is not None] or buckets["amount"][:2]
        for amt_col in amt_cols:
            amt = amt_col["name"]
            measures.append(self._m(
                name=f"Total {amt}",
                expression=f"SUM({ref(amt)})",
                folder="Revenue",
                description=f"Total sum of {amt}.",
                format=_CURRENCY_FMT,
            ))

        # Margin % when a revenue/cost pair is present
        if rev_col_dict is not None and cost_col_dict is not None and rev_col_dict is not cost_col_dict:
            rev_col = rev_col_dict["name"]
            cost_col = cost_col_dict["name"]
            measures.append(self._m(
                name="Gross Margin %",
                expression=(
                    f"DIVIDE(SUM({ref(rev_col)}) - SUM({ref(cost_col)}), "
                    f"SUM({ref(rev_col)}), 0)"
                ),
                folder="Finance",
                description=f"Gross margin percentage: ({rev_col} - {cost_col}) / {rev_col}.",
                format=_PCT_FMT,
            ))
            measures.append(self._m(
                name="Gross Profit",
                expression=f"SUM({ref(rev_col)}) - SUM({ref(cost_col)})",
                folder="Finance",
                description=f"Absolute gross profit: {rev_col} minus {cost_col}.",
                format=_CURRENCY_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject="Gross Margin % / Gross Profit",
                rationale=(
                    f"Two amount columns detected ('{rev_col}', '{cost_col}'); "
                    "generated profitability margin measures in Finance folder."
                ),
                confidence=0.85,
            )

        # RANKX by first category column (revenue per category rank)
        if amt_cols and buckets["category"]:
            amt = amt_cols[0]["name"]
            cat = buckets["category"][0]["name"]
            measures.append(self._m(
                name=f"{cat} Revenue Rank",
                expression=(
                    f"RANKX(ALL({qtable}[{cat}]), SUM({ref(amt)}), , DESC, Dense)"
                ),
                folder="KPI",
                description=f"Rank of each {cat} by total {amt} (1 = highest).",
                format=_INT_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"{cat} Revenue Rank",
                rationale=(
                    f"Category '{cat}' and amount '{amt}' both present; "
                    "RANKX enables top-N analysis."
                ),
                confidence=0.80,
            )

        # Row count (universal denominator)
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="Orders",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))

        return measures

    def _build_measures_statistical(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Statistical strategy: STDEV, MEDIAN, PERCENTILE, and rolling windows.

        Candidate E in the 5-hypothesis tournament. Generates measures oriented
        toward statistical analysis: spread, central tendency, and rolling
        averages. Useful for quality control and trend smoothing dashboards.
        """
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        numeric = (buckets["amount"] + buckets["qty"] + buckets["other_numeric"])[:2]

        for num_col in numeric:
            cname = num_col["name"]
            is_amt = num_col in buckets["amount"]
            fmt = _CURRENCY_FMT if is_amt else _INT_FMT
            folder = "Stats"

            measures.append(self._m(
                name=f"Avg {cname}",
                expression=f"AVERAGE({ref(cname)})",
                folder=folder,
                description=f"Mean of {cname}.",
                format=fmt,
            ))
            measures.append(self._m(
                name=f"Median {cname}",
                expression=f"MEDIAN({ref(cname)})",
                folder=folder,
                description=f"Median (50th percentile) of {cname}.",
                format=fmt,
            ))
            measures.append(self._m(
                name=f"StdDev {cname}",
                expression=f"STDEV.P({ref(cname)})",
                folder=folder,
                description=f"Population standard deviation of {cname}.",
                format=fmt,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"Avg/Median/StdDev {cname}",
                rationale=(
                    f"Statistical measures generated for '{cname}': "
                    "mean, median, and stddev for distribution analysis."
                ),
                confidence=0.75,
            )

        # Rolling 3-period average on first amount + first date column pair
        if buckets["amount"] and buckets["date"]:
            amt = buckets["amount"][0]["name"]
            dcol = buckets["date"][0]["name"]
            measures.append(self._m(
                name=f"{amt} 3-Month Avg",
                expression=(
                    f"AVERAGEX("
                    f"DATESINPERIOD({ref(dcol)}, MAX({ref(dcol)}), -3, MONTH), "
                    f"CALCULATE(SUM({ref(amt)})))"
                ),
                folder="Stats",
                description=f"Rolling 3-month average of {amt}.",
                format=_CURRENCY_FMT,
            ))
            log_decision(
                agent="DAXAgent",
                decision_type="measure_rationale",
                subject=f"{amt} 3-Month Avg",
                rationale=(
                    f"Date column '{dcol}' and amount column '{amt}' both present; "
                    "DATESINPERIOD rolling 3-month average smooths short-term noise."
                ),
                confidence=0.80,
            )

        # Row count
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="Stats",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))

        return measures

    def _build_measures_kpi_targeted(
        self,
        table: str,
        buckets: dict[str, list[dict[str, Any]]],
        potential_kpis: list[str],
    ) -> list[dict[str, Any]]:
        """KPI-targeted strategy: force SUM on every column matching a KPI keyword.

        Candidate F — generated only when candidate_count > 5 (medium/high
        complexity inputs).  Uses the DataAnalyzerAgent's ``potential_kpis``
        list to directly map schema columns to business-critical aggregations,
        maximising semantic kpi_alignment scores.
        """
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        kpi_lower = [k.lower() for k in (potential_kpis or [])]
        matched: list[dict[str, Any]] = []

        # All numeric columns whose name contains any KPI keyword
        all_numeric = buckets["amount"] + buckets["qty"] + buckets["other_numeric"]
        for col in all_numeric:
            lname = col["name"].lower()
            if any(kw in lname or lname in kw for kw in kpi_lower):
                matched.append(col)

        # If no KPI matches, fall back to all amount columns
        if not matched:
            matched = buckets["amount"][:5]

        for col in matched[:6]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Total {cname}",
                expression=f"SUM({ref(cname)})",
                folder="KPI",
                description=f"KPI-targeted total of {cname}.",
                format=_CURRENCY_FMT,
            ))

        # Row count anchor
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="KPI",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))

        return measures

    def _build_measures_executive_summary(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Executive summary strategy: card-ready totals + single top-line KPI.

        Candidate G — generated only when candidate_count > 6 (high complexity
        inputs). Produces the minimal, punchy set that an executive dashboard
        card visual needs: one SUM per amount column, one count, and one ratio.
        """
        measures: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)

        def ref(col: str) -> str:
            return quote_dax_column(table, col)

        # One SUM per amount column (up to 4 — card visual breadth)
        for col in buckets["amount"][:4]:
            cname = col["name"]
            measures.append(self._m(
                name=f"Total {cname}",
                expression=f"SUM({ref(cname)})",
                folder="Executive",
                description=f"Executive card total of {cname}.",
                format=_CURRENCY_FMT,
            ))

        # Row count
        measures.append(self._m(
            name="Order Count",
            expression=f"COUNTROWS({qtable})",
            folder="Executive",
            description="Number of records in the table.",
            format=_INT_FMT,
        ))

        # Revenue per order (top-line executive ratio)
        if buckets["amount"]:
            amt = buckets["amount"][0]["name"]
            measures.append(self._m(
                name=f"Avg {amt} per Order",
                expression=f"DIVIDE(SUM({ref(amt)}), COUNTROWS({qtable}), 0)",
                folder="Executive",
                description=f"Average {amt} per record — executive headline ratio.",
                format=_CURRENCY_FMT,
            ))

        return measures

    def _filler_measures(
        self, table: str, buckets: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Add generic, always-safe measures if we are short of the minimum."""
        extras: list[dict[str, Any]] = []
        qtable = quote_dax_table(table)
        numeric = buckets["amount"] + buckets["qty"] + buckets["other_numeric"]
        if numeric:
            col = numeric[0]["name"]
            extras.append(self._m(
                name=f"Min {col}",
                expression=f"MIN({quote_dax_column(table, col)})",
                folder="Stats",
                description=f"Minimum value of {col}.",
                format=_INT_FMT,
            ))
            extras.append(self._m(
                name=f"Max {col}",
                expression=f"MAX({quote_dax_column(table, col)})",
                folder="Stats",
                description=f"Maximum value of {col}.",
                format=_INT_FMT,
            ))
        extras.append(self._m(
            name="Records All",
            expression=f"COUNTROWS(ALL({qtable}))",
            folder="Stats",
            description="Total rows ignoring filters (denominator helper).",
            format=_INT_FMT,
        ))
        return extras

    # ------------------------------------------------------------------
    # Business Insights Layer — guaranteed insight-bearing measures
    # ------------------------------------------------------------------

    def _ensure_guaranteed_insight_measures(
        self,
        measures: list[dict[str, Any]],
        table: str,
        buckets: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Append YoY / Rank / Anomaly-Count measures when missing.

        These three measures close specific requirement gaps regardless of
        which DAX strategy won the tournament:

        * ``"{amt} YoY %"``            — historical trend comparison always
          available (not just when the time_intelligence/revenue_first
          strategy happens to win).
        * ``"{cat} {amt} Rank"``       — live category ranking, the input an
          analyst needs to spot underperforming categories in Desktop.
        * ``"{amt} Anomaly Count"``    — anomaly detection with NO hardcoded
          threshold: bounds are computed live in DAX via PERCENTILEX.INC and
          recalculate as filters change.

        Each is skipped if a measure whose name already contains the
        relevant marker ("yoy", "rank", "anomaly") exists, so a winning
        strategy that already covers it is never duplicated.
        """
        try:
            existing_lower = " ".join(m.get("name", "").lower() for m in measures)
            qtable = quote_dax_table(table)

            def ref(col: str) -> str:
                return quote_dax_column(table, col)

            amount_cols = buckets.get("amount", [])
            date_cols = buckets.get("date", [])
            cat_cols = buckets.get("category", []) + buckets.get("region", [])

            # --- YoY % (needs amount + date) -----------------------------
            if amount_cols and date_cols and "yoy" not in existing_lower:
                amt = amount_cols[0]["name"]
                dcol = date_cols[0]["name"]
                measures.append(self._m(
                    name=f"{amt} YoY %",
                    expression=(
                        f"DIVIDE(SUM({ref(amt)}) - "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), "
                        f"CALCULATE(SUM({ref(amt)}), DATEADD({ref(dcol)}, -1, YEAR)), 0)"
                    ),
                    folder="Insights",
                    description=(
                        f"Guaranteed year-over-year growth % of {amt}, always included "
                        "so historical trend comparison is available regardless of "
                        "which DAX strategy won the candidate tournament."
                    ),
                    format=_PCT_FMT,
                ))
                existing_lower += " yoy"

            # --- Category Rank (needs amount + category/region) ---------
            if amount_cols and cat_cols and "rank" not in existing_lower:
                amt = amount_cols[0]["name"]
                cat = cat_cols[0]["name"]
                measures.append(self._m(
                    name=f"{cat} {amt} Rank",
                    expression=(
                        f"RANKX(ALL({qtable}[{cat}]), SUM({ref(amt)}), , DESC, Dense)"
                    ),
                    folder="Insights",
                    description=(
                        f"Guaranteed ranking of each {cat} by total {amt} (1 = highest) "
                        "— the live input for spotting underperforming categories."
                    ),
                    format=_INT_FMT,
                ))
                existing_lower += " rank"

            # --- Anomaly Count (needs amount only; no fixed threshold) ---
            if amount_cols and "anomaly" not in existing_lower:
                amt = amount_cols[0]["name"]
                measures.append(self._m(
                    name=f"{amt} Anomaly Count",
                    expression=(
                        f"VAR __Q1 = PERCENTILEX.INC(ALL({qtable}), {ref(amt)}, 0.25)\n"
                        f"VAR __Q3 = PERCENTILEX.INC(ALL({qtable}), {ref(amt)}, 0.75)\n"
                        "VAR __IQR = __Q3 - __Q1\n"
                        "VAR __Lower = __Q1 - 1.5 * __IQR\n"
                        "VAR __Upper = __Q3 + 1.5 * __IQR\n"
                        f"RETURN COUNTROWS(FILTER({qtable}, "
                        f"{ref(amt)} < __Lower || {ref(amt)} > __Upper))"
                    ),
                    folder="Insights",
                    description=(
                        f"Number of statistical outliers in {amt}, computed dynamically "
                        "via the IQR method (no fixed/hardcoded threshold) — recalculates "
                        "live as report filters change."
                    ),
                    format=_INT_FMT,
                ))
                existing_lower += " anomaly"

            return measures
        except Exception as exc:  # noqa: BLE001 — must never block measure generation
            self.log.warning(f"guaranteed insight measures skipped: {exc}")
            return measures

    # ------------------------------------------------------------------
    # Concept Coverage Enforcement (system stabilization)
    # ------------------------------------------------------------------

    def _ensure_concept_coverage_measures(
        self, measures: list[dict[str, Any]], table: str,
    ) -> list[dict[str, Any]]:
        """Guarantee a derived ratio measure for every named-but-uncovered concept.

        Reads ``ctx.extra["business_concepts"]`` (from
        ``utils.concept_coverage.extract_concepts``, run once by the
        orchestrator against the business description) and
        ``ctx.extra["derived_kpi_candidates"]`` (from
        ``utils.kpi_prioritizer.derive_candidate_kpis``). For every concept
        that has a derived-KPI template (margin, discount, cost) but no
        measure yet, adds it — this is the "system MUST generate missing
        KPIs automatically" rule, implemented as one more guarantee in the
        same mechanism ``_ensure_guaranteed_insight_measures`` already uses.

        A concept without a matching derived-KPI candidate (e.g. the
        required raw columns simply don't exist in this schema) is left
        alone here — ``JudgeLayer`` surfaces that residual gap afterwards
        rather than this method fabricating something from nothing.
        """
        try:
            ctx = self.context
            concepts: list[str] = list(ctx.extra.get("business_concepts") or [])
            derived: list[dict[str, Any]] = list(ctx.extra.get("derived_kpi_candidates") or [])
            if not concepts or not derived:
                return measures

            from utils.concept_coverage import check_concept_coverage, missing_concepts

            coverage = check_concept_coverage(concepts, measures)
            gaps = missing_concepts(coverage)
            if not gaps:
                return measures

            def ref(col: str) -> str:
                return quote_dax_column(table, col)

            existing_names = {m.get("name", "").lower() for m in measures}
            by_concept = {d["concept"]: d for d in derived if d.get("concept")}
            for concept in gaps:
                spec = by_concept.get(concept)
                if not spec:
                    continue
                mname = spec.get("name", "")
                if not mname or mname.lower() in existing_names:
                    continue
                num, den = spec.get("numerator"), spec.get("denominator")
                if not num or not den:
                    continue
                measures.append(self._m(
                    name=mname,
                    expression=f"DIVIDE(SUM({ref(num)}), SUM({ref(den)}), 0)",
                    folder="Insights",
                    description=(
                        f"Guaranteed {mname} ({num} / {den}) — added automatically because "
                        f"the business description explicitly named '{concept}' and no "
                        "generated strategy produced a measure for it "
                        "(Concept Coverage Enforcement)."
                    ),
                    format=_PCT_FMT,
                ))
                existing_names.add(mname.lower())
            return measures
        except Exception as exc:  # noqa: BLE001 — must never block measure generation
            self.log.warning(f"concept coverage measures skipped: {exc}")
            return measures

    # ------------------------------------------------------------------
    # Binary-Outcome KPI Synthesis
    # ------------------------------------------------------------------

    def _ensure_outcome_rate_measure(
        self, measures: list[dict[str, Any]], table: str,
    ) -> list[dict[str, Any]]:
        """Guarantee a conversion/outcome-rate measure when a binary
        outcome column was detected (``utils.kpi_prioritizer.detect_outcome_column``,
        computed once by the orchestrator and stored in
        ``ctx.extra["outcome_column"]``).

        Closes the "non-financial domain" gap: a dataset with no monetary
        "amount" column at all (marketing response, churn, fraud) still
        gets its real KPI — a rate derived from the binary outcome column
        — rather than only ever falling back to generic filler measures
        (Order Count, Min/Max of whichever numeric column happened to
        exist).
        """
        try:
            ctx = self.context
            outcome: dict[str, Any] | None = ctx.extra.get("outcome_column")
            if not outcome:
                return measures

            col = outcome.get("column")
            positive_value = outcome.get("positive_value")
            mname = outcome.get("measure_name")
            if not col or positive_value is None or not mname:
                return measures

            existing_names = {m.get("name", "").lower() for m in measures}
            if mname.lower() in existing_names:
                return measures

            qtable = quote_dax_table(table)

            def ref(c: str) -> str:
                return quote_dax_column(table, c)

            # DAX string literals escape an embedded double-quote as "".
            dax_value = str(positive_value).replace('"', '""')
            filter_expr = f'FILTER({qtable}, {ref(col)} = "{dax_value}")'

            measures.append(self._m(
                name=mname,
                expression=f"DIVIDE(COUNTROWS({filter_expr}), COUNTROWS({qtable}), 0)",
                folder="Insights",
                description=(
                    f"Guaranteed {mname} — the fraction of rows where {col} = "
                    f"'{positive_value}', added automatically because no monetary "
                    "amount column exists to derive a KPI from (Binary-Outcome "
                    "KPI Synthesis)."
                ),
                format=_PCT_FMT,
            ))
            existing_names.add(mname.lower())

            # Derive the companion count's name FROM the rate measure's name
            # (stripping " Rate %") rather than re-deriving from the raw
            # column, so it stays consistent whether mname ended up generic
            # ("Conversion Rate %" -> "Conversion Count") or column-specific
            # ("Subscribed Rate %" -> "Subscribed Count").
            count_name = f"{mname[:-len(' Rate %')] if mname.endswith(' Rate %') else mname} Count"
            if count_name.lower() not in existing_names:
                measures.append(self._m(
                    name=count_name,
                    expression=f"COUNTROWS({filter_expr})",
                    folder="Insights",
                    description=(
                        f"Guaranteed row count where {col} = '{positive_value}' — "
                        f"the numerator behind {mname}, for card/visual pairing."
                    ),
                    format=_INT_FMT,
                ))
            return measures
        except Exception as exc:  # noqa: BLE001 — must never block measure generation
            self.log.warning(f"outcome rate measure skipped: {exc}")
            return measures

    # ------------------------------------------------------------------
    # Aggregation Safety Fix
    # ------------------------------------------------------------------

    _SUM_MEASURE_RE = re.compile(r"^SUM\((.+)\)$")
    _COL_NAME_IN_REF_RE = re.compile(r"\[([^\]]+)\]$")

    def _sanitize_rate_aggregations(
        self, measures: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rewrite ``SUM(<rate column>)`` measures to ``AVERAGE`` — a
        per-unit rate/price (e.g. "Manufacturing Price") summed across rows
        produces a meaningless total, regardless of which strategy or
        guaranteed-measure step generated it. This is the single choke
        point where the fix is applied, so it catches every measure the
        pipeline produced instead of touching each of the seven strategy
        builders individually.
        """
        try:
            from utils.kpi_prioritizer import is_rate_column

            sanitized: list[dict[str, Any]] = []
            for m in measures:
                expr = (m.get("expression") or "").strip()
                match = self._SUM_MEASURE_RE.match(expr)
                if match:
                    inner = match.group(1)
                    col_match = self._COL_NAME_IN_REF_RE.search(inner)
                    col_name = col_match.group(1) if col_match else None
                    if col_name and is_rate_column(col_name):
                        m = dict(m)
                        m["expression"] = f"AVERAGE({inner})"
                        if m.get("name", "").lower().startswith("total "):
                            m["name"] = "Avg " + m["name"][len("Total "):]
                        desc = (m.get("description") or "").rstrip(".")
                        m["description"] = (
                            f"{desc}. Aggregation Safety Fix: '{col_name}' is a per-unit "
                            "rate/price, averaged instead of summed to avoid a "
                            "meaningless total."
                        )
                sanitized.append(m)
            return sanitized
        except Exception as exc:  # noqa: BLE001 — must never block measure generation
            self.log.warning(f"rate aggregation sanitization skipped: {exc}")
            return measures

    def _dedupe_measures_by_name(
        self, measures: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop measures whose ``name`` collides with an earlier one in this
        SAME batch, keeping the first occurrence.

        write_tmdl_measures only dedupes against measures already ON DISK
        from a prior call -- it has no visibility into the batch it's
        currently given, so two entries with the same name in one call both
        get written, and Power BI Desktop's TMDL deserializer then rejects
        the whole project on open ("Item '...' already exists in the
        collection"). Confirmed live. A collision can come from more than
        one of the additive layers above computing the same display name
        for the same column, or from ``_sanitize_rate_aggregations`` itself
        renaming a "Total X" measure to "Avg X" and landing on a name
        another measure in the batch already has.

        Matching is case-INSENSITIVE: Power BI Desktop treats measure names
        case-insensitively, so "Min Age" and "Min age" collide and must be
        deduped together (the on-disk dedup in write_tmdl_measures handles
        cross-call casing drift; this guard handles same-batch collisions).
        """
        seen: set[str] = set()  # lowercased keys for case-insensitive dedup
        deduped: list[dict[str, Any]] = []
        for m in measures:
            name = m.get("name", "")
            key = name.lower()
            if key in seen:
                self.log.warning(f"dropped duplicate measure name in batch: {name!r}")
                continue
            seen.add(key)
            deduped.append(m)
        return deduped

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _m(
        *, name: str, expression: str, folder: str,
        description: str, format: str,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "expression": expression.strip(),
            "displayFolder": folder,
            "description": description,
            "formatString": format,
        }

    @staticmethod
    def _has(measures: list[dict[str, Any]], name: str) -> bool:
        return any(m["name"] == name for m in measures)
