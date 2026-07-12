"""Tests for the bus budget gate (ms-54 / e-1000).

The budget gate's whole job is to **structurally prevent a runaway autonomous
loop**. These tests lock in:
  * grant/show/clear round-trip the file on disk,
  * `bus send` decrements on success and refuses when exhausted,
  * a missing budget file means "not armed" — manual one-off DMs from the
    CLI must still work, the gate only applies when explicitly armed,
  * the failure-after-decrement edge: a cloud call that 500s STILL consumed
    the budget (pessimistic — we'd rather under-send than spin on retries).

A corrupted budget file is also treated as "no budget" so a half-written
file from a crashed write can't accidentally re-arm or lock out the agent.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A bare beacon project dir on disk so .beacon/bus-budget.json has a real
    home. The CLI's `get_project_file()` walks for the project; we just point
    it at our tmp_path directly via the env var the CLI already supports."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "test", "milestones": []}))
    monkeypatch.setenv(
        "BEACON_PROJECT_FILE",
        str(beacon_dir / "project.json"),
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _clear_bus_env(monkeypatch):
    for k in ("BEACON_BUS_BUDGET_N", "BEACON_BUS_CHANNEL",
              "BEACON_BUS_PAYLOAD", "BEACON_BUS_SENDER",
              "BEACON_BUS_DELIVERY", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# grant / show / clear
# ---------------------------------------------------------------------------

def test_grant_writes_budget_file_with_expected_schema(project_dir,
                                                         monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    path = project_dir / ".beacon" / "bus-budget.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["total"] == 5
    assert data["used"] == 0
    assert data["granted_at"].endswith("Z")
    assert data["channels"] == []


def test_grant_rejects_non_integer(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "five")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_budget_grant()
    assert exc.value.code == 1


def test_grant_rejects_zero_or_negative(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "0")
    with pytest.raises(SystemExit):
        commands.cmd_bus_budget_grant()


def test_show_when_not_granted(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    commands.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "not granted" in out
    assert "autonomous mode disabled" in out


def test_show_when_armed(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    commands.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "0/3 used" in out
    assert "3 remaining" in out
    assert "armed" in out


def test_clear_removes_budget_file(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    path = project_dir / ".beacon" / "bus-budget.json"
    assert path.exists()
    commands.cmd_bus_budget_clear()
    assert not path.exists()


def test_clear_when_no_budget_is_idempotent(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    commands.cmd_bus_budget_clear()  # must not crash
    capsys.readouterr()


# ---------------------------------------------------------------------------
# _bus_budget_consume_one — the gate primitive
# ---------------------------------------------------------------------------

def test_consume_allows_when_no_budget_file(project_dir, monkeypatch, capsys):
    """Manual one-off CLI sends must work without a budget. The gate only
    applies when explicitly armed — otherwise the autonomous-mode safety
    feature becomes a friction tax on normal usage."""
    _clear_bus_env(monkeypatch)
    allowed, state = commands._bus_budget_consume_one()
    assert allowed is True
    assert state.get("armed") is False


def test_consume_decrements_when_armed(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    allowed, state = commands._bus_budget_consume_one()
    assert allowed is True
    assert state["used"] == 1
    # Round trip the file: another consume sees the higher used count.
    allowed, state = commands._bus_budget_consume_one()
    assert state["used"] == 2


def test_consume_refuses_when_exhausted(project_dir, monkeypatch, capsys):
    """The gate must refuse the (N+1)-th consume to actually be a gate. The
    refuse must not mutate `used` further so re-grant math stays clean."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "2")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    assert commands._bus_budget_consume_one()[0] is True
    assert commands._bus_budget_consume_one()[0] is True
    allowed, state = commands._bus_budget_consume_one()
    assert allowed is False
    assert state["used"] == 2  # NOT incremented past total


def test_consume_treats_corrupted_file_as_no_budget(project_dir, monkeypatch,
                                                     capsys):
    """A crashed write leaving a half-JSON file must not lock out the user
    nor silently re-arm with a stale value. Fall back to "no budget"."""
    _clear_bus_env(monkeypatch)
    path = project_dir / ".beacon" / "bus-budget.json"
    path.write_text("{this is not json")
    allowed, state = commands._bus_budget_consume_one()
    assert allowed is True
    assert state.get("armed") is False


# ---------------------------------------------------------------------------
# bus_send integration — gate must refuse cleanly without hitting the cloud.
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self):
        self.calls = []

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai",
                       envelope=None, requested_action=None,
                       context="", rationale=""):
        # e-1290: tolerate envelope/requested_action kwargs from the
        # envelope-by-default CLI path. The budget tests don't assert on
        # envelope contents — they care about decrement timing — so we just
        # record them alongside the rest.
        # ms-90 / e-3246: context / rationale も real signature に合わせて受ける。
        self.calls.append({
            "channel": channel, "sender": sender_session_id,
            "payload": payload or {}, "delivery": delivery,
            "envelope": envelope, "requested_action": requested_action,
            "context": context, "rationale": rationale,
        })
        return {
            "event_id": f"e-{len(self.calls)}",
            "channel": channel, "sender_session_id": sender_session_id,
            "payload": payload or {}, "delivery": delivery,
            "created_at": "2026-06-07T01:50:00.000000Z",
        }

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized=None,
                            scope=None, data_class="free",
                            conversation_id=None, in_reply_to=None,
                            chain_depth=0, ttl_seconds=3600):
        # e-1290: in-memory mint so cmd_bus_send's envelope-by-default path
        # has something to embed without standing up the real server.
        return {
            "tier": tier,
            "actions_authorized": list(actions_authorized or []),
            "data_class": data_class,
            "signature": "stub-sig",
        }


@pytest.fixture
def stub_client(monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    return stub


def test_manual_send_without_in_reply_to_bypasses_gate(project_dir, monkeypatch,
                                                        capsys, stub_client):
    """Manual usage path: the human typing `bus send` in a terminal IS the
    approval. No --in-reply-to → no gate, even if no budget granted."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    commands.cmd_bus_send()
    out = capsys.readouterr().out
    assert "Sent:" in out
    assert "budget" not in out  # no budget surfaced when bypassed
    assert len(stub_client.calls) == 1


def test_reply_refused_when_no_budget_granted(project_dir, monkeypatch, capsys,
                                                stub_client):
    """The default state must refuse AI-authored replies. The user's safety
    stance: "until a turn limit is explicitly set, replies require human
    approval." A reply without a budget grant is a denied auto-reply."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_send()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no auto-reply budget is granted" in err
    assert "human approval" in err
    # The cloud must not be hit — the gate is structural, not advisory.
    assert len(stub_client.calls) == 0


def test_reply_succeeds_after_budget_grant(project_dir, monkeypatch, capsys,
                                             stub_client):
    """Once the human grants N auto-replies, the AI can send up to N times
    without further per-reply approval."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    commands.cmd_bus_send()
    out = capsys.readouterr().out
    assert "Sent:" in out
    assert "budget: 1/3" in out
    assert "2 remaining" in out
    assert len(stub_client.calls) == 1
    # The reply also threaded the parent event id into the payload — the
    # inbox hook on the *other* side reads this to chain context.
    assert stub_client.calls[0]["payload"]["in_reply_to"] == "e-original"


def test_reply_refused_when_budget_exhausted(project_dir, monkeypatch, capsys,
                                               stub_client):
    """After N replies the (N+1)-th must refuse — that's the actual cap on
    autonomous-loop runaway."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "2")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    commands.cmd_bus_send()
    commands.cmd_bus_send()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_send()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exhausted" in err
    assert "2/2" in err
    assert len(stub_client.calls) == 2


def test_manual_send_still_works_even_with_budget_exhausted(project_dir,
                                                              monkeypatch, capsys,
                                                              stub_client):
    """The gate must NOT lock out manual sends. Even with budget at 0/0, a
    human-typed `bus send` (no --in-reply-to) goes through. Otherwise a
    safety feature becomes a hostage situation when the user wants to send
    a one-off DM after the autonomous budget ran out."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "1")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    # Burn the budget via a reply
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")
    commands.cmd_bus_send()
    # Now omit --in-reply-to; manual send must succeed despite exhausted budget.
    monkeypatch.delenv("BEACON_BUS_IN_REPLY_TO", raising=False)
    commands.cmd_bus_send()
    out = capsys.readouterr().out
    assert "Sent:" in out
    assert len(stub_client.calls) == 2


def test_reply_json_mode_includes_budget_state(project_dir, monkeypatch, capsys,
                                                 stub_client):
    """Autonomous-loop scripts need machine-readable budget info to decide
    whether to keep going. JSON mode appends it under _budget."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_send()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["channel"] == "ch"
    assert parsed["_budget"]["remaining"] == 4
    assert parsed["_budget"]["used"] == 1
    assert parsed["_budget"]["total"] == 5


# ---------------------------------------------------------------------------
# ms-76 / e-1852: structural禁止帯 — bus budget grant is T1-only.
# ---------------------------------------------------------------------------
# CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier framework)
# forbids T2 (= Operation auto-execute) from re-granting the budget. The whole
# point of the cap is that AI cannot self-escalate; if T2 could grant, a
# long-running Operation would route around the gate by writing a
# "grant N more turns" Operation. We block at the CLI entry by env-flag.

def test_grant_refused_under_operation_auto_execute(project_dir, monkeypatch,
                                                      capsys):
    """An Operation runner that exports BEACON_OPERATION_AUTO_EXECUTE=1 MUST
    NOT be able to grant the budget. This is the structural禁止帯 in ms-76.
    """
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_OPERATION_AUTO_EXECUTE", "1")
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_budget_grant()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "T1-only" in err
    assert "structural禁止帯" in err or "AI self-escalation" in err
    # The budget file MUST NOT be written: structural means the write
    # never happens, not "writes then asks forgiveness".
    path = project_dir / ".beacon" / "bus-budget.json"
    assert not path.exists()


def test_grant_refused_when_operation_envelope_id_set(project_dir, monkeypatch,
                                                       capsys):
    """The Operation runner also sets BEACON_OPERATION_ENVELOPE_ID; we treat
    it as equivalent to BEACON_OPERATION_AUTO_EXECUTE=1 because either
    marker indicates "running under T2"."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_OPERATION_ENVELOPE_ID", "env-test-123")
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_budget_grant()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "T1-only" in err


def test_grant_allowed_when_no_operation_context(project_dir, monkeypatch,
                                                   capsys):
    """The guard MUST NOT regress the normal human-typed path. Without the
    Operation env markers, grant proceeds as before."""
    _clear_bus_env(monkeypatch)
    monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
    monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    path = project_dir / ".beacon" / "bus-budget.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["total"] == 5


def test_reply_refunds_budget_when_cloud_call_fails(project_dir, monkeypatch,
                                                    capsys):
    """ms-100 e-2999: the decrement is still pessimistic (happens BEFORE the
    cloud call so the gate can't be raced), but if the send then FAILS the slot
    is refunded — a failed send must not silently burn an autonomous-reply turn.
    Previously (test_reply_consumes_even_if_cloud_call_would_fail) the slot was
    kept even on failure; e-2999 flips that to refund-on-failure."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()

    class _Boom:
        def post_bus_event(self, *a, **kw):
            raise RuntimeError("cloud is down")

        def issue_bus_envelope(self, *a, **kw):
            # e-1290: envelope-by-default. Issuance fails the same 5xx-class
            # way; the CLI falls back to no-envelope, then the post fails.
            raise RuntimeError("cloud is down")

    monkeypatch.setattr(commands, "_get_api_client",
                        lambda: (_Boom(), {"project_id": "p"}))
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")
    with pytest.raises(RuntimeError):
        commands.cmd_bus_send()
    # Budget went 0 → 1 (pessimistic) then refunded back to 0 on the failure.
    budget = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert budget["used"] == 0, "a failed send must refund the consumed slot"


# ---------------------------------------------------------------------------
# Trek-internal bypass (ms-75 / e-2044)
# ---------------------------------------------------------------------------

def test_record_trek_bypass_creates_counter_when_no_budget(project_dir,
                                                          monkeypatch):
    """A Trek-internal send that bypasses the budget still leaves an audit
    trail. When no budget file exists yet, the helper creates one with a
    counter — armed=False and total/used=0 so the gate semantics are
    unchanged for non-Trek sends."""
    _clear_bus_env(monkeypatch)
    commands._record_bus_budget_trek_bypass("tk-abc")
    path = project_dir / ".beacon" / "bus-budget.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["trek_bypassed"]["tk-abc"] == 1
    assert data["used"] == 0
    assert data["total"] == 0
    assert data.get("armed") is False


def test_record_trek_bypass_increments_existing_counter(project_dir, monkeypatch):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    commands._record_bus_budget_trek_bypass("tk-abc")
    commands._record_bus_budget_trek_bypass("tk-abc")
    commands._record_bus_budget_trek_bypass("tk-xyz")
    data = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert data["trek_bypassed"]["tk-abc"] == 2
    assert data["trek_bypassed"]["tk-xyz"] == 1
    # Regular budget counters untouched.
    assert data["total"] == 5
    assert data["used"] == 0


def test_record_trek_bypass_no_op_on_empty_trek_id(project_dir, monkeypatch):
    _clear_bus_env(monkeypatch)
    commands._record_bus_budget_trek_bypass("")
    # No write; if there is no budget file, none is created.
    path = project_dir / ".beacon" / "bus-budget.json"
    assert not path.exists()


def test_budget_show_displays_trek_bypassed_counts(project_dir, monkeypatch,
                                                    capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    commands._record_bus_budget_trek_bypass("tk-trek1")
    commands._record_bus_budget_trek_bypass("tk-trek1")
    commands._record_bus_budget_trek_bypass("tk-trek2")
    commands.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "0/5 used" in out  # main budget untouched
    assert "Trek-internal bypassed: 3 send(s)" in out
    assert "tk-trek1: 2" in out
    assert "tk-trek2: 1" in out


def test_budget_show_omits_trek_section_when_no_bypassed(project_dir,
                                                         monkeypatch, capsys):
    """Don't add Trek noise to budgets that never saw a Trek-internal send."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    commands.cmd_bus_budget_grant()
    capsys.readouterr()
    commands.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "Trek-internal bypassed" not in out


def test_is_trek_internal_send_returns_false_in_local_mode(project_dir,
                                                            monkeypatch):
    """Local mode (= no .beacon/cloud.json) keeps the strict budget gate.
    Trek scope detection requires the cloud-side session directory."""
    _clear_bus_env(monkeypatch)
    # Stub _is_cloud_mode to False explicitly.
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: False)
    bypass, trek_id = commands._is_trek_internal_send("sv-recipient")
    assert bypass is False
    assert trek_id == ""


def test_is_trek_internal_send_returns_false_for_empty_recipient(project_dir,
                                                                  monkeypatch):
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    bypass, trek_id = commands._is_trek_internal_send("")
    assert bypass is False
    assert trek_id == ""


def test_is_trek_internal_send_returns_true_for_shared_active_trek(project_dir,
                                                                    monkeypatch):
    """Both sender and recipient are joined members of an active trek →
    bypass. Mirrors server-side dm_gate.GATE_REASON_SHARED_TREK."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands, "_resolve_creator_identity",
                         lambda: ("u-sender", "sender@x", "sv-1"))

    class _FakeClient:
        def list_sessions(self, _project_id):
            return [
                {"session_id": "sv-recipient",
                 "actor": {"user_id": "u-recipient", "email": "rec@x"}},
            ]
        def list_treks(self):
            return [{
                "trek_id": "tk-shared",
                "status": "active",
                "members": [
                    {"user_id": "u-sender", "joined_at": "2026-06-19T00:00:00Z"},
                    {"user_id": "u-recipient", "joined_at": "2026-06-19T00:00:00Z"},
                ],
            }]
    monkeypatch.setattr(commands, "_get_api_client",
                         lambda: (_FakeClient(), {"project_id": "p"}))
    monkeypatch.setattr(commands, "_resolve_bus_project_id", lambda _c: "p")
    bypass, trek_id = commands._is_trek_internal_send("sv-recipient")
    assert bypass is True
    assert trek_id == "tk-shared"


def test_is_trek_internal_send_returns_false_when_only_one_side_joined(project_dir,
                                                                       monkeypatch):
    """Pending invitation (= joined_at missing) does not count. Trek scope
    requires both sides to have actually accepted membership."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands, "_resolve_creator_identity",
                         lambda: ("u-sender", "sender@x", "sv-1"))

    class _FakeClient:
        def list_sessions(self, _p):
            return [
                {"session_id": "sv-recipient",
                 "actor": {"user_id": "u-recipient"}},
            ]
        def list_treks(self):
            return [{
                "trek_id": "tk",
                "status": "active",
                "members": [
                    {"user_id": "u-sender", "joined_at": "2026-06-19Z"},
                    {"user_id": "u-recipient", "joined_at": ""},  # not yet joined
                ],
            }]
    monkeypatch.setattr(commands, "_get_api_client",
                         lambda: (_FakeClient(), {"project_id": "p"}))
    monkeypatch.setattr(commands, "_resolve_bus_project_id", lambda _c: "p")
    bypass, _ = commands._is_trek_internal_send("sv-recipient")
    assert bypass is False


def test_is_trek_internal_send_returns_false_when_trek_not_active(project_dir,
                                                                   monkeypatch):
    """Planning / archived treks do not grant Trek scope. The gate must not
    bypass for treks that aren't actively running work."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands, "_resolve_creator_identity",
                         lambda: ("u-sender", "sender@x", "sv-1"))

    class _FakeClient:
        def list_sessions(self, _p):
            return [{"session_id": "sv-r",
                     "actor": {"user_id": "u-recipient"}}]
        def list_treks(self):
            return [{
                "trek_id": "tk",
                "status": "planning",  # not active
                "members": [
                    {"user_id": "u-sender", "joined_at": "x"},
                    {"user_id": "u-recipient", "joined_at": "x"},
                ],
            }]
    monkeypatch.setattr(commands, "_get_api_client",
                         lambda: (_FakeClient(), {"project_id": "p"}))
    monkeypatch.setattr(commands, "_resolve_bus_project_id", lambda _c: "p")
    bypass, _ = commands._is_trek_internal_send("sv-r")
    assert bypass is False


def test_is_trek_internal_send_returns_false_for_same_user(project_dir, monkeypatch):
    """Same user is handled by the higher-level same_user rule, not by
    Trek scope. Returning False here defers to that — the budget gate
    itself also doesn't fire (no in_reply_to is a same-user CLI send),
    so we keep our scope narrow to the actual Trek case."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands, "_resolve_creator_identity",
                         lambda: ("u-same", "x", "sv-1"))

    class _FakeClient:
        def list_sessions(self, _p):
            return [{"session_id": "sv-r", "actor": {"user_id": "u-same"}}]
        def list_treks(self):
            return [{"trek_id": "tk", "status": "active",
                     "members": [{"user_id": "u-same", "joined_at": "x"}]}]
    monkeypatch.setattr(commands, "_get_api_client",
                         lambda: (_FakeClient(), {"project_id": "p"}))
    monkeypatch.setattr(commands, "_resolve_bus_project_id", lambda _c: "p")
    bypass, _ = commands._is_trek_internal_send("sv-r")
    assert bypass is False


def test_is_trek_internal_send_fails_safe_when_api_throws(project_dir, monkeypatch):
    """Any error in the detection path must keep the regular budget gate
    in force — silently relaxing on misconfigured infra is exactly what
    the SPEC forbids."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setattr(commands, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(commands, "_resolve_creator_identity",
                         lambda: ("u-sender", "sender@x", "sv-1"))

    class _BoomClient:
        def list_sessions(self, _p):
            raise RuntimeError("server is on fire")
    monkeypatch.setattr(commands, "_get_api_client",
                         lambda: (_BoomClient(), {"project_id": "p"}))
    monkeypatch.setattr(commands, "_resolve_bus_project_id", lambda _c: "p")
    bypass, _ = commands._is_trek_internal_send("sv-r")
    assert bypass is False
