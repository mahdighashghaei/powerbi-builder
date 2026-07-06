"""Rating-stars SVG measure.

Renders 5 stars colored to reflect a 0–``max_score`` rating. Half-step values
fill the half-star polygon.

Args:
* ``measure_name``  -- DAX measure name.
* ``table``         -- table the measure lives on.
* ``score_measure`` -- measure returning a value between 0 and ``max_score``.
* ``max_score``     -- maximum score that maps to 5 stars (default 5).
* ``star_size``     -- side length of each star slot in pixels (default 16).
* ``filled_color``  -- color for filled stars (default ``#F1C40F``, gold).
* ``empty_color``   -- color for empty stars (default ``#E0E0E0``).
* ``display_folder`` -- optional TMDL display folder.
"""
from __future__ import annotations

from typing import Any


# A unit star polygon (10 vertices, scaled to fit inside a 16x16 box by default).
# Coordinates are pre-computed relative to the star center; we shift them per
# slot in the DAX expression.
_STAR_POINTS = (
    "8,1 10.09,5.95 15.51,6.45 11.4,10.04 12.63,15.36 8,12.5 "
    "3.37,15.36 4.6,10.04 0.49,6.45 5.91,5.95"
)


def build(
    measure_name: str,
    table: str,
    score_measure: str,
    max_score: int = 5,
    star_size: int = 16,
    filled_color: str = "#F1C40F",
    empty_color: str = "#E0E0E0",
    display_folder: str | None = None,
) -> dict[str, Any]:
    """Return a measure dict for a 5-star rating SVG."""
    # Build 5 polygon strings; each is the unit star translated by i * star_size.
    polygons = []
    for i in range(5):
        # For each star, the fill color depends on the rating threshold (i+1).
        threshold = (i + 1) / 5 * max_score
        x_offset = i * star_size
        translated = " ".join(
            f"{float(px) + x_offset:.2f},{py}"
            for px, py in (p.split(",") for p in _STAR_POINTS.split(" "))
        )
        polygons.append((threshold, translated))

    parts = []
    for threshold, pts in polygons:
        # Each star: filled if score >= threshold, otherwise empty.
        parts.append(
            f"\"<polygon points='{pts}' fill='\" & "
            f"IF(_Score >= {threshold}, \"{filled_color}\", \"{empty_color}\") "
            f"& \"'/>\""
        )
    polygons_dax = " & ".join(parts)

    total_w = 5 * star_size
    expr = (
        f"VAR _Score = [{score_measure}]\n"
        f"VAR _W     = {total_w}\n"
        f"VAR _H     = {star_size}\n"
        f"VAR _SVG =\n"
        f"    \"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' \" &\n"
        f"    \"width='\" & _W & \"' height='\" & _H & \"' viewBox='0 0 \" & _W & \" \" & _H & \"'>\" &\n"
        f"    {polygons_dax} &\n"
        f"    \"</svg>\"\n"
        f"RETURN IF(NOT ISBLANK(_Score), _SVG, BLANK())"
    )

    measure: dict[str, Any] = {
        "name": measure_name,
        "expression": expr,
        "table": table,
        "description": (
            f"5-star SVG rating of [{score_measure}] (max {max_score})."
        ),
        "dataCategory": "ImageUrl",
        "formatString": None,
    }
    if display_folder:
        measure["displayFolder"] = display_folder
    return measure
