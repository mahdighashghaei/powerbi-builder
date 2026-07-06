"""ADK tool: detect_and_write_relationships + write_single_relationship.

Ported from agents/relationship_agent.py with format fix:
  WRONG: relationship {uuid}
  RIGHT: relationship 'TableA to TableB'   (named, per tmdl-guidelines)

Column names with spaces are single-quoted: Table.'Column Name'
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import atomic_write_text, ensure_dir
from utils.security import safe_join
from utils.identifiers import quote_tmdl_identifier, tmdl_column_ref

_FK_SUFFIXES = ("Id", "Key", "Code", "No", "Number", "Ref")


def _norm(name: str) -> str:
    n = name.lower()
    for s in ("id", "key", "code", "no", "number", "ref"):
        if n.endswith(s) and len(n) > len(s):
            return n[:-len(s)]
    return n


def _likely_pk(columns: list[dict]) -> str | None:
    for col in columns:
        if col["name"].lower() in {"id", "key", "code"} or col["name"].lower().endswith("id"):
            return col["name"]
    for col in columns:
        if col.get("dataType") in {"int64", "string"}:
            return col["name"]
    return None


def _rel_block(rel: dict) -> list[str]:
    """Build correct TMDL lines for one relationship."""
    rel_name = f"{quote_tmdl_identifier(rel['from_table'])} to {quote_tmdl_identifier(rel['to_table'])}"
    from_ref = tmdl_column_ref(rel["from_table"], rel["from_column"])
    to_ref   = tmdl_column_ref(rel["to_table"], rel["to_column"])
    lines = [f"relationship {rel_name}", f"\tfromColumn: {from_ref}", f"\ttoColumn: {to_ref}"]
    if not rel.get("isActive", True):
        lines.append("\tisActive: false")
    return lines + [""]


def _write_relationships(target: Path, relationships: list[dict]) -> None:
    lines: list[str] = []
    for rel in relationships:
        lines.extend(_rel_block(rel))
    atomic_write_text(target, "\n".join(lines))


def write_single_relationship(
    output_dir: str,
    from_table: str,
    from_column: str,
    to_table: str,
    to_column: str,
    is_active: bool = True,
    output_root: str = "./output",
) -> dict:
    """Write (or append) a single relationship to relationships.tmdl.

    Used by write_date_table to add the Date→Fact relationship after
    creating the Date dimension table.

    Args:
        from_table/from_column: many-side (fact table)
        to_table/to_column:     one-side (dimension table)
        is_active: False for role-playing dimensions (USERELATIONSHIP in DAX)
    """
    rel = {
        "from_table": from_table, "from_column": from_column,
        "to_table":   to_table,   "to_column":   to_column,
        "isActive":   is_active,
    }
    try:
        root    = Path(output_root).expanduser().resolve()
        rel_dir = safe_join(root, output_dir)
        ensure_dir(rel_dir)
        target  = rel_dir / "relationships.tmdl"

        # Append to existing file if present, else create
        if target.exists():
            existing_text = target.read_text(encoding="utf-8")
            # Simple check: skip if this exact relationship already exists
            if f"{from_table}.{from_column}" in existing_text:
                return {"ok": True, "message": "Relationship already exists.", "count": 0}

        new_lines = _rel_block(rel)
        if target.exists():
            with open(target, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n" + "\n".join(new_lines))
        else:
            atomic_write_text(target, "\n".join(new_lines))

        return {
            "ok": True,
            "file": str(target),
            "relationship": f"{from_table}[{from_column}] -> {to_table}[{to_column}]",
            "count": 1,
        }
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "count": 0}


def detect_and_write_relationships(
    schemas: list[dict],
    output_dir: str,
    output_root: str = "./output",
) -> dict:
    """Detect FK relationships between tables and write relationships.tmdl.

    Also detects Date table relationships: if a table named 'Date' exists,
    looks for matching datetime columns in fact tables.

    TMDL format (correct):
        relationship 'TableA to TableB'
            fromColumn: TableA.ColumnA
            toColumn: TableB.ColumnB

    Args:
        schemas: list of schema dicts from read_csv_schema
        output_dir: semantic model definition dir
        output_root: base output folder
    """
    if len(schemas) < 2:
        return {
            "ok": True,
            "relationships": [],
            "count": 0,
            "message": "Only one table — no cross-table relationships possible.",
        }

    idx: dict[str, dict] = {}
    for t in schemas:
        tname = t["table_name"]
        idx[tname.lower()] = t
        # removesuffix strips a single trailing 's' (rstrip("s") strips a
        # character set and corrupts "Address"->"Addre", "Class"->"Cla").
        stem = tname.removesuffix("s").lower()
        if stem not in idx:
            idx[stem] = t

    relationships: list[dict] = []
    seen: set[tuple] = set()

    for fact in schemas:
        for col in fact["columns"]:
            cname = col["name"]
            cnorm = _norm(cname)

            # Special case: Date table relationship
            # If there's a 'Date' table and this column is dateTime + named 'Date'
            date_schema = idx.get("date")
            if (date_schema and date_schema["table_name"] != fact["table_name"]
                    and col.get("dataType") in {"dateTime", "date"}
                    and cname.lower() == "date"):
                date_pk = next(
                    (c["name"] for c in date_schema["columns"]
                     if c["name"].lower() == "date"),
                    None,
                )
                if date_pk:
                    key = (fact["table_name"], cname)
                    if key not in seen:
                        seen.add(key)
                        relationships.append({
                            "from_table":    fact["table_name"],
                            "from_column":   cname,
                            "to_table":      date_schema["table_name"],
                            "to_column":     date_pk,
                            "to_cardinality": "one",
                        })
                    continue

            # Standard FK suffix detection
            for tkey, dim in idx.items():
                if dim["table_name"] == fact["table_name"]:
                    continue
                if cnorm in (tkey, dim["table_name"].lower()):
                    pk = _likely_pk(dim["columns"])
                    if not pk:
                        continue
                    key = (fact["table_name"], cname)
                    if key in seen:
                        continue
                    seen.add(key)
                    relationships.append({
                        "from_table":    fact["table_name"],
                        "from_column":   cname,
                        "to_table":      dim["table_name"],
                        "to_column":     pk,
                        "to_cardinality": "one",
                    })
                    break

    if not relationships:
        return {
            "ok": True,
            "relationships": [],
            "count": 0,
            "message": "No relationships detected.",
        }

    try:
        root    = Path(output_root).expanduser().resolve()
        rel_dir = safe_join(root, output_dir)
        ensure_dir(rel_dir)
        target  = rel_dir / "relationships.tmdl"
        _write_relationships(target, relationships)

        return {
            "ok": True,
            "relationships": relationships,
            "count": len(relationships),
            "file": str(target),
            "message": f"Detected and wrote {len(relationships)} relationships.",
        }
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "count": 0}
