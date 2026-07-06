"""Conditional formatting builders for Power BI visuals.

Provides pure functions that return JSON-ready dicts for the three main
conditional formatting types:

* :func:`color_scale_2`  -- 2-color gradient (min/max) for chart data points
* :func:`color_scale_3`  -- 3-color gradient (min/mid/max) for chart data points
* :func:`data_bars`      -- horizontal bars in table/matrix cells
* :func:`icon_set`       -- traffic lights, arrows, stars based on thresholds

All builders return the exact PBIR JSON structure validated against
Power BI Desktop's schema (derived from the data-goblin/pbir-cli reference).

Usage::

    from patterns.conditional_formatting import color_scale_2, data_bars

    # Apply 2-color gradient to bar chart based on "Revenue" measure
    cf = color_scale_2(
        table="Sales", measure="Revenue",
        min_color="#FFC7CE", max_color="#C6EFCE",
    )
    visual["visual"]["objects"] = {"dataPoint": [cf]}
"""
from __future__ import annotations

from typing import Any, Literal

# ---------------------------------------------------------------------------
# Color scales (gradients)
# ---------------------------------------------------------------------------

NullStrategy = Literal["asZero", "noColor", "specificColor"]


def _measure_input(table: str, measure: str) -> dict[str, Any]:
    """The ``Input`` block that points the gradient at a measure."""
    return {
        "Measure": {
            "Expression": {"SourceRef": {"Entity": table}},
            "Property": measure,
        }
    }


def _color_literal(hex_color: str) -> dict[str, Any]:
    """A literal hex color expression (gradients can't use theme colors)."""
    return {"Literal": {"Value": f"'{hex_color}'"}}


def _value_literal(value: float) -> dict[str, Any]:
    """A literal numeric value (e.g., for min/max bounds)."""
    return {"Literal": {"Value": f"{value}D"}}


def _null_strategy(strategy: NullStrategy = "asZero") -> dict[str, Any]:
    return {"strategy": {"Literal": {"Value": f"'{strategy}'"}}}


def color_scale_2(
    *,
    table: str,
    measure: str,
    min_color: str = "#FFC7CE",
    max_color: str = "#C6EFCE",
    min_value: float | None = None,
    max_value: float | None = None,
    null_strategy: NullStrategy = "asZero",
) -> dict[str, Any]:
    """2-color gradient (min → max) driven by a measure.

    Returns a single ``dataPoint`` formatting block ready to drop into
    ``visual.objects.dataPoint`` (chart visuals).

    Args:
        table:        the measure's table.
        measure:      the measure name driving the gradient.
        min_color:    color for the minimum value (default: light red).
        max_color:    color for the maximum value (default: light green).
        min_value:    optional explicit min bound (data-driven if omitted).
        max_value:    optional explicit max bound (data-driven if omitted).
        null_strategy: how to color null values ("asZero", "noColor", "specificColor").
    """
    min_block: dict[str, Any] = {"color": _color_literal(min_color)}
    if min_value is not None:
        min_block["value"] = _value_literal(min_value)

    max_block: dict[str, Any] = {"color": _color_literal(max_color)}
    if max_value is not None:
        max_block["value"] = _value_literal(max_value)

    return {
        "properties": {
            "fill": {
                "solid": {
                    "color": {
                        "expr": {
                            "FillRule": {
                                "Input": _measure_input(table, measure),
                                "FillRule": {
                                    "linearGradient2": {
                                        "min": min_block,
                                        "max": max_block,
                                        "nullColoringStrategy": _null_strategy(null_strategy),
                                    }
                                },
                            }
                        }
                    }
                }
            }
        },
        "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}]
        },
    }


def color_scale_3(
    *,
    table: str,
    measure: str,
    min_color: str = "#F8696B",
    mid_color: str = "#FFEB84",
    max_color: str = "#63BE7B",
    min_value: float | None = None,
    mid_value: float | None = None,
    max_value: float | None = None,
    null_strategy: NullStrategy = "asZero",
) -> dict[str, Any]:
    """3-color gradient (min → mid → max) driven by a measure.

    Common use: red → yellow → green for diverging value ranges.

    Args:
        table:        the measure's table.
        measure:      the measure name driving the gradient.
        min_color:    color for minimum value (default: red).
        mid_color:    color for midpoint (default: yellow).
        max_color:    color for maximum value (default: green).
        min_value/mid_value/max_value: optional explicit bounds.
        null_strategy: how to color null values.
    """
    def _bucket(color: str, value: float | None) -> dict[str, Any]:
        block: dict[str, Any] = {"color": _color_literal(color)}
        if value is not None:
            block["value"] = _value_literal(value)
        return block

    return {
        "properties": {
            "fill": {
                "solid": {
                    "color": {
                        "expr": {
                            "FillRule": {
                                "Input": _measure_input(table, measure),
                                "FillRule": {
                                    "linearGradient3": {
                                        "min": _bucket(min_color, min_value),
                                        "mid": _bucket(mid_color, mid_value),
                                        "max": _bucket(max_color, max_value),
                                        "nullColoringStrategy": _null_strategy(null_strategy),
                                    }
                                },
                            }
                        }
                    }
                }
            }
        },
        "selector": {
            "data": [{"dataViewWildcard": {"matchingOption": 1}}]
        },
    }


# ---------------------------------------------------------------------------
# Data bars (table/matrix only)
# ---------------------------------------------------------------------------


def data_bars(
    *,
    column_metadata: str,
    positive_color: str = "#118DFF",
    negative_color: str = "#D64554",
    axis_color: str = "#999999",
    reverse_direction: bool = False,
    hide_text: bool = False,
) -> dict[str, Any]:
    """Horizontal data bars for a table/matrix column.

    Args:
        column_metadata: dotted reference like ``"Sales.Revenue"`` (table.column/measure).
        positive_color:  bar color for positive values.
        negative_color:  bar color for negative values.
        axis_color:      color of the zero-axis line.
        reverse_direction: True = right-to-left bars.
        hide_text:       True = show only bars (no numbers).

    Returns a single ``columnFormatting`` block ready for
    ``visual.objects.columnFormatting``.
    """
    return {
        "properties": {
            "dataBars": {
                "positiveColor": {
                    "solid": {"color": {"expr": _color_literal(positive_color)}}
                },
                "negativeColor": {
                    "solid": {"color": {"expr": _color_literal(negative_color)}}
                },
                "axisColor": {
                    "solid": {"color": {"expr": _color_literal(axis_color)}}
                },
                "reverseDirection": {
                    "expr": {"Literal": {"Value": str(reverse_direction).lower()}}
                },
                "hideText": {
                    "expr": {"Literal": {"Value": str(hide_text).lower()}}
                },
            }
        },
        "selector": {"metadata": column_metadata},
    }


# ---------------------------------------------------------------------------
# Icon sets (table/matrix only)
# ---------------------------------------------------------------------------

# Common preset icon styles
ICON_PRESETS: dict[str, list[dict[str, Any]]] = {
    "traffic_lights": [
        {"style": "TrafficLightGreen", "percent": 67},
        {"style": "TrafficLightYellow", "percent": 33},
        {"style": "TrafficLightRed", "percent": 0},
    ],
    "arrows": [
        {"style": "ArrowUp", "percent": 67},
        {"style": "ArrowRight", "percent": 33},
        {"style": "ArrowDown", "percent": 0},
    ],
    "triangles": [
        {"style": "TriangleHigh", "percent": 67},
        {"style": "TriangleMedium", "percent": 33},
        {"style": "TriangleLow", "percent": 0},
    ],
    "circles": [
        {"style": "CircleGreen", "percent": 67},
        {"style": "CircleYellow", "percent": 33},
        {"style": "CircleRed", "percent": 0},
    ],
    "stars": [
        {"style": "StarFull", "percent": 67},
        {"style": "StarHalf", "percent": 33},
        {"style": "StarEmpty", "percent": 0},
    ],
    "flags": [
        {"style": "FlagGreen", "percent": 67},
        {"style": "FlagYellow", "percent": 33},
        {"style": "FlagRed", "percent": 0},
    ],
    "signal_bars": [
        {"style": "SignalBarFull", "percent": 80},
        {"style": "SignalBarThreeQuarter", "percent": 60},
        {"style": "SignalBarHalf", "percent": 40},
        {"style": "SignalBarQuarter", "percent": 20},
        {"style": "SignalBarEmpty", "percent": 0},
    ],
}

IconLayout = Literal["Before", "After", "IconOnly"]


def icon_set(
    *,
    preset: str = "traffic_lights",
    layout: IconLayout = "Before",
    icons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Icon-set rule for a table/matrix column.

    Args:
        preset:  name from :data:`ICON_PRESETS` (traffic_lights, arrows, triangles,
                 circles, stars, flags, signal_bars). Ignored when ``icons`` given.
        layout:  ``"Before"`` (icon before value), ``"After"``, or ``"IconOnly"``.
        icons:   optional custom list of ``{"style": ..., "percent": N}`` entries.
                 Overrides ``preset`` when provided.

    Returns a single rule block ready to drop into ``visual.objects``.
    """
    icon_list = icons if icons is not None else ICON_PRESETS.get(preset)
    if icon_list is None:
        raise ValueError(
            f"Unknown icon preset '{preset}'. Available: {list(ICON_PRESETS)}"
        )

    return {
        "properties": {
            "iconRule": {
                "iconDefinition": {
                    "mode": "IconSet",
                    "layout": layout,
                    "icons": list(icon_list),
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_icon_presets() -> list[str]:
    """Return all available icon preset names."""
    return list(ICON_PRESETS.keys())


__all__ = [
    "color_scale_2",
    "color_scale_3",
    "data_bars",
    "icon_set",
    "list_icon_presets",
    "ICON_PRESETS",
]
