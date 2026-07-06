"""Phase 0 — real end-to-end semantic tests (no mocks).

Unlike ``test_phase7_highlevel.py`` (which MagicMock's the OrchestratorAgent),
these tests run the *actual* pipeline (``generate_pbip`` / ``edit_pbip`` →
OrchestratorAgent → SchemaAgent → DAXAgent → ReportAgent → Validator) against
real CSV fixtures and assert *semantic* correctness, not just ``ok=True``:

  * every DAX measure references only columns that actually exist in the schema
    (no "ghost" column references that would break Desktop);
  * every visual binding (``query`` in visual.json) points to a real measure or
    column (delegated to ``check_visual_references``);
  * detected relationships are sensible — the expected FK is found AND a
    deliberately-similar-but-unrelated name does NOT produce a false positive;
  * a baseline snapshot of {schema columns, measure names, page ids, visual
    types} is written to ``tests/baselines/`` so later phases can diff against
    it and confirm any behaviour change was *intentional*.

These tests run fully offline (``GOOGLE_API_KEY`` is forced empty so the
deterministic heuristic path is exercised) — they never call a live LLM, which
keeps them fast + deterministic + free to run in CI.

Run with::

    python -m pytest tests/test_phase0_e2e_semantic.py -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

# make project root importable
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURES = _ROOT / "tests" / "fixtures"
BASELINES = _ROOT / "tests" / "baselines"


def _force_offline() -> None:
    """Ensure the deterministic (non-LLM) path runs: clear any API key.

    Refine_relationships and any optional LLM enhancement must no-op so the
    baseline is reproducible across machines / CI runs.
    """
    os.environ["GOOGLE_API_KEY"] = ""


# ---------------------------------------------------------------------------
# semantic assertion helpers
# ---------------------------------------------------------------------------


def _read_semantic_model(pbip_root: Path) -> dict:
    """Read the semantic model (tables, columns, measure names) from TMDL."""
    from utils.tmdl_parser import read_semantic_model

    sm_dir = next(pbip_root.glob("*.SemanticModel"), None)
    assert sm_dir is not None, f"No *.SemanticModel under {pbip_root}"
    return read_semantic_model(sm_dir)


def _extract_column_refs(expression: str) -> list[tuple[str, str]]:
    """Pull ``Table[Column]`` and bare ``[Column]`` refs out of a DAX expression.

    Returns a list of (table_or_None, column) tuples. Single-quoted table names
    and bracketed column names are both handled.
    """
    refs: list[tuple[str, str]] = []
    # 'Table'[Column]  or  Table[Column]
    for m in re.finditer(r"'?([A-Za-z_][\w ]*)'?\[([A-Za-z_][\w .]*)\]", expression):
        table, col = m.group(1), m.group(2)
        # a bare [Column] with no table produces an empty group-1 match only when
        # the regex aligns on the opening bracket; filter trivial empties.
        refs.append((table, col))
    return refs


def _assert_measures_reference_real_columns(
    case: unittest.TestCase, pbip_root: Path
) -> list[str]:
    """Every column referenced by a DAX measure must exist in the schema.

    Returns the list of measure names for the caller's use. A failed assertion
    means the DAX agent produced a measure pointing at a column that does not
    exist in the table — Desktop would show an error on that measure.
    """
    from utils.tmdl_parser import read_semantic_model

    model = _read_semantic_model(pbip_root)
    # build a lookup: table_name -> set(column_name)
    table_cols: dict[str, set[str]] = {
        t["table_name"]: {c["name"] for c in t.get("columns", [])}
        for t in model.get("tables", [])
    }
    all_tables = set(table_cols)

    # read each table TMDL to get measure *expressions* (read_semantic_model
    # gives names only). Re-parse the files for the expression text.
    sm_dir = next(pbip_root.glob("*.SemanticModel"))
    tables_dir = sm_dir / "definition" / "tables"
    measure_names: list[str] = []
    measure_re = re.compile(r"measure\s+'([^']+)'\s*=\s*(.+?)(?=\n\s*measure\s+'|\Z)",
                            re.DOTALL | re.IGNORECASE)
    for tmdl in sorted(tables_dir.glob("*.tmdl")):
        txt = tmdl.read_text(encoding="utf-8")
        # determine this table's name from the first line
        first = txt.splitlines()[0].strip() if txt.splitlines() else ""
        tname = first.replace("table ", "", 1) if first.lower().startswith("table ") else tmdl.stem
        for m in measure_re.finditer(txt):
            mname, expr = m.group(1), m.group(2)
            measure_names.append(mname)
            for tbl, col in _extract_column_refs(expr):
                # resolve the table: explicit, or assume this measure's table
                target = tbl if tbl and tbl in table_cols else tname
                if target not in table_cols:
                    case.fail(
                        f"Measure '{mname}' references unknown table '{tbl}' "
                        f"(in {tmdl.name}). Known tables: {sorted(all_tables)}"
                    )
                if col not in table_cols[target]:
                    case.fail(
                        f"Measure '{mname}' references column '{col}' which is "
                        f"not in table '{target}' (in {tmdl.name}). "
                        f"Columns: {sorted(table_cols[target])}"
                    )
    case.assertTrue(measure_names, "No measures were generated — pipeline produced nothing.")
    return measure_names


def _assert_no_visual_ghost_refs(case: unittest.TestCase, pbip_root: Path) -> dict:
    """Every visual binding must point to a real measure/column."""
    from adk.tools.review_tools import check_visual_references

    result = check_visual_references(str(pbip_root))
    case.assertTrue(result["ok"], f"check_visual_references failed: {result}")
    ghosts = result.get("ghost_refs", [])
    case.assertEqual(
        len(ghosts), 0,
        f"Found {len(ghosts)} ghost reference(s) in visuals: {ghosts}",
    )
    return result


def _write_baseline(name: str, snapshot: dict) -> Path:
    """Persist a baseline snapshot JSON under tests/baselines/."""
    BASELINES.mkdir(parents=True, exist_ok=True)
    path = BASELINES / f"{name}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _snapshot(pbip_root: Path) -> dict:
    """Build a compact, comparable snapshot of the generated project."""
    model = _read_semantic_model(pbip_root)
    from adk.tools.review_tools import list_pages

    pages = list_pages(str(pbip_root))
    return {
        "tables": [
            {"name": t["table_name"], "columns": [c["name"] for c in t.get("columns", [])]}
            for t in model.get("tables", [])
        ],
        "measures": sorted(model.get("measure_names", [])),
        "pages": [
            {"id": p["page_id"], "display_name": p["display_name"],
             "visual_count": p["visual_count"]}
            for p in pages.get("pages", [])
        ],
        "total_visuals": pages.get("total_visuals", 0),
    }


# ---------------------------------------------------------------------------
# Test 1: simple single-table dataset
# ---------------------------------------------------------------------------


class TestE2ESimpleTable(unittest.TestCase):
    """generate_pbip on a clean single-table CSV: full real pipeline."""

    @classmethod
    def setUpClass(cls) -> None:
        _force_offline()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "out"
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.src = FIXTURES / "simple_single_table.csv"
        from mcp_server import highlevel as hl
        cls.result = hl.generate_pbip(
            str(cls.src),
            "Monthly sales dashboard with revenue and order trends",
            output_root=str(cls.out),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_pipeline_succeeded(self):
        self.assertTrue(self.result["ok"], f"generate_pbip failed: {self.result}")
        val = self.result["data"].get("validation") or {}
        # structural validation must pass (no missing files / bad JSON)
        self.assertTrue(val.get("ok"), f"validation failed: {val}")

    def test_measures_reference_real_columns(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        _assert_measures_reference_real_columns(self, pbip_root)

    def test_no_visual_ghost_refs(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        _assert_no_visual_ghost_refs(self, pbip_root)

    def test_baseline_snapshot_written(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        snap = _snapshot(pbip_root)
        path = _write_baseline("simple_single_table", snap)
        self.assertTrue(path.is_file())
        # the snapshot must list at least one table, some measures, one page
        self.assertGreaterEqual(len(snap["tables"]), 1)
        self.assertGreaterEqual(len(snap["measures"]), 1)
        self.assertGreaterEqual(len(snap["pages"]), 1)


# ---------------------------------------------------------------------------
# Test 2: ambiguous column names (c1, c2, v1, ...)
# ---------------------------------------------------------------------------


class TestE2EAmbiguousColumns(unittest.TestCase):
    """generate_pbip on a CSV with cryptic column names.

    The keyword-based classifier (``_AMOUNT_HINTS`` etc.) cannot confidently
    bucket ``v1``/``c1``, so this stresses the measure/visual builders: they
    must still only reference columns that *actually exist* (no ghost refs),
    even when the bucketing is weak.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _force_offline()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "out"
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.src = FIXTURES / "ambiguous_columns.csv"
        from mcp_server import highlevel as hl
        cls.result = hl.generate_pbip(
            str(cls.src),
            "Operational dashboard from a poorly-named export file",
            output_root=str(cls.out),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_pipeline_succeeded(self):
        self.assertTrue(self.result["ok"], f"generate_pbip failed: {self.result}")
        val = self.result["data"].get("validation") or {}
        self.assertTrue(val.get("ok"), f"validation failed: {val}")

    def test_measures_reference_real_columns(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        names = _assert_measures_reference_real_columns(self, pbip_root)
        # even with cryptic names, the pipeline should produce >=1 measure
        self.assertGreaterEqual(len(names), 1)

    def test_no_visual_ghost_refs(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        _assert_no_visual_ghost_refs(self, pbip_root)

    def test_baseline_snapshot_written(self):
        pbip_root = Path(self.result["data"]["pbip_root"])
        snap = _snapshot(pbip_root)
        path = _write_baseline("ambiguous_columns", snap)
        self.assertTrue(path.is_file())


# ---------------------------------------------------------------------------
# Test 3: multi-table relationship detection (no false positives)
# ---------------------------------------------------------------------------


class TestE2EMultiTableRelationships(unittest.TestCase):
    """Relationship detection must find the real FK and avoid false positives.

    Builds a real two-table PBIP (Orders + Customers) by generating the primary
    table via the real pipeline, then appending a second table TMDL with the
    toolbox, then running ``detect_relationships`` on the parsed ``all_tables``.
    This exercises the same code path RelationshipAgent uses in edit_pbip mode.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _force_offline()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "out"
        cls.out.mkdir(parents=True, exist_ok=True)
        cls.orders_csv = FIXTURES / "multi_table_orders.csv"
        from mcp_server import highlevel as hl
        # 1) generate the Orders table PBIP via the real pipeline
        cls.result = hl.generate_pbip(
            str(cls.orders_csv),
            "Orders report linked to customers",
            output_root=str(cls.out),
        )
        cls.pbip_root = Path(cls.result["data"]["pbip_root"])
        # 2) append a second "Customers" table TMDL to the same model so
        #    relationship detection has two tables to work with
        from mcp_server.server import PbipToolbox
        project_name = cls.result["data"]["project_name"]
        toolbox = PbipToolbox(cls.pbip_root)
        sm_def = f"{project_name}.SemanticModel/definition"
        customers_def = {
            "name": "Customers",
            "columns": [
                {"name": "Id", "dataType": "string"},
                {"name": "CustomerName", "dataType": "string"},
                {"name": "CustomerTier", "dataType": "string"},
            ],
            "measures": [],
            "source_path": str(FIXTURES / "multi_table_customers.csv"),
            "source_type": "csv",
        }
        w = toolbox.write_tmdl_table(sm_def, customers_def)
        assert w.ok, f"failed to write Customers table: {w.message}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_pipeline_succeeded(self):
        self.assertTrue(self.result["ok"], f"generate_pbip failed: {self.result}")

    def test_relationship_finds_real_fk(self):
        from agents.relationship_agent import detect_relationships

        model = _read_semantic_model(self.pbip_root)
        tables = model.get("tables", [])
        self.assertGreaterEqual(len(tables), 2, "Expected >=2 tables for relationship detection")
        rels = detect_relationships(tables)
        from_cols = [(r["from_table"], r["from_column"], r["to_table"], r["to_column"]) for r in rels]
        # The fact table is the one containing the CustomerId FK column (its
        # name may carry a "_clean" suffix from the DataCleaner, so resolve it
        # dynamically rather than hard-coding the source-file stem).
        fact_table = next(
            (t["table_name"] for t in tables if "CustomerId" in {c["name"] for c in t.get("columns", [])}),
            None,
        )
        self.assertIsNotNone(fact_table, f"No table with CustomerId in {tables}")
        # <fact>.CustomerId -> Customers.Id is the real FK
        self.assertIn(
            (fact_table, "CustomerId", "Customers", "Id"),
            from_cols,
            f"Expected {fact_table}.CustomerId -> Customers.Id in {from_cols}",
        )

    def test_no_false_positive_on_random_similar_names(self):
        """A column whose normalised name coincidentally matches a table stem
        must not be matched when there is no real semantic FK.

        ``RegionId`` normalises to 'region' and there is no ``Region`` table
        here, so it must NOT produce a relationship to Customers.
        """
        from agents.relationship_agent import detect_relationships

        model = _read_semantic_model(self.pbip_root)
        tables = model.get("tables", [])
        rels = detect_relationships(tables)
        for r in rels:
            # RegionId must never link to Customers (that would be a false positive)
            if r["from_column"] == "RegionId":
                self.assertNotEqual(
                    r["to_table"], "Customers",
                    f"False positive: RegionId linked to Customers: {r}",
                )

    def test_measures_reference_real_columns(self):
        # the measures generated for the Orders table must still be valid
        _assert_measures_reference_real_columns(self, self.pbip_root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
