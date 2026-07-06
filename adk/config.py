"""Configuration for the powerbi-builder ADK agent.

Env vars are cross-aliased with the legacy ``config.py`` so a single setting
works for both entry points:
  * model:  ``POWERBI_MODEL`` (ADK convention) OR ``MODEL_NAME`` (legacy)
  * output: ``POWERBI_OUTPUT_ROOT`` (ADK) OR ``OUTPUT_DIR`` (legacy)
The ADK-specific names take precedence when both are set.

``.env`` is loaded here (via python-dotenv) so that ``adk web`` / ``adk run``
— which import ``adk.config`` but NOT the legacy ``config.py`` — still pick up
``GOOGLE_API_KEY`` and other settings from the project root ``.env`` file.
Without this, the ADK CLI path would run fully offline even when a key is set.
"""
import os
from pathlib import Path

# ADK-level project root (defined before the dotenv load so it is available
# even if python-dotenv is not installed).
_ROOT = Path(__file__).parent.parent

# Load .env from the project root BEFORE reading any env vars, so ``adk web``
# picks up GOOGLE_API_KEY / POWERBI_MODEL etc. just like the legacy CLI does.
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

# Model to use for the root agent (and all 9 specialist sub-agents in
# adk/agent.py, which all pass model=MODEL_NAME). Resolved centrally via
# utils/model_config.py — the single source of truth for provider/model
# selection across both the ADK layer and the agents/*.py pipeline.
#
# provider="google" (the default, preserving prior behavior exactly): a bare
# model string, e.g. "gemini-2.5-flash" — gemini-2.0-flash has been observed
# to return quota limit 0 ("429 RESOURCE_EXHAUSTED ... limit: 0") on some
# free-tier API keys, so 2.5-flash is the safer default.
# provider="anthropic"/"openai" (opt-in via LLM_PROVIDER): a LiteLlm(...)
# instance. Override the model with LLM_MODEL / POWERBI_MODEL / MODEL_NAME.
#
# Raises MissingAPIKeyError at import time if LLM_PROVIDER names a provider
# whose key isn't set — this is the correct place for that to fail loudly:
# ADK builds all 10 agents once at process startup, so a broken model
# configuration should stop the server immediately rather than surface as a
# mysterious per-request failure later.
from utils.model_config import get_adk_model  # noqa: E402

MODEL_NAME = get_adk_model()

# Default output directory (overridable per-run)
# POWERBI_OUTPUT_ROOT (ADK) takes precedence; OUTPUT_DIR (legacy) is the fallback.
OUTPUT_ROOT = (
    os.getenv("POWERBI_OUTPUT_ROOT")
    or os.getenv("OUTPUT_DIR", str(_ROOT / "output"))
)

# Skills directory (Microsoft skills-for-fabric pattern)
SKILLS_DIR = _ROOT / "skills"

# Agent definitions directory
AGENTS_DIR = _ROOT / "agents"

# Root-agent generation temperature. Low for deterministic tool-calling;
# override with POWERBI_TEMPERATURE for experimentation.
TEMPERATURE = float(os.getenv("POWERBI_TEMPERATURE", "0.1"))

# Max chars when truncating skill markdown loaded into agent context. Avoids
# blowing the context window with very long skill docs.
SKILL_MAX_CHARS = int(os.getenv("POWERBI_SKILL_MAX_CHARS", "2000"))

# Cap on the number of auto-suggested measures produced by the deterministic
# DAX generator. 10 matches the documented "5-10 measures" range; raise it
# for wide fact tables with many amount/qty columns.
MAX_AUTO_MEASURES = int(os.getenv("POWERBI_MAX_AUTO_MEASURES", "10"))

# Session persistence. When set to a SQLAlchemy async URL the REPL uses
# ``DatabaseSessionService`` so sessions survive restarts. For SQLite use the
# async driver form: ``sqlite+aiosqlite:///./output/adk_sessions.db``
# (the ``aiosqlite`` package is required). Empty/None falls back to
# ``InMemorySessionService`` (sessions lost on process exit).
SESSION_DB_URL = os.getenv("POWERBI_SESSION_DB_URL", "")

# --- Trajectory evaluation (OpenTelemetry, Wave A4) -----------------------
# Off by default (fail-safe). Set POWERBI_OTEL_ENABLED=1 to record each agent +
# tool execution as an OTel span for step-by-step trajectory evaluation.
# Exporter: "file" (JSONL trajectory, default) | "otlp" (collector) | "none".
# POWERBI_OTEL_FILE defaults to <output_root>/trajectory.jsonl.
OTEL_ENABLED = os.getenv("POWERBI_OTEL_ENABLED", "0") == "1"
OTEL_EXPORTER = os.getenv("POWERBI_OTEL_EXPORTER", "file")
OTEL_FILE = os.getenv("POWERBI_OTEL_FILE", "")
OTEL_ENDPOINT = os.getenv("POWERBI_OTEL_ENDPOINT", "")
