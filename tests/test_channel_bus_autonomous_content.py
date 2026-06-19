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


# -----------------------------------------------------------------------------
# Trek channels (ms-75 / e-2069): hardcoded CHANNEL_TO_SKILL mapping so the
# MCP push route directly emits a "launch /beacon-trek-execute" or
# "launch /beacon-trek-review" imperative without AI interpretation.
# -----------------------------------------------------------------------------


def _probe_channel_to_skill(channel: str) -> str | None:
    script = textwrap.dedent(f"""
        import {{ CHANNEL_TO_SKILL }} from '{HELPER_MJS.as_posix()}'
        const out = CHANNEL_TO_SKILL[process.argv[1]] || null
        process.stdout.write(JSON.stringify({{ out }}))
    """)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", channel],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"node probe failed (rc={proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)["out"]


def test_channel_to_skill_mapping_covers_operation_and_trek_channels():
    assert _probe_channel_to_skill("operation-trigger") == "/beacon-operation-execute"
    assert _probe_channel_to_skill("trek-progress-check") == "/beacon-trek-execute"
    assert _probe_channel_to_skill("trek-trigger") == "/beacon-trek-execute"
    assert _probe_channel_to_skill("trek-task-review") == "/beacon-trek-review"


def test_channel_to_skill_returns_null_for_unmapped_channel():
    assert _probe_channel_to_skill("dm") is None
    assert _probe_channel_to_skill("unknown-channel") is None


def test_gate_passes_for_trek_progress_check_auto_execute():
    assert _probe_gate("trek-progress-check", "auto-execute", False) is True


def test_gate_passes_for_trek_trigger_auto_execute():
    assert _probe_gate("trek-trigger", "auto-execute", False) is True


def test_gate_passes_for_trek_task_review_auto_execute():
    assert _probe_gate("trek-task-review", "auto-execute", False) is True


def test_gate_blocks_trek_channel_with_non_auto_execute_delivery():
    """Trek channels still require auto-execute delivery; a propose-to-ai
    trek event must defer to the user, not silently launch the Skill."""
    assert _probe_gate("trek-progress-check", "propose-to-ai", False) is False
    assert _probe_gate("trek-task-review", "notify-user-only", False) is False


def test_gate_blocks_all_trek_channels_when_opt_out_env_set():
    """The opt-out env var must disable trek auto-launch the same way it
    disables operation auto-launch, so rollback paths are uniform."""
    assert _probe_gate("trek-progress-check", "auto-execute", True) is False
    assert _probe_gate("trek-trigger", "auto-execute", True) is False
    assert _probe_gate("trek-task-review", "auto-execute", True) is False


def test_trek_progress_check_content_carries_launch_directive():
    out = _probe_content({
        "event_id": "evt-trek-1",
        "channel": "trek-progress-check",
        "payload": {
            "trek_id": "tk-abc",
            "trigger_name": "trek_progress_tk-abc",
        },
    })
    assert "## TREK ACTION" in out
    assert "Run this autonomously without asking the user first." in out
    assert "/beacon-trek-execute tk-abc" in out
    assert "no confirmation prompt" in out
    assert "trek_id: tk-abc" in out
    assert "trigger_name: trek_progress_tk-abc" in out


def test_trek_trigger_content_uses_same_executor_skill():
    """trek-trigger and trek-progress-check both route to /beacon-trek-execute
    so executors invoked by either path enter the same flow."""
    out = _probe_content({
        "event_id": "evt-trek-2",
        "channel": "trek-trigger",
        "payload": {"trek_id": "tk-xyz"},
    })
    assert "## TREK ACTION" in out
    assert "/beacon-trek-execute tk-xyz" in out


def test_trek_task_review_content_carries_picker_directive():
    """trek-task-review routes to /beacon-trek-review with the trek_id +
    task_id positional args, matching the Skill's invocation contract."""
    out = _probe_content({
        "event_id": "evt-trek-3",
        "channel": "trek-task-review",
        "payload": {
            "trek_id": "tk-abc",
            "task_id": "e-2048",
            "state": "waiting-review",
            "note": "implementation chunk done, leader review needed",
        },
    })
    assert "## TREK ACTION — trek task review required" in out
    assert "/beacon-trek-review tk-abc e-2048" in out
    assert "approve / re-work / forward-to-user" in out
    assert "task_id: e-2048" in out
    assert "state: waiting-review" in out
    # Executor note must be preserved (truncated only if long).
    assert "implementation chunk done" in out


def test_trek_task_review_content_truncates_long_executor_note():
    long_note = "x" * 300
    out = _probe_content({
        "event_id": "evt-trek-4",
        "channel": "trek-task-review",
        "payload": {
            "trek_id": "tk-1",
            "task_id": "e-1",
            "state": "done",
            "note": long_note,
        },
    })
    # 200-char cap with ellipsis matches the bus.mjs cap on free-form text.
    assert "x" * 200 + "…" in out
    assert "x" * 201 not in out


def test_trek_content_defaults_to_question_mark_when_payload_empty():
    out = _probe_content({
        "event_id": "evt-trek-5",
        "channel": "trek-progress-check",
        "payload": {},
    })
    assert "trek_id: ?" in out
    assert "/beacon-trek-execute ?" in out


def test_unknown_channel_falls_back_to_operation_format():
    """For backward compatibility, an event without a recognised channel
    (e.g. a legacy operation-trigger event with no channel field) still
    produces the operation imperative — this avoids breaking existing
    operation-trigger flows during the rollout."""
    out = _probe_content({
        "event_id": "evt-legacy",
        "payload": {"op_id": "op-1"},
    })
    assert "## AUTONOMOUS ACTION" in out
    assert "/beacon-operation-execute op-1" in out
