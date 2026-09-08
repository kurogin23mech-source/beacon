"""ms-159 / e-6244 — session execution-state declaration via lifecycle hooks.

Pins the two ends the slice owns on the client side (AC2):

  1. the pure event→state map (lib/session_state_hook) matches SPEC 方針2 exactly;
  2. the hook entry point (bin/beacon-state-hook.py) turns a Claude Code hook
     stdin payload into a .beacon/session-state.json marker the bridge can read,
     and is fail-safe (unrecognized event ⇒ no clobber; no .beacon ⇒ no crash).

The bridge→server leg (marker piggybacked onto the heartbeat, server persists
declared_state/declared_at) is covered by the JS marker/heartbeat tests +
test_session_upsert_declared_state below.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_liveness  # noqa: E402
import session_state_hook  # noqa: E402

HOOK = os.path.join(os.path.dirname(__file__), "..", "bin", "beacon-state-hook.py")


# ===========================================================================
# 1. Pure event→state map (SPEC 方針2 table).
# ===========================================================================

class TestEventToState:
    @pytest.mark.parametrize("event,expected", [
        ("UserPromptSubmit", bus_liveness.STATE_RUNNING),
        ("PreToolUse", bus_liveness.STATE_RUNNING),
        ("PostToolUse", bus_liveness.STATE_RUNNING),
        ("Notification", bus_liveness.STATE_AWAITING_HUMAN),
        ("Stop", bus_liveness.STATE_IDLE),
        ("SessionEnd", bus_liveness.STATE_TERMINATED),
    ])
    def test_mapped_events(self, event, expected):
        assert session_state_hook.event_to_declared_state(event) == expected

    @pytest.mark.parametrize("event", [None, "", "SomethingElse", "PostToolUseX"])
    def test_unrecognized_event_declares_nothing(self, event):
        assert session_state_hook.event_to_declared_state(event) is None

    def test_declared_states_are_all_declarable(self):
        # Nothing this map emits may be the server-only ``unknown`` (方針1).
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                      "Notification", "Stop", "SessionEnd"):
            state = session_state_hook.event_to_declared_state(event)
            assert state in bus_liveness.DECLARABLE_STATES
            assert state != bus_liveness.STATE_UNKNOWN

    def test_build_marker_shape(self):
        m = session_state_hook.build_state_marker("Notification", "2026-09-07T00:00:00.000Z")
        assert m == {
            "declared_state": bus_liveness.STATE_AWAITING_HUMAN,
            "declared_at": "2026-09-07T00:00:00.000Z",
            "state_since": "2026-09-07T00:00:00.000Z",  # first write ⇒ since = now
            "source_event": "Notification",
        }

    def test_build_marker_none_for_unrecognized(self):
        assert session_state_hook.build_state_marker("nope", "2026-09-07T00:00:00.000Z") is None

    def test_state_since_preserved_on_same_state_redeclaration(self):
        # e-6245: re-declaring the SAME state advances declared_at but keeps
        # state_since (so "how long waiting" doesn't reset on every hook fire).
        prev = {"declared_state": bus_liveness.STATE_RUNNING,
                "declared_at": "2026-09-07T00:00:00.000Z",
                "state_since": "2026-09-07T00:00:00.000Z"}
        m = session_state_hook.build_state_marker("PostToolUse", "2026-09-07T00:05:00.000Z", prev_marker=prev)
        assert m["declared_state"] == bus_liveness.STATE_RUNNING
        assert m["declared_at"] == "2026-09-07T00:05:00.000Z"      # advanced
        assert m["state_since"] == "2026-09-07T00:00:00.000Z"      # preserved

    def test_state_since_resets_on_transition(self):
        # A different state ⇒ new entry time.
        prev = {"declared_state": bus_liveness.STATE_RUNNING,
                "declared_at": "2026-09-07T00:00:00.000Z",
                "state_since": "2026-09-07T00:00:00.000Z"}
        m = session_state_hook.build_state_marker("Notification", "2026-09-07T00:05:00.000Z", prev_marker=prev)
        assert m["declared_state"] == bus_liveness.STATE_AWAITING_HUMAN
        assert m["state_since"] == "2026-09-07T00:05:00.000Z"      # reset on transition

    def test_state_since_from_e6244_marker_without_field(self):
        # Back-compat: a prior marker written by e-6244 (no state_since) that
        # matches state ⇒ state_since becomes now (no prior entry time to keep).
        prev = {"declared_state": bus_liveness.STATE_IDLE,
                "declared_at": "2026-09-07T00:00:00.000Z"}
        m = session_state_hook.build_state_marker("Stop", "2026-09-07T00:05:00.000Z", prev_marker=prev)
        assert m["state_since"] == "2026-09-07T00:05:00.000Z"


# ===========================================================================
# 2. The hook entry point (bin/beacon-state-hook.py) — stdin → marker file.
# ===========================================================================

def _run_hook(payload: dict):
    """Invoke the hook with a JSON stdin payload; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=15,
    )


def _marker_path(root):
    return os.path.join(str(root), ".beacon", "session-state.json")


class TestHookWritesMarker:
    def test_stop_writes_idle_marker(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        proc = _run_hook({"hook_event_name": "Stop", "cwd": str(tmp_path)})
        assert proc.returncode == 0, proc.stderr
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["declared_state"] == bus_liveness.STATE_IDLE
        assert marker["source_event"] == "Stop"
        assert marker["declared_at"]  # timestamp stamped

    def test_notification_writes_awaiting_human(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        proc = _run_hook({"hook_event_name": "Notification", "cwd": str(tmp_path)})
        assert proc.returncode == 0, proc.stderr
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["declared_state"] == bus_liveness.STATE_AWAITING_HUMAN

    def test_marker_written_to_beacon_dir_found_by_walking_up(self, tmp_path):
        # cwd is a nested subdir; the hook must walk up to the .beacon root.
        (tmp_path / ".beacon").mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        proc = _run_hook({"hook_event_name": "PreToolUse", "cwd": str(nested)})
        assert proc.returncode == 0, proc.stderr
        # Marker lands in the ROOT .beacon, not a nested one.
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["declared_state"] == bus_liveness.STATE_RUNNING

    def test_unrecognized_event_writes_nothing(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        proc = _run_hook({"hook_event_name": "SubagentStop", "cwd": str(tmp_path)})
        assert proc.returncode == 0, proc.stderr
        assert not os.path.exists(_marker_path(tmp_path))  # no clobber

    def test_unrecognized_event_does_not_clobber_existing_marker(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        # Seed an existing declaration.
        _run_hook({"hook_event_name": "Notification", "cwd": str(tmp_path)})
        # A no-op event must leave it intact.
        _run_hook({"hook_event_name": "PreCompact", "cwd": str(tmp_path)})
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            marker = json.load(f)
        assert marker["declared_state"] == bus_liveness.STATE_AWAITING_HUMAN

    def test_state_since_preserved_across_two_same_state_fires(self, tmp_path):
        # e-6245 end-to-end through the bin hook: two PostToolUse fires keep the
        # first entry time in state_since while declared_at advances.
        (tmp_path / ".beacon").mkdir()
        _run_hook({"hook_event_name": "PreToolUse", "cwd": str(tmp_path)})
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            first = json.load(f)
        _run_hook({"hook_event_name": "PostToolUse", "cwd": str(tmp_path)})
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            second = json.load(f)
        assert second["declared_state"] == bus_liveness.STATE_RUNNING
        assert second["state_since"] == first["state_since"]  # preserved
        assert second["declared_at"] >= first["declared_at"]  # advanced (or equal)

    def test_state_since_resets_across_transition_fires(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        _run_hook({"hook_event_name": "PreToolUse", "cwd": str(tmp_path)})
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            running = json.load(f)
        _run_hook({"hook_event_name": "Notification", "cwd": str(tmp_path)})
        with open(_marker_path(tmp_path), encoding="utf-8") as f:
            waiting = json.load(f)
        assert waiting["declared_state"] == bus_liveness.STATE_AWAITING_HUMAN
        assert waiting["state_since"] != running["state_since"]  # reset on transition

    def test_no_beacon_dir_is_noop_not_crash(self, tmp_path):
        # No .beacon anywhere up the tree ⇒ fail-safe no-op, exit 0.
        proc = _run_hook({"hook_event_name": "Stop", "cwd": str(tmp_path)})
        assert proc.returncode == 0, proc.stderr

    def test_garbage_stdin_is_noop_not_crash(self):
        proc = subprocess.run(
            [sys.executable, HOOK], input="not json at all",
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0, proc.stderr


# ===========================================================================
# 3. Server model — declared_state/declared_at accepted + persisted.
# ===========================================================================

class TestSessionUpsertDeclaredState:
    def test_model_accepts_declared_fields(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
        os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
        import routers_projects  # noqa: E402
        body = routers_projects.SessionUpsert(
            last_active="2026-09-07T00:00:00Z",
            declared_state="awaiting_human",
            declared_at="2026-09-07T00:00:01Z",
        )
        # The upsert handler persists exactly the non-None fields.
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        assert payload["declared_state"] == "awaiting_human"
        assert payload["declared_at"] == "2026-09-07T00:00:01Z"

    def test_heartbeat_without_declaration_omits_fields(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
        import routers_projects  # noqa: E402
        body = routers_projects.SessionUpsert(last_active="2026-09-07T00:00:00Z")
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        # Back-compat: a heartbeat with no marker grows no declared_* keys.
        assert "declared_state" not in payload
        assert "declared_at" not in payload

    def test_all_declarable_states_accepted(self):
        """The Literal (ms-159 review #735) must accept every one of the 5
        canonical DECLARABLE_STATES — no false rejection of a valid declaration."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
        import routers_projects  # noqa: E402
        for state in sorted(bus_liveness.DECLARABLE_STATES):
            body = routers_projects.SessionUpsert(
                last_active="2026-09-07T00:00:00Z", declared_state=state,
                declared_at="2026-09-07T00:00:01Z")
            assert body.declared_state == state

    def test_invalid_declared_state_rejected(self):
        """ms-159 review #735: an out-of-enum declaration (paused/done/…) is
        rejected at the request boundary instead of silently falling back in
        derive_state (a silent no-op is a Beacon 禁忌). The Literal is the guard."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
        import pydantic
        import routers_projects  # noqa: E402
        for bad in ("paused", "done", "unknown", "RUNNING", ""):
            with pytest.raises(pydantic.ValidationError):
                routers_projects.SessionUpsert(
                    last_active="2026-09-07T00:00:00Z", declared_state=bad,
                    declared_at="2026-09-07T00:00:01Z")

    def test_literal_matches_declarable_states_both_ways(self):
        """ms-159 review #735 (maintainability): the server-side Literal and
        lib DECLARABLE_STATES are two copies of one set. `test_all_declarable_
        states_accepted` only pins one direction (canonical ⊆ Literal). Pin the
        reverse too — extract the Literal's members from the model annotation and
        assert set-equality — so a value added to the Literal but NOT to the
        canonical set (or vice versa) fails CI instead of drifting silently."""
        import typing
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
        import routers_projects  # noqa: E402
        ann = routers_projects.SessionUpsert.model_fields["declared_state"].annotation
        # annotation is Optional[Literal[...]] == Union[Literal[...], None];
        # unwrap the Union, then the Literal, to recover the declared members.
        literal_members = set()
        for arg in typing.get_args(ann):
            literal_members.update(typing.get_args(arg))
        assert literal_members == set(bus_liveness.DECLARABLE_STATES), (
            "SessionUpsert.declared_state Literal has drifted from "
            "bus_liveness.DECLARABLE_STATES — keep the two in sync "
            f"(Literal={sorted(literal_members)}, "
            f"canonical={sorted(bus_liveness.DECLARABLE_STATES)})"
        )
