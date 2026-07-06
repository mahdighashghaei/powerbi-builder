"""Single source of truth for Power BI visual type classifications.

Visual types are referenced in several places (the layout engine, the MCP
server's validation/error messages, the ADK layout tool). Keeping the sets in
one module prevents drift — e.g. a new visual type added to the layout engine
but not to the server's allowed list would be silently rejected.

Each predicate (`is_card`, `is_chart`, …) returns True for a visual-type
string. The sets are also exposed directly for membership/iteration.
"""
from __future__ import annotations

# All visual types the project can generate (PBIR visualType values).
ALL_VISUAL_TYPES: frozenset[str] = frozenset({
    "card", "kpi",
    "barChart", "columnChart", "lineChart", "pieChart",
    "donutChart", "scatterChart", "areaChart",
    "tableEx", "table", "matrix",
    "slicer",
})

_CARD_TYPES: frozenset[str] = frozenset({"card", "kpi"})
_CHART_TYPES: frozenset[str] = frozenset({
    "barChart", "columnChart", "lineChart", "pieChart",
    "donutChart", "scatterChart", "areaChart",
})
_TABLE_TYPES: frozenset[str] = frozenset({"tableEx", "table", "matrix"})
_SLICER_TYPES: frozenset[str] = frozenset({"slicer"})


def is_card(t: str) -> bool:
    return t in _CARD_TYPES


def is_chart(t: str) -> bool:
    return t in _CHART_TYPES


def is_table(t: str) -> bool:
    return t in _TABLE_TYPES


def is_slicer(t: str) -> bool:
    return t in _SLICER_TYPES


def is_known(t: str) -> bool:
    """True if ``t`` is any supported visual type."""
    return t in ALL_VISUAL_TYPES


def classify(t: str) -> str:
    """Return the zone name for a visual type: card/chart/table/slicer/other."""
    if is_card(t):
        return "card"
    if is_chart(t):
        return "chart"
    if is_table(t):
        return "table"
    if is_slicer(t):
        return "slicer"
    return "other"
