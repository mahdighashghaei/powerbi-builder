"""Phase 10 tests — intelligent model-driven editing.

These tests cover the new visibility + smart-layout + bug-fix work:
  * ``list_pages`` / ``describe_page`` — read current canvas state
  * ``update_visual`` — fixed to write geometry to the nested ``position``
  * ``review_report`` — fixed overlap detection (reads nested position)
  * ``plan_page_layout`` / ``relayout_page`` — smart zone-based layout
  * ``add_page`` — new ``auto_layout`` param
  * ``read_pbip_schema`` — enhanced with measure details + pages

Tests are stdlib ``unittest`` so they run with or without pytest
(``python tests/test_phase10_intelligent_editing.py`` works standalone).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Helpers — minimal PBIP project on disk (real layout: <root>/<name>/<name>.{...})
# ---------------------------------------------------------------------------


def _make_pbip(root: Path, name: str = "Demo") -> Path:
    """Create a minimal valid PBIP project (semantic model + report skeleton).

    Mirrors the real on-disk layout: ``<root>/<name>/<name>.SemanticModel`` and
    ``<root>/<name>/<name>.Report``. Returns the *project folder*
    (``<root>/<name>``) so callers can pass it as an absolute ``pbip_dir`` to
    read-only tools (review/highlevel/PbipToolbox), or pass ``name`` as a
    relative ``pbip_dir`` with ``output_root=<root>`` to edit tools (which use
    ``safe_join``).
    """
    proj = root / name
    sm = proj / f"{name}.SemanticModel"
    rep = proj / f"{name}.Report"
    (sm / "definition" / "tables").mkdir(parents=True, exist_ok=True)
    (rep / "definition" / "pages").mkdir(parents=True, exist_ok=True)
    (sm / "definition.pbism").write_text("{}", encoding="utf-8")
    (rep / "definition.pbir").write_text("{}", encoding="utf-8")
    (sm / "definition" / "tables" / "Sales.tmdl").write_text(
        "table Sales\n\tlineageTag: abc\n\n"
        "\tcolumn Amount\n\t\tdataType: double\n\n"
        "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
        "\t\tformatString: $ #,##0.00\n"
        "\t\tdisplayFolder: Revenue\n",
        encoding="utf-8",
    )
    return proj


def _make_page(proj: Path, name: str, page_id: str,
               width: int = 1280, height: int = 720) -> Path:
    """Create an empty page folder with a page.json under a PBIP project."""
    pdir = proj / f"{name}.Report" / "definition" / "pages" / page_id
    (pdir / "visuals").mkdir(parents=True, exist_ok=True)
    (pdir / "page.json").write_text(json.dumps({
        "$schema": "x", "name": page_id, "displayName": page_id,
        "width": width, "height": height,
    }), encoding="utf-8")
    return pdir


def _make_visual(proj: Path, name: str, page_id: str, visual_id: str,
                 vtype: str, x: float, y: float, w: float, h: float,
                 title: str | None = None, query_state: dict | None = None) -> Path:
    """Write a single visual.json with the nested position structure PBIR uses."""
    vdir = (proj / f"{name}.Report" / "definition" / "pages" / page_id
            / "visuals" / visual_id)
    vdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.7.0/schema.json",
        "name": visual_id,
        "position": {"x": float(x), "y": float(y), "z": 0,
                     "height": float(h), "width": float(w), "tabOrder": 0},
        "visual": {
            "visualType": vtype,
            "query": {
                "queryState": query_state or {},
                "sortDefinition": {"isDefaultSort": True},
            },
        },
    }
    if title:
        payload["visual"]["objects"] = {"title": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
        }}]}
    vjson = vdir / "visual.json"
    vjson.write_text(json.dumps(payload), encoding="utf-8")
    return vjson


def _visual_position(proj: Path, name: str, page_id: str, visual_id: str) -> dict:
    """Read back the nested position object from a visual.json on disk."""
    vjson = (proj / f"{name}.Report" / "definition" / "pages" / page_id
             / "visuals" / visual_id / "visual.json")
    return json.loads(vjson.read_text(encoding="utf-8"))["position"]


def _overlap(a: dict, b: dict) -> bool:
    """True if two position dicts (x,y,width,height) overlap as rectangles."""
    return (a["x"] < b["x"] + b["width"] and a["x"] + a["width"] > b["x"]
            and a["y"] < b["y"] + b["height"] and a["y"] + a["height"] > b["y"])


# ---------------------------------------------------------------------------
# Visibility tools
# ---------------------------------------------------------------------------


class TestListPages(unittest.TestCase):
    """list_pages — enumerate report pages with visual counts."""

    def test_lists_pages_with_counts(self):
        from adk.tools.review_tools import list_pages
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "overview")
            _make_visual(proj, "Demo", "overview", "card1", "card", 10, 10, 200, 110)
            _make_page(proj, "Demo", "trends")  # empty page
            r = list_pages(str(proj))
            self.assertTrue(r["ok"], r)
            ids = [p["page_id"] for p in r["pages"]]
            self.assertEqual(sorted(ids), ["overview", "trends"])
            counts = {p["page_id"]: p["visual_count"] for p in r["pages"]}
            self.assertEqual(counts["overview"], 1)
            self.assertEqual(counts["trends"], 0)
            self.assertEqual(r["total_pages"], 2)
            self.assertEqual(r["total_visuals"], 1)

    def test_missing_project(self):
        from adk.tools.review_tools import list_pages
        r = list_pages("/no/such/dir")
        self.assertFalse(r["ok"])


class TestDescribePage(unittest.TestCase):
    """describe_page — full detail of one page (positions + bindings)."""

    def test_describes_visuals_with_position_and_bindings(self):
        from adk.tools.review_tools import describe_page
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            _make_visual(proj, "Demo", "p1", "v1", "card", 10, 20, 200, 110,
                         title="Sales",
                         query_state={"Values": [{"kind": "measure",
                                                  "name": "Total Sales"}]})
            _make_visual(proj, "Demo", "p1", "v2", "barChart", 250, 20, 400, 300)
            r = describe_page(str(proj), "p1")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["page_id"], "p1")
            self.assertEqual(r["visual_count"], 2)
            by_id = {v["id"]: v for v in r["visuals"]}
            # position read from nested object, not top-level
            self.assertEqual(by_id["v1"]["position"]["x"], 10)
            self.assertEqual(by_id["v1"]["position"]["y"], 20)
            self.assertEqual(by_id["v1"]["type"], "card")
            self.assertEqual(by_id["v1"]["title"], "Sales")
            # bindings extracted from queryState
            self.assertEqual(by_id["v1"]["bindings"][0]["kind"], "measure")
            self.assertEqual(by_id["v1"]["bindings"][0]["name"], "Total Sales")

    def test_missing_page(self):
        from adk.tools.review_tools import describe_page
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "real")
            r = describe_page(str(proj), "bogus")
            self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# Bug fix: update_visual writes to nested position
# ---------------------------------------------------------------------------


class TestUpdateVisualPositionFix(unittest.TestCase):
    """update_visual must write x/y/... to the nested position object.

    edit_tools use ``safe_join(output_root, pbip_dir)`` so ``pbip_dir`` is the
    *project folder name* (relative) and ``output_root`` is its parent.
    """

    def test_move_updates_nested_position(self):
        from adk.tools.edit_tools import update_visual
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            _make_visual(proj, "Demo", "p1", "v1", "card", 0, 0, 200, 110)
            r = update_visual("Demo", "p1", "v1",
                              {"x": 100, "y": 200, "width": 300},
                              output_root=str(root))
            self.assertTrue(r["ok"], r)
            pos = _visual_position(proj, "Demo", "p1", "v1")
            self.assertEqual(pos["x"], 100)
            self.assertEqual(pos["y"], 200)
            self.assertEqual(pos["width"], 300)

    def test_title_update(self):
        from adk.tools.edit_tools import update_visual
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            _make_visual(proj, "Demo", "p1", "v1", "card", 0, 0, 200, 110)
            r = update_visual("Demo", "p1", "v1",
                              {"title": "New Title"}, output_root=str(root))
            self.assertTrue(r["ok"], r)
            vjson = (proj / "Demo.Report" / "definition" / "pages" / "p1"
                     / "visuals" / "v1" / "visual.json")
            v = json.loads(vjson.read_text(encoding="utf-8"))
            title_val = (v["visual"]["objects"]["title"][0]["properties"]
                         ["text"]["expr"]["Literal"]["Value"])
            self.assertEqual(title_val, "'New Title'")


# ---------------------------------------------------------------------------
# Bug fix: review_report overlap detection reads nested position
# ---------------------------------------------------------------------------


class TestReviewReportOverlapFix(unittest.TestCase):
    """review_report must detect overlaps by reading nested positions."""

    def test_detects_overlapping_visuals(self):
        from adk.tools.review_tools import review_report
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            # two visuals at the exact same spot → overlap
            _make_visual(proj, "Demo", "p1", "v1", "card", 10, 10, 300, 200)
            _make_visual(proj, "Demo", "p1", "v2", "card", 10, 10, 300, 200)
            r = review_report(str(proj))
            self.assertTrue(r["ok"], r)
            self.assertTrue(any("overlap" in i.lower() for i in r["issues"]),
                            f"expected an overlap issue, got: {r['issues']}")

    def test_no_overlap_when_apart(self):
        from adk.tools.review_tools import review_report
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            _make_visual(proj, "Demo", "p1", "v1", "card", 10, 10, 200, 110)
            _make_visual(proj, "Demo", "p1", "v2", "card", 500, 500, 200, 110)
            r = review_report(str(proj))
            self.assertTrue(r["ok"], r)
            self.assertFalse(any("overlap" in i.lower() for i in r["issues"]),
                             f"unexpected overlap: {r['issues']}")


# ---------------------------------------------------------------------------
# Smart layout engine
# ---------------------------------------------------------------------------


class TestPlanPageLayout(unittest.TestCase):
    """plan_page_layout — zone-based non-overlapping positions."""

    def test_zones_and_no_overlaps(self):
        from adk.tools.layout_tools import plan_page_layout
        specs = [
            {"id": "card1", "type": "card"},
            {"id": "card2", "type": "card"},
            {"id": "chart1", "type": "columnChart"},
            {"id": "slicer1", "type": "slicer"},
            {"id": "table1", "type": "tableEx"},
        ]
        r = plan_page_layout(specs)
        self.assertEqual(r["count"], 5)
        pos = r["positions"]
        self.assertIn("card1", pos)
        # no two visuals overlap
        ids = list(pos)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                self.assertFalse(_overlap(pos[ids[i]], pos[ids[j]]),
                                f"{ids[i]} overlaps {ids[j]}")
        # slicer on the right (different column in main grid)
        self.assertGreater(pos["slicer1"]["x"], pos["chart1"]["x"])
        # table in main area — same zone as chart; y ≥ chart.y (may share a row)
        # The new zone model places tables alongside charts in the main grid.
        self.assertGreaterEqual(pos["table1"]["y"], pos["chart1"]["y"])


class TestRelayoutPage(unittest.TestCase):
    """relayout_page — re-apply smart layout to existing visuals."""

    def test_fixes_overlaps(self):
        from adk.tools.edit_tools import relayout_page
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "p1")
            # start with overlapping visuals
            _make_visual(proj, "Demo", "p1", "v1", "card", 0, 0, 500, 500)
            _make_visual(proj, "Demo", "p1", "v2", "barChart", 0, 0, 500, 500)
            r = relayout_page("Demo", "p1", output_root=str(root))
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["visual_count"], 2)
            self.assertEqual(sorted(r["repositioned"]), ["v1", "v2"])
            # read back → no overlap
            p1 = _visual_position(proj, "Demo", "p1", "v1")
            p2 = _visual_position(proj, "Demo", "p1", "v2")
            self.assertFalse(_overlap(p1, p2))

    def test_missing_page(self):
        from adk.tools.edit_tools import relayout_page
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_pbip(root, "Demo")
            r = relayout_page("Demo", "bogus", output_root=str(root))
            self.assertFalse(r["ok"])


# ---------------------------------------------------------------------------
# add_page auto_layout
# ---------------------------------------------------------------------------


class TestAddPageAutoLayout(unittest.TestCase):
    """add_page auto-layouts visuals by default."""

    def test_auto_layout_no_overlaps(self):
        from mcp_server import highlevel as hl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            # visuals with deliberately overlapping geometry
            visuals = [
                {"id": "c1", "visualType": "card", "queryState": {},
                 "x": 0, "y": 0, "width": 800, "height": 600},
                {"id": "c2", "visualType": "card", "queryState": {},
                 "x": 0, "y": 0, "width": 800, "height": 600},
                {"id": "ch1", "visualType": "columnChart", "queryState": {},
                 "x": 0, "y": 0, "width": 800, "height": 600},
                {"id": "sl1", "visualType": "slicer", "queryState": {},
                 "x": 0, "y": 0, "width": 800, "height": 600},
            ]
            res = hl.add_page(str(proj), "Overview", visuals=visuals)
            self.assertTrue(res["ok"], res)
            self.assertTrue(res["data"]["auto_layout"])
            pid = res["data"]["page_id"]
            # read back positions → no overlaps
            positions = []
            vdir = proj / "Demo.Report" / "definition" / "pages" / pid / "visuals"
            for v in vdir.iterdir():
                vj = json.loads((v / "visual.json").read_text(encoding="utf-8"))
                positions.append(vj["position"])
            self.assertEqual(len(positions), 4)
            for i in range(len(positions)):
                for j in range(i + 1, len(positions)):
                    self.assertFalse(_overlap(positions[i], positions[j]),
                                     f"visual {i} overlaps {j}")

    def test_auto_layout_false_preserves_geometry(self):
        from mcp_server import highlevel as hl
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            visuals = [
                {"id": "c1", "visualType": "card", "queryState": {},
                 "x": 42, "y": 55, "width": 200, "height": 110},
            ]
            res = hl.add_page(str(proj), "Manual", visuals=visuals, auto_layout=False)
            self.assertTrue(res["ok"], res)
            self.assertFalse(res["data"]["auto_layout"])
            pid = res["data"]["page_id"]
            pos = _visual_position(proj, "Demo", pid, "c1")
            self.assertEqual(pos["x"], 42)
            self.assertEqual(pos["y"], 55)


# ---------------------------------------------------------------------------
# Enhanced read_pbip_schema
# ---------------------------------------------------------------------------


class TestReadPbipSchemaEnhanced(unittest.TestCase):
    """read_pbip_schema now returns measure details + pages."""

    def test_returns_measures_with_expressions(self):
        from mcp_server.server import PbipToolbox
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            r = PbipToolbox(root).read_pbip_schema(str(proj))
            self.assertTrue(r.ok, r.errors)
            measures = r.data["measures"]
            self.assertTrue(any(m["name"] == "Total Sales" for m in measures),
                            f"measures: {measures}")
            ts = next(m for m in measures if m["name"] == "Total Sales")
            self.assertIn("SUM", ts["expression"])
            self.assertEqual(ts["table"], "Sales")
            self.assertEqual(ts["formatString"], "$ #,##0.00")
            # backward compat: existing_measures still present
            self.assertIn("Total Sales", r.data["existing_measures"])

    def test_returns_pages(self):
        from mcp_server.server import PbipToolbox
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = _make_pbip(root, "Demo")
            _make_page(proj, "Demo", "overview")
            _make_visual(proj, "Demo", "overview", "v1", "card", 0, 0, 200, 110)
            r = PbipToolbox(root).read_pbip_schema(str(proj))
            self.assertTrue(r.ok, r.errors)
            pages = r.data["pages"]
            self.assertTrue(any(p["page_id"] == "overview" for p in pages),
                            f"pages: {pages}")
            ov = next(p for p in pages if p["page_id"] == "overview")
            self.assertEqual(ov["visual_count"], 1)


# ---------------------------------------------------------------------------
# build_layout zone correctness (pure, no disk)
# ---------------------------------------------------------------------------


class TestBuildLayoutZones(unittest.TestCase):
    """build_layout places card/charts/slicers/tables in the right zones."""

    def test_card_top_slicer_right_table_bottom(self):
        from utils.layout_engine import MARGIN, build_layout
        specs = [
            {"id": "card", "type": "card"},
            {"id": "chart", "type": "columnChart"},
            {"id": "slicer", "type": "slicer"},
            {"id": "table", "type": "tableEx"},
        ]
        pos = build_layout(specs)
        # card at the very top (y == MARGIN)
        self.assertEqual(pos["card"]["y"], MARGIN)
        # slicer to the right of chart (different column in main grid)
        self.assertGreater(pos["slicer"]["x"], pos["chart"]["x"])
        # table in main area — same zone as chart (row may be equal or below)
        # The new zone model places tables alongside charts in the main grid
        # rather than in a separate bottom strip, so y ≥ chart.y is the invariant.
        self.assertGreaterEqual(pos["table"]["y"], pos["chart"]["y"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
