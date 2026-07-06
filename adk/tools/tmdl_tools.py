"""TMDL operation tools for Google ADK.

Each function wraps the corresponding PbipToolbox method and is registered
automatically as an ADK FunctionTool when passed to Agent(tools=[...]).
"""
import sys
from pathlib import Path

# Ensure project root is importable when ADK runs from the adk/ subdirectory
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server.server import PbipToolbox  # noqa: E402


def read_csv_schema(csv_path: str, output_root: str = "./output") -> dict:
    """Infer the Power BI schema from a CSV, Excel (.xlsx), or JSON file.

    Args:
        csv_path: Absolute or relative path to a .csv, .xlsx, or .json file.
        output_root: Base output directory (default: ./output).

    Returns:
        dict with keys: ok, tool, message, data (schema with table_name and
        columns list, each having name and dataType), errors.
    """
    return PbipToolbox(output_root).read_csv_schema(csv_path).as_dict()


def write_tmdl_table(output_dir: str, table_def: dict, output_root: str = "./output") -> dict:
    """Write a TMDL table definition file under the semantic model folder.

    Args:
        output_dir: Relative sub-path within output_root, e.g.
            "ProjectName.SemanticModel/definition".
        table_def: Dict with required keys: name (str), columns (list of
            {name, dataType}). Optional: source_path (CSV path for M partition),
            measures (list of {name, expression}).
        output_root: Base output directory (default: ./output).

    Returns:
        dict with keys: ok, tool, message, data (path, table), errors.
    """
    return PbipToolbox(output_root).write_tmdl_table(output_dir, table_def).as_dict()


def write_tmdl_measures(output_dir: str, measures: list, output_root: str = "./output") -> dict:
    """Append DAX measures to one or more existing TMDL table files.

    Args:
        output_dir: Relative sub-path within output_root, e.g.
            "ProjectName.SemanticModel/definition".
        measures: List of measure dicts with required keys: name, expression.
            Optional: table (default "Measures"), displayFolder, formatString.
        output_root: Base output directory (default: ./output).

    Returns:
        dict with keys: ok, tool, message, data (files, appended counts), errors.
    """
    return PbipToolbox(output_root).write_tmdl_measures(output_dir, measures).as_dict()


def read_pbip_schema(pbip_dir: str, output_root: str = "./output") -> dict:
    """Read an existing PBIP folder and return its semantic model schema.

    Use this when editing an existing Power BI project (--pbip mode).

    Args:
        pbip_dir: Absolute or relative path to the PBIP project folder
            (the folder containing *.SemanticModel and *.Report subfolders).
        output_root: Base output directory (default: ./output).

    Returns:
        dict with keys: ok, tool, message, data (schema, existing_measures,
        tables), errors.
    """
    return PbipToolbox(output_root).read_pbip_schema(pbip_dir).as_dict()
