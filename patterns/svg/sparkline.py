"""Sparkline SVG measure.

Generates a polyline sparkline of ``value_measure`` over ``axis_column``,
scaled to fit a small SVG box. The output is a ``data:image/svg+xml;utf8,…``
string Power BI renders inline in a table cell when the measure's data
category is ``ImageUrl``.

The DAX walks the axis (e.g. a Date column), builds an ordered table of
``(x_index, value)`` points, scales them to the SVG viewport, and emits the
polyline path.

Args (all required unless marked optional):
* ``measure_name``  -- DAX measure name (e.g. ``"Sales Sparkline"``).
* ``table``         -- table the measure lives on.
* ``axis_table``    -- table that owns the X-axis column (e.g. ``"Date"``).
* ``axis_column``   -- the X-axis column (sorted ascending).
* ``value_measure`` -- the measure that produces the value at each X point.
* ``width``         -- SVG width in pixels (default 120).
* ``height``        -- SVG height in pixels (default 30).
* ``stroke``        -- line color (default ``#118DFF``).
* ``stroke_width``  -- line width (default 2).
* ``display_folder`` -- optional TMDL display folder.
"""
from __future__ import annotations

from typing import Any


def build(
    measure_name: str,
    table: str,
    axis_table: str,
    axis_column: str,
    value_measure: str,
    width: int = 120,
    height: int = 30,
    stroke: str = "#118DFF",
    stroke_width: int = 2,
    display_folder: str | None = None,
) -> dict[str, Any]:
    """Return a measure dict for a sparkline SVG."""
    # X uses the row index (1..N) so the spacing is uniform regardless of
    # whether dates are missing — typical sparkline behavior.
    expr = (
        f"VAR _Points = ADDCOLUMNS(\n"
        f"        VALUES('{axis_table}'[{axis_column}]),\n"
        f"        \"@Value\", [{value_measure}]\n"
        f"    )\n"
        f"VAR _Ranked = ADDCOLUMNS(\n"
        f"        _Points,\n"
        f"        \"@X\", RANKX(_Points, '{axis_table}'[{axis_column}], , ASC, Dense)\n"
        f"    )\n"
        f"VAR _Count = COUNTROWS(_Ranked)\n"
        f"VAR _MaxV  = MAXX(_Ranked, [@Value])\n"
        f"VAR _MinV  = MINX(_Ranked, [@Value])\n"
        f"VAR _Range = IF(_MaxV - _MinV = 0, 1, _MaxV - _MinV)\n"
        f"VAR _W     = {width}\n"
        f"VAR _H     = {height}\n"
        f"VAR _PadY  = 3\n"
        f"VAR _Points2 = ADDCOLUMNS(\n"
        f"        _Ranked,\n"
        f"        \"@PX\", FORMAT(DIVIDE([@X] - 1, _Count - 1) * _W, \"0.0\"),\n"
        f"        \"@PY\", FORMAT(_H - _PadY - DIVIDE([@Value] - _MinV, _Range) * (_H - 2 * _PadY), \"0.0\")\n"
        f"    )\n"
        f"VAR _Path = CONCATENATEX(_Points2, [@PX] & \",\" & [@PY], \" \", [@X], ASC)\n"
        f"VAR _SVG =\n"
        f"    \"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' \" &\n"
        f"    \"width='\" & _W & \"' height='\" & _H & \"' viewBox='0 0 \" & _W & \" \" & _H & \"'>\" &\n"
        f"    \"<polyline fill='none' stroke='{stroke}' stroke-width='{stroke_width}' \" &\n"
        f"    \"stroke-linejoin='round' stroke-linecap='round' points='\" & _Path & \"'/></svg>\"\n"
        f"RETURN IF(_Count > 1, _SVG, BLANK())"
    )

    measure: dict[str, Any] = {
        "name": measure_name,
        "expression": expr,
        "table": table,
        "description": (
            f"SVG sparkline of [{value_measure}] over '{axis_table}'[{axis_column}]."
        ),
        "dataCategory": "ImageUrl",
        "formatString": None,
    }
    if display_folder:
        measure["displayFolder"] = display_folder
    return measure
