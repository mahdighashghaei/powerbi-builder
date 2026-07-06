"""Tests for the A2UI protocol — dynamic UI manifest (Wave B2).

Verifies:
  * build_manifest produces the a2ui/1.0 schema from pages + visuals.
  * component types map correctly from PBIR visual types.
  * render_ui_manifest reads a built PBIP and writes ui-manifest.json.
  * the tool returns the standard envelope.
  * a real generated PBIP yields a manifest with components.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
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

from adk.a2ui import build_manifest, render_ui_manifest  # noqa: E402


class TestBuildManifest(unittest.TestCase):
    """build_manifest converts pages/visuals into an A2UI manifest."""

    def test_schema_and_project(self):
        manifest = build_manifest("SalesDashboard", [])
        self.assertEqual(manifest["schema"], "a2ui/1.0")
        self.assertEqual(manifest["project"], "SalesDashboard")
        self.assertEqual(manifest["componentCount"], 0)
        self.assertEqual(manifest["layout"]["canvas"], "1280x720")

    def test_components_from_visuals(self):
        pages = [
            {
                "displayName": "Summary",
                "visuals": [
                    {"id": "card-0", "visualType": "card", "title": "Total Amount"},
                    {"id": "bar-1", "visualType": "barChart", "title": "By Region"},
                    {"id": "tbl-2", "visualType": "table", "title": "Details"},
                ],
            }
        ]
        manifest = build_manifest("P", pages)
        self.assertEqual(manifest["componentCount"], 3)
        types = [c["type"] for c in manifest["components"]]
        self.assertEqual(types, ["card", "chart", "table"])
        # The bar chart carries a chartType.
        chart = manifest["components"][1]
        self.assertEqual(chart["chartType"], "bar")
        # Page names recorded in layout.
        self.assertEqual(manifest["layout"]["pages"], ["Summary"])

    def test_unknown_visual_type_becomes_generic(self):
        manifest = build_manifest("P", [{"displayName": "x", "visuals": [
            {"id": "v", "visualType": "weirdType", "title": "t"}
        ]}])
        self.assertEqual(manifest["components"][0]["type"], "generic")

    def test_position_preserved(self):
        manifest = build_manifest("P", [{"displayName": "x", "visuals": [
            {"id": "v", "visualType": "card", "title": "t", "position": {"x": 10, "y": 20}}
        ]}])
        self.assertEqual(manifest["components"][0]["position"], {"x": 10, "y": 20})


class TestRenderUiManifest(unittest.TestCase):
    """render_ui_manifest reads a built PBIP and emits ui-manifest.json."""

    def _write_csv(self, path: Path) -> None:
        path.write_text(
            "OrderDate,Region,Product,Quantity,Amount\n"
            "2024-01-05,North,Widget,10,250.50\n"
            "2024-01-07,South,Gadget,5,99.99\n",
            encoding="utf-8",
        )

    def test_render_on_real_build(self):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            csv = td_path / "sales.csv"
            self._write_csv(csv)
            out = td_path / "out"
            orch = OrchestratorAgent(str(out))
            orch.run(source_path=str(csv), business_description="Monthly sales by region")
            project = next(p for p in out.iterdir() if p.is_dir())
            r = render_ui_manifest(str(project))
            self.assertTrue(r["ok"], f"render failed: {r}")
            self.assertEqual(r["tool"], "render_ui_manifest")
            data = r["data"]
            self.assertEqual(data["schema"], "a2ui/1.0")
            self.assertGreater(data["componentCount"], 0)
            # The manifest file was written to disk.
            manifest_file = project / "ui-manifest.json"
            self.assertTrue(manifest_file.is_file())
            on_disk = json.loads(manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["project"], data["project"])

    def test_render_missing_path(self):
        r = render_ui_manifest("/nonexistent/path/xyz")
        self.assertFalse(r["ok"])
        self.assertTrue(r["errors"])

    def test_render_no_report_folder(self):
        with tempfile.TemporaryDirectory() as td:
            r = render_ui_manifest(td)
            self.assertFalse(r["ok"])
            self.assertIn("Report", r["message"])


if __name__ == "__main__":
    unittest.main()
