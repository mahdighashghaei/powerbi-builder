"""Test Phase 3.3: Theme System.

Smoke tests for:
  - patterns.themes (list_themes, get_theme, make_custom_theme)
  - adk.tools.theme_tools (list_theme_presets, apply_theme)
  - end-to-end theme write + validation
"""
import unittest
import sys
import json
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from patterns.themes import list_themes, get_theme, make_custom_theme
from adk.tools.theme_tools import list_theme_presets, apply_theme
from mcp_server.server import PbipToolbox


class TestThemePresets(unittest.TestCase):
    """Test the preset theme library."""

    def test_list_themes(self) -> None:
        themes = list_themes()
        self.assertIsInstance(themes, list)
        self.assertGreater(len(themes), 0)
        self.assertIn("default", themes)
        self.assertIn("corporate_blue", themes)
        self.assertIn("modern_dark", themes)

    def test_get_theme_default(self) -> None:
        theme = get_theme("default")
        self.assertIsInstance(theme, dict)
        self.assertEqual(theme["name"], "PowerBI Builder Default")
        self.assertIn("dataColors", theme)
        self.assertIsInstance(theme["dataColors"], list)
        self.assertEqual(len(theme["dataColors"]), 8)
        self.assertIn("background", theme)
        self.assertIn("foreground", theme)
        self.assertIn("tableAccent", theme)

    def test_get_theme_corporate_blue(self) -> None:
        theme = get_theme("corporate_blue")
        self.assertEqual(theme["name"], "Corporate Blue")
        self.assertEqual(theme["background"], "#FFFFFF")
        self.assertEqual(len(theme["dataColors"]), 8)

    def test_get_theme_unknown_key_falls_back(self) -> None:
        theme = get_theme("unknown_key_xyz")
        # Fallback to default
        self.assertEqual(theme["name"], "PowerBI Builder Default")

    def test_make_custom_theme(self) -> None:
        theme = make_custom_theme(
            name="Test Brand",
            data_colors=["#FF0000", "#00FF00", "#0000FF"],
        )
        self.assertEqual(theme["name"], "Test Brand")
        self.assertEqual(len(theme["dataColors"]), 8)  # cycled to 8
        self.assertEqual(theme["dataColors"][0], "#FF0000")
        self.assertEqual(theme["dataColors"][3], "#FF0000")  # cycled

    def test_make_custom_theme_single_color(self) -> None:
        theme = make_custom_theme(
            name="Mono",
            data_colors=["#AABBCC"],
        )
        self.assertEqual(len(theme["dataColors"]), 8)
        # All 8 slots filled with the same color
        for c in theme["dataColors"]:
            self.assertEqual(c, "#AABBCC")


class TestThemeADKTools(unittest.TestCase):
    """Test the ADK tool wrappers."""

    def test_list_theme_presets(self) -> None:
        result = list_theme_presets()
        self.assertIn("presets", result)
        self.assertIn("count", result)
        self.assertGreater(result["count"], 0)
        presets = result["presets"]
        self.assertIsInstance(presets, list)
        # Check one preset structure
        p0 = presets[0]
        self.assertIn("key", p0)
        self.assertIn("description", p0)

    def test_apply_theme_preset(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a minimal report structure
            report_root = Path(tmpdir) / "Test.Report"
            report_root.mkdir(parents=True)

            result = apply_theme(
                output_dir="Test.Report",
                preset="vibrant",
                output_root=tmpdir,
            )
            self.assertTrue(result["ok"])
            self.assertIn("path", result.get("data", {}))

            # Theme now written to StaticResources/RegisteredResources/
            theme_file = report_root / "StaticResources" / "RegisteredResources" / "theme.json"
            self.assertTrue(theme_file.exists())
            with open(theme_file, encoding="utf-8") as f:
                theme = json.load(f)
            self.assertEqual(theme["name"], "Vibrant")

    def test_apply_custom_palette(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            report_root = Path(tmpdir) / "Custom.Report"
            report_root.mkdir(parents=True)

            result = apply_theme(
                output_dir="Custom.Report",
                custom_palette=["#112233", "#445566", "#778899"],
                custom_name="My Brand",
                output_root=tmpdir,
            )
            self.assertTrue(result["ok"])

            theme_file = report_root / "StaticResources" / "RegisteredResources" / "theme.json"
            self.assertTrue(theme_file.exists())
            with open(theme_file, encoding="utf-8") as f:
                theme = json.load(f)
            self.assertEqual(theme["name"], "My Brand")
            self.assertEqual(len(theme["dataColors"]), 8)
            self.assertEqual(theme["dataColors"][0], "#112233")


class TestThemeIntegration(unittest.TestCase):
    """End-to-end theme write + validation."""

    def test_write_all_presets(self) -> None:
        import tempfile
        for key in list_themes():
            with tempfile.TemporaryDirectory() as tmpdir:
                report_root = Path(tmpdir) / f"{key}.Report"
                report_root.mkdir(parents=True)

                tb = PbipToolbox(tmpdir)
                theme = get_theme(key)
                result = tb.write_theme_json(f"{key}.Report", theme)
                self.assertTrue(result.ok, f"{key} theme write failed: {result.message}")

                theme_file = report_root / "StaticResources" / "RegisteredResources" / "theme.json"
                self.assertTrue(theme_file.exists(), f"{key} theme.json not found")


class TestThemeDefinitionSuffixStripped(unittest.TestCase):
    """Regression: apply_theme's and write_theme_json's docstrings used to
    (wrongly) instruct callers to pass a ".../definition"-suffixed
    output_dir -- the ONE PBIR tool where that's incorrect, since
    report.json's customTheme reference resolves relative to the bare
    .Report folder. A caller following the old docstring wrote a
    theme.json Power BI Desktop never read, so apply_theme reported
    ok=True on every call but the change never took visual effect.
    PbipToolbox.write_theme_json now strips a trailing /definition
    suffix defensively, regardless of what the caller passes."""

    def _write_and_check(self, tmpdir, output_dir, project="Test"):
        report_root = Path(tmpdir) / f"{project}.Report"
        report_root.mkdir(parents=True, exist_ok=True)
        tb = PbipToolbox(tmpdir)
        result = tb.write_theme_json(output_dir, get_theme("modern_dark"))
        self.assertTrue(result.ok, result.message)
        theme_file = report_root / "StaticResources" / "RegisteredResources" / "theme.json"
        self.assertTrue(theme_file.exists(), f"expected {theme_file} to exist")
        return theme_file

    def test_bare_report_dir_writes_to_correct_location(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_and_check(tmpdir, "Test.Report")

    def test_definition_suffixed_dir_is_normalized_to_same_location(self) -> None:
        """The old (wrong) docstring told callers to pass
        "Test.Report/definition" -- this must land in the SAME place as
        the correct "Test.Report" form, not inside definition/."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            theme_file = self._write_and_check(tmpdir, "Test.Report/definition")
            # must NOT have created a shadow copy inside definition/
            shadow = (
                Path(tmpdir) / "Test.Report" / "definition"
                / "StaticResources" / "RegisteredResources" / "theme.json"
            )
            self.assertFalse(shadow.exists())
            with open(theme_file, encoding="utf-8") as f:
                theme = json.load(f)
            self.assertEqual(theme["name"], "Modern Dark")

    def test_definition_suffix_with_trailing_slash_is_normalized(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_and_check(tmpdir, "Test.Report/definition/")

    def test_apply_theme_tool_also_normalizes(self) -> None:
        """adk.tools.theme_tools.apply_theme delegates to write_theme_json
        -- confirm the normalization applies through the full ADK tool
        call path, not just the raw PbipToolbox method."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            report_root = Path(tmpdir) / "Test.Report"
            report_root.mkdir(parents=True)
            result = apply_theme(
                output_dir="Test.Report/definition",
                preset="earth_tones",
                output_root=tmpdir,
            )
            self.assertTrue(result["ok"])
            theme_file = report_root / "StaticResources" / "RegisteredResources" / "theme.json"
            self.assertTrue(theme_file.exists())
            shadow = report_root / "definition" / "StaticResources" / "RegisteredResources" / "theme.json"
            self.assertFalse(shadow.exists())


if __name__ == "__main__":
    unittest.main()
