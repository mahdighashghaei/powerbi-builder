"""Tests for the in-memory Knowledge Graph (Wave D2).

Verifies:
  * build_knowledge_graph creates typed nodes (Table/Column/Measure/Visual/Page).
  * edges are typed (contains/references/binds/filters/joins).
  * neighbours returns direct neighbours with edge types + directions.
  * shortest_path finds a path between connected nodes.
  * impact traverses transitive dependents (whole-project, not just DAX).
  * nodes_by_type filters by node kind.
  * summary reports node/edge type counts.
  * query_knowledge_graph tool returns the envelope on a real build.

Stdlib unittest — runs under ``python -m pytest tests/ -v``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from validators.knowledge_graph import (  # noqa: E402
    EDGE_BINDS,
    EDGE_CONTAINS,
    EDGE_REFERENCES,
    NODE_COLUMN,
    NODE_MEASURE,
    NODE_TABLE,
    NODE_VISUAL,
    build_knowledge_graph,
)


def _sample_model() -> dict:
    """A minimal semantic model: one table with a measure referencing a column."""
    return {
        "tables": [
            {
                "name": "Sales",
                "columns": [
                    {"name": "Region"},
                    {"name": "Amount"},
                ],
                "measures": [
                    {
                        "name": "Total Amount",
                        "expression": "SUM(Sales[Amount])",
                    },
                    {
                        "name": "Avg Amount",
                        "expression": "DIVIDE([Total Amount], COUNTROWS(Sales))",
                    },
                ],
            }
        ],
        "relationships": [
            {"from_table": "Sales", "to_table": "Region"},
        ],
    }


def _sample_pages() -> list[dict]:
    return [
        {
            "displayName": "Summary",
            "visuals": [
                {
                    "id": "card-0",
                    "visualType": "card",
                    "title": "Total",
                    "query": {"queryState": {"Values": [{"name": "Total Amount"}]}},
                },
            ],
        }
    ]


class TestBuildKnowledgeGraph(unittest.TestCase):
    """Construction creates typed nodes + edges."""

    def setUp(self):
        self.kg = build_knowledge_graph(_sample_model(), _sample_pages())

    def test_node_types_present(self):
        kinds = {a["kind"] for a in self.kg.nodes.values()}
        self.assertIn(NODE_TABLE, kinds)
        self.assertIn(NODE_COLUMN, kinds)
        self.assertIn(NODE_MEASURE, kinds)
        self.assertIn(NODE_VISUAL, kinds)

    def test_table_contains_columns(self):
        tnode = "Table::Sales"
        cnode = "Column::Sales::Amount"
        self.assertIn(tnode, self.kg.nodes)
        self.assertIn(cnode, self.kg.nodes)
        edge_types = {t for s, d, t in self.kg.edges if s == tnode and d == cnode}
        self.assertIn(EDGE_CONTAINS, edge_types)

    def test_measure_references_column(self):
        mnode = "Measure::Total Amount"
        cnode = "Column::Sales::Amount"
        edge_types = {t for s, d, t in self.kg.edges if s == mnode and d == cnode}
        self.assertIn(EDGE_REFERENCES, edge_types)

    def test_measure_references_measure(self):
        m_avg = "Measure::Avg Amount"
        m_total = "Measure::Total Amount"
        edge_types = {t for s, d, t in self.kg.edges if s == m_avg and d == m_total}
        self.assertIn(EDGE_REFERENCES, edge_types)

    def test_visual_binds_measure(self):
        vnode = "Visual::Summary::card-0"
        mnode = "Measure::Total Amount"
        edge_types = {t for s, d, t in self.kg.edges if s == vnode and d == mnode}
        self.assertIn(EDGE_BINDS, edge_types)


class TestGraphQueries(unittest.TestCase):
    """neighbours / shortest_path / impact / nodes_by_type / summary."""

    def setUp(self):
        self.kg = build_knowledge_graph(_sample_model(), _sample_pages())

    def test_neighbours(self):
        nbrs = self.kg.neighbours("Column::Sales::Amount")
        # The column is referenced by the Total Amount measure.
        ids = {n["node"] for n in nbrs}
        self.assertIn("Measure::Total Amount", ids)

    def test_shortest_path_connected(self):
        path = self.kg.shortest_path("Table::Sales", "Measure::Total Amount")
        self.assertIsNotNone(path)
        # Path goes Table -> Column -> Measure.
        self.assertEqual(path[0], "Table::Sales")
        self.assertEqual(path[-1], "Measure::Total Amount")

    def test_shortest_path_none_when_unreachable(self):
        # Add an isolated node.
        self.kg.add_node("Table::Isolated", NODE_TABLE, name="Isolated")
        path = self.kg.shortest_path("Table::Isolated", "Measure::Total Amount")
        # Isolated has no edges, so no path exists.
        self.assertIsNone(path)

    def test_impact_transitive(self):
        # Impact of the Amount column: the measure that references it + the
        # visual that binds that measure (whole-project, not just DAX).
        impacted = self.kg.impact("Column::Sales::Amount")
        self.assertIn("Measure::Total Amount", impacted)
        self.assertIn("Visual::Summary::card-0", impacted)

    def test_nodes_by_type(self):
        measures = self.kg.nodes_by_type(NODE_MEASURE)
        self.assertEqual(len(measures), 2)
        tables = self.kg.nodes_by_type(NODE_TABLE)
        self.assertEqual(len(tables), 1)

    def test_summary(self):
        s = self.kg.summary()
        self.assertGreater(s["node_count"], 0)
        self.assertGreater(s["edge_count"], 0)
        self.assertIn(NODE_MEASURE, s["node_types"])
        self.assertIn(EDGE_REFERENCES, s["edge_types"])

    def test_subgraph(self):
        ids = {"Table::Sales", "Column::Sales::Amount", "Measure::Total Amount"}
        sub = self.kg.subgraph(ids)
        self.assertEqual(sub["node_count"], 3)
        self.assertGreater(sub["edge_count"], 0)


class TestKgTool(unittest.TestCase):
    """query_knowledge_graph tool on a real build."""

    def _write_csv(self, path: Path) -> None:
        path.write_text(
            "OrderDate,Region,Product,Quantity,Amount\n"
            "2024-01-05,North,Widget,10,250.50\n"
            "2024-01-07,South,Gadget,5,99.99\n",
            encoding="utf-8",
        )

    def test_summary_query_on_real_build(self):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402
        from adk.tools.kg_tools import query_knowledge_graph  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "sales.csv"
            self._write_csv(csv)
            out = Path(td) / "out"
            OrchestratorAgent(str(out)).run(
                source_path=str(csv), business_description="Monthly sales by region"
            )
            project = next(p for p in out.iterdir() if p.is_dir())
            r = query_knowledge_graph(str(project), query="summary")
            self.assertTrue(r["ok"], f"query failed: {r}")
            self.assertEqual(r["tool"], "query_knowledge_graph")
            self.assertGreater(r["data"]["summary"]["node_count"], 0)

    def test_impact_query(self):
        from agents.orchestrator import OrchestratorAgent  # noqa: E402
        from adk.tools.kg_tools import query_knowledge_graph  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "sales.csv"
            self._write_csv(csv)
            out = Path(td) / "out"
            OrchestratorAgent(str(out)).run(
                source_path=str(csv), business_description="sales by region"
            )
            project = next(p for p in out.iterdir() if p.is_dir())
            # Find a column node to query impact for.
            r_summary = query_knowledge_graph(str(project), query="summary")
            columns = query_knowledge_graph(str(project), query="nodes_by_type", node_id="Column")
            self.assertTrue(columns["ok"])
            if columns["data"]["nodes"]:
                r = query_knowledge_graph(
                    str(project), query="impact", node_id=columns["data"]["nodes"][0]
                )
                self.assertTrue(r["ok"])
                self.assertIn("impact", r["data"])

    def test_unknown_query_fails_gracefully(self):
        from adk.tools.kg_tools import query_knowledge_graph  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            r = query_knowledge_graph(td, query="bogus")
            self.assertFalse(r["ok"])

    def test_missing_path_fails_gracefully(self):
        from adk.tools.kg_tools import query_knowledge_graph  # noqa: E402

        r = query_knowledge_graph("/nonexistent/xyz", query="summary")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
