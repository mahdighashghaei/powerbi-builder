"""ADK tool wrappers for the A2UI protocol (Wave B2)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adk.a2ui import build_manifest, render_ui_manifest  # noqa: E402

# Re-export render_ui_manifest as the ADK tool (it already returns the envelope).
__all__ = ["render_ui_manifest", "build_manifest"]
