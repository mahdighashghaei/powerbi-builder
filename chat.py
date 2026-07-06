#!/usr/bin/env python3
"""Phase 8 — Interactive chat entry point.

Launches the powerbi-builder AI Chat Assistant REPL in the terminal.
This drives the Google ADK ``Runner`` directly (in-memory session,
artifact, and memory services) — no browser needed.

Usage:
    python chat.py
    python chat.py --output-dir ./output
    python chat.py --app-name powerbi_builder --user-id me

You can also start the stock ADK browser UI with:
    adk web adk/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from adk.chat import ChatRepl  # noqa: E402
from adk.config import OUTPUT_ROOT  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat.py",
        description="PowerBI Builder — interactive AI chat assistant (Google ADK).",
    )
    p.add_argument(
        "--output-dir",
        default=str(OUTPUT_ROOT),
        help="Base output directory for PBIP projects (default: ./output).",
    )
    p.add_argument(
        "--app-name",
        default="powerbi_builder",
        help="ADK app name (default: powerbi_builder).",
    )
    p.add_argument(
        "--user-id",
        default="repl_user",
        help="ADK user id (default: repl_user).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repl = ChatRepl(
        app_name=args.app_name,
        user_id=args.user_id,
        output_root=args.output_dir,
    )
    repl.cmd_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
