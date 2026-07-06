"""Project scaffold tools for Google ADK.

These tools handle the Power BI project entry files (.pbip, .platform, model.tmdl,
database.tmdl, report.json, pages.json, etc.) that must exist for Power BI Desktop
to open the project. Call create_project_scaffold BEFORE writing tables/pages.
"""
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server import pbir_generator as pb  # noqa: E402
from utils import atomic_write_json, atomic_write_text, ensure_dir  # noqa: E402
from utils import pbip_paths as paths  # noqa: E402

# Power BI TMDL compatibility level. 1702 is the value Power BI Desktop
# expects for the V3 semantic model format (powerBI_V3). Not a magic number:
# it tracks the defaultPowerBIDataSourceVersion declared in model.tmdl.
_COMPATIBILITY_LEVEL = 1702


def create_project_scaffold(
    project_name: str,
    table_name: str,
    output_root: str = "./output",
) -> dict:
    """Create the Power BI project entry files so Desktop can open it.

    Must be called BEFORE write_tmdl_table and write_pbir_page. Creates:
    - ProjectName.pbip (root entry file)
    - ProjectName.SemanticModel/.platform + definition.pbism + model.tmdl + database.tmdl
    - ProjectName.Report/.platform + definition.pbir + definition/version.json + report.json

    Args:
        project_name: The project name (no spaces, e.g. "SalesDashboard").
        table_name: Primary data table name (for model.tmdl PBI_QueryOrder).
        output_root: Base output directory (default: ./output).

    Returns:
        dict with ok, project_root (absolute path to use as pbip_dir for
        validation), and the semantic_model_dir and report_dir sub-paths.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        ensure_dir(root)

        sm_dir = paths.sm_root(root, project_name)
        rep_dir = paths.report_root(root, project_name)
        sm_def = paths.sm_definition(sm_dir)
        rep_def = paths.report_definition(rep_dir)
        ensure_dir(sm_def)
        ensure_dir(rep_def)

        sm_folder = f"{project_name}.SemanticModel"
        rep_folder = f"{project_name}.Report"

        # SemanticModel entry files
        atomic_write_json(paths.sm_platform(sm_dir), pb.platform_properties("SemanticModel", project_name))
        atomic_write_json(paths.sm_definition_pbism(sm_dir), pb.definition_pbism())

        # Report entry files
        atomic_write_json(paths.report_platform(rep_dir), pb.platform_properties("Report", project_name))
        atomic_write_json(paths.report_definition_pbir(rep_dir), pb.definition_pbir(f"../{sm_folder}"))
        atomic_write_json(paths.report_definition_version(rep_dir), pb.version_json())

        # report.json (only if not present)
        rjson = paths.report_json_file(rep_dir)
        if not rjson.exists():
            atomic_write_json(rjson, pb.report_json())

        # model.tmdl skeleton
        model_tmdl = paths.sm_model_file(sm_dir)
        if not model_tmdl.exists():
            # json.dumps safely escapes table_name (handles embedded quotes /
            # backslashes) instead of a raw f-string that would break TMDL.
            query_order = json.dumps([table_name])
            model_txt = (
                "model Model\n"
                "\tculture: en-US\n"
                "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                "\tdiscourageImplicitMeasures\n"
                "\tsourceQueryCulture: en-US\n"
                "\tdataAccessOptions\n"
                "\t\tlegacyRedirects\n"
                "\t\treturnErrorValuesAsNull\n"
                "\n"
                "queryGroup Tables\n"
                "\tannotation PBI_QueryGroupOrder = 0\n"
                "\n"
                "annotation __PBI_TimeIntelligenceEnabled = 0\n"
                f"annotation PBI_QueryOrder = {query_order}\n"
            )
            atomic_write_text(model_tmdl, model_txt)

        # database.tmdl
        db_tmdl = paths.sm_database_file(sm_dir)
        if not db_tmdl.exists():
            db_txt = (
                f"database {project_name}\n"
                f"\tcompatibilityLevel: {_COMPATIBILITY_LEVEL}\n"
                "\tcompatibilityMode: powerBI\n"
            )
            atomic_write_text(db_tmdl, db_txt)

        # Root .pbip entry file
        atomic_write_json(
            paths.pbip_entry_file(root, project_name),
            pb.pbip_entry(rep_folder),
        )

        return {
            "ok": True,
            "tool": "create_project_scaffold",
            "message": f"Created project scaffold for '{project_name}'.",
            "data": {
                "project_root": str(root),
                "semantic_model_dir": f"{sm_folder}/definition",
                "report_dir": f"{rep_folder}/definition",
                "pbip_file": f"{project_name}.pbip",
            },
            "errors": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "tool": "create_project_scaffold",
            "message": str(exc),
            "data": {},
            "errors": [str(exc)],
        }


def finalize_pages_index(
    project_name: str,
    page_ids: list,
    output_root: str = "./output",
) -> dict:
    """Write (or update) the pages.json index that lists page order.

    Must be called AFTER all write_pbir_page calls.

    Args:
        project_name: The project name (e.g. "SalesDashboard").
        page_ids: List of page ID strings in display order.
        output_root: Base output directory (default: ./output). When
            editing a project already created by ``generate_pbip``/
            ``build_report`` (a NESTED ``output/<name>/<name>.Report``
            layout), pass the project's own directory here — NOT the
            generic output root — or this call silently writes a
            disconnected duplicate ``.Report`` folder instead of
            updating the real project. See
            ``utils.pbip_paths.detect_flat_vs_nested_layout_mismatch``.

    Returns:
        dict with ok, path to pages.json.
    """
    try:
        root = Path(output_root).expanduser().resolve()
        mismatch = paths.detect_flat_vs_nested_layout_mismatch(root, project_name)
        if mismatch:
            return {
                "ok": False,
                "tool": "finalize_pages_index",
                "message": mismatch,
                "data": {},
                "errors": [mismatch],
            }
        rep_dir = paths.report_root(root, project_name)
        pages_path = paths.report_pages_metadata(rep_dir)
        atomic_write_json(pages_path, pb.pages_metadata(page_ids))
        return {
            "ok": True,
            "tool": "finalize_pages_index",
            "message": f"Written pages.json with {len(page_ids)} page(s).",
            "data": {"path": str(pages_path), "page_ids": page_ids},
            "errors": [],
        }
    except Exception as exc:
        return {
            "ok": False,
            "tool": "finalize_pages_index",
            "message": str(exc),
            "data": {},
            "errors": [str(exc)],
        }
