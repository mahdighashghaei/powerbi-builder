"""Phase 4 tests — Deneb (Vega-Lite) visual presets and build_deneb_visual.

Also covers Phase 4.3 — SVG measure patterns.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.pbir_generator import build_deneb_visual, DENEB_VISUAL_TYPE
from mcp_server.server import PbipToolbox
from patterns.deneb import (
    build_bullet_chart,
    build_calendar_heatmap,
    build_kpi_card,
    build_small_multiples,
    build_spark_line,
    get_deneb_spec,
    list_deneb_presets,
)
from patterns.svg import (
    build_progress_bar,
    build_rating_stars,
    build_sparkline,
    build_svg_measure,
    list_svg_patterns,
)


class TestDenebPresets(unittest.TestCase):
    def test_list_presets(self):
        presets = list_deneb_presets()
        keys = {p["key"] for p in presets}
        self.assertEqual(
            keys,
            {"kpi_card", "bullet_chart", "spark_line",
             "small_multiples", "calendar_heatmap"},
        )

    def test_kpi_card_references_both_fields(self):
        spec = build_kpi_card("Sales", "Sales PY")
        s = json.dumps(spec)
        self.assertIn("datum['Sales']", s)
        self.assertIn("datum['Sales PY']", s)
        # has 3 text layers (title, value, delta)
        self.assertEqual(len(spec["layer"]), 3)

    def test_bullet_chart_topn_and_facet(self):
        spec = build_bullet_chart("Region", "Sales", "Target", top_n=5)
        self.assertEqual(spec["facet"]["row"]["field"], "Region")
        # filter step should reflect top_n
        filt = [t for t in spec["transform"] if "filter" in t]
        self.assertTrue(any("rank <= 5" in t["filter"] for t in filt))

    def test_spark_line_has_no_axes(self):
        spec = build_spark_line("OrderDate", "Sales")
        self.assertIsNone(spec["layer"][0]["encoding"]["x"]["axis"])
        self.assertIsNone(spec["layer"][0]["encoding"]["y"]["axis"])

    def test_small_multiples_default_columns(self):
        spec = build_small_multiples("Region", "Month", "Sales")
        self.assertEqual(spec["facet"]["columns"], 3)

    def test_calendar_heatmap_uses_week_and_day(self):
        spec = build_calendar_heatmap("OrderDate", "OrderCount")
        self.assertEqual(spec["encoding"]["x"]["timeUnit"], "week")
        self.assertEqual(spec["encoding"]["y"]["timeUnit"], "day")

    def test_get_deneb_spec_unknown_raises(self):
        with self.assertRaises(KeyError):
            get_deneb_spec("nonexistent")


class TestBuildDenebVisual(unittest.TestCase):
    def setUp(self):
        self.spec = build_kpi_card("Sales", "Sales PY")
        self.visual = build_deneb_visual(
            visual_id="kpi-x",
            pos={"x": 40, "y": 40, "width": 560, "height": 180},
            table="Orders",
            fields=[
                {"kind": "measure", "name": "Sales"},
                {"kind": "measure", "name": "Sales PY"},
            ],
            vega_lite_spec=self.spec,
        )

    def test_visual_type_is_deneb(self):
        self.assertEqual(self.visual["visual"]["visualType"], DENEB_VISUAL_TYPE)

    def test_query_state_has_dataset_projections(self):
        qs = self.visual["visual"]["query"]["queryState"]
        self.assertIn("dataset", qs)
        self.assertEqual(len(qs["dataset"]["projections"]), 2)

    def test_json_spec_is_literal_string(self):
        json_spec_node = (
            self.visual["visual"]["objects"]["vega"][0]
            ["properties"]["jsonSpec"]["expr"]["Literal"]["Value"]
        )
        # Wrapped in single quotes and contains the spec body
        self.assertTrue(json_spec_node.startswith("'"))
        self.assertTrue(json_spec_node.endswith("'"))
        self.assertIn("vega-lite/v5", json_spec_node)

    def test_position_uses_floats(self):
        self.assertIsInstance(self.visual["position"]["x"], float)
        self.assertIsInstance(self.visual["position"]["height"], float)

    def test_vega_version_default(self):
        version_node = (
            self.visual["visual"]["objects"]["vega"][0]
            ["properties"]["version"]["expr"]["Literal"]["Value"]
        )
        self.assertEqual(version_node, "'5.20.1'")


class TestWriteDenebVisual(unittest.TestCase):
    """End-to-end: PbipToolbox.write_deneb_visual against a real page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.toolbox = PbipToolbox(self.root)
        # Create a page first via write_pbir_page so the directory exists.
        self.report_dir = "MyReport.Report/definition"
        (self.root / self.report_dir).mkdir(parents=True, exist_ok=True)
        page = {
            "id": "p1",
            "displayName": "Overview",
            "visuals": [
                {
                    "id": "v1",
                    "visualType": "card",
                    "queryState": {
                        "Values": {"projections": [
                            {"field": {"Measure": {
                                "Expression": {"SourceRef": {"Entity": "Orders"}},
                                "Property": "Sales",
                            }},
                             "queryRef": "Orders.Sales",
                             "nativeQueryRef": "Sales"}
                        ]}
                    },
                    "x": 0, "y": 0, "width": 200, "height": 100,
                }
            ],
        }
        result = self.toolbox.write_pbir_page(self.report_dir, page)
        self.assertTrue(result.ok, result.message)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_deneb_visual_creates_file(self):
        spec = build_kpi_card("Sales", "Sales PY")
        result = self.toolbox.write_deneb_visual(
            self.report_dir,
            "p1",
            {
                "id": "deneb-kpi",
                "table": "Orders",
                "fields": [
                    {"kind": "measure", "name": "Sales"},
                    {"kind": "measure", "name": "Sales PY"},
                ],
                "vega_lite_spec": spec,
                "x": 40, "y": 40, "width": 560, "height": 180,
            },
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["visual_id"], "deneb-kpi")

        vpath = (self.root / self.report_dir / "pages" / "p1"
                 / "visuals" / "deneb-kpi" / "visual.json")
        self.assertTrue(vpath.exists())
        payload = json.loads(vpath.read_text(encoding="utf-8"))
        self.assertEqual(payload["visual"]["visualType"], DENEB_VISUAL_TYPE)
        # Two measure projections in dataset
        self.assertEqual(
            len(payload["visual"]["query"]["queryState"]["dataset"]["projections"]),
            2,
        )

    def test_write_deneb_visual_missing_fields_fails(self):
        result = self.toolbox.write_deneb_visual(
            self.report_dir,
            "p1",
            {"table": "Orders", "fields": [], "vega_lite_spec": {}},
        )
        self.assertFalse(result.ok)
        self.assertIn("non-empty", result.message)

    def test_write_deneb_visual_missing_page_fails(self):
        result = self.toolbox.write_deneb_visual(
            self.report_dir,
            "no-such-page",
            {
                "table": "Orders",
                "fields": [{"kind": "measure", "name": "Sales"}],
                "vega_lite_spec": {},
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.message)


class TestSvgPatterns(unittest.TestCase):
    def test_list_svg_patterns(self):
        patterns = list_svg_patterns()
        keys = {p["key"] for p in patterns}
        self.assertEqual(keys, {"sparkline", "progress_bar", "rating_stars"})

    def test_sparkline_measure_has_data_category(self):
        m = build_sparkline(
            measure_name="Sales Spark",
            table="SampleData",
            axis_table="Date",
            axis_column="Date",
            value_measure="Total Sales",
        )
        self.assertEqual(m["dataCategory"], "ImageUrl")
        self.assertEqual(m["table"], "SampleData")
        self.assertIn("data:image/svg+xml", m["expression"])
        self.assertIn("polyline", m["expression"])
        self.assertIn("[Total Sales]", m["expression"])

    def test_progress_bar_measure(self):
        m = build_progress_bar(
            measure_name="Sales vs Target",
            table="SampleData",
            value_measure="Total Sales",
            target_measure="Sales Target",
        )
        self.assertEqual(m["dataCategory"], "ImageUrl")
        self.assertIn("rect", m["expression"])
        self.assertIn("[Total Sales]", m["expression"])
        self.assertIn("[Sales Target]", m["expression"])

    def test_rating_stars_measure(self):
        m = build_rating_stars(
            measure_name="CSAT Stars",
            table="Survey",
            score_measure="Avg CSAT",
            max_score=5,
        )
        self.assertEqual(m["dataCategory"], "ImageUrl")
        self.assertIn("polygon", m["expression"])
        # Five threshold checks (one per star)
        self.assertEqual(m["expression"].count("IF(_Score >="), 5)

    def test_build_svg_measure_unknown_raises(self):
        with self.assertRaises(KeyError):
            build_svg_measure("nonexistent")


class TestSvgMeasureWrittenAsImageUrl(unittest.TestCase):
    """Smoke test: SVG measure round-trips through write_tmdl_measures and the
    TMDL file actually carries the ``dataCategory: ImageUrl`` property."""

    def test_measure_tmdl_contains_data_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toolbox = PbipToolbox(root)
            sm_dir = "MyModel.SemanticModel/definition"
            (root / sm_dir / "tables").mkdir(parents=True, exist_ok=True)
            # Pre-create the host table TMDL — write_tmdl_measures wants the
            # ``annotation PBI_ResultType`` marker to know where to splice.
            host = (root / sm_dir / "tables" / "SampleData.tmdl")
            host.write_text(
                "table SampleData\n\n"
                "\tlineageTag: abc\n\n"
                "\tannotation PBI_ResultType = Table\n",
                encoding="utf-8",
            )

            m = build_sparkline(
                measure_name="Sales Spark",
                table="SampleData",
                axis_table="Date",
                axis_column="Date",
                value_measure="Total Sales",
            )
            result = toolbox.write_tmdl_measures(sm_dir, [m])
            self.assertTrue(result.ok, result.message)
            tmdl = host.read_text(encoding="utf-8")
            self.assertIn("dataCategory: ImageUrl", tmdl)
            self.assertIn("measure 'Sales Spark'", tmdl)


if __name__ == "__main__":
    unittest.main(verbosity=2)