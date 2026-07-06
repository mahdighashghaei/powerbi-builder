"""PowerBI Builder MCP Server.

Exposes a suite of tools over the Model Context Protocol that the agents (or
any MCP client) use to read schemas, write TMDL/PBIR files, write themes,
and validate the resulting .pbip project. See the ``@mcp.tool()`` registrations
in :func:`_build_mcp_server` for the current tool list.

Security
--------
* Every write is contained inside an *allowed root* via
  :func:`utils.security.safe_join` (path-traversal defence, depth-2).
* Every read is confined to an allowed read-root set
  (:meth:`PbipToolbox._readable`): ``..`` traversal and symlink escapes are
  always rejected. PBIP project reads/validation additionally require the
  path to lie inside an allowed root (strict containment); schema reads of
  user data files may read from anywhere but log a warning when outside the
  root set. Extra read roots can be added via ``POWERBI_ALLOWED_READ_ROOTS``.
* Under the stdio transport, logging is routed to ``stderr`` (never
  ``stdout``, which is the JSON-RPC stream).
* JSON is validated before writing.
* Every tool call is audit-logged.

Run as an MCP server (stdio)::

    python -m mcp_server.server

Or, in-process, the :class:`PbipToolbox` gives the orchestrator direct access
to the same tool implementations without an MCP round-trip -- which is how the
project is wired by default (single-command deployability). The stdio MCP entry
point is provided so the server can also be consumed by external agents.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2.exceptions import TemplateError

from . import pbir_generator as _pb
from utils import (
    AuditLogger,
    JSONValidationError,
    PathSecurityError,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    safe_join,
    serialize_json,
    stable_uuid,
    utc_now_iso,
)
from utils import pbip_paths as paths
from utils.identifiers import escape_tmdl_string, quote_tmdl_identifier
from utils.tmdl_parser import read_semantic_model
from .schema_inference import infer_csv_schema, infer_json_schema, infer_excel_schema_compat

log = AuditLogger.get("mcp_server")

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
# Register identifier-escaping filters so the TMDL template can quote column /
# measure names safely. Single-quote doubling is the DAX/TMDL escape rule; using
# it here closes an identifier-injection gap (a column named ``A'mount`` must
# render as ``column 'A''mount'``, not ``column 'A'mount'``).
_jinja.filters["q"] = quote_tmdl_identifier          # 'Name' -> '' doubled, wrapped in quotes
_jinja.filters["esc"] = escape_tmdl_string           # bare value escaping (for sourceColumn)

# Regex matching a TMDL measure block: the "measure 'Name' = ..." header line
# plus its indented property lines (formatString/displayFolder/dataCategory)
# and continuation lines, up to the next top-level tab keyword or blank line.
# Group "name" captures the single-quoted measure name (with escaped quotes
# un-escaped so it matches the input name).
# The name group matches the TMDL single-quote-doubling escape rule: a ``'``
# inside a quoted name is written as ``''``, so we consume ``''`` as one unit
# and any non-quote char otherwise. This replaced an earlier backslash-escape
# pattern (``[^'\\]|\\.``) that did not match the doubled-quote rule and left
# quoted names un-deduped.
_MEASURE_BLOCK_RE = re.compile(
    r"\n\tmeasure\s+'(?P<name>(?:''|[^'])*)'\s*="
    r"(?:.*?(?=\n\t(?:measure|annotation|partition)\s|\Z))",
    re.DOTALL | re.IGNORECASE,
)


def _strip_existing_measures(tmdl_text: str, names: set[str]) -> str:
    """Remove existing measure blocks whose name is in ``names`` from TMDL text.

    Used by :meth:`PbipToolbox.write_tmdl_measures` to dedup before inserting,
    so re-running the DAX agent (Phase 4 feedback loop) or calling
    add_measure/write_tmdl_measures twice from ``adk web`` does NOT produce a
    duplicate measure block — which Power BI Desktop rejects with
    ``PFE_TM_OBJECT_NAME_ALREADY_EXISTS``.

    Matching is case-INSENSITIVE (Power BI Desktop itself treats measure names
    case-insensitively, so "Min Age" and "Min age" collide and must be deduped
    together). ``names`` should hold lowercased keys; names are un-escaped
    (``''`` -> ``'``) and lowercased before the membership test.
    """
    if not names:
        return tmdl_text

    # Normalise the caller's names to lowercase once for O(1) lookup.
    names_lower = {n.lower() for n in names}

    def _unesc(s: str) -> str:
        # TMDL/DAX escape rule: '' (doubled single quote) -> ' .
        return s.replace("''", "'")

    def _remove_if_named(m: re.Match) -> str:
        if _unesc(m.group("name")).lower() in names_lower:
            return ""  # drop the whole block
        return m.group(0)

    return _MEASURE_BLOCK_RE.sub(_remove_if_named, tmdl_text)

# ---------------------------------------------------------------------------
# Tool result contract
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Normalised result returned by every tool.

    Using a structured result (instead of bare strings) keeps the contract
    identical whether the tools run in-process or over MCP.
    """

    ok: bool
    tool: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "message": self.message,
            "data": self.data,
            "errors": self.errors,
            "timestamp": utc_now_iso(),
        }


# ---------------------------------------------------------------------------
# Toolbox -- the actual tool implementations (transport-agnostic)
# ---------------------------------------------------------------------------


class PbipToolbox:
    """In-process implementation of the MCP tools.

    ``allowed_root`` is the only directory tools may write into. It is set by
    the orchestrator to the run's ``.pbip`` output folder, and every write is
    funneled through :func:`utils.security.safe_join`. Reads are confined to an
    allowed read-root set (see :meth:`_readable`).
    """

    def __init__(self, allowed_root: str | os.PathLike[str]) -> None:
        self.root = Path(allowed_root).expanduser().resolve()
        ensure_dir(self.root)
        self._read_roots_cache: list[Path] | None = None
        log.info(f"Toolbox bound to allowed_root={self.root}")

    # ---- helper -----------------------------------------------------------

    def _writable(self, *parts: str) -> Path:
        """Resolve ``parts`` under the allowed root, refusing escapes."""
        return safe_join(self.root, *parts)

    def _read_roots(self) -> list[Path]:
        """Allowed roots for read containment.

        Built from (a) the toolbox write root, (b) the process working
        directory, and (c) any extra comma-separated roots supplied via the
        ``POWERBI_ALLOWED_READ_ROOTS`` env var. Defaults keep demo/test
        workflows (which read ``SampleData.csv`` from the project root) working
        while still bounding reads to a configurable set.
        """
        if self._read_roots_cache is not None:
            return self._read_roots_cache
        roots: list[Path] = [self.root, Path.cwd().resolve()]
        extra = os.getenv("POWERBI_ALLOWED_READ_ROOTS", "")
        for part in extra.split(","):
            part = part.strip()
            if part:
                roots.append(Path(part).expanduser().resolve())
        self._read_roots_cache = roots
        return roots

    @staticmethod
    def _is_inside(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _readable(self, path: str | os.PathLike[str], *,
                  strict: bool = False) -> Path:
        """Resolve ``path`` and confine reads to an allowed root set.

        Path traversal (``..``) is always rejected -- it is never legitimate
        for a tool argument. Symlinks are collapsed by ``resolve()`` so an
        escaped target is caught by the containment check.

        Args:
            strict: When True the path MUST lie inside an allowed root
                (used for PBIP project reads/validation, which operate on
                generated projects). When False a path outside the roots is
                allowed but logged as a warning -- ``read_csv_schema`` may
                legitimately read a user's data file from anywhere.

        Raises:
            PathSecurityError: on ``..`` traversal, or when ``strict`` and the
                path is outside every allowed root.
        """
        raw = str(path)
        norm = raw.replace("\\", "/")
        if ".." in norm.split("/"):
            raise PathSecurityError(
                f"Path traversal ('..') is not allowed in read path: {raw!r}"
            )
        p = Path(raw).expanduser().resolve()
        roots = self._read_roots()
        if not any(self._is_inside(p, r) for r in roots):
            if strict:
                raise PathSecurityError(
                    f"Refusing to read path outside allowed roots.\n"
                    f"  allowed={roots}\n  target={p}"
                )
            log.warning(
                f"[read] path outside allowed roots (allowed={roots}): {p}"
            )
        return p

    # ---- tool 1: read_csv_schema -----------------------------------------

    def read_csv_schema(self, csv_path: str,
                        sheet_name: str | None = None) -> ToolResult:
        """Read a CSV, Excel, or JSON schema file and infer its Power BI schema."""
        try:
            p = self._readable(csv_path)  # confined to allowed read roots
            if not p.is_file():
                from mcp_server.highlevel import _file_not_found_msg
                return ToolResult(False, "read_csv_schema",
                                  _file_not_found_msg(csv_path, "File"))
            if p.suffix.lower() == ".json":
                schema = infer_json_schema(p)
            elif p.suffix.lower() in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
                schema = infer_excel_schema_compat(p, sheet_name=sheet_name)
            else:
                schema = infer_csv_schema(p)
            log.info(f"[read_csv_schema] inferred {len(schema['columns'])} columns "
                     f"for table '{schema['table_name']}'")
            return ToolResult(
                True,
                "read_csv_schema",
                f"Inferred schema for '{schema['table_name']}' "
                f"({len(schema['columns'])} columns).",
                data={"schema": schema},
            )
        except Exception as exc:
            log.exception("[read_csv_schema] failed")
            return ToolResult(False, "read_csv_schema", str(exc), errors=[str(exc)])

    def read_pbip_schema(self, pbip_dir: str) -> ToolResult:
        """Read an existing PBIP folder and return its semantic model schema.

        Parses all tables/*.tmdl files and returns a schema dict in the same
        format as read_csv_schema(), so it can feed the same downstream pipeline.
        Also returns existing measure names, full measure details (with DAX
        expressions so the model can reason about editing them), per-table
        connection info, and the report's pages with per-page visual counts.
        """
        try:
            import json as _json

            p = self._readable(pbip_dir, strict=True)  # PBIP reads stay contained
            sm_dirs = list(p.glob("*.SemanticModel"))
            if not sm_dirs:
                return ToolResult(False, "read_pbip_schema",
                                  f"No *.SemanticModel folder found in: {p}")
            sm_dir = sm_dirs[0]
            model = read_semantic_model(sm_dir)
            primary = model["primary_table"]
            schema = {
                "table_name": primary,
                "columns": model["all_columns"],
                "row_count": 0,
                "inferred_relationships": [],
                "source_file": str(sm_dir),
                "inferer": "tmdl",
            }
            # Full measure details (name + expression + table + format + folder)
            # so the model can decide how to edit an existing measure, not just
            # know that it exists. The name-only list is kept as existing_measures.
            measures_detail = [
                {
                    "name": m["name"],
                    "expression": m.get("expression", ""),
                    "table": m.get("table", ""),
                    "formatString": m.get("formatString", ""),
                    "displayFolder": m.get("displayFolder", ""),
                }
                for m in model.get("all_measures", [])
            ]
            # Enumerate report pages with visual counts so the model knows the
            # current report layout before editing (paired with list_pages /
            # describe_page for deeper detail).
            pages_info: list[dict[str, Any]] = []
            rep_dirs = list(p.glob("*.Report"))
            for rep in rep_dirs:
                pages_dir = rep / "definition" / "pages"
                if not pages_dir.is_dir():
                    continue
                for page in sorted(pages_dir.iterdir(), key=lambda x: x.name):
                    if not page.is_dir():
                        continue
                    vdir = page / "visuals"
                    vcount = sum(1 for v in vdir.iterdir() if v.is_dir()) if vdir.is_dir() else 0
                    pages_info.append({"page_id": page.name, "visual_count": vcount})
            log.info(f"[read_pbip_schema] read {len(schema['columns'])} columns "
                     f"from '{primary}', {len(measures_detail)} measures, "
                     f"{len(pages_info)} pages")
            # Surface per-table connection info (source type + M partition) so
            # edit tools can rewrite data sources (e.g. CSV → SQL Server).
            tables_info = [
                {
                    "table_name": t["table_name"],
                    "connection_type": t.get("connection_type", "other"),
                    "partition_source": t.get("partition_source", ""),
                }
                for t in model["tables"]
            ]
            return ToolResult(
                True, "read_pbip_schema",
                f"Read PBIP schema: '{primary}' "
                f"({len(schema['columns'])} columns, "
                f"{len(measures_detail)} existing measures, "
                f"{len(pages_info)} pages).",
                data={
                    "schema": schema,
                    "existing_measures": list(model["measure_names"]),
                    "measures": measures_detail,
                    "tables": tables_info,
                    "pages": pages_info,
                },
            )
        except Exception as exc:
            log.exception("[read_pbip_schema] failed")
            return ToolResult(False, "read_pbip_schema", str(exc), errors=[str(exc)])

    # ---- tool 2: write_tmdl_table ----------------------------------------

    def write_tmdl_table(self, output_dir: str, table_def: dict[str, Any]) -> ToolResult:
        """Render and write a TMDL table file.

        ``table_def`` shape::

            {"name": "Sales", "lineageTag": "<uuid>",
             "source_path": "/abs/path/to/file.csv",   # optional: generates M partition
             "source_type": "csv",                      # optional (default: csv)
             "columns": [{"name","dataType","summarizeBy","sourceColumn"}],
             "measures": [] }

        Every table in Power BI must have at least one partition. When
        ``source_path`` is supplied (CSV), an M import partition is appended
        automatically. Without it, Desktop will reject the project with
        PFE_TM_TABLE_NO_PARTITIONS.
        """
        try:
            self._require_fields(table_def, ["name", "columns"], "table_def")
            self._validate_columns(table_def["columns"])

            table_def = dict(table_def)
            table_def.setdefault("lineageTag", stable_uuid())

            # ensure every column has a lineageTag + summarizeBy + sourceColumn
            normalised_cols = []
            for col in table_def["columns"]:
                col = dict(col)
                col.setdefault("lineageTag", stable_uuid())
                col.setdefault("summarizeBy", _default_summarization(col["dataType"]))
                col.setdefault("sourceColumn", col["name"])
                normalised_cols.append(col)
            table_def["columns"] = normalised_cols

            template = _jinja.get_template("tmdl_table.j2")
            rendered = template.render(table=table_def)

            # Append a partition block so Desktop can load the table.
            # Every import table must have ≥1 partition (PFE_TM_TABLE_NO_PARTITIONS).
            source_path = table_def.get("source_path")
            source_type = table_def.get("source_type", "csv")
            connection_params = table_def.get("connection_params")
            if source_path or source_type in ("sql",):
                rendered += _build_m_partition(
                    table_def["name"], str(source_path or ""),
                    normalised_cols, source_type=source_type,
                    connection_params=connection_params,
                )

            table_name = table_def["name"]
            target = self._writable(output_dir, "tables", f"{table_name}.tmdl")
            atomic_write_text(target, rendered)

            log.info(f"[write_tmdl_table] wrote {target}")
            return ToolResult(
                True,
                "write_tmdl_table",
                f"Wrote TMDL table '{table_name}' ({len(normalised_cols)} columns).",
                data={"path": str(target), "table": table_name},
            )
        except (PathSecurityError, JSONValidationError, ValueError, KeyError, TemplateError) as exc:
            log.exception("[write_tmdl_table] failed")
            return ToolResult(False, "write_tmdl_table", str(exc), errors=[str(exc)])

    # ---- tool 3: write_tmdl_measures -------------------------------------

    def write_tmdl_measures(self, output_dir: str, measures: list[dict[str, Any]]) -> ToolResult:
        """Append DAX measures to an existing table's TMDL file.

        ``measures`` entries are re-rendered via a small inline template and
        appended to the table file indicated by each measure's ``table`` key.
        If a measure has no ``table`` key, it is appended to a default table
        passed via the first measure's table, else 'Measures'.
        """
        try:
            if not isinstance(measures, list) or not measures:
                raise ValueError("'measures' must be a non-empty list.")
            for i, m in enumerate(measures):
                self._require_fields(m, ["name", "expression"], f"measures[{i}]")

            # group measures by their target table
            # "Measures" is a reserved/unsupported table name in Power BI Desktop.
            # When no table is specified, find the first existing data table instead.
            _default_tname: str | None = None
            by_table: dict[str, list[dict[str, Any]]] = {}
            for m in measures:
                tname = m.get("table")
                if not tname:
                    if _default_tname is None:
                        tables_dir = self._writable(output_dir, "tables")
                        if tables_dir.exists():
                            candidates = sorted(
                                f.stem for f in tables_dir.glob("*.tmdl")
                                if f.stem.lower() not in {"date", "measures"}
                            )
                            _default_tname = candidates[0] if candidates else "Key Measures"
                        else:
                            _default_tname = "Key Measures"
                    tname = _default_tname
                by_table.setdefault(tname, []).append(m)

            written: list[str] = []
            appended: list[dict[str, Any]] = []
            for tname, group in by_table.items():
                table_file = self._writable(output_dir, "tables", f"{tname}.tmdl")
                block = "\n"
                for m in group:
                    # TMDL measure syntax: single-quote name (DAX brackets are invalid),
                    # tab-indent inside table block, no outer quotes on formatString value.
                    # NOTE: 'description' is NOT a valid TMDL measure property in Power BI
                    # Desktop (causes UnknownKeyword error); omit it entirely.
                    # Escape: DAX/TMDL doubles single quotes ('' ) — NOT backslash.
                    # Using backslash here left names like ``Bob's`` unescaped AND broke
                    # the dedup in _strip_existing_measures (which matches on the '' rule).
                    name = m['name'].replace("'", "''")
                    # Multi-line DAX expressions need every continuation line
                    # indented with THREE TABS so TMDL recognises them as part
                    # of the expression body (two-tab lines are parsed as new
                    # property/keyword lines and raise InvalidLineType). The
                    # measure-header line keeps a single tab, properties after
                    # the expression use two tabs (formatString / displayFolder
                    # / dataCategory). The reference shape (from real PBIP
                    # outputs) is::
                    #
                    #     \tmeasure 'X' =
                    #     \t\t\tVAR a = ...
                    #     \t\t\tRETURN ...
                    #     \t\tformatString: 0.00
                    #
                    # For single-line expressions we keep the legacy inline
                    # form so simple SUM/COUNT measures stay compact.
                    expr_lines = str(m['expression']).splitlines() or [""]
                    if len(expr_lines) == 1:
                        block += f"\n\tmeasure '{name}' = {expr_lines[0]}\n"
                    else:
                        block += f"\n\tmeasure '{name}' =\n"
                        for cont in expr_lines:
                            stripped = cont.strip()
                            if stripped:
                                block += f"\t\t\t{stripped}\n"
                            else:
                                block += "\n"
                    if m.get("formatString"):
                        block += f"\t\tformatString: {m['formatString']}\n"
                    if m.get("displayFolder"):
                        block += f"\t\tdisplayFolder: {m['displayFolder']}\n"
                    # dataCategory is required for SVG measures (ImageUrl)
                    # so Power BI renders the returned data URI as an image
                    # in tables/matrices instead of showing the raw string.
                    if m.get("dataCategory"):
                        block += f"\t\tdataCategory: {m['dataCategory']}\n"
                # Write measures to the TMDL file.
                # TMDL order inside a table block must be:
                #   columns → measures → annotation PBI_ResultType → partition → ...
                # Simply appending puts measures after the partition which Desktop rejects.
                # Fix: INSERT the block just before the first "	annotation PBI_ResultType" line.
                #
                # DEDUP (critical): before inserting, remove any EXISTING measure
                # block with the same name from the file. Without this, re-running
                # DAXAgent (e.g. the Phase 4 feedback loop) or calling
                # add_measure/write_tmdl_measures twice from `adk web` appends a
                # second copy of the measure, and Power BI Desktop rejects the
                # project with PFE_TM_OBJECT_NAME_ALREADY_EXISTS.
                if table_file.exists():
                    existing = table_file.read_text(encoding="utf-8")
                    # Strip any existing measure block whose name matches (case-
                    # insensitively -- Power BI Desktop treats measure names that
                    # way) one in this batch so we replace rather than duplicate.
                    # A measure block runs from "measure 'Name' =" up to (not
                    # including) the next tab-indented top-level keyword (another
                    # measure, annotation, partition, or end of file).
                    names_to_replace = {m["name"] for m in group}
                    existing = _strip_existing_measures(existing, names_to_replace)
                    insert_marker = "\n\tannotation PBI_ResultType = Table"
                    if insert_marker in existing:
                        new_content = existing.replace(
                            insert_marker, block + insert_marker, 1
                        )
                        atomic_write_text(table_file, new_content)
                    else:
                        # No annotation marker — safe to append
                        with open(table_file, "a", encoding="utf-8", newline="\n") as fh:
                            fh.write(block)
                else:
                    # New standalone measures table: must start with table header
                    atomic_write_text(table_file, f"table {tname}{block}")
                written.append(str(table_file))
                appended.append({"table": tname, "count": len(group)})

            log.info(f"[write_tmdl_measures] appended {len(measures)} measures "
                     f"across {len(by_table)} tables")
            return ToolResult(
                True,
                "write_tmdl_measures",
                f"Appended {len(measures)} DAX measures.",
                data={"files": written, "appended": appended},
            )
        except (PathSecurityError, ValueError, KeyError) as exc:
            log.exception("[write_tmdl_measures] failed")
            return ToolResult(False, "write_tmdl_measures", str(exc), errors=[str(exc)])

    # ---- tool 4: write_pbir_page -----------------------------------------

    def _resolve_data_table(self, output_dir: str) -> str | None:
        """Return the first non-Date, non-Measures data table in the tables dir."""
        tables_dir = self._writable(output_dir, "tables")
        if tables_dir.exists():
            candidates = sorted(
                f.stem for f in tables_dir.glob("*.tmdl")
                if f.stem.lower() not in {"date", "measures"}
            )
            return candidates[0] if candidates else None
        return None

    _CHART_TYPES = {
        "barChart", "columnChart", "lineChart", "pieChart",
        "donutChart", "areaChart", "scatterChart",
    }
    _TABLE_TYPES  = {"tableEx", "matrix"}
    _SLICER_TYPES = {"slicer"}

    def _normalize_query_state(self, visual_type: str, query_state: dict,
                                data_table: str | None) -> dict:
        """Convert agent's simplified select-array queryState to PBIR role-projection format.

        ADK agents often emit: {"select": [{"measure": {"table": T, "name": N}}, ...]}
        Power BI Desktop expects: {"Values": {"projections": [{"field": {...}}]}, ...}

        This method:
          1. Detects the simplified format (presence of "select" key)
          2. Fixes "Measures" table name (reserved in Power BI)
          3. Converts to role-projection format via pbir_generator helpers
        """
        import copy
        qs = copy.deepcopy(query_state)

        # Fix reserved "Measures" table name in the simplified format
        if data_table:
            def _fix_table(obj: Any) -> None:
                if isinstance(obj, dict):
                    for key in ("measure", "column"):
                        if key in obj and isinstance(obj[key], dict):
                            if obj[key].get("table", "").lower() in {"measures", "key measures"}:
                                obj[key]["table"] = data_table
                    for v in obj.values():
                        _fix_table(v)
                elif isinstance(obj, list):
                    for item in obj:
                        _fix_table(item)
            _fix_table(qs)

        # Detect simplified format: {"select": [...], "where": [...], ...}
        # OR a role-based simplified format: {"Rows": [{"kind":"column",...}], ...}
        # (the latter is emitted by highlevel._plan_rich_pages for matrix visuals
        # and has no "select" key — without this branch it passes through
        # unnormalized and Desktop rejects it.)
        if "select" not in qs:
            # Check if any role value is a list of simplified items (kind/name/table).
            needs_norm = any(
                isinstance(v, list) and v and isinstance(v[0], dict) and "kind" in v[0]
                for v in qs.values()
            )
            if not needs_norm:
                return qs  # already in role-projection format
            # Convert each role's simplified item list to projections in place.
            role_qs: dict[str, Any] = {}
            for role, items in qs.items():
                if not isinstance(items, list):
                    continue
                projs = []
                for item in items:
                    kind = item.get("kind")
                    if kind == "measure":
                        projs.append(_pb._projection_for_measure(item["table"], item["name"]))
                    elif kind == "column":
                        projs.append(_pb._projection_for_column(item["table"], item["name"]))
                if projs:
                    role_qs[role] = {"projections": projs}
            return role_qs

        select_items = qs.get("select", [])
        role_qs: dict[str, Any] = {}

        is_chart  = visual_type in self._CHART_TYPES
        is_table  = visual_type in self._TABLE_TYPES
        is_slicer = visual_type in self._SLICER_TYPES

        for item in select_items:
            # Support two simplified formats:
            #   (A) {"measure": {"table": T, "name": N}, "role": ...}  (agent-emitted)
            #   (B) {"kind": "measure", "name": N, "table": T, "role": ...}  (highlevel._simplified_select)
            # Same for columns. Without handling (B), the queryState ends up
            # empty and Power BI Desktop rejects the visual.
            kind = item.get("kind")
            if kind == "measure" or ("measure" in item and isinstance(item["measure"], dict)):
                m = item.get("measure") if isinstance(item.get("measure"), dict) else item
                proj = _pb._projection_for_measure(m["table"], m["name"])
                if is_chart:
                    role = item.get("role", "Y")
                elif is_slicer:
                    role = "Values"
                else:
                    role = item.get("role", "Values")
            elif kind == "column" or ("column" in item and isinstance(item["column"], dict)):
                c = item.get("column") if isinstance(item.get("column"), dict) else item
                proj = _pb._projection_for_column(c["table"], c["name"])
                if is_chart:
                    role = item.get("role", "Category")
                elif is_slicer:
                    role = "Values"
                elif visual_type == "matrix":
                    role = item.get("role", "Rows")
                else:
                    role = item.get("role", "Values")
            else:
                continue
            role_qs.setdefault(role, {"projections": []})
            role_qs[role]["projections"].append(proj)

        return role_qs

    def write_pbir_page(self, output_dir: str, page_def: dict[str, Any]) -> ToolResult:
        """Write a PBIR page (page.json) and any visuals it carries.

        ``page_def`` shape::

            {"id","displayName","width","height",
             "visuals":[{"id","visualType","title","x","y","width","height",
                         "z","tabOrder","queryState"}]}
        """
        try:
            self._require_fields(page_def, ["id", "displayName"], "page_def")
            visuals = page_def.get("visuals", [])
            self._validate_visuals(visuals)

            page_id = str(page_def["id"])
            page_base = self._writable(output_dir, "pages", page_id)
            ensure_dir(page_base / "visuals")

            # Detect the data table so we can fix measure references that
            # mistakenly point at the reserved "Measures" table name.
            _data_table = self._resolve_data_table(output_dir)

            # page.json via pbir_generator (canonical source of truth)
            page_payload = _pb.page_json(
                page_id,
                page_def["displayName"],
                width=page_def.get("width", 1280),
                height=page_def.get("height", 720),
            )
            page_json_path = page_base / "page.json"
            atomic_write_json(page_json_path, page_payload)

            # visuals
            written_visuals: list[str] = []
            for i, v in enumerate(visuals):
                vid = str(v.get("id") or stable_uuid())
                vdir = page_base / "visuals" / vid
                ensure_dir(vdir)
                query_state = self._normalize_query_state(
                    v["visualType"], v.get("queryState", {}), _data_table
                )
                visual_payload = _pb.visual_json(
                    visual_id=vid,
                    visual_type=v["visualType"],
                    query_state=query_state,
                    x=float(v.get("x", 0)),
                    y=float(v.get("y", 0)),
                    width=float(v.get("width", 300)),
                    height=float(v.get("height", 200)),
                    z=int(v.get("z", 0)),
                    tab_order=int(v.get("tabOrder", v.get("tab_order", i))),
                    title=v.get("title"),
                )
                vpath = vdir / "visual.json"
                atomic_write_json(vpath, visual_payload)
                written_visuals.append(str(vpath))

            log.info(f"[write_pbir_page] wrote page '{page_def['displayName']}' "
                     f"with {len(visuals)} visuals")
            return ToolResult(
                True,
                "write_pbir_page",
                f"Wrote PBIR page '{page_def['displayName']}' "
                f"({len(visuals)} visuals).",
                data={
                    "page_json": str(page_json_path),
                    "visuals": written_visuals,
                    "page_id": page_id,
                },
            )
        except (PathSecurityError, JSONValidationError, ValueError, KeyError) as exc:
            log.exception("[write_pbir_page] failed")
            return ToolResult(False, "write_pbir_page", str(exc), errors=[str(exc)])

    # ---- tool 4a: write_deneb_visual ---------------------------------------

    def write_deneb_visual(self, output_dir: str,
                           page_id: str,
                           deneb_def: dict[str, Any]) -> ToolResult:
        """Add a Deneb (Vega-Lite) custom visual to an existing PBIR page.

        The page directory (and its ``page.json``) must already exist —
        typically created by a prior ``write_pbir_page`` call. The Deneb
        visual is written under ``pages/<page_id>/visuals/<visual_id>/visual.json``
        without touching the rest of the page.

        ``deneb_def`` shape::

            {
                "id": str,                     # optional — generated if missing
                "table": str,                  # entity that owns the fields
                "fields": [                    # field bindings (>=1)
                    {"kind":"measure","name":"Total Sales"},
                    {"kind":"measure","name":"Total Sales PY"},
                ],
                "vega_lite_spec": {...},       # full Vega-Lite v5 spec dict
                "x": 40, "y": 40,              # geometry (px)
                "width": 560, "height": 180,
                "z": 0, "tabOrder": 0,
                "config": {...},               # optional Vega config override
            }
        """
        try:
            self._require_fields(
                deneb_def,
                ["table", "fields", "vega_lite_spec"],
                "deneb_def",
            )
            fields = deneb_def["fields"]
            if not isinstance(fields, list) or not fields:
                raise ValueError("'fields' must be a non-empty list")
            for i, f in enumerate(fields):
                self._require_fields(f, ["kind", "name"], f"fields[{i}]")
                if f["kind"] not in ("column", "measure"):
                    raise ValueError(
                        f"fields[{i}].kind must be 'column' or 'measure', "
                        f"got {f['kind']!r}"
                    )

            page_base = self._writable(output_dir, "pages", str(page_id))
            if not page_base.exists():
                raise ValueError(
                    f"page directory '{page_base}' does not exist — "
                    f"create the page first with write_pbir_page"
                )
            ensure_dir(page_base / "visuals")

            vid = str(deneb_def.get("id") or f"deneb-{stable_uuid()[:8]}")
            vdir = page_base / "visuals" / vid
            ensure_dir(vdir)

            # Fix references that point at the reserved "Measures" table.
            _data_table = self._resolve_data_table(output_dir)
            table_name = str(deneb_def["table"])
            if table_name == "Measures":
                table_name = _data_table or table_name

            pos = {
                "x": deneb_def.get("x", 40),
                "y": deneb_def.get("y", 40),
                "z": deneb_def.get("z", 0),
                "width": deneb_def.get("width", 560),
                "height": deneb_def.get("height", 180),
                "tabOrder": deneb_def.get("tabOrder", deneb_def.get("tab_order", 0)),
            }

            visual_payload = _pb.build_deneb_visual(
                visual_id=vid,
                pos=pos,
                table=table_name,
                fields=fields,
                vega_lite_spec=deneb_def["vega_lite_spec"],
                config=deneb_def.get("config"),
            )
            vpath = vdir / "visual.json"
            atomic_write_json(vpath, visual_payload)

            log.info(f"[write_deneb_visual] wrote Deneb visual '{vid}' on page '{page_id}'")
            return ToolResult(
                True,
                "write_deneb_visual",
                f"Wrote Deneb visual '{vid}' on page '{page_id}'.",
                data={
                    "visual_json": str(vpath),
                    "visual_id": vid,
                    "page_id": str(page_id),
                },
            )
        except (PathSecurityError, JSONValidationError, ValueError, KeyError) as exc:
            log.exception("[write_deneb_visual] failed")
            return ToolResult(False, "write_deneb_visual", str(exc), errors=[str(exc)])

    # ---- tool 4b: write_tmdl_calc_group ------------------------------------

    def write_tmdl_calc_group(self, output_dir: str,
                               group_def: dict[str, Any]) -> ToolResult:
        """Write a calculation group as a TMDL table file.

        Calculation groups are special tables whose partition type is
        ``calculationGroup``. They apply DAX transformations to any measure
        dropped onto a visual alongside them.

        ``group_def`` shape::

            {
                "name": str,          # e.g. "Time Intelligence"
                "precedence": int,    # optional (default 10)
                "items": [
                    {
                        "name": str,                    # e.g. "YTD"
                        "expression": str,              # DAX, must use SELECTEDMEASURE()
                        "formatStringDefinition": str,  # optional DAX format expression
                        "ordinal": int,                 # optional display order
                    }
                ]
            }
        """
        try:
            self._require_fields(group_def, ["name", "items"], "group_def")
            gname     = str(group_def["name"])
            prec      = int(group_def.get("precedence", 10))
            items     = group_def["items"]
            if not isinstance(items, list) or not items:
                raise ValueError("'items' must be a non-empty list")

            safe_name = gname.replace("'", "''")
            col_tag   = stable_uuid()
            tbl_tag   = stable_uuid()

            lines: list[str] = [
                f"table '{safe_name}'",
                f"",
                f"\tlineageTag: {tbl_tag}",
                f"",
                f"\tcalculationGroup",
                f"\t\tprecedence: {prec}",
            ]

            for item in items:
                self._require_fields(item, ["name", "expression"], "items[]")
                iname  = str(item["name"]).replace("'", "''")
                iexpr  = str(item["expression"])
                fmt    = item.get("formatStringDefinition")
                ordinal = item.get("ordinal")

                lines.append("")
                if fmt:
                    # multi-property item block (only when formatStringDefinition present)
                    lines.append(f"\t\tcalculationItem '{iname}'")
                    lines.append(f"\t\t\texpression = {iexpr}")
                    lines.append(f"\t\t\tformatStringDefinition = {fmt}")
                else:
                    # single-line shorthand — display order = file order
                    lines.append(f"\t\tcalculationItem '{iname}' = {iexpr}")

            lines += [
                "",
                f"\tcolumn '{safe_name}'",
                f"\t\tdataType: string",
                f"\t\tlineageTag: {col_tag}",
                f"\t\tsourceColumn: Name",
                f"\t\tsummarizeBy: none",
                "",
                f"\tannotation PBI_ResultType = Table",
                "",
                f"\tpartition 'Partition_{safe_name}' = calculationGroup",
                f"\t\tmode: import",
                "",
            ]

            content = "\n".join(lines)
            target  = self._writable(output_dir, "tables",
                                     f"{gname}.tmdl")
            atomic_write_text(target, content)
            log.info(f"[write_tmdl_calc_group] wrote '{gname}' "
                     f"({len(items)} items) → {target}")
            return ToolResult(
                True,
                "write_tmdl_calc_group",
                f"Wrote calculation group '{gname}' ({len(items)} items).",
                data={"file": str(target), "name": gname,
                      "items": [i["name"] for i in items]},
            )
        except (PathSecurityError, ValueError, KeyError) as exc:
            log.exception("[write_tmdl_calc_group] failed")
            return ToolResult(False, "write_tmdl_calc_group",
                              str(exc), errors=[str(exc)])

    # ---- tool 5: write_theme_json ----------------------------------------

    def write_theme_json(self, output_dir: str, theme: dict[str, Any] | None = None) -> ToolResult:
        """Write the report's theme.json to StaticResources/RegisteredResources/.

        Args:
            output_dir: The .Report folder path (e.g., "MyReport.Report")
            theme: Optional theme dict. If None, uses the bundled default theme.

        The theme is written to: <output_dir>/StaticResources/RegisteredResources/theme.json
        """
        try:
            if theme is None:
                with open(_TEMPLATE_DIR / "theme.json", encoding="utf-8") as fh:
                    theme = json.load(fh)
            # validate it's JSON-serialisable + an object
            if not isinstance(theme, dict):
                raise JSONValidationError("theme must be a JSON object")
            serialize_json(theme)  # raises if not serialisable

            # Theme must be in StaticResources/RegisteredResources/ relative
            # to the bare .Report folder, NEVER inside definition/ -- unlike
            # every other write_*/add_* tool in this codebase, which DOES
            # want a ".../definition" suffixed output_dir. A caller passing
            # the (wrong, but consistent-with-sibling-tools) ".../definition"
            # form here writes to a path report.json never references, so
            # Power BI Desktop silently keeps showing the OLD theme forever
            # -- confirmed live: apply_theme reported success on every call,
            # but the change never took visual effect. Strip it defensively
            # rather than relying solely on callers getting this one
            # exception to the convention right.
            output_dir = re.sub(r"[\\/]?definition[\\/]?$", "", output_dir)
            theme_dir = self._writable(output_dir, "StaticResources/RegisteredResources")
            ensure_dir(theme_dir)
            target = theme_dir / "theme.json"
            atomic_write_json(target, theme)
            log.info(f"[write_theme_json] wrote {target}")
            return ToolResult(
                True,
                "write_theme_json",
                f"Wrote report theme ({theme.get('name', 'unnamed')}).",
                data={"path": str(target), "name": theme.get("name", "unnamed")},
            )
        except (PathSecurityError, JSONValidationError, OSError) as exc:
            log.exception("[write_theme_json] failed")
            return ToolResult(False, "write_theme_json", str(exc), errors=[str(exc)])

    # ---- tool 6: validate_pbip_structure ---------------------------------

    def validate_pbip_structure(self, pbip_dir: str) -> ToolResult:
        """Validate the entire .pbip folder structure & required files.

        Checks (non-exhaustive, see ValidatorAgent for the deep semantic pass):
          * Semantic model folder exists with definition/ + tables/
          * Report folder exists with definition/ + report.json
          * At least one *.tmdl table file
          * Every page.json has width/height/displayName
          * Every visual.json has the required fields (per spec)
        """
        errors: list[str] = []
        warnings: list[str] = []
        try:
            root = self._readable(pbip_dir, strict=True)  # PBIP validation stays contained
            if not root.is_dir():
                raise FileNotFoundError(f"pbip_dir not found: {root}")

            # Reads are now confined to the allowed read-root set by _readable;
            # no extra containment logic is needed here.

            sm_dirs = list(root.glob("*.SemanticModel"))
            report_dirs = list(root.glob("*.Report"))
            pbip_files = list(root.glob("*.pbip"))

            if not sm_dirs:
                errors.append("No *.SemanticModel folder found.")
            if not report_dirs:
                errors.append("No *.Report folder found.")
            if not pbip_files:
                errors.append(
                    "No *.pbip entry file found in root -- Power BI Desktop "
                    "cannot open the project without it."
                )
            else:
                # verify the .pbip file references a real report folder
                for pbip_file in pbip_files:
                    try:
                        data = json.loads(pbip_file.read_text(encoding="utf-8"))
                        artifacts = data.get("artifacts", [])
                        report_paths = [
                            a.get("report", {}).get("path")
                            for a in artifacts if isinstance(a, dict)
                        ]
                        report_paths = [p for p in report_paths if p]
                        if not report_paths:
                            errors.append(
                                f"{pbip_file.name}: 'artifacts' has no report path."
                            )
                        for rp in report_paths:
                            if not (root / rp).is_dir():
                                errors.append(
                                    f"{pbip_file.name}: report path '{rp}' "
                                    "does not point to an existing folder."
                                )
                    except json.JSONDecodeError as exc:
                        errors.append(f"{pbip_file.name}: invalid JSON: {exc}")

            table_count = 0
            measure_count = 0
            for sm in sm_dirs:
                tables_dir = sm / "definition" / "tables"
                if not tables_dir.is_dir():
                    errors.append(f"{sm.name}: missing definition/tables/")
                    continue
                tmdl_files = list(tables_dir.glob("*.tmdl"))
                table_count += len(tmdl_files)
                for tf in tmdl_files:
                    txt = tf.read_text(encoding="utf-8")
                    # Strip TMDL line comments (// ... EOL) before counting so
                    # that a comment mentioning "measure '" does not inflate
                    # the count (false positive).
                    stripped_lines = []
                    for ln in txt.splitlines():
                        cidx = ln.find("//")
                        if cidx >= 0:
                            ln = ln[:cidx]
                        stripped_lines.append(ln)
                    code_txt = "\n".join(stripped_lines)
                    # count measures (correct TMDL syntax uses single quotes)
                    measure_count += code_txt.count("measure '")
                    # Duplicate-measure detection (case-insensitive): Power BI
                    # Desktop treats measure names case-insensitively, so two
                    # blocks "Min Age" and "Min age" collide and the project
                    # fails to open with "Item '...' already exists". The dedup
                    # in write_tmdl_measures is the primary guard; this is the
                    # last-resort safety net that surfaces the issue as a build
                    # error BEFORE the user hits the Desktop error.
                    _seen: dict[str, str] = {}  # lowercased name -> original casing
                    _dupes: set[str] = set()
                    for _m in re.finditer(
                        r"\n\tmeasure\s+'([^']+)'", code_txt, re.IGNORECASE
                    ):
                        _orig = _m.group(1)
                        _low = _orig.lower()
                        if _low in _seen and _seen[_low] != _orig:
                            _dupes.add(_low)
                        _seen[_low] = _orig
                    if _dupes:
                        errors.append(
                            f"{tf.name}: duplicate measure names "
                            f"(case-insensitive): {sorted(_dupes)} -- Power BI "
                            f"Desktop will reject this project."
                        )
                    # legacy DAX-bracket measures are a syntax error in TMDL
                    if "measure [" in code_txt:
                        errors.append(
                            f"{tf.name}: TMDL measure uses DAX bracket syntax "
                            "'measure [Name]' — must use single quotes 'measure ''Name'''."
                        )
                    if not txt.strip().startswith("table "):
                        errors.append(f"{tf.name}: TMDL must start with 'table <Name>'.")

            page_count = 0
            visual_count = 0
            for rep in report_dirs:
                rdef = rep / "definition"
                rjson = rdef / "report.json"
                if not rjson.is_file():
                    errors.append(f"{rep.name}: missing definition/report.json")
                pages_dir = rdef / "pages"
                if not pages_dir.is_dir():
                    warnings.append(f"{rep.name}: no pages/ directory")
                    continue
                # Only count pages that are in pages.json's pageOrder — orphan
                # page folders left over from a previous run (or written by an
                # LLM via add_page without updating pages.json) must NOT be
                # counted or validated, otherwise the summary reports a page
                # count that Desktop (which reads pageOrder) does not show.
                page_order: list[str] = []
                pmeta = pages_dir / "pages.json"
                if pmeta.is_file():
                    try:
                        page_order = json.loads(
                            pmeta.read_text(encoding="utf-8")
                        ).get("pageOrder", [])
                    except (json.JSONDecodeError, OSError):
                        page_order = []
                # The REVERSE of the orphan-folder check below: an id listed in
                # pageOrder whose folder no longer exists (e.g. delete_page
                # removed the folder but pages.json was left stale). Desktop
                # reads pageOrder and will fail to open a page it expects to
                # find — this is a hard error, not a warning.
                for pid in page_order:
                    if not (pages_dir / pid).is_dir():
                        errors.append(
                            f"pages.json pageOrder references '{pid}', but no "
                            "matching page folder exists on disk — Desktop "
                            "will fail to open this report."
                        )
                for page_folder in pages_dir.iterdir():
                    if not page_folder.is_dir():
                        continue
                    # Skip orphan pages: folders on disk that are NOT in pageOrder.
                    if page_order and page_folder.name not in page_order:
                        warnings.append(
                            f"{page_folder.name}: orphan page folder (not in "
                            "pages.json pageOrder) — Desktop will not show it."
                        )
                        continue
                    page_count += 1
                    pjson = page_folder / "page.json"
                    if not pjson.is_file():
                        errors.append(f"{page_folder.name}: missing page.json")
                        continue
                    try:
                        pdata = json.loads(pjson.read_text(encoding="utf-8"))
                        for f in ("width", "height"):
                            if f not in pdata:
                                errors.append(f"{page_folder.name}: page.json missing '{f}'")
                    except json.JSONDecodeError as exc:
                        errors.append(f"{page_folder.name}: page.json invalid JSON: {exc}")

                    for vdir in (page_folder / "visuals").glob("*") if (page_folder / "visuals").is_dir() else []:
                        if not vdir.is_dir():
                            continue
                        visual_count += 1
                        vjson = vdir / "visual.json"
                        if not vjson.is_file():
                            errors.append(f"{vdir.name}: missing visual.json")
                            continue
                        try:
                            vdata = json.loads(vjson.read_text(encoding="utf-8"))
                        except json.JSONDecodeError as exc:
                            errors.append(f"{vdir.name}: visual.json invalid JSON: {exc}")
                            continue
                        self._check_visual_fields(vdata, vdir.name, errors)

            ok = not errors
            summary = {
                "tables": table_count,
                "measures": measure_count,
                "pages": page_count,
                "visuals": visual_count,
                "errors": errors,
                "warnings": warnings,
            }
            log.info(f"[validate_pbip_structure] ok={ok} tables={table_count} "
                     f"measures={measure_count} pages={page_count} visuals={visual_count} "
                     f"errors={len(errors)}")
            return ToolResult(
                ok,
                "validate_pbip_structure",
                "Validation passed." if ok else f"Validation failed with {len(errors)} error(s).",
                data=summary,
                errors=errors,
            )
        except Exception as exc:
            log.exception("[validate_pbip_structure] failed")
            return ToolResult(False, "validate_pbip_structure", str(exc), errors=[str(exc)])

    # ---- internal validators ---------------------------------------------

    def _require_fields(self, obj: dict[str, Any], fields: list[str], ctx: str) -> None:
        if not isinstance(obj, dict):
            raise ValueError(f"{ctx}: expected an object, got {type(obj).__name__}")
        for f in fields:
            if f not in obj:
                raise ValueError(f"{ctx}: missing required field '{f}'")

    def _validate_columns(self, columns: list[dict[str, Any]]) -> None:
        if not isinstance(columns, list) or not columns:
            raise ValueError("'columns' must be a non-empty list")
        for i, col in enumerate(columns):
            self._require_fields(col, ["name", "dataType"], f"columns[{i}]")
            if not col["name"] or not str(col["name"]).strip():
                raise ValueError(f"columns[{i}]: name cannot be empty")
            if col["dataType"] not in {
                "string", "int64", "double", "decimal", "boolean", "dateTime", "date"
            }:
                raise ValueError(
                    f"columns[{i}]: unsupported dataType '{col['dataType']}'"
                )

    def _validate_visuals(self, visuals: list[dict[str, Any]]) -> None:
        valid_types = {
            "card", "barChart", "columnChart", "lineChart", "pieChart",
            "tableEx", "table", "matrix", "kpi", "slicer",
            "donutChart", "scatterChart", "areaChart", "map",
        }
        for i, v in enumerate(visuals):
            self._require_fields(v, ["visualType"], f"visuals[{i}]")
            if v["visualType"] not in valid_types:
                raise ValueError(
                    f"visuals[{i}]: unknown visualType '{v['visualType']}'. "
                    f"Allowed: {sorted(valid_types)}"
                )

    @staticmethod
    def _check_visual_fields(vdata: dict[str, Any], vname: str, errors: list[str]) -> None:
        for f in ("$schema", "name", "position", "visual"):
            if f not in vdata:
                errors.append(f"{vname}: visual.json missing '{f}'")
        pos = vdata.get("position") or {}
        for f in ("x", "y", "width", "height", "tabOrder"):
            if f not in pos:
                errors.append(f"{vname}: visual.json position missing '{f}'")
        visual = vdata.get("visual") or {}
        if "visualType" not in visual:
            errors.append(f"{vname}: visual.json visual missing 'visualType'")


def _default_summarization(pb_type: str) -> str:
    """Local mirror of schema_inference's summarization rule (avoids import cycle)."""
    if pb_type in {"int64", "double", "decimal"}:
        return "sum"
    return "none"


# Power BI dataType -> M type expression
_M_TYPE_MAP: dict[str, str] = {
    "string": "type text",
    "int64": "Int64.Type",
    "double": "type number",
    "decimal": "type number",
    "boolean": "type logical",
    "dateTime": "type datetime",
    "date": "type date",
}


def _build_m_partition(table_name: str, source_path: str,
                       columns: list[dict[str, Any]],
                       source_type: str = "csv",
                       connection_params: dict[str, Any] | None = None) -> str:
    """Build a TMDL partition block with an M query.

    Dispatches on ``source_type``:
      * ``csv``     — Csv.Document (default, original behaviour)
      * ``sql``     — Sql.Database(server, database, [Query=...])
      * ``excel``   — Excel.Workbook(File.Contents(path))
      * ``web``     — Web.Contents(url)

    Power BI Desktop requires every import table to have at least one partition
    (PFE_TM_TABLE_NO_PARTITIONS).

    IMPORTANT: Every M statement must be on a SINGLE line within the TMDL block.
    Line breaks inside an M statement cause the TMDL parser to return a null
    query object, leading to the 'Non-null assertion failure: query' crash.
    """
    n = len(columns)
    # Escape a value for an M double-quoted string literal: " must be doubled
    # (the M language escape rule inside "..." strings). Without this, a column
    # named ``A"x`` produces {{"A"x", ...}} which breaks the M parser.
    def _m_str(value: str) -> str:
        return str(value).replace('"', '""')

    # Build the TransformColumnTypes list: {{"Col", type text}, ...}
    type_pairs = ", ".join(
        '{"' + _m_str(col["name"]) + '", ' + _M_TYPE_MAP.get(col["dataType"], "type text") + '}'
        for col in columns
    )
    m_types = "{" + type_pairs + "}"
    params = connection_params or {}

    # --- build the RawData line based on source type ---
    if source_type == "sql":
        server = _m_str(params.get("server", "localhost"))
        database = _m_str(params.get("database", ""))
        query = _m_str(params.get("query", ""))
        table_or_view = _m_str(params.get("table", ""))
        # If a raw SQL query is given, use it; otherwise select from the table
        if query:
            raw_data = (
                f'Source = Sql.Database("{server}", "{database}", [Query="{query}"]),'
            )
        else:
            raw_data = (
                f'Source = Sql.Database("{server}", "{database}"),'
            )
            # Navigate to the table: Source{[Schema="dbo",Item="Table"]}[Data]
            raw_data += f' RawTable = Source{{[Schema="dbo",Item="{table_or_view}"]}}[Data],'
        steps = [
            raw_data,
            f"TypedData = Table.TransformColumnTypes("
            + (f"RawTable," if not query else f"Source,")
            + f"{m_types})",
        ]
        let_body = "\n".join(f"\t\t\t\t{s}" for s in steps)
        in_expr = "\t\t\t\tTypedData"
    elif source_type == "excel":
        m_path = _m_str(source_path.replace("\\", "/"))
        sheet = _m_str(params.get("sheet", ""))
        sheet_nav = f'{{[Item="{sheet}",Kind="Sheet"]}}[Data]' if sheet else "[Data]"
        steps = [
            f'Source = Excel.Workbook(File.Contents("{m_path}"), null, true),',
            f'SheetData = Source{sheet_nav},',
            f"TypedData = Table.TransformColumnTypes(SheetData,{m_types})",
        ]
        let_body = "\n".join(f"\t\t\t\t{s}" for s in steps)
        in_expr = "\t\t\t\tTypedData"
    elif source_type == "web":
        url = _m_str(params.get("url", source_path))
        steps = [
            f'Source = Csv.Document(Web.Contents("{url}"),[Delimiter=",",Columns={n},Encoding=65001,QuoteStyle=QuoteStyle.None]),',
            f"WithHeaders = Table.PromoteHeaders(Source,[PromoteAllScalars=true]),",
            f"TypedData = Table.TransformColumnTypes(WithHeaders,{m_types})",
        ]
        let_body = "\n".join(f"\t\t\t\t{s}" for s in steps)
        in_expr = "\t\t\t\tTypedData"
    else:
        # CSV (default, original behaviour)
        m_path = _m_str(source_path.replace("\\", "/"))
        steps = [
            f'RawData = Csv.Document(File.Contents("{m_path}"),[Delimiter=",",Columns={n},Encoding=65001,QuoteStyle=QuoteStyle.None]),',
            f"WithHeaders = Table.PromoteHeaders(RawData,[PromoteAllScalars=true]),",
            f"TrimmedHeaders = Table.TransformColumnNames(WithHeaders, Text.Trim),",
            f"TypedData = Table.TransformColumnTypes(TrimmedHeaders,{m_types})",
        ]
        let_body = "\n".join(f"\t\t\t\t{s}" for s in steps)
        in_expr = "\t\t\t\tTypedData"

    return (
        f"\n\tpartition {table_name} = m\n"
        f"\t\tmode: import\n"
        f"\t\tqueryGroup: Tables\n"
        f"\t\tsource =\n"
        f"\t\t\tlet\n"
        f"{let_body}\n"
        f"\t\t\tin\n"
        f"\t\t\t{in_expr}\n"
        f"\n"
        f"\tannotation PBI_NavigationStepName = Navigation\n"
    )


# ---------------------------------------------------------------------------
# MCP stdio entry point
# ---------------------------------------------------------------------------


def _build_mcp_server(allowed_root: str | os.PathLike[str]):
    """Build an MCP server exposing the project tools over stdio.

    The MCP SDK is imported lazily so the rest of the project (and its tests)
    can run even if the ``mcp`` package is not installed. The orchestrator uses
    :class:`PbipToolbox` directly for in-process calls.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'mcp' package is required to run the MCP server. "
            "Install it with: pip install mcp"
        ) from exc

    toolbox = PbipToolbox(allowed_root)
    mcp = FastMCP("powerbi-builder")

    @mcp.tool()
    def read_csv_schema(csv_path: str, sheet_name: str | None = None) -> str:
        """Read a CSV, JSON, or Excel schema file and infer its Power BI table schema.

        Args:
            csv_path: Absolute or relative path to a .csv / .json schema / .xlsx file.
            sheet_name: Excel sheet name to read (only for .xlsx/.xls; None = first sheet).
        Returns:
            JSON string with the inferred schema, columns, and sample values.
        """
        return serialize_json(
            toolbox.read_csv_schema(csv_path, sheet_name=sheet_name).as_dict()
        )

    @mcp.tool()
    def write_tmdl_table(output_dir: str, table_def: dict) -> str:
        """Write a TMDL table definition file under the allowed output root.

        Args:
            output_dir: Relative sub-path of the allowed root to write into
                (e.g. ``"<Name>.SemanticModel/definition"``).
            table_def: Object with ``name``, ``columns[]`` (each with
                ``name``, ``dataType``), and optional ``measures[]``.
        Returns:
            JSON string describing the result and written path.
        """
        return serialize_json(toolbox.write_tmdl_table(output_dir, table_def).as_dict())

    @mcp.tool()
    def write_tmdl_measures(output_dir: str, measures: list) -> str:
        """Append DAX measures to one or more existing TMDL table files.

        Args:
            output_dir: Relative sub-path of the allowed root.
            measures: List of measure objects with ``name``, ``expression``,
                and optional ``table``, ``displayFolder``, ``description``,
                ``formatString``.
        Returns:
            JSON string describing how many measures were appended where.
        """
        return serialize_json(toolbox.write_tmdl_measures(output_dir, measures).as_dict())

    @mcp.tool()
    def write_pbir_page(output_dir: str, page_def: dict) -> str:
        """Write a PBIR page (page.json) and its visuals to the report folder.

        Args:
            output_dir: Relative sub-path under the allowed root
                (e.g. ``"<Name>.Report/definition"``).
            page_def: Object with ``id``, ``displayName``, optional
                ``width``/``height``, and ``visuals[]``.
        Returns:
            JSON string with the page.json path and the list of visual paths.
        """
        return serialize_json(toolbox.write_pbir_page(output_dir, page_def).as_dict())

    @mcp.tool()
    def write_deneb_visual(output_dir: str, page_id: str, deneb_def: dict) -> str:
        """Add a Deneb (Vega-Lite) custom visual to an existing PBIR page.

        Args:
            output_dir: Relative sub-path under the allowed root
                (e.g. ``"<Name>.Report/definition"``).
            page_id: id of the existing page to attach the visual to.
            deneb_def: Object with ``table``, ``fields`` (list of
                ``{"kind","name"}``), ``vega_lite_spec`` (Vega-Lite v5 dict),
                and optional geometry / config / id.
        Returns:
            JSON string with the visual.json path and the visual id.
        """
        return serialize_json(
            toolbox.write_deneb_visual(output_dir, page_id, deneb_def).as_dict()
        )

    @mcp.tool()
    def write_theme_json(output_dir: str, theme: dict | None = None) -> str:
        """Write the report's theme.json using the bundled theme if none given.

        Args:
            output_dir: Relative sub-path of the allowed root
                (e.g. ``"<Name>.Report/definition"``).
            theme: Optional theme object. If omitted, the default theme is used.
        Returns:
            JSON string with the path of the written theme.json.
        """
        return serialize_json(toolbox.write_theme_json(output_dir, theme).as_dict())

    @mcp.tool()
    def validate_pbip_structure(pbip_dir: str) -> str:
        """Validate a .pbip folder's structure and required files.

        Args:
            pbip_dir: Path to the .pbip folder to validate.
        Returns:
            JSON string with ok flag, counts, errors and warnings.
        """
        return serialize_json(toolbox.validate_pbip_structure(pbip_dir).as_dict())

    # ---- Phase 7: high-level orchestration tools ---------------------------
    from . import highlevel as _hl  # lazy import (avoids cycles)

    @mcp.tool()
    def generate_pbip(source: str, description: str,
                       project_name: str = "",
                       output_root: str = "./output",
                       theme_preset: str = "default",
                       sheet: str = "",
                       num_pages: int = 0,
                       visual_variety: str = "") -> str:
        """Build a complete PBIP from CSV / Excel / JSON in one call.

        Drives the full orchestrator pipeline (Schema → DAX → Report → Validate).

        Args:
            source: Path to the input file (.csv/.xlsx/.json schema).
            description: Plain-English description of what to build.
            project_name: Optional override for the auto-derived name.
            output_root: Where to write the .pbip (default ./output).
            theme_preset: default | corporate_blue | modern_dark | earth_tones | vibrant
            sheet: Excel sheet name (default first sheet).
            num_pages: Optional explicit page count (0 = infer from description).
            visual_variety: "" (default) or "all" to include scatter/pie/kpi visuals.
        """
        return serialize_json(_hl.generate_pbip(
            source=source, description=description,
            project_name=project_name or None,
            output_root=output_root, theme_preset=theme_preset,
            sheet=sheet or None,
            num_pages=num_pages, visual_variety=visual_variety,
        ))

    @mcp.tool()
    def edit_pbip(pbip_dir: str, description: str,
                  output_root: str = "",
                  theme_preset: str = "default") -> str:
        """Edit an existing PBIP — add measures/pages from a description.

        Copies the source PBIP into output_root then runs the edit pipeline
        (ReadPBIP → DAX → Report → Validate). The original is left untouched.

        Args:
            pbip_dir: Path to an existing PBIP project folder.
            description: Edits to apply (e.g. "add YoY % and a trend page").
            output_root: Where to write the edited copy (default: parent of pbip_dir).
            theme_preset: Theme preset key.
        """
        return serialize_json(_hl.edit_pbip(
            pbip_dir=pbip_dir, description=description,
            output_root=output_root or None,
            theme_preset=theme_preset,
        ))

    @mcp.tool()
    def add_measure(pbip_dir: str, name: str, expression: str,
                    table: str = "", format_string: str = "",
                    display_folder: str = "") -> str:
        """Append a single DAX measure to a PBIP semantic model.

        Args:
            pbip_dir: Path to an existing PBIP project folder.
            name: Measure name.
            expression: DAX expression.
            table: Target table name (auto-detected if empty).
            format_string: Optional TMDL formatString (e.g. "#,0.00").
            display_folder: Optional display folder.
        """
        return serialize_json(_hl.add_measure(
            pbip_dir=pbip_dir, name=name, expression=expression,
            table=table or None,
            format_string=format_string or None,
            display_folder=display_folder or None,
        ))

    @mcp.tool()
    def add_visual(pbip_dir: str, page_id: str, visual_type: str,
                   query_state: dict, title: str = "",
                   x: float = 40, y: float = 40,
                   width: float = 400, height: float = 300,
                   visual_id: str = "") -> str:
        """Add one visual to an existing page in a PBIP report.

        Args:
            pbip_dir: Path to an existing PBIP project folder.
            page_id: id of an existing page (the pages/<id> folder name).
            visual_type: card | barChart | columnChart | lineChart | tableEx |
                         matrix | kpi | slicer | donutChart | scatterChart | ...
            query_state: Either simplified ``{"select":[...]}`` or full role-projection form.
            title: Optional visual title.
            x, y, width, height: Geometry in pixels.
            visual_id: Optional explicit id; otherwise generated.
        """
        return serialize_json(_hl.add_visual(
            pbip_dir=pbip_dir, page_id=page_id, visual_type=visual_type,
            query_state=query_state, title=title or None,
            x=x, y=y, width=width, height=height,
            visual_id=visual_id or None,
        ))

    @mcp.tool()
    def add_page(pbip_dir: str, display_name: str,
                 visuals: list | None = None,
                 width: int = 1280, height: int = 720,
                 page_id: str = "") -> str:
        """Add a new page to a PBIP report and update pages.json.

        Args:
            pbip_dir: Path to an existing PBIP project folder.
            display_name: Display name for the new page.
            visuals: Optional list of visual definitions.
            width, height: Page size in pixels.
            page_id: Optional explicit page id; otherwise generated.
        """
        return serialize_json(_hl.add_page(
            pbip_dir=pbip_dir, display_name=display_name,
            visuals=visuals or [],
            width=width, height=height,
            page_id=page_id or None,
        ))

    @mcp.tool()
    def deploy_to_fabric(pbip_dir: str, workspace: str,
                          mode: str = "auto",
                          dry_run: bool = True,
                          skip_report: bool = False,
                          skip_model: bool = False) -> str:
        """Upload a PBIP to a Microsoft Fabric workspace via the fab CLI.

        Defaults to dry_run=True for safety.

        Args:
            pbip_dir: Path to an existing PBIP project folder.
            workspace: Target Fabric workspace name.
            mode: auto | create | update (default auto).
            dry_run: True = print fab commands, do not execute. Default True.
            skip_report: Skip the Report import (model only).
            skip_model: Skip the SemanticModel import (rarely useful).
        """
        return serialize_json(_hl.deploy_to_fabric(
            pbip_dir=pbip_dir, workspace=workspace, mode=mode,
            dry_run=dry_run, skip_report=skip_report, skip_model=skip_model,
        ))

    @mcp.tool()
    def suggest_measures(source: str,
                          base_table: str = "",
                          pattern_types: list | None = None,
                          base_name: str = "",
                          base_expr: str = "",
                          date_col: str = "'Date'[Date]") -> str:
        """Propose DAX measures for a CSV / Excel / JSON / PBIP project.

        Two modes:
            * Auto (default): deterministic schema-driven suggestions.
            * Pattern-driven: pass ``pattern_types`` (e.g. ["ytd","yoy_pct"]) +
              ``base_name`` to materialise measures from the pattern library.

        Args:
            source: Path to a CSV / Excel / JSON file OR an existing PBIP folder.
            base_table: Table name override.
            pattern_types: Optional pattern keys.
            base_name: Required when pattern_types is set.
            base_expr: Base measure DAX (default ``[<base_name>]``).
            date_col: Date column for time patterns (default ``'Date'[Date]``).
        """
        return serialize_json(_hl.suggest_measures(
            source=source,
            base_table=base_table or None,
            pattern_types=pattern_types,
            base_name=base_name or None,
            base_expr=base_expr or None,
            date_col=date_col,
        ))

    return mcp


def main() -> None:
    """Stdio MCP server entry point."""
    import sys

    AuditLogger.configure(
        log_file=os.getenv("LOG_FILE", "./logs/powerbi_builder.log"),
        level=os.getenv("LOG_LEVEL", "INFO"),
        mcp_stdio=True,  # stdout is the JSON-RPC transport; log to stderr
    )
    # The allowed root for a standalone MCP run defaults to ./output; clients
    # pass relative sub-paths inside it. The orchestrator uses an explicit root.
    root = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_DIR", "./output")
    mcp = _build_mcp_server(root)
    log.info(f"Starting MCP server, allowed_root={root}")
    mcp.run()


if __name__ == "__main__":
    main()
