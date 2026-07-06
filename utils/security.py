"""Security & I/O utilities for the PowerBI Builder.

This module centralises every security-sensitive operation used across the
project so that all agents and the MCP server funnel through ONE place:

* Path resolution that blocks traversal attacks (`..`, absolute roots, symlinks).
* JSON validation before anything is written to disk.
* Centralised audit logger (file + console) so agent actions are always recorded.
* Idempotent, atomic file writes.

Design goal: it must be impossible to write *anywhere* outside the configured
output root, no matter what an agent or an attacker-supplied path provides.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Secure path resolution  (path-traversal defence)
# ---------------------------------------------------------------------------


class PathSecurityError(ValueError):
    """Raised when a path tries to escape its allowed root directory."""


def _resolve_root(root: str | os.PathLike[str]) -> Path:
    """Return an absolute, real (no symlinks) path for ``root``."""
    p = Path(root).expanduser().resolve()
    return p


def safe_join(root: str | os.PathLike[str], *parts: str) -> Path:
    """Join ``parts`` under ``root`` and refuse escapes.

    Defences implemented (defence in depth):
      1. Lexical check: reject ``..`` segments before resolution.
      2. Resolved-path containment check after symlink expansion.
      3. Reject Windows drive-absolute paths that point outside the root.

    Raises:
        PathSecurityError: if the resulting path is not inside ``root``.
    """
    if not parts:
        raise PathSecurityError("safe_join requires at least one path part")

    root_path = _resolve_root(root)

    # --- Defence 1: lexical segment check ------------------------------------
    for part in parts:
        # normalise separators so "../" and "..\\" are both caught
        norm = str(part).replace("\\", "/")
        if ".." in norm.split("/"):
            raise PathSecurityError(
                f"Path traversal ('..') is not allowed in path segment: {part!r}"
            )

    candidate = (root_path.joinpath(*parts)).resolve()

    # --- Defence 2: containment after resolution -----------------------------
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise PathSecurityError(
            f"Refusing to resolve path outside output root.\n"
            f"  root={root_path}\n  target={candidate}"
        ) from exc

    return candidate


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """Create ``path`` (and parents) if missing. Returns the resolved Path."""
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 2. JSON validation
# ---------------------------------------------------------------------------


class JSONValidationError(ValueError):
    """Raised when JSON content fails validation."""


def validate_json_string(raw: str) -> Any:
    """Parse ``raw`` as JSON, raising :class:`JSONValidationError` on failure."""
    if not isinstance(raw, str):
        raise JSONValidationError(f"Expected str, got {type(raw).__name__}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JSONValidationError(f"Invalid JSON: {exc.msg} (line {exc.lineno})") from exc


def _json_default(obj: Any) -> Any:
    """Fallback encoder for values ``json.dumps`` doesn't know natively.

    Callers across this project build result dicts from pandas/numpy
    profiling (schema stats, quality scores) without always remembering to
    cast every scalar to a native Python type. That's harmless when a dict
    is passed in-process, but breaks the instant something actually JSON
    -encodes it (e.g. the MCP stdio transport) -- ``.item()`` is the
    standard numpy-scalar-to-native-Python conversion and covers int64,
    float64, bool_, etc. without requiring numpy as an import here.
    """
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001 — fall through to the TypeError below
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def serialize_json(data: Any) -> str:
    """Serialise ``data`` to deterministic, pretty JSON."""
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=_json_default)


# ---------------------------------------------------------------------------
# 3. Atomic file writes (so a crash never leaves a half-written file)
# ---------------------------------------------------------------------------


def atomic_write_text(path: str | os.PathLike[str], content: str) -> Path:
    """Write ``content`` to ``path`` atomically (write-temp + os.replace)."""
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    # write to a temp file in the SAME directory so os.replace is atomic
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp_name, p)
    except Exception:
        # cleanup the temp file on any failure
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> Path:
    """Validate-then-serialise-then-atomic-write ``data`` as JSON."""
    payload = serialize_json(data)
    return atomic_write_text(path, payload)


# ---------------------------------------------------------------------------
# 4. Audit logging  (security requirement: log to FILE, not stdout only)
# ---------------------------------------------------------------------------


class AuditLogger:
    """Centralised logger writing to the console (stdout or stderr) and a file.

    The file handler is created once and attached to the root ``powerbi_builder``
    logger so that every agent / MCP tool inherits it.

    Reconfiguration is supported: if :meth:`configure` is called again with a
    log file (e.g. by ``main.py`` after the module-level ``AuditLogger.get``
    calls in imported modules), the previous handlers are cleared and the new
    ones are attached. This matters because import order can trigger an early
    ``get()`` before ``configure()`` has been called with a real log file.
    """

    LOGGER_NAME = "powerbi_builder"

    # Sticky across configure() calls: once a real MCP stdio server sets this
    # True, a later configure() call that doesn't mention mcp_stdio at all
    # (e.g. OrchestratorAgent.__init__, called fresh for every generate_pbip
    # request) must NOT silently flip logging back onto stdout mid-run --
    # that corrupts the JSON-RPC transport the server is actively using.
    #
    # The same stickiness now applies to the file handler: a later
    # configure() with log_file=None (again, OrchestratorAgent.__init__)
    # must NOT drop the FileHandler that main() installed, or every agent
    # log line after the first tool call vanishes from the audit log file.
    _mcp_stdio: bool = False

    @classmethod
    def configure(
        cls,
        log_file: str | os.PathLike[str] | None = None,
        level: str = "INFO",
        mcp_stdio: bool | None = None,
    ) -> logging.Logger:
        """Configure the project logger.

        Safe to call multiple times; later calls replace earlier handlers.

        Args:
            log_file: Path to the audit log. If None, the existing file
                handler (if any) is PRESERVED -- this mirrors how
                ``mcp_stdio=None`` means "don't touch the stdio mode": a
                re-entrant ``configure()`` call (e.g.
                ``OrchestratorAgent.__init__``, invoked fresh for every
                ``generate_pbip`` request) must NOT drop the FileHandler
                that ``main()`` installed, or every agent log line after
                the first tool call vanishes from the audit log file. Pass
                an explicit path to switch files; pass None to keep the
                current one.
            level: Logging level name (e.g. ``"INFO"``, ``"DEBUG"``).
            mcp_stdio: When True, route console output to ``sys.stderr`` instead
                of ``sys.stdout``. This is REQUIRED for an MCP stdio server,
                where ``stdout`` *is* the JSON-RPC transport -- any stray log
                line on stdout corrupts the protocol. CLI / web entry points
                leave this False. ``None`` (the default) means "don't change
                the current mode" -- e.g. code paths that reconfigure logging
                for an unrelated reason (a new log file, a new level) without
                ever intending to touch stdio routing, so a stdio server
                started via ``main()`` stays in stderr mode for its whole
                lifetime regardless of how many times something else calls
                ``configure()`` afterward.
        """
        logger = logging.getLogger(cls.LOGGER_NAME)

        if mcp_stdio is not None:
            cls._mcp_stdio = mcp_stdio

        # Separate existing handlers into file handlers (which we may keep)
        # and everything else (which we always refresh). This is the crux of
        # the log_file=None preservation: a re-entrant configure() call must
        # not close the FileHandler an earlier caller (main()) installed.
        existing_file_handlers: list[logging.FileHandler] = []
        for h in list(logger.handlers):
            if isinstance(h, logging.FileHandler):
                existing_file_handlers.append(h)
            else:
                logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass

        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False

        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler -- never write to stdout under MCP stdio: stdout IS
        # the JSON-RPC transport, so a log line would break the protocol.
        stream = sys.stderr if cls._mcp_stdio else sys.stdout
        # Windows consoles default stdout/stderr to the system codepage
        # (e.g. cp1252), which can't encode characters some log messages
        # use (e.g. "->" arrows in DataCleanerAgent's quality-score lines).
        # That raises inside the logging module itself, which silently
        # drops the record after printing "--- Logging error ---" -- force
        # UTF-8 with replacement so a stray character never loses a log line.
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        sh = logging.StreamHandler(stream)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        # File handler (audit trail)
        if log_file:
            # Switching to a new file (or replacing the default): close any
            # old file handlers we carried over and attach the new one.
            log_path = Path(log_file).expanduser().resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            for h in existing_file_handlers:
                try:
                    h.close()
                except Exception:
                    pass
        else:
            # log_file=None means "keep whatever file handler is already
            # configured" -- mirrors how mcp_stdio=None means "don't touch".
            # Without this, a re-entrant configure() (e.g. the orchestrator
            # constructor, invoked per generate_pbip request) silently drops
            # the FileHandler main() set up, and every subsequent agent log
            # line goes only to the console, never to the audit log file.
            for h in existing_file_handlers:
                # Re-attach (it was never removed) and ensure the formatter
                # matches the current configuration.
                h.setFormatter(fmt)
                if h not in logger.handlers:
                    logger.addHandler(h)

        return logger

    @classmethod
    def get(cls, name: str | None = None) -> logging.Logger:
        """Return a child logger, ensuring the logger is minimally configured.

        If ``configure`` has not been called yet, a stdout-only default is set
        up so imported modules can log at import time. ``main.py`` will later
        call ``configure`` with a real log file, which replaces this default.
        """
        logger = logging.getLogger(cls.LOGGER_NAME)
        if not logger.handlers:
            cls.configure()
        return logger if name is None else logger.getChild(name)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (used in audit entries)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
