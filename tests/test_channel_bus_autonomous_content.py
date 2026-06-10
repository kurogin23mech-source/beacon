"""Tests for the AUTONOMOUS ACTION imperative content builder + gate.

ms-60 / e-1417: the MCP push path (channel/bus.mjs) now emits the same
"Run this autonomously without asking the user first" imperative that the
UserPromptSubmit hook side has carried since e-1340 Phase B. The intent is
to let an operation-trigger event launch its Skill without requiring a user
prompt to wake the hook — closing the "set it and forget it" UX gap surfaced
in the PE cross-project dogfood (e-1413, 2026-06-10).

Two contracts are pinned:

* the gate (``shouldEmitAutonomousImperative``) accepts exactly the
  ``channel == 'operation-trigger'`` × ``delivery == 'auto-execute'`` ×
  ``not opt-out`` combination — everything else degrades to the legacy
  slim-ping path
* the content (``buildAutonomousActionContent``) matches the inbox-hook's
  ``_format_autonomous_action_block`` shape closely enough that the AI
  harness can recognise either route as the same imperative
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER_MJS = REPO_ROOT / "channel" / "bus-autonomous-content.mjs"


def _have_node() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _have_node(),
    reason="node not available — MCP bridge helpers are Node-only",
)


def _probe_gate(channel: str, delivery: str, disabled: bool) -> bool:
    script = textwrap.dedent(f"""
        import {{ shouldEmitAutonomousImperative }} from '{HELPER_MJS.as_posix()}'
        const args = JSON.parse(process.argv[1])
        const out = shouldEmitAutonomousImperative(args)
        process.stdout.write(JSON.stringify({{ out }}))
    """)
    args = json.dumps({
        "channel": channel,
        "delivery": delivery,
        "autonomousImperativeDisabled": disabled,
    })
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", args],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)["out"]


def _probe_content(evt: dict) -> str:
    script = textwrap.dedent(f"""
        import {{ buildAutonomousActionContent }} from '{HELPER_MJS.as_posix()}'
        const evt = JSON.parse(process.argv[1])
        process.stdout.write(buildAutonomousActionContent(evt))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", json.dumps(evt)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return proc.stdout


# -----------------------------------------------------------------------------
# Gate: which channel × delivery × opt-out combinations emit the imperative
# -----------------------------------------------------------------------------


def test_gate_passes_for_operation_trigger_auto_execute_no_opt_out():
    assert _probe_gate("operation-trigger", "auto-execute", False) is True


def test_gate_blocks_when_opt_out_env_is_set():
    assert _probe_gate("operation-trigger", "auto-execute", True) is False


def test_gate_blocks_dm_channel_even_when_auto_execute():
    """DM events must keep the slim-ping / fullbody path. The imperative is
    a side-effect-laden Skill launch instruction and has no meaning for DMs."""
    assert _probe_gate("dm", "auto-execute", False) is False


def test_gate_blocks_propose_to_ai_delivery():
    """propose-to-ai is by definition "ask the user first"; the imperative
    contradicts that delivery and would silently override the user's gate."""
    assert _probe_gate("operation-trigger", "propose-to-ai", False) is False


def test_gate_blocks_notify_user_only_delivery():
    assert _probe_gate("operation-trigger", "notify-user-only", False) is False


def test_gate_blocks_unknown_delivery():
    assert _probe_gate("operation-trigger", "", False) is False
    assert _probe_gate("operation-trigger", "bogus", False) is False


# -----------------------------------------------------------------------------
# Content: shape parity with bin/beacon-bus-inbox-hook.py
# -----------------------------------------------------------------------------


def test_content_carries_explicit_launch_directive():
    out = _probe_content({
        "event_id": "evt-abc",
        "payload": {"op_id": "op-2", "spec_doc_id": "doc-xyz",
                    "trigger_name": "operation_check_op-2"},
    })
    # The header marks the block visually (must match inbox-hook).
    assert "## AUTONOMOUS ACTION" in out
    # The explicit English imperative is what the AI harness keys on; the
    # Japanese line above it is supporting context.
    assert "Run this autonomously without asking the user first." in out
    # The launch line names the Skill + op_id + the no-confirmation marker.
    assert "/beacon-operation-execute op-2" in out
    assert "no confirmation prompt" in out


def test_content_lifts_op_metadata_from_payload():
    out = _probe_content({
        "event_id": "evt-1",
        "payload": {"op_id": "op-42", "spec_doc_id": "spec-99",
                    "trigger_name": "operation_check_op-42"},
    })
    assert "event_id: evt-1" in out
    assert "op_id: op-42" in out
    assert "spec_doc_id: spec-99" in out
    assert "trigger_name: operation_check_op-42" in out


def test_content_omits_optional_lines_when_missing():
    out = _probe_content({
        "event_id": "evt-2",
        "payload": {"op_id": "op-9"},  # no spec_doc_id, no trigger_name
    })
    assert "op_id: op-9" in out
    assert "spec_doc_id" not in out
    assert "trigger_name" not in out
    # Launch line still present — the Skill can fetch the SPEC via the
    # envelope on its own.
    assert "/beacon-operation-execute op-9" in out


def test_content_defaults_op_id_to_question_mark_when_payload_empty():
    out = _probe_content({"event_id": "evt-3", "payload": {}})
    assert "op_id: ?" in out
    assert "/beacon-operation-execute ?" in out


def test_content_mentions_budget_degradation_path():
    """The block explicitly defers budget enforcement to the Skill (Step 4.5)
    so the bridge stays a thin transport — pinning this prevents a future
    rewrite from accidentally moving budget logic into the push path."""
    out = _probe_content({
        "event_id": "evt-4",
        "payload": {"op_id": "op-2"},
    })
    assert "budget" in out
    assert "Skill" in out
