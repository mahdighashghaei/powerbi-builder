"""Phase 8 — Interactive chat REPL for the powerbi-builder ADK agent.

Runs the ADK ``Runner`` against an in-memory session directly in the
terminal (no browser needed). Showcases the full ADK service stack:

* ``InMemorySessionService``  — conversational session + ``state`` tracking
* ``InMemoryArtifactService`` — build reports saved as versioned artifacts
* ``InMemoryMemoryService``   — cross-session recall via ``search_memory``
* ``Runner`` with a ``BasePlugin`` — cross-cutting logging + artifact saving
* ``root_agent`` multi-agent system — auto-delegation to specialist sub-agents

The REPL also provides a set of slash commands for project inspection,
state/memory/artifact queries, and quick edits without an LLM round-trip.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from google.adk.agents import BaseAgent  # noqa: E402
from google.adk.agents.run_config import RunConfig  # noqa: E402
from google.adk.artifacts import InMemoryArtifactService  # noqa: E402
from google.adk.events import Event  # noqa: E402
from google.adk.memory import InMemoryMemoryService  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import BaseSessionService, InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from adk.config import OUTPUT_ROOT, SESSION_DB_URL  # noqa: E402
from utils import AuditLogger  # noqa: E402

log = AuditLogger.get("adk.chat")

# Per-turn LLM call budget — prevents runaway agent loops (default 500 is
# far too high for a tool-heavy build pipeline). Override with the
# POWERBI_MAX_LLM_CALLS env var for complex multi-table models.
_MAX_LLM_CALLS = int(os.getenv("POWERBI_MAX_LLM_CALLS", "30"))


def _build_session_service() -> BaseSessionService:
    """Build the session service from configuration.

    When ``POWERBI_SESSION_DB_URL`` is set (e.g. an SQLite URL) a
    ``DatabaseSessionService`` is used so sessions persist across process
    restarts. Otherwise we fall back to ``InMemorySessionService`` (sessions
    are lost on exit). The DB extra (SQLAlchemy) is imported lazily so the
    REPL still runs without it.
    """
    if SESSION_DB_URL:
        try:
            from google.adk.sessions import DatabaseSessionService  # noqa: E402
            svc = DatabaseSessionService(SESSION_DB_URL)
            log.info("session persistence enabled: %s", SESSION_DB_URL)
            return svc
        except Exception as exc:  # noqa: BLE001 - missing extra, bad URL, etc.
            log.warning(
                "DatabaseSessionService unavailable (%s); falling back to "
                "InMemorySessionService (sessions will NOT persist). "
                "Install google-adk[db] to enable persistence.", exc,
            )
    return InMemorySessionService()

# ---------------------------------------------------------------------------
# Sync/async bridge
# ---------------------------------------------------------------------------
# ADK 2.x session/memory/artifact service methods are *async*, but the REPL
# runs on the main (sync) thread.  ``Runner.run`` (sync) launches its own
# asyncio loop on a *separate* thread, so the main thread has no running
# loop — we can safely use ``asyncio.run`` to drive the async service calls.


def _run_async(coro):
    """Run a coroutine to completion from sync code.

    Uses ``asyncio.run`` (a fresh event loop) when there is no running loop on
    the current thread — the normal REPL path, where ``Runner.run`` runs its
    own loop on a separate thread. If we are *already* inside an event loop on
    this thread (e.g. an async test or wrapper), ``asyncio.run`` would raise
    "cannot be called from a running event loop", so run the coroutine on a
    separate thread with its own loop instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop on this thread — safe to create one.
        return asyncio.run(coro)
    # A loop is already running here; drive the coroutine on a worker thread
    # that owns its own loop, blocking until it finishes.
    import threading  # noqa: E402 - stdlib, lazy import

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on main thread
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def event_text(event: Event) -> str:
    """Extract concatenated text from an ADK ``Event``."""
    content = getattr(event, "content", None)
    if content is None or getattr(content, "parts", None) is None:
        return ""
    return "".join(p.text for p in content.parts if getattr(p, "text", None))


def event_has_error(event: Event) -> Optional[str]:
    """Return an error string if the event carries one, else ``None``.

    Uses the drift-guard wrappers in :mod:`adk.genai_compat` so a renamed
    ``error_code``/``error_message`` attribute degrades gracefully.
    """
    from adk.genai_compat import event_error_code, event_error_message
    code = event_error_code(event)
    if code:
        msg = event_error_message(event) or code
        return f"[error {code}] {msg}"
    return None


# ---------------------------------------------------------------------------
# Slash-command table
# ---------------------------------------------------------------------------

SLASH_HELP = """\
Slash commands:
  /help                 Show this help
  /new                  Start a fresh session (clears history + state)
  /state                Show current session state (current_project, artifacts, ...)
  /list                 List PBIP projects found under the output dir
  /open [name]          Show the path to open a project in Power BI Desktop
  /upload <path> [as name]  Upload a local file into the session (no path typing needed)
  /download [project] [to]  Zip + export a generated project to a destination
  /samples              List bundled sample data files for quick use
  /files                Show uploaded files + generated projects
  /memory <query>       Search the memory service for prior turns
  /artifact [name]      List saved artifacts (or show one's text)
  /theme <preset>       Apply a theme preset to the current project
  /add-measure <n> <expr>  Append a DAX measure to the current project
  /deploy <workspace>   Dry-run deploy the current project to a Fabric workspace
  /exit                 Quit the REPL
"""


class ChatRepl:
    """Terminal REPL driving the ADK ``Runner``.

    All dependencies are constructor-injected (with sensible in-memory
    defaults) so tests can pass mocks for the ``Runner`` and services.
    """

    BANNER = (
        "╔══════════════════════════════════════════════════════════╗\n"
        "║   PowerBI Builder — AI Chat Assistant  (Phase 8)          ║\n"
        "║   Google ADK 2.x  •  multi-agent  •  session + memory     ║\n"
        "╚══════════════════════════════════════════════════════════╝\n"
        "Type /help for commands, /exit to quit.\n"
    )

    def __init__(
        self,
        *,
        agent: Optional[BaseAgent] = None,
        # Must match App.name in adk/agent.py ("adk") so sessions created by
        # the REPL and by `adk web` are interchangeable. Was "powerbi_builder",
        # which mismatched and made sessions non-portable across entry points.
        app_name: str = "adk",
        user_id: str = "repl_user",
        output_root: Optional[str] = None,
        runner: Optional[Runner] = None,
        session_service: Optional[BaseSessionService] = None,
        artifact_service: Optional[InMemoryArtifactService] = None,
        memory_service: Optional[InMemoryMemoryService] = None,
        plugin: Optional[Any] = None,
    ) -> None:
        # Lazy import of root_agent to avoid a circular import at module load.
        if agent is None and runner is None:
            from adk.agent import root_agent  # noqa: E402
            agent = root_agent
        self.agent = agent
        self.app_name = app_name
        self.user_id = user_id
        self.output_root = Path(output_root or OUTPUT_ROOT).expanduser().resolve()

        # Services (injectable for tests). The session service defaults to the
        # config-driven builder (DatabaseSessionService when POWERBI_SESSION_DB_URL
        # is set, else InMemorySessionService). Tests pass mocks explicitly.
        self.session_service = session_service or _build_session_service()
        self.artifact_service = artifact_service or InMemoryArtifactService()
        self.memory_service = memory_service or InMemoryMemoryService()
        self.plugin = plugin  # may be None; ChatRepl.create_runner picks a default

        # Runner (injectable; otherwise build from agent + services)
        if runner is not None:
            self.runner = runner
        else:
            self.runner = self._create_runner()

        # Create the initial session (async service → bridge to sync)
        self.session = self._create_session()
        self.session_id = self.session.id
        self._should_quit = False

    def _create_session(self):
        """Create a new session, catching failures gracefully."""
        try:
            return _run_async(
                self.session_service.create_session(
                    app_name=self.app_name, user_id=self.user_id
                )
            )
        except Exception as exc:
            log.error("create_session failed: %s", exc)
            raise RuntimeError(f"Failed to create session: {exc}") from exc

    # -- wiring -----------------------------------------------------------

    def _create_runner(self) -> Runner:
        """Build a ``Runner`` from the agent + services (+ default plugins).

        Wires two plugins:
        * ``PowerBIBuilderPlugin`` — audit logging + build-report artifacts.
        * ``SaveFilesAsArtifactsPlugin`` — lets users upload files in
          ``adk web`` (captures inline_data parts as session artifacts).
        """
        plugins = []
        if self.plugin is not None:
            plugins = [self.plugin]
        else:
            try:
                from adk.plugin import PowerBIBuilderPlugin
                plugins = [PowerBIBuilderPlugin()]
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(f"PowerBIBuilderPlugin unavailable: {exc}")
        # Add SaveFilesAsArtifactsPlugin for browser file uploads (adk web).
        try:
            from google.adk.plugins.save_files_as_artifacts_plugin import (
                SaveFilesAsArtifactsPlugin,
            )
            plugins.append(SaveFilesAsArtifactsPlugin())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(f"SaveFilesAsArtifactsPlugin unavailable: {exc}")
        return Runner(
            agent=self.agent,
            app_name=self.app_name,
            session_service=self.session_service,
            artifact_service=self.artifact_service,
            memory_service=self.memory_service,
            plugins=plugins or None,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Current session state dict (project tracking, artifacts, ...)."""
        return self.session.state

    # -- core turn --------------------------------------------------------

    def run_turn(self, user_text: str, *,
                 on_event: Optional[Callable[[str], None]] = None) -> str:
        """Run one conversational turn and return the rendered agent reply.

        Streams events from ``Runner.run`` (sync generator), concatenates
        partial chunks, and surfaces errors.  Tool-call events
        (function_call / function_response) are surfaced live via
        ``on_event`` so the user sees progress instead of silence.
        After the turn completes the session is added to the memory service
        for cross-session recall.

        Args:
            user_text: The user's message text.
            on_event: Optional callback for live progress feedback (tool
                calls, errors). Each call receives a short status string.
        """
        if not user_text or not user_text.strip():
            return ""

        content = types.Content(
            role="user", parts=[types.Part(text=user_text)]
        )
        chunks: list[str] = []
        errors: list[str] = []
        run_config = RunConfig(max_llm_calls=_MAX_LLM_CALLS)
        try:
            for event in self.runner.run(
                user_id=self.user_id,
                session_id=self.session_id,
                new_message=content,
                run_config=run_config,
            ):
                self._absorb_event(event)

                # Surface tool-call events live (prevents "silent hang" UX)
                func_calls = event.get_function_calls() or []
                func_responses = event.get_function_responses() or []
                for fc in func_calls:
                    args_summary = _summarize_args(fc.args)
                    line = f"  🔧 Calling {fc.name}({args_summary})..."
                    if on_event:
                        on_event(line)
                for fr in func_responses:
                    ok = "✅" if isinstance(fr.response, dict) and fr.response.get("ok") else "⏹"
                    line = f"  {ok} {fr.name} done"
                    if on_event:
                        on_event(line)

                err = event_has_error(event)
                if err:
                    errors.append(err)
                    if on_event:
                        on_event(f"  ⚠️ {err}")
                    continue
                text = event_text(event)
                if text:
                    chunks.append(text)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("run_turn failed")
            return f"[repl error] {exc}"

        # persist turn to memory for future /memory searches
        self._add_to_memory()

        reply = "".join(chunks).strip()
        if not reply and not errors:
            return "(no text reply — the agent completed tool calls but produced no summary)"
        if errors and not reply:
            return "\n".join(errors)
        if errors:
            return reply + "\n" + "\n".join(errors)
        return reply

    def _absorb_event(self, event: Event) -> None:
        """Apply state_delta carried by an event to the live session state.

        The Runner normally merges state deltas into the session, but we
        also reflect them here so ``self.state`` stays current even when a
        mock Runner is used in tests (mocks bypass the real merge path).
        """
        actions = getattr(event, "actions", None)
        if actions is None:
            return
        delta = getattr(actions, "state_delta", None)
        if delta:
            self.session.state.update(delta)

    def _add_to_memory(self) -> None:
        """Add the current session to the memory service (best-effort).

        The Runner appends events to the service's *internal* session copy,
        not to ``self.session`` (which is a shallow copy from
        ``create_session``).  We must fetch the fresh session from the
        service so ``add_session_to_memory`` sees the accumulated events.
        """
        try:
            fresh = _run_async(
                self.session_service.get_session(
                    app_name=self.app_name,
                    user_id=self.user_id,
                    session_id=self.session_id,
                )
            )
            if fresh is not None:
                # sync state from the service's copy so /state stays current
                self.session.state.update(fresh.state)
                _run_async(self.memory_service.add_session_to_memory(fresh))
            else:
                # fallback: use whatever we have (mock Runner path)
                _run_async(self.memory_service.add_session_to_memory(self.session))
        except Exception:  # pragma: no cover - defensive
            log.debug("add_session_to_memory failed", exc_info=True)

    # -- slash commands ---------------------------------------------------

    def handle_slash(self, line: str) -> Optional[str]:
        """Parse and dispatch a slash command. Returns the response string,
        or ``None`` if the line is not a slash command. Side effects
        (e.g. ``/new``, ``/exit``) mutate REPL state directly."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            return None
        # Split command from args.  Use shlex for quote-aware splitting,
        # but on Windows backslash paths get mangled by posix-mode shlex.
        # Strategy: split only the command name, pass the rest as raw args
        # for path-bearing commands; use shlex for others.
        first_space = stripped.find(" ")
        if first_space == -1:
            cmd = stripped.lower()
            raw_rest = ""
        else:
            cmd = stripped[:first_space].lower()
            raw_rest = stripped[first_space + 1:]

        # For path-bearing commands, split the raw rest simply to preserve
        # backslashes in Windows paths.  For others, use shlex.
        _PATH_COMMANDS = {"upload", "download", "open"}
        if cmd.lstrip("/") in _PATH_COMMANDS and raw_rest:
            # Simple split that preserves backslashes
            args = raw_rest.split()
        else:
            try:
                parts = shlex.split(stripped)
                args = parts[1:]
            except ValueError:
                args = raw_rest.split()

        # map command name → method name: "/add-measure" → "_cmd_add_measure"
        method_name = "_cmd_" + cmd.lstrip("/").replace("-", "_")
        handler = getattr(self, method_name, None)
        if handler is None:
            return f"Unknown command: {cmd}\nType /help for the list."
        return handler(args)

    # Each handler returns a string (or None to stay quiet).

    def _cmd_help(self, args: list[str]) -> str:
        return SLASH_HELP

    def _cmd_exit(self, args: list[str]) -> str:
        self._should_quit = True
        return "Goodbye! 👋"

    # -- file upload / download -------------------------------------------

    def _cmd_upload(self, args: list[str]) -> str:
        """/upload <path> [as <name>] — read a local file and store it as an
        ADK artifact so the agent can use it without a path.  Also records
        the file in ``state['uploaded_files']`` for ``/files`` listing."""
        if not args:
            return "Usage: /upload <path> [as <name>]"
        path_str = args[0]
        # optional "as <name>" syntax
        name = None
        if len(args) >= 3 and args[1].lower() == "as":
            name = args[2]
        elif len(args) == 2:
            name = args[1]
        p = Path(path_str).expanduser().resolve()
        if not p.is_file():
            return f"File not found: {path_str}"
        if name is None:
            name = p.name
        # Ensure user-scoped prefix so no session_id needed
        if not name.startswith("user:"):
            name = f"user:{name}"
        try:
            data = p.read_bytes()
            # Guess mime type
            import mimetypes
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            part = types.Part(inline_data=types.Blob(data=data, mime_type=mime))
            version = _run_async(self.artifact_service.save_artifact(
                app_name=self.app_name, user_id=self.user_id,
                filename=name, artifact=part,
            ))
            # Also write a copy to output_root/_uploads/ so generate_pbip
            # can find it via source_artifact without a path.
            upload_dir = self.output_root / "_uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            (upload_dir / name.removeprefix("user:")).write_bytes(data)
        except Exception as exc:
            return f"[upload error] {exc}"
        # Record in state
        uploads = list(self.state.get("uploaded_files", []))
        uploads.append({"filename": name, "original_path": str(p),
                        "version": version, "size": len(data)})
        self.state["uploaded_files"] = uploads
        log.info("[repl] uploaded %s as %s (%d bytes)", p, name, len(data))
        return (f"✅ Uploaded '{p.name}' as '{name}' "
                f"({len(data)} bytes, v{version}).\n"
                f"   The agent can now use this file. "
                f"Just say: \"build a report from the uploaded file\"")

    def _cmd_download(self, args: list[str]) -> str:
        """/download [project] [to <path>] — zip a generated PBIP project
        and copy it to a destination (defaults to output_root/<name>.zip)."""
        # Two supported forms:
        #   /download to /path            -> project from state, dest = /path
        #   /download <project> to /path  -> explicit project, dest = /path
        #   /download <project>           -> dest defaults to output_root/<name>.zip
        proj_name: str | None = None
        dest = None
        if args and args[0].lower() == "to" and len(args) >= 2:
            # "/download to /path" — project comes from current state
            proj_name = self.state.get("current_project")
            dest = args[1]
        elif len(args) >= 3 and args[1].lower() == "to":
            # "/download <project> to /path"
            proj_name = args[0]
            dest = args[2]
        elif args:
            # "/download <project>"
            proj_name = args[0]
        else:
            proj_name = self.state.get("current_project")
        if not proj_name:
            return "No project name given and no current project in state."
        # Find the project — try several layouts:
        # 1. output_root/<name>/  (nested: <name>/<name>.SemanticModel + .Report)
        # 2. output_root/  (flat: <name>.SemanticModel + .Report directly under root)
        proj_dir = self.output_root / proj_name
        if not proj_dir.is_dir():
            # Try nested: look for <name>*/<name>.SemanticModel
            sm_dirs = list(self.output_root.glob(f"{proj_name}*/{proj_name}.SemanticModel"))
            if sm_dirs:
                proj_dir = sm_dirs[0].parent
            else:
                # Try flat: .SemanticModel directly under output_root
                sm_flat = self.output_root / f"{proj_name}.SemanticModel"
                if sm_flat.is_dir():
                    proj_dir = self.output_root
                else:
                    return f"Project '{proj_name}' not found under {self.output_root}"
        # Default destination
        if dest is None:
            dest = str(self.output_root / f"{proj_name}.zip")
        dest_path = Path(dest).expanduser().resolve()
        try:
            # Reuse the shared zip helper so internal-only files
            # (build.spec.json, decisions.log.json, feedback_history.json,
            # learning_memory.json) are excluded consistently — the user
            # should only get Power BI files, not our agent's build metadata.
            from utils.zip_utils import zip_project_dir
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            zip_bytes = zip_project_dir(str(proj_dir), proj_name)
            dest_path.write_bytes(zip_bytes)
            size_kb = dest_path.stat().st_size / 1024
            return (f"✅ Exported '{proj_name}' to:\n"
                    f"   {dest_path} ({size_kb:.1f} KB)\n"
                    f"   Unzip and open the .pbip file in Power BI Desktop.")
        except Exception as exc:
            return f"[download error] {exc}"

    def _cmd_samples(self, args: list[str]) -> str:
        """/samples — list bundled sample data files for quick use."""
        from mcp_server.highlevel import suggest_data_files
        files = suggest_data_files()
        if not files:
            return "No sample data files found."
        lines = ["Bundled sample data files:"]
        for i, f in enumerate(files, 1):
            name = Path(f).name
            size = Path(f).stat().st_size
            lines.append(f"  {i}. {name} ({size:,} bytes) — {f}")
        lines.append("")
        lines.append("To use: /upload <path>  or  tell the agent: \"build from <name>\"")
        return "\n".join(lines)

    def _cmd_files(self, args: list[str]) -> str:
        """/files — show uploaded files + generated projects."""
        lines = ["=== Files ==="]
        # Uploaded files
        uploads = list(self.state.get("uploaded_files", []))
        if uploads:
            lines.append("\nUploaded files:")
            for u in uploads:
                lines.append(f"  • {u['filename']} ({u['size']:,} bytes) ← {u['original_path']}")
        else:
            lines.append("\nUploaded files: (none — use /upload <path>)")
        # Generated projects — detect both nested and flat layouts
        if self.output_root.is_dir():
            projects: list[str] = []
            # Nested: <dir>/<name>.SemanticModel
            for d in self.output_root.iterdir():
                if d.is_dir() and any(d.glob("*.SemanticModel")):
                    projects.append(d.name)
            # Flat: <name>.SemanticModel directly under output_root
            for sm in self.output_root.glob("*.SemanticModel"):
                pname = sm.name.removesuffix(".SemanticModel")
                if pname not in projects and pname != "_uploads":
                    projects.append(pname)
            projects.sort()
            if projects:
                lines.append("\nGenerated projects:")
                cur = self.state.get("current_project")
                for p in projects:
                    mark = " ← current" if p == cur else ""
                    lines.append(f"  • {p}{mark}")
            else:
                lines.append("\nGenerated projects: (none yet)")
        return "\n".join(lines)

    def _cmd_new(self, args: list[str]) -> str:
        # Delete old session to prevent memory leak
        try:
            _run_async(self.session_service.delete_session(
                app_name=self.app_name, user_id=self.user_id,
                session_id=self.session_id,
            ))
        except Exception as exc:  # pragma: no cover - best-effort
            log.debug(f"session cleanup on /new failed: {exc}")
        self.session = self._create_session()
        self.session_id = self.session.id
        return "✨ New session started (history + state cleared)."

    def _cmd_state(self, args: list[str]) -> str:
        if not self.state:
            return "Session state is empty."
        return "Session state:\n" + _json.dumps(
            dict(self.state), indent=2, ensure_ascii=False, default=str
        )

    def _cmd_list(self, args: list[str]) -> str:
        root = self.output_root
        if not root.is_dir():
            return f"Output dir not found: {root}"
        projects = sorted(
            d.name.removesuffix(".SemanticModel")
            for d in root.glob("*.SemanticModel")
        )
        if not projects:
            return f"No PBIP projects found under {root}"
        lines = [f"Projects under {root}:"]
        cur = self.state.get("current_project")
        for p in projects:
            mark = " ← current" if p == cur else ""
            lines.append(f"  • {p}{mark}")
        return "\n".join(lines)

    def _cmd_open(self, args: list[str]) -> str:
        name = args[0] if args else self.state.get("current_project")
        if not name:
            return "No project name given and no current project in state."
        entry = self.output_root / f"{name}.pbip"
        sm = self.output_root / f"{name}.SemanticModel"
        if not sm.is_dir():
            return f"Project '{name}' not found under {self.output_root}"
        lines = [
            f"📂 Open in Power BI Desktop (File → Open report → Browse):",
            f"   {entry}",
        ]
        if not entry.exists():
            lines.append(f"   (entry file missing — open the .SemanticModel folder: {sm})")
        return "\n".join(lines)

    def _cmd_memory(self, args: list[str]) -> str:
        query = " ".join(args)
        if not query:
            return "Usage: /memory <query>"
        try:
            resp = _run_async(
                self.memory_service.search_memory(
                    app_name=self.app_name, user_id=self.user_id, query=query
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            return f"[memory error] {exc}"
        memories = getattr(resp, "memories", None) or []
        if not memories:
            return f"No memories matched '{query}'."
        lines = [f"Found {len(memories)} memory match(es) for '{query}':"]
        for m in memories:
            lines.append(f"  • {getattr(m, 'author', '?')}: {str(getattr(m, 'content', ''))[:120]}")
        return "\n".join(lines)

    def _cmd_artifact(self, args: list[str]) -> str:
        # /artifact        → list artifacts recorded in state
        # /artifact <name> → load + print that artifact's text
        recorded = list(self.state.get("artifacts", []))
        if not args:
            if not recorded:
                return "No artifacts saved yet."
            lines = ["Saved artifacts:"]
            for a in recorded:
                lines.append(f"  • {a.get('filename')} (v{a.get('version')}) [{a.get('project')}]")
            return "\n".join(lines)
        fname = args[0]
        try:
            part = _run_async(
                self.artifact_service.load_artifact(
                    app_name=self.app_name, user_id=self.user_id,
                    filename=fname, session_id=self.session_id,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            return f"[artifact error] {exc}"
        if part is None:
            return f"Artifact '{fname}' not found."
        text = getattr(part, "text", None) or str(part)
        return f"--- {fname} ---\n{text}"

    def _cmd_theme(self, args: list[str]) -> str:
        preset = args[0] if args else None
        if not preset:
            return "Usage: /theme <preset>  (e.g. corporate_blue, modern_dark, vibrant)"
        root = self.state.get("current_project_root")
        if not root:
            return "No current project in state. Build/open a project first."
        # Resolve the report definition folder relative to output_root so both
        # nested (<name>/<name>.Report/definition) and flat (<name>.Report/definition)
        # layouts work. apply_theme takes output_dir relative to output_root.
        from adk.tools.theme_tools import apply_theme
        report_def = Path(root) / f"{self.state.get('current_project')}.Report" / "definition"
        if not report_def.is_dir():
            # flat layout: .Report directly under output_root
            report_def = self.output_root / f"{self.state.get('current_project')}.Report" / "definition"
        rel = report_def.resolve().relative_to(self.output_root.resolve())
        result = apply_theme(
            output_dir=str(rel).replace("\\", "/"),
            preset=preset,
            output_root=str(self.output_root),
        )
        return _fmt_tool_result(result, "apply_theme")

    def _cmd_add_measure(self, args: list[str]) -> str:
        if len(args) < 2:
            return "Usage: /add-measure <name> <expression>  (quote the expression)"
        name = args[0]
        expression = " ".join(args[1:])
        root = self.state.get("current_project_root")
        if not root:
            return "No current project in state. Build/open a project first."
        # Route through the registered ADK tool so validate_paths,
        # track_project, and plugin callbacks all run (bypassing them by
        # calling mcp_server.highlevel directly skips that safety net).
        from adk.tools.highlevel_tools import add_measure
        result = add_measure(pbip_dir=str(root), name=name, expression=expression)
        return _fmt_tool_result(result, "add_measure")

    def _cmd_deploy(self, args: list[str]) -> str:
        workspace = args[0] if args else None
        if not workspace:
            return "Usage: /deploy <workspace>  (dry-run only from REPL)"
        root = self.state.get("current_project_root")
        if not root:
            return "No current project in state. Build/open a project first."
        # Route through the registered ADK tool so validate_paths and plugin
        # callbacks run (deploy_pbip_to_fabric defaults to dry_run=True).
        from adk.tools.fabric_tools import deploy_pbip_to_fabric
        result = deploy_pbip_to_fabric(pbip_dir=str(root), workspace=workspace, dry_run=True)
        return _fmt_tool_result(result, "deploy_pbip_to_fabric")

    # -- main loop --------------------------------------------------------

    def cmd_loop(self, input_fn=input, output_fn=print) -> None:
        """Run the read/eval/print loop until ``/exit`` or EOF."""
        output_fn(self.BANNER)
        while not self._should_quit:
            try:
                line = input_fn("you> ")
            except (EOFError, KeyboardInterrupt):
                output_fn("\n" + self._cmd_exit([]))
                break
            if not line.strip():
                continue
            if line.strip().startswith("/"):
                resp = self.handle_slash(line)
                if resp:
                    output_fn(resp)
                if self._should_quit:
                    break
                continue
            reply = self.run_turn(line, on_event=lambda s: output_fn(s))
            if reply:
                output_fn(f"agent> {reply}")


# ---------------------------------------------------------------------------
# Output formatting helper
# ---------------------------------------------------------------------------


def _summarize_args(args: dict) -> str:
    """Short summary of tool-call args for live REPL feedback.

    Shows key args (source, pbip_dir, project_name, description, …) truncated
    so the user sees what the agent is doing without a wall of JSON.
    """
    if not args:
        return ""
    parts: list[str] = []
    for key in ("source", "csv_path", "pbip_dir", "project_name",
                "description", "display_name", "name", "preset",
                "num_pages", "visual_variety", "workspace"):
        if key in args:
            val = str(args[key])
            if len(val) > 60:
                val = val[:57] + "..."
            parts.append(f'{key}="{val}"')
    return ", ".join(parts) if parts else str(args)[:80]


def _fmt_tool_result(result: Any, tool_name: str) -> str:
    """Format a high-level tool result dict into a short REPL string."""
    if not isinstance(result, dict):
        return f"{tool_name}: {result}"
    ok = result.get("ok")
    mark = "✅" if ok else "❌"
    msg = result.get("message", "")
    errs = result.get("errors") or []
    out = f"{mark} {tool_name}: {msg}"
    if errs:
        out += "\n  errors: " + "; ".join(errs)
    return out
