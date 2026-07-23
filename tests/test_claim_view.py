"""Tests for the 2-layer claim view (ms-112 e-3674).

Pins the SPEC ``xgoANbBympPE2TCnJThO`` acceptance contracts at the read layer:

- AC1: for a target, get "who is LIVE (session layer) + who is the persistent
  assignee (assignee layer)" in ONE view, across target-classes (milestone /
  opportunity / account).
- AC5: non-exclusive — the view only ever produces flags/warnings, never a
  block. ``claim_warns_collision`` is advice, not a gate.
- AC6: 2-layer fallback — a target with no LIVE claimer but a persistent
  assignee still reads as "owned" so a consumer can組み立てる around it.

Also pins the liveness verification tri-state: verified-live / verified-stale /
unverified (local mode) map to healthy True / False / None respectively, and
only non-False claims count as "someone working now".
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import claim_view


def _ms(id="ms-1", **extra):
    base = {"id": id, "title": "Sample", "status": "in_progress"}
    base.update(extra)
    return base


def _occ(session_id="sv-a", machine="mac", agent="claude"):
    return {"session_id": session_id, "machine": machine, "agent": agent,
            "claimed_at": "2026-07-23T00:00:00Z"}


class TestSplitAssigneesParity:
    """The inlined ``_split_assignees`` must stay byte-identical in behaviour to
    ``core._split_assignees`` (the docstring promises parity)."""

    def test_parity_with_core(self):
        import core
        cases = [
            "", None, "alice", "alice,bob", " alice , bob ",
            ["alice", "bob"], ["  ", "carol"], "a,,b", [],
        ]
        for c in cases:
            assert claim_view._split_assignees(c) == core._split_assignees(c), c


class TestSingleView:
    def test_unclaimed_target(self):
        v = claim_view.build_claim_view(_ms())
        assert v["target_id"] == "ms-1"
        assert v["target_kind"] == "milestone"
        assert v["live"] == []
        assert v["assignees"] == []
        assert v["flags"]["unclaimed"] is True
        assert v["flags"]["live"] is False
        assert v["flags"]["assigned"] is False

    def test_live_verified_healthy_by_other(self):
        ms = _ms(occupation=_occ(session_id="sv-other"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-other"}, my_session_id="sv-me")
        assert v["flags"]["live"] is True
        assert v["flags"]["live_by_others"] is True
        assert v["flags"]["live_by_me"] is False
        assert v["live"][0]["healthy"] is True
        assert v["flags"]["unclaimed"] is False

    def test_live_verified_stale_does_not_count(self):
        # Claim exists but its session is NOT among the healthy live set →
        # stale → does not count as "someone working now".
        ms = _ms(occupation=_occ(session_id="sv-dead"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-someone-else"}, my_session_id="sv-me")
        assert v["live"][0]["healthy"] is False
        assert v["flags"]["live"] is False
        assert v["flags"]["live_by_others"] is False
        # No assignee either → still counts as unclaimed (grabbable).
        assert v["flags"]["unclaimed"] is True

    def test_live_unverified_local_mode(self):
        # live_session_ids=None → cannot verify → healthy None → counts as live
        # (conservative, surfaces the warning).
        ms = _ms(occupation=_occ(session_id="sv-other"))
        v = claim_view.build_claim_view(ms, my_session_id="sv-me")
        assert v["live"][0]["healthy"] is None
        assert v["flags"]["live"] is True
        assert v["flags"]["live_by_others"] is True

    def test_live_by_me(self):
        ms = _ms(occupation=_occ(session_id="sv-me"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-me"}, my_session_id="sv-me")
        assert v["flags"]["live_by_me"] is True
        assert v["flags"]["live_by_others"] is False

    def test_assignee_layer_me_vs_others(self):
        ms = _ms(assignee="alice,bob")
        v = claim_view.build_claim_view(ms, my_identities={"ALICE"})
        assert v["assignees"] == ["alice", "bob"]
        assert v["flags"]["assigned"] is True
        assert v["flags"]["assigned_to_me"] is True
        assert v["flags"]["assigned_to_others"] is True  # bob

    def test_ac6_assignee_fallback_without_live(self):
        # No LIVE claimer, but a persistent assignee → reads as owned, NOT
        # unclaimed (2-layer fallback, SPEC AC6).
        ms = _ms(assignee="carol")
        v = claim_view.build_claim_view(
            ms, live_session_ids=set(), my_identities={"me"})
        assert v["flags"]["live"] is False
        assert v["flags"]["assigned"] is True
        assert v["flags"]["unclaimed"] is False


class TestTargetClassAgnostic:
    def test_opportunity_target(self):
        opp = {"id": "opp-3", "label": "Big deal", "assignee": "seller"}
        v = claim_view.build_claim_view(opp, my_identities={"seller"})
        assert v["target_kind"] == "opportunity"
        assert v["label"] == "Big deal"
        assert v["flags"]["assigned_to_me"] is True

    def test_account_target(self):
        acc = {"id": "acc-9", "name": "AcmeCo", "assignee": "rep"}
        v = claim_view.build_claim_view(acc)
        assert v["target_kind"] == "account"
        assert v["label"] == "AcmeCo"


class TestAllTargets:
    def test_build_views_across_collections(self):
        data = {
            "milestones": [_ms("ms-1", occupation=_occ("sv-x")), _ms("ms-2")],
            "opportunities": [{"id": "opp-1", "label": "Deal", "assignee": "s"}],
        }
        views = claim_view.build_claim_views(
            data, live_session_ids={"sv-x"}, my_session_id="sv-x")
        assert set(views) == {"ms-1", "ms-2", "opp-1"}
        assert views["ms-1"]["flags"]["live_by_me"] is True
        assert views["ms-2"]["flags"]["unclaimed"] is True
        assert views["opp-1"]["flags"]["assigned"] is True

    def test_skips_targets_without_id(self):
        data = {"milestones": [{"title": "no id"}, _ms("ms-1")]}
        views = claim_view.build_claim_views(data)
        assert set(views) == {"ms-1"}


class TestConsumerHelpers:
    def test_format_line_empty_when_unclaimed(self):
        v = claim_view.build_claim_view(_ms())
        assert claim_view.format_claim_line(v) == ""

    def test_format_line_live_by_me(self):
        ms = _ms(occupation=_occ("sv-me"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-me"}, my_session_id="sv-me")
        assert "あなたが作業中" in claim_view.format_claim_line(v)

    def test_format_line_live_by_other_verified(self):
        ms = _ms(occupation=_occ("sv-o", machine="win", agent="codex"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-o"}, my_session_id="sv-me")
        line = claim_view.format_claim_line(v)
        assert "別セッションが作業中" in line
        assert "win/codex" in line

    def test_format_line_unverified_hedges(self):
        ms = _ms(occupation=_occ("sv-o"))
        v = claim_view.build_claim_view(ms, my_session_id="sv-me")
        assert "liveness 未確認" in claim_view.format_claim_line(v)

    def test_format_line_assignee_without_live(self):
        ms = _ms(assignee="carol")
        v = claim_view.build_claim_view(ms, live_session_ids=set())
        assert "担当 carol (LIVE claimer なし)" == claim_view.format_claim_line(v)

    def test_warns_collision_non_exclusive(self):
        # Someone else live → warns.
        ms = _ms(occupation=_occ("sv-o"))
        v = claim_view.build_claim_view(
            ms, live_session_ids={"sv-o"}, my_session_id="sv-me")
        assert claim_view.claim_warns_collision(v) is True

        # Mine → no warning.
        ms2 = _ms(occupation=_occ("sv-me"), assignee="me")
        v2 = claim_view.build_claim_view(
            ms2, live_session_ids={"sv-me"}, my_session_id="sv-me",
            my_identities={"me"})
        assert claim_view.claim_warns_collision(v2) is False

    def test_warns_collision_assignee_other(self):
        ms = _ms(assignee="alice")
        v = claim_view.build_claim_view(ms, my_identities={"me"})
        assert claim_view.claim_warns_collision(v) is True


class TestRobustness:
    def test_non_dict_target(self):
        v = claim_view.build_claim_view(None)
        assert v["target_id"] == ""
        assert v["flags"]["unclaimed"] is True

    def test_malformed_occupation_ignored(self):
        ms = _ms(occupation={"machine": "mac"})  # no session_id
        v = claim_view.build_claim_view(ms)
        assert v["live"] == []
