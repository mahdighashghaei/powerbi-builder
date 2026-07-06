"""Phase 5.3 tests — Lineage Analysis."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from validators.lineage import (
    build_dependency_graph,
    detect_cycles,
    find_impacts,
    summarize_lineage,
    topological_order,
    _extract_refs,
)
from adk.tools.lineage_tools import (
    analyze_lineage,
    detect_circular_dependencies,
    find_column_impacts,
    find_measure_impacts,
    suggest_safe_rename_order,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model(measures=None, tables=None):
    measures = measures or []
    tables = tables or [{
        "table_name": "Sales",
        "columns": [
            {"name": "Sales", "dataType": "double"},
            {"name": "Profit", "dataType": "double"},
        ],
    }]
    return {
        "tables":        tables,
        "all_measures":  measures,
        "primary_table": tables[0]["table_name"],
    }


def _measure(name, expression, table="Sales"):
    return {"name": name, "expression": expression, "table": table}


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------

class TestRefExtraction(unittest.TestCase):

    def test_extract_measure_refs(self):
        mrefs, crefs = _extract_refs("[Total Sales] * 2")
        self.assertEqual(mrefs, {"Total Sales"})
        self.assertEqual(crefs, set())

    def test_extract_column_refs(self):
        mrefs, crefs = _extract_refs("SUM('Sales'[Profit])")
        self.assertEqual(mrefs, set())
        self.assertEqual(crefs, {("Sales", "Profit")})

    def test_extract_mixed(self):
        expr = "CALCULATE([Total Sales], 'Sales'[Country] = \"US\")"
        mrefs, crefs = _extract_refs(expr)
        self.assertEqual(mrefs, {"Total Sales"})
        self.assertEqual(crefs, {("Sales", "Country")})

    def test_string_literals_ignored(self):
        mrefs, crefs = _extract_refs('"This [Looks] like a ref but is not"')
        self.assertEqual(mrefs, set())
        self.assertEqual(crefs, set())

    def test_empty_expression(self):
        mrefs, crefs = _extract_refs("")
        self.assertEqual(mrefs, set())
        self.assertEqual(crefs, set())


# ---------------------------------------------------------------------------
# Graph building
# ---------------------------------------------------------------------------

class TestGraphBuild(unittest.TestCase):

    def test_simple_measure_to_column(self):
        m = _model(measures=[_measure("Total Sales", "SUM('Sales'[Sales])")])
        g = build_dependency_graph(m)
        self.assertIn("M::Total Sales", g["nodes"])
        self.assertIn("C::Sales::Sales", g["nodes"])
        self.assertIn(("M::Total Sales", "C::Sales::Sales"), g["edges"])

    def test_measure_referencing_measure(self):
        m = _model(measures=[
            _measure("Total Sales", "SUM('Sales'[Sales])"),
            _measure("Sales x2", "[Total Sales] * 2"),
        ])
        g = build_dependency_graph(m)
        self.assertIn(("M::Sales x2", "M::Total Sales"), g["edges"])
        self.assertEqual(
            g["deps"]["M::Sales x2"],
            ["M::Total Sales"],
        )

    def test_self_reference_excluded(self):
        m = _model(measures=[_measure("Loop", "[Loop] + 1")])
        g = build_dependency_graph(m)
        # Self refs filtered out at the edge level
        self.assertNotIn(("M::Loop", "M::Loop"), g["edges"])

    def test_unknown_measure_refs_dropped(self):
        m = _model(measures=[_measure("A", "[Does Not Exist]")])
        g = build_dependency_graph(m)
        # No measure node "Does Not Exist", so no edge to anywhere
        self.assertEqual(g["deps"].get("M::A", []), [])

    def test_unknown_column_refs_dropped(self):
        m = _model(measures=[_measure("A", "SUM('Other'[Foo])")])
        g = build_dependency_graph(m)
        # 'Other'[Foo] doesn't exist, no edge added
        self.assertEqual(g["deps"].get("M::A", []), [])


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

class TestImpacts(unittest.TestCase):

    def _chain_model(self):
        return _model(measures=[
            _measure("Sales", "SUM('Sales'[Sales])"),
            _measure("Sales 2", "[Sales]"),
            _measure("Sales 3", "[Sales 2]"),
            _measure("Independent", "1"),
        ])

    def test_direct_impacts(self):
        g = build_dependency_graph(self._chain_model())
        out = find_impacts(g, "Sales")
        self.assertTrue(out["found"])
        self.assertEqual(out["direct"], ["M::Sales 2"])

    def test_transitive_impacts(self):
        g = build_dependency_graph(self._chain_model())
        out = find_impacts(g, "Sales")
        self.assertIn("M::Sales 2", out["transitive"])
        self.assertIn("M::Sales 3", out["transitive"])
        self.assertEqual(out["count_transitive"], 2)

    def test_no_impacts(self):
        g = build_dependency_graph(self._chain_model())
        out = find_impacts(g, "Sales 3")
        self.assertEqual(out["count_direct"], 0)
        self.assertEqual(out["count_transitive"], 0)

    def test_unknown_target(self):
        g = build_dependency_graph(self._chain_model())
        out = find_impacts(g, "Nonexistent")
        self.assertFalse(out["found"])

    def test_column_impacts(self):
        g = build_dependency_graph(self._chain_model())
        out = find_impacts(g, "Sales.Sales", kind="column")
        self.assertTrue(out["found"])
        self.assertIn("M::Sales", out["direct"])


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycles(unittest.TestCase):

    def test_no_cycle(self):
        m = _model(measures=[
            _measure("A", "[B]"),
            _measure("B", "SUM('Sales'[Sales])"),
        ])
        g = build_dependency_graph(m)
        self.assertEqual(detect_cycles(g), [])

    def test_two_node_cycle(self):
        m = _model(measures=[
            _measure("A", "[B]"),
            _measure("B", "[A]"),
        ])
        g = build_dependency_graph(m)
        cycles = detect_cycles(g)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"M::A", "M::B"})

    def test_three_node_cycle(self):
        m = _model(measures=[
            _measure("A", "[B]"),
            _measure("B", "[C]"),
            _measure("C", "[A]"),
        ])
        g = build_dependency_graph(m)
        cycles = detect_cycles(g)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"M::A", "M::B", "M::C"})


# ---------------------------------------------------------------------------
# Topological order
# ---------------------------------------------------------------------------

class TestTopoOrder(unittest.TestCase):

    def test_topo_emits_leaves_first(self):
        m = _model(measures=[
            _measure("A", "[B]"),
            _measure("B", "[C]"),
            _measure("C", "SUM('Sales'[Sales])"),
        ])
        g = build_dependency_graph(m)
        order = topological_order(g)
        idx = {n: i for i, n in enumerate(order)}
        # C has no measure-deps but depends on column; column comes first
        self.assertLess(idx["C::Sales::Sales"], idx["M::C"])
        self.assertLess(idx["M::C"], idx["M::B"])
        self.assertLess(idx["M::B"], idx["M::A"])


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary(unittest.TestCase):

    def test_summary_counts(self):
        m = _model(measures=[
            _measure("A", "[B]"),
            _measure("B", "SUM('Sales'[Sales])"),
            _measure("Isolated", "1"),
        ])
        g = build_dependency_graph(m)
        s = summarize_lineage(g)
        self.assertEqual(s["measures"], 3)
        self.assertEqual(s["columns"], 2)
        self.assertEqual(s["cycle_count"], 0)
        self.assertIn("M::Isolated", s["isolated_measures"])


# ---------------------------------------------------------------------------
# ADK wrapper — end-to-end against a real SemanticModel folder
# ---------------------------------------------------------------------------

class TestAdkWrappers(unittest.TestCase):

    def _make_sm(self, root: Path) -> Path:
        sm = root / "L.SemanticModel"
        tables = sm / "definition" / "tables"
        tables.mkdir(parents=True, exist_ok=True)
        (tables / "Sales.tmdl").write_text(
            "table Sales\n"
            "\n"
            "\tcolumn 'Sales'\n"
            "\t\tdataType: double\n"
            "\t\tsummarizeBy: sum\n"
            "\t\tsourceColumn: Sales\n"
            "\n"
            "\tmeasure 'Total Sales' = SUM('Sales'[Sales])\n"
            "\tmeasure 'Sales x2' = [Total Sales] * 2\n",
            encoding="utf-8",
        )
        return sm

    def test_analyze_lineage(self):
        with tempfile.TemporaryDirectory() as td:
            sm = self._make_sm(Path(td))
            out = analyze_lineage(str(sm))
            self.assertNotIn("error", out)
            self.assertEqual(out["summary"]["measures"], 2)
            self.assertEqual(out["model_path"], str(sm))

    def test_find_measure_impacts(self):
        with tempfile.TemporaryDirectory() as td:
            sm = self._make_sm(Path(td))
            out = find_measure_impacts(str(sm), "Total Sales")
            self.assertTrue(out["found"])
            self.assertIn("M::Sales x2", out["direct"])

    def test_find_column_impacts(self):
        with tempfile.TemporaryDirectory() as td:
            sm = self._make_sm(Path(td))
            out = find_column_impacts(str(sm), "Sales.Sales")
            self.assertTrue(out["found"])
            self.assertIn("M::Total Sales", out["direct"])

    def test_detect_circular_dependencies_clean(self):
        with tempfile.TemporaryDirectory() as td:
            sm = self._make_sm(Path(td))
            out = detect_circular_dependencies(str(sm))
            self.assertEqual(out["cycle_count"], 0)

    def test_suggest_safe_rename_order(self):
        with tempfile.TemporaryDirectory() as td:
            sm = self._make_sm(Path(td))
            out = suggest_safe_rename_order(str(sm))
            self.assertGreater(out["count"], 0)
            self.assertIn("M::Total Sales", out["order"])

    def test_missing_path(self):
        out = analyze_lineage("/nonexistent/xyz")
        self.assertFalse(out["ok"])
        self.assertTrue(out["errors"])  # non-empty error list


if __name__ == "__main__":
    unittest.main()
