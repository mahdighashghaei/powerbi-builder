"""ADK tools for auto Date dimension table generation.

Wraps utils/date_table.py — generates a complete Power BI Date table
(18 calendar columns, M partition using CALENDAR over fact table range,
mark-as-date-table annotations) and writes it as Date.tmdl.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import atomic_write_text, ensure_dir
from utils.security import safe_join
from utils.date_table import build_date_table_tmdl, needs_date_table
from .relationship_tools import write_single_relationship


def check_needs_date_table(schema: dict) -> dict:
    """Check whether a schema has a datetime column suitable for a Date table.

    Args:
        schema: the schema dict returned by read_csv_schema

    Returns:
        {"needs_date_table": bool, "fact_table": str, "date_column": str}
    """
    needed, fact, col = needs_date_table(schema)
    return {
        "needs_date_table": needed,
        "fact_table": fact,
        "date_column": col,
        "message": (
            f"DateTime column '{col}' found in '{fact}' — recommend creating Date table."
            if needed else
            "No datetime column found — Date table not needed."
        ),
    }


def write_date_table(
    output_dir: str,
    fact_table: str,
    date_column: str,
    fiscal_year_start_month: int = 1,
    auto_relationship: bool = True,
    output_root: str = "./output",
) -> dict:
    """Write a complete Date dimension TMDL file.

    Generates 18 calendar columns with a Power Query M partition that
    expands from MIN to MAX of the fact table's date column at refresh time.
    Also marks the table as a Date Table via TMDL annotations.

    Args:
        output_dir:   semantic model definition dir,
                      e.g. \"MyProject.SemanticModel/definition\"
        fact_table:   name of the fact table, e.g. \"SampleData\"
        date_column:  name of the datetime column, e.g. \"Date\"
        fiscal_year_start_month: 1-12, month when fiscal year starts (default 1 = Jan)
        auto_relationship: if True (default), also writes the Date[Date] → fact_table[date_column]
                           relationship to relationships.tmdl
        output_root:  base output folder (default \"./output\")

    Returns:
        {"ok": bool, "file": str, "columns": int, "message": str}
    """
    try:
        tmdl_content = build_date_table_tmdl(
            fact_table=fact_table,
            date_column=date_column,
            fiscal_year_start_month=fiscal_year_start_month,
        )

        root      = Path(output_root).expanduser().resolve()
        tables_dir = safe_join(root, output_dir, "tables")
        ensure_dir(tables_dir)
        target = tables_dir / "Date.tmdl"
        atomic_write_text(target, tmdl_content)

        # Count columns from generated content
        col_count = tmdl_content.count("\n\tcolumn ")

        rel_result = None
        if auto_relationship:
            # Write relationship: fact_table[date_column] → Date[Date]  (many → one)
            rel_result = write_single_relationship(
                output_dir=output_dir,
                from_table=fact_table,
                from_column=date_column,
                to_table="Date",
                to_column="Date",
                output_root=output_root,
            )

        return {
            "ok": True,
            "file": str(target),
            "columns": col_count,
            "fact_table": fact_table,
            "date_column": date_column,
            "relationship_written": rel_result.get("ok") if rel_result else False,
            "message": (
                f"Wrote Date.tmdl ({col_count} columns) + "
                f"relationship {fact_table}[{date_column}] -> Date[Date]."
                if auto_relationship else
                f"Wrote Date.tmdl ({col_count} columns)."
            ),
        }
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "message": str(exc)}
