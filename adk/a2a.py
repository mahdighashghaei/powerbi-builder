"""Agent-to-Agent (A2A) protocol surface for powerbi-builder.

The A2A protocol is a standard for inter-agent collaboration: an agent
publishes an **AgentCard** describing its identity and capabilities, and accepts
**tasks** from other agents (or orchestrators) over HTTP/JSON. This module
implements the A2A surface for the powerbi-builder root agent:

  * :func:`build_agent_card` — serialize the root agent + its sub-agents into an
    A2A AgentCard (served at ``/.well-known/agent-card.json``).
  * :func:`handle_task` — execute a submitted task against the root agent and
    return an A2A-compliant task status/result.

This is the *real* A2A protocol shape (agent card + task send/get), mounted on
the existing FastAPI app in ``adk/server.py``. It is deliberately lightweight —
it does not require a separate A2A SDK — so the project stays installable
without extra dependencies while still speaking the A2A contract.

Reference: https://a2a-protocol.org/ (Agent Card + Tasks API).
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------

def _root_agent():
    """Import and return the root agent lazily (avoid import cycle at load)."""
    from adk.agent import root_agent  # noqa: E402

    return root_agent


def build_agent_card() -> dict[str, Any]:
    """Build an A2A Agent Card from the powerbi-builder root agent.

    The card advertises the agent's identity, capabilities, and the specialist
    sub-agents it can delegate to. Served at ``/.well-known/agent-card.json``.
    """
    agent = _root_agent()
    sub_agents = []
    for sub in getattr(agent, "sub_agents", []) or []:
        sub_agents.append(
            {
                "name": getattr(sub, "name", str(sub)),
                "description": getattr(sub, "description", "") or "",
            }
        )
    # Collect the tool names the agent exposes (for the skills field).
    tool_names = []
    for t in getattr(agent, "tools", []) or []:
        tool_names.append(getattr(t, "__name__", getattr(t, "name", str(t))))
    return {
        "name": getattr(agent, "name", "powerbi_builder"),
        "description": getattr(agent, "description", "") or "",
        "version": "1.0.0",
        "protocol": "A2A",
        "url": "",  # filled by the server when mounted (base URL + /a2a)
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransition": True,
        },
        "skills": [
            {
                "id": tool,
                "name": tool,
                "description": f"Power BI builder tool: {tool}",
            }
            for tool in tool_names
        ],
        "subAgents": sub_agents,
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "json"],
    }


# ---------------------------------------------------------------------------
# Task handling
# ---------------------------------------------------------------------------

# In-memory task store (a production deployment would persist these).
_TASKS: dict[str, dict[str, Any]] = {}


def handle_task(params: dict[str, Any]) -> dict[str, Any]:
    """Execute an A2A task and return the task object with a final status.

    The ``params`` follow the A2A ``tasks/send`` shape::

        {"task": {"id": "...", "message": {"parts": [{"text": "..."}]}}}}

    If no task id is supplied, one is generated. The task is run synchronously
    (the powerbi-builder pipeline is fast for offline deterministic builds) and
    returned with ``state: "completed"`` (or ``"failed"`` on error). A
    fail-safe philosophy applies: a malformed task yields a ``failed`` task
    object with an error message, never an exception.
    """
    task_in = params.get("task") or {}
    task_id = task_in.get("id") or f"task-{uuid.uuid4().hex[:12]}"
    message = task_in.get("message") or {}
    parts = message.get("parts") or []
    prompt = " ".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type", "text") == "text"
    ).strip()
    if not prompt:
        prompt = task_in.get("input", "") or ""

    now = _now_iso()
    task: dict[str, Any] = {
        "id": task_id,
        "state": "working",
        "createdAt": now,
        "updatedAt": now,
        "input": prompt,
        "messages": [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
        ],
    }

    if not prompt:
        task["state"] = "failed"
        task["error"] = {"code": "empty_prompt", "message": "No task prompt provided."}
        _TASKS[task_id] = task
        return task

    # Execute the prompt against the root agent via the high-level generate
    # pipeline. We use the deterministic generate_pbip path so an A2A task can
    # produce a real PBIP without requiring an LLM round-trip.
    try:
        result = _execute_prompt(prompt)
        task["state"] = "completed"
        task["result"] = result
        task["messages"].append(
            {
                "role": "agent",
                "parts": [
                    {
                        "type": "text",
                        "text": _summarize_result(result),
                    }
                ],
            }
        )
    except Exception as exc:  # fail-safe: never raise to the A2A caller
        task["state"] = "failed"
        task["error"] = {"code": "execution_error", "message": str(exc)}
    task["updatedAt"] = _now_iso()
    _TASKS[task_id] = task
    return task


def get_task(task_id: str) -> dict[str, Any] | None:
    """Retrieve a previously-submitted task by id (A2A ``tasks/get``)."""
    return _TASKS.get(task_id)


def _execute_prompt(prompt: str) -> dict[str, Any]:
    """Run a natural-language prompt through the build pipeline.

    For a simple, deterministic A2A surface we parse a CSV path + description
    out of the prompt and call the in-process ``generate_pbip``. This keeps the
    A2A task self-contained without spinning up the full ADK Runner (which would
    need an LLM). External A2A clients get a real PBIP build back.
    """
    from mcp_server.highlevel import generate_pbip as _gen  # noqa: E402

    source, description = _parse_prompt(prompt)
    if not source:
        raise ValueError(
            "Could not find a .csv/.xlsx/.json file path in the task prompt."
        )
    return _gen(
        source=source,
        description=description or prompt,
        project_name=None,
        output_root=str(_ROOT / "output"),
    )


def _parse_prompt(prompt: str) -> tuple[str, str]:
    """Extract a source file path and a description from a free-text prompt.

    Looks for a .csv/.xlsx/.json path token; the rest is the description.
    """
    import re

    m = re.search(r'([\w./\\-]+\.(?:csv|xlsx|json))', prompt, re.IGNORECASE)
    source = m.group(1) if m else ""
    description = prompt.replace(m.group(0), "").strip() if m else prompt
    return source, description


def _summarize_result(result: dict[str, Any]) -> str:
    data = result.get("data", {}) if isinstance(result, dict) else {}
    if result.get("ok"):
        return (
            f"Built '{data.get('project_name', '?')}' — "
            f"validation: {data.get('validation', {}).get('ok', '?')}. "
            f"Location: {data.get('pbip_root', '?')}"
        )
    return f"Build did not complete: {result.get('message', 'unknown error')}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["build_agent_card", "handle_task", "get_task"]
