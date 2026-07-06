"""ReadPBIPAgent -- reads an existing PBIP folder into AgentContext.

Role
----
In edit mode, this agent replaces SchemaAgent. It:

1. Validates that the source path is a valid PBIP folder
   (has at least one *.SemanticModel and *.Report sub-folder).
2. Parses all tables/*.tmdl files to extract the column schema and
   existing measures (so downstream agents can avoid duplicating them).
3. Reads the report pages/ folder to record existing page ids
   (so ReportAgent adds new pages rather than overwriting old ones).
4. Populates ctx.schema, ctx.existing_measures, ctx.existing_page_ids,
   and ctx.project_name from the actual PBIP content.

MCP tools used: none (reads files directly via stdlib).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent
from utils.tmdl_parser import read_semantic_model


class ReadPBIPAgent(BaseAgent):
    """Reads an existing PBIP folder and populates context for edit mode."""

    name = "ReadPBIPAgent"
    description = (
        "You are the ReadPBIPAgent. Read an existing Power BI Project folder, "
        "extract the data model schema (tables, columns, existing measures) and "
        "the existing report structure (pages). Populate the shared context so "
        "downstream agents can add to the project without duplicating content."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        pbip_src = ctx.existing_pbip_path

        if not pbip_src or not pbip_src.is_dir():
            return AgentResult(
                agent=self.name, ok=False,
                message=f"PBIP source folder not found: {pbip_src}",
                errors=[f"Path does not exist or is not a directory: {pbip_src}"],
            )

        # 1) discover project name from *.SemanticModel folder
        sm_dirs = list(pbip_src.glob("*.SemanticModel"))
        rep_dirs = list(pbip_src.glob("*.Report"))

        if not sm_dirs:
            return AgentResult(
                agent=self.name, ok=False,
                message="No *.SemanticModel folder found in the PBIP source.",
                errors=["Missing SemanticModel folder"],
            )

        sm_dir = sm_dirs[0]
        project_name = sm_dir.name.removesuffix(".SemanticModel")
        # Always use the source PBIP's project name so all generated paths
        # (TMDL writes, report pages, validator checks) match the actual folders.
        # The output *directory* is controlled by pbip_root, not project_name.
        ctx.project_name = project_name

        # 2) parse TMDL to extract schema + existing measures
        try:
            model_info = read_semantic_model(sm_dir)
        except Exception as exc:
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Failed to parse TMDL files: {exc}",
                errors=[str(exc)],
            )

        primary = model_info["primary_table"]
        columns = model_info["all_columns"]

        # Build schema in the same format as infer_csv_schema()
        ctx.schema = {
            "table_name": primary,
            "columns": columns,
            "row_count": 0,
            "inferred_relationships": [],
            "source_file": str(sm_dir),
            "inferer": "tmdl",
            "all_tables": model_info["tables"],
        }

        # 3) record existing measure names to avoid duplication
        ctx.existing_measures = list(model_info["measure_names"])

        # 4) read existing page ids from the report
        ctx.existing_page_ids = self._read_existing_pages(rep_dirs)

        # 5) log summary
        n_cols = len(columns)
        n_meas = len(ctx.existing_measures)
        n_pages = len(ctx.existing_page_ids)
        self.log.info(
            f"Read PBIP '{project_name}': {n_cols} columns, "
            f"{n_meas} existing measures, {n_pages} existing pages"
        )

        return AgentResult(
            agent=self.name,
            ok=True,
            message=(
                f"Read PBIP '{project_name}' — "
                f"{n_cols} columns, {n_meas} existing measures, {n_pages} pages."
            ),
            data={
                "project_name": project_name,
                "primary_table": primary,
                "column_count": n_cols,
                "existing_measure_count": n_meas,
                "existing_page_count": n_pages,
            },
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_existing_pages(rep_dirs: list[Path]) -> list[str]:
        """Return page folder names from pages.json (or by scanning the folder)."""
        if not rep_dirs:
            return []
        rep_dir = rep_dirs[0]
        pages_meta = rep_dir / "definition" / "pages" / "pages.json"
        if pages_meta.is_file():
            try:
                data = json.loads(pages_meta.read_text(encoding="utf-8"))
                return list(data.get("pageOrder", []))
            except (json.JSONDecodeError, KeyError):
                pass

        # fallback: scan the pages/ directory
        pages_dir = rep_dir / "definition" / "pages"
        if pages_dir.is_dir():
            return [
                p.name for p in pages_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ]
        return []
