"""ADK tools for the Power BI theme system.

Exposes:

* ``list_theme_presets``  -- enumerate the available preset themes
* ``apply_theme``         -- write a preset (or custom) theme to the bare
                              .Report folder (NOT the definition/ subfolder)

Both delegate to :mod:`patterns.themes` for the actual theme payloads and to
the existing ``write_theme_json`` MCP tool for the file write so all on-disk
behaviour stays in one place.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.server import PbipToolbox  # noqa: E402
from patterns.themes import (  # noqa: E402
    get_theme,
    list_themes,
    make_custom_theme,
)


def list_theme_presets() -> dict:
    """Return the list of preset theme keys + a short blurb for each."""
    blurbs = {
        "default":        "Power BI Builder default — light, blue accent.",
        "corporate_blue": "Navy / steel-blue palette for enterprise reports.",
        "modern_dark":    "Dark background with vivid accents.",
        "earth_tones":    "Warm browns and olives on cream background.",
        "vibrant":        "High-contrast palette for marketing dashboards.",
    }
    return {
        "presets": [
            {"key": k, "description": blurbs.get(k, "")}
            for k in list_themes()
        ],
        "count": len(list_themes()),
    }


def apply_theme(
    output_dir: str,
    preset: str = "default",
    custom_palette: list[str] | None = None,
    custom_name: str | None = None,
    output_root: str = "./output",
) -> dict:
    """Write ``theme.json`` to the bare ``.Report`` folder.

    Unlike every other write_*/add_* PBIR tool, the theme file lives
    ALONGSIDE ``definition/``, not inside it -- ``report.json``'s
    ``customTheme`` reference resolves relative to the bare ``.Report``
    folder. Passing a ``.../definition``-suffixed path here writes a
    theme.json that Power BI Desktop never reads.

    Args:
        output_dir:     the ``.Report`` folder relative to ``output_root``
                        (e.g. ``"MyReport.Report"`` -- no ``/definition``
                        suffix).
        preset:         one of the keys from :func:`list_theme_presets`. Ignored
                        when ``custom_palette`` is supplied.
        custom_palette: optional 1-8 hex colors; when given, a custom theme is
                        built from this palette and ``preset`` is ignored.
        custom_name:    optional display name when ``custom_palette`` is used.
        output_root:    project output root (default ``./output``).

    Returns:
        the ``ToolResult.as_dict()`` payload from ``write_theme_json``.
    """
    if custom_palette:
        theme: dict[str, Any] = make_custom_theme(
            name=custom_name or "Custom Theme",
            data_colors=list(custom_palette),
        )
    else:
        theme = get_theme(preset)

    tb = PbipToolbox(output_root)
    return tb.write_theme_json(output_dir, theme).as_dict()


__all__ = ["list_theme_presets", "apply_theme"]
