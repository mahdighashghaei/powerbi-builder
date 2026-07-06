"""Tests for progressive-disclosure skill loading (Wave A1).

Verifies:
  * frontmatter parsing (name + folded description) without pyyaml.
  * the skill index is lightweight (metadata only, no bodies).
  * on-demand loading of a skill body + reference files.
  * path containment on reference reads (no ``..`` traversal).
  * the root agent instruction contains the index table, not full bodies.
  * the system prompt is materially smaller than the old eager approach.

Stdlib unittest — runs under ``python -m pytest tests/ -v`` and
``python tests/test_skills_progressive.py``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adk.skills_index import (  # noqa: E402
    build_index,
    index_as_table,
    list_reference_files,
    parse_frontmatter,
    read_reference_file,
    read_skill_body,
)
from adk.tools.skill_tools import (  # noqa: E402
    list_skills,
    load_skill_detail,
    load_skill_reference,
)


class TestFrontmatterParsing(unittest.TestCase):
    """parse_frontmatter extracts name + folded description; strips frontmatter."""

    def test_parses_name_and_folded_description(self):
        text = (
            "---\n"
            "name: my-skill\n"
            "description: >\n"
            "  This is a folded\n"
            "  description spanning\n"
            "  several lines.\n"
            "---\n\n"
            "Body starts here.\n"
        )
        fields, body = parse_frontmatter(text)
        self.assertEqual(fields["name"], "my-skill")
        self.assertEqual(
            fields["description"],
            "This is a folded description spanning several lines.",
        )
        self.assertTrue(body.lstrip().startswith("Body starts here."))

    def test_strip_folded_indicator(self):
        text = (
            "---\n"
            "name: s\n"
            "description: >-\n"
            "  one line\n"
            "---\n\nbody\n"
        )
        fields, body = parse_frontmatter(text)
        self.assertEqual(fields["description"], "one line")
        self.assertIn("body", body)

    def test_no_frontmatter_returns_empty_fields_and_full_body(self):
        text = "no front matter\nhere\n"
        fields, body = parse_frontmatter(text)
        self.assertEqual(fields, {})
        self.assertEqual(body, text)

    def test_unknown_metadata_key_ignored(self):
        text = (
            "---\n"
            "name: s\n"
            "description: >\n"
            "  desc\n"
            "metadata:\n"
            "  version: 0.1.0\n"
            "---\n\nbody\n"
        )
        fields, body = parse_frontmatter(text)
        self.assertEqual(fields["name"], "s")
        self.assertEqual(fields["description"], "desc")
        self.assertIn("body", body)
        # metadata is not surfaced in the lightweight fields.
        self.assertNotIn("metadata", fields)


class TestSkillIndex(unittest.TestCase):
    """build_index scans the real skills/ folder."""

    def test_index_has_all_32_skills(self):
        idx = build_index()
        # The project ships 32 skill folders, each with a SKILL.md.
        self.assertEqual(len(idx), 32)

    def test_index_entries_have_name_and_description(self):
        idx = build_index()
        names = {m.name for m in idx}
        self.assertIn("semantic-model-authoring", names)
        self.assertIn("powerbi-report-authoring", names)
        # The two skills that use quoted (non-folded) descriptions must parse too.
        self.assertIn("fabriciq-ontology-authoring-cli", names)
        self.assertIn("powerbi-report-management", names)
        # Every entry has a non-empty description (all shipped skills have one).
        for m in idx:
            self.assertTrue(m.description, f"skill {m.name} missing description")

    def test_index_is_sorted_by_name(self):
        idx = build_index()
        names = [m.name for m in idx]
        self.assertEqual(names, sorted(names))

    def test_index_table_is_compact(self):
        table = index_as_table(build_index())
        # 32 skills => 2 header lines + 32 rows = 34 lines.
        lines = table.split("\n")
        self.assertEqual(len(lines), 34)
        # The table must NOT contain the full skill body (which is long).
        self.assertNotIn("Update Check — ONCE PER SESSION", table)

    def test_index_is_much_smaller_than_eager_bodies(self):
        """The lightweight index must be materially smaller than loading all
        skill bodies — that is the whole point of progressive disclosure."""
        idx = build_index()
        index_size = sum(len(m.description) + len(m.name) for m in idx)
        # Sum the full body sizes for the 5 skills the old code eagerly loaded.
        eager_skills = [
            "semantic-model-authoring",
            "powerbi-report-authoring",
            "powerbi-report-planning",
            "powerbi-report-design",
            "powerbi-report-management",
        ]
        eager_size = sum(len(read_skill_body(s)) for s in eager_skills)
        # The index (all 32) should be smaller than even just the 5 eager bodies.
        self.assertLess(index_size, eager_size)


class TestOnDemandLoading(unittest.TestCase):
    """load_skill_detail / load_skill_reference fetch on demand."""

    def test_load_skill_detail_returns_body_and_refs(self):
        r = load_skill_detail("semantic-model-authoring")
        self.assertTrue(r["ok"])
        data = r["data"]
        self.assertEqual(data["name"], "semantic-model-authoring")
        self.assertIsInstance(data["body"], str)
        self.assertGreater(len(data["body"]), 100)
        # semantic-model-authoring has a references/ subdir.
        self.assertIn("references", data)
        self.assertIn("dax-guidelines.md", data["references"])

    def test_load_skill_detail_unknown_skill(self):
        r = load_skill_detail("does-not-exist")
        self.assertFalse(r["ok"])
        self.assertTrue(r["errors"])

    def test_load_skill_reference_content(self):
        r = load_skill_reference("semantic-model-authoring", "dax-guidelines.md")
        self.assertTrue(r["ok"])
        self.assertGreater(len(r["data"]["content"]), 50)

    def test_load_skill_reference_traversal_blocked(self):
        r = load_skill_reference("semantic-model-authoring", "../../etc/passwd")
        self.assertFalse(r["ok"])

    def test_list_reference_files(self):
        files = list_reference_files("semantic-model-authoring", "references")
        self.assertIn("dax-guidelines.md", files)
        # No files for an absent subdir kind.
        self.assertEqual(list_reference_files("semantic-model-authoring", "assets"), [])


class TestSkillToolsEnvelope(unittest.TestCase):
    """Tools return the standard {ok, tool, message, data, errors} envelope."""

    def test_list_skills_envelope(self):
        r = list_skills()
        self.assertTrue(r["ok"])
        self.assertEqual(r["tool"], "list_skills")
        self.assertEqual(r["count"], 32)
        self.assertEqual(len(r["skills"]), 32)
        # No 'errors' key on success, or it's empty.
        self.assertFalse(r.get("errors", []))


class TestRootAgentInstruction(unittest.TestCase):
    """The root agent instruction carries the index, not full skill bodies."""

    def test_instruction_contains_index_table_header(self):
        import adk.agent as a  # noqa: E402

        self.assertIn("Available Skills (Progressive Disclosure)", a.root_agent.instruction)
        self.assertIn("| Skill | Description |", a.root_agent.instruction)
        # Should NOT embed the old full body of a skill.
        self.assertNotIn("## Available Skills (Reference)", a.root_agent.instruction)

    def test_skill_tools_registered_on_root(self):
        import adk.agent as a  # noqa: E402

        # ADK stores bare function refs in `tools` (it wraps them lazily), so
        # read the function __name__ rather than a .name attribute.
        tool_names = {getattr(t, "__name__", getattr(t, "name", "")) for t in a.root_agent.tools}
        self.assertIn("list_skills", tool_names)
        self.assertIn("load_skill_detail", tool_names)
        self.assertIn("load_skill_reference", tool_names)


if __name__ == "__main__":
    unittest.main()
