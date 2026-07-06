"""Shared zip helper for packaging a generated PBIP project folder.

Used by ``adk/plugin.py`` (artifact saving for the ADK web download button)
and ``mcp_server`` (kept dependency-free from ``adk/`` — this module lives in
the shared ``utils`` layer so neither side needs to import the other).
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

# Internal-only artifacts written into the PBIP project root during a build
# run. These are consumed by the builder's own learning/explainability loop
# and must NEVER ship inside the user-facing downloadable ZIP — the user only
# needs the Power BI files (model, report, .pbip, README, ...). Any filename
# appearing here is skipped at every path depth when archiving the project.
EXCLUDED_INTERNAL_FILES: frozenset[str] = frozenset({
    "build.spec.json",
    "decisions.log.json",
    "feedback_history.json",
    "learning_memory.json",
})


def zip_project_dir(pbip_root: str, project_name: str) -> bytes:
    """Zip a generated PBIP project folder into a bytes blob.

    Returns empty bytes if the path is invalid or empty. ``project_name``
    is accepted for call-site symmetry with callers that already have it on
    hand, but the archive's internal paths are derived from ``pbip_root``
    itself (relative to its parent), not from ``project_name``.

    Internal-only files listed in :data:`EXCLUDED_INTERNAL_FILES`
    (e.g. ``build.spec.json``, ``decisions.log.json``,
    ``feedback_history.json``, ``learning_memory.json``) are intentionally
    omitted from the archive — they are build-time metadata for the agent's
    own use, not part of the Power BI project the user downloads and opens.
    """
    if not pbip_root:
        return b""
    root = Path(pbip_root)
    if not root.is_dir():
        return b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in root.rglob("*"):
            if fpath.is_file() and fpath.name not in EXCLUDED_INTERNAL_FILES:
                arcname = fpath.relative_to(root.parent)
                zf.write(fpath, arcname)
    return buf.getvalue()
