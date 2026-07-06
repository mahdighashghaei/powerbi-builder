"""DAX measure pattern library.

Each sub-module exposes factory functions that return measure dicts
compatible with ``write_tmdl_measures``:

    {
        "name": str,          # measure display name
        "expression": str,    # single-line DAX expression
        "formatString": str,  # optional, e.g. "#,0" or "0.0%"
        "displayFolder": str, # optional, e.g. "Time Intelligence"
        "table": str,         # target table name in the semantic model
    }

Usage::

    from patterns.dax.time_intelligence import ytd, prior_year, yoy_pct
    measures = [
        ytd("Total Sales", "[Total Sales]", "Date", "'Date'[Date]"),
        prior_year("Total Sales", "[Total Sales]", "Date", "'Date'[Date]"),
        yoy_pct("Total Sales", "[Total Sales]", "Date", "'Date'[Date]"),
    ]
    # pass measures to write_tmdl_measures(output_dir, measures)
"""

from .time_intelligence import (
    ytd, mtd, qtd,
    prior_year, yoy_change, yoy_pct,
    mom_change, mom_pct,
    rolling_n_months,
)
from .ranking import rank_all, rank_within, top_n_pct
from .ratio import share_of_total, share_of_parent, gross_margin_pct, safe_divide
from .statistical import col_average, col_median, col_stdev, col_min, col_max, col_percentile

__all__ = [
    # time intelligence
    "ytd", "mtd", "qtd",
    "prior_year", "yoy_change", "yoy_pct",
    "mom_change", "mom_pct",
    "rolling_n_months",
    # ranking
    "rank_all", "rank_within", "top_n_pct",
    # ratio
    "share_of_total", "share_of_parent", "gross_margin_pct", "safe_divide",
    # statistical
    "col_average", "col_median", "col_stdev",
    "col_min", "col_max", "col_percentile",
]
