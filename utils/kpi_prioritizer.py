"""utils/kpi_prioritizer.py — Business-aware KPI Prioritization Layer.

Replaces the "first monetary column discovered" heuristic (``buckets["amount"][0]``,
in raw CSV column order) with a ranking derived from:

  1. **Common BI conventions** — semantic keyword tiers (profit/margin outrank
     revenue/sales, which outrank cost, which outranks unit prices and
     discounts).
  2. **Business analysis** — ``BusinessAnalysis.important_measures`` /
     ``.executive_metrics`` (``agents/data_analyzer_agent.py``), already
     computed upstream but previously only used for a narrow boolean
     partition.
  3. **Business description** — tokens from the user's actual request boost
     matching columns, so "track revenue, profit margin..." prioritizes
     exactly those columns for *this* run.
  4. **Schema context** — ties are broken by original column order, so a
     dataset with no distinguishing signal keeps its previous (byte-identical)
     behavior.

The ranking is computed once (``agents/orchestrator.py``, right after the
schema is finalized) and stored in ``ctx.extra["prioritized_kpis"]`` so
DAXAgent, ReportAgent/VisualPlannerAgent, InsightsAgent, and JudgeLayer all
agree on the same business-importance order. Every consumer also accepts a
local fallback (calling ``rank_kpi_candidates`` directly) so standalone/unit
usage without an orchestrator-populated context still works.

Fail-safe contract: every public function is exception-safe and degrades to
a neutral default (original order / ``None``) on any internal error.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Semantic tiers (common BI conventions)
# ---------------------------------------------------------------------------

_TIER_KEYWORDS: list[tuple[float, tuple[str, ...]]] = [
    (100.0, ("net profit", "profit", "margin", "ebitda", "net income")),
    (90.0,  ("revenue", "net sales", "sales", "turnover")),
    (75.0,  ("gross sales", "bookings", "billings", "gmv", "income")),
    (55.0,  ("amount", "value", "total")),
    (45.0,  ("cost", "cogs", "expense", "expenditure", "spend")),
    # Part 2 robustness finding: "price"/"rate" alone missed real per-unit
    # metrics with unconventional naming (e.g. "avg_ticket", "per_unit_cost",
    # "yield_pct"). Added generic, domain-agnostic BI-convention markers for
    # "this is already an average/per-unit/percentage figure" rather than
    # trying to enumerate domain-specific rate vocabulary (that doesn't
    # generalize — e.g. "conversion", "click_through" are deliberately left
    # uncaught; see utils/kpi_prioritizer.py's Part 2 stress-test notes).
    (25.0,  ("unit price", "manufacturing price", "price", "rate",
             "avg", "average", "per unit", "unit_", "pct", "percent")),
    (10.0,  ("discount", "fee", "charge", "tax", "surcharge")),
]

# Unknown amount-like columns with no keyword match — a mid-tier default so
# they're neither favored nor penalised relative to generic "amount" columns.
_DEFAULT_TIER = 50.0

_PROFIT_TIER_KEYWORDS = _TIER_KEYWORDS[0][1]
_REVENUE_TIER_KEYWORDS = _TIER_KEYWORDS[1][1] + _TIER_KEYWORDS[2][1]
_COST_TIER_KEYWORDS = _TIER_KEYWORDS[4][1]

_IMPORTANT_BONUS = 15.0
_EXECUTIVE_BONUS = 10.0
_DESCRIPTION_BONUS = 20.0
_MIN_DESC_TOKEN_LEN = 4  # skip short/noise words ("the", "and", ...)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9]*", (text or "").lower()))


def _tier_score(name: str) -> float:
    """Highest matching tier, or the neutral default when nothing matches.

    Note: matched tiers are compared against each other, NOT against the
    default — several tiers (cost=45, price=25, discount=10) sit below the
    default (50) on purpose, so a matched low tier must not be floor-clamped
    back up to the default.
    """
    lname = name.lower()
    matched_tiers = [tier for tier, keywords in _TIER_KEYWORDS if any(kw in lname for kw in keywords)]
    return max(matched_tiers) if matched_tiers else _DEFAULT_TIER


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rank_kpi_candidates(
    amount_columns: list[dict[str, Any]],
    business_analysis: Any | None = None,
    business_description: str = "",
    semantic_model: dict[str, Any] | None = None,
) -> list[str]:
    """Return amount-column names ordered by business importance (highest first).

    Args:
        amount_columns:        Column dicts (``{"name": ...}``) from the
                                ``"amount"`` bucket of ``_classify_columns``.
        business_analysis:     Optional ``BusinessAnalysis`` (may be ``None``,
                                e.g. in edit_pbip/edit_pbix mode).
        business_description:  Raw business intent text for this run.
        semantic_model:        Optional model from
                                ``utils.semantic_model.discover_semantic_relationships``.
                                When provided, its ``canonical_metrics`` break
                                same-tier ties using the *empirically
                                discovered* net/gross/profit/cost structure
                                (e.g. "Sales" over "Gross Sales") instead of
                                falling through to raw column order — column
                                order is now a documented last-resort only,
                                used solely when neither the tier score nor
                                the semantic structure can distinguish two
                                candidates.
    """
    try:
        if not amount_columns:
            return []

        important: set[str] = set()
        executive: set[str] = set()
        if business_analysis is not None:
            important = {
                m.lower() for m in (getattr(business_analysis, "important_measures", None) or [])
            }
            executive = {
                m.lower() for m in (getattr(business_analysis, "executive_metrics", None) or [])
            }
        desc_tokens = {t for t in _tokenize(business_description) if len(t) >= _MIN_DESC_TOKEN_LEN}

        canonical: dict[str, str] = (
            (semantic_model or {}).get("canonical_metrics") or {} if semantic_model else {}
        )
        # Structural preference, derived from discovered relationships, not
        # column naming: the most-derived bottom-line figure ranks highest,
        # the pre-deduction "gross" figure ranks lowest among the chain.
        semantic_rank: dict[str, int] = {}
        for role, rank in (("profit", 3), ("net_revenue", 2), ("cost", 1),
                           ("deduction", 1), ("gross_revenue", 0)):
            col_name = canonical.get(role)
            if col_name:
                semantic_rank.setdefault(col_name, rank)

        scored: list[tuple[float, int, int, str]] = []
        for idx, col in enumerate(amount_columns):
            name = col.get("name")
            if not name:
                continue
            lname = name.lower()
            score = _tier_score(name)
            if lname in important:
                score += _IMPORTANT_BONUS
            if lname in executive:
                score += _EXECUTIVE_BONUS
            if any(tok in lname for tok in desc_tokens):
                score += _DESCRIPTION_BONUS
            scored.append((score, semantic_rank.get(name, -1), -idx, name))

        # Higher score first; on a tie, higher semantic structural rank
        # first; only as an absolute last resort, smaller original index.
        scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [name for *_, name in scored]
    except Exception:  # noqa: BLE001 — fail-safe: preserve original order
        return [c.get("name") for c in amount_columns if c.get("name")]


def reorder_by_priority(
    columns: list[dict[str, Any]],
    priority_names: list[str] | None,
) -> list[dict[str, Any]]:
    """Reorder *columns* so entries named in *priority_names* come first, in
    that order; everything else keeps its original relative order after."""
    try:
        if not priority_names:
            return list(columns)
        by_name = {c.get("name"): c for c in columns}
        ordered = [by_name[n] for n in priority_names if n in by_name]
        placed = {c.get("name") for c in ordered}
        remainder = [c for c in columns if c.get("name") not in placed]
        return ordered + remainder
    except Exception:  # noqa: BLE001
        return list(columns)


def get_primary_kpi(
    amount_columns: list[dict[str, Any]],
    business_analysis: Any | None = None,
    business_description: str = "",
) -> str | None:
    """Convenience wrapper — the single highest-priority KPI column name."""
    ranked = rank_kpi_candidates(amount_columns, business_analysis, business_description)
    return ranked[0] if ranked else None


def pick_revenue_and_cost_columns(
    amount_columns: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pick a (revenue-like, cost-like) column pair for margin calculations.

    Position-independent — a profit-tier column (e.g. "Profit") is NEVER
    picked as "revenue" (a margin needs a top-line figure, not an
    already-derived profit). When no explicit cost-tier column exists, falls
    back to the first two candidates in list order — the same behavior as
    the positional ``buckets["amount"][:2]`` slicing this replaces, so
    datasets with no cost-tier column (most existing fixtures) are
    unaffected.
    """
    try:
        if not amount_columns:
            return None, None

        cost_col = next(
            (c for c in amount_columns
             if any(kw in (c.get("name") or "").lower() for kw in _COST_TIER_KEYWORDS)),
            None,
        )

        rev_candidates = [
            c for c in amount_columns
            if c is not cost_col
            and not any(kw in (c.get("name") or "").lower() for kw in _PROFIT_TIER_KEYWORDS)
        ]
        rev_col = next(
            (c for c in rev_candidates
             if any(kw in (c.get("name") or "").lower() for kw in _REVENUE_TIER_KEYWORDS)),
            None,
        )
        if rev_col is None and rev_candidates:
            rev_col = rev_candidates[0]
        if rev_col is None and amount_columns:
            # every column is profit-tier or the cost column — fall back to
            # positional behavior rather than returning nothing.
            rev_col = next((c for c in amount_columns if c is not cost_col), amount_columns[0])

        if cost_col is None:
            # A profit-tier column must never be picked as "cost" either — a
            # margin needs (revenue, cost), never (revenue, profit) or
            # (profit, profit).
            remaining = [
                c for c in amount_columns
                if c is not rev_col
                and not any(kw in (c.get("name") or "").lower() for kw in _PROFIT_TIER_KEYWORDS)
            ]
            cost_col = remaining[0] if remaining else None

        return rev_col, cost_col
    except Exception:  # noqa: BLE001 — fall back to the old positional behavior
        return (
            amount_columns[0] if amount_columns else None,
            amount_columns[1] if len(amount_columns) > 1 else None,
        )


# ---------------------------------------------------------------------------
# Aggregation Safety Fix — rate vs. flow classification
# ---------------------------------------------------------------------------

_RATE_TIER_KEYWORDS = _TIER_KEYWORDS[5][1]  # ("unit price", "manufacturing price", "price", "rate")


def is_rate_column(name: str) -> bool:
    """True when *name* looks like a per-unit rate/price, not a summable flow.

    Rate columns (unit prices, manufacturing cost per unit, rates) must
    never be ``SUM``-aggregated into a headline KPI — see
    ``agents/dax_agent.py::_sanitize_rate_aggregations``.
    """
    try:
        lname = (name or "").lower()
        return any(kw in lname for kw in _RATE_TIER_KEYWORDS)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Derived-KPI catalog — metric-centric prioritization (root-cause fix)
# ---------------------------------------------------------------------------
#
# The prior ranking could only ever nominate an EXISTING raw column as the
# "primary KPI" — a derived concept the user explicitly names ("profit
# margin", "discount impact") could never win because it isn't a column.
# This catalog synthesizes those concepts as first-class candidates, bound
# to real column names via the Semantic Truth Layer when available.

CANONICAL_METRIC_TEMPLATES: tuple[dict[str, str], ...] = (
    {"concept": "margin", "name": "Profit Margin %",
     "numerator_role": "profit", "denominator_role": "net_revenue"},
    {"concept": "discount", "name": "Discount Rate %",
     "numerator_role": "deduction", "denominator_role": "gross_revenue"},
    {"concept": "cost", "name": "Cost Ratio %",
     "numerator_role": "cost", "denominator_role": "net_revenue"},
)


def _find_column_by_keywords(
    amount_columns: list[dict[str, Any]], keywords: tuple[str, ...],
    exclude: set[str] | None = None,
) -> dict[str, Any] | None:
    exclude = exclude or set()
    for c in amount_columns:
        name = c.get("name")
        if not name or name in exclude:
            continue
        if any(kw in name.lower() for kw in keywords):
            return c
    return None


def derive_candidate_kpis(
    semantic_model: dict[str, Any] | None,
    amount_columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Synthesize derived-metric KPI candidates (Margin %, Discount Rate %,
    Cost Ratio %) bound to real column names.

    Prefers the Semantic Truth Layer's empirically-discovered
    ``canonical_metrics`` (numerator/denominator are *proven* by the data,
    not guessed); falls back to tier-keyword pairing when no semantic model
    is available, so behavior degrades gracefully rather than producing
    nothing. Returns ``[]`` when neither source yields a usable pair for a
    given concept — never fabricates a ratio from unrelated columns.
    """
    try:
        candidates: list[dict[str, Any]] = []
        canonical: dict[str, str] = (
            (semantic_model or {}).get("canonical_metrics") or {} if semantic_model else {}
        )
        available = {c.get("name") for c in (amount_columns or []) if c.get("name")}

        covered_concepts: set[str] = set()
        for tmpl in CANONICAL_METRIC_TEMPLATES:
            num = canonical.get(tmpl["numerator_role"])
            den = canonical.get(tmpl["denominator_role"])
            if num and den and num != den and num in available and den in available:
                candidates.append({
                    "concept": tmpl["concept"], "name": tmpl["name"],
                    "numerator": num, "denominator": den,
                    "formula": f"{num} / {den}", "source": "semantic_model",
                })
                covered_concepts.add(tmpl["concept"])

        # Fallback: tier-keyword pairing for any concept the semantic model
        # didn't resolve (no model available, or the relevant relationship
        # wasn't empirically discovered for this dataset).
        if "margin" not in covered_concepts:
            profit_col = _find_column_by_keywords(amount_columns, _PROFIT_TIER_KEYWORDS)
            revenue_col = _find_column_by_keywords(
                amount_columns, _REVENUE_TIER_KEYWORDS,
                exclude={profit_col["name"]} if profit_col else set(),
            )
            if profit_col and revenue_col:
                candidates.append({
                    "concept": "margin", "name": "Profit Margin %",
                    "numerator": profit_col["name"], "denominator": revenue_col["name"],
                    "formula": f"{profit_col['name']} / {revenue_col['name']}",
                    "source": "tier_fallback",
                })

        if "discount" not in covered_concepts:
            discount_col = _find_column_by_keywords(amount_columns, _TIER_KEYWORDS[6][1])
            gross_col = _find_column_by_keywords(amount_columns, _TIER_KEYWORDS[2][1]) or \
                _find_column_by_keywords(
                    amount_columns, _REVENUE_TIER_KEYWORDS,
                    exclude={discount_col["name"]} if discount_col else set(),
                )
            if discount_col and gross_col and discount_col["name"] != gross_col["name"]:
                candidates.append({
                    "concept": "discount", "name": "Discount Rate %",
                    "numerator": discount_col["name"], "denominator": gross_col["name"],
                    "formula": f"{discount_col['name']} / {gross_col['name']}",
                    "source": "tier_fallback",
                })

        if "cost" not in covered_concepts:
            cost_col = _find_column_by_keywords(amount_columns, _COST_TIER_KEYWORDS)
            revenue_col = _find_column_by_keywords(
                amount_columns, _REVENUE_TIER_KEYWORDS,
                exclude={cost_col["name"]} if cost_col else set(),
            )
            if cost_col and revenue_col:
                candidates.append({
                    "concept": "cost", "name": "Cost Ratio %",
                    "numerator": cost_col["name"], "denominator": revenue_col["name"],
                    "formula": f"{cost_col['name']} / {revenue_col['name']}",
                    "source": "tier_fallback",
                })

        return candidates
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Binary-outcome column detection — closes the "non-financial domain" gap
# ---------------------------------------------------------------------------
#
# Everything above ranks/derives KPIs from numeric "amount" columns. Many
# real datasets (marketing response, churn, fraud, conversion) have no
# monetary amount at all — their real KPI is a RATE derived from a binary
# categorical outcome column (e.g. "y" = yes/no in a bank-marketing
# dataset). This section detects that column so DAXAgent can guarantee a
# conversion/outcome-rate measure the same way it guarantees Margin %/
# Discount Rate % for financial datasets.

# Strong hints: genuinely placeholder-like names that don't describe a
# specific real-world attribute on their own — almost always the ML/BI
# convention for "the variable of interest," never a demographic feature.
_STRONG_OUTCOME_NAME_HINTS = ("y", "target", "label", "outcome", "response", "class", "result")

# Weak hints: plausible outcome names, but also plausible as an ordinary
# feature column in some domains (e.g. "default" is the target in a credit-
# risk dataset, but just a feature in a marketing dataset) — worth a little
# credit, not enough to fire alone without the value-vocabulary match too.
_WEAK_OUTCOME_NAME_HINTS = ("conversion", "converted", "success", "churn", "churned",
                            "subscribed", "subscription", "purchased", "default",
                            "fraud", "clicked", "flag")

# Recognized binary-outcome value encodings (case-insensitive). Deliberately
# does NOT include demographic-coded pairs like {"m","f"} / {"male","female"}
# — those must never be mistaken for an outcome column.
_BINARY_VALUE_VOCAB: tuple[frozenset[str], ...] = (
    frozenset({"yes", "no"}), frozenset({"true", "false"}), frozenset({"1", "0"}),
    frozenset({"y", "n"}), frozenset({"success", "failure"}), frozenset({"success", "fail"}),
    frozenset({"converted", "not converted"}), frozenset({"subscribed", "not subscribed"}),
    frozenset({"active", "inactive"}), frozenset({"positive", "negative"}),
)

# Which value, when present, is treated as "the positive outcome" for the
# rate's numerator — first match wins.
_POSITIVE_VALUE_PRIORITY = ("yes", "true", "1", "y", "success", "converted",
                             "subscribed", "active", "completed", "positive")

_GENERIC_PLACEHOLDER_NAMES = frozenset({"y", "target", "label", "class", "outcome", "response", "result"})

_OUTCOME_STRONG_NAME_SCORE = 3
_OUTCOME_WEAK_NAME_SCORE = 1
_OUTCOME_VALUE_VOCAB_SCORE = 2
_OUTCOME_LAST_COLUMN_SCORE = 1   # "the label is the last column" — a common ML/BI dataset convention
_OUTCOME_DESCRIPTION_SCORE = 1
_OUTCOME_MIN_FIRE_SCORE = 3       # must clear this bar to fire at all


def _pick_positive_value(distinct_values: list[str]) -> str | None:
    """Pick which of the two distinct values is "the positive outcome",
    preserving the ORIGINAL casing from the data (DAX filter needs the
    actual value, not a normalized one)."""
    by_lower = {str(v).strip().lower(): v for v in distinct_values}
    for candidate in _POSITIVE_VALUE_PRIORITY:
        if candidate in by_lower:
            return by_lower[candidate]
    return distinct_values[0] if distinct_values else None


def detect_outcome_column(
    schema_columns: list[dict[str, Any]],
    data_profile: dict[str, Any] | None,
    business_description: str = "",
) -> dict[str, Any] | None:
    """Find a 2-distinct-value column that looks like a binary outcome/target.

    Uses ``distinct_count``/``distinct_values`` already computed during
    schema inference (``mcp_server/schema_inference.py``) — no re-read of
    the source file. Must clear ``_OUTCOME_MIN_FIRE_SCORE`` to fire at all,
    so a plain demographic binary (e.g. a "Gender" M/F column) is never
    mistaken for an outcome column — see ``tests/test_kpi_prioritizer.py``
    for the explicit false-positive regression test.

    Returns:
        ``{"column": name, "positive_value": ..., "measure_name": ...}``
        or ``None`` when nothing clears the bar.
    """
    try:
        if not schema_columns or not data_profile:
            return None
        cols_by_name: dict[str, Any] = (data_profile.get("quality") or {}).get("columns") or {}
        if not cols_by_name:
            return None

        from utils.concept_coverage import extract_concepts
        description_hits_conversion = "conversion" in set(extract_concepts(business_description))

        last_column_name = schema_columns[-1].get("name") if schema_columns else None

        candidates: list[tuple[int, str, list[str]]] = []
        for col in schema_columns:
            name = col.get("name")
            if not name:
                continue
            prof = cols_by_name.get(name) or {}
            distinct_values = prof.get("distinct_values")
            if prof.get("distinct_count") != 2 or not distinct_values or len(distinct_values) != 2:
                continue

            score = 0
            lname = name.lower().replace(".", " ").replace("_", " ")
            if any(hint in lname for hint in _STRONG_OUTCOME_NAME_HINTS):
                score += _OUTCOME_STRONG_NAME_SCORE
            elif any(hint in lname for hint in _WEAK_OUTCOME_NAME_HINTS):
                score += _OUTCOME_WEAK_NAME_SCORE

            values_lower = frozenset(str(v).strip().lower() for v in distinct_values)
            if values_lower in _BINARY_VALUE_VOCAB:
                score += _OUTCOME_VALUE_VOCAB_SCORE

            if name == last_column_name:
                score += _OUTCOME_LAST_COLUMN_SCORE
            if description_hits_conversion:
                score += _OUTCOME_DESCRIPTION_SCORE

            if score >= _OUTCOME_MIN_FIRE_SCORE:
                candidates.append((score, name, list(distinct_values)))

        if not candidates:
            return None

        # Highest score wins; Python's sort is stable, so on a tie the
        # earlier-encountered column (schema order) is kept — a documented
        # last resort, same convention as utils.kpi_prioritizer's own
        # column-order tie-break.
        candidates.sort(key=lambda t: t[0], reverse=True)
        _, col_name, distinct_values = candidates[0]

        positive_value = _pick_positive_value(distinct_values)
        if positive_value is None:
            return None

        if col_name.lower() in _GENERIC_PLACEHOLDER_NAMES:
            measure_name = "Conversion Rate %"
        else:
            measure_name = f"{col_name.replace('_', ' ').replace('.', ' ').title()} Rate %"

        return {
            "column": col_name,
            "positive_value": positive_value,
            "measure_name": measure_name,
        }
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "rank_kpi_candidates",
    "reorder_by_priority",
    "get_primary_kpi",
    "pick_revenue_and_cost_columns",
    "is_rate_column",
    "derive_candidate_kpis",
    "CANONICAL_METRIC_TEMPLATES",
    "detect_outcome_column",
]
