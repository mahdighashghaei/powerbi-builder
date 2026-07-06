"""ADK tools for build planning (Planner agent).

Exposes:
  * ``create_build_plan`` — analyse the user's description + data profile and
                             produce an ordered build plan.
  * ``validate_plan``     — check a plan is compatible with available agents/tools.

Phase 2 (fixes P1/P2/P3/P6):
  * P1: ``has_raw_data`` now inferred from profile keys (issues, quality_score, etc.)
  * P2: Result now includes ``needs_cleaning`` and ``report_style``.
  * P3: ``validate_plan`` now checks phase ordering, critical phases, duplicates.
  * P6: ``detect_cycles`` checks for circular step dependencies.
"""
from __future__ import annotations

from typing import Any


# The canonical pipeline phases, in execution order.
_KNOWN_PHASES = [
    "plan", "analyze", "clean", "schema", "relationship",
    "dax", "report", "validate", "review", "deploy",
]

# Phases that MUST appear in every valid plan (fail-safe).
_CRITICAL_PHASES = {"schema", "dax", "report", "validate"}

# Maximum visual count per report style.
_STYLE_MAX_VISUALS = {"minimal": 3, "standard": 6, "rich": 12}


def _infer_has_raw_data(profile: dict[str, Any]) -> bool:
    """Infer whether raw data is available from the profile.

    A DataProfile has ``quality_score``, ``issues``, ``questions`` — NOT
    ``schema``.  We infer the presence of raw data from any of those keys.
    """
    return any(
        key in profile
        for key in ("quality_score", "issues", "questions", "blocking_issues")
    )


def create_build_plan(
    description: str, data_profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Produce an ordered build plan from the description + data profile.

    Returns ``{ok, plan, step_count, needs_cleaning, report_style}``.

    Phase 2 fixes:
      * P1: ``has_raw_data`` is now inferred from profile keys, not ``schema``.
      * P2: Result now includes ``needs_cleaning`` and ``report_style``.
    """
    profile = data_profile or {}
    quality_score = profile.get("quality_score", 100)
    issues = profile.get("issues", [])
    blocking = profile.get("blocking_issues", [])

    plan: list[dict[str, Any]] = []
    needs_cleaning = False

    # P1: Infer raw data presence from profile keys (not a non-existent "schema" key)
    has_raw_data = _infer_has_raw_data(profile)
    if has_raw_data:
        plan.append({
            "phase": "analyze",
            "agent": "DataAnalyzer",
            "action": "Profile data quality (nulls, outliers, duplicates)",
            "rationale": "Understand the data before building a model on it.",
        })
        if quality_score < 90 or issues:
            needs_cleaning = True
            plan.append({
                "phase": "clean",
                "agent": "DataCleaner",
                "action": "Apply cleaning steps based on the profile",
                "rationale": f"Quality score is {quality_score}/100 with {len(issues)} issue(s).",
            })

    # Core build phases
    plan.append({
        "phase": "schema",
        "agent": "SchemaAgent",
        "action": "Infer schema and write TMDL table definitions",
        "rationale": "Create the semantic model structure from the (cleaned) data.",
    })
    plan.append({
        "phase": "relationship",
        "agent": "RelationshipAgent",
        "action": "Detect cross-table relationships (heuristic + LLM)",
        "rationale": "Link fact and dimension tables for a navigable model.",
    })
    plan.append({
        "phase": "dax",
        "agent": "DAXAgent",
        "action": "Generate 5-10 DAX measures with best-practice formatting",
        "rationale": "Add analytical measures (totals, averages, time intelligence).",
    })
    plan.append({
        "phase": "report",
        "agent": "ReportAgent",
        "action": "Build PBIR report pages with visuals",
        "rationale": "Turn the model into a visual dashboard.",
    })
    plan.append({
        "phase": "validate",
        "agent": "ValidatorAgent",
        "action": "Structural + semantic validation, auto-fix trivial issues",
        "rationale": "Ensure Power BI Desktop can open the project.",
    })
    plan.append({
        "phase": "review",
        "agent": "ReportReviewer",
        "action": "Semantic review of the generated report",
        "rationale": "Check visuals reference real measures/columns, layout is sound.",
    })

    # Adjust for blocking issues
    if blocking:
        plan.insert(0, {
            "phase": "plan",
            "agent": "Planner",
            "action": "WARNING: blocking issues detected — user input required",
            "rationale": "; ".join(blocking),
        })

    # P2: Determine report_style from description keywords
    desc_lower = description.lower() if description else ""
    if any(w in desc_lower for w in ("comprehensive", "all visuals", "rich", "detailed")):
        report_style = "rich"
    elif any(w in desc_lower for w in ("simple", "quick", "minimal", "just")):
        report_style = "minimal"
    else:
        report_style = "standard"

    return {
        "ok": True,
        "plan": plan,
        "step_count": len(plan),
        "needs_cleaning": needs_cleaning,
        "report_style": report_style,
    }


def detect_cycles(plan: list[dict[str, Any]]) -> list[list[str]]:
    """Detect circular dependencies in a plan's ``depends_on`` fields.

    Returns a list of cycles (each cycle is a list of phase names).
    Uses DFS-based cycle detection.

    Fix P6: No dependency cycle detection existed before.
    """
    # Build adjacency list from depends_on fields
    graph: dict[str, list[str]] = {}
    for step in plan:
        phase = step.get("phase", "")
        deps = step.get("depends_on", [])
        if isinstance(deps, str):
            deps = [deps]
        graph[phase] = [d for d in deps if isinstance(d, str)]

    cycles: list[list[str]] = []
    visited: set[str] = set()
    stack: set[str] = set()

    def _dfs(node: str, path: list[str]) -> None:
        if node in stack:
            # Found a cycle — extract it
            cycle_start = path.index(node)
            cycles.append(path[cycle_start:] + [node])
            return
        if node in visited:
            return
        visited.add(node)
        stack.add(node)
        for neighbor in graph.get(node, []):
            _dfs(neighbor, path + [node])
        stack.discard(node)

    for node in graph:
        if node not in visited:
            _dfs(node, [])

    return cycles


def validate_plan(plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate that a build plan is well-formed and uses known phases.

    Returns ``{ok, valid, errors, warnings}``.

    Phase 2 fixes (P3):
      * Check phase ordering (phases should appear in _KNOWN_PHASES order).
      * Check critical phases exist (schema, dax, report, validate).
      * Detect duplicate phases.
      * Detect circular dependencies (P6).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not plan:
        return {"ok": True, "valid": False, "errors": ["Plan is empty"], "warnings": []}

    seen_phases: set[str] = set()
    phases_present: list[str] = []
    last_order_idx = -1

    for i, step in enumerate(plan):
        phase = step.get("phase", "")

        # Check known phase
        if phase not in _KNOWN_PHASES:
            errors.append(f"Step {i}: unknown phase '{phase}'")

        # Check for duplicates (plan phase is exempt — can appear once at start)
        if phase in seen_phases and phase != "plan":
            errors.append(f"Step {i}: duplicate phase '{phase}'")
        seen_phases.add(phase)
        phases_present.append(phase)

        # Check ordering: phases should appear in _KNOWN_PHASES order
        if phase in _KNOWN_PHASES:
            order_idx = _KNOWN_PHASES.index(phase)
            if order_idx < last_order_idx:
                warnings.append(
                    f"Step {i}: phase '{phase}' is out of order "
                    f"(expected after '{_KNOWN_PHASES[last_order_idx]}')"
                )
            else:
                last_order_idx = order_idx

        # Check required fields
        if not step.get("agent"):
            errors.append(f"Step {i}: missing 'agent'")
        if not step.get("action"):
            warnings.append(f"Step {i}: missing 'action'")

    # P3: Check critical phases exist
    missing_critical = _CRITICAL_PHASES - seen_phases
    if missing_critical:
        errors.append(
            f"Missing critical phases: {sorted(missing_critical)}"
        )

    # P6: Check for circular dependencies
    cycles = detect_cycles(plan)
    for cycle in cycles:
        errors.append(f"Circular dependency detected: {' → '.join(cycle)}")

    return {
        "ok": True,
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


__all__ = ["create_build_plan", "validate_plan", "detect_cycles"]