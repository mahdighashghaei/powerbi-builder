"""FastAPI server for the powerbi-builder ADK agent -- the actual ``adk web``
chat UI, plus a ``/health`` endpoint and the project's own A2A routes, all in
one process (what the Docker image runs).

``adk web adk/`` builds its FastAPI app via
``google.adk.cli.fast_api.get_fast_api_app`` internally; this module calls
that SAME factory directly (with the identical ``agents_dir``/``web=True``
setup the CLI uses) and then adds two extra things onto the returned app:

* ``GET /health`` for liveness/readiness probes (Docker, load balancers, CI
  smoke checks), and
* the project's own hand-rolled A2A routes (``adk/a2a.py``) -- passing
  ``a2a=True`` to ``get_fast_api_app`` would register ADK's OWN A2A
  endpoints at the same paths and collide with these, so it stays off here.

Run directly::

    python -m adk.server
    # then: curl http://localhost:8000/health
    # and:  open http://localhost:8000 for the chat UI

The health check verifies that the root agent is importable and that the
configured output root is writable — the two things a deployment needs to
be functional. It does NOT make any LLM call.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402

from adk.config import OUTPUT_ROOT, SESSION_DB_URL  # noqa: E402


def _health_payload() -> dict[str, Any]:
    """Return the health-check payload (no LLM calls)."""
    out_root = Path(OUTPUT_ROOT).expanduser().resolve()
    # output root must exist and be writable for the agent to function
    root_ok = out_root.is_dir()
    writable = False
    if root_ok:
        try:
            (out_root / ".health_probe").write_text("ok", encoding="utf-8")
            (out_root / ".health_probe").unlink(missing_ok=True)
            writable = True
        except OSError:
            writable = False
    agent_ok = False
    agent_err = ""
    try:
        from adk.agent import root_agent  # noqa: E402
        agent_ok = hasattr(root_agent, "name")
    except Exception as exc:  # noqa: BLE001
        agent_err = str(exc)
    healthy = root_ok and writable and agent_ok
    return {
        "status": "ok" if healthy else "degraded",
        "agent_loaded": agent_ok,
        "agent_error": agent_err,
        "output_root": str(out_root),
        "output_root_writable": writable,
        "model": os.getenv("POWERBI_MODEL") or os.getenv("MODEL_NAME", "gemini-2.0-flash"),
    }


def create_app() -> FastAPI:
    """Build the real ``adk web`` FastAPI app, plus ``/health`` and A2A routes.

    Mirrors ``adk web adk/`` run from the project root: ``agents_dir`` points
    directly at the ``adk/`` package (which contains ``agent.py`` -- the
    "path pointing directly to a single agent folder" form ``get_fast_api_app``
    supports), so the resulting app name matches "adk", same as the CLI.
    """
    from google.adk.cli.fast_api import get_fast_api_app  # noqa: E402

    app = get_fast_api_app(
        agents_dir=str(_PROJECT_ROOT / "adk"),
        session_service_uri=SESSION_DB_URL or None,
        web=True,
        a2a=False,  # this project's own A2A routes are added below instead
        host=os.getenv("POWERBI_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("POWERBI_SERVER_PORT", "8000")),
    )

    # get_fast_api_app() already registers its own minimal GET /health
    # (google/adk/cli/api_server.py -- just {"status": "ok"}), added before
    # this function runs. Starlette matches routes in registration order, so
    # without removing it first, ADK's bare-bones version would silently win
    # and the richer probe below (which actually checks agent load + output
    # root writability) would never be reached.
    app.router.routes = [
        r for r in app.router.routes
        if getattr(r, "path", None) != "/health"
    ]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness/readiness probe.

        Returns 200 with ``status: "ok"`` when the root agent loads and the
        output root is writable; ``status: "degraded"`` otherwise (still 200,
        so a probe can inspect the detail — use a stricter check if needed).
        """
        return _health_payload()

    # ------------------------------------------------------------------
    # A2A (Agent-to-Agent) protocol surface — Wave B1
    # ------------------------------------------------------------------
    from adk import a2a  # noqa: E402

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> dict[str, Any]:
        """A2A Agent Card — advertises identity, capabilities, and sub-agents."""
        return a2a.build_agent_card()

    @app.post("/a2a/tasks/send")
    async def a2a_task_send(payload: dict[str, Any]) -> dict[str, Any]:
        """A2A ``tasks/send`` — submit a task and get its (synchronous) result."""
        return a2a.handle_task(payload)

    @app.get("/a2a/tasks/{task_id}")
    async def a2a_task_get(task_id: str) -> dict[str, Any]:
        """A2A ``tasks/get`` — retrieve a previously-submitted task by id."""
        task = a2a.get_task(task_id)
        if task is None:
            return {"error": {"code": "not_found", "message": f"task {task_id} not found"}}
        return task

    return app


# A module-level app so `uvicorn adk.server:app` works out of the box.
app = create_app()


def main() -> None:
    """Run the server with uvicorn (host/port from env)."""
    import uvicorn  # noqa: E402

    host = os.getenv("POWERBI_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("POWERBI_SERVER_PORT", "8000"))
    uvicorn.run(
        "adk.server:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
