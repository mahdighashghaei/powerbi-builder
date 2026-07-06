"""BPA rule definitions.

Each rule is a callable that receives the parsed model dict and returns
zero or more finding dicts. The engine wires them up.

Rule keys follow the convention ``<CATEGORY>_<SHORT_NAME>``:
    PERF_*  performance impact
    META_*  metadata / discoverability
    STYLE_* naming / formatting conventions
    DAX_*   DAX expression quality
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable


Model = dict[str, Any]
Finding = dict[str, Any]
RuleFn = Callable[[Model], Iterable[Finding]]


def _finding(
    rule_id: str,
    severity: str,
    category: str,
    summary: str,
    target: str,
    detail: str = "",
    fix_hint: str = "",
) -> Finding:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "category": category,
        "summary": summary,
        "target": target,
        "detail": detail,
        "fix_hint": fix_hint,
    }


# ---------------------------------------------------------------------------
# Performance rules
# ---------------------------------------------------------------------------

def rule_perf_use_summarizecolumns(model: Model) -> Iterable[Finding]:
    """Flag measures that use SUMMARIZE() instead of SUMMARIZECOLUMNS().

    SUMMARIZE() with extension columns has well-known performance issues
    compared to SUMMARIZECOLUMNS() or ADDCOLUMNS(SUMMARIZE(...), ...).
    """
    pat = re.compile(r"\bSUMMARIZE\s*\(", re.IGNORECASE)
    sc_pat = re.compile(r"\bSUMMARIZECOLUMNS\s*\(", re.IGNORECASE)
    for m in model.get("all_measures", []):
        expr = m.get("expression", "") or ""
        if pat.search(expr) and not sc_pat.search(expr):
            yield _finding(
                "PERF_USE_SUMMARIZECOLUMNS",
                "warning",
                "performance",
                "Avoid SUMMARIZE with extension columns",
                f"measure '{m['name']}'",
                detail="SUMMARIZE materialises intermediate tables; SUMMARIZECOLUMNS is faster.",
                fix_hint="Replace SUMMARIZE(table, group, \"x\", expr) with SUMMARIZECOLUMNS(group, table, \"x\", expr).",
            )


def rule_perf_avoid_calculated_columns(model: Model) -> Iterable[Finding]:
    """Warn when calculated columns are detected on import tables.

    Calculated columns consume memory and bypass query folding; prefer
    Power Query transformations or measures.
    """
    for t in model.get("tables", []):
        for c in t.get("columns", []):
            # Heuristic: calculated columns usually have no sourceColumn match
            # We can't detect this perfectly without TMDL "expression" parsing —
            # this is a placeholder hook for future enhancement.
            if c.get("isCalculated"):
                yield _finding(
                    "PERF_AVOID_CALCULATED_COLUMNS",
                    "warning",
                    "performance",
                    "Calculated column detected",
                    f"column '{t['table_name']}'[{c['name']}]",
                    detail="Calculated columns increase model size and disable query folding.",
                    fix_hint="Move logic to Power Query (M) or a measure if possible.",
                )


def rule_perf_avoid_iterators_on_large(model: Model) -> Iterable[Finding]:
    """Flag SUMX/AVERAGEX/etc. over likely-large fact tables in measures."""
    iter_pat = re.compile(
        r"\b(SUMX|AVERAGEX|MINX|MAXX|COUNTX|PRODUCTX|GEOMEANX|RANKX)\s*\(",
        re.IGNORECASE,
    )
    for m in model.get("all_measures", []):
        expr = m.get("expression", "") or ""
        if iter_pat.search(expr) and "FILTER" in expr.upper():
            yield _finding(
                "PERF_AVOID_ITERATORS",
                "info",
                "performance",
                "Iterator + FILTER combination",
                f"measure '{m['name']}'",
                detail="X-iterators wrapping FILTER often miss the storage-engine fast path.",
                fix_hint="Consider CALCULATE(SUM(...), <condition>) or KEEPFILTERS.",
            )


# ---------------------------------------------------------------------------
# Metadata rules
# ---------------------------------------------------------------------------

def rule_meta_display_folder(model: Model) -> Iterable[Finding]:
    """Every measure should have a displayFolder for organization."""
    for m in model.get("all_measures", []):
        if not (m.get("displayFolder") or "").strip():
            yield _finding(
                "META_DISPLAY_FOLDER",
                "info",
                "metadata",
                "Measure missing displayFolder",
                f"measure '{m['name']}'",
                detail="Folders group related measures in the field pane.",
                fix_hint=f"Add displayFolder, e.g. 'Sales' or 'Sales\\\\YoY'.",
            )


def rule_meta_format_string(model: Model) -> Iterable[Finding]:
    """Numeric / currency / percent measures should set formatString."""
    for m in model.get("all_measures", []):
        if not (m.get("formatString") or "").strip():
            # only flag if the expression hints at a numeric output
            expr = (m.get("expression") or "").upper()
            looks_numeric = any(
                kw in expr for kw in ("SUM(", "AVERAGE(", "COUNT", "DIVIDE", "CALCULATE")
            )
            if looks_numeric:
                yield _finding(
                    "META_FORMAT_STRING",
                    "info",
                    "metadata",
                    "Numeric measure missing formatString",
                    f"measure '{m['name']}'",
                    detail="formatString controls how the value displays in visuals.",
                    fix_hint='Add formatString, e.g. "$"#,0 or 0.0%;-0.0%;0.0%.',
                )


# ---------------------------------------------------------------------------
# Style / naming rules
# ---------------------------------------------------------------------------

_RESERVED_TABLES = {"measures", "table", "values", "row"}


def rule_style_measure_naming(model: Model) -> Iterable[Finding]:
    """Measure names should not contain underscores or be all-lowercase."""
    for m in model.get("all_measures", []):
        name = m.get("name", "")
        if "_" in name:
            yield _finding(
                "STYLE_MEASURE_NAMING_UNDERSCORE",
                "info",
                "style",
                "Measure name contains underscore",
                f"measure '{name}'",
                fix_hint="Use spaces or PascalCase, e.g. 'Total Sales' or 'TotalSales'.",
            )
        if name and name.islower():
            yield _finding(
                "STYLE_MEASURE_NAMING_LOWERCASE",
                "info",
                "style",
                "Measure name is all lowercase",
                f"measure '{name}'",
                fix_hint="Capitalise first letter of each word.",
            )


def rule_style_no_reserved_table_name(model: Model) -> Iterable[Finding]:
    """Avoid reserved/ambiguous table names like 'Measures' or 'Table'."""
    for t in model.get("tables", []):
        nm = (t.get("table_name") or "").lower()
        if nm in _RESERVED_TABLES:
            yield _finding(
                "STYLE_RESERVED_TABLE_NAME",
                "warning",
                "style",
                "Reserved or ambiguous table name",
                f"table '{t['table_name']}'",
                detail=f"Power BI reserves names like {sorted(_RESERVED_TABLES)}.",
                fix_hint="Rename to something domain-specific, e.g. '_Metrics' or 'Sales'.",
            )


# ---------------------------------------------------------------------------
# DAX quality rules
# ---------------------------------------------------------------------------

def rule_dax_avoid_divide_operator(model: Model) -> Iterable[Finding]:
    """Flag /  in DAX where DIVIDE() should be used to avoid /0 errors.

    Heuristic: a "/" between two parenthesised expressions and not inside a
    string literal. We don't try to be perfect — we flag candidates.
    """
    # strip string literals before scanning
    str_lit = re.compile(r'"[^"\n]*"')
    div_pat = re.compile(r"\)\s*/\s*[A-Za-z\[(]")
    for m in model.get("all_measures", []):
        expr = m.get("expression", "") or ""
        cleaned = str_lit.sub('""', expr)
        if div_pat.search(cleaned) and "DIVIDE" not in cleaned.upper():
            yield _finding(
                "DAX_USE_DIVIDE",
                "info",
                "dax",
                "Division operator used instead of DIVIDE()",
                f"measure '{m['name']}'",
                detail="DIVIDE(a, b) returns blank on zero-divide; '/' returns infinity error.",
                fix_hint="Wrap the division: DIVIDE(<numer>, <denom>).",
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BPA_RULES: list[tuple[str, str, RuleFn]] = [
    ("PERF_USE_SUMMARIZECOLUMNS",          "performance", rule_perf_use_summarizecolumns),
    ("PERF_AVOID_CALCULATED_COLUMNS",      "performance", rule_perf_avoid_calculated_columns),
    ("PERF_AVOID_ITERATORS",               "performance", rule_perf_avoid_iterators_on_large),
    ("META_DISPLAY_FOLDER",                "metadata",    rule_meta_display_folder),
    ("META_FORMAT_STRING",                 "metadata",    rule_meta_format_string),
    ("STYLE_MEASURE_NAMING",               "style",       rule_style_measure_naming),
    ("STYLE_RESERVED_TABLE_NAME",          "style",       rule_style_no_reserved_table_name),
    ("DAX_USE_DIVIDE",                     "dax",         rule_dax_avoid_divide_operator),
]


_RULE_DESCRIPTIONS = {
    "PERF_USE_SUMMARIZECOLUMNS":     "Detect SUMMARIZE used as a replacement for SUMMARIZECOLUMNS.",
    "PERF_AVOID_CALCULATED_COLUMNS": "Warn on calculated columns (prefer M or measures).",
    "PERF_AVOID_ITERATORS":          "Iterator + FILTER pattern that misses storage-engine fast path.",
    "META_DISPLAY_FOLDER":           "Every measure should have a displayFolder.",
    "META_FORMAT_STRING":            "Numeric measures should set formatString.",
    "STYLE_MEASURE_NAMING":          "Measure names should be PascalCase or 'Title Case' (no underscores).",
    "STYLE_RESERVED_TABLE_NAME":     "Reject reserved/ambiguous table names (Measures, Table, ...).",
    "DAX_USE_DIVIDE":                "Prefer DIVIDE() over the '/' operator.",
}


def list_bpa_rules() -> list[dict[str, str]]:
    """Return the list of registered rules with category + description."""
    return [
        {
            "rule_id": rid,
            "category": cat,
            "description": _RULE_DESCRIPTIONS.get(rid, ""),
        }
        for rid, cat, _fn in BPA_RULES
    ]
