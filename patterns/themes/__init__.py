"""Theme presets for Power BI reports.

Each preset is a complete PBIR ``theme.json`` payload (the same shape used by
the existing default ``templates/theme.json``). They follow Power BI's
ReportThemeSchemaUserVersion1 format which is what Desktop accepts inline
without requiring a separate theme resource file.

Usage::

    from patterns.themes import get_theme, list_themes
    theme = get_theme("modern_dark")      # -> full theme dict ready to write
    keys  = list_themes()                 # -> ["default", "corporate_blue", ...]
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# ---------------------------------------------------------------------------
# Reusable building blocks
# ---------------------------------------------------------------------------

def _text_classes(foreground: str) -> dict[str, Any]:
    """Standard set of textClasses with the given foreground color."""
    return {
        "title":   {"fontSize": 16, "fontFace": "Segoe UI Semibold", "color": foreground},
        "header":  {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": foreground},
        "label":   {"fontSize": 10, "fontFace": "Segoe UI",          "color": foreground},
        "callout": {"fontSize": 28, "fontFace": "Segoe UI Semibold", "color": foreground},
    }


def _visual_styles(background: str, border: str) -> dict[str, Any]:
    """Default visual container styling (background + border).

    IMPORTANT: Only style visual containers, NOT Power BI Desktop UI elements
    like the Filter Pane. We explicitly set outspacePane and filterCard to
    light colors to keep the UI usable regardless of report theme.
    """
    return {
        "*": {
            "*": {
                "background": [
                    {"show": True,
                     "color": {"solid": {"color": background}},
                     "transparency": 0}
                ],
                "border": [
                    {"show": True, "color": {"solid": {"color": border}}}
                ],
                # Filter Pane (Desktop UI) - always keep light/readable
                "outspacePane": [
                    {
                        "backgroundColor": {"solid": {"color": "#FFFFFF"}},
                        "foregroundColor": {"solid": {"color": "#404040"}},
                        "transparency": 0,
                        "border": True,
                        "borderColor": {"solid": {"color": "#B3B0AD"}},
                    }
                ],
                "filterCard": [
                    {
                        "$id": "Applied",
                        "transparency": 0,
                        "foregroundColor": {"solid": {"color": "#404040"}},
                        "backgroundColor": {"solid": {"color": "#F0F0F0"}},
                        "border": True,
                    },
                    {
                        "$id": "Available",
                        "transparency": 0,
                        "foregroundColor": {"solid": {"color": "#404040"}},
                        "backgroundColor": {"solid": {"color": "#FFFFFF"}},
                        "border": True,
                    },
                ],
            }
        }
    }


def _build(
    *,
    name: str,
    data_colors: list[str],
    background: str,
    foreground: str,
    accent: str,
    good: str = "#1AAB40",
    neutral: str = "#D9B300",
    bad: str = "#D60057",
    border: str | None = None,
) -> dict[str, Any]:
    """Assemble a complete theme dict from the standard knobs.

    This follows the Power BI theme schema closely, including only the fields
    that are commonly used and don't interfere with Desktop UI elements.
    """
    border = border or "#E0E0E0"

    return {
        "$schema": "https://powerbi.com/product/schema#reportTheme",
        "name": name,
        "dataColors": data_colors,
        "good": good,
        "neutral": neutral,
        "bad": bad,
        "maximum": data_colors[0],
        "center": border,
        "minimum": data_colors[-1] if len(data_colors) > 1 else background,
        "null": "#FF7F0E",
        # Basic colors - keep minimal to avoid interfering with Desktop UI
        "background": background,
        "foreground": foreground,
        "tableAccent": accent,
        # Text styling
        "textClasses": _text_classes(foreground),
        # Visual container styling
        "visualStyles": _visual_styles(background, border),
    }


# ---------------------------------------------------------------------------
# Preset palette definitions
# ---------------------------------------------------------------------------

_PRESETS: dict[str, dict[str, Any]] = {
    "default": _build(
        name="PowerBI Builder Default",
        data_colors=[
            "#118DFF", "#12239E", "#E66C37", "#6B007B",
            "#E044A7", "#7FEC00", "#9B6E00", "#3D5F00",
        ],
        background="#FFFFFF",
        foreground="#252423",
        accent="#118DFF",
    ),
    "corporate_blue": _build(
        name="Corporate Blue",
        data_colors=[
            "#1F3864", "#2E75B6", "#5B9BD5", "#9DC3E6",
            "#BDD7EE", "#264478", "#4472C4", "#8FAADC",
        ],
        background="#FFFFFF",
        foreground="#1F3864",
        accent="#1F3864",
        good="#2E7D32",
        neutral="#F9A825",
        bad="#C62828",
    ),
    "modern_dark": _build(
        name="Modern Dark",
        data_colors=[
            "#00B5E2", "#7FBA00", "#FFBA00", "#F25022",
            "#A1C8F0", "#737373", "#9B59B6", "#1ABC9C",
        ],
        background="#1F1F1F",
        foreground="#F2F2F2",
        accent="#00B5E2",
        good="#7FBA00",
        neutral="#FFBA00",
        bad="#F25022",
        border="#3A3A3A",
    ),
    "earth_tones": _build(
        name="Earth Tones",
        data_colors=[
            "#8B5A2B", "#A0826D", "#C9B79C", "#6B8E23",
            "#556B2F", "#D2B48C", "#8FBC8F", "#BC8F8F",
        ],
        background="#FAF8F2",
        foreground="#3E2723",
        accent="#8B5A2B",
        good="#558B2F",
        neutral="#F9A825",
        bad="#8B0000",
        border="#D7CCB9",
    ),
    "vibrant": _build(
        name="Vibrant",
        data_colors=[
            "#FF6B6B", "#FFD93D", "#6BCB77", "#4D96FF",
            "#FF9F40", "#9D4EDD", "#F72585", "#06D6A0",
        ],
        background="#FFFFFF",
        foreground="#2D3436",
        accent="#FF6B6B",
        good="#06D6A0",
        neutral="#FFD93D",
        bad="#F72585",
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_themes() -> list[str]:
    """Return all available theme preset keys."""
    return list(_PRESETS.keys())


def get_theme(key: str) -> dict[str, Any]:
    """Return a deep copy of the preset theme dict for the given key.

    Falls back to ``"default"`` when the key is unknown.
    """
    preset = _PRESETS.get(key) or _PRESETS["default"]
    return deepcopy(preset)


def make_custom_theme(
    *,
    name: str,
    data_colors: list[str],
    background: str = "#FFFFFF",
    foreground: str = "#252423",
    accent: str = "",
) -> dict[str, Any]:
    """Build a complete theme from a 1-8 hex color palette.

    The list is cycled to fill the 8-slot ``dataColors`` array Desktop
    expects. ``accent`` defaults to ``data_colors[0]``.
    """
    if not data_colors:
        raise ValueError("data_colors must contain at least one hex color")
    # cycle to exactly 8 entries
    full = [data_colors[i % len(data_colors)] for i in range(8)]
    return _build(
        name=name,
        data_colors=full,
        background=background,
        foreground=foreground,
        accent=accent or full[0],
    )


__all__ = ["list_themes", "get_theme", "make_custom_theme"]
