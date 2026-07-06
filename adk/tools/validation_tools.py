"""Validation tools for Google ADK."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server.server import PbipToolbox  # noqa: E402


def validate_pbip_structure(pbip_dir: str, output_root: str = "./output") -> dict:
    """Validate a .pbip project folder structure and required files.

    Checks: SemanticModel folder, Report folder, .pbip entry file,
    TMDL table files, page.json fields, visual.json fields.

    Args:
        pbip_dir: Absolute path to the .pbip project folder to validate
            (the folder containing *.SemanticModel and *.Report subfolders).
        output_root: Base output directory (default: ./output).

    Returns:
        dict with keys: ok, tool, message, data (tables count, measures count,
        pages count, visuals count, errors list, warnings list), errors.
    """
    return PbipToolbox(output_root).validate_pbip_structure(pbip_dir).as_dict()
