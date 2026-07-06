"""RelationshipAgent -- detects and writes cross-table relationships.

Role
----
Runs after SchemaAgent (when multiple tables are present) and before DAXAgent.
It:

1. Inspects column names across all tables in ctx.schema["all_tables"] for
   FK-style patterns (e.g. CustomerId → Customers.Id, RegionCode → Region.Code).
2. In edit_pbip mode, also reads existing relationships.tmdl to avoid duplication.
3. Writes ``definition/relationships.tmdl`` with the detected relationships.
4. Stores detected relationships in ctx.extra["relationships"] for downstream use.

Heuristics used (no LLM required):
  - Column name ends with suffix of another table's PK: Orders.CustomerId → Customers.Id
  - Exact name match between tables: Orders.Region → Region.Region (or Id)
  - Common FK patterns: *Id, *Key, *Code, *No matching primary table names

MCP tools used: none (writes files directly).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent
from utils import atomic_write_text, ensure_dir
from utils.explainability import log_decision
from utils import pbip_paths as paths
from utils.identifiers import quote_tmdl_identifier, tmdl_column_ref


# column name suffixes that suggest FK
_FK_SUFFIXES = ("Id", "Key", "Code", "No", "Number", "Ref")


def _norm(name: str) -> str:
    """Normalise column name for matching: lowercase, strip common suffixes."""
    n = name.lower()
    for suffix in ("id", "key", "code", "no", "number", "ref"):
        if n.endswith(suffix) and len(n) > len(suffix):
            return n[: -len(suffix)]
    return n


def _likely_pk(columns: list[dict[str, Any]]) -> str | None:
    """Find the most likely primary key column in a table."""
    for col in columns:
        name = col["name"].lower()
        if name in {"id", "key", "code"} or name.endswith("id"):
            return col["name"]
    # fallback: first string or int column (no name filter) — a best-effort
    # PK guess when no column is conventionally named id/key/code.
    for col in columns:
        if col["dataType"] in {"int64", "string"}:
            return col["name"]
    return None


def detect_relationships(
    tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of relationship dicts from a list of table schema dicts.

    Each returned dict:
      {"from_table", "from_column", "to_table", "to_column",
       "to_cardinality": "one" (default)}
    """
    if len(tables) < 2:
        return []

    # build index: normalised table name → table dict
    table_index: dict[str, dict[str, Any]] = {}
    for t in tables:
        table_index[t["table_name"].lower()] = t
        # also index by stem (e.g. "Customer" from "Customers"). Use
        # removesuffix so only a SINGLE trailing 's' is stripped -- rstrip("s")
        # strips a character *set* and mangles names like "Address"/"Class".
        stem = t["table_name"].removesuffix("s").lower()
        if stem not in table_index:
            table_index[stem] = t

    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for fact_table in tables:
        for col in fact_table["columns"]:
            cname = col["name"]
            cnorm = _norm(cname)

            # try to match normalised column name against table names
            for tname, dim_table in table_index.items():
                if dim_table["table_name"] == fact_table["table_name"]:
                    continue
                if cnorm == tname or cnorm == dim_table["table_name"].lower():
                    pk = _likely_pk(dim_table["columns"])
                    if not pk:
                        continue
                    key = (fact_table["table_name"], cname)
                    if key in seen:
                        continue
                    seen.add(key)
                    relationships.append({
                        "from_table": fact_table["table_name"],
                        "from_column": cname,
                        "to_table": dim_table["table_name"],
                        "to_column": pk,
                        "to_cardinality": "one",
                    })
                    break

    return relationships


def _write_relationships_tmdl(
    rel_dir: Path, relationships: list[dict[str, Any]]
) -> Path:
    """Write definition/relationships.tmdl and return its path."""
    ensure_dir(rel_dir)
    target = rel_dir / "relationships.tmdl"

    lines: list[str] = []
    for rel in relationships:
        # TMDL relationship header must be a descriptive 'TableA to TableB' form,
        # NOT a bare guid (the old `relationship {guid}` syntax is invalid TMDL
        # and Power BI Desktop rejects it). Table names are single-quote
        # escaped via the helper so spaces/' are handled safely.
        from_table = quote_tmdl_identifier(rel["from_table"])
        to_table = quote_tmdl_identifier(rel["to_table"])
        lines.append(f"relationship {from_table} to {to_table}")
        lines.append(f"\tfromColumn: {tmdl_column_ref(rel['from_table'], rel['from_column'])}")
        lines.append(f"\ttoColumn: {tmdl_column_ref(rel['to_table'], rel['to_column'])}")
        # to_cardinality defaults to many-to-one in TMDL; "one" needs no extra
        # property, so there is nothing to emit here.
        lines.append("")

    atomic_write_text(target, "\n".join(lines))
    return target


class RelationshipAgent(BaseAgent):
    """Detects FK relationships between tables and writes relationships.tmdl."""

    name = "RelationshipAgent"
    description = (
        "You are the RelationshipAgent. Inspect column names across all tables "
        "in the data model to detect foreign-key relationships. Write a valid "
        "relationships.tmdl file so Power BI Desktop can link tables correctly."
    )

    def _ensure_extra_table_tmdl(self, ctx, all_tables: list[dict[str, Any]]) -> None:
        """Write TMDL for any table in ``all_tables`` that lacks one.

        Best-effort: in create mode, SchemaAgent only wrote the primary table.
        Extra tables discovered for relationship detection need their own
        TMDL file (with an M partition pointing at the same source file) so
        Power BI Desktop can open the model. Skips tables that already have a
        .tmdl file (edit_pbip mode) and skips the primary table.
        """
        if not ctx.schema:
            return
        primary = ctx.schema.get("table_name")
        sm_def_rel = ctx.sm_definition_rel
        tables_dir = paths.sm_tables_dir(
            paths.sm_root(ctx.pbip_root, ctx.project_name)
        )
        for t in all_tables:
            tname = t.get("table_name")
            if not tname or tname == primary:
                continue
            # skip if the TMDL already exists (edit mode wrote it)
            if (tables_dir / f"{tname}.tmdl").is_file():
                continue
            cols = [
                {
                    "name": c["name"],
                    "dataType": c.get("dataType", "string"),
                    "summarizeBy": c.get("summarizeBy", "none"),
                    "sourceColumn": c.get("sourceColumn", c["name"]),
                }
                for c in t.get("columns", [])
            ]
            table_def = {
                "name": tname,
                "columns": cols,
                "measures": [],
                # Reuse the primary source path; the extra table shares the
                # workbook / folder. A real multi-source model would need per-
                # table paths, which the LLM relationship flow can refine.
                "source_path": str(ctx.source_path),
                "source_type": "csv",
            }
            try:
                w = ctx.toolbox.write_tmdl_table(sm_def_rel, table_def)
                if w.ok:
                    self.log.info(f"wrote extra table TMDL for '{tname}'")
                else:
                    self.log.warning(f"extra table '{tname}' write failed: {w.message}")
            except Exception as exc:  # noqa: BLE001 - best-effort
                self.log.warning(f"extra table '{tname}' write raised: {exc}")

    def _run(self) -> AgentResult:
        ctx = self.context

        if not ctx.schema:
            return AgentResult(
                agent=self.name, ok=True,
                message="No schema — skipping relationship detection.",
                data={"relationships": []},
            )

        # gather all tables: primary + any extras from edit mode
        all_tables: list[dict[str, Any]] = ctx.schema.get("all_tables", [])
        if not all_tables:
            # single-table create mode: no cross-table relationships possible
            ctx.extra["relationships"] = []
            return AgentResult(
                agent=self.name, ok=True,
                message="Single-table model — no relationships to detect.",
                data={"relationships": [], "table_count": 1},
            )

        if len(all_tables) < 2:
            ctx.extra["relationships"] = []
            return AgentResult(
                agent=self.name, ok=True,
                message=f"Only {len(all_tables)} table — no relationships to detect.",
                data={"relationships": [], "table_count": len(all_tables)},
            )

        relationships = detect_relationships(all_tables)

        # log each heuristic relationship inference
        for rel in relationships:
            log_decision(
                agent=self.name,
                decision_type="relationship_inferred",
                subject=f"{rel['from_table']}.{rel['from_column']} → {rel['to_table']}.{rel['to_column']}",
                rationale=(
                    f"Heuristic FK match: column name '{rel['from_column']}' "
                    f"normalises to table name '{rel['to_table']}'; "
                    f"cardinality=many-to-one."
                ),
                confidence=0.75,
                extra={"from_table": rel["from_table"], "to_table": rel["to_table"]},
            )

        if not relationships:
            log_decision(
                agent=self.name,
                decision_type="relationship_inferred",
                subject="no_relationships",
                rationale=(
                    f"Heuristic FK scan across {len(all_tables)} table(s) found no matches. "
                    "No relationships written."
                ),
                confidence=1.0,
            )

        # Phase 3 — LLM refinement is now a STANDARD part of the pipeline (not
        # optional). refine_relationships (rewritten in Phase 1) retries
        # transient errors and validates the response against the
        # RelationshipSet schema; with no API key it returns the heuristic
        # unchanged so the offline baseline stays byte-identical (fail-safe).
        from utils.llm_client import refine_relationships
        refined = refine_relationships(all_tables, relationships)
        if refined is not relationships and refined != relationships:
            self.log.info(
                f"LLM refined relationships: {len(relationships)} -> {len(refined)}"
            )
            log_decision(
                agent=self.name,
                decision_type="relationship_inferred",
                subject="llm_refinement",
                rationale=(
                    f"LLM refined heuristic result from {len(relationships)} "
                    f"to {len(refined)} relationship(s)."
                ),
                confidence=0.85,
                extra={"before": len(relationships), "after": len(refined)},
            )
            relationships = refined

        # In create mode, extra tables (e.g. other Excel sheets / CSVs in a
        # folder) were collected by SchemaAgent but only the PRIMARY table has
        # a TMDL file yet. Write the missing table TMDLs so relationships
        # reference real tables, otherwise Power BI Desktop rejects the model.
        self._ensure_extra_table_tmdl(ctx, all_tables)

        if relationships:
            sm_dir = paths.sm_root(ctx.pbip_root, ctx.project_name)
            def_dir = paths.sm_definition(sm_dir)
            tmdl_path = _write_relationships_tmdl(def_dir, relationships)
            self.log.info(
                f"wrote {len(relationships)} relationship(s) to {tmdl_path}"
            )

        ctx.extra["relationships"] = relationships

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Detected {len(relationships)} relationship(s) "
                f"across {len(all_tables)} tables."
                if relationships
                else f"No relationships found across {len(all_tables)} tables."
            ),
            data={
                "relationships": relationships,
                "table_count": len(all_tables),
            },
        )
