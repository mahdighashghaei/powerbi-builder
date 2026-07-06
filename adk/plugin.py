"""Phase 8 — PowerBIBuilderPlugin (Google ADK ``BasePlugin``).

A cross-cutting plugin that demonstrates the ADK plugin/callback surface
outside the agent itself. It:

* audit-logs every tool call and agent lifecycle event (before/after).
* saves a Markdown build report as an **artifact** whenever the
  ``generate_pbip`` high-level tool succeeds — showcasing the ADK
  ``ArtifactService`` wired through ``ToolContext.save_artifact``.
* records the list of saved artifacts in ``tool_context.state`` so the
  REPL ``/artifact`` command can enumerate them.

The plugin is async (``BasePlugin`` callbacks are coroutines) and is
passed to the ``Runner`` via ``Runner(plugins=[...])``.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from google.adk.plugins import BasePlugin  # noqa: E402
from google.genai import types  # noqa: E402

from adk.genai_compat import (  # noqa: E402
    blob_data,
    blob_display_name,
    blob_mime_type,
    part_inline_data,
)
from adk import telemetry  # noqa: E402
from utils import AuditLogger  # noqa: E402

log = AuditLogger.get("adk.plugin")


# ---------------------------------------------------------------------------
# Build-report renderer
# ---------------------------------------------------------------------------


def _render_build_report(
    result: dict[str, Any],
    project_name: str | None = None,
    pbip_root: str | None = None,
) -> str:
    """Render a Markdown build report from a tool result dict.

    ``project_name``/``pbip_root`` override ``result["data"]`` when given —
    needed for the lower-level editing tools (apply_theme, delete_page,
    etc.) whose result dicts don't carry these fields at all; the caller
    resolves them separately via ``_resolve_project_identity`` so this
    report doesn't say "unknown" for every edit after the initial build.
    """
    data = result.get("data", {}) or {}
    project_name = project_name or data.get("project_name", "unknown")
    pbip_root = pbip_root or data.get("pbip_root", "")
    # Regression note: this used to read data.get("summary", []) -- a key
    # generate_pbip has never actually produced (the real key is "steps",
    # see mcp_server/highlevel.py::generate_pbip's return dict), so the
    # Agent Summary table below rendered as an empty header in every build
    # report ever saved.
    summary = data.get("steps", []) or []
    lines = [
        f"# Build Report — {project_name}",
        "",
        f"- **Project:** {project_name}",
        f"- **Location:** {pbip_root}",
        f"- **Status:** {'success' if result.get('ok') else 'failed'}",
        "",
    ]
    lines += [
        "## Agent Summary",
        "",
        "| Agent | Status | Message |",
        "|-------|--------|---------|",
    ]
    for step in summary:
        agent = step.get("agent", "")
        ok = "ok" if step.get("ok") else "fail"
        msg = step.get("message", "").replace("|", "\\|")
        lines.append(f"| {agent} | {ok} | {msg} |")
    lines.append("")
    return "\n".join(lines)


def _zip_project(pbip_root: str, project_name: str) -> bytes:
    """Zip a generated PBIP project folder into a bytes blob.

    Thin wrapper -- the actual implementation lives in
    :mod:`utils.zip_utils` so ``mcp_server`` can reuse it without
    depending on ``adk/``.
    """
    from utils.zip_utils import zip_project_dir
    return zip_project_dir(pbip_root, project_name)


def _looks_like_a_pbip_project(path: str) -> bool:
    """True if ``path`` itself contains a ``*.Report`` or ``*.SemanticModel``
    folder -- i.e. it's a SPECIFIC project's own directory, not something
    generic like the shared output root (which could contain many
    unrelated projects and would be enormous / wrong to zip)."""
    try:
        p = Path(path)
        return p.is_dir() and (
            any(p.glob("*.Report")) or any(p.glob("*.SemanticModel"))
        )
    except Exception:  # noqa: BLE001
        return False


def _resolve_project_identity(
    tool_args: dict[str, Any], result: dict[str, Any],
) -> tuple[str, str] | None:
    """Best-effort ``(project_name, pbip_root)`` for artifact saving.

    The high-level tools (generate_pbip, edit_pbip, add_measure, add_visual,
    add_page, build_report) echo both back in ``result["data"]``. The
    lower-level editing tools the model also calls directly (apply_theme,
    update_visual, delete_visual, delete_page, edit_measure, delete_measure,
    edit_table_source, relayout_page) do NOT — but every one of them takes
    the project's own directory as an argument, just under one of two
    different names depending on which module they came from:
    ``pbip_dir`` (adk/tools/edit_tools.py) or ``output_root``
    (adk/tools/theme_tools.py / pbir_tools.py, when called against an
    EXISTING project rather than the generic output root — see the "Decide
    First" prompt guidance in adk/agent.py). Missing this resolution
    entirely (rather than falling back to the arguments) was a real,
    observed bug: `apply_theme` succeeded but no fresh zip was ever
    offered, because its result carries only ``{"path", "name"}``.

    Returns ``None`` when nothing resolvable is found, or when a
    fallback (tool_args-derived) candidate doesn't actually look like a
    specific PBIP project directory (guards against zipping a generic/
    shared output root by accident). The TRUSTED source
    (``result["data"]["pbip_root"]``, already computed/verified by the
    tool itself) is never subjected to this existence check — it's
    authoritative by construction.
    """
    data = result.get("data", {}) or {}
    trusted_root = data.get("pbip_root")
    if trusted_root:
        return str(data.get("project_name") or Path(trusted_root).name), str(trusted_root)

    candidate = tool_args.get("pbip_dir") or tool_args.get("output_root")
    if candidate and _looks_like_a_pbip_project(str(candidate)):
        return Path(str(candidate)).name, str(candidate)

    # Real, observed miss: apply_theme called with output_root="./output"
    # (the GENERIC root -- correctly fails the check above) plus
    # output_dir="BankCampaign/BankCampaign.Report" (the project's own
    # relative path, nested one level deeper than a bare "<Name>.Report").
    # output_root alone can't resolve; combine it with output_dir and walk
    # up looking for the actual project root, since output_dir always
    # points AT or BELOW it, never above. Confirmed live: this exact
    # combination silently skipped the zip refresh, so a newly-applied
    # theme was never reflected in the downloadable project -- the chat
    # then confidently told the user their (stale) zip was ready.
    output_dir = tool_args.get("output_dir")
    if candidate and output_dir:
        probe = Path(str(candidate)) / str(output_dir)
        for _ in range(3):  # output_dir is shallow (1-2 segments) -- bounded walk-up
            if _looks_like_a_pbip_project(str(probe)):
                return probe.name, str(probe)
            parent = probe.parent
            if parent == probe:
                break
            probe = parent

    return None


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class PowerBIBuilderPlugin(BasePlugin):
    """ADK plugin: audit logging + artifact saving for powerbi-builder.

    Demonstrates ``before_tool_callback`` / ``after_tool_callback`` /
    ``before_agent_callback`` / ``after_agent_callback`` from
    ``BasePlugin``.  All callbacks are async and keyword-only.
    """

    # Tools whose successful result should be saved as an artifact. Every
    # tool that mutates a PBIP project belongs here, not just generate_pbip —
    # confirmed missing tools were a repeated, real bug (build_report,
    # then apply_theme, each found live: the edit succeeded but the web UI
    # had no fresh zip artifact to offer, leaving the user with only a raw
    # filesystem path). _resolve_project_identity() handles both the
    # high-level tools (whose result echoes project_name/pbip_root) and the
    # lower-level editing tools (whose result doesn't -- their project
    # identity is recovered from the tool's OWN arguments instead), so this
    # list only needs to name which tools mutate a project, not which shape
    # their result carries.
    _ARTIFACT_TOOLS = {
        # High-level (mcp_server/highlevel.py via adk/tools/highlevel_tools.py)
        "generate_pbip", "edit_pbip", "add_measure", "add_visual",
        "add_page", "build_report",
        # Lower-level editing tools (adk/tools/edit_tools.py, theme_tools.py)
        "apply_theme", "update_visual", "delete_visual", "delete_page",
        "edit_measure", "delete_measure", "edit_table_source", "relayout_page",
    }

    def __init__(self, name: str = "powerbi_builder_plugin", *, artifact_tag: str = "build_report") -> None:
        super().__init__(name)
        self._artifact_tag = artifact_tag
        # Track per-invocation tool timing. ADK may run sub-agents concurrently,
        # so the dict is guarded by a lock (was documented as "not thread-safe").
        self._tool_starts: dict[str, float] = {}
        self._tool_starts_lock = threading.Lock()
        # Trajectory spans (Wave A4): active spans keyed like _tool_starts, plus
        # an agent-starts dict. When telemetry is disabled these stay empty and
        # the span() calls return no-op spans, so there is no overhead.
        self._tool_spans: dict[str, Any] = {}
        self._agent_starts: dict[str, float] = {}
        self._spans_lock = threading.Lock()

    # -- tool callbacks ----------------------------------------------------

    async def before_tool_callback(
        self, *, tool, tool_args: dict[str, Any], tool_context
    ):  # type: ignore[override]
        """Log the start of a tool call and open a telemetry span. Returning a
        dict would *skip* the tool — we return ``None`` so execution proceeds."""
        name = getattr(tool, "name", str(tool))
        key = f"{name}:{id(tool_context)}"
        with self._tool_starts_lock:
            self._tool_starts[key] = time.time()
        # Open a trajectory span (no-op when telemetry is disabled).
        span = telemetry.start_span(f"tool.{name}", kind="tool", attributes={"tool": name})
        try:
            for k, v in tool_args.items():
                span.set_attribute(f"arg.{k}", v)
        except Exception:
            pass
        with self._spans_lock:
            self._tool_spans[key] = span
        log.info("[plugin] tool_start name=%s args_keys=%s", name, list(tool_args))
        return None

    async def after_tool_callback(
        self, *, tool, tool_args: dict[str, Any], tool_context, result: dict
    ):  # type: ignore[override]
        """Log the end of a tool call, close its telemetry span, and for
        ``generate_pbip`` save a build-report artifact.  Returning a dict would
        *replace* the tool result — we return ``None`` to keep it unchanged."""
        name = getattr(tool, "name", str(tool))
        key = f"{name}:{id(tool_context)}"
        with self._tool_starts_lock:
            started = self._tool_starts.pop(key, time.time())
        elapsed = time.time() - started
        ok = bool(result.get("ok")) if isinstance(result, dict) else None
        log.info("[plugin] tool_end name=%s ok=%s elapsed=%.3fs", name, ok, elapsed)
        # Close the trajectory span, recording the outcome + duration.
        with self._spans_lock:
            span = self._tool_spans.pop(key, None)
        if span is not None:
            try:
                span.set_attribute("ok", bool(ok))
                span.set_attribute("elapsed_s", round(elapsed, 4))
                span.end("ok" if ok else "error")
            except Exception:
                pass

        if name in self._ARTIFACT_TOOLS and isinstance(result, dict) and result.get("ok"):
            await self._save_build_artifact(tool_context, tool_args, result)
        return None

    # -- agent callbacks ---------------------------------------------------

    async def before_agent_callback(self, *, agent, callback_context):  # type: ignore[override]
        name = getattr(agent, "name", agent)
        log.info("[plugin] agent_start name=%s", name)
        # Open an agent-level trajectory span.
        span = telemetry.start_span(f"agent.{name}", kind="agent", attributes={"agent": name})
        with self._spans_lock:
            self._agent_starts[f"{name}:{id(callback_context)}"] = time.time()
            self._tool_spans[f"agent:{name}:{id(callback_context)}"] = span
        return None

    async def after_agent_callback(self, *, agent, callback_context):  # type: ignore[override]
        name = getattr(agent, "name", agent)
        key = f"agent:{name}:{id(callback_context)}"
        with self._spans_lock:
            started = self._agent_starts.pop(key, time.time())
            span = self._tool_spans.pop(key, None)
        if span is not None:
            try:
                span.set_attribute("elapsed_s", round(time.time() - started, 4))
                span.end("ok")
            except Exception:
                pass
        log.info("[plugin] agent_end name=%s", name)
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):  # type: ignore[override]
        """Clean up ``_tool_starts`` and close the span as error when a tool raises."""
        name = getattr(tool, "name", str(tool))
        key = f"{name}:{id(tool_context)}"
        with self._tool_starts_lock:
            self._tool_starts.pop(key, None)
        with self._spans_lock:
            span = self._tool_spans.pop(key, None)
        if span is not None:
            try:
                span.record_exception(error)
                span.end("error")
            except Exception:
                pass
        log.warning("[plugin] tool_error name=%s error=%s", name, error)
        return None

    # -- user message callback (web upload bridge) -------------------------

    # MIME type prefixes Gemini accepts as inline data.  Anything else
    # (e.g. application/vnd.ms-excel, text/csv, application/octet-stream)
    # must be stripped from the message before it reaches the model, or
    # Gemini returns 400 INVALID_ARGUMENT "Unsupported MIME type".
    _GEMINI_OK_PREFIXES = ("image/", "audio/", "video/", "application/pdf")

    async def on_user_message_callback(self, *, invocation_context, user_message):  # type: ignore[override]
        """Bridge web-uploaded files to disk + strip unsupported MIME types.

        Two jobs:
        1. **Materialize to disk**: write inline_data bytes to
           ``output_root/_uploads/<name>`` so ``generate_pbip(source_artifact=...)``
           can find the file without a path.
        2. **Strip unsupported MIME types**: Gemini rejects inline_data with
           non-media MIME types (e.g. ``application/vnd.ms-excel``,
           ``text/csv``, ``application/octet-stream``) with a 400 error.
           Replace those parts with a text placeholder so the model knows
           the file was uploaded and can call ``generate_pbip`` with
           ``source_artifact``.

        Returns a modified ``types.Content`` if any parts were replaced,
        or ``None`` to let the original message proceed unchanged.
        """
        if user_message is None or user_message.parts is None:
            return None

        # Resolve the output root at call time so a runtime env override
        # (POWERBI_OUTPUT_ROOT / OUTPUT_DIR) is honoured. The default comes
        # from adk.config.OUTPUT_ROOT so the fallback is defined in one place.
        from adk.config import OUTPUT_ROOT as _default_output_root
        output_root = Path(
            os.environ.get("POWERBI_OUTPUT_ROOT")
            or os.environ.get("OUTPUT_DIR", str(_default_output_root))
        )
        upload_dir = output_root / "_uploads"

        modified = False
        new_parts = []
        for part in user_message.parts:
            inline = part_inline_data(part)
            data = blob_data(inline) if inline is not None else None
            if inline is None or not data:
                new_parts.append(part)
                continue

            # 1. Materialize to disk
            fname = blob_display_name(inline) or "uploaded_file"
            try:
                upload_dir.mkdir(parents=True, exist_ok=True)
                (upload_dir / fname).write_bytes(data)
                log.info("[plugin] web_upload_materialized name=%s size=%d",
                         fname, len(data))
            except Exception as exc:  # pragma: no cover
                log.warning("[plugin] web_upload_write_failed name=%s err=%s",
                            fname, exc)

            # 2. Check if Gemini supports this MIME type
            mime = blob_mime_type(inline)
            if mime.startswith(self._GEMINI_OK_PREFIXES):
                # Gemini can handle it (image/audio/video/pdf) — keep as-is
                new_parts.append(part)
            else:
                # Unsupported MIME type (Excel, CSV, JSON, etc.) — replace
                # with a text placeholder so the model knows the file exists
                # and can call generate_pbip(source_artifact=...).
                size_kb = len(data) / 1024
                placeholder = (
                    f'[Uploaded file: "{fname}" ({size_kb:.1f} KB, {mime or "unknown"})]\n'
                    f'This file has been saved to the session. '
                    f'Call generate_pbip with source_artifact="{fname}" '
                    f'to build a Power BI project from it.'
                )
                new_parts.append(types.Part(text=placeholder))
                modified = True
                log.info("[plugin] web_upload_stripped_mime name=%s mime=%s → text placeholder",
                         fname, mime)

        if modified:
            return types.Content(role=user_message.role, parts=new_parts)
        return None

    # -- helpers -----------------------------------------------------------

    async def _save_build_artifact(
        self, tool_context, tool_args: dict[str, Any], result: dict,
    ) -> None:
        """Save a Markdown build report + a zipped project as ADK artifacts.

        Both are user-scoped (``user:`` prefix) so they survive across
        sessions and the web UI's Artifacts panel ``file_download`` button
        can serve them.

        * ``user:build_report_<project>.md`` — human-readable summary.
        * ``user:project_<project>.zip`` — the zipped .pbip folder, so the
          user can download and open it in Power BI Desktop directly from
          the web UI without filesystem access to the server.

        No-ops (logs a warning, saves nothing) when the project identity
        can't be resolved — e.g. a lower-level tool called with the
        generic output root instead of its own project directory. Silently
        skipping here is deliberate: better to occasionally miss a
        refresh than to zip the wrong (possibly huge, multi-project)
        directory.
        """
        identity = _resolve_project_identity(tool_args, result)
        if identity is None:
            log.warning(
                "[plugin] artifact_skip: could not resolve project identity "
                "from tool_args=%s result_data_keys=%s",
                list(tool_args.keys()), list((result.get("data") or {}).keys()),
            )
            return
        project_name, pbip_root = identity

        # 1. Markdown build report
        filename = f"user:{self._artifact_tag}_{project_name}.md"
        report_md = _render_build_report(result, project_name, pbip_root)
        try:
            version = await tool_context.save_artifact(
                filename=filename, artifact=types.Part(text=report_md)
            )
            log.info("[plugin] artifact_saved filename=%s version=%s", filename, version)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[plugin] artifact_save_failed filename=%s err=%s", filename, exc)

        # 2. Zipped project for download
        zip_filename = f"user:project_{project_name}.zip"
        try:
            zip_bytes = _zip_project(pbip_root, project_name)
            if zip_bytes:
                await tool_context.save_artifact(
                    filename=zip_filename,
                    artifact=types.Part(inline_data=types.Blob(
                        data=zip_bytes, mime_type="application/zip",
                    )),
                )
                log.info("[plugin] project_zip_saved filename=%s size=%d",
                         zip_filename, len(zip_bytes))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("[plugin] project_zip_failed name=%s err=%s",
                        zip_filename, exc)

        # record in state so /artifact can enumerate; reassign to trigger delta
        existing = list(tool_context.state.get("artifacts", []))
        for fn in (filename, zip_filename):
            if not any(e.get("filename") == fn for e in existing):
                existing.append({"filename": fn, "project": project_name})
        tool_context.state["artifacts"] = existing
