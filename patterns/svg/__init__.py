"""SVG-measure DAX patterns.

A *SVG measure* returns a string-encoded SVG (data URI) that Power BI renders
as an image in a Table/Matrix when the host column's Data Category is set to
``ImageUrl``. These let us draw rich inline visuals (sparkline, progress bar,
star rating) without a custom visual.

The returned measure dicts follow the same shape ``write_tmdl_measures`` and
``suggest_dax_measures`` already use::

    {
        "name": str,
        "expression": str,            # DAX, returns a "data:image/svg+xml;..." string
        "table": str,                 # target table for the measure
        "displayFolder": str | None,  # optional folder
        "description": str,
        "dataCategory": "ImageUrl",   # CRITICAL — Power BI renders SVG only when this is set
        "formatString": None,
    }

The ``dataCategory`` field is special: ``mcp_server.server.write_tmdl_measures``
already passes ``dataCategory`` through to the TMDL writer, which emits
``dataCategory: ImageUrl`` on the measure block. Without it Power BI shows the
raw SVG markup string.
"""
from __future__ import annotations

from typing import Any

from . import sparkline, progress_bar, rating_stars

_PRESETS = {
    "sparkline":     sparkline.build,
    "progress_bar":  progress_bar.build,
    "rating_stars":  rating_stars.build,
}

_BLURBS = {
    "sparkline":    "Inline line chart drawn from a daily value column / measure.",
    "progress_bar": "Horizontal bar showing actual vs target as a percentage.",
    "rating_stars": "5-star rating from a 0–N score (e.g. CSAT).",
}


def list_svg_patterns() -> list[dict[str, str]]:
    """Return the available SVG measure preset keys and descriptions."""
    return [{"key": k, "description": _BLURBS.get(k, "")} for k in _PRESETS]


def build_svg_measure(preset: str, **kwargs: Any) -> dict[str, Any]:
    """Build a single SVG measure dict.

    Args:
        preset: one of the keys from :func:`list_svg_patterns`.
        **kwargs: forwarded to the preset builder. See each preset's ``build``
            docstring for required arguments.

    Raises:
        KeyError: if ``preset`` is unknown.
    """
    if preset not in _PRESETS:
        raise KeyError(
            f"Unknown SVG preset: {preset!r}. "
            f"Available: {sorted(_PRESETS)}"
        )
    return _PRESETS[preset](**kwargs)


build_sparkline = sparkline.build
build_progress_bar = progress_bar.build
build_rating_stars = rating_stars.build


__all__ = [
    "list_svg_patterns",
    "build_svg_measure",
    "build_sparkline",
    "build_progress_bar",
    "build_rating_stars",
]
