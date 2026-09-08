"""ms-159 / e-6293 — the ops面 roster: all sessions grouped by root target.

The C+A attention面 (e-6246) showed only 要対応 sessions. The D slice widens it
to a roster (default) grouped by root target with 作業 target / 状態 / activity /
待機, demoting 要対応 to `--attention-only`. We pin the pure roster helpers
(scope / root grouping / sort) and the CLI's default-roster vs --attention-only
rendering + dispatch parity for the new flags.
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import attention  # noqa: E402
import bus_liveness  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _row(sid, state, since, *, root=None, target=None, activity="", user="", email=""):
    r = {"session_id": sid, "state": state, "state_since": since,
         "activity": activity}
    wt = {}
    if root is not None or target is not None:
        wt = {"root": root, "target": target}
    r["working_target"] = wt
    if user:
        r["user_id"] = user
    if email:
        r["actor"] = {"email": email}
    return r


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

class TestScopeMatches:
    def test_matches_user_id(self):
        assert attention.scope_matches({"user_id": "u1"}, "u1")

    def test_matches_actor_email_fallback(self):
        assert attention.scope_matches({"actor": {"email": "a@b"}}, "a@b")

    def test_no_match(self):
        assert not attention.scope_matches({"user_id": "u2"}, "u1")

    def test_empty_identity_matches_all(self):
        assert attention.scope_matches({"user_id": "u2"}, "")


class TestRootAndTarget:
    def test_root_of(self):
        r = _row("s", "idle", None,
                 root={"id": "beacon-1", "label": "Beacon"})
        assert attention.root_of(r) == ("beacon-1", "Beacon")

    def test_root_of_missing(self):
        assert attention.root_of({"working_target": {}}) == ("", "(no target)")

    def test_target_label(self):
        r = _row("s", "idle", None, target={"kind": "milestone", "id": "ms-159"})
        assert attention.target_label(r) == "milestone:ms-159"

    def test_target_label_none(self):
        assert attention.target_label({"working_target": {"target": {}}}) == "—"


class TestFilterRoster:
    def _rows(self, now):
        ago = lambda **k: _iso(now - datetime.timedelta(**k))
        return [
            _row("s-run", bus_liveness.STATE_RUNNING, ago(minutes=1),
                 root={"id": "p1", "label": "P1"}, user="me"),
            _row("s-wait", bus_liveness.STATE_AWAITING_HUMAN, ago(hours=2),
                 root={"id": "p1", "label": "P1"}, user="me"),
            _row("s-other", bus_liveness.STATE_IDLE, ago(minutes=1),
                 root={"id": "p2", "label": "P2"}, user="teammate"),
        ]

    def test_scope_self_filters_others(self):
        now = _now()
        out = attention.filter_roster(self._rows(now), my_identity="me", scope="self")
        assert {r["session_id"] for r in out} == {"s-run", "s-wait"}

    def test_scope_team_keeps_all(self):
        now = _now()
        out = attention.filter_roster(self._rows(now), my_identity="me", scope="team")
        assert len(out) == 3

    def test_attention_only(self):
        now = _now()
        out = attention.filter_roster(self._rows(now), my_identity="me",
                                      scope="self", attention_only=True)
        assert {r["session_id"] for r in out} == {"s-wait"}

    def test_root_filter(self):
        now = _now()
        out = attention.filter_roster(self._rows(now), my_identity="me",
                                      scope="team", root_id="p2")
        assert {r["session_id"] for r in out} == {"s-other"}


class TestGroupByRoot:
    def test_groups_and_orders_by_urgency(self):
        now = _now()
        ago = lambda **k: _iso(now - datetime.timedelta(**k))
        rows = [
            _row("s-run", bus_liveness.STATE_RUNNING, ago(minutes=1),
                 root={"id": "p2", "label": "P2"}),
            _row("s-wait", bus_liveness.STATE_AWAITING_HUMAN, ago(hours=3),
                 root={"id": "p1", "label": "P1"}),
            _row("s-idle", bus_liveness.STATE_IDLE, ago(minutes=1),
                 root={"id": "p1", "label": "P1"}),
        ]
        groups = attention.group_by_root(rows)
        labels = [g[0] for g in groups]
        # P1 has the awaiting_human → floats above P2 (which is only running).
        assert labels == ["P1", "P2"]
        # within P1, the awaiting_human sorts before the idle.
        p1_rows = groups[0][1]
        assert [r["session_id"] for r in p1_rows] == ["s-wait", "s-idle"]


# ---------------------------------------------------------------------------
# CLI — roster default vs --attention-only
# ---------------------------------------------------------------------------

class TestCmdAttentionRoster:
    @pytest.fixture
    def wired(self, monkeypatch):
        import cmd_attention
        now = _now()
        ago = lambda **k: _iso(now - datetime.timedelta(**k))
        rows = [
            _row("sv-run", bus_liveness.STATE_RUNNING, ago(minutes=1),
                 root={"id": "p1", "label": "Beacon"},
                 target={"kind": "milestone", "id": "ms-159"},
                 activity="D スライス実装中", user="me"),
            _row("sv-wait", bus_liveness.STATE_AWAITING_HUMAN, ago(hours=2),
                 root={"id": "p1", "label": "Beacon"},
                 target={"kind": "task", "id": "e-6293"}, user="me"),
            _row("sv-team", bus_liveness.STATE_IDLE, ago(minutes=1),
                 root={"id": "p2", "label": "Sales"}, user="teammate"),
        ]

        class FakeClient:
            def list_user_sessions(self, **k):
                return list(rows)

            def list_sessions(self, pid, **k):
                return list(rows)

        monkeypatch.setattr(cmd_attention, "_get_api_client", lambda: (FakeClient(), {}))
        monkeypatch.setattr(cmd_attention, "_resolve_bus_project_id", lambda cfg: "proj")
        monkeypatch.setattr(cmd_attention, "_read_credentials_for_identity",
                            lambda: ("", "me"))
        return cmd_attention

    def test_default_roster_scope_self(self, wired, capsys, monkeypatch):
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "")
        monkeypatch.setenv("BEACON_ATTENTION_SCOPE", "")
        monkeypatch.setenv("BEACON_ATTENTION_TARGET", "")
        monkeypatch.setenv("BEACON_JSON", "")
        wired.cmd_attention()
        out = capsys.readouterr().out
        # scope=self (default) → my two sessions shown, teammate's hidden.
        assert "sv-run" in out and "sv-wait" in out
        assert "sv-team" not in out
        # grouped under the root label, and activity + target rendered.
        assert "Beacon" in out
        assert "ms-159" in out and "D スライス実装中" in out

    def test_scope_team_shows_others(self, wired, capsys, monkeypatch):
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "")
        monkeypatch.setenv("BEACON_ATTENTION_SCOPE", "team")
        monkeypatch.setenv("BEACON_ATTENTION_TARGET", "")
        monkeypatch.setenv("BEACON_JSON", "")
        wired.cmd_attention()
        out = capsys.readouterr().out
        assert "sv-team" in out and "Sales" in out

    def test_attention_only_narrows(self, wired, capsys, monkeypatch):
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "1")
        monkeypatch.setenv("BEACON_ATTENTION_SCOPE", "")
        monkeypatch.setenv("BEACON_ATTENTION_TARGET", "")
        monkeypatch.setenv("BEACON_JSON", "")
        wired.cmd_attention()
        out = capsys.readouterr().out
        assert "sv-wait" in out
        assert "sv-run" not in out  # running folded in 要対応 view


# ---------------------------------------------------------------------------
# dispatch (Windows parity) — new flags
# ---------------------------------------------------------------------------

class TestDispatchParity:
    @pytest.fixture
    def captured(self, monkeypatch):
        from beacon_cli import dispatch
        box = {}
        monkeypatch.setattr(dispatch.subprocess, "call",
                            lambda cmd, env=None, **k: box.update(cmd=list(cmd), env=dict(env or {})) or 0)
        return box

    @pytest.fixture
    def project_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".beacon").mkdir()
        (tmp_path / ".beacon" / "project.json").write_text('{"name":"t","milestones":[]}')
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
        yield tmp_path

    @pytest.fixture
    def no_bash(self, monkeypatch):
        from beacon_cli import main as main_mod
        monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
        monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)

    def test_new_flags_translate(self, captured, project_dir, no_bash):
        from beacon_cli import main as main_mod
        rc = main_mod.main([
            "attention", "--attention-only", "--scope", "team",
            "--target", "ms-159", "--json"])
        assert rc == 0
        env = captured["env"]
        assert env["BEACON_ATTENTION_ATTENTION_ONLY"] == "1"
        assert env["BEACON_ATTENTION_SCOPE"] == "team"
        assert env["BEACON_ATTENTION_TARGET"] == "ms-159"
        assert env["BEACON_JSON"] == "1"
        assert captured["cmd"][-1] == "attention"
