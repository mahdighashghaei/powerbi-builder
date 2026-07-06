"""Time intelligence DAX patterns.

All functions return a measure dict for use with write_tmdl_measures.

Parameters shared by most functions:
    base_name:    display name of the base measure, e.g. "Total Sales"
    base_expr:    DAX reference to base measure, e.g. "[Total Sales]"
    date_table:   name of the Date table, e.g. "Date"
    date_col:     fully-qualified date column, e.g. "'Date'[Date]"
    table:        target table in the semantic model (where measures live)
    folder:       display folder override (defaults to "Time Intelligence")
"""
from __future__ import annotations


def _m(name: str, expr: str, fmt: str, folder: str, table: str) -> dict:
    return {"name": name, "expression": expr,
            "formatString": fmt, "displayFolder": folder, "table": table}


# ---------------------------------------------------------------------------
# Period-to-date
# ---------------------------------------------------------------------------

def ytd(base_name: str, base_expr: str,
        date_table: str = "Date", date_col: str = "'Date'[Date]",
        table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Year-to-date using TOTALYTD."""
    expr = f"TOTALYTD({base_expr}, {date_col})"
    return _m(f"{base_name} YTD", expr, "#,0", folder, table)


def mtd(base_name: str, base_expr: str,
        date_table: str = "Date", date_col: str = "'Date'[Date]",
        table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Month-to-date using TOTALMTD."""
    expr = f"TOTALMTD({base_expr}, {date_col})"
    return _m(f"{base_name} MTD", expr, "#,0", folder, table)


def qtd(base_name: str, base_expr: str,
        date_table: str = "Date", date_col: str = "'Date'[Date]",
        table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Quarter-to-date using TOTALQTD."""
    expr = f"TOTALQTD({base_expr}, {date_col})"
    return _m(f"{base_name} QTD", expr, "#,0", folder, table)


# ---------------------------------------------------------------------------
# Prior period
# ---------------------------------------------------------------------------

def prior_year(base_name: str, base_expr: str,
               date_table: str = "Date", date_col: str = "'Date'[Date]",
               table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Same period last year using CALCULATE + SAMEPERIODLASTYEAR."""
    expr = (
        f"CALCULATE({base_expr}, SAMEPERIODLASTYEAR({date_col}))"
    )
    return _m(f"{base_name} PY", expr, "#,0", folder, table)


def prior_month(base_name: str, base_expr: str,
                date_col: str = "'Date'[Date]",
                table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Same measure one month ago using DATEADD."""
    expr = f"CALCULATE({base_expr}, DATEADD({date_col}, -1, MONTH))"
    return _m(f"{base_name} PM", expr, "#,0", folder, table)


# ---------------------------------------------------------------------------
# Year-over-Year
# ---------------------------------------------------------------------------

def yoy_change(base_name: str, base_expr: str,
               date_table: str = "Date", date_col: str = "'Date'[Date]",
               table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Absolute YoY change with VAR pattern."""
    expr = (
        f"VAR _cur = {base_expr} "
        f"VAR _py = CALCULATE({base_expr}, SAMEPERIODLASTYEAR({date_col})) "
        f"RETURN IF(NOT ISBLANK(_py), _cur - _py)"
    )
    return _m(f"{base_name} YoY Chg", expr, "#,0;-#,0", folder, table)


def yoy_pct(base_name: str, base_expr: str,
            date_table: str = "Date", date_col: str = "'Date'[Date]",
            table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """YoY percentage change using DIVIDE for divide-by-zero safety."""
    expr = (
        f"VAR _cur = {base_expr} "
        f"VAR _py = CALCULATE({base_expr}, SAMEPERIODLASTYEAR({date_col})) "
        f"RETURN IF(NOT ISBLANK(_py), DIVIDE(_cur - _py, _py))"
    )
    return _m(f"{base_name} YoY %", expr, "0.0%;-0.0%", folder, table)


# ---------------------------------------------------------------------------
# Month-over-Month
# ---------------------------------------------------------------------------

def mom_change(base_name: str, base_expr: str,
               date_col: str = "'Date'[Date]",
               table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Absolute MoM change."""
    expr = (
        f"VAR _cur = {base_expr} "
        f"VAR _pm = CALCULATE({base_expr}, DATEADD({date_col}, -1, MONTH)) "
        f"RETURN IF(NOT ISBLANK(_pm), _cur - _pm)"
    )
    return _m(f"{base_name} MoM Chg", expr, "#,0;-#,0", folder, table)


def mom_pct(base_name: str, base_expr: str,
            date_col: str = "'Date'[Date]",
            table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """MoM percentage change."""
    expr = (
        f"VAR _cur = {base_expr} "
        f"VAR _pm = CALCULATE({base_expr}, DATEADD({date_col}, -1, MONTH)) "
        f"RETURN IF(NOT ISBLANK(_pm), DIVIDE(_cur - _pm, _pm))"
    )
    return _m(f"{base_name} MoM %", expr, "0.0%;-0.0%", folder, table)


# ---------------------------------------------------------------------------
# Rolling windows
# ---------------------------------------------------------------------------

def rolling_n_months(base_name: str, base_expr: str, n: int = 3,
                     date_col: str = "'Date'[Date]",
                     table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Rolling N-month average using DATESINPERIOD.

    Uses -N as the offset per DAX guidelines (DATESINPERIOD offset must match
    the exact number of periods, not N-1).
    """
    expr = (
        f"AVERAGEX("
        f"DATESINPERIOD({date_col}, LASTDATE({date_col}), -{n}, MONTH), "
        f"{base_expr})"
    )
    return _m(f"{base_name} {n}M Rolling Avg", expr, "#,0.0", folder, table)


def rolling_12m(base_name: str, base_expr: str,
                date_col: str = "'Date'[Date]",
                table: str = "SampleData", folder: str = "Time Intelligence") -> dict:
    """Rolling 12-month total (MAT — Moving Annual Total)."""
    expr = (
        f"CALCULATE({base_expr}, "
        f"DATESINPERIOD({date_col}, LASTDATE({date_col}), -12, MONTH))"
    )
    return _m(f"{base_name} MAT", expr, "#,0", folder, table)
