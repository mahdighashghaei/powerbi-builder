"""PBIR (Power BI Report) operation tools for Google ADK."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server.server import PbipToolbox  # noqa: E402
from utils import pbip_paths as _paths  # noqa: E402


def _project_name_from_output_dir(output_dir: str) -> str | None:
    """Derive the project name from an ``output_dir`` like
    "ProjectName.Report/definition" -- used only for the defensive
    flat-vs-nested layout check below; never raises."""
    try:
        first_segment = output_dir.replace("\\", "/").split("/")[0]
        for suffix in (".Report", ".SemanticModel"):
            if first_segment.endswith(suffix):
                return first_segment[: -len(suffix)]
        return None
    except Exception:  # noqa: BLE001
        return None


def _layout_mismatch_result(tool: str, output_root: str, output_dir: str) -> dict | None:
    project_name = _project_name_from_output_dir(output_dir)
    if not project_name:
        return None
    mismatch = _paths.detect_flat_vs_nested_layout_mismatch(output_root, project_name)
    if not mismatch:
        return None
    return {"ok": False, "tool": tool, "message": mismatch, "data": {}, "errors": [mismatch]}


def write_pbir_page(output_dir: str, page_def: dict, output_root: str = "./output") -> dict:
    """Write a PBIR report page (page.json) and its visuals.

    Args:
        output_dir: Relative sub-path within output_root, e.g.
            "ProjectName.Report/definition".
        page_def: Dict with required keys: id (str), displayName (str).
            Optional: width (default 1280), height (default 720),
            visuals (list of {id, visualType, title, x, y, width, height,
            z, tabOrder, queryState}).
            Valid visualTypes: card, barChart, columnChart, lineChart,
            pieChart, table, matrix, kpi, map, slicer, donutChart, scatterChart.
        output_root: Base output directory (default: ./output). When
            editing a project already created by ``generate_pbip``/
            ``build_report`` (a NESTED ``output/<name>/<name>.Report``
            layout), pass the project's own directory here — NOT the
            generic output root — or this call silently writes a
            disconnected duplicate ``.Report`` folder instead of
            updating the real project.

    Returns:
        dict with keys: ok, tool, message, data (page_json path, visuals
        paths, page_id), errors.
    """
    mismatch_result = _layout_mismatch_result("write_pbir_page", output_root, output_dir)
    if mismatch_result:
        return mismatch_result
    return PbipToolbox(output_root).write_pbir_page(output_dir, page_def).as_dict()


def write_theme_json(output_dir: str, theme: dict = None, output_root: str = "./output") -> dict:
    """Write the report theme.json. Uses the default corporate theme if none given.

    Unlike ``write_pbir_page`` and every other write_*/add_* PBIR tool,
    the theme file lives ALONGSIDE ``definition/``, not inside it --
    ``report.json``'s ``customTheme`` reference resolves relative to the
    bare ``.Report`` folder. Passing a ``.../definition``-suffixed
    ``output_dir`` here writes a theme.json Power BI Desktop never reads.

    Args:
        output_dir: The project's ``.Report`` folder relative to
            output_root, e.g. "ProjectName.Report" -- no ``/definition``
            suffix (contrast with ``write_pbir_page`` above).
        theme: Optional theme dict. If omitted, the bundled default theme is used.
        output_root: Base output directory (default: ./output). See
            ``write_pbir_page`` — the same nested-vs-flat layout caveat
            applies here.

    Returns:
        dict with keys: ok, tool, message, data (path, name), errors.
    """
    mismatch_result = _layout_mismatch_result("write_theme_json", output_root, output_dir)
    if mismatch_result:
        return mismatch_result
    return PbipToolbox(output_root).write_theme_json(output_dir, theme).as_dict()
