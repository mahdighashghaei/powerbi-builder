"""Progress-bar SVG measure.

A horizontal bar showing ``value / target`` as a percentage of full width.
The bar fills with one color when on/over target, another when under target.

Args:
* ``measure_name``   -- DAX measure name.
* ``table``          -- table the measure lives on.
* ``value_measure``  -- measure giving the actual value.
* ``target_measure`` -- measure giving the target.
* ``width``          -- SVG width in pixels (default 120).
* ``height``         -- SVG height in pixels (default 16).
* ``positive_color`` -- fill color when value >= target (default green-ish).
* ``negative_color`` -- fill color when value <  target (default red-ish).
* ``track_color``    -- background track color (default light gray).
* ``display_folder`` -- optional TMDL display folder.
"""
from __future__ import annotations

from typing import Any


def build(
    measure_name: str,
    table: str,
    value_measure: str,
    target_measure: str,
    width: int = 120,
    height: int = 16,
    positive_color: str = "#2EA84A",
    negative_color: str = "#E66C37",
    track_color: str = "#E8E8E8",
    display_folder: str | None = None,
) -> dict[str, Any]:
    """Return a measure dict for a progress-bar SVG."""
    expr = (
        f"VAR _Value  = [{value_measure}]\n"
        f"VAR _Target = [{target_measure}]\n"
        f"VAR _Ratio  = DIVIDE(_Value, _Target)\n"
        f"VAR _Pct    = MIN(MAX(_Ratio, 0), 1)\n"
        f"VAR _W      = {width}\n"
        f"VAR _H      = {height}\n"
        f"VAR _BarW   = FORMAT(_Pct * _W, \"0.0\")\n"
        f"VAR _Color  = IF(_Ratio >= 1, \"{positive_color}\", \"{negative_color}\")\n"
        f"VAR _SVG =\n"
        f"    \"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' \" &\n"
        f"    \"width='\" & _W & \"' height='\" & _H & \"' viewBox='0 0 \" & _W & \" \" & _H & \"'>\" &\n"
        f"    \"<rect x='0' y='0' width='\" & _W & \"' height='\" & _H & \"' rx='3' ry='3' fill='{track_color}'/>\" &\n"
        f"    \"<rect x='0' y='0' width='\" & _BarW & \"' height='\" & _H & \"' rx='3' ry='3' fill='\" & _Color & \"'/>\" &\n"
        f"    \"</svg>\"\n"
        f"RETURN IF(NOT ISBLANK(_Value) && NOT ISBLANK(_Target), _SVG, BLANK())"
    )

    measure: dict[str, Any] = {
        "name": measure_name,
        "expression": expr,
        "table": table,
        "description": (
            f"SVG progress bar of [{value_measure}] / [{target_measure}]."
        ),
        "dataCategory": "ImageUrl",
        "formatString": None,
    }
    if display_folder:
        measure["displayFolder"] = display_folder
    return measure
