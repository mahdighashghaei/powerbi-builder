"""Lineage / dependency analysis for PBIP semantic models.

Builds a directed graph: measure → measure (reference) and measure → column.

Public API:
    build_dependency_graph(model) -> dict
    find_impacts(graph, target)   -> dict      # who depends on a node?
    detect_cycles(graph)          -> list[list[str]]
    topological_order(graph)      -> list[str] # safe rename order

Pure-python — no networkx dependency.

The model dict matches ``utils.tmdl_parser.read_semantic_model``.

Node IDs:
    measure  ->  "M::<measure_name>"
    column   ->  "C::<table>::<column>"
"""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# DAX reference extraction
# ---------------------------------------------------------------------------

# [MeasureName] anywhere outside a string literal
_MEASURE_REF_RE = re.compile(r"(?<!')\[([^\]]+)\]")

# 'Table'[Column]
_COLUMN_REF_RE = re.compile(r"'([^']+)'\s*\[([^\]]+)\]")

# Strip string literals so refs inside text don't get picked up
_STR_LITERAL_RE = re.compile(r'"[^"]*"')


def _extract_refs(expr: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Extract (measure_refs, column_refs) from a DAX expression.

    Returns:
        ({measure_name, ...}, {(table, column), ...})
    """
    if not expr:
        return set(), set()
    cleaned = _STR_LITERAL_RE.sub('""', expr)

    column_refs = set(_COLUMN_REF_RE.findall(cleaned))
    # Drop column refs from text before scanning measure refs
    # so [Column] inside 'Table'[Column] isn't double-counted.
    no_cols = _COLUMN_REF_RE.sub("", cleaned)
    measure_refs = set(_MEASURE_REF_RE.findall(no_cols))

    return measure_refs, column_refs


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _node_measure(name: str) -> str:
    return f"M::{name}"


def _node_column(table: str, column: str) -> str:
    return f"C::{table}::{column}"


def build_dependency_graph(model: dict[str, Any]) -> dict[str, Any]:
    """Build a dependency graph from the parsed model.

    Returns:
        {
            "nodes": {node_id: {"kind": "measure"|"column", "name": ..., "table": ...}},
            "edges": [(src, dst), ...],      # src depends on dst
            "deps":  {node: {dependencies}}, # forward edges
            "rdeps": {node: {dependents}},   # reverse edges
        }
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[tuple[str, str]] = []
    deps: dict[str, set[str]] = {}
    rdeps: dict[str, set[str]] = {}

    # First pass: register all measure + column nodes
    measure_names: set[str] = set()
    for m in model.get("all_measures", []):
        name = m.get("name", "")
        if not name:
            continue
        nid = _node_measure(name)
        nodes[nid] = {
            "kind": "measure",
            "name": name,
            "table": m.get("table", ""),
            "expression": m.get("expression", ""),
        }
        measure_names.add(name)

    for t in model.get("tables", []):
        tname = t.get("table_name", "")
        for col in t.get("columns", []):
            cn = col.get("name", "")
            if not cn:
                continue
            nid = _node_column(tname, cn)
            nodes[nid] = {
                "kind": "column",
                "name": cn,
                "table": tname,
            }

    # Second pass: extract dependencies from each measure's DAX
    for m in model.get("all_measures", []):
        name = m.get("name", "")
        expr = m.get("expression", "")
        if not name:
            continue
        src = _node_measure(name)
        mrefs, crefs = _extract_refs(expr)

        for ref_name in mrefs:
            # only count refs that point to an actual measure in the model
            if ref_name in measure_names and ref_name != name:
                dst = _node_measure(ref_name)
                edges.append((src, dst))
                deps.setdefault(src, set()).add(dst)
                rdeps.setdefault(dst, set()).add(src)

        for (tbl, col) in crefs:
            dst = _node_column(tbl, col)
            # only emit if the column exists; otherwise we'd be lying
            if dst in nodes:
                edges.append((src, dst))
                deps.setdefault(src, set()).add(dst)
                rdeps.setdefault(dst, set()).add(src)

    return {
        "nodes": nodes,
        "edges": edges,
        "deps": {k: sorted(v) for k, v in deps.items()},
        "rdeps": {k: sorted(v) for k, v in rdeps.items()},
    }


# ---------------------------------------------------------------------------
# Impact analysis (BFS over reverse edges)
# ---------------------------------------------------------------------------

def find_impacts(
    graph: dict[str, Any],
    target: str,
    kind: str = "measure",
) -> dict[str, Any]:
    """Find everything that depends (directly or transitively) on a target.

    Args:
        graph:  output of build_dependency_graph
        target: name of the measure or column to query
        kind:   "measure" | "column" — when kind="column", target should be
                "Table.Column" or just "Column" (first match wins).

    Returns:
        {
            "target":   <resolved node_id>,
            "direct":   [node_id, ...],   # things that directly reference target
            "transitive": [node_id, ...], # all transitive dependents
            "count_direct": int,
            "count_transitive": int,
        }
    """
    nodes = graph["nodes"]
    rdeps_map: dict[str, list[str]] = graph["rdeps"]

    if kind == "measure":
        nid = _node_measure(target)
    elif kind == "column":
        if "." in target:
            tbl, col = target.split(".", 1)
            nid = _node_column(tbl, col)
        else:
            # find first column with that name
            nid = next(
                (n for n, meta in nodes.items()
                 if meta["kind"] == "column" and meta["name"] == target),
                "",
            )
    else:
        nid = target  # raw node id

    if nid not in nodes:
        return {
            "target": target,
            "resolved": "",
            "found": False,
            "direct": [],
            "transitive": [],
            "count_direct": 0,
            "count_transitive": 0,
        }

    direct = list(rdeps_map.get(nid, []))
    visited: set[str] = set()
    queue = list(direct)
    while queue:
        cur = queue.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for parent in rdeps_map.get(cur, []):
            if parent not in visited:
                queue.append(parent)

    return {
        "target": target,
        "resolved": nid,
        "found": True,
        "direct": sorted(direct),
        "transitive": sorted(visited),
        "count_direct": len(direct),
        "count_transitive": len(visited),
    }


# ---------------------------------------------------------------------------
# Cycle detection (Tarjan-style DFS)
# ---------------------------------------------------------------------------

def detect_cycles(graph: dict[str, Any]) -> list[list[str]]:
    """Return strongly connected components of size > 1 (i.e., cycles).

    A measure that references itself directly is also reported.
    """
    deps_map: dict[str, list[str]] = graph["deps"]
    nodes = graph["nodes"]

    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in deps_map.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                result.append(sorted(scc))
            elif v in deps_map.get(v, []):
                # self-loop
                result.append([v])

    for n in nodes:
        if n not in index:
            strongconnect(n)

    return result


# ---------------------------------------------------------------------------
# Topological sort (safe rename order)
# ---------------------------------------------------------------------------

def topological_order(graph: dict[str, Any]) -> list[str]:
    """Kahn's algorithm — return nodes in dependency order (deps first).

    If a cycle exists, the cyclic nodes are returned at the end in arbitrary
    order so callers can still iterate.
    """
    deps_map: dict[str, list[str]] = graph["deps"]
    rdeps_map: dict[str, list[str]] = graph["rdeps"]
    nodes = list(graph["nodes"].keys())

    # in-degree based on rdeps (number of things pointing TO us means we are
    # depended-on by them; topological order = dependencies emitted first).
    # We want: if A -> B (A depends on B), emit B before A. So we sort by
    # "leaves" (nodes nobody depends on) -> root.
    indegree: dict[str, int] = {n: 0 for n in nodes}
    for src in deps_map:
        for dst in deps_map[src]:
            indegree[src] = indegree[src] + 0  # ensure key
    for src, dsts in deps_map.items():
        # src depends on each dst — dst should come first.
        # We compute classic Kahn on the *reverse* graph: edges dst->src.
        for dst in dsts:
            indegree[src] = indegree[src] + 1

    queue = sorted([n for n in nodes if indegree[n] == 0])
    order: list[str] = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for parent in rdeps_map.get(n, []):
            indegree[parent] -= 1
            if indegree[parent] == 0:
                queue.append(parent)
        queue.sort()

    # Append any nodes left (cycles)
    leftover = [n for n in nodes if n not in order]
    return order + sorted(leftover)


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def summarize_lineage(graph: dict[str, Any]) -> dict[str, Any]:
    """Return high-level stats about a dependency graph."""
    nodes = graph["nodes"]
    measure_count = sum(1 for n in nodes.values() if n["kind"] == "measure")
    column_count = sum(1 for n in nodes.values() if n["kind"] == "column")
    cycles = detect_cycles(graph)
    isolated = [
        n for n, meta in nodes.items()
        if meta["kind"] == "measure"
        and not graph["deps"].get(n)
        and not graph["rdeps"].get(n)
    ]
    return {
        "total_nodes": len(nodes),
        "measures": measure_count,
        "columns": column_count,
        "edges": len(graph["edges"]),
        "cycles": cycles,
        "cycle_count": len(cycles),
        "isolated_measures": sorted(isolated),
    }
