"""Naming convention engine — Phase 5.2.

Normalises measure / column / table names and suggests display folder
hierarchy based on lexical hints (time-intelligence prefixes, aggregation
keywords, KPI markers).

Public API:
    pascal_case(name)              -> str
    title_case(name)               -> str
    normalize_measure(name)        -> str
    suggest_folder(measure_name, base_expression=None) -> str
    plan_renames(model)            -> dict — bulk rename + folder proposal

The model dict matches the one from ``utils.tmdl_parser.read_semantic_model``.
"""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

# Words to keep upper-cased when reassembling
_ACRONYMS = {
    "YTD", "MTD", "QTD", "YOY", "MOM", "QOQ", "WOW",
    "KPI", "ID", "URL", "PY", "PM", "AM",
    "USD", "EUR", "GBP", "JPY", "CNY",
    "GB", "MB", "KB", "TB",
    "API", "ETL", "SQL", "DAX",
}

# Abbreviations to keep as one token regardless of case (YoY, MoM, ...).
# These get matched first and replaced with placeholders before generic splits.
_PROTECTED_ABBR = sorted(_ACRONYMS, key=len, reverse=True)
_PROTECTED_RE = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in _PROTECTED_ABBR) + r")\b",
    re.IGNORECASE,
)


def _tokenize(name: str) -> list[str]:
    """Split a name into word tokens, preserving acronyms.

    Handles:
        * separators (space, _, -, /)
        * acronym + PascalCase ("USDToEUR" -> ["USD", "To", "EUR"])
        * camelCase ("fooBar" -> ["foo", "Bar"])
        * letter + digit ("Q1" -> ["Q", "1"])
        * pre-registered abbreviations matched case-insensitively
          ("YoY", "MoM", "py") kept as single upper-case tokens.
    """
    if not name:
        return []

    # 1) Find protected abbreviations first (case-insensitive). We replace
    #    each match with a SINGLE letter placeholder unlikely to collide with
    #    real text, then split, then restore.
    matches: list[tuple[int, int, str]] = []
    for m in _PROTECTED_RE.finditer(name):
        matches.append((m.start(), m.end(), m.group(0).upper()))

    # Build a token list by walking the string and splitting non-protected
    # spans with the heuristic regexes; protected spans become one token.
    tokens: list[str] = []
    pos = 0
    text = name.strip()

    def _split_generic(s: str) -> list[str]:
        s = re.sub(r"[_\-/\s]+", " ", s)
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
        s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
        s = re.sub(r"(\d)([A-Za-z])", r"\1 \2", s)
        return [t for t in s.split() if t]

    # Re-walk: alternate between text-between-matches and matches.
    cursor = 0
    for start, end, upper in matches:
        if start > cursor:
            tokens.extend(_split_generic(text[cursor:start]))
        tokens.append(upper)
        cursor = end
    if cursor < len(text):
        tokens.extend(_split_generic(text[cursor:]))

    return tokens


# ---------------------------------------------------------------------------
# Case styles
# ---------------------------------------------------------------------------

def pascal_case(name: str) -> str:
    """Convert to PascalCase, preserving registered acronyms (YoY, KPI, ...).

    Examples:
        "total sales py" -> "TotalSalesPY"
        "yoy_pct"        -> "YoYPct"
        "USD_to_EUR"     -> "USDToEUR"
    """
    out = []
    for tok in _tokenize(name):
        upper = tok.upper()
        if upper in _ACRONYMS:
            out.append(upper)
        elif tok.isupper() and len(tok) <= 4:
            out.append(tok)  # likely an acronym already
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return "".join(out)


def title_case(name: str) -> str:
    """Convert to 'Title Case', keeping acronyms upper-cased.

    This is the preferred style for measure DISPLAY names ('Total Sales YoY').
    """
    out = []
    for tok in _tokenize(name):
        upper = tok.upper()
        if upper in _ACRONYMS:
            out.append(upper)
        elif tok.isupper() and len(tok) <= 4:
            out.append(tok)
        else:
            out.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(out)


def normalize_measure(name: str, style: str = "title") -> str:
    """Return a cleaned measure name.

    Args:
        name:  raw measure name
        style: "title" -> 'Total Sales YoY' | "pascal" -> 'TotalSalesYoY'
    """
    if style == "pascal":
        return pascal_case(name)
    return title_case(name)


# ---------------------------------------------------------------------------
# Display folder inference
# ---------------------------------------------------------------------------

_TIME_INTEL_KEYWORDS = ("YOY", "MOM", "QOQ", "WOW", "YTD", "MTD", "QTD", "PRIOR", "LAST YEAR", "PY")
_AGG_KEYWORDS_MAP = {
    "AVERAGE": "Aggregations",
    "AVG":     "Aggregations",
    "MEDIAN":  "Aggregations",
    "MIN":     "Aggregations",
    "MAX":     "Aggregations",
    "COUNT":   "Aggregations",
    "DISTINCT":"Aggregations",
}
_TARGET_KEYWORDS = ("TARGET", "PLAN", "BUDGET", "FORECAST", "QUOTA")
_PCT_KEYWORDS = ("PCT", "PERCENT", "%", "RATIO", "SHARE")


def suggest_folder(
    measure_name: str,
    base_expression: str | None = None,
    default: str = "Measures",
) -> str:
    """Suggest a displayFolder for a measure based on lexical cues.

    Returns a folder string with '\\' separator for hierarchy, e.g.
    "Sales\\YoY" or "Aggregations\\Counts".
    """
    name_upper = (measure_name or "").upper()
    expr_upper = (base_expression or "").upper()
    combined = f"{name_upper} {expr_upper}"

    # Time intelligence wins highest priority
    for kw in _TIME_INTEL_KEYWORDS:
        if kw in combined:
            return f"{default}\\Time Intelligence"

    # Target / budget / forecast
    for kw in _TARGET_KEYWORDS:
        if kw in combined:
            return f"{default}\\Targets"

    # Percentages / ratios
    for kw in _PCT_KEYWORDS:
        if kw in combined:
            return f"{default}\\Ratios"

    # Aggregations
    for kw, folder in _AGG_KEYWORDS_MAP.items():
        if kw in expr_upper:
            return f"{default}\\{folder}"

    return default


# ---------------------------------------------------------------------------
# Bulk rename planner
# ---------------------------------------------------------------------------

def plan_renames(
    model: dict[str, Any],
    style: str = "title",
    base_folder: str = "Measures",
    only_if_changed: bool = True,
) -> dict[str, Any]:
    """Plan renames + folder assignments for every measure in a model.

    Args:
        model:           the parsed model dict.
        style:           "title" (default) or "pascal".
        base_folder:     root folder name. Final folders look like
                         "Sales\\YoY", "Sales\\Targets", etc.
        only_if_changed: when True, omit entries whose old/new names match.

    Returns:
        {
            "renames": [{"old_name": ..., "new_name": ..., "table": ...}, ...],
            "folders": [{"name": ..., "old_folder": ..., "new_folder": ..., "table": ...}, ...],
            "skipped": [{"name": ..., "reason": ...}],
            "total_measures": int,
            "total_renames":  int,
            "total_folder_changes": int,
        }
    """
    renames: list[dict[str, str]] = []
    folders: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    seen_new: set[str] = set()

    for m in model.get("all_measures", []):
        old_name = m.get("name", "")
        if not old_name:
            continue
        new_name = normalize_measure(old_name, style=style)

        # disambiguate collisions
        if new_name in seen_new:
            skipped.append({"name": old_name, "reason": "collision with existing rename"})
            continue
        seen_new.add(new_name)

        if (not only_if_changed) or (new_name != old_name):
            renames.append({
                "old_name": old_name,
                "new_name": new_name,
                "table": m.get("table", ""),
            })

        old_folder = m.get("displayFolder", "") or ""
        new_folder = suggest_folder(
            new_name,
            base_expression=m.get("expression", ""),
            default=base_folder,
        )
        if (not only_if_changed) or (new_folder != old_folder):
            folders.append({
                "name": new_name,
                "old_folder": old_folder,
                "new_folder": new_folder,
                "table": m.get("table", ""),
            })

    return {
        "renames": renames,
        "folders": folders,
        "skipped": skipped,
        "total_measures": len(model.get("all_measures", [])),
        "total_renames": len(renames),
        "total_folder_changes": len(folders),
    }
