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


# ---------------------------------------------------------------------------
# ms-97 / e-2711 — Level 3 imperative dispatch blocks for periodic trek DMs
# (trek-progress-check / trek-task-review / trek-leader-digest)
# ---------------------------------------------------------------------------


def _trek_progress_check_event(eid: str = "ev-pc-1",
                                *, trek_id: str = "tk-abc12345") -> dict:
    """On-wire shape posted by build_progress_check_payload + server fanout."""
    return {
        "event_id": eid,
        "channel": "trek-progress-check",
        "delivery": "auto-execute",
        "sender_session_id": "",
        "payload": {
            "trek_id": trek_id,
            "kind": "trek-progress-check",
            "body": "next, please",
            "target_entries": ["e-target-1"],
            "created_at": "2026-06-29T01:00:00.000000Z",
        },
        "created_at": "2026-06-29T01:00:00.000000Z",
    }


def _trek_task_review_event(eid: str = "ev-tr-1",
                             *, trek_id: str = "tk-abc12345",
                             task_id: str = "e-xyz") -> dict:
    """On-wire shape posted when executor flips a task to a terminal state."""
    return {
        "event_id": eid,
        "channel": "trek-task-review",
        "delivery": "auto-execute",
        "sender_session_id": "",
        "payload": {
            "trek_id": trek_id,
            "task_id": task_id,
            "kind": "trek-task-review",
            "body": "task awaiting leader judgment",
            "created_at": "2026-06-29T01:00:00.000000Z",
        },
        "created_at": "2026-06-29T01:00:00.000000Z",
    }


def _trek_leader_digest_event(eid: str = "ev-ld-1",
                               *, trek_id: str = "tk-abc12345",
                               leader_review_queue: list[dict] | None = None,
                               leader_review_queue_count: int | None = None
                               ) -> dict:
    """On-wire shape posted by build_leader_digest_payload (with e-2707 fields)."""
    if leader_review_queue is None:
        leader_review_queue = []
    if leader_review_queue_count is None:
        leader_review_queue_count = len(leader_review_queue)
    return {
        "event_id": eid,
        "channel": "trek-leader-digest",
        "delivery": "auto-execute",
        "sender_session_id": "",
        "payload": {
            "trek_id": trek_id,
            "kind": "trek-leader-digest",
            "summary": {
                "active": 1, "stuck": 0, "idle": 0,
                "needs_leader_judgment": 0,
                "leader_review_queue_count": leader_review_queue_count,
                "total_acks_across_sessions": 1,
            },
            "task_state_aggregate": {
                "counts": {
                    "leader_review": leader_review_queue_count,
                    "done": 0, "user_review": 0, "working": 1, "todo": 0,
                },
                "leader_review_queue": leader_review_queue,
                "overall_state": (
                    "leader_review" if leader_review_queue_count > 0
                    else "working"
                ),
            },
            "sessions": [],
            "body": "[Trek leader digest]",
            "created_at": "2026-06-29T01:00:00.000000Z",
        },
        "created_at": "2026-06-29T01:00:00.000000Z",
    }


# ----------- trek-progress-check Level 3 imperative -----------


def test_progress_check_opt_in_emits_level3_imperative(hook_module, tmp_path,
                                                       monkeypatch, capsys):
    """``trek-progress-check`` in allowlist → Level 3 imperative block fires
    with "You MUST immediately invoke /beacon-trek-execute <trek-id>".
    Regression pin: this closes the 2026-06-29 mechanical ack pathology
    where AI received the DM but never invoked the Skill."""
    root = tmp_path / "proj-pc"
    _write_project(root, allowlist=["trek-progress-check"])
    captured = _run_main(
        hook_module, root, [_trek_progress_check_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK PROGRESS CHECK" in ctx
    assert "Level 3 imperative" in ctx
    assert "You MUST immediately invoke" in ctx
    assert "/beacon-trek-execute tk-abc12345" in ctx


def test_progress_check_opt_out_omits_block(hook_module, tmp_path,
                                             monkeypatch, capsys):
    """Without the channel in allowlist, the event downgrades and no Level 3
    block fires (= same opt-in / opt-out symmetry as AUTONOMOUS ACTION)."""
    root = tmp_path / "proj-pc-out"
    _write_project(root, allowlist=[])
    captured = _run_main(
        hook_module, root, [_trek_progress_check_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK PROGRESS CHECK" not in ctx
    assert "auto-execute downgraded from" in ctx


# ----------- trek-task-review Level 3 imperative -----------


def test_task_review_opt_in_emits_level3_imperative(hook_module, tmp_path,
                                                     monkeypatch, capsys):
    """``trek-task-review`` in allowlist → Level 3 imperative block fires
    with "You MUST immediately invoke /beacon-trek-review <trek-id> <task-id>".
    Closes the leader mechanical ack pathology (= "read DM, ack, don't act")."""
    root = tmp_path / "proj-tr"
    _write_project(root, allowlist=["trek-task-review"])
    captured = _run_main(
        hook_module, root,
        [_trek_task_review_event(trek_id="tk-z", task_id="e-q")],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK TASK REVIEW" in ctx
    assert "Level 3 imperative" in ctx
    assert "You MUST immediately invoke" in ctx
    assert "/beacon-trek-review tk-z e-q" in ctx


def test_task_review_opt_out_omits_block(hook_module, tmp_path,
                                          monkeypatch, capsys):
    root = tmp_path / "proj-tr-out"
    _write_project(root, allowlist=[])
    captured = _run_main(
        hook_module, root, [_trek_task_review_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK TASK REVIEW" not in ctx


# ----------- trek-leader-digest Level 3 imperative (× e-2707 payload) -----------


def test_leader_digest_with_queue_emits_level3_imperative(
        hook_module, tmp_path, monkeypatch, capsys):
    """leader-digest event with non-empty leader_review_queue → forces
    /beacon-trek-review invoke with the count + task_id preview.
    Closes the 2026-06-29 LPS dogfood "leader read past digest for 10
    min" pathology (= e-2707 motivation)."""
    root = tmp_path / "proj-ld"
    _write_project(root, allowlist=["trek-leader-digest"])
    queue = [
        {"task_id": "e-blocked-1", "updated_by_session_id": "sv-exec-1",
         "updated_at": "2026-06-29T00:00:00Z", "note": "approve me"},
        {"task_id": "e-blocked-2", "updated_by_session_id": "sv-exec-2",
         "updated_at": "2026-06-29T00:10:00Z", "note": "ready"},
    ]
    captured = _run_main(
        hook_module, root,
        [_trek_leader_digest_event(trek_id="tk-ld",
                                     leader_review_queue=queue)],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK LEADER DIGEST" in ctx
    assert "Level 3 imperative" in ctx
    assert "leader_review queue: 2 件" in ctx
    assert "You MUST immediately invoke" in ctx
    assert "/beacon-trek-review tk-ld" in ctx
    # Preview ids surface so the leader can act on them inline.
    assert "e-blocked-1" in ctx
    assert "e-blocked-2" in ctx


def test_leader_digest_with_empty_queue_does_not_force_invoke(
        hook_module, tmp_path, monkeypatch, capsys):
    """Clean digest (= queue empty) shows the block as info-only — no
    "You MUST invoke" force, just "queue 空 → invoke 不要". This is the
    structural defence against forcing invoke when there is no action."""
    root = tmp_path / "proj-ld-clean"
    _write_project(root, allowlist=["trek-leader-digest"])
    captured = _run_main(
        hook_module, root,
        [_trek_leader_digest_event(leader_review_queue=[])],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK LEADER DIGEST" in ctx
    # block exists for transparency, but invoke is NOT forced
    assert "leader_review queue: 0 件" in ctx
    assert "queue 空" in ctx
    assert "You MUST immediately invoke" not in ctx


def test_leader_digest_opt_out_omits_block(hook_module, tmp_path,
                                            monkeypatch, capsys):
    root = tmp_path / "proj-ld-out"
    _write_project(root, allowlist=[])
    captured = _run_main(
        hook_module, root, [_trek_leader_digest_event()],
        monkeypatch, capsys,
    )
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TREK LEADER DIGEST" not in ctx


# ----------- Layout invariant: Level 3 blocks appear above event list -----------


def test_level3_blocks_appear_above_event_list(hook_module, tmp_path,
                                                 monkeypatch, capsys):
    """All three Level 3 blocks must precede the generic "AI コンテキスト
    inject 対象" list so the AI sees the imperatives before the noise.
    This pins the layout — a future refactor that reorders parts[] could
    bury the imperative below noisy event payloads, regressing e-2711."""
    root = tmp_path / "proj-layout"
    _write_project(root, allowlist=[
        "trek-progress-check", "trek-task-review", "trek-leader-digest"])
    events = [
        _trek_progress_check_event(),
        _trek_task_review_event(),
        _trek_leader_digest_event(leader_review_queue=[{
            "task_id": "e-q", "updated_by_session_id": "sv-x",
            "updated_at": "2026-06-29T00:00:00Z", "note": ""}]),
    ]
    captured = _run_main(hook_module, root, events, monkeypatch, capsys)
    payload = json.loads(captured.out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    event_list_idx = ctx.index("AI コンテキスト inject 対象")
    for header in ("TREK PROGRESS CHECK", "TREK TASK REVIEW",
                   "TREK LEADER DIGEST"):
        assert ctx.index(header) < event_list_idx, (
            f"{header} block must precede the generic event list — "
            f"imperative before noise"
        )


# ----------- Unit-level guards on each formatter -----------


def test_format_trek_progress_check_block_empty_input_returns_empty(hook_module):
    assert hook_module._format_trek_progress_check_block([]) == ""


def test_format_trek_task_review_block_empty_input_returns_empty(hook_module):
    assert hook_module._format_trek_task_review_block([]) == ""


def test_format_trek_leader_digest_block_empty_input_returns_empty(hook_module):
    assert hook_module._format_trek_leader_digest_block([]) == ""


def test_format_trek_progress_check_block_multiple_events_all_surface(hook_module):
    """Each progress-check event surfaces its own /beacon-trek-execute
    command — a burst must not collapse to one launchable line."""
    block = hook_module._format_trek_progress_check_block([
        _trek_progress_check_event("ev-a", trek_id="tk-1"),
        _trek_progress_check_event("ev-b", trek_id="tk-2"),
    ])
    assert "/beacon-trek-execute tk-1" in block
    assert "/beacon-trek-execute tk-2" in block


def test_format_trek_leader_digest_block_fallback_to_summary_count(hook_module):
    """If payload omits task_state_aggregate but summary has the count,
    the block still surfaces the queue count (defensive against older
    server builds that haven't shipped e-2707)."""
    ev = {
        "event_id": "ev-fb",
        "channel": "trek-leader-digest",
        "delivery": "auto-execute",
        "sender_session_id": "",
        "payload": {
            "trek_id": "tk-fb",
            "kind": "trek-leader-digest",
            "summary": {"leader_review_queue_count": 3},
            # no task_state_aggregate (= legacy payload)
        },
        "created_at": "2026-06-29T01:00:00.000000Z",
    }
    block = hook_module._format_trek_leader_digest_block([ev])
    assert "leader_review queue: 3 件" in block
    # No task preview because queue list itself is absent, but invoke
    # is still forced — count is the structural signal.
    assert "You MUST immediately invoke" in block
