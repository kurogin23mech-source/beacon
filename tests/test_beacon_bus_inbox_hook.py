"""Tests for the bus-inbox hook's AUTONOMOUS ACTION dispatch (ms-60 / e-1340
Phase B = e-1384).

Where ``test_bus_inbox_hook.py`` covers the generic propose-to-ai /
notify-user-only / downgrade contract, this module locks in the
**structured dispatch** the inbox hook performs when an opted-in
operation-trigger event arrives:

  * opt-in path: ``delivery=auto-execute`` + ``channel=operation-trigger``
    + channel IS in ``bus_auto_execute_channels`` → an extra
    ``## AUTONOMOUS ACTION — operation autonomy active`` block is emitted
    ABOVE the generic event list, carrying ``op_id`` / ``spec_doc_id`` /
    ``trigger_name`` so the Skill picks them up without re-reading the raw
    event.

  * opt-out path: same event but channel NOT in the allowlist → the event
    is downgraded to ``propose-to-ai`` with the ``_downgraded_from`` marker
    (existing safety net), AND no AUTONOMOUS ACTION block is emitted (=
    confirms opt-out fully restores the half-auto fallback).

The opt-in / opt-out symmetry is the structural guarantee — if either path
regresses, the autonomous loop becomes either invisible to the AI
(false-negative) or unstoppable (false-positive).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "bin" / "beacon-bus-inbox-hook.py"


@pytest.fixture(scope="module")
def hook_module():
    """Import the hook script as a module (same pattern as
    test_bus_inbox_hook.py)."""
    spec = importlib.util.spec_from_file_location(
        "bus_inbox_hook_e1384", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook_e1384"] = module
    spec.loader.exec_module(module)
    return module


def _write_project(root: Path, *, allowlist: list[str]) -> None:
    """Materialize the receiver-side project state with a specific
    ``bus_auto_execute_channels`` value. An explicit allowlist (even empty)
    is required — the hook's read path fail-closes on missing key."""
    (root / ".beacon").mkdir(parents=True, exist_ok=True)
    (root / ".beacon" / "project.json").write_text(
        json.dumps({"bus_auto_execute_channels": allowlist}))
    (root / ".beacon" / "session.json").write_text(
        json.dumps({"session_id": "sess-X"}))
    (root / ".beacon" / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))


@pytest.fixture
def fake_project_opted_in(tmp_path):
    """``operation-trigger`` IS in the allowlist — autonomous block expected."""
    root = tmp_path / "proj-in"
    _write_project(root, allowlist=["operation-trigger"])
    return SimpleNamespace(root=root)


@pytest.fixture
def fake_project_opted_out(tmp_path):
    """``operation-trigger`` is NOT in the allowlist — downgrade expected,
    no autonomous block."""
    root = tmp_path / "proj-out"
    _write_project(root, allowlist=[])
    return SimpleNamespace(root=root)


def _operation_trigger_event(eid: str = "ev-op-1", *, op_id="op-42",
                              spec_doc_id="doc-spec-99",
                              trigger_name="operation_check_op-42") -> dict:
    """The on-wire shape posted by ``_push_operation_trigger_to_bus`` in
    lib/commands.py (Phase A, commit 9dba838). Keeping the shape in sync is
    the regression net — if commands.py drops a field this test catches it."""
    return {
        "event_id": eid,
        "channel": "operation-trigger",
        "delivery": "auto-execute",
        "sender_session_id": "",
        "payload": {
            "op_id": op_id,
            "spec_doc_id": spec_doc_id,
            "trigger_name": trigger_name,
            "log_source": "manual",
            "message": "scheduled operation check",
            "created_at": "2026-06-09T01:00:00.000000Z",
        },
        "created_at": "2026-06-09T01:00:00.000000Z",
    }


def _run_main(hook_module, project_root: Path, events: list[dict],
              monkeypatch, capsys, hook_event_name: str = "UserPromptSubmit"):
    """Drive main() with mocked network + supplied stdin."""
    monkeypatch.setattr(hook_module, "_load_id_token", lambda: "fake-token")
    monkeypatch.setattr(hook_module, "_list_unread",
                        lambda *a, **kw: list(events))
    monkeypatch.setattr(hook_module, "_ack_cursor", lambda *a, **kw: None)
    payload = json.dumps({
        "cwd": str(project_root),
        "session_id": "sess-X",
        "hook_event_name": hook_event_name,
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    hook_module.main()
    return capsys.readouterr()


# ---------------------------------------------------------------------------
# Opt-in path: AUTONOMOUS ACTION block must appear
# ---------------------------------------------------------------------------

def test_opt_in_emits_autonomous_action_block(hook_module,
                                                fake_project_opted_in,
                                                monkeypatch, capsys):
    """When ``bus_auto_execute_channels`` includes ``operation-trigger`` and a
    matching event arrives, the hook emits the AUTONOMOUS ACTION block with
    op_id / spec_doc_id / trigger_name lifted into the structured surface.
    This is the e-1384 acceptance criterion #1 happy path."""
    captured = _run_main(
        hook_module, fake_project_opted_in.root,
        [_operation_trigger_event()],
        monkeypatch, capsys,
    )
    assert captured.out.strip(), \
        "opt-in path must emit additionalContext for operation-trigger events"
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]

    # The distinct header — separate from the generic propose-to-ai list.
    assert "AUTONOMOUS ACTION" in ctx
    assert "operation autonomy active" in ctx

    # Structured fields hoisted from payload so the Skill doesn't re-parse.
    assert "op-42" in ctx
    assert "doc-spec-99" in ctx
    assert "operation_check_op-42" in ctx

    # The explicit "run without asking" sentence — guards against a cautious
    # AI defaulting to "ask the user first" on autonomous channels.
    assert "without asking the user first" in ctx

    # The Skill launch hint includes the op_id so it's a copy-paste command.
    assert "/beacon-operation-execute op-42" in ctx


def test_opt_in_autonomous_block_appears_above_event_list(hook_module,
                                                            fake_project_opted_in,
                                                            monkeypatch,
                                                            capsys):
    """Layout invariant: the AUTONOMOUS ACTION block must come BEFORE the
    generic 'AI コンテキスト inject 対象' event list so the AI sees the
    instruction before the noise. If a future refactor reorders the parts
    list, the autonomous instruction could end up buried below noisy event
    payloads — this test pins the order."""
    captured = _run_main(
        hook_module, fake_project_opted_in.root,
        [_operation_trigger_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    autonomous_idx = ctx.index("AUTONOMOUS ACTION")
    event_list_idx = ctx.index("AI コンテキスト inject 対象")
    assert autonomous_idx < event_list_idx, (
        "AUTONOMOUS ACTION block must precede the generic event list — "
        "instruction before noise"
    )


# ---------------------------------------------------------------------------
# Opt-out path: downgrade marker present, autonomous block absent
# ---------------------------------------------------------------------------

def test_opt_out_downgrades_and_omits_autonomous_block(hook_module,
                                                        fake_project_opted_out,
                                                        monkeypatch, capsys):
    """When ``operation-trigger`` is NOT in the allowlist, the same event must:
      (a) be downgraded to propose-to-ai (existing _downgraded_from marker)
      (b) NOT trigger an AUTONOMOUS ACTION block (= opt-out fully restores
          the half-auto fallback path; the AI sees a generic propose-to-ai
          event and the user reviews manually)

    Together these confirm the opt-in / opt-out symmetry is structural —
    the allowlist is the single switch that toggles autonomous behavior."""
    captured = _run_main(
        hook_module, fake_project_opted_out.root,
        [_operation_trigger_event()],
        monkeypatch, capsys,
    )
    assert captured.out.strip(), (
        "downgraded event still injects as propose-to-ai (just not as "
        "autonomous)"
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]

    # (a) downgrade marker surfaced by _format_event
    assert "auto-execute downgraded from" in ctx
    assert "auto-execute" in ctx  # the original delivery name is named
    # The summary line that calls out the downgrade count must also fire.
    assert "安全側降格" in ctx

    # (b) no autonomous block — the structural opt-out guarantee
    assert "AUTONOMOUS ACTION" not in ctx, (
        "AUTONOMOUS ACTION block leaked despite channel not being in "
        "bus_auto_execute_channels — opt-out is broken, autonomous "
        "execution would fire without project consent"
    )


def test_opt_out_with_unrelated_allowlist_still_omits_block(hook_module,
                                                              tmp_path,
                                                              monkeypatch,
                                                              capsys):
    """A non-empty allowlist that doesn't include ``operation-trigger`` must
    behave the same as an empty allowlist for operation-trigger events. This
    catches a "any allowlist entry enables everything" regression class."""
    root = tmp_path / "proj-other"
    _write_project(root, allowlist=["session-dm", "release"])
    captured = _run_main(
        hook_module, root, [_operation_trigger_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "AUTONOMOUS ACTION" not in ctx
    assert "auto-execute downgraded from" in ctx


# ---------------------------------------------------------------------------
# _format_autonomous_action_block (unit)
# ---------------------------------------------------------------------------

def test_format_autonomous_action_block_empty_input_returns_empty(hook_module):
    """No autonomous events → no block. _render_context relies on the empty
    return to skip appending stale whitespace."""
    assert hook_module._format_autonomous_action_block([]) == ""


def test_format_autonomous_action_block_multiple_events(hook_module):
    """Each autonomous event gets its own launch line. The hook must surface
    every op_id (not just the first) so a burst of triggers doesn't collapse
    into a single launchable command."""
    block = hook_module._format_autonomous_action_block([
        _operation_trigger_event("ev-a", op_id="op-1",
                                  spec_doc_id="doc-a",
                                  trigger_name="t-1"),
        _operation_trigger_event("ev-b", op_id="op-2",
                                  spec_doc_id="doc-b",
                                  trigger_name="t-2"),
    ])
    assert "op-1" in block
    assert "op-2" in block
    assert "/beacon-operation-execute op-1" in block
    assert "/beacon-operation-execute op-2" in block
