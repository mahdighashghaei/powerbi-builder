"""ADK tools for Deneb (Vega-Lite) custom visuals.

Exposes:

* ``list_deneb_presets`` — enumerate the available preset specs.
* ``write_deneb_visual`` — add a Deneb visual to an existing PBIR page,
                            either from a preset key + simple field args, or
                            from a hand-written Vega-Lite spec dict.

The actual file write goes through ``PbipToolbox.write_deneb_visual`` so the
on-disk path validation and ``$schema`` choice stays in one place.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.server import PbipToolbox  # noqa: E402
from patterns.deneb import (  # noqa: E402
    get_deneb_spec,
    list_deneb_presets as _list_presets,
)


def list_deneb_presets() -> dict:
    """Return the available Deneb preset keys and short descriptions."""
    presets = _list_presets()
    return {"presets": presets, "count": len(presets)}


def write_deneb_visual(
    output_dir: str,
    page_id: str,
    table: str,
    fields: list[dict[str, str]],
    preset: str | None = None,
    preset_kwargs: dict[str, Any] | None = None,
    vega_lite_spec: dict[str, Any] | None = None,
    visual_id: str | None = None,
    x: float = 40,
    y: float = 40,
    width: float = 560,
    height: float = 180,
    output_root: str = "./output",
) -> dict:
    """Write a Deneb visual to ``pages/<page_id>/visuals/<visual_id>/visual.json``.

    Either pass ``preset`` (+ optional ``preset_kwargs``) to build the spec
    from a bundled template, OR pass ``vega_lite_spec`` directly with your
    own Vega-Lite v5 spec dict. ``fields`` is what Power BI projects into the
    Deneb dataset; the spec references those field names by their PB property.

    Args:
        output_dir: report definition folder relative to ``output_root``
            (e.g. ``"MyReport.Report/definition"``).
        page_id: id of the existing page to attach the visual to.
        table: entity name (table) that owns the projected fields.
        fields: list of ``{"kind":"column"|"measure", "name": <pb name>}``.
        preset: optional preset key from :func:`list_deneb_presets`. Mutually
            exclusive with ``vega_lite_spec``.
        preset_kwargs: kwargs forwarded to the preset builder. Defaults to
            an empty dict.
        vega_lite_spec: explicit Vega-Lite v5 spec — used when ``preset`` is
            omitted.
        visual_id: optional internal id; auto-generated if missing.
        x/y/width/height: visual geometry in pixels.
        output_root: project output root (default ``./output``).

    Returns:
        ``ToolResult.as_dict()`` payload from the underlying write, or
        ``{"ok": False, "errors": [...]}`` on a bad argument combination.
    """
    if preset and vega_lite_spec:
        return {
            "ok": False,
            "errors": ["Pass either 'preset' or 'vega_lite_spec', not both."],
        }
    if preset:
        spec = get_deneb_spec(preset, **(preset_kwargs or {}))
    elif vega_lite_spec:
        spec = vega_lite_spec
    else:
        return {
            "ok": False,
            "errors": ["Provide one of 'preset' or 'vega_lite_spec'."],
        }

    deneb_def: dict[str, Any] = {
        "table": table,
        "fields": list(fields),
        "vega_lite_spec": spec,
        "x": x, "y": y,
        "width": width, "height": height,
    }
    if visual_id:
        deneb_def["id"] = visual_id

    tb = PbipToolbox(output_root)
    return tb.write_deneb_visual(output_dir, page_id, deneb_def).as_dict()


__all__ = ["list_deneb_presets", "write_deneb_visual"]
