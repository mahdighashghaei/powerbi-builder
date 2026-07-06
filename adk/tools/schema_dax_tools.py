"""ADK tool: auto_suggest_measures — deterministic DAX measures from CSV schema.

Ported from agents/dax_agent.py (_classify_columns + _build_measures).
Works fully offline — no LLM needed. The ADK agent can call this first,
then enrich the result with suggest_dax_measures() for time intelligence etc.
"""
from __future__ import annotations

from adk.config import MAX_AUTO_MEASURES
from utils.identifiers import quote_dax_column, quote_dax_table

# ---------------------------------------------------------------------------
# Column classification hints (same as legacy DAXAgent)
# ---------------------------------------------------------------------------

_AMOUNT_HINTS  = ("amount","revenue","sales","price","cost","value","total","profit","discount","gross")
_QTY_HINTS     = ("quantity","qty","count","units","volume","sold")
_DATE_HINTS    = ("date","month","year","period")
_REGION_HINTS  = ("region","country","state","city","market","territory")
_CATEGORY_HINTS= ("product","category","segment","type","brand","department")
_PCT_HINTS     = ("rate","ratio","margin","percent")

_CURRENCY_FMT  = "#,0.00"
_INT_FMT       = "#,0"
_PCT_FMT       = "0.00%"


def _classify(columns: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {
        "amount":[], "qty":[], "date":[],
        "region":[], "category":[], "pct":[],
        "other_numeric":[], "other":[],
    }
    for c in columns:
        ln   = c["name"].lower()
        dt   = c.get("dataType","")
        num  = dt in {"double","decimal","int64"}
        if any(h in ln for h in _AMOUNT_HINTS) and num:
            buckets["amount"].append(c)
        elif any(h in ln for h in _QTY_HINTS) and num:
            buckets["qty"].append(c)
        elif any(h in ln for h in _DATE_HINTS) or dt in {"dateTime","date"}:
            buckets["date"].append(c)
        elif any(h in ln for h in _PCT_HINTS) and num:
            buckets["pct"].append(c)
        elif any(h in ln for h in _REGION_HINTS):
            buckets["region"].append(c)
        elif any(h in ln for h in _CATEGORY_HINTS):
            buckets["category"].append(c)
        elif num:
            buckets["other_numeric"].append(c)
        else:
            buckets["other"].append(c)
    return buckets


def _m(name, expr, folder, fmt, table):
    return {"name": name, "expression": expr.strip(),
            "displayFolder": folder, "formatString": fmt, "table": table}


def _build(table: str, buckets: dict) -> list[dict]:
    measures: list[dict] = []
    qt = quote_dax_table(table)

    # helper: fully-qualified, safely-quoted column reference
    def ref(col: str) -> str:
        return quote_dax_column(table, col)

    # Revenue: SUM + AVG for first 2 amount columns
    for col in buckets["amount"][:2]:
        cn = col["name"]
        measures.append(_m(f"Total {cn}", f"SUM({ref(cn)})", "Revenue", _CURRENCY_FMT, table))
        measures.append(_m(f"Avg {cn}",   f"AVERAGE({ref(cn)})", "Revenue", _CURRENCY_FMT, table))

    # Orders: quantity totals
    for col in buckets["qty"][:1]:
        cn = col["name"]
        measures.append(_m(f"Total {cn}", f"SUM({ref(cn)})", "Orders", _INT_FMT, table))

    # Always: row count
    measures.append(_m("Row Count", f"COUNTROWS({qt})", "Orders", _INT_FMT, table))

    # Distinct count of first category column
    if buckets["category"]:
        cat = buckets["category"][0]["name"]
        measures.append(_m(f"Distinct {cat}", f"DISTINCTCOUNT({ref(cat)})", "Orders", _INT_FMT, table))

    # Revenue per order ratio
    if buckets["amount"]:
        amt = buckets["amount"][0]["name"]
        measures.append(_m(
            f"Avg {amt} per Row",
            f"DIVIDE(SUM({ref(amt)}), COUNTROWS({qt}), 0)",
            "Revenue", _CURRENCY_FMT, table,
        ))

    # Time intelligence on first amount + first date column
    if buckets["amount"] and buckets["date"]:
        amt  = buckets["amount"][0]["name"]
        dcol = buckets["date"][0]["name"]
        measures += [
            _m(f"{amt} YTD",
               f"TOTALYTD(SUM({ref(amt)}), {ref(dcol)})",
               "Dates", _CURRENCY_FMT, table),
            _m(f"{amt} PY",
               f"CALCULATE(SUM({ref(amt)}), SAMEPERIODLASTYEAR({ref(dcol)}))",
               "Dates", _CURRENCY_FMT, table),
            _m(f"{amt} YoY %",
               f"VAR _c = SUM({ref(amt)}) VAR _p = CALCULATE(SUM({ref(amt)}), SAMEPERIODLASTYEAR({ref(dcol)})) RETURN DIVIDE(_c - _p, _p)",
               "Dates", _PCT_FMT, table),
        ]

    # Fallback: add stats if still < 5
    if len(measures) < 5:
        numeric = buckets["amount"] + buckets["qty"] + buckets["other_numeric"]
        if numeric:
            col = numeric[0]["name"]
            measures.append(_m(f"Min {col}", f"MIN({ref(col)})", "Stats", _INT_FMT, table))
            measures.append(_m(f"Max {col}", f"MAX({ref(col)})", "Stats", _INT_FMT, table))
        measures.append(_m("Records All", f"COUNTROWS(ALL({qt}))", "Stats", _INT_FMT, table))

    return measures[:MAX_AUTO_MEASURES]


def auto_suggest_measures(
    schema: dict,
    existing_measures: list[str] | None = None,
) -> dict:
    """Generate 5-10 DAX measures automatically from a CSV schema dict.

    Uses deterministic heuristics — no LLM required. Call after read_csv_schema.

    Args:
        schema: the schema dict returned by read_csv_schema
                (keys: table_name, columns[{name, dataType}])
        existing_measures: list of measure names already in the model
                           (edit mode — these will be skipped)

    Returns:
        {"measures": [...], "count": int, "folders": [...], "skipped": int}
    """
    table   = schema.get("table_name", "Table")
    columns = schema.get("columns", [])
    buckets = _classify(columns)
    measures = _build(table, buckets)

    skipped = 0
    if existing_measures:
        existing_set = set(existing_measures)
        before   = len(measures)
        measures = [m for m in measures if m["name"] not in existing_set]
        skipped  = before - len(measures)

    folders = sorted({m["displayFolder"] for m in measures})
    return {
        "measures": measures,
        "count": len(measures),
        "folders": folders,
        "skipped": skipped,
    }
