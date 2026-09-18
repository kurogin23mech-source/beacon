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

    def test_wait_row_activity_without_kind_is_proxy_not_self_report(self):
        # ms-173 e-6562 で旧契約を supersede: kind を stamp しない legacy server
        # (e-6292..e-6533) は git head-subject の代理値を `activity` に stamp する
        # ため、これを自己申告扱いすると待機中の行が直近コミットで作業中に見える
        # (2026-09-18 実測)。kind 無し + 待機 state では wait detail が勝つ。
        row = self._row(state=bl.STATE_AWAITING_HUMAN, activity="Merge pull request #757",
                        state_detail="選択肢への応答待ち")
        assert wt.activity_for_row(row) == "選択肢への応答待ち"

    def test_stamped_kind_makes_row_activity_authoritative(self):
        # e-6533+ server は (activity, kind) を対で stamp する — その activity は
        # verbatim に信じる (自己申告も wait detail もサーバが選別済み)。
        row = self._row(state=bl.STATE_AWAITING_HUMAN, activity="明示 activity",
                        activity_kind=wt.ACTIVITY_KIND_WORK,
                        state_detail="使われない")
        assert wt.activity_for_row(row) == "明示 activity"
        assert wt.activity_kind_for_row(row) == wt.ACTIVITY_KIND_WORK

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


class TestDeriveActivityKind:
    """#755 review AX-F1/F2 — the KIND labels what `activity` means so a consumer
    (and the empty case) is interpretable without co-reading `state`. Two kinds,
    mirroring derive_activity's branches exactly."""

    def test_awaiting_human_is_wait(self):
        assert wt.derive_activity_kind(
            None, state=bl.STATE_AWAITING_HUMAN) == wt.ACTIVITY_KIND_WAIT

    def test_blocked_is_wait(self):
        assert wt.derive_activity_kind(
            None, state=bl.STATE_BLOCKED) == wt.ACTIVITY_KIND_WAIT

    def test_running_is_work(self):
        assert wt.derive_activity_kind(
            None, state=bl.STATE_RUNNING) == wt.ACTIVITY_KIND_WORK

    def test_idle_is_work(self):
        assert wt.derive_activity_kind(
            None, state=bl.STATE_IDLE) == wt.ACTIVITY_KIND_WORK

    def test_unknown_is_work_not_a_third_kind(self):
        # A live-but-undeclared session is `unknown` yet its activity is the
        # head-subject work proxy → the kind must be `work`, consistent with the
        # string. This is why there is deliberately no third `unknown` kind.
        assert wt.derive_activity_kind(
            None, state=bl.STATE_UNKNOWN) == wt.ACTIVITY_KIND_WORK

    def test_empty_state_is_work(self):
        assert wt.derive_activity_kind(None, state="") == wt.ACTIVITY_KIND_WORK

    def test_declared_activity_is_work_even_when_waiting(self):
        # A self-report is a work summary the session chose to show; it wins in
        # derive_activity, so the kind follows it to `work` (label matches string).
        assert wt.derive_activity_kind(
            "手動で調査中", state=bl.STATE_AWAITING_HUMAN) == wt.ACTIVITY_KIND_WORK


class TestActivityKindForRow:
    def test_awaiting_row_is_wait(self):
        row = {"state": bl.STATE_AWAITING_HUMAN,
               "git": {"head_subject": "feat: x"}}
        assert wt.activity_kind_for_row(row) == wt.ACTIVITY_KIND_WAIT

    def test_running_row_is_work(self):
        row = {"state": bl.STATE_RUNNING}
        assert wt.activity_kind_for_row(row) == wt.ACTIVITY_KIND_WORK

    def test_wait_row_activity_without_stamped_kind_is_wait(self):
        # ms-173 e-6562 で旧契約 (「row activity = 自己申告 ⇒ work」) を supersede:
        # legacy server (e-6292..e-6533) の activity は head-subject 代理値であり
        # うるため、kind 無し + 待機 state は wait とする (実測: awaiting_human 行が
        # 「Merge pull request #757 …」を work として運用室に出た)。
        row = {"state": bl.STATE_AWAITING_HUMAN, "activity": "明示"}
        assert wt.activity_kind_for_row(row) == wt.ACTIVITY_KIND_WAIT

    def test_stamped_kind_is_authoritative_over_state(self):
        # e-6533+ server が stamp した kind は再導出で上書きしない — サーバは
        # intent (自己申告) の有無を知っており、行だけ見るこちらより正確。
        row = {"state": bl.STATE_AWAITING_HUMAN, "activity": "明示",
               "activity_kind": wt.ACTIVITY_KIND_WORK}
        assert wt.activity_kind_for_row(row) == wt.ACTIVITY_KIND_WORK

    def test_enrich_row_stamps_kind_wait_for_empty_waiting(self):
        # The AX-F2 crux: an empty activity on a waiting row is labelled `wait`,
        # so a consumer renders "待機・理由不明" rather than blank/"working".
        row = {"session_id": "s1", "state": bl.STATE_AWAITING_HUMAN,
               "git": {"head_subject": "Merge pull request #999"}}
        out = wt.enrich_row(row)
        assert out["activity"] == ""
        assert out["activity_kind"] == wt.ACTIVITY_KIND_WAIT

    def test_enrich_row_stamps_kind_work_for_running(self):
        row = {"session_id": "s1", "state": bl.STATE_RUNNING,
               "git": {"head_subject": "feat: y"}}
        out = wt.enrich_row(row)
        assert out["activity_kind"] == wt.ACTIVITY_KIND_WORK
