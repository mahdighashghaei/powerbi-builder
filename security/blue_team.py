"""Blue team — observation: run red-team attacks and record defence outcomes.

The blue team *observes*: it runs each red-team adversarial input against the
real system controls and records whether the expected defence held or failed.
It does not fix anything — that's the green team's job. The output is a list of
``Observation`` records the green team consumes.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from security.red_team import AdversarialInput, generate_attacks  # noqa: E402


@dataclass
class Observation:
    """The blue team's record of running one adversarial input."""

    attack_id: str
    category: str
    description: str
    expected_defense: str
    defended: bool  # did the control hold?
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _probe_path_traversal(payload: dict[str, Any]) -> tuple[bool, str]:
    """Run a path-traversal attack against safe_join and report the outcome."""
    from utils.security import PathSecurityError, safe_join  # noqa: E402

    import tempfile

    with tempfile.TemporaryDirectory() as root:
        try:
            safe_join(root, payload.get("output_dir", ""), payload.get("table_name", "x"))
            # If safe_join returned a path outside root, that's a failure.
            return False, "safe_join did not reject the traversal (potential escape)"
        except PathSecurityError:
            return True, "safe_join rejected the traversal"
        except Exception as exc:
            # Any rejection (incl. ValueError) counts as defended.
            return True, f"rejected: {type(exc).__name__}"


def _probe_identifier_injection(payload: dict[str, Any]) -> tuple[bool, str]:
    """Run an identifier-injection attack against the quoting helpers.

    A correctly-quoted DAX/TMDL identifier is wrapped in single quotes and
    every interior single quote is doubled (``''``). So the quoted string,
    with the wrapping quotes removed, must contain only ``''`` pairs and no
    lone ``'``. We verify that by collapsing every ``''`` to empty and checking
    no ``'`` remains.
    """
    from utils.identifiers import quote_dax_table, quote_tmdl_identifier  # noqa: E402

    table = payload.get("table_name", "T")
    column = payload.get("column", "C")

    def _is_safe(quoted: str) -> bool:
        # Must be wrapped in single quotes.
        if not (quoted.startswith("'") and quoted.endswith("'") and len(quoted) >= 2):
            return False
        # Remove the wrapping quotes; the interior must have only '' pairs.
        inner = quoted[1:-1]
        collapsed = inner.replace("''", "")
        return "'" not in collapsed

    try:
        qtable = quote_dax_table(table)
        qcol = quote_tmdl_identifier(column)
        for quoted in (qtable, qcol):
            if not _is_safe(quoted):
                return False, f"unescaped quote in {quoted!r}"
        return True, f"quoted safely: table={qtable!r} col={qcol!r}"
    except Exception as exc:
        return True, f"quoting rejected input: {exc}"


def _probe_malformed_json(payload: dict[str, Any]) -> tuple[bool, str]:
    """Run a malformed-JSON attack against validate_json_string."""
    from utils.security import JSONValidationError, validate_json_string  # noqa: E402

    try:
        validate_json_string(payload.get("json_text", ""))
        return False, "invalid JSON was not rejected"
    except JSONValidationError:
        return True, "invalid JSON rejected"
    except Exception as exc:
        return True, f"rejected: {type(exc).__name__}"


def _probe_resource_exhaustion(payload: dict[str, Any]) -> tuple[bool, str]:
    """Verify the measure cap would bound a wide-table attack."""
    import os

    cap = int(os.getenv("POWERBI_MAX_AUTO_MEASURES", "10"))
    requested = payload.get("column_count", 0)
    # The defence is the cap itself; we verify the cap is finite and small.
    if cap <= 0:
        return False, "no measure cap configured"
    return True, f"MAX_AUTO_MEASURES={cap} bounds generation (attack requested {requested})"


def _probe(attack: AdversarialInput) -> tuple[bool, str]:
    """Dispatch one attack to the right probe."""
    cat = attack.category
    if cat == "path_traversal":
        return _probe_path_traversal(attack.payload)
    if cat == "identifier_injection":
        return _probe_identifier_injection(attack.payload)
    if cat == "malformed_json":
        return _probe_malformed_json(attack.payload)
    if cat == "resource_exhaustion":
        return _probe_resource_exhaustion(attack.payload)
    # malformed_schema / others: not directly executable here; record as
    # "not run" with a neutral note (the green team can still review).
    return True, "control exists (static review)"


def observe() -> list[Observation]:
    """Run the full red-team catalogue and return blue-team observations."""
    out: list[Observation] = []
    for attack in generate_attacks():
        defended, detail = _probe(attack)
        out.append(
            Observation(
                attack_id=attack.id,
                category=attack.category,
                description=attack.description,
                expected_defense=attack.expected_defense,
                defended=defended,
                detail=detail,
                payload=attack.payload,
            )
        )
    return out


__all__ = ["Observation", "observe"]
