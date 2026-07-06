"""ADK tools for on-demand skill loading (Progressive Disclosure).

These tools realise the *progressive disclosure* pattern: only a lightweight
skill index lives in the root agent's system prompt (name + one-line
description); the full skill body and its ``references/`` / ``resources/`` /
``assets/`` files are fetched **on demand** by the model via these tools.

This keeps the context window light (preventing *context rot*) while still
giving the model access to the full, precise instructions of a skill exactly
when it needs them.

Tools:
    * ``list_skills``           — return the skill index (metadata only).
    * ``load_skill_detail``     — return the full SKILL.md body + file listing.
    * ``load_skill_reference``  — return the contents of one reference file.

All tools return the project's standard envelope:
``{"ok": bool, "tool": str, "message": str, "data": dict, "errors": list}``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adk.skills_index import (  # noqa: E402
    build_index,
    list_reference_files,
    read_reference_file,
    read_skill_body,
    skill_dir,
)


def list_skills() -> dict[str, Any]:
    """List all available skills as a lightweight index (metadata only).

    Returns the skill ``name``, a one-line ``description``, the ``folder`` name,
    and which supporting subdirs (``references`` / ``resources`` / ``assets``)
    exist. No skill bodies are loaded — call ``load_skill_detail`` to fetch the
    full instructions for a specific skill when you need them.

    Returns:
        ``{"ok": True, "tool": "list_skills", "count": N, "skills": [...]}``
    """
    index = build_index()
    skills = [m.as_dict() for m in index]
    return {
        "ok": True,
        "tool": "list_skills",
        "message": f"{len(skills)} skills available",
        "count": len(skills),
        "skills": skills,
    }


def load_skill_detail(name: str) -> dict[str, Any]:
    """Load the full body + file listing of one skill on demand.

    Call this only when you actually need the detailed instructions of a skill
    (the index from ``list_skills`` is enough to decide *which* skill). The
    returned ``body`` is the SKILL.md content with frontmatter stripped;
    ``references`` / ``resources`` / ``assets`` list the available supporting
    files you can then fetch with ``load_skill_reference``.

    Args:
        name: Skill ``name`` (e.g. ``semantic-model-authoring``) or folder name.

    Returns:
        ``{"ok": True, "tool": "load_skill_detail", "data": {name, body,
        references, resources, assets}}`` or ``{"ok": False, "errors": [...]}``
        if the skill is unknown.
    """
    # ``name`` may be the canonical skill name or the folder name.
    folder = name
    d = skill_dir(folder)
    if d is None:
        # Fall back to a name->folder lookup across the index.
        for meta in build_index():
            if meta.name == name:
                folder = meta.folder
                d = skill_dir(folder)
                break
    if d is None:
        return {
            "ok": False,
            "tool": "load_skill_detail",
            "message": f"unknown skill: {name}",
            "errors": [f"unknown skill: {name}"],
        }
    body = read_skill_body(folder)
    references = list_reference_files(folder, "references")
    resources = list_reference_files(folder, "resources")
    assets = list_reference_files(folder, "assets")
    return {
        "ok": True,
        "tool": "load_skill_detail",
        "message": f"loaded skill '{folder}' ({len(body)} chars)",
        "data": {
            "name": folder,
            "body": body,
            "references": references,
            "resources": resources,
            "assets": assets,
        },
    }


def load_skill_reference(
    name: str, reference: str, kind: str = "references"
) -> dict[str, Any]:
    """Load the contents of a single supporting file from a skill.

    Use the file listing returned by ``load_skill_detail`` to pick a
    ``reference`` path (relative to the skill's ``kind`` subdir).

    Args:
        name: Skill ``name`` or folder name.
        reference: Relative path of the file (e.g. ``dax-guidelines.md``).
        kind: One of ``references`` / ``resources`` / ``assets`` (default
            ``references``).

    Returns:
        ``{"ok": True, "tool": "load_skill_reference", "data": {name, kind,
        reference, content}}`` or ``{"ok": False, "errors": [...]}``.
    """
    if kind not in ("references", "resources", "assets"):
        return {
            "ok": False,
            "tool": "load_skill_reference",
            "message": f"invalid kind: {kind}",
            "errors": [f"kind must be references/resources/assets, got {kind}"],
        }
    folder = name
    if skill_dir(folder) is None:
        for meta in build_index():
            if meta.name == name:
                folder = meta.folder
                break
    content = read_reference_file(folder, reference, kind=kind)
    if content is None:
        return {
            "ok": False,
            "tool": "load_skill_reference",
            "message": f"file not found: {name}/{kind}/{reference}",
            "errors": [f"file not found: {name}/{kind}/{reference}"],
        }
    return {
        "ok": True,
        "tool": "load_skill_reference",
        "message": f"loaded {kind}/{reference} from '{folder}'",
        "data": {
            "name": folder,
            "kind": kind,
            "reference": reference,
            "content": content,
        },
    }


__all__ = ["list_skills", "load_skill_detail", "load_skill_reference"]
