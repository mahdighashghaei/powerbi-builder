"""Ratio and share DAX patterns.

Functions return measure dicts for use with write_tmdl_measures.
Uses DIVIDE() throughout per DAX guidelines (never raw /).
"""
from __future__ import annotations


def _m(name: str, expr: str, fmt: str, folder: str, table: str) -> dict:
    return {"name": name, "expression": expr,
            "formatString": fmt, "displayFolder": folder, "table": table}


def share_of_total(base_name: str, base_expr: str,
                   remove_col: str = "",
                   table: str = "SampleData", folder: str = "Ratios") -> dict:
    """% share of the grand total (removes all filters on the table).

    remove_col: if provided, removes filters on that specific column only,
                e.g. \"'SampleData'[Segment]\". Otherwise removes all filters.
    """
    if remove_col:
        total_expr = f"CALCULATE({base_expr}, REMOVEFILTERS({remove_col}))"
    else:
        total_expr = f"CALCULATE({base_expr}, REMOVEFILTERS('{table}'))"
    expr = f"DIVIDE({base_expr}, {total_expr})"
    return _m(f"{base_name} % of Total", expr, "0.0%", folder, table)


def share_of_parent(base_name: str, base_expr: str,
                    parent_col: str = "",
                    child_col: str = "",
                    table: str = "SampleData", folder: str = "Ratios") -> dict:
    """% share relative to the parent level (e.g. Product within Category).

    parent_col: e.g. \"'SampleData'[Segment]\"
    child_col:  e.g. \"'SampleData'[Product]\"
    """
    if not parent_col:
        parent_col = f"'{table}'[Segment]"
    if not child_col:
        child_col = f"'{table}'[Product]"
    expr = (
        f"VAR _parent = CALCULATE({base_expr}, REMOVEFILTERS({child_col})) "
        f"RETURN DIVIDE({base_expr}, _parent)"
    )
    return _m(f"{base_name} % of Parent", expr, "0.0%", folder, table)


def gross_margin_pct(revenue_expr: str = "[Total Sales]",
                     cost_expr: str = "[Total COGS]",
                     table: str = "SampleData", folder: str = "Ratios") -> dict:
    """Gross margin % = (Revenue - Cost) / Revenue using VAR + DIVIDE."""
    expr = (
        f"VAR _rev = {revenue_expr} "
        f"VAR _cost = {cost_expr} "
        f"RETURN DIVIDE(_rev - _cost, _rev)"
    )
    return _m("Gross Margin %", expr, "0.0%", folder, table)


def safe_divide(numerator_name: str, numerator_expr: str,
                denominator_expr: str,
                alternate: str = "BLANK()",
                table: str = "SampleData", folder: str = "Ratios") -> dict:
    """Generic safe division: DIVIDE(num, den, alternate)."""
    expr = f"DIVIDE({numerator_expr}, {denominator_expr}, {alternate})"
    return _m(f"{numerator_name} Ratio", expr, "0.00", folder, table)


def contribution_index(base_name: str, base_expr: str,
                       benchmark_expr: str = "",
                       table: str = "SampleData", folder: str = "Ratios") -> dict:
    """Contribution index: actual / average (>1 = above average, <1 = below).

    benchmark_expr: if blank, uses AVERAGE of base_expr over all rows.
    """
    if not benchmark_expr:
        benchmark_expr = f"CALCULATE(AVERAGEX('{table}', {base_expr}), REMOVEFILTERS('{table}'))"
    expr = f"DIVIDE({base_expr}, {benchmark_expr})"
    return _m(f"{base_name} Index", expr, "0.00x", folder, table)
