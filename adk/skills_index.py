"""Progressive-disclosure skill index for the powerbi-builder agent.

Instead of eagerly loading the full body of every ``SKILL.md`` into the root
agent's system prompt (the *context-rot* anti-pattern), this module builds a
**lightweight index** of all skills: just ``name`` + a one-line ``description``
extracted from the YAML frontmatter. The full skill body (and its
``references/`` / ``resources/`` / ``assets/`` files) is loaded **on demand**
via the tools in :mod:`adk.tools.skill_tools`.

Frontmatter parsing is intentionally dependency-free (no ``pyyaml`` required):
skills use a constrained YAML subset — a leading ``---`` block with ``name:``
and a folded ``description: >`` / ``description: >-`` block scalar. We parse
just those two fields and ignore the rest. If a SKILL.md is malformed or has no
frontmatter, it is still indexed with its folder name and an empty description
(fail-safe: never crash agent construction over a bad skill file).

See ``STRUCTURE.md`` §11 for the skill folder layout.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from adk.config import SKILLS_DIR  # noqa: E402  (sys.path set by adk.config import chain)

log_scope = "adk.skills_index"


@dataclass(frozen=True)
class SkillMeta:
    """Lightweight metadata for a single skill (no body loaded)."""

    name: str
    description: str
    folder: str
    has_references: bool
    has_resources: bool
    has_assets: bool

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Collapse the three has_* flags into a single list of subdir kinds,
        # which is what the index table and the on-demand tools consume.
        d["subdirs"] = [
            kind
            for kind, present in (
                ("references", self.has_references),
                ("resources", self.has_resources),
                ("assets", self.has_assets),
            )
            if present
        ]
        return {
            "name": self.name,
            "description": self.description,
            "folder": self.folder,
            "subdirs": d["subdirs"],
        }


_FM_OPEN = re.compile(r"\A---\s*\n")
# The closing fence is a line that is exactly '---' (possibly with trailing ws).
_FM_CLOSE = re.compile(r"\n---\s*\n")


def _unquote_scalar(value: str) -> str:
    """Unquote a single-line YAML scalar (plain, single- or double-quoted).

    Handles the common cases used in skill frontmatter:
      * ``"double quoted"`` — supports ``\\"`` / ``\\\\`` / ``\\n`` / ``\\t``.
      * ``'single quoted'`` — ``''`` is a literal ``'`` (YAML rule).
      * plain (unquoted) — returned as-is.
    Trailing whitespace is stripped. Does not support multi-line quoted scalars
    (skills keep descriptions on a single quoted line or use folded ``>``).
    """
    v = value.strip()
    if not v:
        return ""
    if v[0] == '"':
        # Find the matching closing quote (last double quote on the line).
        body = v[1:]
        if body.endswith('"'):
            body = body[:-1]
        # Apply the escape sequences YAML allows in double-quoted scalars.
        return (
            body.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    if v[0] == "'":
        body = v[1:]
        if body.endswith("'"):
            body = body[:-1]
        # In single-quoted YAML scalars, '' is an escaped literal quote.
        return body.replace("''", "'")
    return v


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into ``(frontmatter_fields, body)``.

    Only ``name`` and ``description`` are extracted; unknown keys are ignored.
    ``description`` is a folded block scalar (``>`` / ``>-``) so we join its
    indented continuation lines with spaces and collapse whitespace.

    Returns ``({}, text)`` if there is no leading frontmatter (fail-safe).
    """
    if not _FM_OPEN.match(text):
        return {}, text
    close = _FM_CLOSE.search(text, 3)
    if not close:
        # No closing fence — treat the whole file as body.
        return {}, text
    fm = text[4 : close.start() + 1]  # between the two '---' lines
    body = text[close.end() :]
    fields: dict[str, str] = {}
    lines = fm.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key in ("name",):
            fields[key] = _unquote_scalar(rest)
            i += 1
            continue
        if key == "description":
            if rest in (">", ">-"):
                # Folded block scalar: subsequent more-indented lines are content.
                folded: list[str] = []
                i += 1
                while i < n:
                    cont = lines[i]
                    # A blank line inside a folded scalar becomes a space.
                    if cont.strip() == "":
                        folded.append(" ")
                        i += 1
                        continue
                    # A less-indented line (or a key: value) ends the block.
                    if cont and not cont.startswith(" ") and not cont.startswith("\t"):
                        break
                    folded.append(cont.strip())
                    i += 1
                desc = " ".join(part for part in folded if part).strip()
                desc = re.sub(r"\s+", " ", desc)
                fields[key] = desc
                continue
            if rest and rest[0] in ("'", '"'):
                # Single-line quoted scalar (plain or with embedded quotes).
                fields[key] = _unquote_scalar(rest)
                i += 1
                continue
        # Unknown key (e.g. metadata:) — skip its scalar/block without parsing.
        if rest == "" :
            # likely a nested block; skip following indented lines
            i += 1
            while i < n and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                i += 1
        else:
            i += 1
    return fields, body


def _has_subdir(skill_dir: Path, kind: str) -> bool:
    sub = skill_dir / kind
    return sub.is_dir() and any(sub.iterdir())


def build_index(skills_dir: Path | None = None) -> list[SkillMeta]:
    """Scan ``skills_dir`` and return a sorted index of skill metadata.

    Skills are discovered by folder (each must contain a ``SKILL.md``). The
    index is sorted by skill ``name`` for stable output. This never raises on a
    single bad skill file — it is skipped with a warning logged via the audit
    logger.
    """
    from utils import AuditLogger  # noqa: E402  (import lazily to avoid cycles)

    log = AuditLogger.get(log_scope)

    root = skills_dir or SKILLS_DIR
    if not root.is_dir():
        return []
    index: list[SkillMeta] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem edge case
            log.warning("skill_index: cannot read %s: %s", skill_md, exc)
            continue
        try:
            fields, _body = parse_frontmatter(text)
        except Exception as exc:  # fail-safe: never break agent construction
            log.warning("skill_index: parse failed for %s: %s", skill_md, exc)
            fields = {}
        name = fields.get("name") or entry.name
        description = fields.get("description", "")
        index.append(
            SkillMeta(
                name=name,
                description=description,
                folder=entry.name,
                has_references=_has_subdir(entry, "references"),
                has_resources=_has_subdir(entry, "resources"),
                has_assets=_has_subdir(entry, "assets"),
            )
        )
    index.sort(key=lambda m: m.name)
    return index


def index_as_table(index: list[SkillMeta] | None = None) -> str:
    """Render the skill index as a compact Markdown table for the system prompt.

    This is the *only* skill content placed in the root agent's instruction —
    the model fetches full details on demand via ``load_skill_detail``.
    """
    idx = index if index is not None else build_index()
    if not idx:
        return "_(no skills available)_"
    lines = ["| Skill | Description |", "|---|---|"]
    for m in idx:
        desc = m.description if m.description else "_(no description)_"
        # Cap the description in the index table so the whole index stays light.
        if len(desc) > 160:
            desc = desc[:157].rstrip() + "..."
        lines.append(f"| `{m.name}` | {desc} |")
    return "\n".join(lines)


def skill_dir(folder: str) -> Path | None:
    """Return the directory for a skill ``folder`` name, or None if absent."""
    d = SKILLS_DIR / folder
    return d if d.is_dir() else None


def read_skill_body(folder: str) -> str:
    """Return the full SKILL.md body for ``folder`` (frontmatter stripped).

    Returns an empty string if the skill is unknown (fail-safe).
    """
    d = skill_dir(folder)
    if d is None:
        return ""
    skill_md = d / "SKILL.md"
    if not skill_md.is_file():
        return ""
    text = skill_md.read_text(encoding="utf-8")
    _fields, body = parse_frontmatter(text)
    return body.strip()


def list_reference_files(folder: str, kind: str = "references") -> list[str]:
    """List the relative paths of files under a skill's ``kind`` subdir.

    ``kind`` is one of ``references`` / ``resources`` / ``assets``. Returns a
    sorted list of POSIX-style relative paths (e.g. ``["dax-guidelines.md",
    "modeling-guidelines.md"]``). Empty list if the subdir is absent.
    """
    d = skill_dir(folder)
    if d is None:
        return []
    sub = d / kind
    if not sub.is_dir():
        return []
    files: list[str] = []
    for path in sorted(sub.rglob("*")):
        if path.is_file():
            files.append(path.relative_to(sub).as_posix())
    return files


def read_reference_file(folder: str, rel_path: str, kind: str = "references") -> str | None:
    """Read a single reference/resource/asset file from a skill.

    ``rel_path`` is relative to the ``kind`` subdir. Returns ``None`` if the
    file is absent or escapes the subdir (path-containment: no ``..``).
    """
    d = skill_dir(folder)
    if d is None:
        return None
    sub = d / kind
    if not sub.is_dir():
        return None
    # Resolve and confine to the subdir to prevent traversal.
    try:
        target = (sub / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        target.relative_to(sub.resolve())
    except ValueError:
        return None  # escaped the subdir
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


__all__ = [
    "SkillMeta",
    "parse_frontmatter",
    "build_index",
    "index_as_table",
    "skill_dir",
    "read_skill_body",
    "list_reference_files",
    "read_reference_file",
]
