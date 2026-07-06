"""Google ADK integration for powerbi-builder.

Re-exports the root agent and app so consumers can do ``from adk import
root_agent, app`` without reaching into :mod:`adk.agent` directly. ``adk web``
discovers them by scanning ``adk/agent.py`` as well.
"""

from .agent import app, root_agent

__all__ = ["root_agent", "app"]
