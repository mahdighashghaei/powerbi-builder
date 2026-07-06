"""Test Phase 3.4: Conditional Formatting.

Smoke tests for:
  - patterns.conditional_formatting (color_scale_2, color_scale_3, data_bars, icon_set)
  - pbir_generator.apply_* helpers (color scales, data bars, icon sets on visuals)
  - end-to-end CF application to barChart, tableEx, and matrix visuals
"""
import unittest
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from patterns.conditional_formatting import (
    color_scale_2,
    color_scale_3,
    data_bars,
    icon_set,
    list_icon_presets,
    ICON_PRESETS,
)
from mcp_server.pbir_generator import (
    build_bar_chart,
    build_table,
    apply_color_scale,
    apply_data_bars,
    apply_icon_set,
)


class TestColorScales(unittest.TestCase):
    """Test color gradient builders."""

    def test_color_scale_2_basic(self) -> None:
        cf = color_scale_2(table="Sales", measure="Revenue")
        self.assertIn("properties", cf)
        self.assertIn("selector", cf)
        fill = cf["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]
        self.assertEqual(
            fill["Input"]["Measure"]["Property"], "Revenue"
        )
        self.assertEqual(
            fill["Input"]["Measure"]["Expression"]["SourceRef"]["Entity"], "Sales"
        )
        # Default colors
        gradient = fill["FillRule"]["linearGradient2"]
        self.assertIn("min", gradient)
        self.assertIn("max", gradient)
        self.assertEqual(
            gradient["min"]["color"]["Literal"]["Value"], "'#FFC7CE'"
        )
        self.assertEqual(
            gradient["max"]["color"]["Literal"]["Value"], "'#C6EFCE'"
        )

    def test_color_scale_2_custom_colors(self) -> None:
        cf = color_scale_2(
            table="Sales", measure="Revenue",
            min_color="#FF0000", max_color="#00FF00",
        )
        gradient = cf["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]["linearGradient2"]
        self.assertEqual(gradient["min"]["color"]["Literal"]["Value"], "'#FF0000'")
        self.assertEqual(gradient["max"]["color"]["Literal"]["Value"], "'#00FF00'")

    def test_color_scale_2_with_explicit_bounds(self) -> None:
        cf = color_scale_2(
            table="Sales", measure="Revenue",
            min_value=0, max_value=1000,
        )
        gradient = cf["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]["linearGradient2"]
        self.assertEqual(gradient["min"]["value"]["Literal"]["Value"], "0D")
        self.assertEqual(gradient["max"]["value"]["Literal"]["Value"], "1000D")

    def test_color_scale_3_basic(self) -> None:
        cf = color_scale_3(table="Sales", measure="Profit")
        gradient = cf["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]["linearGradient3"]
        self.assertIn("min", gradient)
        self.assertIn("mid", gradient)
        self.assertIn("max", gradient)

    def test_color_scale_3_custom_palette(self) -> None:
        cf = color_scale_3(
            table="Sales", measure="Profit",
            min_color="#FF0000", mid_color="#FFFF00", max_color="#00FF00",
            min_value=-100, mid_value=0, max_value=100,
        )
        gradient = cf["properties"]["fill"]["solid"]["color"]["expr"]["FillRule"]["FillRule"]["linearGradient3"]
        self.assertEqual(gradient["min"]["color"]["Literal"]["Value"], "'#FF0000'")
        self.assertEqual(gradient["mid"]["color"]["Literal"]["Value"], "'#FFFF00'")
        self.assertEqual(gradient["max"]["color"]["Literal"]["Value"], "'#00FF00'")


class TestDataBars(unittest.TestCase):
    """Test data bars builder."""

    def test_data_bars_default(self) -> None:
        cf = data_bars(column_metadata="Sales.Revenue")
        self.assertIn("properties", cf)
        self.assertIn("dataBars", cf["properties"])
        bars = cf["properties"]["dataBars"]
        self.assertIn("positiveColor", bars)
        self.assertIn("negativeColor", bars)
        self.assertIn("axisColor", bars)
        self.assertEqual(cf["selector"]["metadata"], "Sales.Revenue")

    def test_data_bars_custom_colors(self) -> None:
        cf = data_bars(
            column_metadata="Sales.Profit",
            positive_color="#00FF00",
            negative_color="#FF0000",
            hide_text=True,
        )
        bars = cf["properties"]["dataBars"]
        self.assertEqual(
            bars["positiveColor"]["solid"]["color"]["expr"]["Literal"]["Value"],
            "'#00FF00'",
        )
        self.assertEqual(
            bars["hideText"]["expr"]["Literal"]["Value"], "true"
        )


class TestIconSets(unittest.TestCase):
    """Test icon set builders."""

    def test_list_icon_presets(self) -> None:
        presets = list_icon_presets()
        self.assertIsInstance(presets, list)
        self.assertIn("traffic_lights", presets)
        self.assertIn("arrows", presets)
        self.assertIn("stars", presets)

    def test_icon_set_traffic_lights(self) -> None:
        cf = icon_set(preset="traffic_lights")
        icons = cf["properties"]["iconRule"]["iconDefinition"]["icons"]
        self.assertEqual(len(icons), 3)
        self.assertEqual(icons[0]["style"], "TrafficLightGreen")
        self.assertEqual(icons[2]["style"], "TrafficLightRed")

    def test_icon_set_arrows(self) -> None:
        cf = icon_set(preset="arrows", layout="After")
        defn = cf["properties"]["iconRule"]["iconDefinition"]
        self.assertEqual(defn["layout"], "After")
        self.assertEqual(defn["icons"][0]["style"], "ArrowUp")

    def test_icon_set_custom_icons(self) -> None:
        custom = [
            {"style": "StarFull", "percent": 75},
            {"style": "StarEmpty", "percent": 25},
        ]
        cf = icon_set(icons=custom)
        self.assertEqual(
            cf["properties"]["iconRule"]["iconDefinition"]["icons"], custom
        )

    def test_icon_set_invalid_preset(self) -> None:
        with self.assertRaises(ValueError):
            icon_set(preset="not_a_real_preset")


class TestApplyHelpers(unittest.TestCase):
    """Test that the apply_* helpers correctly attach CF to visuals."""

    def test_apply_color_scale_to_bar_chart(self) -> None:
        visual = build_bar_chart(
            "bar-1", {"x": 0, "y": 0, "height": 200, "width": 300},
            "Sales", "Region", "Revenue",
        )
        cf = color_scale_2(table="Sales", measure="Revenue")
        result = apply_color_scale(visual, cf)
        self.assertIn("objects", result["visual"])
        self.assertIn("dataPoint", result["visual"]["objects"])
        self.assertEqual(len(result["visual"]["objects"]["dataPoint"]), 1)

    def test_apply_data_bars_to_table(self) -> None:
        visual = build_table(
            "tbl-1", {"x": 0, "y": 0, "height": 200, "width": 300},
            [("Sales", "Region"), ("Sales", "Revenue", "measure")],
        )
        cf = data_bars(column_metadata="Sales.Revenue")
        result = apply_data_bars(visual, cf)
        self.assertIn("columnFormatting", result["visual"]["objects"])

    def test_apply_icon_set_to_table(self) -> None:
        visual = build_table(
            "tbl-1", {"x": 0, "y": 0, "height": 200, "width": 300},
            [("Sales", "Revenue", "measure")],
        )
        cf = icon_set(preset="traffic_lights")
        result = apply_icon_set(visual, cf)
        self.assertIn("values", result["visual"]["objects"])


if __name__ == "__main__":
    unittest.main()
