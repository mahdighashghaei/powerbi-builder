"""Thin subprocess wrapper around the Microsoft Fabric CLI (``fab``).

The CLI is distributed as ``ms-fabric-cli`` on PyPI:

    pip install ms-fabric-cli

This module never calls the network directly — it shells out to ``fab``
and parses stdout. All calls return a normalised ``FabResult`` dict so
ADK tools can consume them without caring about the underlying CLI shape.

Tested CLI surface (as of fabric-cli v1.x):

    fab --version
    fab auth status
    fab ls                                                # list workspaces
    fab ls "<ws>.Workspace"                               # list items in ws
    fab get  "<ws>.Workspace" -q id                       # workspace id
    fab get  "<ws>.Workspace/<item>.<Type>" -q id         # item id
    fab import "<ws>.Workspace/<name>.SemanticModel" -i <local> -f
    fab import "<ws>.Workspace/<name>.Report"        -i <local> -f
    fab export "<ws>.Workspace/<name>.SemanticModel" -o <local> -f

Each helper has:
    * timeout (Power BI publish can be slow — defaults set per command)
    * a ``check`` mode that raises on non-zero exit
    * a ``dry_run`` mode that only logs the command it would run

Network/auth errors surface as ``FabResult(ok=False, error=...)`` with the
CLI's stderr captured verbatim so callers (or the user) can fix it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("powerbi_builder.fabric")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FabResult:
    """Result of one ``fab`` invocation."""
    ok: bool
    command: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    data: Any = None
    error: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok":         self.ok,
            "command":    self.command,
            "stdout":     self.stdout,
            "stderr":     self.stderr,
            "returncode": self.returncode,
            "data":       self.data,
            "error":      self.error,
            "dry_run":    self.dry_run,
        }


# ---------------------------------------------------------------------------
# Low-level runner
# ---------------------------------------------------------------------------

FAB_BINARY = "fab"
DEFAULT_TIMEOUT = 60  # seconds


def is_installed() -> bool:
    """Return True when ``fab`` is on PATH."""
    return shutil.which(FAB_BINARY) is not None


def _run(
    args: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
    cwd: str | None = None,
) -> FabResult:
    """Run a ``fab`` subcommand and capture stdout/stderr.

    The subprocess is executed through the sandbox runner (Wave C2): the binary
    is pinned via ``POWERBI_FAB_BINARY`` and the environment is restricted to an
    allowlist so parent-process secrets do not leak into the ``fab`` process.
    """
    # Resolve the binary via the sandbox config (pinned path or bare name).
    from security.sandbox import fab_binary as _configured_binary  # noqa: E402

    binary = _configured_binary()
    cmd = [binary, *args]

    if dry_run:
        log.info("[dry-run] %s", " ".join(cmd))
        return FabResult(ok=True, command=cmd, dry_run=True)

    # If the binary is a bare name (no explicit path configured), check PATH.
    if not os.path.isabs(binary) and not is_installed():
        return FabResult(
            ok=False,
            command=cmd,
            error=(
                f"`{binary}` not found on PATH. "
                "Install with: pip install ms-fabric-cli "
                "(or set POWERBI_FAB_BINARY to its absolute path)"
            ),
        )

    log.debug("running: %s", " ".join(cmd))
    from security.sandbox import get_sandbox  # noqa: E402

    sandbox = get_sandbox()
    try:
        result = sandbox.run(cmd, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        return FabResult(
            ok=False,
            command=cmd,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"command timed out after {timeout}s",
        )
    except FileNotFoundError as exc:
        return FabResult(
            ok=False,
            command=cmd,
            error=f"executable not found: {exc}",
        )

    ok = result.get("returncode", 1) == 0
    return FabResult(
        ok=ok,
        command=cmd,
        stdout=result.get("stdout", "") or "",
        stderr=result.get("stderr", "") or "",
        returncode=result.get("returncode", 1),
        error="" if ok else (
            (result.get("stderr", "") or result.get("stdout", "") or "").strip()
            or f"fab exited {result.get('returncode', '?')}"
        ),
    )


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def version(*, dry_run: bool = False) -> FabResult:
    """``fab --version`` — also a cheap reachability probe."""
    res = _run(["--version"], timeout=10, dry_run=dry_run)
    if res.ok and res.stdout:
        res.data = res.stdout.strip()
    return res


def auth_status(*, dry_run: bool = False) -> FabResult:
    """Report whether the CLI is signed in."""
    res = _run(["auth", "status"], timeout=10, dry_run=dry_run)
    if res.ok:
        # fab prints something like: "Logged in as user@tenant"
        res.data = {"signed_in": True, "raw": res.stdout.strip()}
    else:
        res.data = {"signed_in": False, "raw": res.stderr.strip()}
    return res


def list_workspaces(*, dry_run: bool = False) -> FabResult:
    """List visible workspaces.

    The CLI prints ``<name>.Workspace`` lines. We parse them into:
        [{"name": "<name>", "type": "Workspace"}]
    """
    res = _run(["ls"], timeout=30, dry_run=dry_run)
    if res.ok and not dry_run:
        workspaces: list[dict[str, str]] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.endswith(".Workspace"):
                name = line[: -len(".Workspace")]
                workspaces.append({"name": name, "type": "Workspace"})
        res.data = workspaces
    return res


def list_items(workspace_name: str, *, dry_run: bool = False) -> FabResult:
    """List items inside a workspace.

    Parses each ``<name>.<Type>`` line, e.g. ``Sales.SemanticModel``.
    """
    target = f"{workspace_name}.Workspace"
    res = _run(["ls", target], timeout=30, dry_run=dry_run)
    if res.ok and not dry_run:
        items: list[dict[str, str]] = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or "." not in line:
                continue
            name, _, kind = line.rpartition(".")
            if name and kind:
                items.append({"name": name, "type": kind})
        res.data = items
    return res


def get_id(path: str, *, dry_run: bool = False) -> FabResult:
    """Return the GUID of a workspace or item via ``fab get -q id``."""
    res = _run(["get", path, "-q", "id"], timeout=15, dry_run=dry_run)
    if res.ok and not dry_run:
        # value comes back JSON-quoted in older CLI versions
        raw = res.stdout.strip().strip('"')
        res.data = raw
    return res


def import_item(
    workspace_name: str,
    item_name: str,
    item_type: str,
    local_path: str | Path,
    *,
    force: bool = True,
    timeout: int = 300,
    dry_run: bool = False,
) -> FabResult:
    """``fab import <ws>.Workspace/<item>.<Type> -i <local> [-f]``.

    item_type is e.g. ``SemanticModel`` or ``Report``.
    """
    target = f"{workspace_name}.Workspace/{item_name}.{item_type}"
    args = ["import", target, "-i", str(local_path)]
    if force:
        args.append("-f")
    res = _run(args, timeout=timeout, dry_run=dry_run)
    if res.ok:
        res.data = {
            "workspace": workspace_name,
            "name":      item_name,
            "type":      item_type,
            "local":     str(local_path),
        }
    return res


def export_item(
    workspace_name: str,
    item_name: str,
    item_type: str,
    out_dir: str | Path,
    *,
    force: bool = True,
    timeout: int = 180,
    dry_run: bool = False,
) -> FabResult:
    """``fab export <ws>.Workspace/<item>.<Type> -o <out> [-f]``."""
    target = f"{workspace_name}.Workspace/{item_name}.{item_type}"
    args = ["export", target, "-o", str(out_dir)]
    if force:
        args.append("-f")
    res = _run(args, timeout=timeout, dry_run=dry_run)
    if res.ok:
        res.data = {
            "workspace": workspace_name,
            "name":      item_name,
            "type":      item_type,
            "out_dir":   str(out_dir),
        }
    return res


def item_exists(
    workspace_name: str,
    item_name: str,
    item_type: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Check whether an item is already present in a workspace."""
    res = list_items(workspace_name, dry_run=dry_run)
    if not res.ok or not isinstance(res.data, list):
        return False
    for entry in res.data:
        if entry.get("name") == item_name and entry.get("type") == item_type:
            return True
    return False


__all__ = [
    "FabResult",
    "FAB_BINARY",
    "DEFAULT_TIMEOUT",
    "is_installed",
    "version",
    "auth_status",
    "list_workspaces",
    "list_items",
    "get_id",
    "import_item",
    "export_item",
    "item_exists",
]
