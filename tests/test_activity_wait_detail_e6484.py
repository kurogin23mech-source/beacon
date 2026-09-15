"""ms-159 / e-6484 — pin the state-aware activity contract (lib/working_target
``derive_activity`` / ``activity_for_row``).

e-6399 made a session's activity fall back to its last commit subject. e-6484
refines that for WAITING sessions: an ``awaiting_human`` / ``blocked`` session's
activity must be WHAT it is waiting for (its wait detail), and EMPTY when that is
unknown — never the last commit subject, which would misrepresent a stalled
session as actively working (判定できない待機理由は素直に空、ms-173 方針2 = no
fabrication). Every other state keeps the e-6399 head-subject fallback. Pinned as
pure functions so the projection is verifiable without a live bus (the server
state stamp that populates row["state"] is a separate, not-yet-deployed layer).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_liveness as bl  # noqa: E402
import working_target as wt  # noqa: E402


class TestDeriveActivityStateAware:
    def test_declared_wins_even_when_awaiting(self):
        # An explicit self-report always wins, regardless of state.
        assert wt.derive_activity(
            "レビュー待ち中", head_subject="fix: x",
            state=bl.STATE_AWAITING_HUMAN, state_detail="別の待機") == "レビュー待ち中"

    def test_awaiting_human_uses_wait_detail(self):
        assert wt.derive_activity(
            None, head_subject="feat: last commit",
            state=bl.STATE_AWAITING_HUMAN,
            state_detail="cmd_bus.py の編集許可待ち") == "cmd_bus.py の編集許可待ち"

    def test_awaiting_human_without_detail_is_empty_not_head_subject(self):
        # The crux: a waiting session with no known detail shows NOTHING, not its
        # last commit subject (no fabrication).
        assert wt.derive_activity(
            None, head_subject="Merge pull request #747",
            state=bl.STATE_AWAITING_HUMAN, state_detail="") == ""

    def test_blocked_uses_wait_detail(self):
        assert wt.derive_activity(
            None, head_subject="fix: x", state=bl.STATE_BLOCKED,
            state_detail="外部 API レート制限") == "外部 API レート制限"

    def test_blocked_without_detail_is_empty(self):
        assert wt.derive_activity(
            None, head_subject="fix: x", state=bl.STATE_BLOCKED) == ""

    def test_running_still_uses_head_subject(self):
        assert wt.derive_activity(
            None, head_subject="feat(ms-159): produce D data",
            state=bl.STATE_RUNNING) == "feat(ms-159): produce D data"

    def test_idle_uses_head_subject(self):
        assert wt.derive_activity(
            None, head_subject="last thing", state=bl.STATE_IDLE) == "last thing"

    def test_terminated_uses_head_subject(self):
        assert wt.derive_activity(
            None, head_subject="last thing",
            state=bl.STATE_TERMINATED) == "last thing"

    def test_empty_state_is_backcompat_head_subject(self):
        # No state known (server stamp not deployed / old caller) → e-6399 behaviour.
        assert wt.derive_activity(None, head_subject="hs") == "hs"
        assert wt.derive_activity(None, head_subject="hs", state="") == "hs"

    def test_unknown_state_falls_to_head_subject(self):
        # `unknown` is not a wait state — it gets the head-subject proxy, not empty.
        assert wt.derive_activity(
            None, head_subject="hs", state=bl.STATE_UNKNOWN) == "hs"

    def test_wait_detail_ignored_for_non_wait_states(self):
        # A stray wait_detail on a running row must not leak into the activity.
        assert wt.derive_activity(
            None, head_subject="hs", state=bl.STATE_RUNNING,
            state_detail="should be ignored") == "hs"


class TestActivityForRowStateAware:
    def _row(self, **over):
        row = {"git": {"head_subject": "feat: last commit"}}
        row.update(over)
        return row

    def test_awaiting_row_shows_state_detail(self):
        row = self._row(state=bl.STATE_AWAITING_HUMAN,
                        state_detail="page.html の編集許可待ち")
        assert wt.activity_for_row(row) == "page.html の編集許可待ち"

    def test_awaiting_row_without_detail_is_empty(self):
        row = self._row(state=bl.STATE_AWAITING_HUMAN)
        assert wt.activity_for_row(row) == ""

    def test_running_row_shows_head_subject(self):
        row = self._row(state=bl.STATE_RUNNING)
        assert wt.activity_for_row(row) == "feat: last commit"

    def test_declared_activity_on_row_wins(self):
        row = self._row(state=bl.STATE_AWAITING_HUMAN, activity="明示 activity",
                        state_detail="無視される")
        assert wt.activity_for_row(row) == "明示 activity"

    def test_no_state_row_backcompat(self):
        # Rows from the (undeployed) server carry no `state` → head-subject.
        row = self._row()
        assert wt.activity_for_row(row) == "feat: last commit"


class TestEnrichRowStateAware:
    def test_awaiting_row_enriched_activity_empty(self):
        row = {"session_id": "s1", "state": bl.STATE_AWAITING_HUMAN,
               "git": {"head_subject": "Merge pull request #999"}}
        out = wt.enrich_row(row)
        # not the misleading merge-commit subject
        assert out["activity"] == ""

    def test_awaiting_row_enriched_activity_detail(self):
        row = {"session_id": "s1", "state": bl.STATE_AWAITING_HUMAN,
               "state_detail": "cmd_bus.py の編集許可待ち",
               "git": {"head_subject": "Merge pull request #999"}}
        out = wt.enrich_row(row)
        assert out["activity"] == "cmd_bus.py の編集許可待ち"
