"""Persistent MCP client for calling ``mcp_server/server.py``'s real,
stdio-transported MCP tools from inside the ADK agent.

Why this exists
----------------
``adk/tools/highlevel_tools.py::generate_pbip`` used to call
``mcp_server.highlevel.generate_pbip`` as a bare in-process Python import --
correct output, but no MCP protocol was ever actually exercised. This module
gives that same tool call a genuine client -> subprocess-server round trip
over the Model Context Protocol, while keeping the ADK-facing function's
signature, artifact-upload handling, and callback identity unchanged (see
the tool's own module docstring for why a full ``McpToolset`` swap on
``Agent(tools=[...])`` is the wrong shape for this specific tool).

The session is a **module-level singleton, connected lazily on first use and
kept open** for the process lifetime -- spawning a fresh Python process
(which imports the whole ``agents`` package + pandas) on every single tool
call would add several seconds of latency per call. This mirrors the
project's fail-safe philosophy: a bounded timeout guards only the initial
connection handshake (a local subprocess starting up), never the build
itself (which can legitimately take minutes) -- a stuck/uninstallable `mcp`
package must fail fast with a clear message, not hang the chat turn.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from utils import AuditLogger

log = AuditLogger.get("adk.mcp_client")

_PROJECT_ROOT = Path(__file__).parent.parent

# Only the initial subprocess handshake is bounded -- NOT tool execution
# (a generate_pbip build can legitimately take minutes). This is a new,
# local-subprocess-only timeout, unrelated to the LLM-call timeouts removed
# elsewhere in this project.
_CONNECT_TIMEOUT_SECONDS = 15.0

_stack: AsyncExitStack | None = None
_session: Any = None
_loop: asyncio.AbstractEventLoop | None = None
_connect_lock: asyncio.Lock | None = None


async def _connect() -> Any:
    """Start (if needed) the ``mcp_server.server`` subprocess and return a
    live, initialized :class:`mcp.ClientSession`. Idempotent -- safe to call
    on every invocation; reuses the existing session once connected.

    A session (and the anyio task group/streams underneath it) is bound to
    the asyncio event loop it was created in. The real ``adk web`` process
    runs one continuous loop for its whole life, so this is a non-issue
    there -- but callers that wrap each call in their own ``asyncio.run()``
    (each of which spins up and tears down its own loop, e.g. the test
    suite) would otherwise reuse a session tied to an already-closed loop
    and get an ``anyio.ClosedResourceError`` on the second call. Detect a
    loop change and transparently reconnect rather than reusing stale
    streams.
    """
    global _stack, _session, _loop, _connect_lock

    current_loop = asyncio.get_running_loop()
    if _session is not None and _loop is current_loop:
        return _session

    if _session is not None and _loop is not current_loop:
        # Stale session from a previous (now-closed) loop -- don't try to
        # gracefully aclose() it (that requires the SAME loop/task that
        # opened it); just drop the reference and reconnect fresh.
        log.info("[mcp_client] event loop changed, reconnecting")
        _stack = None
        _session = None
        _connect_lock = None

    if _connect_lock is None:
        _connect_lock = asyncio.Lock()

    async with _connect_lock:
        if _session is not None and _loop is current_loop:  # lost the race
            return _session

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            cwd=str(_PROJECT_ROOT),
            # This is a trusted, local subprocess re-launching the SAME
            # codebase (not an untrusted external server) -- inherit the
            # full parent environment rather than the MCP SDK's default
            # curated subset. Without this, a caller that blanks LLM_*
            # vars to force deterministic, no-network behavior (e.g. the
            # test suite) would silently have no effect on the child: the
            # default env doesn't include those keys either way, so the
            # child would fall back to whatever's in .env regardless of
            # what the parent process explicitly set.
            env=dict(os.environ),
        )

        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(server_params)),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT_SECONDS)
        except Exception:
            await stack.aclose()
            raise

        _stack = stack
        _session = session
        _loop = current_loop
        log.info("[mcp_client] connected to mcp_server.server subprocess")
        return _session


async def call_mcp_tool(name: str, **kwargs: Any) -> dict[str, Any]:
    """Call ``name`` on the real MCP server over stdio and return its
    parsed JSON result as a dict.

    Fail-safe: any connection, timeout, or protocol error is caught and
    turned into a normal ``{"ok": False, ...}`` result dict -- never raises
    into the calling tool function, and never hangs indefinitely (only the
    connection handshake is timeout-bounded; a slow build is expected and
    allowed to run to completion).
    """
    try:
        session = await _connect()
        result = await session.call_tool(name, kwargs)
    except Exception as exc:
        log.exception(f"[mcp_client] round-trip to '{name}' failed")
        return {
            "ok": False,
            "tool": name,
            "message": f"MCP round-trip failed: {exc}",
            "data": {},
            "errors": [str(exc)],
        }

    text_parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    if not text_parts:
        return {
            "ok": False,
            "tool": name,
            "message": "MCP call returned no text content",
            "data": {},
            "errors": ["empty response content"],
        }

    try:
        parsed = json.loads(text_parts[0])
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "ok": False,
            "tool": name,
            "message": f"MCP result was not valid JSON: {exc}",
            "data": {},
            "errors": [str(exc)],
        }

    if result.isError and isinstance(parsed, dict) and "ok" not in parsed:
        parsed = {"ok": False, "tool": name, "message": str(parsed), "data": {}, "errors": [str(parsed)]}
    return parsed


def _atexit_cleanup() -> None:
    """Best-effort marker only -- deliberately does NOT try to re-enter and
    ``aclose()`` the connection's ``AsyncExitStack`` from a fresh event
    loop/task. anyio's cancel scopes must be entered and exited from the
    same asyncio Task; ``asyncio.run()`` here would create a new one,
    which raises (not suppresses) a "cancel scope in a different task"
    error on every interpreter exit -- worse than doing nothing.

    In practice the subprocess reaps itself: it's reading its stdin in a
    loop, and interpreter shutdown closes our end of that pipe, so the
    child sees EOF and exits on its own. A hard kill (task manager,
    SIGKILL) bypasses cleanup entirely regardless of what runs here."""
    global _stack, _session, _loop, _connect_lock
    _stack = None
    _session = None
    _loop = None
    _connect_lock = None


atexit.register(_atexit_cleanup)
