"""ADK tools for Microsoft Fabric deployment (Phase 6)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fabric import fab_cli  # noqa: E402
from fabric.deploy import deploy as _deploy, resolve_layout  # noqa: E402


def check_fab_installation() -> dict:
    """Verify the fab CLI is installed and reachable.

    Returns:
        {"ok": bool, "installed": bool, "version": str, "error": str}
    """
    if not fab_cli.is_installed():
        return {
            "ok":        False,
            "installed": False,
            "version":   "",
            "error":     "fab not on PATH. Install with: pip install ms-fabric-cli",
        }
    v = fab_cli.version()
    return {
        "ok":        True,
        "installed": True,
        "version":   v.data or "",
        "error":     v.error,
    }


def check_fab_auth() -> dict:
    """Report whether the fab CLI is signed in."""
    res = fab_cli.auth_status()
    return {
        "ok":        res.ok,
        "signed_in": bool(res.data and res.data.get("signed_in")),
        "raw":       (res.data or {}).get("raw", ""),
        "error":     res.error,
    }


def list_fabric_workspaces() -> dict:
    """List visible Fabric workspaces."""
    res = fab_cli.list_workspaces()
    return {
        "ok":         res.ok,
        "workspaces": res.data or [],
        "count":      len(res.data or []),
        "error":      res.error,
    }


def list_fabric_items(workspace_name: str) -> dict:
    """List items in a Fabric workspace by name."""
    res = fab_cli.list_items(workspace_name)
    return {
        "ok":        res.ok,
        "workspace": workspace_name,
        "items":     res.data or [],
        "count":     len(res.data or []),
        "error":     res.error,
    }


def preview_pbip_for_deploy(pbip_dir: str) -> dict:
    """Resolve the SemanticModel + Report folders inside a PBIP project.

    Useful as a pre-flight before calling ``deploy_pbip_to_fabric``.
    """
    try:
        layout = resolve_layout(pbip_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **layout.as_dict()}


def deploy_pbip_to_fabric(
    pbip_dir: str,
    workspace: str,
    mode: str = "auto",
    dry_run: bool = True,
    skip_report: bool = False,
    skip_model: bool = False,
) -> dict:
    """Deploy a PBIP project to a Fabric workspace.

    Args:
        pbip_dir:    project root containing *.SemanticModel + *.Report
        workspace:   workspace NAME (resolved by fab CLI)
        mode:        "auto" | "create" | "update" (default: auto)
        dry_run:     when True (default), only print the fab commands —
                     nothing is uploaded. Pass dry_run=False to actually
                     deploy. The default is True to make accidental
                     deploys from an agent loop harmless.
        skip_report: deploy only the SemanticModel
        skip_model:  deploy only the Report
    """
    result = _deploy(
        pbip_dir=pbip_dir,
        workspace=workspace,
        mode=mode,
        dry_run=dry_run,
        skip_report=skip_report,
        skip_model=skip_model,
    )
    return result.as_dict()


__all__ = [
    "check_fab_installation",
    "check_fab_auth",
    "list_fabric_workspaces",
    "list_fabric_items",
    "preview_pbip_for_deploy",
    "deploy_pbip_to_fabric",
]
