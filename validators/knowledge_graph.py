"""In-memory Knowledge Graph for Power BI projects.

Scope
=====
This module is a **standalone post-build analysis utility**. It is NOT invoked
by any pipeline agent (SchemaAgent, DAXAgent, ReportAgent, ValidatorAgent, etc.)
and has no effect on the build flow. Call it independently — e.g. from the CLI
or a notebook — after a successful build to query the finished project structure.

Entry point: ``adk.tools.kg_tools.query_knowledge_graph(pbip_dir, query=...)``

What this module provides
-------------------------
An in-memory typed graph with real graph queries:

Node types: ``Table``, ``Column``, ``Measure``, ``Relationship``, ``Visual``,
``Page``.
Edge types: ``contains`` (Table→Column, Page→Visual), ``references``
(Measure→Measure, Measure→Column), ``joins`` (Relationship→Table), ``filters``
(Visual→Column/Measure), ``binds`` (Visual→Measure/Column).

Built from a parsed semantic model (``utils.tmdl_parser``) plus the report
pages/visuals. Pure Python — no networkx dependency.

Queries:
  * ``neighbours(node_id)``        — direct neighbours of a node.
  * ``shortest_path(src, dst)``    — BFS shortest path between two nodes.
  * ``subgraph(node_ids)``         — induced subgraph on a node set.
  * ``impact(node_id)``            — transitive dependents (whole-project):
    renaming/removing a table affects its columns, the measures that reference
    them, the visuals that bind to those measures, etc.
  * ``nodes_by_type(type)``        — all nodes of a given type.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

# Reuse the DAX reference extractor from the lineage module for consistency.
from validators.lineage import _extract_refs  # noqa: E402


# ---------------------------------------------------------------------------
# Node / edge helpers
# ---------------------------------------------------------------------------

NODE_TABLE = "Table"
NODE_COLUMN = "Column"
NODE_MEASURE = "Measure"
NODE_RELATIONSHIP = "Relationship"
NODE_VISUAL = "Visual"
NODE_PAGE = "Page"

EDGE_CONTAINS = "contains"
EDGE_REFERENCES = "references"
EDGE_JOINS = "joins"
EDGE_FILTERS = "filters"
EDGE_BINDS = "binds"


def _nid(kind: str, *parts: str) -> str:
    return "::".join([kind, *parts])


class KnowledgeGraph:
    """An in-memory typed graph of a Power BI project's entities."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []  # (src, dst, edge_type)
        self._adj: dict[str, list[tuple[str, str]]] = defaultdict(list)  # forward
        self._radj: dict[str, list[tuple[str, str]]] = defaultdict(list)  # reverse

    # -- construction --------------------------------------------------

    def add_node(self, node_id: str, kind: str, **attrs: Any) -> None:
        if node_id not in self.nodes:
            self.nodes[node_id] = {"kind": kind, **attrs}
        else:
            self.nodes[node_id].update(attrs)

    def add_edge(self, src: str, dst: str, edge_type: str) -> None:
        if (src, dst, edge_type) not in self.edges:
            self.edges.append((src, dst, edge_type))
        self._adj[src].append((dst, edge_type))
        self._radj[dst].append((src, edge_type))

    # -- queries -------------------------------------------------------

    def neighbours(self, node_id: str) -> list[dict[str, Any]]:
        """Direct neighbours (out + in) of a node, with edge types."""
        out = [
            {"node": dst, "edge": etype, "direction": "out"}
            for dst, etype in self._adj.get(node_id, [])
        ]
        inn = [
            {"node": src, "edge": etype, "direction": "in"}
            for src, etype in self._radj.get(node_id, [])
        ]
        return out + inn

    def shortest_path(self, src: str, dst: str) -> list[str] | None:
        """BFS shortest path (undirected) between two nodes, or None."""
        if src not in self.nodes or dst not in self.nodes:
            return None
        if src == dst:
            return [src]
        visited = {src}
        queue: deque[tuple[str, list[str]]] = deque([(src, [src])])
        while queue:
            cur, path = queue.popleft()
            for nbr, _ in self._adj.get(cur, []):
                if nbr == dst:
                    return path + [nbr]
                if nbr not in visited:
                    visited.add(nbr)
                    queue.append((nbr, path + [nbr]))
            for nbr, _ in self._radj.get(cur, []):
                if nbr == dst:
                    return path + [nbr]
                if nbr not in visited:
                    visited.add(nbr)
                    queue.append((nbr, path + [nbr]))
        return None

    def subgraph(self, node_ids: set[str]) -> dict[str, Any]:
        """Induced subgraph on a node set: nodes + edges among them."""
        ids = {n for n in node_ids if n in self.nodes}
        sub_edges = [(s, d, t) for s, d, t in self.edges if s in ids and d in ids]
        return {
            "nodes": {n: self.nodes[n] for n in ids},
            "edges": sub_edges,
            "node_count": len(ids),
            "edge_count": len(sub_edges),
        }

    def impact(self, node_id: str) -> list[str]:
        """Transitive dependents of a node (whole-project impact).

        Traverses reverse edges (who points to this node) transitively, so e.g.
        removing a Table affects its Columns → the Measures that reference them
        → the Visuals that bind to those Measures.
        """
        if node_id not in self.nodes:
            return []
        visited: set[str] = set()
        queue: deque[str] = deque([node_id])
        while queue:
            cur = queue.popleft()
            for src, _ in self._radj.get(cur, []):
                if src not in visited:
                    visited.add(src)
                    queue.append(src)
        return sorted(visited)

    def nodes_by_type(self, kind: str) -> list[str]:
        return [nid for nid, attrs in self.nodes.items() if attrs.get("kind") == kind]

    def summary(self) -> dict[str, Any]:
        type_counts: dict[str, int] = defaultdict(int)
        for attrs in self.nodes.values():
            type_counts[attrs.get("kind", "?")] += 1
        edge_counts: dict[str, int] = defaultdict(int)
        for _, _, etype in self.edges:
            edge_counts[etype] += 1
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "node_types": dict(type_counts),
            "edge_types": dict(edge_counts),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": [{"src": s, "dst": d, "type": t} for s, d, t in self.edges],
            "summary": self.summary(),
        }


# ---------------------------------------------------------------------------
# Builder: from a parsed semantic model + report pages
# ---------------------------------------------------------------------------

def build_knowledge_graph(model: dict[str, Any], pages: list[dict[str, Any]] | None = None) -> KnowledgeGraph:
    """Build a knowledge graph from a semantic model + report pages.

    Args:
        model: a parsed semantic model (``utils.tmdl_parser.read_semantic_model``).
        pages: the report pages (``ctx.pages``), each with ``visuals``.

    Returns:
        A populated :class:`KnowledgeGraph`.
    """
    kg = KnowledgeGraph()
    tables = model.get("tables", []) or []
    for table in tables:
        tname = table.get("name", "")
        tnode = _nid(NODE_TABLE, tname)
        kg.add_node(tnode, NODE_TABLE, name=tname)
        for col in table.get("columns", []) or []:
            cname = col.get("name", col) if isinstance(col, dict) else str(col)
            cnode = _nid(NODE_COLUMN, tname, cname)
            kg.add_node(cnode, NODE_COLUMN, name=cname, table=tname)
            kg.add_edge(tnode, cnode, EDGE_CONTAINS)
        for m in table.get("measures", []) or []:
            mname = m.get("name", "")
            mexpr = m.get("expression", "")
            mnode = _nid(NODE_MEASURE, mname)
            kg.add_node(mnode, NODE_MEASURE, name=mname, table=tname, expression=mexpr)
            # Measure references (other measures + columns).
            mrefs, crefs = _extract_refs(mexpr)
            # First add the explicit quoted column refs.
            for (rtname, rcname) in crefs:
                kg.add_edge(mnode, _nid(NODE_COLUMN, rtname, rcname), EDGE_REFERENCES)
            # The extractor treats bare ``[Name]`` as a measure ref, but it may
            # actually be a column written without the ``'Table'`` prefix. We
            # disambiguate: if the ref is a known measure node, treat it as a
            # measure reference; otherwise look for a matching column across
            # tables and link to that. (We do this *after* all measures are
            # registered, so the measure lookup is reliable.)
            # (ambiguous [Name] refs resolved in the second pass below.)
        # Second pass: resolve the ambiguous [Name] refs now that all measure
        # nodes exist. We iterate the measures again to keep the lookup simple.
        for table in tables:
            tname = table.get("name", "")
            for m in table.get("measures", []) or []:
                mname = m.get("name", "")
                mexpr = m.get("expression", "")
                mnode = _nid(NODE_MEASURE, mname)
                mrefs, _crefs = _extract_refs(mexpr)
                for ref in mrefs:
                    candidate_measure = _nid(NODE_MEASURE, ref)
                    if candidate_measure in kg.nodes and candidate_measure != mnode:
                        kg.add_edge(mnode, candidate_measure, EDGE_REFERENCES)
                    else:
                        # Look for a column named ``ref`` in any table.
                        for nid, attrs in kg.nodes.items():
                            if (
                                attrs.get("kind") == NODE_COLUMN
                                and attrs.get("name") == ref
                            ):
                                kg.add_edge(mnode, nid, EDGE_REFERENCES)

    # Relationships (join edges between tables).
    for rel in model.get("relationships", []) or []:
        from_table = rel.get("from_table", rel.get("fromTable", ""))
        to_table = rel.get("to_table", rel.get("toTable", ""))
        rnode = _nid(NODE_RELATIONSHIP, from_table, to_table)
        kg.add_node(rnode, NODE_RELATIONSHIP, from_table=from_table, to_table=to_table)
        if from_table:
            kg.add_edge(rnode, _nid(NODE_TABLE, from_table), EDGE_JOINS)
        if to_table:
            kg.add_edge(rnode, _nid(NODE_TABLE, to_table), EDGE_JOINS)

    # Report pages + visuals (binding + filter edges).
    for page in pages or []:
        pname = page.get("displayName", "Page")
        pnode = _nid(NODE_PAGE, pname)
        kg.add_node(pnode, NODE_PAGE, name=pname)
        for i, visual in enumerate(page.get("visuals", []) or []):
            vid = visual.get("id", f"visual-{i}")
            vtype = visual.get("visualType", "card")
            vtitle = visual.get("title", "")
            vnode = _nid(NODE_VISUAL, pname, vid)
            kg.add_node(vnode, NODE_VISUAL, id=vid, visual_type=vtype, title=vtitle, page=pname)
            kg.add_edge(pnode, vnode, EDGE_CONTAINS)
            # Bindings from the query state (which measures/columns the visual uses).
            query = visual.get("query", {})
            state = query.get("queryState", {}) if isinstance(query, dict) else {}
            for role, fields in state.items():
                if not isinstance(fields, list):
                    continue
                for f in fields:
                    if not isinstance(f, dict):
                        continue
                    name = f.get("name") or f.get("column") or ""
                    if not name:
                        continue
                    # Try measure binding first, then column.
                    mnode = _nid(NODE_MEASURE, name)
                    if mnode in kg.nodes:
                        kg.add_edge(vnode, mnode, EDGE_BINDS)
                        continue
                    # Column binding: search across tables for a matching column node.
                    for nid, attrs in kg.nodes.items():
                        if attrs.get("kind") == NODE_COLUMN and attrs.get("name") == name:
                            kg.add_edge(vnode, nid, EDGE_FILTERS)
                            break
    return kg


__all__ = [
    "KnowledgeGraph",
    "build_knowledge_graph",
    "NODE_TABLE",
    "NODE_COLUMN",
    "NODE_MEASURE",
    "NODE_RELATIONSHIP",
    "NODE_VISUAL",
    "NODE_PAGE",
    "EDGE_CONTAINS",
    "EDGE_REFERENCES",
    "EDGE_JOINS",
    "EDGE_FILTERS",
    "EDGE_BINDS",
]
