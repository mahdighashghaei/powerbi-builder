"""ADK tools for data cleaning (Data Cleaner agent).

Exposes:
  * ``plan_cleaning``  — turn a data profile + user answers into a concrete
                        list of cleaning steps.
  * ``apply_cleaning`` — execute the plan against the source file with pandas
                        and write a cleaned CSV, returning before/after stats.
  * ``verify_cleaning`` — compare before/after profiles and report the delta.

The Cleaner runs AFTER the Analyzer and BEFORE schema generation, so downstream
agents build on cleaned data. All cleaning is non-destructive: the original
file is never modified; a cleaned copy is written under ``output_root/_clean/``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def plan_cleaning(data_profile: dict[str, Any], answers: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a cleaning plan from a data profile + user answers.

    Each step is ``{column, action, params}`` where action is one of:
    drop_column, impute, dedupe, fix_type, trim_whitespace, remove_outliers, cap_outliers.

    Returns ``{ok, plan, step_count}``.
    """
    answers = answers or data_profile.get("answers", {})
    quality = data_profile.get("quality", {})
    cols = quality.get("columns", {})
    schema = data_profile.get("schema", {})
    issues = data_profile.get("issues", [])
    plan: list[dict[str, Any]] = []

    for col in schema.get("columns", []):
        name = col["name"]
        cp = cols.get(name, {})
        null_pct = cp.get("null_pct", 0)

        # null handling — driven by user answer or best-effort default
        ans = answers.get(f"nulls_{name}", "")
        if null_pct > 60 or ans == "drop":
            plan.append({"column": name, "action": "drop_column", "params": {}})
        elif ans == "leave_as_is":
            pass  # explicit override (e.g. from the semantic-assist layer): skip auto-impute
        elif null_pct > 0 and (ans.startswith("impute") or null_pct > 5):
            method = ans.replace("impute_", "") if ans.startswith("impute") else (
                "median" if col["dataType"] in {"int64", "double", "decimal"} else "mode"
            )
            plan.append({"column": name, "action": "impute", "params": {"method": method}})

        # single-value column
        if cp.get("distinct_count", 0) <= 1 and null_pct < 100:
            if answers.get(f"single_{name}", "drop") == "drop":
                plan.append({"column": name, "action": "drop_column", "params": {"reason": "single_value"}})

        # outliers
        outliers = cp.get("outlier_count", 0)
        if outliers > 0 and col["dataType"] in {"int64", "double", "decimal"}:
            oans = answers.get(f"outliers_{name}", "cap")
            if oans == "remove":
                plan.append({"column": name, "action": "remove_outliers", "params": {}})
            elif oans == "cap":
                plan.append({"column": name, "action": "cap_outliers", "params": {}})

        # whitespace for string columns
        if col["dataType"] == "string" and null_pct < 100:
            plan.append({"column": name, "action": "trim_whitespace", "params": {}})

    # table-level: dedupe
    dup_rows = quality.get("duplicate_rows", 0)
    if dup_rows > 0:
        plan.append({"column": "*", "action": "dedupe", "params": {}})

    return {"ok": True, "plan": plan, "step_count": len(plan)}


def apply_cleaning(source: str, plan: list[dict[str, Any]],
                   output_root: str = "./output") -> dict[str, Any]:
    """Execute a cleaning plan and write a cleaned CSV.

    Returns ``{ok, cleaned_path, before_profile, after_profile, actions_applied}``.
    The original file is never modified.
    """
    try:
        import pandas as pd
    except ImportError:
        return {"ok": False, "errors": ["pandas is required for cleaning"]}

    from mcp_server.schema_inference import profile_data_file

    src = Path(source).expanduser().resolve()
    if not src.is_file():
        return {"ok": False, "errors": [f"Source not found: {source}"]}

    # before profile
    before = profile_data_file(src)

    try:
        df = pd.read_csv(src)
    except Exception:
        try:
            df = pd.read_excel(src)
        except Exception as exc:
            return {"ok": False, "errors": [f"Could not read source: {exc}"]}

    applied: list[str] = []
    drop_cols: list[str] = []

    for step in plan:
        col = step["column"]
        action = step["action"]
        params = step.get("params", {})

        if action == "drop_column" and col != "*" and col in df.columns:
            drop_cols.append(col)
            applied.append(f"dropped column '{col}'")
        elif action == "impute" and col in df.columns:
            method = params.get("method", "median")
            if method == "median":
                df[col] = df[col].fillna(df[col].median())
            elif method == "mean":
                df[col] = df[col].fillna(df[col].mean())
            elif method == "mode":
                mode_val = df[col].mode()
                df[col] = df[col].fillna(mode_val[0] if not mode_val.empty else None)
            else:
                df[col] = df[col].fillna(params.get("constant", 0))
            applied.append(f"imputed '{col}' with {method}")
        elif action == "trim_whitespace" and col in df.columns:
            df[col] = df[col].astype(str).str.strip()
            applied.append(f"trimmed whitespace on '{col}'")
        elif action == "cap_outliers" and col in df.columns:
            q1 = df[col].quantile(0.25)
            q3 = df[col].quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                df[col] = df[col].clip(lower, upper)
                applied.append(f"capped outliers on '{col}'")
        elif action == "remove_outliers" and col in df.columns:
            q1 = df[col].quantile(0.25)
            q3 = df[col].quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                before_rows = len(df)
                df = df[(df[col] >= lower) & (df[col] <= upper)]
                applied.append(f"removed {before_rows - len(df)} outlier rows from '{col}'")
        elif action == "dedupe":
            before_rows = len(df)
            df = df.drop_duplicates()
            applied.append(f"removed {before_rows - len(df)} duplicate rows")

    # drop columns last (so other steps can still reference them)
    for dc in drop_cols:
        if dc in df.columns:
            df = df.drop(columns=[dc])

    # write cleaned file
    clean_dir = Path(output_root).expanduser().resolve() / "_clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    cleaned_path = clean_dir / f"{src.stem}_clean.csv"
    df.to_csv(cleaned_path, index=False)

    after = profile_data_file(cleaned_path)
    return {
        "ok": True,
        "cleaned_path": str(cleaned_path),
        "before_score": before.get("quality_score", 100),
        "after_score": after.get("quality_score", 100),
        "before_issues": len(before.get("issues", [])),
        "after_issues": len(after.get("issues", [])),
        "actions_applied": applied,
        "action_count": len(applied),
        "rows_before": before.get("schema", {}).get("row_count", 0),
        "rows_after": after.get("schema", {}).get("row_count", 0),
    }


def verify_cleaning(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare before/after profiles and report whether cleaning helped.

    Returns ``{ok, improved, deltas: {quality_score, issue_count, duplicate_rows}}``.
    """
    deltas: dict[str, Any] = {}
    bs = before.get("quality_score", 100)
    a_s = after.get("quality_score", 100)
    deltas["quality_score"] = round(a_s - bs, 1)
    deltas["issue_count"] = len(after.get("issues", [])) - len(before.get("issues", []))
    bq = before.get("quality", {})
    aq = after.get("quality", {})
    deltas["duplicate_rows"] = aq.get("duplicate_rows", 0) - bq.get("duplicate_rows", 0)
    improved = deltas["quality_score"] > 0 or deltas["issue_count"] < 0 or deltas["duplicate_rows"] < 0
    return {"ok": True, "improved": improved, "deltas": deltas}


__all__ = ["plan_cleaning", "apply_cleaning", "verify_cleaning"]
