"""Deneb (Vega-Lite) visual presets for Power BI.

Each preset returns a fully-formed Vega-Lite spec dict that, once paired with a
field binding by ``mcp_server.pbir_generator.build_deneb_visual``, renders inside
the Deneb custom visual (visualType ``deneb7E15AEF80B9E4D4F8E12924291ECE89A``).

The presets follow the conventions from
``pbir-ref/plugins/custom-visuals/skills/deneb-visuals``:

* Data source name is always ``"dataset"`` (Deneb feeds the dataset under that
  name regardless of which Power BI fields are projected).
* Each preset references the projected fields by their *Power BI* property
  names — the bound measure/column names you pass to
  :func:`build_kpi_card`/etc. become Vega-Lite field references via the
  generated ``transform`` block.
* All presets use the ``$schema`` URL for Vega-Lite v5.

Usage::

    from patterns.deneb import build_kpi_card, list_deneb_presets

    spec = build_kpi_card(value_field="Total Sales", target_field="Sales Target",
                          title="Sales vs Target")
    presets = list_deneb_presets()        # -> ["kpi_card", "bullet_chart", ...]
"""
from __future__ import annotations

from typing import Any

from . import kpi_card, bullet_chart, spark_line, small_multiples, calendar_heatmap

_PRESETS = {
    "kpi_card":         kpi_card.build,
    "bullet_chart":     bullet_chart.build,
    "spark_line":       spark_line.build,
    "small_multiples":  small_multiples.build,
    "calendar_heatmap": calendar_heatmap.build,
}

_BLURBS = {
    "kpi_card":         "Big-number KPI with title and delta % vs target.",
    "bullet_chart":     "Actual vs target horizontal bars (top-N).",
    "spark_line":       "Compact line trend, no axes — for inline rows.",
    "small_multiples":  "Faceted small-multiple bar chart by category.",
    "calendar_heatmap": "Year-view heatmap (day × week) of a daily metric.",
}


def list_deneb_presets() -> list[dict[str, str]]:
    """Return the available Deneb preset keys and short descriptions."""
    return [{"key": k, "description": _BLURBS.get(k, "")} for k in _PRESETS]


def get_deneb_spec(preset: str, **kwargs: Any) -> dict[str, Any]:
    """Build a Vega-Lite spec for ``preset`` with the given field bindings.

    Raises:
        KeyError: if ``preset`` is unknown.
    """
    if preset not in _PRESETS:
        raise KeyError(
            f"Unknown Deneb preset: {preset!r}. "
            f"Available: {sorted(_PRESETS)}"
        )
    return _PRESETS[preset](**kwargs)


# Re-export individual builders for direct import
build_kpi_card = kpi_card.build
build_bullet_chart = bullet_chart.build
build_spark_line = spark_line.build
build_small_multiples = small_multiples.build
build_calendar_heatmap = calendar_heatmap.build


__all__ = [
    "list_deneb_presets",
    "get_deneb_spec",
    "build_kpi_card",
    "build_bullet_chart",
    "build_spark_line",
    "build_small_multiples",
    "build_calendar_heatmap",
]
