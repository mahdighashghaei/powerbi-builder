"""Ranking DAX patterns.

Functions return measure dicts for use with write_tmdl_measures.
"""
from __future__ import annotations


def _m(name: str, expr: str, fmt: str, folder: str, table: str) -> dict:
    return {"name": name, "expression": expr,
            "formatString": fmt, "displayFolder": folder, "table": table}


def rank_all(base_name: str, base_expr: str,
             rank_table: str | None = None,
             table: str = "SampleData", folder: str = "Ranking") -> dict:
    """RANKX rank of the current filter context against all rows of rank_table.

    Uses DENSE ranking (no gaps) and DESC order.
    Explicitly passes the value argument to avoid double-comma syntax
    that some Desktop versions do not parse correctly.
    """
    rt = rank_table or table
    expr = f"RANKX(ALL('{rt}'), {base_expr}, {base_expr}, DESC, Dense)"
    return _m(f"{base_name} Rank", expr, "0", folder, table)


def rank_within(base_name: str, base_expr: str,
                group_col: str = "",
                table: str = "SampleData", folder: str = "Ranking") -> dict:
    """Rank within a group column (e.g. within Category).

    group_col: fully-qualified column, e.g. \"'SampleData'[Segment]\"
    """
    if not group_col:
        group_col = f"'{table}'[Segment]"
    expr = (
        f"VAR _grp = SELECTEDVALUE({group_col}) "
        f"RETURN RANKX("
        f"CALCULATETABLE(VALUES({group_col}), {group_col} = _grp), "
        f"{base_expr}, {base_expr}, DESC, Dense)"
    )
    return _m(f"{base_name} Rank in Group", expr, "0", folder, table)


def top_n_flag(base_name: str, base_expr: str, n: int = 5,
               dim_col: str = "",
               table: str = "SampleData", folder: str = "Ranking") -> dict:
    """Returns 1 if the current item is in the Top N, 0 otherwise.

    dim_col: fully-qualified dimension column, e.g. \"'SampleData'[Product]\"
    Useful for visual-level filtering without TOPN().
    """
    if not dim_col:
        dim_col = f"'{table}'[Product]"
    expr = (
        f"VAR _rank = RANKX(ALL({dim_col}), {base_expr}, {base_expr}, DESC, Dense) "
        f"RETURN IF(_rank <= {n}, 1, 0)"
    )
    return _m(f"{base_name} Top {n} Flag", expr, "0", folder, table)


def top_n_pct(base_name: str, base_expr: str,
              table: str = "SampleData", folder: str = "Ranking") -> dict:
    """Percentile rank (0–100) of the current value in the full table."""
    expr = (
        f"VAR _total = COUNTROWS(ALL('{table}')) "
        f"VAR _rank = RANKX(ALL('{table}'), {base_expr}, {base_expr}, DESC, Dense) "
        f"RETURN DIVIDE(_total - _rank, _total - 1) * 100"
    )
    return _m(f"{base_name} Percentile Rank", expr, "0.0", folder, table)
