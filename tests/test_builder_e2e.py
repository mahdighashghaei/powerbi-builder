"""End-to-end builder smoke test — proves the full pipeline actually works.

This is the single test that, if green, means the powerbi-builder end-to-end
pipeline is functional: feed it a real CSV + description, run the real
OrchestratorAgent (no mocks, no LLM — the deterministic offline path), and
assert the generated .pbip folder is structurally valid AND opens cleanly in
Power BI Desktop's eyes (valid TMDL, valid PBIR, no ghost refs, valid JSON).

Run standalone::

    python -m pytest tests/test_builder_e2e.py -v
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

# Force the deterministic offline path (no LLM) so the test is reproducible.
import os  # noqa: E402

os.environ["GOOGLE_API_KEY"] = ""


class TestBuilderEndToEnd(unittest.TestCase):
    """The full builder pipeline on a real CSV produces a valid PBIP."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "out"
        cls.csv = Path(cls._tmp.name) / "sales.csv"
        cls.csv.write_text(
            "OrderDate,Region,Product,Quantity,Amount\n"
            "2024-01-05,North,Widget,10,250.50\n"
            "2024-01-07,South,Gadget,5,99.99\n"
            "2024-02-10,East,Widget,8,200.00\n"
            "2024-02-15,West,Gadget,12,239.88\n"
            "2024-03-01,North,Gadget,3,89.97\n",
            encoding="utf-8",
        )
        from mcp_server import highlevel as hl  # noqa: E402

        cls.result = hl.generate_pbip(
            str(cls.csv),
            "Monthly sales by region with revenue and order trends",
            output_root=str(cls.out),
        )
        cls.project = next(
            p for p in cls.out.iterdir() if p.is_dir() and any(p.glob("*.SemanticModel"))
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # -- the build itself ------------------------------------------------

    def test_build_succeeded(self):
        self.assertTrue(self.result["ok"], f"build failed: {self.result}")
        val = self.result["data"].get("validation") or {}
        self.assertTrue(val.get("ok"), f"validation failed: {val}")

    def test_pbip_entry_file_exists(self):
        pbip = next(self.project.glob("*.pbip"), None)
        self.assertIsNotNone(pbip, "no .pbip entry file")

    # -- semantic model --------------------------------------------------

    def test_semantic_model_has_tmdl(self):
        sm = next(self.project.glob("*.SemanticModel"), None)
        self.assertIsNotNone(sm, "no .SemanticModel folder")
        tables = list((sm / "definition" / "tables").glob("*.tmdl"))
        self.assertGreater(len(tables), 0, "no TMDL table files")
        # database.tmdl + model.tmdl must exist.
        self.assertTrue((sm / "definition" / "database.tmdl").is_file())
        self.assertTrue((sm / "definition" / "model.tmdl").is_file())

    def test_columns_typed_and_measures_generated(self):
        from utils.tmdl_parser import read_semantic_model  # noqa: E402

        sm = next(self.project.glob("*.SemanticModel"))
        model = read_semantic_model(sm)
        self.assertGreater(len(model.get("tables", [])), 0)
        table = model["tables"][0]
        self.assertGreaterEqual(len(table.get("columns", [])), 4)
        # At least a few measures were generated.
        measure_names = model.get("measure_names", [])
        self.assertGreaterEqual(len(measure_names), 3, f"measures: {measure_names}")

    # -- report ----------------------------------------------------------

    def test_report_has_pages_and_visuals(self):
        report = next(self.project.glob("*.Report"), None)
        self.assertIsNotNone(report, "no .Report folder")
        pages_dir = report / "definition" / "pages"
        self.assertTrue(pages_dir.is_dir(), "no pages dir")
        page_folders = [p for p in pages_dir.iterdir() if p.is_dir()]
        self.assertGreater(len(page_folders), 0, "no page folders")
        # Each page must have at least one visual.
        for page in page_folders:
            visuals = list((page / "visuals").glob("*"))
            self.assertGreater(len(visuals), 0, f"page {page.name} has no visuals")

    def test_report_json_and_pages_index_valid(self):
        report = next(self.project.glob("*.Report"))
        rjson = report / "definition" / "report.json"
        self.assertTrue(rjson.is_file(), "no report.json")
        data = json.loads(rjson.read_text(encoding="utf-8"))
        self.assertIn("themeCollection", data)
        pages_json = report / "definition" / "pages" / "pages.json"
        self.assertTrue(pages_json.is_file(), "no pages.json")
        pdata = json.loads(pages_json.read_text(encoding="utf-8"))
        self.assertIn("pageOrder", pdata)
        self.assertGreater(len(pdata["pageOrder"]), 0)

    # -- no ghost references (would break Desktop) -----------------------

    def test_no_visual_ghost_refs(self):
        from adk.tools.review_tools import check_visual_references  # noqa: E402

        result = check_visual_references(str(self.project))
        self.assertTrue(result["ok"], f"check_visual_references failed: {result}")
        self.assertEqual(len(result.get("ghost_refs", [])), 0)

    # -- artifacts written alongside the build ---------------------------

    def test_readme_written(self):
        self.assertTrue((self.project / "README.md").is_file())

    def test_build_spec_written_and_valid(self):
        spec_file = self.project / "build.spec.json"
        self.assertTrue(spec_file.is_file(), "build.spec.json missing")
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        self.assertEqual(spec["schema_version"], "1.0")
        self.assertTrue(spec["project_name"])
        self.assertIn("schema", spec)
        self.assertIsInstance(spec["measures"], list)
        self.assertIsInstance(spec["trajectory"], list)
        self.assertGreater(len(spec["trajectory"]), 0, "spec trajectory empty")

    # -- structural validation passes ------------------------------------

    def test_validate_pbip_structure_passes(self):
        from mcp_server.server import PbipToolbox  # noqa: E402

        tb = PbipToolbox(str(self.out))
        res = tb.validate_pbip_structure(str(self.project))
        self.assertTrue(res.ok, f"validate_pbip_structure failed: {res.errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
