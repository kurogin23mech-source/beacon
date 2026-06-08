"""Tests for the MCP bridge's envelope tier-selection helper (ms-54 / e-1290).

The reply tool in ``channel/bus.mjs`` mints a bus envelope before posting,
falling back to the legacy lane on 404 / 400. The tier-selection logic
(``selectTierForBridge`` + ``validateT5Payload``) lives in
``channel/bus-envelope.mjs`` so it's unit-testable without standing up the
full MCP server.

We drive the helper from Python via ``node`` (same pattern as
``test_bus_budget_mcp_reply.py``) so the assertion shape matches the rest of
this test suite. Going through Node ensures we test the actual module the MCP
server imports — no test-private re-implementation that could silently drift
from the production shape.

Pinned contract:

  * Payload conforms to the T5 short-ping schema (keys ⊆ {ping, ack, status,
    kind, ts}, primitive values, strings ≤32 chars) AND no requested action
    → tier T5, empty actions_authorized.
  * Anything else (free text, requested action, long string) → tier T1 with
    empty actions_authorized. T1 establishes provenance + chain_depth and
    unlocks ``propose-to-ai`` delivery; the receiver still won't auto-execute
    arbitrary actions because actions_authorized is [].
  * T3 reply chains are intentionally out of scope here — the MCP bridge
    always sends T1 or T5.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUS_ENVELOPE_MJS = REPO_ROOT / "channel" / "bus-envelope.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — MCP bridge tier-selection is Node-only",
)


def _probe(payload, requested_action=None) -> dict:
    """Run selectTierForBridge against ``payload`` and return its verdict.

    The probe imports the helper from its real path so any breakage in
    channel/bus-envelope.mjs surfaces here as a probe failure (no test-private
    duplicate of the logic).
    """
    script = textwrap.dedent(f"""
        import {{ selectTierForBridge, validateT5Payload }} from '{BUS_ENVELOPE_MJS.as_posix()}'
        const payload = JSON.parse(process.argv[1])
        const requestedAction = process.argv[2] === 'null' ? null : process.argv[2]
        const verdict = selectTierForBridge(payload, requestedAction)
        const t5err = validateT5Payload(payload)
        process.stdout.write(JSON.stringify({{ verdict, t5err }}))
    """)
    payload_arg = json.dumps(payload)
    action_arg = requested_action if requested_action is not None else "null"
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script,
         "--", payload_arg, action_arg],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# T5 ping-shape passes the validator and selects T5
# ---------------------------------------------------------------------------


def test_short_ping_with_no_action_selects_t5():
    result = _probe({"ping": "ok"})
    assert result["t5err"] is None
    assert result["verdict"] == {
        "tier": "T5", "actionsAuthorized": [], "dataClass": "free",
    }


def test_short_ping_with_multiple_allowed_keys_selects_t5():
    result = _probe({"ack": "yes", "status": "awake", "ts": 1234567890})
    assert result["t5err"] is None
    assert result["verdict"]["tier"] == "T5"


def test_short_ping_with_requested_action_selects_t1():
    """A payload that *would* qualify as T5 still becomes T1 when an action
    is requested — T5 forbids actions outright (server rule)."""
    result = _probe({"ping": "ok"}, requested_action="deploy")
    # Validator says the payload is fine on its own; tier selection still
    # bumps to T1 because of the action.
    assert result["t5err"] is None
    assert result["verdict"]["tier"] == "T1"
    assert result["verdict"]["actionsAuthorized"] == []


# ---------------------------------------------------------------------------
# Free-text payloads (the common reply-tool case) fall to T1
# ---------------------------------------------------------------------------


def test_text_payload_does_not_qualify_for_t5():
    """The MCP reply tool always carries a ``text`` field. That key isn't in
    the T5 allowlist so the validator rejects it; tier selection falls to T1."""
    payload = {"recipient_session_id": "S1", "text": "hello", "source_project": "P"}
    result = _probe(payload)
    assert result["t5err"] is not None  # validator rejected
    assert result["verdict"]["tier"] == "T1"
    assert result["verdict"]["actionsAuthorized"] == []


def test_long_ping_string_falls_back_to_t1():
    """T5 caps string values at 32 chars to prevent free-text smuggling
    through ping shape. A 33-char value pushes us to T1."""
    long_value = "x" * 33
    result = _probe({"status": long_value})
    assert result["t5err"] is not None
    assert "32 chars" in result["t5err"] or "exceeds" in result["t5err"]
    assert result["verdict"]["tier"] == "T1"


def test_non_primitive_ping_value_falls_back_to_t1():
    """Nested objects / arrays in ping values aren't primitives, so the
    validator rejects them and tier-selection lands on T1."""
    result = _probe({"status": {"nested": "obj"}})
    assert result["t5err"] is not None
    assert result["verdict"]["tier"] == "T1"


def test_array_payload_rejected_and_falls_back_to_t1():
    """A non-dict payload isn't a valid T5 payload at all."""
    result = _probe([1, 2, 3])
    assert result["t5err"] is not None
    assert result["verdict"]["tier"] == "T1"


def test_empty_payload_qualifies_for_t5():
    """An empty dict trivially passes the T5 schema (no disallowed keys, no
    over-long values). With no action, we mint a T5 envelope."""
    result = _probe({})
    assert result["t5err"] is None
    assert result["verdict"]["tier"] == "T5"
