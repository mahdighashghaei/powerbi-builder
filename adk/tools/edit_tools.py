"""ADK tools for editing existing PBIP projects (edit/delete visuals, pages,
measures, and rewriting table data sources).

These complement the additive tools (``add_measure``, ``add_visual``,
``add_page``) with the ability to *modify* and *remove* existing elements,
and to switch a table's data source (e.g. CSV → SQL Server).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import atomic_write_json, atomic_write_text, safe_join  # noqa: E402
from utils.security import PathSecurityError  # noqa: E402


def _resolve_pbip(pbip_dir: str) -> Path:
    p = Path(pbip_dir).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"PBIP dir not found: {p}")
    return p


# Geometry keys that PBIR stores under the nested ``position`` object (written
# by pbir_generator.visual_json), NOT at the visual.json top level. The old
# update_visual merged these at the root, so moves silently added stray keys
# (e.g. a top-level ``x``) that Power BI Desktop ignores.
_POSITION_KEYS = {"x", "y", "z", "width", "height", "tabOrder", "tab_order"}


def _set_visual_title(visual: dict, text: str) -> None:
    """Set the visual title, matching pbir_generator.visual_json's title format.

    Writes ``visual.visual.objects.title[0].properties.text.expr.Literal.Value``
    with the text wrapped in single quotes, plus ``show=true`` — exactly the
    shape pbir_generator emits (see pbir_generator.py:368-378).
    """
    visual.setdefault("visual", {}).setdefault("objects", {})
    visual["visual"]["objects"]["title"] = [
        {
            "properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}},
                "text": {"expr": {"Literal": {"Value": f"'{text}'"}}},
            }
        }
    ]


def update_visual(pbip_dir: str, page_id: str, visual_id: str,
                  changes: dict[str, Any], output_root: str = "./output") -> dict:
    """Update fields on an existing visual.json (e.g. position, title, visualType).

    Args:
        changes: key-value pairs to apply. Position keys (``x``, ``y``, ``z``,
            ``width``, ``height``, ``tabOrder``/``tab_order``) are written to
            the nested ``position`` object where PBIR actually stores geometry.
            ``title`` updates the visual title; ``visualType`` changes the
            visual kind; any other key is merged at the visual.json top level.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        rep_dirs = list((pbip).glob("*.Report"))
        if not rep_dirs:
            return {"ok": False, "errors": ["No *.Report folder found"]}
        vjson = rep_dirs[0] / "definition" / "pages" / page_id / "visuals" / visual_id / "visual.json"
        if not vjson.is_file():
            return {"ok": False, "errors": [f"Visual '{visual_id}' not found on page '{page_id}'"]}
        visual = json.loads(vjson.read_text(encoding="utf-8"))
        for key, val in changes.items():
            if key in _POSITION_KEYS:
                # PBIR geometry lives under the nested "position" object.
                visual.setdefault("position", {})
                pk = "tabOrder" if key == "tab_order" else key
                visual["position"][pk] = val
            elif key == "position" and isinstance(val, dict):
                visual.setdefault("position", {}).update(val)
            elif key == "title":
                _set_visual_title(visual, str(val))
            elif key == "visualType":
                visual.setdefault("visual", {})["visualType"] = val
            else:
                visual[key] = val
        atomic_write_json(vjson, visual)
        return {"ok": True, "visual_id": visual_id, "changes": list(changes.keys()),
                "position": visual.get("position", {}), "path": str(vjson)}
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"update_visual failed: {exc}"]}


def delete_visual(pbip_dir: str, page_id: str, visual_id: str,
                  output_root: str = "./output") -> dict:
    """Delete a visual (its folder + visual.json) from a page."""
    try:
        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        rep_dirs = list(pbip.glob("*.Report"))
        if not rep_dirs:
            return {"ok": False, "errors": ["No *.Report folder found"]}
        vdir = rep_dirs[0] / "definition" / "pages" / page_id / "visuals" / visual_id
        if not vdir.is_dir():
            return {"ok": False, "errors": [f"Visual '{visual_id}' not found on page '{page_id}'"]}
        import shutil
        shutil.rmtree(vdir)
        return {"ok": True, "deleted": str(vdir)}
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"delete_visual failed: {exc}"]}


def delete_page(pbip_dir: str, page_id: str,
                output_root: str = "./output") -> dict:
    """Delete a page folder and remove it from pages.json.

    ``pages.json``'s real schema (see ``mcp_server/pbir_generator.pages_metadata``)
    is ``{"$schema": ..., "pageOrder": [<id>, ...], "activePageName": <id>}`` --
    a flat list of id STRINGS, not a "pages" list of dicts. A prior version of
    this function filtered a non-existent "pages" key, which silently did
    nothing: the deleted page's id stayed in ``pageOrder`` forever, so Power
    BI Desktop would try to load a page folder that no longer exists and
    fail to open the report.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        rep_dirs = list(pbip.glob("*.Report"))
        if not rep_dirs:
            return {"ok": False, "errors": ["No *.Report folder found"]}
        page_dir = rep_dirs[0] / "definition" / "pages" / page_id
        if not page_dir.is_dir():
            return {"ok": False, "errors": [f"Page '{page_id}' not found"]}
        import shutil
        shutil.rmtree(page_dir)
        # remove from pages.json's pageOrder (the actual schema)
        pages_json = rep_dirs[0] / "definition" / "pages" / "pages.json"
        if pages_json.is_file():
            pages = json.loads(pages_json.read_text(encoding="utf-8"))
            order = [pid for pid in pages.get("pageOrder", []) if pid != page_id]
            pages["pageOrder"] = order
            if pages.get("activePageName") == page_id:
                pages["activePageName"] = order[0] if order else None
            atomic_write_json(pages_json, pages)
        return {"ok": True, "deleted": str(page_dir)}
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"delete_page failed: {exc}"]}


def edit_measure(pbip_dir: str, measure_name: str, new_expression: str,
                 table_name: str = "", output_root: str = "./output") -> dict:
    """Edit an existing DAX measure's expression in its TMDL file.

    If ``table_name`` is omitted, searches all table TMDL files for the measure.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        sm_dirs = list(pbip.glob("*.SemanticModel"))
        if not sm_dirs:
            return {"ok": False, "errors": ["No *.SemanticModel folder found"]}
        tables_dir = sm_dirs[0] / "definition" / "tables"
        if not tables_dir.is_dir():
            return {"ok": False, "errors": ["No definition/tables/ folder"]}

        # find the TMDL file containing the measure
        target_file = None
        candidates = [tables_dir / f"{table_name}.tmdl"] if table_name else list(tables_dir.glob("*.tmdl"))
        for tf in candidates:
            if not tf.is_file():
                continue
            txt = tf.read_text(encoding="utf-8")
            # match: measure 'Name' = ... (single line or multi-line until next measure/column)
            if f"measure '{measure_name}'" in txt:
                target_file = tf
                break
        if target_file is None:
            return {"ok": False, "errors": [f"Measure '{measure_name}' not found in any table TMDL"]}

        txt = target_file.read_text(encoding="utf-8")
        # Replace the expression after "measure 'Name' = "
        # The expression runs until the next top-level \tmeasure / \tcolumn / \n\tannotation
        pattern = re.compile(
            r"(measure\s+'" + re.escape(measure_name) + r"'\s*=\s*)([^\n]*(?:\n\t+[^\n]*)*)",
            re.IGNORECASE,
        )
        new_txt = pattern.sub(
            lambda m: m.group(1) + new_expression,
            txt,
            count=1,
        )
        if new_txt == txt:
            return {"ok": False, "errors": ["Could not locate the measure expression to replace"]}
        atomic_write_text(target_file, new_txt)
        return {"ok": True, "measure": measure_name, "table_file": target_file.name}
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"edit_measure failed: {exc}"]}


def delete_measure(pbip_dir: str, measure_name: str,
                   table_name: str = "", output_root: str = "./output") -> dict:
    """Delete a DAX measure from its TMDL file."""
    try:
        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        sm_dirs = list(pbip.glob("*.SemanticModel"))
        if not sm_dirs:
            return {"ok": False, "errors": ["No *.SemanticModel folder found"]}
        tables_dir = sm_dirs[0] / "definition" / "tables"
        candidates = [tables_dir / f"{table_name}.tmdl"] if table_name else list(tables_dir.glob("*.tmdl"))
        for tf in candidates:
            if not tf.is_file():
                continue
            txt = tf.read_text(encoding="utf-8")
            if f"measure '{measure_name}'" not in txt:
                continue
            # remove the measure block (header + indented properties until next \tmeasure/\tcolumn/\n)
            pattern = re.compile(
                r"\t*measure\s+'" + re.escape(measure_name) + r"'\s*=\s*[^\n]*(?:\n\t+[^\n]*)*",
                re.IGNORECASE,
            )
            new_txt = pattern.sub("", txt, count=1)
            # clean up any double blank lines
            new_txt = re.sub(r"\n{3,}", "\n\n", new_txt)
            atomic_write_text(tf, new_txt)
            return {"ok": True, "deleted_measure": measure_name, "table_file": tf.name}
        return {"ok": False, "errors": [f"Measure '{measure_name}' not found"]}
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"delete_measure failed: {exc}"]}


def edit_table_source(pbip_dir: str, table_name: str, source_type: str,
                      connection_params: dict[str, Any] | None = None,
                      source_path: str = "",
                      output_root: str = "./output") -> dict:
    """Rewrite a table's M partition to point at a new data source.

    This lets you switch a table from CSV to SQL Server (or Excel/Web) without
    recreating the whole model. The column definitions are preserved; only the
    partition block is regenerated.
    """
    try:
        from mcp_server.server import _build_m_partition
        from utils.tmdl_parser import read_semantic_model

        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        sm_dirs = list(pbip.glob("*.SemanticModel"))
        if not sm_dirs:
            return {"ok": False, "errors": ["No *.SemanticModel folder found"]}
        tf = sm_dirs[0] / "definition" / "tables" / f"{table_name}.tmdl"
        if not tf.is_file():
            return {"ok": False, "errors": [f"Table '{table_name}' TMDL not found"]}

        # read existing columns from the TMDL
        model = read_semantic_model(sm_dirs[0])
        table_info = None
        for t in model.get("tables", []):
            if t["table_name"] == table_name:
                table_info = t
                break
        if not table_info:
            return {"ok": False, "errors": [f"Table '{table_name}' not found in model"]}
        cols = table_info.get("columns", [])

        # build the new partition block
        new_partition = _build_m_partition(
            table_name, source_path, cols,
            source_type=source_type, connection_params=connection_params,
        )

        # replace the existing partition block in the TMDL
        txt = tf.read_text(encoding="utf-8")
        # remove old partition block (from \tpartition to the next \n\n or end)
        new_txt = re.sub(
            r"\n\tpartition\s+" + re.escape(table_name) + r"\s*=\s*m.*?(?=\n\tannotation|\Z)",
            new_partition,
            txt,
            flags=re.DOTALL,
            count=1,
        )
        if new_txt == txt:
            # no existing partition — append
            new_txt = txt.rstrip() + "\n" + new_partition
        atomic_write_text(tf, new_txt)
        return {
            "ok": True,
            "table": table_name,
            "new_source_type": source_type,
            "table_file": tf.name,
        }
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"edit_table_source failed: {exc}"]}


def relayout_page(pbip_dir: str, page_id: str,
                  page_width: int = 1280, page_height: int = 720,
                  output_root: str = "./output") -> dict:
    """Re-apply the smart zone-based layout to all visuals on an existing page.

    Reads every ``visual.json`` on the page, classifies each by its visualType
    (cards → top strip, slicers → right column, charts → centre, tables →
    bottom), computes non-overlapping positions via ``utils.layout_engine``,
    and writes the updated ``position`` block back to each file. Use this after
    adding several visuals to a page (or whenever visuals overlap) to clean up
    the layout in one call.

    Args:
        pbip_dir: PBIP project folder (relative to output_root).
        page_id:  Page folder name to relayout.
        page_width, page_height: Canvas dimensions (default 1280×720).
    """
    try:
        from utils.layout_engine import build_layout

        root = Path(output_root).expanduser().resolve()
        pbip = safe_join(root, pbip_dir)
        rep_dirs = list(pbip.glob("*.Report"))
        if not rep_dirs:
            return {"ok": False, "errors": ["No *.Report folder found"]}
        visuals_dir = rep_dirs[0] / "definition" / "pages" / page_id / "visuals"
        if not visuals_dir.is_dir():
            return {"ok": False, "errors": [f"Page '{page_id}' has no visuals folder"]}

        # Collect existing visual ids + types from disk.
        specs: list[dict[str, str]] = []
        vjson_paths: list[Path] = []
        for vdir in sorted(visuals_dir.iterdir(), key=lambda p: p.name):
            if not vdir.is_dir():
                continue
            vjson = vdir / "visual.json"
            if not vjson.is_file():
                continue
            try:
                v = json.loads(vjson.read_text(encoding="utf-8"))
            except Exception:
                continue
            vtype = v.get("visual", {}).get("visualType", "card")
            specs.append({"id": vdir.name, "type": vtype})
            vjson_paths.append(vjson)

        if not specs:
            return {"ok": True, "page_id": page_id, "visual_count": 0,
                    "repositioned": [], "zones": {}}

        positions = build_layout(specs, page_width, page_height)
        repositioned: list[str] = []
        for vjson, spec in zip(vjson_paths, specs):
            pos = positions.get(spec["id"])
            if pos is None:
                continue
            v = json.loads(vjson.read_text(encoding="utf-8"))
            v["position"] = {
                "x": float(pos["x"]), "y": float(pos["y"]), "z": int(pos["z"]),
                "height": float(pos["height"]), "width": float(pos["width"]),
                "tabOrder": int(pos["tabOrder"]),
            }
            atomic_write_json(vjson, v)
            repositioned.append(spec["id"])

        # Report the zones for transparency (mirrors plan_page_layout output).
        from utils.layout_engine import _is_card, _is_chart, _is_slicer, _is_table
        zones: dict[str, list[str]] = {
            "cards": [], "charts": [], "tables": [], "slicers": [], "other": [],
        }
        for s in specs:
            if _is_card(s["type"]):
                zones["cards"].append(s["id"])
            elif _is_chart(s["type"]):
                zones["charts"].append(s["id"])
            elif _is_table(s["type"]):
                zones["tables"].append(s["id"])
            elif _is_slicer(s["type"]):
                zones["slicers"].append(s["id"])
            else:
                zones["other"].append(s["id"])

        return {
            "ok": True,
            "page_id": page_id,
            "visual_count": len(specs),
            "repositioned": repositioned,
            "zones": {k: v for k, v in zones.items() if v},
            "page_width": page_width,
            "page_height": page_height,
        }
    except (FileNotFoundError, PathSecurityError) as exc:
        return {"ok": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"ok": False, "errors": [f"relayout_page failed: {exc}"]}


__all__ = [
    "update_visual",
    "delete_visual",
    "delete_page",
    "edit_measure",
    "delete_measure",
    "edit_table_source",
    "relayout_page",
]
