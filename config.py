"""Central configuration loader.

Loads settings from (in priority order):
  1. Environment variables
  2. A local ``.env`` file (via python-dotenv) -- NEVER committed
  3. Sensible built-in defaults

No secrets are read here except optional LLM API keys, and those are only
ever returned as *boolean flags* (``has_llm``) so they never leak into logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the whole project."""

    output_dir: Path = field(default_factory=lambda: Path("./output").resolve())
    log_file: Path | None = field(default_factory=lambda: Path("./logs/powerbi_builder.log").resolve())
    log_level: str = "INFO"
    model_name: str = "gemini-2.5-flash"
    has_llm: bool = False
    llm_provider: str | None = None

    # Optional LLM-assisted semantic decision layer for DataAnalyzerAgent /
    # DataCleanerAgent (advisor-only — never executes cleaning itself, never
    # touches raw data). Off by default: zero LLM calls, zero behavior
    # change, matching every other optional-LLM agent's fail-safe default.
    semantic_llm_assist_enabled: bool = False
    semantic_llm_confidence_threshold: float = 0.6

    # The single place where .pbip artefacts for the *current* run live.
    # Set per-invocation by the orchestrator after it picks a project name.
    pbip_root: Path | None = None


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def load_settings() -> Settings:
    """Build a :class:`Settings` instance from the environment / .env file.

    Provider/model detection is delegated to ``utils/model_config.py`` (the
    single source of truth shared with the ADK layer and the 5 optional-LLM
    pipeline agents) rather than duplicating its own key-detection logic.
    A ``MissingAPIKeyError`` (an explicit ``LLM_PROVIDER`` whose key is
    absent) degrades to ``has_llm=False`` here rather than raising — this
    function runs at import time via ``DEFAULT_SETTINGS`` below, and a
    misconfiguration must not crash the whole CLI before any agent gets a
    chance to log it; the loud failure still happens at the point of actual
    use (see ``utils/model_config.get_llm_config`` callers).
    """
    from utils.model_config import MissingAPIKeyError, get_llm_config

    try:
        llm_config = get_llm_config()
    except MissingAPIKeyError:
        llm_config = None

    output_dir = Path(os.getenv("OUTPUT_DIR", "./output")).expanduser().resolve()
    log_file_env = os.getenv("LOG_FILE", "./logs/powerbi_builder.log")
    log_file = Path(log_file_env).expanduser().resolve() if log_file_env else None

    return Settings(
        output_dir=output_dir,
        log_file=log_file,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        model_name=llm_config.model if llm_config else "gemini-2.5-flash",
        has_llm=llm_config is not None,
        llm_provider=llm_config.provider if llm_config else None,
        semantic_llm_assist_enabled=_bool_env("ENABLE_SEMANTIC_LLM_ASSIST", default=False),
        semantic_llm_confidence_threshold=_float_env("SEMANTIC_LLM_CONFIDENCE_THRESHOLD", 0.6),
    )


# Default singleton -- imported across agents + MCP server for convenience.
# Per-run overrides happen via Settings(...) instances passed explicitly.
DEFAULT_SETTINGS = load_settings()
