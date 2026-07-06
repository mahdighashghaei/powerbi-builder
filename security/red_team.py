"""Red team — adversarial input generation for agent security testing.

The red team *attacks*: it generates malicious / edge-case inputs designed to
probe the agent's defences (path traversal, identifier injection, malformed
schema, oversized data). These inputs are then fed to the system by the blue
team to observe whether the controls hold.

This is part of the red/blue/green security-team pattern (Wave C1). The red
team does NOT execute attacks against a live system — it produces a catalogue
of adversarial inputs that the blue team runs in a controlled drill.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdversarialInput:
    """One adversarial test input produced by the red team."""

    id: str
    category: str  # path_traversal | identifier_injection | malformed_schema | ...
    description: str
    payload: dict[str, Any] = field(default_factory=dict)
    expected_defense: str = ""  # which control should stop this


def generate_attacks() -> list[AdversarialInput]:
    """Generate the red-team catalogue of adversarial inputs.

    Each entry targets a specific control in the powerbi-builder security model
    (see ``utils/security.py`` + ``utils/identifiers.py``). The catalogue is
    deterministic so a drill is reproducible.
    """
    attacks: list[AdversarialInput] = [
        # --- Path traversal (targets safe_join) ---
        AdversarialInput(
            id="RT-001",
            category="path_traversal",
            description="relative parent traversal in output path",
            payload={"output_dir": "../../etc", "table_name": "evil"},
            expected_defense="safe_join rejects '..' segments",
        ),
        AdversarialInput(
            id="RT-002",
            category="path_traversal",
            description="nested traversal with backslashes (windows)",
            payload={"output_dir": "..\\..\\evil", "table_name": "x"},
            expected_defense="safe_join rejects '..' after path normalization",
        ),
        AdversarialInput(
            id="RT-003",
            category="path_traversal",
            description="absolute path escape attempt",
            payload={"output_dir": "/etc/passwd", "table_name": "x"},
            expected_defense="safe_join confines to allowed root",
        ),
        # --- Identifier injection (targets utils/identifiers) ---
        AdversarialInput(
            id="RT-004",
            category="identifier_injection",
            description="table name with single quote to break DAX quoting",
            payload={"table_name": "Sales'); DROP TABLE x; --", "column": "Amount"},
            expected_defense="quote_dax_table doubles single quotes",
        ),
        AdversarialInput(
            id="RT-005",
            category="identifier_injection",
            description="column name with embedded quote for TMDL injection",
            payload={"table_name": "T", "column": "A'mount"},
            expected_defense="quote_tmdl_identifier escapes quotes",
        ),
        # --- Malformed schema (targets schema_inference + JSON validation) ---
        AdversarialInput(
            id="RT-006",
            category="malformed_schema",
            description="empty CSV (no columns)",
            payload={"csv_content": "", "table_name": "empty"},
            expected_defense="schema inference handles empty input gracefully",
        ),
        AdversarialInput(
            id="RT-007",
            category="malformed_schema",
            description="CSV with only a header row (no data)",
            payload={"csv_content": "A,B,C\n", "table_name": "header_only"},
            expected_defense="schema inference produces a valid empty schema",
        ),
        # --- JSON validation (targets validate_json_string) ---
        AdversarialInput(
            id="RT-008",
            category="malformed_json",
            description="invalid JSON passed to a write",
            payload={"json_text": "{not valid json", "target": "report.json"},
            expected_defense="validate_json_string raises JSONValidationError",
        ),
        # --- Oversized / denial-of-service-ish (targets measure caps) ---
        AdversarialInput(
            id="RT-009",
            category="resource_exhaustion",
            description="schema with hundreds of numeric columns (measure cap)",
            payload={"column_count": 500, "all_numeric": True, "table_name": "wide"},
            expected_defense="MAX_AUTO_MEASURES caps measure generation",
        ),
    ]
    return attacks


__all__ = ["AdversarialInput", "generate_attacks"]
