"""ValidatorAgent -- validates the generated .pbip and auto-fixes simple issues.

Role
----
Run after all other agents have written their files. It:

1. Calls the MCP tool ``validate_pbip_structure`` for the structural pass
   (required files/folders, required JSON fields on every visual).
2. Performs a deeper semantic pass on the TMDL:
   * every table file starts with ``table <Name>``;
   * every column declares a supported ``dataType``;
   * every measure has ``displayFolder`` + ``formatString`` (the DAX
     best-practice rule from the spec). ``description`` is intentionally not
     checked because it is not valid TMDL syntax.
3. Optionally auto-fixes simple, safe issues:
   * missing ``width``/``height`` on page.json -> defaults (1280x720);
   * visual.json missing ``position`` or ``visual.visualType`` -> patched
     from the sibling folder name / a safe default.

The agent never deletes user content and never rewrites TMDL measures; it only
fills in clearly-missing, well-defined fields.

Phase 4 — agent_responsible routing
------------------------------------
Each issue now carries an ``agent_responsible`` field (which agent should retry
to fix it) and a ``suggested_fix`` so the Phase 4 feedback loop can route the
issue back to the culprit agent instead of just reporting it. The routing is
rule-based on the error text (measure → DAXAgent/MeasureSelectorAgent, visual →
ReportAgent/VisualPlannerAgent, table → SchemaAgent). When the deterministic
agents ran (offline), the legacy agent names are used; the loop still works.

MCP tools used: ``validate_pbip_structure``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from utils.layout_engine import PAGE_W as _DEFAULT_PAGE_W
from utils.layout_engine import PAGE_H as _DEFAULT_PAGE_H
from typing import Any

from agents.base import AgentResult, BaseAgent
from agents.schemas import ValidationIssue, ValidationResult
from utils import atomic_write_json
from utils import pbip_paths as paths


_VALID_TYPES = {"string", "int64", "double", "decimal", "boolean", "dateTime", "date"}
# TMDL measure names use single quotes: measure 'Name' = ...
_MEASURE_RE = re.compile(r"measure\s+'(?P<name>[^']+)'\s*=", re.IGNORECASE)


# Phase 4 — routing rules: keyword → (agent_responsible, suggested_fix).
# Checked in order; first match wins. The agent names are the deterministic
# pipeline agents so the loop can re-run them directly.
_ROUTING_RULES: list[tuple[str, str, str, str]] = [
    # (keyword_in_error, agent_responsible, severity, suggested_fix)
    # Order matters: more-specific keywords first so "ghost" (a visual issue)
    # is matched before "measure" (which appears in ghost-ref messages too).
    ("ghost", "ReportAgent", "error",
     "Re-plan the visual so it binds to a measure/column that exists in the model."),
    ("visual", "ReportAgent", "error",
     "Re-plan the visual binding or visual type."),
    ("measure", "DAXAgent", "error",
     "Re-author the measure so its DAX expression references only columns that exist in the table."),
    ("datatype", "SchemaAgent", "error",
     "Re-infer the column data type to one of the supported TMDL types."),
    ("table ", "SchemaAgent", "error",
     "Re-write the TMDL table definition starting with 'table <Name>'."),
    ("partition", "SchemaAgent", "error",
     "Re-write the table TMDL with a valid M partition."),
    ("report", "ReportAgent", "warning",
     "Check the report structure / pages.json index."),
    ("semantic model", "SchemaAgent", "error",
     "Re-create the semantic model scaffold."),
]


def _route_issue(message: str) -> tuple[str, str, str]:
    """Return (agent_responsible, severity, suggested_fix) for an error message.

    Falls back to ("", "error", "") when no rule matches — the feedback loop
    treats an empty agent_responsible as non-routable (no retry, just report).
    """
    low = message.lower()
    for keyword, agent, severity, fix in _ROUTING_RULES:
        if keyword in low:
            return agent, severity, fix
    return "", "error", ""


class ValidatorAgent(BaseAgent):
    """Validates TMDL + PBIR output and auto-fixes trivial issues."""

    name = "ValidatorAgent"
    description = (
        "You are the ValidatorAgent. Verify the generated .pbip project is "
        "structurally and semantically valid for Power BI Desktop: required "
        "files exist, TMDL tables/columns/measures are well-formed, every "
        "visual has the required fields. Auto-fix simple, safe issues (missing "
        "page dimensions, missing visualType). Report everything you find with "
        "the agent responsible for each issue so the feedback loop can retry."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        pbip_root = Path(ctx.pbip_root)

        # 1) structural validation via MCP tool
        structural = ctx.toolbox.validate_pbip_structure(str(pbip_root))

        # 2) deeper semantic pass on TMDL + visual metadata
        errors: list[str] = []
        warnings: list[str] = []
        fixes_applied: list[str] = []

        self._validate_tmdl(ctx, errors, warnings)
        self._validate_and_fix_visuals(ctx, errors, fixes_applied)

        # merge structural errors with semantic ones (dedupe)
        all_errors = _dedupe(structural.errors + errors)
        all_warnings = _dedupe(structural.data.get("warnings", []) + warnings)

        # Phase 4 — build routed issues with agent_responsible + suggested_fix
        issues: list[ValidationIssue] = []
        for msg in all_errors:
            agent, sev, fix = _route_issue(msg)
            issues.append(ValidationIssue(
                severity=sev, message=msg,
                agent_responsible=agent, suggested_fix=fix,
            ))
        for msg in all_warnings:
            agent, _sev, fix = _route_issue(msg)
            issues.append(ValidationIssue(
                severity="warning", message=msg,
                agent_responsible=agent, suggested_fix=fix,
            ))

        ok = not all_errors

        ctx.validation = {
            "ok": ok,
            "tables": structural.data.get("tables", 0),
            "measures": structural.data.get("measures", 0),
            "pages": structural.data.get("pages", 0),
            "visuals": structural.data.get("visuals", 0),
            "errors": all_errors,
            "warnings": all_warnings,
            "fixes_applied": fixes_applied,
            # Phase 4 — routed issues for the feedback loop
            "issues": [i.model_dump() for i in issues],
        }

        self.log.info(
            f"validation ok={ok} errors={len(all_errors)} "
            f"warnings={len(all_warnings)} fixes={len(fixes_applied)} "
            f"routable={sum(1 for i in issues if i.agent_responsible)}"
        )
        if fixes_applied:
            for f in fixes_applied:
                self.log.info(f"  autofix: {f}")

        return AgentResult(
            agent=self.name,
            ok=ok,
            message=(
                "Validation passed." if ok
                else f"Validation found {len(all_errors)} error(s)."
            ),
            data=ctx.validation,
            errors=all_errors,
        )

    # ------------------------------------------------------------------
    # TMDL semantic checks
    # ------------------------------------------------------------------

    def _validate_tmdl(self, ctx, errors: list[str], warnings: list[str]) -> None:
        sm_dir = paths.sm_root(ctx.pbip_root, ctx.project_name)
        tables_dir = paths.sm_tables_dir(sm_dir)
        if not tables_dir.is_dir():
            errors.append("Semantic model definition/tables/ missing.")
            return

        for tmdl in tables_dir.glob("*.tmdl"):
            txt = tmdl.read_text(encoding="utf-8")
            lines = [ln for ln in txt.splitlines() if ln.strip()]
            if not lines:
                errors.append(f"{tmdl.name}: empty TMDL file.")
                continue
            if not lines[0].strip().lower().startswith("table "):
                errors.append(f"{tmdl.name}: first line must declare 'table <Name>'.")

            # column dataType check (cheap parse: look for dataType: <x>)
            for i, ln in enumerate(lines):
                m = re.match(r"\s*dataType:\s*(\S+)", ln)
                if m and m.group(1) not in _VALID_TYPES:
                    errors.append(
                        f"{tmdl.name}: line {i+1} unsupported dataType '{m.group(1)}'."
                    )

            # DAX bracket syntax check: 'measure [Name]' is TMDL syntax error
            if "measure [" in txt:
                errors.append(
                    f"{tmdl.name}: measure uses DAX bracket notation 'measure [Name]' "
                    "-- TMDL requires single quotes: measure 'Name'."
                )
            # measure best-practice check: every measure block should have the
            # three required properties
            self._check_measure_block(tmdl.name, txt, warnings)

    def _check_measure_block(self, fname: str, text: str, warnings: list[str]) -> None:
        """Warn (not error) when a measure misses a best-practice property."""
        # split into measure blocks: each starts with a "measure 'Name' =" line
        blocks = re.split(r"(?=^\s*measure\s+')", text, flags=re.MULTILINE)
        for block in blocks:
            # only consider blocks that actually start with a measure declaration
            # (the split also produces a leading preamble block before the first
            # measure). The correct TMDL syntax is `measure 'Name'`; the legacy
            # `measure [Name]` bracket form is already flagged as an error above.
            if not re.match(r"\s*measure\s+'", block):
                continue
            mm = _MEASURE_RE.search(block)
            name = mm.group("name") if mm else "<unknown>"
            # 'description' is NOT valid TMDL; only check displayFolder + formatString
            missing = [
                prop for prop in ("displayFolder", "formatString")
                if not re.search(rf"^\s*{prop}\s*:", block, re.MULTILINE)
            ]
            if missing:
                warnings.append(
                    f"{fname}: measure [{name}] missing best-practice "
                    f"propert{'y' if len(missing) == 1 else 'ies'}: {', '.join(missing)}."
                )

    # ------------------------------------------------------------------
    # PBIR visual checks + auto-fix
    # ------------------------------------------------------------------

    def _validate_and_fix_visuals(
        self, ctx, errors: list[str], fixes: list[str]
    ) -> None:
        rep_dir = paths.report_root(ctx.pbip_root, ctx.project_name)
        pages_dir = paths.report_pages_dir(rep_dir)
        if not pages_dir.is_dir():
            return

        for page_folder in pages_dir.iterdir():
            if not page_folder.is_dir():
                continue
            # page.json width/height autofix
            pjson = page_folder / "page.json"
            if pjson.is_file():
                self._maybe_fix_page_json(pjson, fixes)

            visuals_dir = page_folder / "visuals"
            if not visuals_dir.is_dir():
                continue
            for vdir in visuals_dir.iterdir():
                if not vdir.is_dir():
                    continue
                vjson = vdir / "visual.json"
                if not vjson.is_file():
                    errors.append(f"{vdir.name}: missing visual.json")
                    continue
                self._maybe_fix_visual_json(vjson, vdir.name, errors, fixes)

    def _maybe_fix_page_json(self, pjson: Path, fixes: list[str]) -> None:
        try:
            data = json.loads(pjson.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # leave JSON errors for the structural validator to report
            return
        changed = False
        if "width" not in data:
            data["width"] = _DEFAULT_PAGE_W
            changed = True
        if "height" not in data:
            data["height"] = _DEFAULT_PAGE_H
            changed = True
        if "displayName" not in data:
            data["displayName"] = pjson.parent.name
            changed = True
        if changed:
            atomic_write_json(pjson, data)
            fixes.append(f"{pjson.parent.name}/page.json: filled missing dimensions/displayName")

    def _maybe_fix_visual_json(
        self, vjson: Path, vname: str, errors: list[str], fixes: list[str]
    ) -> None:
        try:
            data = json.loads(vjson.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{vname}: visual.json invalid JSON: {exc}")
            return

        changed = False
        # position block
        pos = data.get("position")
        if not isinstance(pos, dict):
            data["position"] = {"x": 0, "y": 0, "width": 300, "height": 200}
            changed = True
        else:
            for field, default in (("x", 0), ("y", 0), ("width", 300), ("height", 200)):
                if field not in pos:
                    pos[field] = default
                    changed = True
            data["position"] = pos

        # visual block + visualType
        visual = data.get("visual")
        if not isinstance(visual, dict):
            data["visual"] = {"visualType": "card", "query": {"queryState": {}}, "title": {"show": True}}
            changed = True
        else:
            if "visualType" not in visual:
                # cannot safely guess a real chart type -> fall back to card
                visual["visualType"] = "card"
                changed = True
            if "query" not in visual:
                visual["query"] = {"queryState": {}}
                changed = True
            data["visual"] = visual

        if "name" not in data:
            data["name"] = vname
            changed = True

        if changed:
            atomic_write_json(vjson, data)
            fixes.append(f"{vname}/visual.json: filled missing required fields")


def _dedupe(items: list[str]) -> list[str]:
    """Return items deduped while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out
