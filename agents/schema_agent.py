"""SchemaAgent -- infers the data model from the input file and writes TMDL.

Role
----
Read the CSV (or JSON schema) supplied by the user, infer the Power BI data
model (table + columns + data types), and write the table's TMDL definition
via the MCP tool ``write_tmdl_table``.

It also seeds the shared context with the inferred schema so the downstream
DAX/Report agents know which columns and types they can reference.

MCP tools used: ``read_csv_schema``, ``write_tmdl_table``.
"""

from __future__ import annotations

import re

from agents.base import AgentResult, BaseAgent
from utils.explainability import log_decision
from utils.scoring import DEFAULT_WEIGHTS, score_schema_candidate, tournament_select


_TMDL_SAFE = re.compile(r"[^A-Za-z0-9_]")

# Power BI numeric types where SUM/COUNT aggregation makes sense.
_NUMERIC_TYPES = {"int64", "double", "decimal"}


def _apply_schema_strategy(
    raw_columns: list[dict],
    strategy: str,
    amount_names: set[str],
    qty_names: set[str],
    other_numeric_names: set[str],
    kpi_names: set[str] | None = None,
) -> list[dict]:
    """Return a copy of raw_columns with strategy-adjusted summarizeBy.

    Only ``summarizeBy`` may change; ``name``, ``dataType``, and
    ``sourceColumn`` are never modified so downstream measure expressions
    never produce ghost column references.

    Strategies
    ----------
    conservative
        Keep every column exactly as inferred by read_csv_schema.
    analytical
        Force ``summarizeBy = "sum"`` on amount and qty columns so that
        Power BI Desktop automatically sums revenue/volume KPIs.
    categorical
        Force ``summarizeBy = "none"`` on other_numeric columns (likely
        IDs, codes, indices) to prevent meaningless aggregations.
    kpi_focused  (NEW)
        Force ``summarizeBy = "sum"`` on columns whose names match any
        KPI identified by DataAnalyzerAgent, and "none" on all other
        numeric columns. Maximises KPI semantic alignment.
    relationship_aware  (NEW)
        Force ``summarizeBy = "none"`` on likely foreign-key / ID columns
        (suffix 'id', 'key', 'fk', 'code', 'num') while applying "sum"
        on pure amount columns. Optimises for multi-table relationship
        detection by RelationshipAgent.
    """
    _FK_HINTS = ("id", "key", "fk", "code", "num", "seq", "ref", "pk")
    kpi_lowers: set[str] = {k.lower() for k in (kpi_names or set())}

    result = []
    for c in raw_columns:
        col = dict(c)  # shallow copy — only mutating summarizeBy
        lname = col["name"].lower()

        if strategy == "analytical":
            if col["name"] in amount_names or col["name"] in qty_names:
                col["summarizeBy"] = "sum"

        elif strategy == "categorical":
            if col["name"] in other_numeric_names:
                col["summarizeBy"] = "none"

        elif strategy == "kpi_focused":
            # Match column name to any KPI keyword — promote those to "sum"
            if kpi_lowers and any(kw in lname for kw in kpi_lowers):
                col["summarizeBy"] = "sum"
            elif col["name"] in amount_names:
                col["summarizeBy"] = "sum"
            elif col["name"] in other_numeric_names:
                col["summarizeBy"] = "none"

        elif strategy == "relationship_aware":
            # Demote FK/PK/code columns to "none" (they are join keys, not KPIs)
            if any(lname.endswith(h) or lname.startswith(h) for h in _FK_HINTS):
                col["summarizeBy"] = "none"
            elif col["name"] in amount_names:
                col["summarizeBy"] = "sum"
            # qty columns: keep existing summarization (already "sum" from inference)

        elif strategy == "aggressive_numeric":
            # Force "sum" on every numeric column regardless of classification.
            # Maximises data_coverage when schema complexity is high and KPIs
            # are uncertain.  Generated only when candidate_count > 5.
            if col.get("dataType", "") in _NUMERIC_TYPES:
                col["summarizeBy"] = "sum"

        elif strategy == "minimal_aggregation":
            # Set summarizeBy = "none" on every column.
            # Produces a clean baseline where Power BI Desktop will not
            # auto-aggregate anything, leaving all aggregation to DAX measures.
            # Generated only when candidate_count > 6.
            col["summarizeBy"] = "none"

        # "conservative": no changes
        result.append(col)
    return result


def _safe_project_name(raw: str) -> str:
    """Turn a business description (or table name) into a PBIP-safe project name.

    Power BI project folder names must be filesystem-safe and cannot collide
    with reserved suffixes (``.SemanticModel`` / ``.Report``).
    """
    # take the first few words of the description, title-cased
    words = re.findall(r"[A-Za-z0-9]+", raw)
    if not words:
        name = "PowerBIProject"
    else:
        name = "".join(w[:1].upper() + w[1:] for w in words[:4])
    # strip any non-identifier chars and ensure it does not start with a digit
    name = _TMDL_SAFE.sub("", name) or "PowerBIProject"
    if name[0].isdigit():
        name = "P" + name
    return name


class SchemaAgent(BaseAgent):
    """Infers the schema and writes the first TMDL table."""

    name = "SchemaAgent"
    description = (
        "You are the SchemaAgent. Read the user's data file, infer the Power BI "
        "table schema (columns, data types, summarization), and write a TMDL "
        "table definition. You produce the data model that downstream agents "
        "build DAX measures and reports on top of."
    )

    def _infer_all_tables(self, ctx, primary_schema: dict) -> list[dict]:
        """Collect every table available for relationship detection.

        In create mode the primary table is read above; this gathers *extra*
        tables so RelationshipAgent can detect cross-table FKs without needing
        edit_pbip mode. Sources of extra tables:
          * Excel: every non-empty sheet (via infer_all_sheets)
          * A directory of CSV/JSON files: each file becomes a table
        Returns ``[primary_schema]`` (+ extras) so ``all_tables`` is never empty.
        """
        tables: list[dict] = [primary_schema]
        src = ctx.source_path
        try:
            if src.is_file() and src.suffix.lower() in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
                from utils.excel_reader import infer_all_sheets
                for s in infer_all_sheets(src):
                    # skip the sheet we already used as the primary table
                    if s.get("table_name") != primary_schema.get("table_name"):
                        tables.append(s)
            elif src.is_dir():
                for f in sorted(src.iterdir()):
                    if f.is_file() and f.suffix.lower() in {".csv", ".json"}:
                        r = ctx.toolbox.read_csv_schema(str(f))
                        if r.ok:
                            tables.append(r.data["schema"])
        except Exception as exc:  # noqa: BLE001 - extra tables are best-effort
            self.log.warning(f"multi-table inference skipped: {exc}")
        return tables

    def _run(self) -> AgentResult:
        ctx = self.context

        # 1) pick a project name from the business description (idempotent-ish)
        if not getattr(ctx, "project_name", None):
            ctx.project_name = _safe_project_name(ctx.business_description)
        self.log.info(f"project_name = {ctx.project_name}")

        # 2) read the schema via MCP tool (supports CSV, JSON, Excel)
        extra_kwargs: dict = {}
        if ctx.input_mode == "edit_excel":
            extra_kwargs["sheet_name"] = ctx.extra.get("excel_sheet")
        read = ctx.toolbox.read_csv_schema(str(ctx.source_path), **extra_kwargs)
        if not read.ok:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Schema inference failed: {read.message}",
                errors=read.errors,
            )
        schema = read.data["schema"]

        # 3) multi-candidate schema mapping — generate 5 alternative column
        #    interpretations, score each with the blended semantic+heuristic
        #    utility model, then tournament-select the best-scoring one.
        #    Only ``summarizeBy`` varies; column names and dataTypes never change
        #    so downstream agents are never exposed to ghost column references.
        from agents.dax_agent import _classify_columns

        raw_cols = schema["columns"]
        buckets_s = _classify_columns(raw_cols)
        amount_names  = {c["name"] for c in buckets_s["amount"]}
        qty_names     = {c["name"] for c in buckets_s["qty"]}
        other_n_names = {c["name"] for c in buckets_s["other_numeric"]}

        biz_s = ctx.extra.get("business_analysis")
        weights_s = dict(ctx.extra.get("scoring_weights") or DEFAULT_WEIGHTS)  # type: ignore[arg-type]
        biz_desc = ctx.business_description or ""

        # KPI names from DataAnalyzerAgent for kpi_focused strategy
        kpi_names_s: set[str] = set()
        if biz_s is not None:
            kpi_names_s = set(getattr(biz_s, "potential_kpis", []) or [])

        base_cols = [
            {
                "name": c["name"],
                "dataType": c["dataType"],
                "summarizeBy": c.get("summarizeBy", "none"),
                "sourceColumn": c.get("sourceColumn", c["name"]),
            }
            for c in raw_cols
        ]

        cols_conservative        = _apply_schema_strategy(
            base_cols, "conservative",        amount_names, qty_names, other_n_names
        )
        cols_analytical          = _apply_schema_strategy(
            base_cols, "analytical",          amount_names, qty_names, other_n_names
        )
        cols_categorical         = _apply_schema_strategy(
            base_cols, "categorical",         amount_names, qty_names, other_n_names
        )
        cols_kpi_focused         = _apply_schema_strategy(
            base_cols, "kpi_focused",         amount_names, qty_names, other_n_names,
            kpi_names=kpi_names_s,
        )
        cols_relationship_aware  = _apply_schema_strategy(
            base_cols, "relationship_aware",  amount_names, qty_names, other_n_names
        )

        # Adaptive candidate count + bias (injected by orchestrator after
        # complexity scoring; defaults preserve prior behaviour exactly).
        candidate_count: int = int(ctx.extra.get("candidate_count", 5))
        adaptive_bias: float = float(ctx.extra.get("adaptive_bias", 0.0))

        all_schema_candidates = [
            cols_conservative, cols_analytical, cols_categorical,
            cols_kpi_focused,  cols_relationship_aware,
        ]
        schema_candidate_ids = [
            "conservative", "analytical", "categorical",
            "kpi_focused",  "relationship_aware",
        ]

        # Expanded pool for medium/high-complexity inputs
        if candidate_count > 5:
            # aggressive_numeric: SUM on every numeric column — max data_coverage
            cols_aggressive = _apply_schema_strategy(
                base_cols, "aggressive_numeric", amount_names, qty_names, other_n_names
            )
            all_schema_candidates.append(cols_aggressive)
            schema_candidate_ids.append("aggressive_numeric")

        if candidate_count > 6:
            # minimal_aggregation: all "none" — let DAX measures own aggregation
            cols_minimal = _apply_schema_strategy(
                base_cols, "minimal_aggregation", amount_names, qty_names, other_n_names
            )
            all_schema_candidates.append(cols_minimal)
            schema_candidate_ids.append("minimal_aggregation")

        # Strategy Synthesis Layer (additive) — inject any schema strategies
        # the orchestrator synthesized from failure/judge signals (empty by
        # default, so behaviour is byte-identical when no gap was detected).
        for _spec in (ctx.extra.get("synthesized_strategies", {}).get("schema") or []):
            try:
                from utils.strategy_synthesizer import apply_schema_strategy_spec
                synth_cols = apply_schema_strategy_spec(
                    _spec, base_cols, amount_names, qty_names, other_n_names,
                )
                if synth_cols:
                    all_schema_candidates.append(synth_cols)
                    schema_candidate_ids.append(_spec["strategy_id"])
            except Exception as _synth_exc:  # noqa: BLE001 — fail-safe
                self.log.warning(
                    f"synthesized schema strategy '{_spec.get('strategy_id')}' skipped: {_synth_exc}"
                )

        schema_scores = [
            score_schema_candidate(
                cid, cols, biz_s, weights_s, biz_desc,
                adaptive_bias=adaptive_bias,
            )
            for cid, cols in zip(schema_candidate_ids, all_schema_candidates)
        ]

        _kpi_score_map_s: dict[str, float] = {
            s.candidate_id: s.semantic.kpi_semantic_alignment
            for s in schema_scores
        }
        best_s_idx, best_s_score, rejected_s = tournament_select(
            all_schema_candidates, schema_scores,
            context_aware=(candidate_count > 5),
            kpi_scores=_kpi_score_map_s,
        )
        columns = all_schema_candidates[best_s_idx]

        log_decision(
            agent=self.name,
            decision_type="measure_rationale",
            subject="schema_strategy_selection",
            rationale=(
                f"Tournament-selected schema strategy '{best_s_score.candidate_id}' "
                f"(final={best_s_score.total:.3f}, "
                f"semantic={best_s_score.semantic_total:.3f}, "
                f"heuristic={best_s_score.heuristic_total:.3f}). "
                + "Rejected: "
                + ", ".join(f"'{r['candidate_id']}'={r['score']:.3f}" for r in rejected_s)
                + f". summarizeBy adjusted for {len(amount_names)} amount + "
                f"{len(qty_names)} qty + {len(other_n_names)} other-numeric columns."
            ),
            confidence=best_s_score.total,
            extra={
                "selected":  best_s_score.as_dict(),
                "rejected":  rejected_s,
                "strategy":  f"tournament_{len(all_schema_candidates)}_candidates",
            },
        )
        ctx.extra["schema_candidates"] = [s.as_dict() for s in schema_scores]
        # Derive source_type from the actual file suffix (was hardcoded "csv",
        # which was wrong for JSON/Excel inputs). edit_pbip/edit_pbix read from
        # TMDL so there is no file suffix; fall back to "csv" there.
        suffix = ctx.source_path.suffix.lower()
        if suffix in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
            source_type = "excel"
        elif suffix == ".json":
            source_type = "json"
        else:
            source_type = "csv"
        table_def = {
            "name": schema["table_name"],
            "columns": columns,
            "measures": [],  # DAXAgent appends these
            # Pass absolute path so write_tmdl_table can generate an M partition.
            # Without a partition, Desktop rejects the table (PFE_TM_TABLE_NO_PARTITIONS).
            "source_path": str(ctx.source_path),
            "source_type": source_type,
        }

        # log type-inference decision for each column
        for c in columns:
            log_decision(
                agent=self.name,
                decision_type="measure_rationale",
                subject=c["name"],
                rationale=(
                    f"Column '{c['name']}' inferred as dataType='{c['dataType']}', "
                    f"summarizeBy='{c['summarizeBy']}'."
                ),
                confidence=0.85,
            )

        # 4) write the TMDL table via MCP tool
        write = ctx.toolbox.write_tmdl_table(ctx.sm_definition_rel, table_def)
        if not write.ok:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"write_tmdl_table failed: {write.message}",
                errors=write.errors,
            )

        # 5) share the schema with downstream agents
        ctx.schema = schema
        ctx.schema["all_tables"] = self._infer_all_tables(ctx, schema)
        ctx.extra["table_definition"] = table_def
        ctx.extra["tmdl_table_path"] = write.data.get("path")

        extra_count = len(ctx.schema["all_tables"]) - 1
        self.log.info(
            f"inferred table '{schema['table_name']}' with {len(columns)} columns; "
            f"types={ {c['name']: c['dataType'] for c in columns} }"
            + (f"; +{extra_count} extra table(s) for relationships" if extra_count else "")
        )

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Inferred schema for '{schema['table_name']}' "
                f"({len(columns)} columns) and wrote TMDL."
            ),
            data={
                "table_name": schema["table_name"],
                "column_count": len(columns),
                "columns": [{"name": c["name"], "dataType": c["dataType"]} for c in columns],
            },
        )
