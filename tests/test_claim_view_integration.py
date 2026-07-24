"""End-to-end cross-cutting verification of the 2-layer claim view (ms-112 e-3678).

The unit tests (``test_claim_view.py``) pin each helper in isolation. This suite
pins the two SPEC-level GUARANTEES that must hold across the whole consuming
chain (session-start「次の一手」/ dispatch / sales cockpit all read the same
view), so a future refactor can't silently break the contract those three
skills depend on:

- **AC5 非排他 (never blocks)**: the claim view surface only ever produces
  advice (flags / a boolean / a string). There is NO function that removes a
  target, raises to forbid work, or returns a "blocked" state. This test walks
  the public surface and asserts the non-exclusive shape holds for every claim
  configuration (live-by-other, assigned-to-other, both) — work is never
  stopped, only annotated.

- **AC6 2層 fallback (live-empty still owned)**: a target with a persistent
  assignee but NO live claimer reads as owned (``unclaimed == False``), across
  every target-class (milestone / opportunity / account). A consumer can組み立てる
  around the assignee when nobody is live.

Plus a **consumer flag-contract** guard: the exact flag keys the three skills'
instructions reference must exist in every view — if a flag is renamed, the
skills' documented behaviour silently breaks, so this test fails loudly instead.

Plus one **hermetic CLI smoke**: ``beacon claim view --json`` run in a temp
LOCAL project (no cloud) must emit valid JSON with the flag contract, proving
the full I/O shell works end-to-end offline (the liveness-unverified path).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import claim_view


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The flag keys the three consuming skills (session-start / dispatch / cockpit)
# read by name. Keep in lockstep with the skill instructions + build_claim_view.
CONSUMER_FLAG_KEYS = {
    "live", "live_by_me", "live_by_others",
    "assigned", "assigned_to_me", "assigned_to_others",
    "unclaimed",
}


def _mixed_project():
    """A realistic cross-occupation project: dev milestones + sales targets, in
    every claim configuration the consumers must handle."""
    return {
        "milestones": [
            # live by another session + assigned to another → double-collision
            {"id": "ms-1", "title": "A", "assignee": "bob",
             "occupation": {"session_id": "sv-bob", "machine": "win",
                            "agent": "codex", "claimed_at": "2026-07-23T00:00:00Z"}},
            # assigned to me, nobody live
            {"id": "ms-2", "title": "B", "assignee": "me"},
            # totally free
            {"id": "ms-3", "title": "C"},
            # live by ME
            {"id": "ms-4", "title": "D",
             "occupation": {"session_id": "sv-me", "machine": "mac",
                            "agent": "claude", "claimed_at": "2026-07-23T00:00:00Z"}},
        ],
        "opportunities": [
            {"id": "opp-1", "label": "Deal", "assignee": "seller"},   # other's deal
            {"id": "opp-2", "label": "Mine", "assignee": "me"},       # my deal
        ],
        "accounts": [
            {"id": "acc-1", "name": "Acme", "assignee": "rep"},        # other's account
        ],
    }


class TestNonExclusiveGuarantee:
    """AC5: the view never blocks — only annotates."""

    def test_public_surface_has_no_block_path(self):
        # The module exposes readers + advice only. Assert there is no function
        # whose name implies mutation / gating (block / forbid / reject / lock /
        # remove / release / claim-taking). If someone adds one, this fails so
        # the non-exclusive contract is re-examined deliberately.
        forbidden = ("block", "forbid", "reject", "lock", "deny",
                     "remove", "release", "acquire", "take", "reserve")
        public = [n for n in dir(claim_view) if not n.startswith("_")]
        offenders = [n for n in public
                     if any(tok in n.lower() for tok in forbidden)]
        assert offenders == [], f"non-exclusive surface gained a gate: {offenders}"

    def test_collision_configs_only_advise(self):
        # For every collision configuration, the strongest signal available is a
        # boolean advisory — the call returns cleanly, never raises to stop work.
        data = _mixed_project()
        views = claim_view.build_claim_views(
            data, live_session_ids={"sv-bob", "sv-me"},
            my_session_id="sv-me", my_identities={"me"})
        for tid, view in views.items():
            # advice, not a gate: bool, and the target itself is always present
            advice = claim_view.claim_warns_collision(view)
            assert advice in (True, False)
            assert view["target_id"] == tid  # never dropped/hidden

    def test_warns_but_target_still_actionable(self):
        # ms-1 collides (other live + other assigned) — the view WARNS but still
        # carries the full target so a consumer can proceed if the user insists.
        data = _mixed_project()
        views = claim_view.build_claim_views(
            data, live_session_ids={"sv-bob"}, my_session_id="sv-me",
            my_identities={"me"})
        v = views["ms-1"]
        assert claim_view.claim_warns_collision(v) is True
        assert v["flags"]["live_by_others"] is True
        assert v["flags"]["assigned_to_others"] is True
        # Non-exclusive: it is NOT marked unclaimed-forbidden; it's just held.
        assert v["flags"]["unclaimed"] is False
        assert claim_view.format_claim_line(v)  # a human warning exists


class TestTwoLayerFallbackAcrossClasses:
    """AC6: assignee-owned-but-not-live reads as owned, every target-class."""

    def test_fallback_verified_no_live(self):
        data = _mixed_project()
        # Verified liveness, and NONE of the assignees' sessions are live.
        views = claim_view.build_claim_views(
            data, live_session_ids=set(), my_identities={"me"})
        # milestone / opportunity / account each: assigned-to-other, no live →
        # owned (not unclaimed), so a consumer defers rather than grabbing.
        for tid in ("ms-1", "opp-1", "acc-1"):
            v = views[tid]
            assert v["flags"]["live"] is False, tid
            assert v["flags"]["assigned"] is True, tid
            assert v["flags"]["unclaimed"] is False, tid  # ← the fallback
        # A truly free target IS unclaimed (grabbable).
        assert views["ms-3"]["flags"]["unclaimed"] is True

    def test_fallback_is_class_agnostic(self):
        # The same rule fires identically for a milestone and an opportunity.
        data = _mixed_project()
        views = claim_view.build_claim_views(data, live_session_ids=set())
        assert views["ms-2"]["target_kind"] == "milestone"
        assert views["opp-2"]["target_kind"] == "opportunity"
        assert views["ms-2"]["flags"]["unclaimed"] is False
        assert views["opp-2"]["flags"]["unclaimed"] is False


class TestConsumerFlagContract:
    """The flag keys the 3 skills read must exist in every view (drift guard)."""

    def test_every_view_carries_the_contract(self):
        data = _mixed_project()
        for live in (None, set(), {"sv-bob", "sv-me"}):
            views = claim_view.build_claim_views(
                data, live_session_ids=live, my_session_id="sv-me",
                my_identities={"me"})
            for view in views.values():
                assert CONSUMER_FLAG_KEYS <= set(view["flags"]), (
                    live, set(view["flags"]))
                # every flag is a real bool (consumers branch on them)
                assert all(isinstance(view["flags"][k], bool)
                           for k in CONSUMER_FLAG_KEYS)


class TestUnverifiedLivenessPath:
    """local mode (live_session_ids=None) → conservative, still non-blocking."""

    def test_unverified_hedges_not_blocks(self):
        data = _mixed_project()
        views = claim_view.build_claim_views(
            data, live_session_ids=None, my_session_id="sv-me",
            my_identities={"me"})
        # ms-1's other-session claim can't be health-checked → counts as live
        # (surface the warning) but is still just advice.
        v = views["ms-1"]
        assert v["live"][0]["healthy"] is None
        assert v["flags"]["live_by_others"] is True
        assert claim_view.claim_warns_collision(v) is True


class TestCmdClaimViewIOShell:
    """Drive the CLI I/O shell (``commands.cmd_claim_view``) end-to-end, hermetic.

    The real ``beacon claim view`` binary is cloud-first (a bare local
    project.json is not a runnable project), so instead of a subprocess we call
    the command function directly with the cloud round-trips stubbed to the
    local / directory-unreachable path — the exact degraded mode a fresh /
    offline session hits. This exercises the whole shell: JSON assembly, the
    all-targets vs ``--target`` branch, and the flag contract in output."""

    def _load_commands(self, monkeypatch, fixture, *, live_ids=None,
                       focus_directory=None):
        sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
        import commands
        monkeypatch.setattr(commands, "load_project", lambda: fixture)
        monkeypatch.setattr(commands, "_resolve_session_id", lambda: "sv-me")
        monkeypatch.setattr(
            commands, "_resolve_healthy_session_ids", lambda: live_ids)
        # ms-125 e-4094: stub the second LIVE source (bus-directory focus) so the
        # shell stays hermetic — otherwise it would attempt a cloud round-trip.
        monkeypatch.setattr(
            commands, "_resolve_focus_directory", lambda _live: focus_directory)
        return commands

    def test_all_targets_json(self, monkeypatch, capsys):
        fixture = {
            "name": "smoke", "profession": "dev",
            "milestones": [
                {"id": "ms-1", "title": "Foo", "assignee": "someone"},
                {"id": "ms-2", "title": "Bar"},
            ],
        }
        commands = self._load_commands(monkeypatch, fixture, live_ids=None)
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.delenv("BEACON_CLAIM_TARGET_ID", raising=False)
        monkeypatch.delenv("BEACON_CLAIM_TARGET_KIND", raising=False)
        commands.cmd_claim_view()
        views = json.loads(capsys.readouterr().out)
        assert set(views) == {"ms-1", "ms-2"}
        # local / unverified liveness → still non-blocking, flag contract holds.
        assert CONSUMER_FLAG_KEYS <= set(views["ms-1"]["flags"])
        assert views["ms-1"]["flags"]["assigned"] is True
        assert views["ms-1"]["flags"]["unclaimed"] is False
        assert views["ms-2"]["flags"]["unclaimed"] is True

    def test_single_target_json(self, monkeypatch, capsys):
        fixture = {
            "name": "smoke", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "Foo", "assignee": "someone"}],
        }
        commands = self._load_commands(monkeypatch, fixture, live_ids=set())
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-1")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
        commands.cmd_claim_view()
        view = json.loads(capsys.readouterr().out)
        assert view["target_id"] == "ms-1"
        assert CONSUMER_FLAG_KEYS <= set(view["flags"])
        assert view["flags"]["assigned"] is True

    def test_missing_target_is_not_unclaimed(self, monkeypatch, capsys):
        # ms-112 AX finding: a --target that isn't a real walked target must
        # NOT read as unclaimed=true (= free to grab). It carries exists=false
        # and unclaimed=false so a typo can't become a false "safe to claim".
        fixture = {"name": "smoke", "profession": "dev", "milestones": []}
        commands = self._load_commands(monkeypatch, fixture, live_ids=set())
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-999")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
        commands.cmd_claim_view()
        view = json.loads(capsys.readouterr().out)
        assert view["target_id"] == "ms-999"
        assert view["exists"] is False
        assert view["flags"]["unclaimed"] is False

    def test_focus_directory_flows_through_shell(self, monkeypatch, capsys):
        # ms-125 e-4094: the shell must structurally consult BOTH LIVE sources.
        # ms-1 has no occupation claim, but a live session is focused on it via
        # the bus directory — the view must read it as live (not unclaimed).
        fixture = {
            "name": "smoke", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "Foo"}],
        }
        commands = self._load_commands(
            monkeypatch, fixture, live_ids={"sv-other"},
            focus_directory={"ms-1": [{
                "session_id": "sv-other", "machine": "mac",
                "agent": "claude", "focused_at": "2026-07-24T00:00:00Z"}]},
        )
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.delenv("BEACON_CLAIM_TARGET_ID", raising=False)
        monkeypatch.delenv("BEACON_CLAIM_TARGET_KIND", raising=False)
        commands.cmd_claim_view()
        views = json.loads(capsys.readouterr().out)
        assert views["ms-1"]["flags"]["live"] is True
        assert views["ms-1"]["flags"]["live_by_others"] is True
        assert views["ms-1"]["flags"]["unclaimed"] is False
        assert views["ms-1"]["live"][0]["source"] == "focus"

    def test_missing_target_reads_focus(self, monkeypatch, capsys):
        # A --target that isn't in project.json still consults the focus source,
        # so a session focused on it surfaces rather than reading unclaimed.
        fixture = {"name": "smoke", "profession": "dev", "milestones": []}
        commands = self._load_commands(
            monkeypatch, fixture, live_ids={"sv-other"},
            focus_directory={"ms-7": [{
                "session_id": "sv-other", "machine": "mac",
                "agent": "claude", "focused_at": "2026-07-24T00:00:00Z"}]},
        )
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-7")
        monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
        commands.cmd_claim_view()
        view = json.loads(capsys.readouterr().out)
        assert view["flags"]["live"] is True
        assert view["live"][0]["source"] == "focus"

    def test_focus_source_status_disclosed(self, monkeypatch, capsys):
        # ms-125 review (AX): when liveness is verified but the focus source
        # could NOT be fetched, the view discloses focus_source=unavailable
        # instead of silently reading occupation-only.
        fixture = {"name": "smoke", "profession": "dev",
                   "milestones": [{"id": "ms-1", "title": "Foo"}]}
        commands = self._load_commands(
            monkeypatch, fixture, live_ids={"sv-x"}, focus_directory=None)
        monkeypatch.setenv("BEACON_JSON", "1")
        monkeypatch.delenv("BEACON_CLAIM_TARGET_ID", raising=False)
        monkeypatch.delenv("BEACON_CLAIM_TARGET_KIND", raising=False)
        commands.cmd_claim_view()
        views = json.loads(capsys.readouterr().out)
        assert views["ms-1"]["focus_source"] == "unavailable"


class TestInvertFocusDirectory:
    """ms-125 review (maint): the directory→per-target inversion is a pure
    function, unit-tested directly (not hidden behind a stubbed cloud call)."""

    def _load(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
        import commands
        return commands

    def test_inverts_by_focus_milestone(self):
        commands = self._load()
        sessions = [
            {"session_id": "sv-a", "focus": {"milestone": {"id": "ms-1"}},
             "actor": {"machine": "mac", "agent": "claude"},
             "last_heartbeat_at": "2026-07-24T00:00:00Z"},
            {"session_id": "sv-b", "focus": {"milestone": {"id": "ms-1"}},
             "actor": {"machine": "win", "agent": "codex"},
             "last_heartbeat_at": "2026-07-24T00:01:00Z"},
        ]
        out = commands._invert_focus_directory(sessions)
        assert set(out) == {"ms-1"}
        assert {e["session_id"] for e in out["ms-1"]} == {"sv-a", "sv-b"}
        assert out["ms-1"][0]["machine"] == "mac"

    def test_skips_sessions_without_focus_or_sid(self):
        commands = self._load()
        sessions = [
            {"session_id": "sv-a"},                       # no focus
            {"focus": {"milestone": {"id": "ms-1"}}},     # no session_id
            {"session_id": "sv-c", "focus": {}},          # empty focus
            None,                                          # malformed
        ]
        assert commands._invert_focus_directory(sessions) == {}

    def test_falls_back_to_last_active_for_timestamp(self):
        commands = self._load()
        sessions = [{"session_id": "sv-a",
                     "focus": {"milestone": {"id": "ms-2"}},
                     "last_active": "2026-07-24T09:00:00Z"}]
        out = commands._invert_focus_directory(sessions)
        assert out["ms-2"][0]["focused_at"] == "2026-07-24T09:00:00Z"
