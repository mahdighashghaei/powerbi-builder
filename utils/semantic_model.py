"""utils/semantic_model.py — Semantic Truth Layer.

Discovers what numeric columns *actually mean* by empirically testing
arithmetic relationships between them (row-wise, on real data) instead of
guessing from column names. On ``SampleData.csv`` this is how the system
learns — from the data itself, once — that ``Sales = Gross Sales - Discounts``
and ``Profit = Sales - COGS``, i.e. that ``Sales`` is the *net* revenue figure
and ``Gross Sales`` is a pre-discount intermediate, something no keyword
heuristic can determine reliably.

The discovered model is persisted (via ``utils.learning_memory.LearningMemory``,
keyed by a schema fingerprint) so the *same* relationships are never
rediscovered on a future run against the same schema shape — this is the
"semantic truth", found once and remembered.

Fail-safe contract: every public function is exception-safe and returns a
neutral empty model on any internal error (missing pandas, bad data, etc.),
matching the convention used throughout ``utils/``.
"""
from __future__ import annotations

import hashlib
from itertools import combinations
from typing import Any

# Row-wise relative tolerance for "A - B ≈ C" / "A × B ≈ C" checks. Generous
# enough to tolerate real-world rounding (invoice-level cent rounding, etc.)
# while still rejecting coincidental near-matches.
_REL_TOLERANCE = 0.01
_MIN_MATCH_FRACTION = 0.98  # fraction of rows that must satisfy the formula
_MIN_ROWS_FOR_DISCOVERY = 5


# ---------------------------------------------------------------------------
# Schema fingerprint (cache key)
# ---------------------------------------------------------------------------


def compute_schema_fingerprint(columns: list[dict[str, Any]]) -> str:
    """Stable hash of a schema's (name, dataType) shape.

    Two schemas with the same columns (any order) produce the same
    fingerprint, so a semantic model discovered for one is reused for the
    other — this is the cache key that prevents rediscovery.
    """
    try:
        pairs = sorted(
            (str(c.get("name", "")), str(c.get("dataType", "")))
            for c in (columns or [])
        )
        blob = "|".join(f"{n}:{t}" for n, t in pairs)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Relationship discovery
# ---------------------------------------------------------------------------


def _row_match_fraction(series_a, series_b, series_c, op: str) -> float:
    """Fraction of rows where ``op(a, b) ≈ c`` within relative tolerance."""
    import numpy as np  # pandas dependency already pulls in numpy

    a, b, c = series_a.to_numpy(dtype=float), series_b.to_numpy(dtype=float), series_c.to_numpy(dtype=float)
    predicted = (a - b) if op == "sub" else (a * b)
    denom = np.maximum(np.abs(c), 1e-9)
    close = np.abs(predicted - c) / denom <= _REL_TOLERANCE
    return float(close.mean()) if len(close) else 0.0


def discover_semantic_relationships(
    df: Any | None,
    amount_columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Empirically discover subtraction/product relationships among numeric columns.

    Args:
        df:              A pandas DataFrame (row-level data). ``None`` (or
                          too few rows) yields the neutral empty model.
        amount_columns:  Column dicts (``{"name": ...}``) to test — typically
                          the "amount" bucket from ``_classify_columns`` plus
                          any quantity columns the caller wants considered.

    Returns:
        ``{"entities": {col: role}, "relationships": [...], "canonical_metrics": {...}}``
        where ``role`` is one of ``"net"``, ``"gross"``, ``"deduction"``,
        ``"computed_total"``, ``"component"``, or ``"unclassified"``, and
        ``canonical_metrics`` binds abstract BI roles (``net_revenue``,
        ``gross_revenue``, ``deduction``, ``cost``, ``profit``) to the
        actual column names discovered for *this* schema.
    """
    empty: dict[str, Any] = {"entities": {}, "relationships": [], "canonical_metrics": {}}
    try:
        if df is None or len(df) < _MIN_ROWS_FOR_DISCOVERY:
            return empty
        names = [c.get("name") for c in (amount_columns or []) if c.get("name") in getattr(df, "columns", [])]
        names = [n for n in names if str(df[n].dtype).startswith(("float", "int"))]
        if len(names) < 2:
            return empty

        relationships: list[dict[str, Any]] = []
        entities: dict[str, str] = {n: "unclassified" for n in names}

        # Whole/parts decomposition: does part_x + part_y == whole?
        # Phrased as addition (not "whole - part_x = part_y") so each true
        # relationship is discovered exactly ONCE — "A - B = C" and
        # "A - C = B" are the same underlying fact and would otherwise be
        # recorded twice with the part/net roles inverted between them,
        # which is a real trap: which of the two parts is "the meaningful
        # continuation" (net revenue) vs "the deducted portion" cannot be
        # told apart from the arithmetic alone. That ambiguity is resolved
        # structurally below (a "whole" column split into two "part"
        # columns; the part that goes on to be split further itself is the
        # continuing chain, e.g. Sales; the other part is the deduction).
        for whole in names:
            for part_x, part_y in combinations([n for n in names if n != whole], 2):
                try:
                    frac = _row_match_fraction(df[whole], df[part_y], df[part_x], "sub")
                except Exception:  # noqa: BLE001
                    continue
                # whole - part_y ≈ part_x  <=>  part_x + part_y ≈ whole
                if frac >= _MIN_MATCH_FRACTION:
                    relationships.append({
                        "type": "sum_decomposition",
                        "whole": whole, "parts": sorted([part_x, part_y]),
                        "match_fraction": round(frac, 4),
                    })
                    if entities.get(whole) == "unclassified":
                        entities[whole] = "whole"
                    if entities.get(part_x) == "unclassified":
                        entities[part_x] = "part"
                    if entities.get(part_y) == "unclassified":
                        entities[part_y] = "part"

        # Product chains: does A * B match some third column?
        for a, b in combinations(names, 2):
            for c in names:
                if c in (a, b):
                    continue
                try:
                    frac = _row_match_fraction(df[a], df[b], df[c], "mul")
                except Exception:  # noqa: BLE001
                    continue
                if frac >= _MIN_MATCH_FRACTION:
                    relationships.append({
                        "type": "product", "factor_a": a, "factor_b": b, "total": c,
                        "match_fraction": round(frac, 4),
                    })
                    if entities.get(c, "unclassified") == "unclassified":
                        entities[c] = "computed_total"
                    if entities.get(a, "unclassified") == "unclassified":
                        entities[a] = "component"
                    if entities.get(b, "unclassified") == "unclassified":
                        entities[b] = "component"

        canonical_metrics = _bind_canonical_roles(entities, relationships)

        return {
            "entities": entities,
            "relationships": relationships,
            "canonical_metrics": canonical_metrics,
        }
    except Exception:  # noqa: BLE001
        return empty


# Used ONLY as a last-resort disambiguator (see _bind_canonical_roles) when
# pure structure genuinely cannot distinguish two terminal leaves of the
# same split (e.g. "Profit" vs "COGS" — both are terminal, neither splits
# further, so nothing in the *data* says which is which). Every other role
# binding below is 100% structural/empirical.
_PROFIT_LEAF_HINTS = ("profit", "margin", "net income", "ebitda")


def _bind_canonical_roles(
    entities: dict[str, str],
    relationships: list[dict[str, Any]],
) -> dict[str, str]:
    """Bind abstract BI roles to concrete column names from discovered structure.

    Structural algorithm (no column-name assumptions except the single
    documented last-resort case):

    1. **Root** (``gross_revenue``) — a column that is the "whole" of some
       split but never itself a "part" of another split (the top of the chain).
    2. **Hub** (``net_revenue``) — the part of the root's split that is ITSELF
       further split (the chain continues through it — e.g. ``Sales`` is a
       part of ``Gross Sales``'s split, and is also the whole of its own
       split into cost/profit).
    3. **Deduction** — the other part of the root's split (whatever isn't
       the hub) — fully determined once the hub is known, no ambiguity.
    4. **Profit / Cost** — the two parts of the hub's own split. Both are
       terminal leaves (neither splits further), so structure alone cannot
       tell them apart — this is the one place a light keyword check is
       used, purely to LABEL an already-structurally-discovered pair, never
       to invent or assume a relationship that wasn't found in the data.
    """
    try:
        canonical: dict[str, str] = {}
        decomp = [r for r in relationships if r["type"] == "sum_decomposition"]
        if not decomp:
            return canonical

        times_as_whole: dict[str, int] = {}
        times_as_part: dict[str, int] = {}
        for r in decomp:
            times_as_whole[r["whole"]] = times_as_whole.get(r["whole"], 0) + 1
            for p in r["parts"]:
                times_as_part[p] = times_as_part.get(p, 0) + 1
        all_nodes = set(times_as_whole) | set(times_as_part)

        roots = sorted(
            n for n in all_nodes
            if times_as_whole.get(n, 0) > 0 and times_as_part.get(n, 0) == 0
        )
        if not roots:
            return canonical
        # Multiple candidate roots (unusual/ambiguous schema): deterministic,
        # structural tie-break — the root with the most splits recorded
        # against it is the most "central" node; ties broken alphabetically
        # (never a business-meaning guess, just a stable last resort).
        root = max(roots, key=lambda n: (times_as_whole[n], ""), default=roots[0])
        canonical["gross_revenue"] = root

        root_splits = [r for r in decomp if r["whole"] == root]
        root_split = root_splits[0]  # deterministic: first (parts is already sorted)
        part_a, part_b = root_split["parts"]

        hub_candidates = [p for p in (part_a, part_b) if times_as_whole.get(p, 0) > 0]
        if len(hub_candidates) == 1:
            hub = hub_candidates[0]
            deduction = part_b if hub == part_a else part_a
            canonical["net_revenue"] = hub
            canonical["deduction"] = deduction

            hub_splits = [r for r in decomp if r["whole"] == hub]
            if hub_splits:
                leaf_a, leaf_b = hub_splits[0]["parts"]

                def _is_profit_leaf(name: str) -> bool:
                    lname = name.lower()
                    return any(kw in lname for kw in _PROFIT_LEAF_HINTS)

                if _is_profit_leaf(leaf_a) and not _is_profit_leaf(leaf_b):
                    canonical["profit"], canonical["cost"] = leaf_a, leaf_b
                elif _is_profit_leaf(leaf_b) and not _is_profit_leaf(leaf_a):
                    canonical["profit"], canonical["cost"] = leaf_b, leaf_a
                else:
                    # Structure is genuinely symmetric and no keyword hint
                    # applies either — bind deterministically (alphabetical)
                    # and mark low confidence rather than guessing silently.
                    ordered = sorted([leaf_a, leaf_b])
                    canonical["profit"], canonical["cost"] = ordered[0], ordered[1]
                    canonical["_profit_cost_confidence"] = "low"

        return canonical
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Cache validation — the schema fingerprint is (name, dataType) only, so two
# genuinely different datasets that happen to share a column-name/type
# signature (e.g. a project name reused for unrelated data next time) will
# collide on the SAME fingerprint. A false cache hit there would silently
# apply one dataset's discovered relationships (e.g. "Profit" bound to a
# real profit figure) to a completely different dataset where that column
# means something else entirely — worse than having no semantic model at
# all, because it looks authoritative. This re-checks a cached model's
# relationships against the CURRENT data before trusting it.
# ---------------------------------------------------------------------------


def validate_cached_semantic_model(model: dict[str, Any] | None, df: Any) -> bool:
    """Re-verify a cached semantic model's relationships against fresh data.

    Only checks the relationships already recorded in *model* (cheap — a
    handful of formula checks) rather than repeating the full O(column^3)
    discovery search, so most of caching's performance benefit is kept while
    closing the fingerprint-collision gap.

    Returns:
        ``True`` when the model is empty (nothing to contradict) or ALL of
        its checkable relationships still hold; ``False`` otherwise —
        callers should treat ``False`` as a cache miss and rediscover from
        scratch. Deliberately strict (not "most" or "half") — a chain's
        role bindings (e.g. "profit"/"cost") depend on EVERY link in the
        chain; one broken link makes the whole binding suspect even if an
        earlier, more generic link (e.g. a plain "A = B + C" pattern that
        coincidentally holds in unrelated data) still happens to check out.
        Also fails **closed** (returns ``False``) on any internal error —
        unlike the rest of this module, the safe default here is to
        distrust a cache we can't verify, not to trust it.
    """
    try:
        if not model:
            return False
        relationships = model.get("relationships") or []
        if not relationships:
            return True  # an empty model asserts nothing that can be contradicted

        checked = 0
        confirmed = 0
        for r in relationships:
            try:
                if r.get("type") == "sum_decomposition":
                    whole, parts = r.get("whole"), r.get("parts") or []
                    if len(parts) != 2:
                        continue
                    part_x, part_y = parts
                    if any(c not in df.columns for c in (whole, part_x, part_y)):
                        continue
                    checked += 1
                    frac = _row_match_fraction(df[whole], df[part_y], df[part_x], "sub")
                    if frac >= _MIN_MATCH_FRACTION:
                        confirmed += 1
                elif r.get("type") == "product":
                    a, b, c = r.get("factor_a"), r.get("factor_b"), r.get("total")
                    if any(col not in df.columns for col in (a, b, c)):
                        continue
                    checked += 1
                    frac = _row_match_fraction(df[a], df[b], df[c], "mul")
                    if frac >= _MIN_MATCH_FRACTION:
                        confirmed += 1
            except Exception:  # noqa: BLE001 — one bad relationship check must not sink the rest
                continue

        if checked == 0:
            # Every referenced column is missing from the current data despite
            # the fingerprint matching (shouldn't normally happen — the
            # fingerprint IS the column name/type set) — can't confirm
            # anything, so don't trust it.
            return False
        return confirmed == checked
    except Exception:  # noqa: BLE001 — fails CLOSED (distrust), see docstring
        return False


__all__ = [
    "compute_schema_fingerprint",
    "discover_semantic_relationships",
    "validate_cached_semantic_model",
]
