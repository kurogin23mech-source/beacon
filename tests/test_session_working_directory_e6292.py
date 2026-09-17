"""ms-159 / e-6292 — server projects working_target / activity / user_id onto
directory rows.

`_stamp_session_liveness` is the single choke point every `bus directory` row
passes through (same as the state stamp, e-6245). These tests pin that it now
also stamps:

  * working_target — declared (intent.working_target) wins; else derived from
    git.branch / cwd / focus (lib/working_target, e-6290);
  * activity — declared intent.text wins; else git.head_subject;
  * user_id — actor.user_id, else actor.email (the scope-self identity).
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import working_target as wt  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class TestStampWorkingTarget:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        monkeypatch.setattr(app_module.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(app_module.db, "list_bus_events", lambda pid, **k: [])
        return app_module

    def _row(self, now, **extra):
        row = {"session_id": "sv-x",
               "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
               "last_active": _iso(now - datetime.timedelta(seconds=3)),
               "poll_interval_ms": 5000}
        row.update(extra)
        return row

    # --- working_target -----------------------------------------------------

    def test_declared_working_target_wins(self, _app):
        now = _now()
        row = self._row(now, intent={
            "working_target": {"target": {"kind": "task", "id": "e-6292",
                                          "title": "server stamp"}}},
            git={"branch": "ms-999-fork-z"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["working_target"]["source"] == wt.SOURCE_DECLARED
        assert row["working_target"]["target"]["id"] == "e-6292"

    def test_derived_from_branch_when_no_declaration(self, _app):
        now = _now()
        row = self._row(now, git={"branch": "ms-159-fork-361e58"},
                        cwd="/w/ms-159")
        _app._stamp_session_liveness(row, "proj", now)
        assert row["working_target"]["source"] == wt.SOURCE_BRANCH
        assert row["working_target"]["target"]["id"] == "ms-159"

    def test_derived_from_focus_when_no_branch_ms(self, _app):
        now = _now()
        row = self._row(now, git={"branch": "main"}, cwd="/repo",
                        focus={"milestone": {"id": "ms-133", "title": "職種"}})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["working_target"]["source"] == wt.SOURCE_FOCUS
        assert row["working_target"]["target"]["id"] == "ms-133"

    def test_root_carries_project_id(self, _app):
        now = _now()
        row = self._row(now, git={"branch": "ms-5"}, project_name="Beacon")
        _app._stamp_session_liveness(row, "proj-abc", now)
        assert row["working_target"]["root"]["id"] == "proj-abc"
        assert row["working_target"]["root"]["label"] == "Beacon"

    # --- activity -----------------------------------------------------------

    def test_activity_declared_intent_text_wins(self, _app):
        now = _now()
        row = self._row(now, intent={"text": "D スライス実装中"},
                        git={"head_subject": "feat: whatever"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == "D スライス実装中"

    def test_activity_falls_back_to_head_subject(self, _app):
        now = _now()
        row = self._row(now, git={"head_subject": "feat(ms-159): derive fallback"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == "feat(ms-159): derive fallback"

    def test_activity_empty_when_nothing(self, _app):
        now = _now()
        row = self._row(now)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == ""

    # --- activity: state-aware wait detail (ms-159 / e-6533) ----------------
    # The projection is now state-aware: an awaiting_human / blocked row surfaces
    # WHAT it is waiting on (state_detail) as its activity, instead of the stale
    # git.head_subject that would misrepresent a stalled session as still working.

    def test_activity_state_aware_awaiting_human_shows_wait_detail(self, _app):
        now = _now()
        row = self._row(
            now, declared_state="awaiting_human",
            declared_at=_iso(now - datetime.timedelta(seconds=5)),
            state_detail="Claude needs your permission to use Bash",
            git={"head_subject": "feat(ms-159): last commit"})
        _app._stamp_session_liveness(row, "proj", now)
        # wait state → the wait detail wins over head_subject
        assert row["activity"] == "Claude needs your permission to use Bash"

    def test_activity_state_aware_blocked_shows_wait_detail(self, _app):
        now = _now()
        row = self._row(
            now, declared_state="blocked",
            declared_at=_iso(now - datetime.timedelta(seconds=5)),
            state_detail="waiting on CI to go green",
            git={"head_subject": "feat: irrelevant"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == "waiting on CI to go green"

    def test_activity_state_aware_awaiting_human_empty_when_no_detail(self, _app):
        # No fabrication (ms-173 方針2): a wait state with no known detail shows
        # EMPTY, NOT the git head subject.
        now = _now()
        row = self._row(
            now, declared_state="awaiting_human",
            declared_at=_iso(now - datetime.timedelta(seconds=5)),
            git={"head_subject": "feat(ms-159): last commit"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == ""

    def test_activity_declared_text_still_wins_over_wait_detail(self, _app):
        # Precedence unchanged: the session's own self-report beats the derived
        # wait detail.
        now = _now()
        row = self._row(
            now, declared_state="awaiting_human",
            declared_at=_iso(now - datetime.timedelta(seconds=5)),
            state_detail="permission prompt",
            intent={"text": "手動で調査中"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity"] == "手動で調査中"

    # --- activity_kind: label the activity's meaning (ms-159 e-6533, #755 AX-F1/F2)
    # so a consumer interprets the string — and its emptiness — without state.

    def test_activity_kind_wait_for_awaiting_human(self, _app):
        now = _now()
        row = self._row(
            now, declared_state="awaiting_human",
            declared_at=_iso(now - datetime.timedelta(seconds=5)),
            git={"head_subject": "feat: x"})
        _app._stamp_session_liveness(row, "proj", now)
        # empty activity + kind=wait ⇒ "waiting, reason unknown"
        assert row["activity"] == ""
        assert row["activity_kind"] == "wait"

    def test_activity_kind_work_for_running(self, _app):
        now = _now()
        row = self._row(now, git={"head_subject": "feat: y"},
                        declared_state="running",
                        declared_at=_iso(now - datetime.timedelta(seconds=5)))
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity_kind"] == "work"

    def test_activity_kind_work_for_undeclared_unknown(self, _app):
        # A live-but-undeclared session derives state=unknown; its activity is the
        # head-subject work proxy, so the kind is work (label matches string).
        now = _now()
        row = self._row(now, git={"head_subject": "feat: z"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["activity_kind"] == "work"

    # --- user_id ------------------------------------------------------------

    def test_user_id_prefers_actor_user_id(self, _app):
        now = _now()
        row = self._row(now, actor={"user_id": "uid-1", "email": "a@b.com"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["user_id"] == "uid-1"

    def test_user_id_falls_back_to_email(self, _app):
        now = _now()
        row = self._row(now, actor={"email": "a@b.com"})
        _app._stamp_session_liveness(row, "proj", now)
        assert row["user_id"] == "a@b.com"

    def test_user_id_empty_when_no_actor(self, _app):
        now = _now()
        row = self._row(now)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["user_id"] == ""

    def test_row_shape_stable_with_garbage_intent(self, _app):
        # A non-dict intent / actor must not crash the stamp (fail-safe).
        now = _now()
        row = self._row(now, intent="nope", actor="nope", git="nope")
        _app._stamp_session_liveness(row, "proj", now)
        assert "working_target" in row and "activity" in row and "user_id" in row
        assert row["activity"] == ""
        assert row["user_id"] == ""
