"""Smart Layout Engine for Power BI report pages.

Public API
----------
compute_page_layout(plans)   → list of position dicts (same order as plans)
split_to_pages(plans, n)     → list[list[dict]] — pages of visual plan dicts
min_size(kind)               → (min_w, min_h)

Backward-compatible aliases
---------------------------
build_layout(specs)          → {id: position_dict}   (old signature)
apply_layout(visuals)        → visuals with ``position`` updated in-place

Zone model for one page
-----------------------
::

    ┌──────────────────────────────────────────────┐  ← y = MARGIN (24)
    │  card  │  card  │  card  │  card             │  h = CARD_STRIP_H (90)
    ├──────────────────────────────────────────────┤  ← y = MARGIN + CARD_STRIP_H + GAP
    │        │        │                            │
    │  main  │  main  │  main     row_h ≥ MIN_H    │
    │        │        │                            │
    ├────────┼────────┼────────────────────────────┤
    │  main  │  main  │  main                      │
    │        │        │                            │
    └──────────────────────────────────────────────┘  ← y ≤ PAGE_H - MARGIN (696)

Cards are always compact (CARD_STRIP_H = 90 px, min-width = 160 px).
Charts, matrices, slicers, and tables go into the main grid with per-type
minimum sizes enforced.  If all main visuals cannot fit at their minimum
height, ``split_to_pages`` distributes the overflow to subsequent pages.
"""
from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Canvas constants  (MARGIN/GAP match report_agent.py's existing values)
# ---------------------------------------------------------------------------

PAGE_W: int = 1280
PAGE_H: int = 720
MARGIN: int = 24
GAP:    int = 20

# ---------------------------------------------------------------------------
# Visual-type helpers (single source of truth lives in utils.visual_types)
# ---------------------------------------------------------------------------

from utils.visual_types import (  # noqa: E402
    is_card as _is_card,
    is_chart as _is_chart,
    is_slicer as _is_slicer,
    is_table as _is_table,
)

# Kinds that belong to the compact card strip at the top of every page.
CARD_KINDS: frozenset[str] = frozenset({"card", "kpi", "kpiVisual"})

# ---------------------------------------------------------------------------
# Per-visual-type minimum pixel sizes
# ---------------------------------------------------------------------------

#: Minimum width (px) below which a visual loses meaningful content.
MIN_W: dict[str, float] = {
    "card":              160.0,  "kpi":              160.0,  "kpiVisual":        160.0,
    "barChart":          280.0,  "clusteredBarChart": 280.0,
    "columnChart":       280.0,  "lineChart":         280.0,
    "areaChart":         280.0,  "pieChart":          220.0,
    "donutChart":        220.0,  "scatterChart":      280.0,
    "matrix":            300.0,  "tableEx":           300.0,  "table":            300.0,
    "slicer":            180.0,
}

#: Minimum height (px) below which a visual loses meaningful content.
MIN_H: dict[str, float] = {
    "card":               80.0,  "kpi":               80.0,  "kpiVisual":         80.0,
    "barChart":          160.0,  "clusteredBarChart": 160.0,
    "columnChart":       160.0,  "lineChart":         160.0,
    "areaChart":         160.0,  "pieChart":          180.0,
    "donutChart":        180.0,  "scatterChart":      180.0,
    "matrix":            160.0,  "tableEx":           160.0,  "table":            160.0,
    "slicer":            100.0,
}

#: Fallback when a visual type is not listed above.
DEFAULT_MIN_W: float = 220.0
DEFAULT_MIN_H: float = 140.0

#: Fixed height (px) for each row in the card strip.
CARD_STRIP_H: float = 90.0

#: Maximum number of columns in the main visual grid.
MAX_COLS: int = 3


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def min_size(kind: str) -> tuple[float, float]:
    """Return ``(min_width, min_height)`` for a Power BI visual type string."""
    return MIN_W.get(kind, DEFAULT_MIN_W), MIN_H.get(kind, DEFAULT_MIN_H)


def _make_pos(x: float, y: float, w: float, h: float,
              z: int = 0, tab: int = 0) -> dict[str, Any]:
    """Create a PBIR-compatible position dict."""
    return {
        "x": float(x), "y": float(y), "z": z,
        "height": float(h), "width": float(w), "tabOrder": tab,
    }


# ---------------------------------------------------------------------------
# Core layout function
# ---------------------------------------------------------------------------

def compute_page_layout(plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute position dicts for all visuals on a single page.

    Parameters
    ----------
    plans:
        Visual plan dicts — each must have a ``"kind"`` key containing the
        Power BI ``visualType`` string (e.g. ``"card"``, ``"barChart"``).

    Returns
    -------
    list[dict]
        One position dict per plan, in the **same order** as *plans*, each
        containing: ``x``, ``y``, ``z``, ``height``, ``width``, ``tabOrder``.

    Zone rules
    ----------
    * Cards / KPIs → compact strip at the top (``CARD_STRIP_H`` px tall).
      Up to 6 cards per row; if more, they wrap to a second row.
    * All other visuals (charts, tables, slicers) → uniform grid below the
      card strip (or at the top when there are no cards).  Column count is
      capped at ``MAX_COLS`` and constrained by the widest ``MIN_W``.
    """
    if not plans:
        return []

    n = len(plans)
    positions: list[dict[str, Any] | None] = [None] * n

    avail_w = float(PAGE_W - 2 * MARGIN)   # 1232 px

    # ── Classify by zone ─────────────────────────────────────────────────
    card_indices = [i for i, p in enumerate(plans) if p["kind"] in CARD_KINDS]
    main_indices = [i for i, p in enumerate(plans) if p["kind"] not in CARD_KINDS]

    # ── Card strip (top) ─────────────────────────────────────────────────
    if card_indices:
        n_cards      = len(card_indices)
        card_min_w   = MIN_W.get("card", DEFAULT_MIN_W)          # 160 px
        max_per_row  = max(1, int((avail_w + GAP) / (card_min_w + GAP)))  # 6

        if n_cards <= max_per_row:
            card_rows, per_row = 1, n_cards
        else:
            card_rows = 2
            per_row   = math.ceil(n_cards / 2)

        card_w = (avail_w - (per_row - 1) * GAP) / per_row

        for seq, idx in enumerate(card_indices):
            row, col = divmod(seq, per_row)
            positions[idx] = _make_pos(
                x   = MARGIN + col * (card_w + GAP),
                y   = MARGIN + row * (CARD_STRIP_H + GAP),
                w   = card_w,
                h   = CARD_STRIP_H,
                tab = idx,
            )

        # Top of main area = bottom of last card row + one gap
        cards_h      = card_rows * CARD_STRIP_H + (card_rows - 1) * GAP
        main_y_start = MARGIN + cards_h + GAP   # e.g. 24 + 90 + 20 = 134
    else:
        main_y_start = float(MARGIN)             # no cards → start at top margin

    # Available vertical space below the card strip (or from top if no cards)
    # Bottom boundary: PAGE_H - MARGIN = 696 px
    avail_h_main = float(PAGE_H - MARGIN) - main_y_start

    # ── Main grid (charts, tables, slicers, unknown) ──────────────────────
    if main_indices:
        main_plans   = [plans[i] for i in main_indices]
        n_main       = len(main_plans)

        # Strictest (largest) minimum width across all main visuals
        global_min_w = max(MIN_W.get(p["kind"], DEFAULT_MIN_W) for p in main_plans)

        cols = min(
            MAX_COLS,
            n_main,
            max(1, int((avail_w + GAP) / (global_min_w + GAP))),
        )
        rows   = math.ceil(n_main / cols)
        col_w  = (avail_w - (cols - 1) * GAP) / cols
        row_h  = (avail_h_main - (rows - 1) * GAP) / rows

        for seq, idx in enumerate(main_indices):
            r, c = divmod(seq, cols)
            positions[idx] = _make_pos(
                x   = MARGIN + c * (col_w + GAP),
                y   = main_y_start + r * (row_h + GAP),
                w   = col_w,
                h   = row_h,
                tab = idx,
            )

    # ── Safety: fill any unfilled slot (defensive, should not happen) ─────
    for i in range(n):
        if positions[i] is None:
            positions[i] = _make_pos(
                x=float(MARGIN), y=float(MARGIN),
                w=avail_w, h=CARD_STRIP_H, tab=i,
            )

    return positions  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Page-overflow splitter
# ---------------------------------------------------------------------------

def split_to_pages(
    all_plans: list[dict[str, Any]],
    max_pages: int = 3,
) -> list[list[dict[str, Any]]]:
    """Distribute visuals across pages respecting minimum-size constraints.

    Strategy
    --------
    * Cards stay on **page 1 only** (they provide global KPI context).
    * Main visuals (charts, tables, slicers) are chunked so each page's
      grid rows remain at or above each visual's ``MIN_H``.
    * Overflow goes to subsequent pages (no cards), capped at *max_pages*.

    Parameters
    ----------
    all_plans:
        Flat list of all visual plan dicts (must have ``"kind"`` keys).
    max_pages:
        Hard cap on the number of pages returned.

    Returns
    -------
    list[list[dict]]
        Each inner list contains the visual plans for one page, in order.
    """
    if not all_plans:
        return [[]]

    cards = [p for p in all_plans if p["kind"] in CARD_KINDS]
    mains = [p for p in all_plans if p["kind"] not in CARD_KINDS]

    if not mains:
        # Cards-only report — single page
        return [cards]

    avail_w = float(PAGE_W - 2 * MARGIN)   # 1232 px

    # Main-area heights
    # Page 1: subtract one card-strip row + its gap (even if n_cards > max_per_row,
    # we budget for 1 row as a conservative lower bound for capacity)
    avail_h_main_p1   = (float(PAGE_H - MARGIN) - (MARGIN + CARD_STRIP_H + GAP)
                          if cards else float(PAGE_H - 2 * MARGIN))
    avail_h_main_rest = float(PAGE_H - 2 * MARGIN)   # subsequent pages have no cards

    # Strictest minimum sizes across *all* main visuals (consistent packing)
    global_min_w = max(MIN_W.get(p["kind"], DEFAULT_MIN_W) for p in mains)
    global_min_h = max(MIN_H.get(p["kind"], DEFAULT_MIN_H) for p in mains)

    max_cols = min(MAX_COLS, max(1, int((avail_w + GAP) / (global_min_w + GAP))))

    def _capacity(avail_h: float) -> int:
        max_rows = max(1, int((avail_h + GAP) / (global_min_h + GAP)))
        return max_cols * max_rows

    cap_p1   = _capacity(avail_h_main_p1)
    cap_rest = _capacity(avail_h_main_rest)

    pages:     list[list[dict[str, Any]]] = []
    remaining: list[dict[str, Any]]       = list(mains)

    # Page 1: cards + first chunk of main visuals
    chunk1    = remaining[:cap_p1]
    remaining = remaining[cap_p1:]
    pages.append(cards + chunk1)

    # Subsequent pages: main visuals only
    while remaining and len(pages) < max_pages:
        chunk     = remaining[:cap_rest]
        remaining = remaining[cap_rest:]
        pages.append(chunk)

    return pages


# ---------------------------------------------------------------------------
# Backward-compatible API
# ---------------------------------------------------------------------------

def build_layout(
    specs: list[dict[str, Any]],
    page_width: int = PAGE_W,   # noqa: ARG001 — kept for API compat
    page_height: int = PAGE_H,  # noqa: ARG001 — kept for API compat
) -> dict[str, dict[str, Any]]:
    """Return ``{visual_id: position_dict}`` for a list of visual specs.

    *specs* must be a list of dicts with at least ``"id"`` and ``"type"`` keys.
    The *page_width* / *page_height* parameters are accepted for backward
    compatibility but the engine always targets the canonical 1280×720 canvas.
    """
    plans     = [{"kind": s["type"]} for s in specs]
    positions = compute_page_layout(plans)
    return {s["id"]: pos for s, pos in zip(specs, positions)}


def apply_layout(
    visuals: list[dict[str, Any]],
    page_width: int = PAGE_W,   # noqa: ARG001
    page_height: int = PAGE_H,  # noqa: ARG001
) -> list[dict[str, Any]]:
    """Update ``position`` in-place for a list of fully-built visual dicts.

    Each visual must have a ``"name"`` key and ``visual["visual"]["visualType"]``.
    Returns the same list (mutations applied in-place).
    """
    plans     = [{"kind": v["visual"]["visualType"]} for v in visuals]
    positions = compute_page_layout(plans)
    for v, pos in zip(visuals, positions):
        v["position"] = pos
    return visuals
