"""ReadPBIXAgent -- extracts schema from a .pbix file.

Role
----
A .pbix file is a ZIP archive. Its internal structure contains a binary
Analysis Services model (DataModel/model.bim or DataModel/schema.ini for
older files). Fully parsing the binary model is complex; instead this agent:

1. Unzips the PBIX into a temp directory.
2. Looks for ``DataModel`` folder and tries to read ``model.bim`` (JSON).
3. If found, extracts table names and columns from the BIM JSON.
4. Falls back to reading the Report/Layout JSON to at least discover tables.
5. If nothing readable is found, returns a clear error advising the user to
   open the file in Power BI Desktop and "Save As" PBIP format.

The extracted schema is written into ctx so downstream agents run normally.

MCP tools used: none.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from agents.base import AgentResult, BaseAgent

# Power BI type codes (BIM) → Power BI TMDL data types
_BIM_TYPE_MAP: dict[int, str] = {
    2: "string",
    6: "int64",
    8: "double",
    9: "decimal",
    11: "boolean",
    13: "dateTime",
}


class ReadPBIXAgent(BaseAgent):
    """Extracts model schema from a .pbix file (ZIP archive)."""

    name = "ReadPBIXAgent"
    description = (
        "You are the ReadPBIXAgent. Extract the data model schema from a Power "
        "BI .pbix file. The file is a ZIP archive; try to read model.bim (JSON) "
        "if available, otherwise parse the report layout to discover tables."
    )

    def _run(self) -> AgentResult:
        ctx = self.context
        pbix_path = ctx.source_path

        if not pbix_path.is_file() or pbix_path.suffix.lower() != ".pbix":
            return AgentResult(
                agent=self.name, ok=False,
                message=f"Not a .pbix file: {pbix_path}",
                errors=[f"Expected .pbix file, got: {pbix_path}"],
            )

        if not zipfile.is_zipfile(pbix_path):
            return AgentResult(
                agent=self.name, ok=False,
                message="File is not a valid ZIP / .pbix archive.",
                errors=["Invalid ZIP structure"],
            )

        tmp_dir = Path(tempfile.mkdtemp(prefix="pbix_extract_"))
        try:
            with zipfile.ZipFile(pbix_path, "r") as zf:
                zf.extractall(tmp_dir)

            # try BIM JSON first
            schema = self._try_bim_json(tmp_dir, pbix_path)
            if schema:
                ctx.schema = schema
                ctx.project_name = ctx.project_name or pbix_path.stem
                n = len(schema["columns"])
                return AgentResult(
                    agent=self.name, ok=True,
                    message=f"Extracted schema from model.bim: {n} columns.",
                    data={"column_count": n, "source": "bim"},
                )

            # fallback: layout JSON
            schema = self._try_layout_json(tmp_dir, pbix_path)
            if schema:
                ctx.schema = schema
                ctx.project_name = ctx.project_name or pbix_path.stem
                n = len(schema["columns"])
                return AgentResult(
                    agent=self.name, ok=True,
                    message=(
                        f"Extracted partial schema from Report/Layout: {n} columns. "
                        "Data types may be approximate."
                    ),
                    data={"column_count": n, "source": "layout"},
                )

            # nothing worked
            return AgentResult(
                agent=self.name, ok=False,
                message=(
                    "Cannot read the data model from this .pbix file. "
                    "The binary model format is not supported directly. "
                    "Please open the file in Power BI Desktop and save it "
                    "as a .pbip project (File > Save as > Power BI Project), "
                    "then use --pbip instead of --pbix."
                ),
                errors=["Binary DataModel not readable; use --pbip after saving from Desktop"],
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # BIM JSON extraction
    # ------------------------------------------------------------------

    def _try_bim_json(self, tmp: Path, src: Path) -> dict[str, Any] | None:
        """Try to read model.bim (JSON) from the extracted PBIX."""
        # model.bim is normally a file; occasionally the DataModel folder
        # itself holds the model JSON. Handle both so neither path is dead.
        bim_file = tmp / "DataModel" / "model.bim"
        bim_dir = tmp / "DataModel"
        candidates: list[Path] = []
        if bim_file.is_file():
            candidates.append(bim_file)
        if bim_dir.is_dir():
            # the folder itself may be JSON (some PBIX variants)
            candidates.append(bim_dir)
        for candidate in candidates:
            try:
                bim = json.loads(candidate.read_text(encoding="utf-8"))
                return self._parse_bim(bim, src)
            except (json.JSONDecodeError, KeyError, IsADirectoryError):
                continue
        return None

    @staticmethod
    def _parse_bim(bim: dict[str, Any], src: Path) -> dict[str, Any] | None:
        """Parse model.bim JSON and return a schema dict.

        Now also extracts per-table partition info (connection type, M source)
        and populates ``all_tables`` so RelationshipAgent + edit tools can see
        every table and its data source (e.g. SQL Server).
        """
        from utils.tmdl_parser import infer_connection_type

        model = bim.get("model", bim)
        tables = model.get("tables", [])
        if not tables:
            return None

        # Build a schema dict for EVERY table (was: only the largest).
        all_tables: list[dict[str, Any]] = []
        for t in tables:
            tname = t.get("name", "Table")
            tcols = []
            for col in t.get("columns", []):
                if col.get("type") == "calculated":
                    continue
                raw_type = col.get("dataType", 2)
                pb_type = _BIM_TYPE_MAP.get(
                    int(raw_type) if isinstance(raw_type, (int, float)) else 2, "string"
                )
                col_name = col.get("name", "Column")
                tcols.append({
                    "name": col_name,
                    "dataType": pb_type,
                    "summarizeBy": "sum" if pb_type in {"int64", "double", "decimal"} else "none",
                    "sourceColumn": col.get("sourceColumn", col_name),
                    "sample_values": [],
                })
            # extract partition M source for connection-type detection
            partition_source = ""
            conn_type = "other"
            partitions = t.get("partitions", [])
            if partitions:
                source = partitions[0].get("source", {})
                # M source can be a dict with "queryGroup"/"steps" or a raw string
                if isinstance(source, str):
                    partition_source = source
                elif isinstance(source, dict):
                    # try to reconstruct a rough M string for inference
                    query = source.get("query", "")
                    partition_source = str(query) if query else str(source)
                conn_type = infer_connection_type(partition_source)
            all_tables.append({
                "table_name": tname,
                "columns": tcols,
                "connection_type": conn_type,
                "partition_source": partition_source,
            })

        # pick the table with most columns as the primary
        def col_count(t: dict) -> int:
            return len(t.get("columns", []))

        primary = max(all_tables, key=col_count)
        table_name = primary["table_name"]
        columns = primary["columns"]

        if not columns:
            return None

        return {
            "table_name": table_name,
            "row_count": 0,
            "columns": columns,
            "inferred_relationships": [],
            "source_file": str(src),
            "inferer": "pbix_bim",
            "all_tables": all_tables,
        }

    # ------------------------------------------------------------------
    # Layout JSON fallback
    # ------------------------------------------------------------------

    def _try_layout_json(self, tmp: Path, src: Path) -> dict[str, Any] | None:
        """Parse Report/Layout to discover field names used in visuals."""
        layout = tmp / "Report" / "Layout"
        if not layout.is_file():
            return None
        try:
            data = json.loads(layout.read_text(encoding="utf-16-le"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                data = json.loads(layout.read_text(encoding="utf-8"))
            except Exception:
                return None

        # collect field references from visual query data
        fields: set[str] = set()
        table_name = "Table"

        def recurse(obj: Any) -> None:
            nonlocal table_name
            if isinstance(obj, dict):
                if "Entity" in obj:
                    table_name = obj["Entity"]
                if "Property" in obj:
                    fields.add(obj["Property"])
                for v in obj.values():
                    recurse(v)
            elif isinstance(obj, list):
                for item in obj:
                    recurse(item)

        recurse(data)

        if not fields:
            return None

        columns = [
            {
                "name": f,
                "dataType": "string",  # unknown from layout
                "summarizeBy": "none",
                "sourceColumn": f,
                "sample_values": [],
            }
            for f in sorted(fields)
        ]

        return {
            "table_name": table_name,
            "row_count": 0,
            "columns": columns,
            "inferred_relationships": [],
            "source_file": str(src),
            "inferer": "pbix_layout",
        }
