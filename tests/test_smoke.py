"""Test suite for the PowerBI Builder.

Run with::

    python -m pytest tests/ -v

These tests are stdlib-only where possible so they run without pytest too
(via ``python tests/test_smoke.py``), which keeps the single-command
deployability promise. If pytest is available it auto-discovers them.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# make project root importable
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mcp_server.schema_inference import infer_csv_schema, infer_json_schema  # noqa: E402
from mcp_server.server import PbipToolbox, ToolResult  # noqa: E402
from utils.security import (  # noqa: E402
    AuditLogger, JSONValidationError, PathSecurityError, safe_join,
    serialize_json, validate_json_string,
)
from utils.tmdl_parser import parse_table_tmdl, infer_connection_type  # noqa: E402
from agents.relationship_agent import detect_relationships  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# security tests
# ---------------------------------------------------------------------------


class TestSecurity(unittest.TestCase):
    """Verify path-traversal defence and JSON validation."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()

    def test_safe_join_allows_nested(self) -> None:
        p = safe_join(self.root, "a", "b", "c.tmdl")
        self.assertTrue(str(p).startswith(self.root))

    def test_safe_join_blocks_dotdot(self) -> None:
        with self.assertRaises(PathSecurityError):
            safe_join(self.root, "..", "evil")
        with self.assertRaises(PathSecurityError):
            safe_join(self.root, "ok", "..\\..\\evil")

    def test_safe_join_blocks_absolute_outside(self) -> None:
        # an absolute path pointing elsewhere must be rejected
        with self.assertRaises(PathSecurityError):
            safe_join(self.root, "/etc/passwd")

    def test_validate_json_string_accepts_valid(self) -> None:
        self.assertEqual(validate_json_string('{"a": 1}'), {"a": 1})

    def test_validate_json_string_rejects_invalid(self) -> None:
        with self.assertRaises(JSONValidationError):
            validate_json_string("{not json}")

    def test_serialize_json_roundtrip(self) -> None:
        obj = {"b": 2, "a": 1}
        roundtripped = json.loads(serialize_json(obj))
        self.assertEqual(roundtripped, obj)

    def test_serialize_json_handles_numpy_scalars(self) -> None:
        """Regression: pandas .min()/.max() etc. return numpy scalars
        (e.g. numpy.int64), not native Python int/float. json.dumps()
        can't encode those by default -- this only ever broke a caller
        that actually JSON-encodes a profile dict built from such values
        (e.g. the real MCP stdio transport), not in-process callers that
        just pass the dict around, which is why it went unnoticed until
        generate_pbip was made reachable over a genuine MCP round trip."""
        import numpy as np

        obj = {
            "min": np.int64(3), "max": np.float64(9.5),
            "flag": np.bool_(True), "nested": {"n": np.int64(7)},
        }
        roundtripped = json.loads(serialize_json(obj))
        self.assertEqual(roundtripped, {"min": 3, "max": 9.5, "flag": True, "nested": {"n": 7}})

    def test_serialize_json_still_rejects_truly_unserializable(self) -> None:
        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            serialize_json({"x": Unserializable()})


# ---------------------------------------------------------------------------
# schema inference tests
# ---------------------------------------------------------------------------


class TestSchemaInference(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_csv_inference_types(self) -> None:
        p = _write_csv(
            Path(self.tmp) / "sales.csv",
            [
                "OrderDate,Region,Quantity,Amount",
                "2024-01-05,North,10,250.50",
                "2024-02-10,South,5,99.99",
            ],
        )
        schema = infer_csv_schema(p)
        types = {c["name"]: c["dataType"] for c in schema["columns"]}
        self.assertEqual(types["OrderDate"], "dateTime")
        self.assertEqual(types["Region"], "string")
        self.assertEqual(types["Quantity"], "int64")
        self.assertEqual(types["Amount"], "double")
        self.assertEqual(schema["table_name"], "sales")

    def test_csv_inference_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            infer_csv_schema(Path(self.tmp) / "nope.csv")

    def test_json_schema_inference(self) -> None:
        p = Path(self.tmp) / "schema.json"
        p.write_text(json.dumps({
            "name": "Orders",
            "columns": [
                {"name": "Id", "dataType": "int64"},
                {"name": "Total", "dataType": "double"},
            ],
        }), encoding="utf-8")
        schema = infer_json_schema(p)
        self.assertEqual(schema["table_name"], "Orders")
        self.assertEqual(len(schema["columns"]), 2)

    def test_numeric_column_min_max_are_native_python_types(self) -> None:
        """Regression: _profile_column's min/max used to leave raw pandas
        (numpy) scalars in the profile dict -- every other stat in the
        same function already cast to a native type, so this was a narrow
        miss, only ever surfaced by a caller that actually JSON-encodes
        the profile (json.dumps can't serialize numpy.int64/float64)."""
        import pandas as pd
        from mcp_server.schema_inference import _profile_column

        series = pd.Series([10, 20, 30, 40])
        prof = _profile_column(series, "Quantity", "int64", row_count=4)
        self.assertIsInstance(prof["min"], int)
        self.assertIsInstance(prof["max"], int)
        self.assertEqual(prof["min"], 10)
        self.assertEqual(prof["max"], 40)
        json.dumps(prof)  # must not raise


# ---------------------------------------------------------------------------
# MCP toolbox tests
# ---------------------------------------------------------------------------


class TestPbipToolbox(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.tb = PbipToolbox(self.root)
        self.csv = _write_csv(
            Path(self.root) / "src.csv",
            ["OrderDate,Region,Quantity,Amount",
             "2024-01-05,North,10,250.50",
             "2024-02-10,South,5,99.99"],
        )

    def test_read_csv_schema(self) -> None:
        r = self.tb.read_csv_schema(str(self.csv))
        self.assertTrue(r.ok)
        self.assertEqual(len(r.data["schema"]["columns"]), 4)

    def test_write_tmdl_table_creates_file(self) -> None:
        r = self.tb.write_tmdl_table(
            "Demo.SemanticModel/definition",
            {"name": "Sales",
             "columns": [{"name": "Amt", "dataType": "double"}]},
        )
        self.assertTrue(r.ok, r.errors)
        target = Path(r.data["path"])
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertTrue(content.strip().startswith("table Sales"))
        self.assertIn("dataType: double", content)

    def test_write_tmdl_table_rejects_traversal(self) -> None:
        r = self.tb.write_tmdl_table(
            "../../../etc/evil",
            {"name": "X", "columns": [{"name": "Y", "dataType": "string"}]},
        )
        self.assertFalse(r.ok)

    def test_write_tmdl_measures_appends(self) -> None:
        # need a table file first
        self.tb.write_tmdl_table(
            "Demo.SemanticModel/definition",
            {"name": "Sales",
             "columns": [{"name": "Amt", "dataType": "double"}]},
        )
        r = self.tb.write_tmdl_measures(
            "Demo.SemanticModel/definition",
            [{"name": "Total Sales", "expression": "SUM(Sales[Amt])",
              "table": "Sales", "displayFolder": "Revenue",
              "description": "Sum", "formatString": "$ #,##0.00"}],
        )
        self.assertTrue(r.ok, r.errors)
        tmdl = (Path(self.root) / "Demo.SemanticModel" / "definition"
                / "tables" / "Sales.tmdl")
        # TMDL uses single quotes, not DAX bracket notation
        self.assertIn("measure 'Total Sales'", tmdl.read_text(encoding="utf-8"))

    def test_write_tmdl_measures_dedups_on_rerun(self) -> None:
        """Re-writing the same measure must replace, not duplicate.

        Regression for the Power BI Desktop ``PFE_TM_OBJECT_NAME_ALREADY_EXISTS``
        error: when DAXAgent re-runs (Phase 4 feedback loop) or ``add_measure``
        is called twice from ``adk web``, the same measure name must NOT appear
        twice in the TMDL — Desktop rejects a project with duplicate measures.
        """
        self.tb.write_tmdl_table(
            "Demo.SemanticModel/definition",
            {"name": "Sales",
             "columns": [{"name": "Amt", "dataType": "double"}]},
        )
        measures = [
            {"name": "Total Sales", "expression": "SUM(Sales[Amt])",
             "table": "Sales", "displayFolder": "Revenue",
             "formatString": "$ #,##0.00"},
            {"name": "Order Count", "expression": "COUNTROWS('Sales')",
             "table": "Sales", "displayFolder": "Orders",
             "formatString": "#,##0"},
        ]
        # write two measures
        self.tb.write_tmdl_measures("Demo.SemanticModel/definition", measures)
        # re-write ONE of them (simulates a rerun / add_measure)
        self.tb.write_tmdl_measures(
            "Demo.SemanticModel/definition",
            [measures[0]],  # Total Sales again
        )
        tmdl = (Path(self.root) / "Demo.SemanticModel" / "definition"
                / "tables" / "Sales.tmdl").read_text(encoding="utf-8")
        # each measure must appear exactly once
        self.assertEqual(tmdl.count("measure 'Total Sales'"), 1,
                         "duplicate measure 'Total Sales' — Desktop would reject")
        self.assertEqual(tmdl.count("measure 'Order Count'"), 1,
                         "unrelated measure 'Order Count' was wrongly removed")

    def test_write_pbir_page_with_visuals(self) -> None:
        r = self.tb.write_pbir_page(
            "Demo.Report/definition",
            {"id": "p1", "displayName": "Summary",
             "visuals": [
                 {"id": "v1", "visualType": "card", "title": "KPI",
                  "x": 0, "y": 0, "width": 200, "height": 150},
                 {"id": "v2", "visualType": "barChart", "title": "Chart",
                  "x": 220, "y": 0, "width": 300, "height": 200},
             ]},
        )
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(len(r.data["visuals"]), 2)
        # each visual.json must be valid + have required PBIR fields
        for vpath in r.data["visuals"]:
            data = json.loads(Path(vpath).read_text(encoding="utf-8"))
            self.assertIn("$schema", data)
            self.assertIn("name", data)
            self.assertIn("position", data)
            self.assertIn("tabOrder", data["position"])
            self.assertIn("visual", data)
            self.assertIn("visualType", data["visual"])
            self.assertIn("query", data["visual"])
            self.assertIn("sortDefinition", data["visual"]["query"])

    def test_write_pbir_page_rejects_bad_visual_type(self) -> None:
        r = self.tb.write_pbir_page(
            "Demo.Report/definition",
            {"id": "p1", "displayName": "X",
             "visuals": [{"id": "v1", "visualType": "nonsense"}]},
        )
        self.assertFalse(r.ok)

    def test_write_theme_json_default(self) -> None:
        r = self.tb.write_theme_json("Demo.Report/definition")
        self.assertTrue(r.ok)
        theme = json.loads(Path(r.data["path"]).read_text(encoding="utf-8"))
        self.assertIn("dataColors", theme)

    def test_validate_pbip_structure_reports_missing_report(self) -> None:
        # only write a semantic model, no report
        self.tb.write_tmdl_table(
            "Demo.SemanticModel/definition",
            {"name": "Sales",
             "columns": [{"name": "Amt", "dataType": "double"}]},
        )
        r = self.tb.validate_pbip_structure(self.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("Report" in e for e in r.errors))


# ---------------------------------------------------------------------------
# ToolResult contract
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TMDL parser tests
# ---------------------------------------------------------------------------


class TestTmdlParser(unittest.TestCase):
    _TABLE_TMDL = (
        "table Sales\n\n"
        "\tlineageTag: abc123\n\n"
        "\tcolumn OrderDate\n"
        "\t\tdataType: dateTime\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: OrderDate\n\n"
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tsummarizeBy: none\n"
        "\t\tsourceColumn: Region\n\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
        "\t\tsummarizeBy: sum\n"
        "\t\tsourceColumn: Amount\n\n"
        "\tmeasure 'Total Amount' = SUM(Sales[Amount])\n"
        "\t\tformatString: $ #,##0.00\n"
        "\t\tdisplayFolder: Revenue\n\n"
        "\tpartition Sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        '\t\t\tlet\n'
        '\t\t\t\tRawData = Csv.Document(File.Contents("C:/data/sales.csv"),[Delimiter=","])\n'
        "\t\t\tin\n"
        "\t\t\t\tRawData\n"
    )

    def test_parse_table_name(self) -> None:
        result = parse_table_tmdl(self._TABLE_TMDL)
        self.assertEqual(result["table_name"], "Sales")

    def test_parse_columns(self) -> None:
        result = parse_table_tmdl(self._TABLE_TMDL)
        names = [c["name"] for c in result["columns"]]
        self.assertIn("OrderDate", names)
        self.assertIn("Region", names)
        self.assertIn("Amount", names)

    def test_parse_column_types(self) -> None:
        result = parse_table_tmdl(self._TABLE_TMDL)
        types = {c["name"]: c["dataType"] for c in result["columns"]}
        self.assertEqual(types["OrderDate"], "dateTime")
        self.assertEqual(types["Region"], "string")
        self.assertEqual(types["Amount"], "double")

    def test_parse_measures(self) -> None:
        result = parse_table_tmdl(self._TABLE_TMDL)
        measure_names = [m["name"] for m in result["measures"]]
        self.assertIn("Total Amount", measure_names)

    def test_parse_partition_source(self) -> None:
        result = parse_table_tmdl(self._TABLE_TMDL)
        self.assertIn("Csv.Document", result["partition_source"])

    def test_infer_connection_type_csv(self) -> None:
        self.assertEqual(infer_connection_type('Csv.Document(File.Contents("x.csv"))'), "csv")

    def test_infer_connection_type_sql(self) -> None:
        self.assertEqual(infer_connection_type('Sql.Database("server","db")'), "sql")

    def test_infer_connection_type_other(self) -> None:
        self.assertEqual(infer_connection_type(""), "other")


# ---------------------------------------------------------------------------
# read_pbip_schema MCP tool test
# ---------------------------------------------------------------------------


class TestReadPbipSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.tb = PbipToolbox(self.root)

    def _make_pbip(self) -> str:
        """Create a minimal fake PBIP structure for testing."""
        pbip_dir = Path(self.root) / "TestProject"
        sm_dir = pbip_dir / "TestProject.SemanticModel" / "definition" / "tables"
        sm_dir.mkdir(parents=True)
        tmdl = (
            "table Orders\n\n"
            "\tlineageTag: x\n\n"
            "\tcolumn Id\n"
            "\t\tdataType: int64\n"
            "\t\tsummarizeBy: sum\n\n"
            "\tcolumn Name\n"
            "\t\tdataType: string\n"
            "\t\tsummarizeBy: none\n\n"
            "\tmeasure 'Count Orders' = COUNTROWS('Orders')\n"
            "\t\tformatString: #,##0\n"
            "\t\tdisplayFolder: Orders\n"
        )
        (sm_dir / "Orders.tmdl").write_text(tmdl, encoding="utf-8")
        return str(pbip_dir)

    def test_read_pbip_schema_ok(self) -> None:
        pbip_dir = self._make_pbip()
        # read_pbip_schema reads from an absolute path (not under allowed_root)
        r = self.tb.read_pbip_schema(pbip_dir)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.data["schema"]["table_name"], "Orders")
        self.assertEqual(len(r.data["schema"]["columns"]), 2)
        self.assertIn("Count Orders", r.data["existing_measures"])

    def test_read_pbip_schema_missing(self) -> None:
        r = self.tb.read_pbip_schema(str(Path(self.root) / "nonexistent"))
        self.assertFalse(r.ok)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Relationship detection tests
# ---------------------------------------------------------------------------


class TestRelationshipDetection(unittest.TestCase):
    def _make_tables(self) -> list[dict]:
        return [
            {
                "table_name": "Orders",
                "columns": [
                    {"name": "OrderId", "dataType": "int64"},
                    {"name": "CustomerId", "dataType": "int64"},
                    {"name": "RegionId", "dataType": "int64"},
                    {"name": "Amount", "dataType": "double"},
                ],
                "measures": [],
            },
            {
                "table_name": "Customers",
                "columns": [
                    {"name": "Id", "dataType": "int64"},
                    {"name": "Name", "dataType": "string"},
                ],
                "measures": [],
            },
            {
                "table_name": "Region",
                "columns": [
                    {"name": "Id", "dataType": "int64"},
                    {"name": "RegionName", "dataType": "string"},
                ],
                "measures": [],
            },
        ]

    def test_detects_customer_fk(self) -> None:
        rels = detect_relationships(self._make_tables())
        from_cols = [(r["from_table"], r["from_column"]) for r in rels]
        self.assertIn(("Orders", "CustomerId"), from_cols)

    def test_detects_region_fk(self) -> None:
        rels = detect_relationships(self._make_tables())
        from_cols = [(r["from_table"], r["from_column"]) for r in rels]
        self.assertIn(("Orders", "RegionId"), from_cols)

    def test_no_relationships_single_table(self) -> None:
        rels = detect_relationships([self._make_tables()[0]])
        self.assertEqual(rels, [])

    def test_to_cardinality_is_one(self) -> None:
        rels = detect_relationships(self._make_tables())
        for r in rels:
            self.assertEqual(r["to_cardinality"], "one")


class TestToolResult(unittest.TestCase):
    def test_as_dict_has_required_keys(self) -> None:
        tr = ToolResult(ok=True, tool="t", message="m")
        d = tr.as_dict()
        for key in ("ok", "tool", "message", "data", "errors", "timestamp"):
            self.assertIn(key, d)


class TestZipUtils(unittest.TestCase):
    """utils/zip_utils.py is the shared zip implementation adk/plugin.py
    and mcp_server both need -- it lives in utils/ specifically so
    mcp_server never has to import from adk/ to get it. Import it here
    directly (not via adk.plugin) to prove that."""

    def test_zip_project_dir_importable_without_adk(self) -> None:
        # Importing utils.zip_utils on its own must not pull in adk.plugin
        # (mcp_server needs this helper without depending on adk/). Checked
        # via the module's own __module__/no-adk-import shape rather than
        # sys.modules, since other test files in the same suite process may
        # have already imported adk.plugin for unrelated reasons.
        import utils.zip_utils as zu
        self.assertNotIn("adk", zu.__name__)
        with open(zu.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("import adk", source)

        from utils.zip_utils import zip_project_dir
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "MyProj"
            (root / "MyProj.SemanticModel").mkdir(parents=True)
            (root / "MyProj.SemanticModel" / "model.tmdl").write_text("model")
            zb = zip_project_dir(str(root), "MyProj")
            self.assertGreater(len(zb), 0)

    def test_zip_project_dir_excludes_internal_only_files(self) -> None:
        """Internal build-metadata files must never end up in the
        user-facing ZIP — the user only needs Power BI files, not our
        agent's build.spec.json / decisions.log.json / feedback_history.json
        / learning_memory.json. Regression guard for the exclusion list."""
        import io
        import zipfile
        from utils.zip_utils import zip_project_dir, EXCLUDED_INTERNAL_FILES

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "MyProj"
            sm = root / "MyProj.SemanticModel"
            sm.mkdir(parents=True)
            (sm / "model.tmdl").write_text("model")
            # Power BI files that MUST be included
            (root / "MyProj.pbip").write_text("{}")
            (root / "README.md").write_text("readme")
            # Internal-only files that MUST be excluded
            for name in EXCLUDED_INTERNAL_FILES:
                (root / name).write_text('{"internal": true}')
                # also nest one under the semantic model dir to confirm
                # exclusion is applied at every depth, not just the root
                (sm / name).write_text('{"internal": true}')

            zb = zip_project_dir(str(root), "MyProj")
            self.assertGreater(len(zb), 0)
            with zipfile.ZipFile(io.BytesIO(zb)) as zf:
                names = zf.namelist()
            # Power BI files are present
            self.assertTrue(any(n.endswith("MyProj.pbip") for n in names),
                            f".pbip missing from zip: {names}")
            self.assertTrue(any(n.endswith("README.md") for n in names),
                            f"README.md missing from zip: {names}")
            self.assertTrue(any(n.endswith("model.tmdl") for n in names),
                            f"model.tmdl missing from zip: {names}")
            # Every internal-only file is absent at every depth
            for name in EXCLUDED_INTERNAL_FILES:
                offenders = [n for n in names if n.endswith(name)]
                self.assertFalse(offenders,
                                 f"internal file '{name}' leaked into zip: {offenders}")


class TestAuditLoggerStdioMode(unittest.TestCase):
    """Regression: a real MCP stdio server (mcp_server/server.py::main())
    sets mcp_stdio=True so log lines go to stderr, not stdout (stdout IS
    the JSON-RPC transport). But agents.orchestrator.OrchestratorAgent.
    __init__ calls AuditLogger.configure(log_file=..., level=...) fresh
    for every request WITHOUT passing mcp_stdio at all -- which used to
    reset logging back to stdout mid-run, corrupting the protocol every
    time a build actually went through the real MCP server. Only
    surfaced once generate_pbip was reachable over an actual round trip;
    the CLI/web entry points never depend on stdout being reserved, so
    nobody noticed. mcp_stdio is now sticky: a configure() call that
    doesn't mention it at all must preserve whatever was set before."""

    def tearDown(self):
        # Restore a normal (non-stdio) configuration so later tests in the
        # suite aren't affected by this test's stderr-mode side effect.
        AuditLogger.configure(mcp_stdio=False)

    def test_omitting_mcp_stdio_preserves_prior_stderr_mode(self):
        AuditLogger.configure(mcp_stdio=True)
        self.assertIs(AuditLogger._mcp_stdio, True)

        # Simulates OrchestratorAgent.__init__'s call: no mcp_stdio kwarg.
        AuditLogger.configure(log_file=None, level="INFO")
        self.assertIs(AuditLogger._mcp_stdio, True)

    def test_explicit_false_still_overrides(self):
        AuditLogger.configure(mcp_stdio=True)
        AuditLogger.configure(mcp_stdio=False)
        self.assertIs(AuditLogger._mcp_stdio, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
