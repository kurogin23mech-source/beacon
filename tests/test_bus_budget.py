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
import cmd_bus  # noqa: E402  (ms-127 e-4803: bus handlers live here)


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
              "BEACON_BUS_DELIVERY", "BEACON_JSON",
              "BEACON_BUS_IN_REPLY_TO", "BEACON_BUS_MANUAL"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# grant / show / clear
# ---------------------------------------------------------------------------

def test_grant_writes_budget_file_with_expected_schema(project_dir,
                                                         monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    cmd_bus.cmd_bus_budget_grant()
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
        cmd_bus.cmd_bus_budget_grant()
    assert exc.value.code == 1


def test_grant_rejects_zero_or_negative(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "0")
    with pytest.raises(SystemExit):
        cmd_bus.cmd_bus_budget_grant()


def test_show_when_not_granted(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    cmd_bus.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "not granted" in out
    assert "autonomous mode disabled" in out


def test_show_when_armed(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    cmd_bus.cmd_bus_budget_show()
    out = capsys.readouterr().out
    assert "0/3 used" in out
    assert "3 remaining" in out
    assert "armed" in out


def test_clear_removes_budget_file(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    path = project_dir / ".beacon" / "bus-budget.json"
    assert path.exists()
    cmd_bus.cmd_bus_budget_clear()
    assert not path.exists()


def test_clear_when_no_budget_is_idempotent(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    cmd_bus.cmd_bus_budget_clear()  # must not crash
    capsys.readouterr()


# ---------------------------------------------------------------------------
# _bus_budget_consume_one — the gate primitive
# ---------------------------------------------------------------------------

def test_consume_allows_when_no_budget_file(project_dir, monkeypatch, capsys):
    """Manual one-off CLI sends must work without a budget. The gate only
    applies when explicitly armed — otherwise the autonomous-mode safety
    feature becomes a friction tax on normal usage."""
    _clear_bus_env(monkeypatch)
    allowed, state = cmd_bus._bus_budget_consume_one()
    assert allowed is True
    assert state.get("armed") is False


def test_consume_decrements_when_armed(project_dir, monkeypatch, capsys):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    allowed, state = cmd_bus._bus_budget_consume_one()
    assert allowed is True
    assert state["used"] == 1
    # Round trip the file: another consume sees the higher used count.
    allowed, state = cmd_bus._bus_budget_consume_one()
    assert state["used"] == 2


def test_consume_refuses_when_exhausted(project_dir, monkeypatch, capsys):
    """The gate must refuse the (N+1)-th consume to actually be a gate. The
    refuse must not mutate `used` further so re-grant math stays clean."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "2")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    assert cmd_bus._bus_budget_consume_one()[0] is True
    assert cmd_bus._bus_budget_consume_one()[0] is True
    allowed, state = cmd_bus._bus_budget_consume_one()
    assert allowed is False
    assert state["used"] == 2  # NOT incremented past total


def test_consume_treats_corrupted_file_as_no_budget(project_dir, monkeypatch,
                                                     capsys):
    """A crashed write leaving a half-JSON file must not lock out the user
    nor silently re-arm with a stale value. Fall back to "no budget"."""
    _clear_bus_env(monkeypatch)
    path = project_dir / ".beacon" / "bus-budget.json"
    path.write_text("{this is not json")
    allowed, state = cmd_bus._bus_budget_consume_one()
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
                       context="", rationale="", client_event_id="", is_retry=False):
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
        cmd_bus, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    return stub


def test_manual_send_without_in_reply_to_bypasses_gate(project_dir, monkeypatch,
                                                        capsys, stub_client):
    """Manual usage path: the human typing `bus send` in a terminal IS the
    approval. No --in-reply-to → no gate, even if no budget granted."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    cmd_bus.cmd_bus_send()
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
        cmd_bus.cmd_bus_send()
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
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    cmd_bus.cmd_bus_send()
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
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    cmd_bus.cmd_bus_send()
    cmd_bus.cmd_bus_send()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cmd_bus.cmd_bus_send()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "exhausted" in err
    assert "2/2" in err
    assert len(stub_client.calls) == 2


def test_manual_send_still_works_even_with_budget_exhausted(project_dir,
                                                              monkeypatch, capsys,
                                                              stub_client):
    """The gate must NOT lock out human one-off sends — but e-3901 changed HOW.

    Before e-3901 a bare send (no --in-reply-to) bypassed the gate (fail-open) —
    exactly the hole an armed autonomous loop could exploit by omitting the flag.
    Now the escape is an EXPLICIT, audited `--manual`: with the budget exhausted
    a bare send is REFUSED (armed ⇒ gated), while `--manual` sends the one-off.
    The human keeps their escape hatch; the autonomous loop cannot forge a silent
    bypass by dropping a flag."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "1")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    # Burn the budget via a reply
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")
    cmd_bus.cmd_bus_send()
    capsys.readouterr()
    # e-3901: a bare send while armed+exhausted (no --in-reply-to, no --manual)
    # is now GATED — the old fail-open bypass is closed.
    monkeypatch.delenv("BEACON_BUS_IN_REPLY_TO", raising=False)
    with pytest.raises(SystemExit) as exc:
        cmd_bus.cmd_bus_send()
    assert exc.value.code == 1
    assert "budget exhausted" in capsys.readouterr().err
    assert len(stub_client.calls) == 1, "gated bare send must not reach the client"
    # The explicit human escape: --manual sends the one-off despite exhaustion,
    # recorded as an audited override.
    monkeypatch.setenv("BEACON_BUS_MANUAL", "1")
    cmd_bus.cmd_bus_send()
    out = capsys.readouterr().out
    assert "Sent:" in out
    assert len(stub_client.calls) == 2


def test_armed_bare_send_is_gated_without_in_reply_to(project_dir, monkeypatch,
                                                      capsys, stub_client):
    """e-3901 core regression: an armed session (budget granted) that sends
    WITHOUT --in-reply-to is still gated — a budget slot is consumed. Before
    e-3901 the gate keyed off --in-reply-to, so an autonomous loop could drop
    the flag to bypass the budget cap AND the cross-user HOLD. Pins that the
    send context (armed), not the presence of the flag, triggers the gate."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "3")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    # No BEACON_BUS_IN_REPLY_TO — the exact shape an autonomous loop would use
    # to escape the pre-e-3901 gate.
    cmd_bus.cmd_bus_send()
    assert "Sent:" in capsys.readouterr().out
    budget = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert budget["used"] == 1, "armed bare send must consume a budget slot"


def test_armed_manual_override_bypasses_gate(project_dir, monkeypatch, capsys,
                                              stub_client):
    """e-3901: --manual is the explicit, audited escape from the armed gate. An
    armed bare send with --manual does NOT consume the budget and stamps
    manual_override on the payload for the decision-event trail. Unlike the
    old fail-open bypass (drop --in-reply-to), this leaves a loud audit record."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "2")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_MANUAL", "1")
    cmd_bus.cmd_bus_send()
    capsys.readouterr()
    budget = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert budget["used"] == 0, "--manual override must not consume budget"
    assert stub_client.calls, "manual send must reach the client"
    assert stub_client.calls[-1]["payload"].get("manual_override") is True


def test_reply_json_mode_includes_budget_state(project_dir, monkeypatch, capsys,
                                                 stub_client):
    """Autonomous-loop scripts need machine-readable budget info to decide
    whether to keep going. JSON mode appends it under _budget."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_BUDGET_N", "5")
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-original")
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_bus.cmd_bus_send()
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
        cmd_bus.cmd_bus_budget_grant()
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
        cmd_bus.cmd_bus_budget_grant()
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
    cmd_bus.cmd_bus_budget_grant()
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
    cmd_bus.cmd_bus_budget_grant()
    capsys.readouterr()

    class _Boom:
        def post_bus_event(self, *a, **kw):
            raise RuntimeError("cloud is down")

        def issue_bus_envelope(self, *a, **kw):
            # e-1290: envelope-by-default. Issuance fails the same 5xx-class
            # way; the CLI falls back to no-envelope, then the post fails.
            raise RuntimeError("cloud is down")

    monkeypatch.setattr(cmd_bus, "_get_api_client",
                        lambda: (_Boom(), {"project_id": "p"}))
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ch")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "e-x")
    with pytest.raises(RuntimeError):
        cmd_bus.cmd_bus_send()
    # Budget went 0 → 1 (pessimistic) then refunded back to 0 on the failure.
    budget = json.loads(
        (project_dir / ".beacon" / "bus-budget.json").read_text())
    assert budget["used"] == 0, "a failed send must refund the consumed slot"


# ---------------------------------------------------------------------------
# ms-185 e-7080 / e-7321 — ここに送信予算の迂回 (Trek-internal bypass) を測る
# 19 件が在った。内訳: 迂回カウンタ 3 件 / カウンタ表示 2 件 / 迂回判定 9 件 /
# 参加者の粒度 5 件。
#
# **判定ごと落としたので、試験も一緒に落とした。** 死んだ判定のための試験を
# 残すと、保守対象が減らないまま「測っているつもり」の緑が積み上がる
# (e-7321 が指摘した病理そのもの)。粒度 5 件の注記も「撤去は e-7080 で、その時
# この 5 件も一緒に落ちる」と予告していた。
#
# カウンタ表示の 2 件のうち ``test_budget_show_omits_trek_section_when_no_bypassed``
# は、表示を落とした今は **自明に通る空振りの緑** になる。空振りの緑は無ガードより
# 悪いので残さない。
#
# いま守るべき性質は「自動送信が例外なく予算ゲートを通る」ことで、それはこの
# ファイル前半の予算ゲート本体の試験が測っている (迂回路が無いので別枠が要らない)。
# 戻す方向のガードは scripts/invert-shared-trek-rule-removal.py が持つ。
#
# 消した 413 行を読み直したいときは git 履歴から取る (このファイルは追跡されて
# いるので、削除の 1 つ前の版がそのまま残っている):
#   git show <この commit>^:tests/test_bus_budget.py
# ---------------------------------------------------------------------------
