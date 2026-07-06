"""Path-layout helpers for the Power BI Project (.pbip) folder structure.

Power BI Desktop expects a specific directory layout when opening a .pbip
project. This module gives the agents and MCP server one source of truth for
those paths so they never drift apart.

Reference layout (Power BI Desktop, semantic model + report):

    <MyProject>.pbip                    # ★ root entry file (the file you OPEN)
        {
          "version": "1.0",
          "artifacts": [{"report": {"path": "<MyProject>.Report"}}]
        }

    <MyProject>.SemanticModel/
        definition/
            definition.pbism           # semantic-model entry file
            model.tmdl                 # top-level TMDL (model + relationships)
            database.tmdl              # database properties
            tables/
                Sales.tmdl             # one TMDL file per table
            expressions/
                ...
        item.config.json               # ★ format version + logicalId
        item.metadata.json             # ★ type + displayName

    <MyProject>.Report/
        definition.pbir                # ★ report entry file (at REPORT ROOT,
                                       #   NOT inside definition/)
        definition/
            version.json               # ★ PBIR format version (required)
            report.json                # ★ report root (PBIR schema)
            pages/
                pages.json             # ★ page-id -> displayName + ordering
                <page-id>/
                    page.json          # ★ root of each page
                    visuals/
                        <visual-id>/
                            visual.json
        item.config.json               # ★ format version + logicalId
        item.metadata.json             # ★ type + displayName

These helpers are deliberately defensive: they only ever build paths inside a
given root (the MCP tools enforce containment via ``utils.security.safe_join``).
"""

from __future__ import annotations

import uuid
from pathlib import Path


def stable_uuid() -> str:
    """Return a new random UUID4 as a string (used for lineageTags & visuals)."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Semantic-model side
# ---------------------------------------------------------------------------


def sm_root(pbip_root: str | Path, name: str) -> Path:
    return Path(pbip_root) / f"{name}.SemanticModel"


def sm_definition(sm_root_dir: str | Path) -> Path:
    return Path(sm_root_dir) / "definition"


def sm_tables_dir(sm_root_dir: str | Path) -> Path:
    return sm_definition(sm_root_dir) / "tables"


def sm_expressions_dir(sm_root_dir: str | Path) -> Path:
    return sm_definition(sm_root_dir) / "expressions"


def sm_table_file(sm_root_dir: str | Path, table_name: str) -> Path:
    return sm_tables_dir(sm_root_dir) / f"{table_name}.tmdl"


def sm_model_file(sm_root_dir: str | Path) -> Path:
    return sm_definition(sm_root_dir) / "model.tmdl"


def sm_database_file(sm_root_dir: str | Path) -> Path:
    return sm_definition(sm_root_dir) / "database.tmdl"


def sm_item_metadata(sm_root_dir: str | Path) -> Path:
    return Path(sm_root_dir) / "item.metadata.json"


def sm_item_config(sm_root_dir: str | Path) -> Path:
    return Path(sm_root_dir) / "item.config.json"


def sm_definition_pbism(sm_root_dir: str | Path) -> Path:
    """The semantic-model entry file: definition.pbism at the SM ROOT.

    NOTE: Like definition.pbir for Reports, this file sits at the root of the
    .SemanticModel folder (next to .platform), NOT inside definition/.
    Desktop looks for it at <Name>.SemanticModel/definition.pbism.
    """
    return Path(sm_root_dir) / "definition.pbism"


def sm_platform(sm_root_dir: str | Path) -> Path:
    """The ``.platform`` file at the semantic-model-item root."""
    return Path(sm_root_dir) / ".platform"


# ---------------------------------------------------------------------------
# Top-level .pbip entry file (the file Power BI Desktop actually opens)
# ---------------------------------------------------------------------------


def pbip_entry_file(pbip_root: str | Path, name: str) -> Path:
    """Return the path to the root ``<name>.pbip`` entry file.

    This is THE file the user opens in Power BI Desktop. It is a small JSON
    file that points to the report (and optionally the semantic model) folder.
    """
    return Path(pbip_root) / f"{name}.pbip"


# ---------------------------------------------------------------------------
# Report side
# ---------------------------------------------------------------------------


def report_root(pbip_root: str | Path, name: str) -> Path:
    return Path(pbip_root) / f"{name}.Report"


def report_platform(report_root_dir: str | Path) -> Path:
    """The ``.platform`` file at the report-item root (item type + logicalId).

    NOTE: the current PBIP layout uses ``.platform`` (a hidden file), NOT
    ``item.config.json``. Desktop reads this to identify the folder as a Report.
    """
    return Path(report_root_dir) / ".platform"


def report_definition(report_root_dir: str | Path) -> Path:
    return Path(report_root_dir) / "definition"


def report_json_file(report_root_dir: str | Path) -> Path:
    return report_definition(report_root_dir) / "report.json"


def report_definition_pbir(report_root_dir: str | Path) -> Path:
    """The report entry file: definition.pbir (lives at the REPORT ROOT).

    NOTE: This is a common mistake -- definition.pbir is NOT inside the
    definition/ subfolder. It sits next to item.config.json at the root of
    the .Report folder.
    """
    return Path(report_root_dir) / "definition.pbir"


def report_definition_version(report_root_dir: str | Path) -> Path:
    """definition/version.json -- declares the PBIR format version (required)."""
    return report_definition(report_root_dir) / "version.json"


def report_pages_metadata(report_root_dir: str | Path) -> Path:
    """definition/pages/pages.json -- page-id + display-name + ordering."""
    return report_pages_dir(report_root_dir) / "pages.json"


def report_pages_dir(report_root_dir: str | Path) -> Path:
    return report_definition(report_root_dir) / "pages"


def page_dir(report_root_dir: str | Path, page_folder: str) -> Path:
    """Folder for a page, named per PBIR convention ``<DisplayName>.Page``."""
    return report_pages_dir(report_root_dir) / page_folder


def page_json_file(report_root_dir: str | Path, page_folder: str) -> Path:
    """Return the path to ``page.json`` inside a page folder.

    NOTE: The file is literally named ``page.json`` (extension included),
    not ``<page_id>.json`` -- this is a well-documented Power BI quirk
    that trips up a lot of generators.
    """
    return page_dir(report_root_dir, page_folder) / "page.json"


def page_visuals_dir(report_root_dir: str | Path, page_folder: str) -> Path:
    return page_dir(report_root_dir, page_folder) / "visuals"


def visual_json_file(report_root_dir: str | Path, page_folder: str,
                     visual_folder: str) -> Path:
    """``visuals/<VisualName>/visual.json`` for a visual."""
    return page_visuals_dir(report_root_dir, page_folder) / visual_folder / "visual.json"


def report_item_metadata(report_root_dir: str | Path) -> Path:
    return Path(report_root_dir) / "item.metadata.json"


def report_item_config(report_root_dir: str | Path) -> Path:
    return Path(report_root_dir) / "item.config.json"


def report_theme_file(report_root_dir: str | Path) -> Path:
    return report_definition(report_root_dir) / "theme.json"


def detect_flat_vs_nested_layout_mismatch(output_root: str | Path, project_name: str) -> str | None:
    """Guard against a real, observed bug class: some low-level ADK tools
    (``write_pbir_page``, ``write_theme_json``, ``finalize_pages_index``)
    treat ``output_root`` as the DIRECT parent of ``<project_name>.Report``
    (a "flat" layout, e.g. ``output/Foo.Report``) — the convention
    ``create_project_scaffold`` uses when building from scratch. But
    ``generate_pbip``/the orchestrator use a "nested" layout instead
    (``output/Foo/Foo.Report``), and the two conventions are silently
    incompatible: passing the generic output root instead of the actual
    project directory for these tools writes a brand-new, DISCONNECTED
    ``.Report``/``.SemanticModel`` folder next to the real project,
    while the real project silently keeps its stale content — no
    exception, no validation error, just a build that looks like it
    "worked" but never touched the project the user actually meant.

    Returns a human-readable warning message if this exact mismatch is
    detected (a nested project folder exists, but the flat-layout target
    location does not), else ``None``.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        flat_target = root / f"{project_name}.Report"
        nested_project = root / project_name
        nested_target = nested_project / f"{project_name}.Report"
        if not flat_target.exists() and nested_project.is_dir() and nested_target.is_dir():
            return (
                f"'{project_name}.Report' does not exist directly under {root}, but a "
                f"nested project folder was found at {nested_project} (the layout "
                f"generate_pbip/build_report use). This call would create a new, "
                f"DISCONNECTED '{project_name}.Report' folder instead of editing the "
                f"real project. Pass output_root={str(nested_project)!r} (the "
                f"project's own directory) instead of the generic output root."
            )
        return None
    except Exception:  # noqa: BLE001 — advisory check only, never block the caller
        return None
