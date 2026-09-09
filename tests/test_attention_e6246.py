"""ms-159 / e-6246 — the attention filter/sort (lib/attention).

Pins AC4's contract: `beacon attention` keeps only awaiting_human / blocked /
terminated:failed, folds running / idle / unknown / clean-terminated, and orders
the survivors by state_since oldest-first (longest wait at the top).
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import attention  # noqa: E402
import bus_liveness  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _row(sid, state, since=None, **extra):
    r = {"session_id": sid, "state": state}
    if since is not None:
        r["state_since"] = since
    r.update(extra)
    return r


class TestNeedsAttention:
    def test_awaiting_human_and_blocked_kept(self):
        assert attention.needs_attention(_row("a", bus_liveness.STATE_AWAITING_HUMAN))
        assert attention.needs_attention(_row("b", bus_liveness.STATE_BLOCKED))

    def test_running_idle_unknown_folded(self):
        assert not attention.needs_attention(_row("a", bus_liveness.STATE_RUNNING))
        assert not attention.needs_attention(_row("b", bus_liveness.STATE_IDLE))
        assert not attention.needs_attention(_row("c", bus_liveness.STATE_UNKNOWN))

    def test_clean_terminated_folded_failed_kept(self):
        assert not attention.needs_attention(_row("a", bus_liveness.STATE_TERMINATED))
        assert attention.needs_attention(
            _row("b", bus_liveness.STATE_TERMINATED, state_detail="failed"))
        assert attention.needs_attention(
            _row("c", bus_liveness.STATE_TERMINATED, terminated_reason="failed"))

    def test_missing_or_unknown_state_field(self):
        assert not attention.needs_attention({"session_id": "x"})
        assert not attention.needs_attention(_row("y", "some-native-state"))


class TestFilterAttention:
    def test_folds_non_attention_states(self):
        now = _now()
        rows = [
            _row("run", bus_liveness.STATE_RUNNING, _iso(now)),
            _row("idle", bus_liveness.STATE_IDLE, _iso(now)),
            _row("wait", bus_liveness.STATE_AWAITING_HUMAN, _iso(now)),
            _row("unk", bus_liveness.STATE_UNKNOWN, _iso(now)),
        ]
        out = attention.filter_attention(rows)
        assert [r["session_id"] for r in out] == ["wait"]

    def test_sorted_oldest_first(self):
        now = _now()
        rows = [
            _row("recent", bus_liveness.STATE_AWAITING_HUMAN,
                 _iso(now - datetime.timedelta(minutes=1))),
            _row("oldest", bus_liveness.STATE_BLOCKED,
                 _iso(now - datetime.timedelta(hours=3))),
            _row("middle", bus_liveness.STATE_AWAITING_HUMAN,
                 _iso(now - datetime.timedelta(minutes=30))),
        ]
        out = attention.filter_attention(rows)
        # Longest wait first.
        assert [r["session_id"] for r in out] == ["oldest", "middle", "recent"]

    def test_rows_without_state_since_sort_last(self):
        now = _now()
        rows = [
            _row("no-since", bus_liveness.STATE_AWAITING_HUMAN),
            _row("has-since", bus_liveness.STATE_AWAITING_HUMAN,
                 _iso(now - datetime.timedelta(minutes=5))),
        ]
        out = attention.filter_attention(rows)
        assert [r["session_id"] for r in out] == ["has-since", "no-since"]

    def test_empty_and_none(self):
        assert attention.filter_attention([]) == []
        assert attention.filter_attention(None) == []

    def test_stable_order_on_equal_state_since(self):
        # Ties broken by session_id for a deterministic total order.
        same = _iso(_now() - datetime.timedelta(minutes=10))
        rows = [
            _row("zeta", bus_liveness.STATE_AWAITING_HUMAN, same),
            _row("alpha", bus_liveness.STATE_AWAITING_HUMAN, same),
        ]
        out = attention.filter_attention(rows)
        assert [r["session_id"] for r in out] == ["alpha", "zeta"]


class TestFormatWait:
    def test_durations(self):
        now = _now()
        assert attention.format_wait(_iso(now - datetime.timedelta(seconds=30)), now) == "just now"
        assert attention.format_wait(_iso(now - datetime.timedelta(minutes=8)), now) == "8m"
        assert attention.format_wait(_iso(now - datetime.timedelta(hours=3, minutes=12)), now) == "3h12m"
        assert attention.format_wait(_iso(now - datetime.timedelta(days=2, hours=5)), now) == "2d05h"

    def test_unparseable_is_question_mark(self):
        assert attention.format_wait("", _now()) == "?"
        assert attention.format_wait("garbage", _now()) == "?"


class TestCmdAttention:
    """The CLI shell (lib/cmd_attention): fetch → filter → render."""

    @pytest.fixture
    def wired(self, monkeypatch):
        import cmd_attention
        now = _now()

        def _iso_ago(**kw):
            return _iso(now - datetime.timedelta(**kw))

        rows = [
            _row("sv-run", bus_liveness.STATE_RUNNING, _iso_ago(minutes=1)),
            _row("sv-old", bus_liveness.STATE_AWAITING_HUMAN, _iso_ago(hours=3),
                 actor={"machine": "mac"}, cwd="/wt/old", project_name="beacon"),
            _row("sv-new", bus_liveness.STATE_AWAITING_HUMAN, _iso_ago(minutes=8),
                 actor={"email": "b@y"}, project_name="cairn"),
            _row("sv-idle", bus_liveness.STATE_IDLE, _iso_ago(minutes=2)),
            _row("sv-unknown", bus_liveness.STATE_UNKNOWN, _iso_ago(minutes=5)),
        ]

        class FakeClient:
            def list_user_sessions(self, **k):
                return list(rows)

            def list_sessions(self, pid, **k):
                return list(rows)

        monkeypatch.setattr(cmd_attention, "_get_api_client", lambda: (FakeClient(), {}))
        monkeypatch.setattr(cmd_attention, "_resolve_bus_project_id", lambda cfg: "proj")
        # Empty identity ⇒ scope=self matches all rows (these tests pin the
        # 要対応 filter, not the scope filter; don't let real local creds hide rows).
        monkeypatch.setattr(cmd_attention, "_read_credentials_for_identity",
                            lambda: ("", ""))
        return cmd_attention

    def test_human_output_filters_and_sorts(self, wired, capsys, monkeypatch):
        # C+A "who is waiting on me" is now the --attention-only view (ms-159
        # e-6293 demoted 要対応 from the default to a filter over the roster).
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "1")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "1")
        monkeypatch.setenv("BEACON_JSON", "")
        wired.cmd_attention()
        out = capsys.readouterr().out
        assert "2 件" in out
        # awaiting_human kept; running/idle/unknown folded.
        assert "sv-old" in out and "sv-new" in out
        assert "sv-run" not in out and "sv-idle" not in out and "sv-unknown" not in out
        # oldest-first ordering.
        assert out.index("sv-old") < out.index("sv-new")

    def test_json_output_is_filtered_rows(self, wired, capsys, monkeypatch):
        import json as _json
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "1")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "1")
        monkeypatch.setenv("BEACON_JSON", "1")
        wired.cmd_attention()
        out = capsys.readouterr().out
        data = _json.loads(out)
        assert {r["session_id"] for r in data} == {"sv-old", "sv-new"}

    def test_empty_message_when_nothing_waiting(self, wired, capsys, monkeypatch):
        import cmd_attention

        class EmptyClient:
            def list_user_sessions(self, **k):
                return []
            def list_sessions(self, pid, **k):
                return []
        monkeypatch.setattr(cmd_attention, "_get_api_client", lambda: (EmptyClient(), {}))
        monkeypatch.setenv("BEACON_ATTENTION_ALL_PROJECTS", "")
        monkeypatch.setenv("BEACON_ATTENTION_ATTENTION_ONLY", "1")
        monkeypatch.setenv("BEACON_JSON", "")
        cmd_attention.cmd_attention()
        out = capsys.readouterr().out
        assert "待っているセッションはありません" in out
