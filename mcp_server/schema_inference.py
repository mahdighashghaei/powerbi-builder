"""Lightweight CSV schema inference.

Kept separate from :mod:`server` so it can be unit-tested without spinning up
the MCP transport, and so the SchemaAgent can reuse the same inference logic
directly (the agents do not need an MCP round-trip to read a CSV).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

# pandas is the nice path, but we keep a stdlib fallback so the project still
# runs if pandas is somehow unavailable (e.g. a minimal test env).
try:
    import pandas as _pd  # type: ignore
    _HAS_PANDAS = True
except Exception:  # pragma: no cover
    _pd = None
    _HAS_PANDAS = False

# TMDL/Power BI supported data types
_PB_TYPES = {"string", "int64", "double", "decimal", "boolean", "dateTime", "date"}

# Map pandas/numpy dtypes -> Power BI data types
_PANDAS_DTYPE_MAP = {
    "object": "string",
    "string": "string",
    "int64": "int64",
    "Int64": "int64",
    "int32": "int64",
    "float64": "double",
    "float32": "double",
    "bool": "boolean",
    "boolean": "boolean",
    "datetime64[ns]": "dateTime",
}


# ---------------------------------------------------------------------------
# Data-quality profiling (Phase 1 — foundation for DataAnalyzer/Cleaner)
# ---------------------------------------------------------------------------


def _profile_column(series, name: str, pb_type: str, row_count: int) -> dict[str, Any]:
    """Compute quality statistics for a single column.

    Returns a dict with null_count, null_pct, distinct_count, unique_pct,
    min/max (numeric/date), min_len/max_len (string), outlier_count (IQR).
    All stats are derived from the in-memory sample; they are estimates when
    the file was sampled (caller passes the sample row_count).
    """
    prof: dict[str, Any] = {
        "null_count": 0,
        "null_pct": 0.0,
        "distinct_count": 0,
        "unique_pct": 0.0,
        "outlier_count": 0,
    }
    try:
        null_count = int(series.isna().sum())
        non_null = series.dropna()
        distinct = int(non_null.nunique())
        prof["null_count"] = null_count
        prof["null_pct"] = round(null_count / max(row_count, 1) * 100, 2)
        prof["distinct_count"] = distinct
        prof["unique_pct"] = round(distinct / max(len(non_null), 1) * 100, 2)

        # Low-cardinality columns (e.g. a binary yes/no outcome column) are
        # cheap to fully enumerate since the column is already in memory —
        # capture the actual values, not just the count. Used by
        # utils.kpi_prioritizer.detect_outcome_column to recognize a binary
        # outcome/target column without a second read of the source file.
        # Gated to distinct <= 10 so real high-cardinality columns never see
        # their profile bloated.
        if 0 < distinct <= 10:
            try:
                prof["distinct_values"] = sorted(str(v) for v in non_null.unique())
            except Exception:
                pass

        if pb_type in {"int64", "double", "decimal"}:
            try:
                # .min()/.max() on a numeric Series return a numpy scalar
                # (e.g. numpy.int64), not a native Python int/float -- unlike
                # every other stat in this function, which already casts.
                # json.dumps() can't serialize numpy scalars, so this only
                # ever broke callers that actually JSON-encode the profile
                # (e.g. the real MCP stdio transport) rather than passing
                # the dict through in-process.
                prof["min"] = non_null.min().item()
                prof["max"] = non_null.max().item()
                # IQR outlier detection
                q1 = non_null.quantile(0.25)
                q3 = non_null.quantile(0.75)
                iqr = q3 - q1
                if iqr > 0:
                    lower = q1 - 1.5 * iqr
                    upper = q3 + 1.5 * iqr
                    prof["outlier_count"] = int(((non_null < lower) | (non_null > upper)).sum())
            except Exception:
                pass
        elif pb_type in {"dateTime", "date"}:
            try:
                prof["min"] = str(non_null.min())
                prof["max"] = str(non_null.max())
            except Exception:
                pass
        elif pb_type == "string":
            try:
                lengths = non_null.astype(str).str.len()
                prof["min_len"] = int(lengths.min()) if len(lengths) else 0
                prof["max_len"] = int(lengths.max()) if len(lengths) else 0
                prof["avg_len"] = round(float(lengths.mean()), 1) if len(lengths) else 0.0
            except Exception:
                pass
    except Exception:
        pass
    return prof


def _profile_table(df, columns: list[dict[str, Any]], row_count: int) -> dict[str, Any]:
    """Compute table-level quality stats: duplicate rows + per-column profiles."""
    profile: dict[str, Any] = {
        "duplicate_rows": 0,
        "duplicate_pct": 0.0,
        "columns": {},
    }
    try:
        dup = int(df.duplicated().sum())
        profile["duplicate_rows"] = dup
        profile["duplicate_pct"] = round(dup / max(row_count, 1) * 100, 2)
    except Exception:
        pass
    for col in columns:
        name = col["name"]
        if name in df.columns:
            profile["columns"][name] = _profile_column(df[name], name, col["dataType"], row_count)
        else:
            profile["columns"][name] = {
                "null_count": 0, "null_pct": 0.0, "distinct_count": 0,
                "unique_pct": 0.0, "outlier_count": 0,
            }
    return profile


def profile_data_file(path: str | os.PathLike[str], sample_rows: int = 1000) -> dict[str, Any]:
    """Read a CSV/Excel file and return a schema + full data-quality profile.

    The returned dict extends the standard schema with a ``quality`` key::

        {
            "schema": {table_name, columns, row_count, ...},
            "quality": {duplicate_rows, duplicate_pct, columns: {name: {...}}},
            "quality_score": 0-100,
            "issues": ["...", ...],
        }

    Falls back to a type-only schema (no quality stats) when pandas is absent.
    """
    p = Path(path).expanduser().resolve()
    suffix = p.suffix.lower()

    # Get the base schema first
    if suffix in {".csv", ".tsv", ".txt"}:
        schema = infer_csv_schema(p)
    elif suffix in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
        schema = infer_excel_schema_compat(p)
    elif suffix == ".json":
        schema = infer_json_schema(p)
    else:
        schema = infer_csv_schema(p)  # best-effort

    result: dict[str, Any] = {"schema": schema, "quality": {}, "quality_score": 100, "issues": []}

    if not _HAS_PANDAS:
        result["issues"].append("pandas unavailable — quality stats skipped")
        return result

    try:
        if suffix in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
            df = _pd.read_excel(p, nrows=sample_rows)
        else:
            df = _pd.read_csv(p, nrows=sample_rows)
        row_count = len(df)
        quality = _profile_table(df, schema["columns"], row_count)
        result["quality"] = quality

        # Compute a simple quality score + collect issues
        score = 100.0
        issues: list[str] = []
        for col in schema["columns"]:
            cp = quality["columns"].get(col["name"], {})
            null_pct = cp.get("null_pct", 0)
            if null_pct > 60:
                score -= 15
                issues.append(f"Column '{col['name']}' has {null_pct}% nulls — consider dropping")
            elif null_pct > 40:
                score -= 8
                issues.append(f"Column '{col['name']}' has {null_pct}% nulls — impute recommended")
            elif null_pct > 20:
                score -= 3
                issues.append(f"Column '{col['name']}' has {null_pct}% nulls")
            outliers = cp.get("outlier_count", 0)
            if outliers > 0 and col["dataType"] in {"int64", "double", "decimal"}:
                issues.append(f"Column '{col['name']}' has {outliers} outlier(s) (IQR method)")
                score -= 2
            if cp.get("unique_pct", 100) < 0.1 and cp.get("distinct_count", 1) <= 1:
                issues.append(f"Column '{col['name']}' has a single value — no analytical value")
                score -= 5
        dup_pct = quality.get("duplicate_pct", 0)
        if dup_pct > 5:
            score -= 10
            issues.append(f"{quality.get('duplicate_rows', 0)} duplicate rows ({dup_pct}%)")
        result["quality_score"] = max(0, round(score, 1))
        result["issues"] = issues
    except Exception as exc:
        result["issues"].append(f"profiling failed: {exc}")
    return result


def _infer_with_pandas(path: Path) -> dict[str, Any]:
    df = _pd.read_csv(path, nrows=1000)  # sample first 1000 rows for speed
    row_count = int(len(_pd.read_csv(path, usecols=[0])) if _HAS_PANDAS else 0)
    columns = []
    for name in df.columns:
        clean_name = str(name).strip()
        dtype = str(df[name].dtype)
        pb_type = _PANDAS_DTYPE_MAP.get(dtype, "string")
        # datetime-looking strings stored as object?
        if pb_type == "string":
            pb_type = _guess_object_type(df[name].dropna().head(50))
        col_dict = {
            "name": clean_name,
            "dataType": pb_type,
            "summarizeBy": _default_summarization(pb_type),
            "sample_values": _sample_values(df[name]),
        }
        # attach per-column quality profile (nulls, distinct, outliers, ...)
        col_dict["profile"] = _profile_column(df[name], clean_name, pb_type, row_count)
        columns.append(col_dict)
    return {
        "table_name": _derive_table_name(path),
        "row_count": row_count,
        "columns": columns,
        "inferred_relationships": [],
        "source_file": str(path),
        "inferer": "pandas",
    }


def _infer_with_stdlib(path: Path) -> dict[str, Any]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"CSV file is empty: {path}") from exc

        sample_rows = []
        for _ in range(200):
            try:
                sample_rows.append(next(reader))
            except StopIteration:
                break

    columns = []
    for idx, name in enumerate(header):
        clean_name = str(name).strip()
        col_values = [r[idx] for r in sample_rows if idx < len(r)]
        pb_type = _guess_string_type(col_values)
        columns.append(
            {
                "name": clean_name,
                "dataType": pb_type,
                "summarizeBy": _default_summarization(pb_type),
                "sample_values": col_values[:5],
            }
        )

    # full row count
    with open(path, newline="", encoding="utf-8-sig") as fh:
        row_count = max(0, sum(1 for _ in fh) - 1)

    return {
        "table_name": _derive_table_name(path),
        "row_count": row_count,
        "columns": columns,
        "inferred_relationships": [],
        "source_file": str(path),
        "inferer": "stdlib",
    }


def _derive_table_name(path: Path) -> str:
    base = path.stem
    # sanitise to a TMDL-safe identifier (letters/digits/underscore)
    safe = "".join(c if c.isalnum() else "_" for c in base)
    safe = safe.strip("_") or "Table"
    # TMDL table names cannot start with a digit
    if safe[0].isdigit():
        safe = "T_" + safe
    return safe


def _default_summarization(pb_type: str) -> str:
    """Pick a sensible default ``summarizeBy`` for a Power BI column."""
    if pb_type in {"int64", "double", "decimal"}:
        return "sum"
    if pb_type == "boolean":
        return "none"
    return "none"


def _guess_object_type(series) -> str:
    """Inspect object-typed pandas series to detect dates/numbers."""
    try:
        import warnings

        import pandas as pd  # type: ignore

        # Guard: pd.to_datetime happily parses bare month names like "December"
        # as dates (Dec 1 of the current year). Require at least one digit in
        # every non-null value before trusting the datetime parse.
        non_null = series.dropna().astype(str)
        if len(non_null) and not all(any(c.isdigit() for c in v) for v in non_null.head(50)):
            return "string"

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(series, errors="coerce")
        if len(series) and parsed.notna().sum() / max(len(series), 1) > 0.8:
            return "dateTime"
    except Exception:
        pass
    return "string"


def _guess_string_type(values: list[str]) -> str:
    """Inspect raw strings to guess a Power BI data type."""
    non_empty = [v for v in values if v not in ("", None)]
    if not non_empty:
        return "string"

    # all ints?
    if all(_is_int(v) for v in non_empty):
        return "int64"
    # all numbers?
    if all(_is_number(v) for v in non_empty):
        return "double"
    # all booleans?
    if all(v.strip().lower() in {"true", "false", "0", "1", "yes", "no"} for v in non_empty):
        return "boolean"
    # all dates (ISO-ish)?
    if all(_looks_like_date(v) for v in non_empty):
        return "dateTime"
    return "string"


def _is_int(v: str) -> bool:
    try:
        f = float(v)
        return f.is_integer()
    except (ValueError, TypeError):
        return False


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


_DATE_TOKENS = ("-", "/")


def _looks_like_date(v: str) -> bool:
    s = str(v).strip()
    if not (8 <= len(s) <= 25):
        return False
    if not any(tok in s for tok in _DATE_TOKENS):
        return False
    try:
        import datetime as dt

        # try a few common formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                dt.datetime.strptime(s[:10], fmt)
                return True
            except ValueError:
                continue
    except Exception:
        pass
    return False


def _sample_values(series) -> list[Any]:
    try:
        return [str(x) for x in series.dropna().head(5).tolist()]
    except Exception:
        return []


def infer_csv_schema(csv_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a CSV and return a schema dict.

    Returns a dict with: ``table_name``, ``row_count``, ``columns`` (each
    having ``name``, ``dataType``, ``summarizeBy``, ``sample_values``),
    ``inferred_relationships``, ``source_file``, ``inferer``.

    Raises:
        FileNotFoundError: if ``csv_path`` does not exist.
        ValueError: if the file is empty / unreadable as CSV.
    """
    path = Path(csv_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")
    if path.suffix.lower() not in {".csv", ".tsv", ".txt"}:
        raise ValueError(f"Expected a CSV file, got: {path.name}")

    if _HAS_PANDAS:
        try:
            return _infer_with_pandas(path)
        except Exception:
            # fall back to stdlib on any pandas error
            pass
    return _infer_with_stdlib(path)


def infer_json_schema(json_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a JSON file describing a schema directly and normalise it.

    Accepted JSON shape::

        {"name": "Sales", "columns": [{"name": "...", "dataType": "..."}, ...]}

    This is the alternate input path (CSV *or* JSON schema, per the spec).
    """
    import json

    path = Path(json_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"JSON schema not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, dict):
        raise ValueError("JSON schema must be an object with 'columns'.")

    cols_in = raw.get("columns", [])
    if not cols_in:
        raise ValueError("JSON schema must contain a non-empty 'columns' list.")

    columns = []
    for c in cols_in:
        if not isinstance(c, dict) or "name" not in c:
            raise ValueError("Each column must be an object with at least 'name'.")
        dtype = str(c.get("dataType", "string")).strip()
        if dtype not in _PB_TYPES:
            raise ValueError(f"Unsupported dataType '{dtype}'. Allowed: {sorted(_PB_TYPES)}")
        columns.append(
            {
                "name": str(c["name"]),
                "dataType": dtype,
                "summarizeBy": c.get("summarizeBy") or _default_summarization(dtype),
                "sample_values": c.get("sample_values", []),
            }
        )

    table_name = str(raw.get("name") or raw.get("table_name") or _derive_table_name(path))
    return {
        "table_name": table_name,
        "row_count": int(raw.get("row_count", 0)),
        "columns": columns,
        "inferred_relationships": raw.get("relationships", []),
        "source_file": str(path),
        "inferer": "json",
    }


def infer_excel_schema_compat(excel_path: str | os.PathLike[str],
                              sheet_name: str | None = None) -> dict[str, Any]:
    """Read an Excel file and return the same schema dict as infer_csv_schema().

    Delegates to utils.excel_reader which supports both openpyxl and pandas.
    The path suffix must be .xlsx, .xls, or .xlsm.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError:        if the suffix is not an Excel extension.
        ImportError:       if neither openpyxl nor pandas is installed.
    """
    from utils.excel_reader import infer_excel_schema

    path = Path(excel_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Excel file not found: {path}")
    if path.suffix.lower() not in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
        raise ValueError(f"Expected an Excel file (.xlsx/.xls/.xlsm), got: {path.name}")

    return infer_excel_schema(path, sheet_name=sheet_name)
