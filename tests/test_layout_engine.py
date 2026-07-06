"""Unit tests for utils.layout_engine — zone-based layout + overflow splitter.

Test groups
-----------
01-05  Card strip placement (y, height, width, row wrap)
06-10  Main grid min-size enforcement (width, height)
11-15  Canvas-bounds guarantee (right ≤ PAGE_W−MARGIN, bottom ≤ PAGE_H−MARGIN)
16-20  Overflow splitting across pages
21-25  Edge cases (empty, cards-only, single visual, slicer-only, unknown kind)
26-28  Backward-compatible API (build_layout, apply_layout, min_size helper)
"""
from __future__ import annotations

import unittest

from utils.layout_engine import (
    CARD_STRIP_H,
    DEFAULT_MIN_H,
    DEFAULT_MIN_W,
    GAP,
    MARGIN,
    MAX_COLS,
    MIN_H,
    MIN_W,
    PAGE_H,
    PAGE_W,
    apply_layout,
    build_layout,
    compute_page_layout,
    min_size,
    split_to_pages,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _p(kind: str, name: str | None = None) -> dict:
    """Build a minimal visual plan dict."""
    return {"kind": kind, "name": name or kind}


def _plans(*kinds: str) -> list[dict]:
    return [_p(k, f"{k}-{i}") for i, k in enumerate(kinds)]


# ---------------------------------------------------------------------------
# Group 01-05 — Card strip placement
# ---------------------------------------------------------------------------

class TestCardStrip(unittest.TestCase):
    """Cards must land in the compact top strip."""

    def test_01_card_y_equals_margin(self) -> None:
        """First card row must start at y = MARGIN."""
        plans = _plans("card", "card", "card")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertAlmostEqual(p["y"], MARGIN, places=1)

    def test_02_card_height_equals_strip_h(self) -> None:
        """Every card must have height = CARD_STRIP_H."""
        plans = _plans("card", "card", "kpiVisual")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertAlmostEqual(p["height"], CARD_STRIP_H, places=1)

    def test_03_card_width_at_least_min_w(self) -> None:
        """Card width must be ≥ MIN_W['card']."""
        plans = _plans(*["card"] * 5)
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertGreaterEqual(p["width"], MIN_W["card"] - 0.5)

    def test_04_many_cards_wrap_to_two_rows(self) -> None:
        """More than max-per-row cards must produce a second card row."""
        # max_per_row = int((1232+20)/(160+20)) = 6; use 7 to force wrap
        plans = _plans(*["card"] * 7)
        pos = compute_page_layout(plans)
        y_values = sorted({round(p["y"]) for p in pos})
        self.assertEqual(len(y_values), 2, "Expected 2 card rows")
        self.assertAlmostEqual(y_values[0], MARGIN, places=1)
        # Second row y = MARGIN + CARD_STRIP_H + GAP
        expected_row2 = MARGIN + CARD_STRIP_H + GAP
        self.assertAlmostEqual(y_values[1], expected_row2, places=1)

    def test_05_cards_same_width_in_one_row(self) -> None:
        """All cards in a single row must have equal widths."""
        plans = _plans("card", "card", "card", "card")
        pos = compute_page_layout(plans)
        widths = [round(p["width"], 2) for p in pos]
        self.assertEqual(len(set(widths)), 1, "Cards should share equal width")


# ---------------------------------------------------------------------------
# Group 06-10 — Main grid minimum sizes
# ---------------------------------------------------------------------------

class TestMainGridMinSizes(unittest.TestCase):
    """Chart/table/slicer visuals must meet their per-type minimums."""

    def test_06_bar_chart_width_gte_min(self) -> None:
        plans = _plans("barChart", "barChart", "barChart")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertGreaterEqual(p["width"], MIN_W["barChart"] - 0.5)

    def test_07_bar_chart_height_gte_min(self) -> None:
        # 3 charts, no cards → row_h = (672-20)/1 = 652 if 1 row or (672-20)/3 = 217 for 3 rows
        # With 3 charts and 3 cols → 1 row → row_h = 672 ≥ 160 ✓
        plans = _plans("barChart", "barChart", "barChart")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertGreaterEqual(p["height"], MIN_H["barChart"] - 0.5)

    def test_08_matrix_width_gte_min(self) -> None:
        plans = _plans("matrix", "matrix")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertGreaterEqual(p["width"], MIN_W["matrix"] - 0.5)

    def test_09_slicer_width_gte_min(self) -> None:
        plans = _plans("slicer")
        pos = compute_page_layout(plans)
        self.assertGreaterEqual(pos[0]["width"], MIN_W["slicer"] - 0.5)

    def test_10_mixed_page_min_sizes(self) -> None:
        """bank-CSV scenario: 4 cards + 6 mixed main visuals."""
        plans = _plans(
            "card", "card", "card", "card",
            "barChart", "lineChart", "matrix", "barChart", "barChart", "slicer",
        )
        pos = compute_page_layout(plans)
        for p_dict, plan in zip(pos, plans):
            kind = plan["kind"]
            mw, mh = min_size(kind)
            self.assertGreaterEqual(p_dict["width"],  mw - 0.5,
                                    f"{kind} width {p_dict['width']:.1f} < min {mw}")
            self.assertGreaterEqual(p_dict["height"], mh - 0.5,
                                    f"{kind} height {p_dict['height']:.1f} < min {mh}")


# ---------------------------------------------------------------------------
# Group 11-15 — Canvas-bounds guarantee
# ---------------------------------------------------------------------------

class TestCanvasBounds(unittest.TestCase):
    """No visual may overflow the canvas."""

    BOUND_RIGHT  = PAGE_W - MARGIN   # 1256
    BOUND_BOTTOM = PAGE_H - MARGIN   # 696

    def _check_bounds(self, plans: list[dict]) -> None:
        pos = compute_page_layout(plans)
        for p_dict, plan in zip(pos, plans):
            right  = p_dict["x"] + p_dict["width"]
            bottom = p_dict["y"] + p_dict["height"]
            self.assertLessEqual(
                right, self.BOUND_RIGHT + 0.5,
                f"{plan['kind']} right={right:.1f} > {self.BOUND_RIGHT}",
            )
            self.assertLessEqual(
                bottom, self.BOUND_BOTTOM + 0.5,
                f"{plan['kind']} bottom={bottom:.1f} > {self.BOUND_BOTTOM}",
            )

    def test_11_bounds_one_card(self) -> None:
        self._check_bounds(_plans("card"))

    def test_12_bounds_six_charts(self) -> None:
        self._check_bounds(_plans(*["barChart"] * 6))

    def test_13_bounds_four_cards_six_charts(self) -> None:
        self._check_bounds(_plans("card", "card", "card", "card",
                                  "barChart", "lineChart", "matrix",
                                  "barChart", "barChart", "slicer"))

    def test_14_bounds_nine_charts_no_cards(self) -> None:
        self._check_bounds(_plans(*["columnChart"] * 9))

    def test_15_bounds_max_columns_respected(self) -> None:
        """A grid must never produce more than MAX_COLS columns."""
        plans = _plans(*["barChart"] * 6)
        pos = compute_page_layout(plans)
        # All x values must be from at most MAX_COLS distinct columns
        x_starts = sorted({round(p["x"]) for p in pos})
        self.assertLessEqual(len(x_starts), MAX_COLS)


# ---------------------------------------------------------------------------
# Group 16-20 — Overflow splitting
# ---------------------------------------------------------------------------

class TestSplitToPages(unittest.TestCase):
    """split_to_pages must distribute overflow to additional pages."""

    def test_16_six_charts_no_overflow(self) -> None:
        """6 charts + no cards should fit on 1 page."""
        pages = split_to_pages(_plans(*["barChart"] * 6), max_pages=3)
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]), 6)

    def test_17_cards_always_on_page_one_only(self) -> None:
        """Cards must appear only on page 1, never on subsequent pages."""
        # Create enough charts to overflow onto page 2
        plans = _plans("card", "card") + _plans(*["barChart"] * 20)
        pages = split_to_pages(plans, max_pages=3)
        self.assertGreater(len(pages), 1, "Expected overflow to page 2")
        # Page 1: starts with cards
        page1_kinds = {p["kind"] for p in pages[0]}
        self.assertIn("card", page1_kinds)
        # Page 2+: no cards
        for page in pages[1:]:
            for p in page:
                self.assertNotIn(p["kind"], ("card", "kpi", "kpiVisual"),
                                 "Card found on page 2+")

    def test_18_overflow_respects_max_pages(self) -> None:
        """split_to_pages must not exceed max_pages."""
        plans = _plans(*["barChart"] * 30)
        pages = split_to_pages(plans, max_pages=2)
        self.assertLessEqual(len(pages), 2)

    def test_19_each_overflow_page_fits_bounds(self) -> None:
        """All pages produced by split_to_pages must fit within canvas bounds."""
        plans = _plans("card", "card") + _plans(*["columnChart"] * 15)
        pages = split_to_pages(plans, max_pages=3)
        bound_r = PAGE_W - MARGIN + 0.5
        bound_b = PAGE_H - MARGIN + 0.5
        for pg_idx, page in enumerate(pages):
            pos_list = compute_page_layout(page)
            for p_dict, plan in zip(pos_list, page):
                right  = p_dict["x"] + p_dict["width"]
                bottom = p_dict["y"] + p_dict["height"]
                self.assertLessEqual(right,  bound_r,
                    f"Page {pg_idx}: {plan['kind']} right overflow")
                self.assertLessEqual(bottom, bound_b,
                    f"Page {pg_idx}: {plan['kind']} bottom overflow")

    def test_20_no_visuals_lost_in_split(self) -> None:
        """Total visual count across all pages must equal input count."""
        plans = _plans("card", "card") + _plans(*["lineChart"] * 12)
        pages = split_to_pages(plans, max_pages=3)
        total = sum(len(pg) for pg in pages)
        self.assertEqual(total, len(plans))


# ---------------------------------------------------------------------------
# Group 21-25 — Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_21_empty_plans(self) -> None:
        """Empty input → empty output."""
        self.assertEqual(compute_page_layout([]), [])

    def test_22_cards_only_page(self) -> None:
        """Cards-only page: all positions filled, heights = CARD_STRIP_H."""
        plans = _plans("card", "kpiVisual", "card")
        pos = compute_page_layout(plans)
        self.assertEqual(len(pos), 3)
        for p in pos:
            self.assertAlmostEqual(p["height"], CARD_STRIP_H, places=1)

    def test_23_single_chart_fills_page(self) -> None:
        """A single chart should use most of the available canvas area."""
        pos = compute_page_layout(_plans("barChart"))
        p = pos[0]
        avail_w = PAGE_W - 2 * MARGIN
        avail_h = PAGE_H - 2 * MARGIN
        # Single chart: width ≥ 80% of available width
        self.assertGreater(p["width"],  avail_w * 0.8)
        self.assertGreater(p["height"], avail_h * 0.8)

    def test_24_slicer_only_page(self) -> None:
        """Slicer-only page (no cards) → bounds respected."""
        plans = _plans("slicer", "slicer")
        pos = compute_page_layout(plans)
        for p in pos:
            self.assertLessEqual(p["x"] + p["width"],  PAGE_W - MARGIN + 0.5)
            self.assertLessEqual(p["y"] + p["height"], PAGE_H - MARGIN + 0.5)

    def test_25_unknown_kind_uses_defaults(self) -> None:
        """An unrecognised kind must use DEFAULT_MIN_W / DEFAULT_MIN_H."""
        plans = [_p("customVisualXYZ")]
        pos = compute_page_layout(plans)
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertGreaterEqual(p["width"],  DEFAULT_MIN_W - 0.5)
        self.assertGreaterEqual(p["height"], DEFAULT_MIN_H - 0.5)


# ---------------------------------------------------------------------------
# Group 26-28 — Backward-compatible API
# ---------------------------------------------------------------------------

class TestBackwardCompatAPI(unittest.TestCase):

    def test_26_min_size_helper(self) -> None:
        self.assertEqual(min_size("card"),     (MIN_W["card"],    MIN_H["card"]))
        self.assertEqual(min_size("barChart"), (MIN_W["barChart"], MIN_H["barChart"]))
        mw, mh = min_size("unknownType")
        self.assertEqual((mw, mh), (DEFAULT_MIN_W, DEFAULT_MIN_H))

    def test_27_build_layout_returns_id_keyed_dict(self) -> None:
        specs = [
            {"id": "v1", "type": "card"},
            {"id": "v2", "type": "barChart"},
        ]
        result = build_layout(specs)
        self.assertIn("v1", result)
        self.assertIn("v2", result)
        for pos in result.values():
            for key in ("x", "y", "height", "width"):
                self.assertIn(key, pos)

    def test_28_apply_layout_updates_position_in_place(self) -> None:
        visuals = [
            {"name": "a", "visual": {"visualType": "card"},     "position": {}},
            {"name": "b", "visual": {"visualType": "barChart"}, "position": {}},
        ]
        returned = apply_layout(visuals)
        self.assertIs(returned, visuals, "apply_layout must return the same list")
        for v in visuals:
            pos = v["position"]
            self.assertIn("x", pos)
            self.assertIn("y", pos)
            self.assertIn("height", pos)
            self.assertIn("width", pos)


if __name__ == "__main__":
    unittest.main()
