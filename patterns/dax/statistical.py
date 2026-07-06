"""Statistical DAX patterns.

Functions return measure dicts for use with write_tmdl_measures.
"""
from __future__ import annotations


def _m(name: str, expr: str, fmt: str, folder: str, table: str) -> dict:
    return {"name": name, "expression": expr,
            "formatString": fmt, "displayFolder": folder, "table": table}


def col_average(col_name: str, col_ref: str,
                table: str = "SampleData", folder: str = "Statistics") -> dict:
    """AVERAGE of a column."""
    return _m(f"Avg {col_name}", f"AVERAGE({col_ref})", "#,0.00", folder, table)


def col_median(col_name: str, col_ref: str,
               table: str = "SampleData", folder: str = "Statistics") -> dict:
    """MEDIAN of a column."""
    return _m(f"Median {col_name}", f"MEDIAN({col_ref})", "#,0.00", folder, table)


def col_stdev(col_name: str, col_ref: str, population: bool = False,
              table: str = "SampleData", folder: str = "Statistics") -> dict:
    """Standard deviation (sample by default, population if population=True)."""
    fn = "STDEVX.P" if population else "STDEVX.S"
    expr = f"{fn}('{table}', {col_ref})"
    suffix = "σ" if population else "s"
    return _m(f"{col_name} StdDev ({suffix})", expr, "#,0.00", folder, table)


def col_min(col_name: str, col_ref: str,
            table: str = "SampleData", folder: str = "Statistics") -> dict:
    """MIN of a column."""
    return _m(f"Min {col_name}", f"MIN({col_ref})", "#,0.00", folder, table)


def col_max(col_name: str, col_ref: str,
            table: str = "SampleData", folder: str = "Statistics") -> dict:
    """MAX of a column."""
    return _m(f"Max {col_name}", f"MAX({col_ref})", "#,0.00", folder, table)


def col_percentile(col_name: str, col_ref: str, pct: float = 0.75,
                   table: str = "SampleData", folder: str = "Statistics") -> dict:
    """PERCENTILE.INC of a column at a given percentile (0–1).

    Uses PERCENTILEX.INC via CALCULATE over the full table context.
    """
    pct_label = int(pct * 100)
    expr = f"PERCENTILEX.INC('{table}', {col_ref}, {pct})"
    return _m(f"{col_name} P{pct_label}", expr, "#,0.00", folder, table)


def col_variance(col_name: str, col_ref: str, population: bool = False,
                 table: str = "SampleData", folder: str = "Statistics") -> dict:
    """Variance (sample or population)."""
    fn = "VARX.P" if population else "VARX.S"
    expr = f"{fn}('{table}', {col_ref})"
    return _m(f"{col_name} Variance", expr, "#,0.00", folder, table)


def count_distinct(col_name: str, col_ref: str,
                   table: str = "SampleData", folder: str = "Statistics") -> dict:
    """Distinct count of a column."""
    return _m(f"Distinct {col_name}", f"DISTINCTCOUNT({col_ref})", "#,0", folder, table)


def running_total(base_name: str, base_expr: str,
                  sort_col: str = "'Date'[Date]",
                  table: str = "SampleData", folder: str = "Statistics") -> dict:
    """Running total — cumulative sum ordered by sort_col."""
    expr = (
        f"CALCULATE({base_expr}, "
        f"FILTER(ALLSELECTED({sort_col}), "
        f"{sort_col} <= MAX({sort_col})))"
    )
    return _m(f"{base_name} Running Total", expr, "#,0", folder, table)
