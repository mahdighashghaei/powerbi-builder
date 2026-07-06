"""ADK tool: suggest_dax_measures — build DAX measures from pattern library.

Wraps patterns/dax/* so the agent can request ready-made, correct DAX
expressions without constructing them manually.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from patterns.dax import time_intelligence as _ti
from patterns.dax import ranking as _rk
from patterns.dax import ratio as _rt
from patterns.dax import statistical as _st

# ---------------------------------------------------------------------------
# Catalogue: maps pattern_type string → factory (base_name, base_expr, **kw)
# ---------------------------------------------------------------------------

_CATALOGUE: dict[str, str] = {
    # time intelligence
    "ytd":            "Year-to-date total",
    "mtd":            "Month-to-date total",
    "qtd":            "Quarter-to-date total",
    "prior_year":     "Same period last year (PY)",
    "yoy_change":     "Year-over-year absolute change",
    "yoy_pct":        "Year-over-year % change",
    "mom_change":     "Month-over-month absolute change",
    "mom_pct":        "Month-over-month % change",
    "rolling_3m":     "3-month rolling average",
    "rolling_12m":    "12-month moving annual total (MAT)",
    # ranking
    "rank_all":       "Rank in full table (RANKX Dense DESC)",
    "top_n_flag":     "Top-N flag (1/0 for Top 5 by default)",
    "top_n_pct":      "Percentile rank (0–100)",
    # ratio
    "share_of_total": "% share of grand total",
    "gross_margin":   "Gross margin % (Revenue − Cost) / Revenue",
    "contribution":   "Contribution index (actual ÷ average)",
    # statistical
    "average":        "AVERAGE of a column",
    "median":         "MEDIAN of a column",
    "stdev":          "Sample standard deviation",
    "min":            "MIN of a column",
    "max":            "MAX of a column",
    "p75":            "75th percentile",
    "p90":            "90th percentile",
    "count_distinct": "Distinct count of a column",
    "running_total":  "Cumulative running total",
}


def list_dax_patterns() -> dict:
    """List all available DAX pattern types with descriptions.

    Returns a dict with keys:
        patterns: {type_key: description} mapping
        usage: short usage note for suggest_dax_measures
    """
    return {
        "patterns": _CATALOGUE,
        "usage": (
            "Call suggest_dax_measures(pattern_types=[...], base_name=..., "
            "base_expr=..., table=...) to get ready-made measure dicts."
        ),
    }


def suggest_dax_measures(
    pattern_types: list[str],
    base_name: str,
    base_expr: str,
    table: str = "SampleData",
    date_col: str = "'Date'[Date]",
    col_ref: str = "",
    n: int = 5,
    cost_expr: str = "",
) -> dict:
    """Generate DAX measure dicts from the pattern library.

    Args:
        pattern_types: list of pattern keys from list_dax_patterns().
                       E.g. ["ytd", "yoy_pct", "share_of_total"]
        base_name:     human-readable base measure name, e.g. "Total Sales"
        base_expr:     DAX reference to the base measure, e.g. "[Total Sales]"
        table:         target table name in the semantic model (default "SampleData")
        date_col:      fully-qualified date column for time patterns (default "'Date'[Date]")
        col_ref:       fully-qualified column for statistical patterns,
                       e.g. \"'SampleData'[Sales]\". Defaults to table[base_name].
        n:             N for top_n_flag pattern (default 5)
        cost_expr:     DAX reference to the cost measure for gross_margin
                       (e.g. "[Total COGS]"). Required for the "gross_margin"
                       pattern; defaults to "[Total COGS]" for demo compat.

    Returns:
        {"measures": [{"name", "expression", "formatString", "displayFolder", "table"}, ...],
         "count": int,
         "skipped": [pattern_type, ...]}
    """
    if not col_ref:
        col_ref = f"'{table}'[{base_name}]"
    if not cost_expr:
        cost_expr = "[Total COGS]"

    measures: list[dict] = []
    skipped: list[str] = []

    # NOTE: keep _DISPATCH keys in sync with _CATALOGUE above. The assert
    # after the dict catches drift (a pattern advertised in the catalogue but
    # missing a builder, or vice versa) so a new pattern is never silently skipped.
    _DISPATCH: dict[str, object] = {
        "ytd":            lambda: _ti.ytd(base_name, base_expr, date_col=date_col, table=table),
        "mtd":            lambda: _ti.mtd(base_name, base_expr, date_col=date_col, table=table),
        "qtd":            lambda: _ti.qtd(base_name, base_expr, date_col=date_col, table=table),
        "prior_year":     lambda: _ti.prior_year(base_name, base_expr, date_col=date_col, table=table),
        "yoy_change":     lambda: _ti.yoy_change(base_name, base_expr, date_col=date_col, table=table),
        "yoy_pct":        lambda: _ti.yoy_pct(base_name, base_expr, date_col=date_col, table=table),
        "mom_change":     lambda: _ti.mom_change(base_name, base_expr, date_col=date_col, table=table),
        "mom_pct":        lambda: _ti.mom_pct(base_name, base_expr, date_col=date_col, table=table),
        "rolling_3m":     lambda: _ti.rolling_n_months(base_name, base_expr, n=3, date_col=date_col, table=table),
        "rolling_12m":    lambda: _ti.rolling_12m(base_name, base_expr, date_col=date_col, table=table),
        "rank_all":       lambda: _rk.rank_all(base_name, base_expr, table=table),
        "top_n_flag":     lambda: _rk.top_n_flag(base_name, base_expr, n=n, table=table),
        "top_n_pct":      lambda: _rk.top_n_pct(base_name, base_expr, table=table),
        "share_of_total": lambda: _rt.share_of_total(base_name, base_expr, table=table),
        "gross_margin":   lambda: _rt.gross_margin_pct(base_expr, cost_expr, table=table),
        "contribution":   lambda: _rt.contribution_index(base_name, base_expr, table=table),
        "average":        lambda: _st.col_average(base_name, col_ref, table=table),
        "median":         lambda: _st.col_median(base_name, col_ref, table=table),
        "stdev":          lambda: _st.col_stdev(base_name, col_ref, table=table),
        "min":            lambda: _st.col_min(base_name, col_ref, table=table),
        "max":            lambda: _st.col_max(base_name, col_ref, table=table),
        "p75":            lambda: _st.col_percentile(base_name, col_ref, 0.75, table=table),
        "p90":            lambda: _st.col_percentile(base_name, col_ref, 0.90, table=table),
        "count_distinct": lambda: _st.count_distinct(base_name, col_ref, table=table),
        "running_total":  lambda: _st.running_total(base_name, base_expr, date_col, table=table),
    }
    # Guard against catalogue/dispatch drift: every advertised pattern must
    # have a builder and vice versa, or a pattern would be silently skipped.
    assert set(_CATALOGUE) == set(_DISPATCH), (
        f"dax pattern catalogue/dispatch drift: "
        f"catalogue-only={set(_CATALOGUE) - set(_DISPATCH)}, "
        f"dispatch-only={set(_DISPATCH) - set(_CATALOGUE)}"
    )

    for pt in pattern_types:
        factory = _DISPATCH.get(pt)
        if factory is None:
            skipped.append(pt)
            continue
        try:
            measures.append(factory())
        except Exception as exc:
            skipped.append(f"{pt} (error: {exc})")

    return {"measures": measures, "count": len(measures), "skipped": skipped}
