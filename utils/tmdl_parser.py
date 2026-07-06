"""TMDL text parser -- extracts schema information from TMDL files.

Parses Power BI TMDL (Tabular Model Definition Language) files without
external dependencies. Uses regex + indentation-based line-by-line parsing.

Returns dicts compatible with the schema format used by infer_csv_schema()
so ReadPBIPAgent can feed existing TMDL directly into the DAX/Report pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Connection type inference
# ---------------------------------------------------------------------------

def infer_connection_type(partition_source: str) -> str:
    """Detect the data source kind from an M expression string.

    Returns one of: "csv" | "excel" | "sql" | "sharepoint" | "web" | "other"
    """
    src = partition_source.lower()
    if "csv.document" in src or "file.contents" in src and ".csv" in src:
        return "csv"
    if "excel.workbook" in src or ".xlsx" in src or ".xls" in src:
        return "excel"
    if "sql.database" in src or "sql.databases" in src or "odbc.datasource" in src:
        return "sql"
    if "sharepoint" in src or "sharepoint.files" in src:
        return "sharepoint"
    if "web.contents" in src or "web.page" in src:
        return "web"
    return "other"


import re as _re  # noqa: E402 - used by parse_partition_m below

_SQL_DB_RE = _re.compile(r'Sql\.Database\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"', _re.IGNORECASE)
_SQL_QUERY_RE = _re.compile(r'Query\s*=\s*"([^"]*)"', _re.IGNORECASE)
_SQL_ITEM_RE = _re.compile(r'Item\s*=\s*"([^"]*)"', _re.IGNORECASE)


def parse_partition_m(m_expr: str) -> dict[str, Any]:
    """Extract structured connection info from an M partition expression.

    For SQL Server partitions, returns ``{server, database, query, table}``.
    For other types, returns what can be inferred (e.g. ``{url}`` for web,
    ``{path}`` for CSV/Excel). Always includes ``connection_type``.
    """
    conn_type = infer_connection_type(m_expr)
    info: dict[str, Any] = {"connection_type": conn_type}
    if conn_type == "sql":
        m = _SQL_DB_RE.search(m_expr)
        if m:
            info["server"] = m.group(1)
            info["database"] = m.group(2)
        mq = _SQL_QUERY_RE.search(m_expr)
        if mq:
            info["query"] = mq.group(1)
        mi = _SQL_ITEM_RE.search(m_expr)
        if mi:
            info["table"] = mi.group(1)
    elif conn_type in ("csv", "excel"):
        # extract the path from File.Contents("...")
        pm = _re.search(r'File\.Contents\s*\(\s*"([^"]*)"', m_expr, _re.IGNORECASE)
        if pm:
            info["path"] = pm.group(1)
    elif conn_type == "web":
        wm = _re.search(r'Web\.Contents\s*\(\s*"([^"]*)"', m_expr, _re.IGNORECASE)
        if wm:
            info["url"] = wm.group(1)
    return info


# ---------------------------------------------------------------------------
# Column + measure block parsers
# ---------------------------------------------------------------------------

def _count_leading_tabs(line: str) -> int:
    """Count the number of leading tab characters."""
    return len(line) - len(line.lstrip("\t"))


def _strip_quotes(s: str) -> str:
    """Remove surrounding single or double quotes from a TMDL name."""
    s = s.strip()
    if (s.startswith("'") and s.endswith("'")) or \
       (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    return s


# dataType values Power BI emits in TMDL
_VALID_TYPES = {"string", "int64", "double", "decimal", "boolean", "dateTime", "date"}


def _default_summarize(data_type: str) -> str:
    if data_type in {"int64", "double", "decimal"}:
        return "sum"
    return "none"


def parse_column_block(lines: list[str], start: int) -> dict[str, Any]:
    """Parse a 'column <Name>' block starting at lines[start].

    Returns a column dict compatible with the schema format:
      {"name", "dataType", "summarizeBy", "sourceColumn"}
    """
    header = lines[start].strip()
    # column Name  OR  column 'Quoted Name'
    m = re.match(r"column\s+(.+)$", header)
    name = _strip_quotes(m.group(1)) if m else header

    base_indent = _count_leading_tabs(lines[start])
    col: dict[str, Any] = {
        "name": name,
        "dataType": "string",
        "summarizeBy": "none",
        "sourceColumn": name,
        "isCalculated": False,
    }

    i = start + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = _count_leading_tabs(line)
        if indent <= base_indent:
            break
        stripped = line.strip()

        if stripped.startswith("dataType:"):
            dt = stripped.split(":", 1)[1].strip()
            if dt in _VALID_TYPES:
                col["dataType"] = dt
                col["summarizeBy"] = _default_summarize(dt)
        elif stripped.startswith("summarizeBy:"):
            col["summarizeBy"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("sourceColumn:"):
            col["sourceColumn"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("expression"):
            # A calculated column carries a DAX `expression = ...` block;
            # a regular source column never does. Flag it so the BPA
            # PERF_AVOID_CALCULATED_COLUMNS rule can actually fire.
            col["isCalculated"] = True
        i += 1

    return col


def parse_measure_block(lines: list[str], start: int) -> dict[str, Any]:
    """Parse a "measure 'Name' = ..." block starting at lines[start].

    Returns a measure dict:
      {"name", "expression", "formatString", "displayFolder"}
    """
    header = lines[start].strip()
    # measure 'Name' = expression  OR  measure "Name" = expression
    m = re.match(r"measure\s+'([^']+)'\s*=\s*(.*)", header)
    if not m:
        m = re.match(r'measure\s+"([^"]+)"\s*=\s*(.*)', header)
    if not m:
        return {}

    name = m.group(1)
    first_expr = m.group(2).strip()

    base_indent = _count_leading_tabs(lines[start])
    measure: dict[str, Any] = {
        "name": name,
        "expression": first_expr,
        "formatString": "",
        "displayFolder": "",
    }

    # Collect multi-line expression and properties
    expr_lines: list[str] = [first_expr] if first_expr else []
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        indent = _count_leading_tabs(line)
        if indent <= base_indent:
            break
        stripped = line.strip()

        if stripped.startswith("formatString:"):
            measure["formatString"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("displayFolder:"):
            measure["displayFolder"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("lineageTag:") or stripped.startswith("annotation "):
            pass  # skip metadata
        else:
            # continuation of DAX expression
            expr_lines.append(stripped)
        i += 1

    if expr_lines:
        measure["expression"] = "\n".join(expr_lines)
    return measure


# ---------------------------------------------------------------------------
# Partition source extractor
# ---------------------------------------------------------------------------

def _extract_partition_source(lines: list[str], start: int) -> str:
    """Extract the M source expression from a partition block."""
    base_indent = _count_leading_tabs(lines[start])
    source_lines: list[str] = []
    in_source = False
    i = start + 1

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        indent = _count_leading_tabs(line)
        if indent <= base_indent:
            break

        if stripped.startswith("source =") or stripped.startswith("source="):
            in_source = True
            after_eq = stripped.split("=", 1)[1].strip()
            if after_eq:
                source_lines.append(after_eq)
        elif in_source and indent > base_indent:
            source_lines.append(stripped)
        elif stripped.startswith("mode:") or stripped.startswith("queryGroup:"):
            pass  # skip, not source
        elif in_source:
            break

        i += 1

    return "\n".join(source_lines)


# ---------------------------------------------------------------------------
# Table TMDL parser
# ---------------------------------------------------------------------------

def parse_table_tmdl(text: str) -> dict[str, Any]:
    """Parse a complete table .tmdl file.

    Returns a dict compatible with the schema returned by infer_csv_schema():
    {
        "table_name": str,
        "columns": [{"name", "dataType", "summarizeBy", "sourceColumn"}, ...],
        "measures": [{"name", "expression", "formatString", "displayFolder"}, ...],
        "partition_source": str,   # raw M expression
        "connection_type": str,    # "csv" | "excel" | "sql" | "other"
        "row_count": 0,
    }
    """
    lines = text.splitlines()

    table_name = "Table"
    columns: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    partition_source = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith("//"):
            i += 1
            continue

        indent = _count_leading_tabs(line)

        # table declaration (indent 0)
        if indent == 0 and re.match(r"table\s+", stripped):
            m = re.match(r"table\s+(.+)$", stripped)
            if m:
                table_name = _strip_quotes(m.group(1))
            i += 1
            continue

        # column block (indent 1)
        if indent == 1 and re.match(r"column\s+", stripped):
            col = parse_column_block(lines, i)
            if col and col.get("name"):
                columns.append(col)
            # advance past the block
            i += 1
            while i < len(lines):
                if not lines[i].strip():
                    i += 1
                    continue
                if _count_leading_tabs(lines[i]) <= 1:
                    break
                i += 1
            continue

        # measure block (indent 1)
        if indent == 1 and re.match(r"measure\s+", stripped):
            meas = parse_measure_block(lines, i)
            if meas and meas.get("name"):
                measures.append(meas)
            i += 1
            while i < len(lines):
                if not lines[i].strip():
                    i += 1
                    continue
                if _count_leading_tabs(lines[i]) <= 1:
                    break
                i += 1
            continue

        # partition block (indent 1)
        if indent == 1 and re.match(r"partition\s+", stripped):
            src = _extract_partition_source(lines, i)
            if src:
                partition_source = src
            i += 1
            while i < len(lines):
                if not lines[i].strip():
                    i += 1
                    continue
                if _count_leading_tabs(lines[i]) <= 1:
                    break
                i += 1
            continue

        i += 1

    return {
        "table_name": table_name,
        "columns": columns,
        "measures": measures,
        "partition_source": partition_source,
        "connection_type": infer_connection_type(partition_source),
        "row_count": 0,
        "inferred_relationships": [],
    }


# ---------------------------------------------------------------------------
# SemanticModel folder reader
# ---------------------------------------------------------------------------

def read_semantic_model(sm_dir: str | Path) -> dict[str, Any]:
    """Read all TMDL files from a SemanticModel definition folder.

    Returns a merged schema representing the whole model:
    {
        "tables": [parse_table_tmdl(...), ...],
        "primary_table": str,       # first/main table name
        "all_columns": [...],       # columns from primary table
        "all_measures": [...],      # measures from ALL tables
        "measure_names": set[str],  # quick lookup set
    }
    """
    sm_path = Path(sm_dir)
    tables_dir = sm_path / "definition" / "tables"

    if not tables_dir.is_dir():
        raise FileNotFoundError(f"No tables/ dir in {sm_path}")

    tables: list[dict[str, Any]] = []
    for tmdl_file in sorted(tables_dir.glob("*.tmdl")):
        try:
            text = tmdl_file.read_text(encoding="utf-8")
            parsed = parse_table_tmdl(text)
            tables.append(parsed)
        except Exception:
            continue  # skip malformed files

    if not tables:
        raise ValueError(f"No parseable table TMDL files found in {tables_dir}")

    # Choose primary table: the data table that carries measures (the user's
    # fact table), not a Date/calendar table or a calculation group. Picking
    # the table with the most columns (the old heuristic) selected the auto
    # Date table (17 cols) over the real data table (5 cols), which made
    # build_report bind all rich-page visuals to Date — wrong + Desktop-rejected.
    # Heuristic: prefer the table with the most measures; tie-break on having
    # a data partition (not a calc-group); final fallback: most columns.
    def _table_score(t: dict[str, Any]) -> tuple[int, int, int]:
        has_measures = len(t.get("measures", []))
        has_data_partition = any(
            "calculationGroup" not in (p.get("source", "") or "")
            for p in t.get("partitions", [])
        )
        col_count = len(t.get("columns", []))
        # Skip Date/calender tables: they're support tables, not the fact table.
        is_date = t.get("table_name", "").lower() in {"date", "calendar", "dim_date"}
        is_calc_group = any("calculationGroup" in str(t.get(k, "")) for k in ("partitions",))
        if is_calc_group:
            return (-1, 0, col_count)
        if is_date and has_measures == 0:
            return (0, int(has_data_partition), col_count)
        return (has_measures, int(has_data_partition), col_count)

    primary = max(tables, key=_table_score)

    all_measures: list[dict[str, Any]] = []
    for t in tables:
        for m in t["measures"]:
            m["table"] = t["table_name"]
            all_measures.append(m)

    return {
        "tables": tables,
        "primary_table": primary["table_name"],
        "all_columns": primary["columns"],
        "all_measures": all_measures,
        "measure_names": {m["name"] for m in all_measures},
    }
