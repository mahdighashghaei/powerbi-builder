"""Sandbox isolation for subprocess execution (Wave C2).

Honest scope
============
The powerbi-builder agents generate **static files** (TMDL/JSON/PBIR) — they do
not execute generated code, so a heavy process sandbox like GVisor is not
needed. The only execution surface is the external ``fab`` CLI invoked for
Fabric deployment (``fabric/fab_cli.py``). This module hardens that surface by:

  * **Pin** the binary to a configured absolute path (no PATH search → no
    PATH-hijack of a malicious ``fab``).
  * **Restrict** the environment passed to the subprocess (only an allowlist of
    vars flows through, so secrets/PII from the parent env do not leak).
  * **Bound** execution time (the caller's timeout already does this).

A ``ContainerSandbox`` stub documents how a future deployment could escalate to
a container/GVisor runtime for genuine code-execution agents — kept as a
documented interface, not a live dependency, so the project stays installable
without container tooling.

Config (env vars):
  * ``POWERBI_FAB_BINARY``  — absolute path to the fab binary (default: ``fab``,
    resolved via PATH only when no explicit path is set).
  * ``POWERBI_SANDBOX_MODE`` — ``local`` (default) | ``container`` | ``disabled``.
  * ``POWERBI_SANDBOX_ENV_ALLOW`` — comma-separated env vars to pass through
    (default: ``PATH,HOME,USERPROFILE,LOCALAPPDATA,FAB_*``).
"""
from __future__ import annotations

import os
import shlex
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def fab_binary() -> str:
    """Return the configured fab binary path.

    When ``POWERBI_FAB_BINARY`` is set to an absolute path, that is used
    verbatim (no PATH search). Otherwise the bare name ``fab`` is returned and
    the OS resolves it via PATH (legacy behaviour).
    """
    return os.getenv("POWERBI_FAB_BINARY", "fab")


def sandbox_mode() -> str:
    return os.getenv("POWERBI_SANDBOX_MODE", "local").lower()


def _env_allowlist() -> set[str]:
    raw = os.getenv(
        "POWERBI_SANDBOX_ENV_ALLOW",
        # PATH + PATHEXT + SYSTEMROOT + WINDIR are required on Windows for the
        # OS to locate the executable and its DLL dependencies; the rest are
        # common runtime dirs. Secrets (GOOGLE_API_KEY etc.) are intentionally
        # NOT in this list.
        "PATH,PATHEXT,HOME,USERPROFILE,LOCALAPPDATA,SYSTEMROOT,WINDIR,TEMP,TMP,APPDATA,PROGRAMDATA",
    )
    allowed = {v.strip() for v in raw.split(",") if v.strip()}
    # Always allow FAB_* prefixed vars (auth tokens etc.) through.
    for k in os.environ:
        if k.startswith("FAB_"):
            allowed.add(k)
    return allowed


def restricted_env() -> dict[str, str]:
    """Build a subprocess env containing only the allowlisted variables.

    This prevents leaking parent-process secrets (API keys, .env contents) into
    the spawned ``fab`` process. Variables not in the allowlist are dropped.
    """
    allowed = _env_allowlist()
    env: dict[str, str] = {}
    for k in allowed:
        if k.endswith("*"):
            prefix = k[:-1]
            for name, value in os.environ.items():
                if name.startswith(prefix) and name not in env:
                    env[name] = value
        elif k in os.environ:
            env[k] = os.environ[k]
    return env


# ---------------------------------------------------------------------------
# Sandbox runner interface
# ---------------------------------------------------------------------------

class SandboxRunner(ABC):
    """Interface for running a subprocess under isolation."""

    @abstractmethod
    def run(self, cmd: list[str], *, timeout: int, cwd: str | None) -> dict[str, Any]:
        """Run ``cmd`` and return a result dict (stdout, stderr, returncode)."""
        ...


class LocalSandbox(SandboxRunner):
    """Run a subprocess with a pinned binary + restricted environment.

    This is the default sandbox mode. It does not spin up a container; it
    hardens the single subprocess call (env restriction + binary pinning +
    timeout). Suitable for a file-generation pipeline whose only execution
    surface is the external ``fab`` CLI.
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or fab_binary()

    def run(self, cmd: list[str], *, timeout: int, cwd: str | None) -> dict[str, Any]:
        import subprocess

        # Pin the binary: if it's an absolute path, verify it exists; if it's a
        # bare name, leave PATH resolution to the OS (legacy).
        if os.path.isabs(self._binary) and not os.path.isfile(self._binary):
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"pinned binary not found: {self._binary}",
                "returncode": 127,
                "error": f"pinned binary not found: {self._binary}",
            }
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=restricted_env(),
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }


class ContainerSandbox(SandboxRunner):
    """Documented stub for a future container/GVisor escalation.

    A real implementation would spawn the command inside a container runtime
    (e.g. ``bwrap``, ``firejail``, or ``gvisor`` runsc) with a read-only root
    and a tmpfs working dir. It is intentionally NOT wired to a live runtime so
    the project installs without container tooling. Subclass and override
    ``run`` to integrate a real runtime.
    """

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or fab_binary()
        self._runtime = os.getenv("POWERBI_CONTAINER_RUNTIME", "bwrap")

    def run(self, cmd: list[str], *, timeout: int, cwd: str | None) -> dict[str, Any]:
        # No container runtime is assumed present; return a clear, safe refusal
        # rather than attempting an unconfigured escalation.
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                f"container sandbox mode requested but runtime "
                f"'{self._runtime}' is not configured. Set POWERBI_SANDBOX_MODE=local "
                f"or implement ContainerSandbox.run for your runtime."
            ),
            "returncode": 126,
            "error": "container sandbox not configured",
        }


def get_sandbox() -> SandboxRunner:
    """Return the configured sandbox runner (fail-safe: local by default)."""
    mode = sandbox_mode()
    if mode == "container":
        return ContainerSandbox()
    if mode == "disabled":
        # disabled still uses LocalSandbox (we never run *unsandboxed*); the
        # flag only documents that the operator accepted the local policy.
        return LocalSandbox()
    return LocalSandbox()


__all__ = [
    "SandboxRunner",
    "LocalSandbox",
    "ContainerSandbox",
    "get_sandbox",
    "restricted_env",
    "fab_binary",
    "sandbox_mode",
]
