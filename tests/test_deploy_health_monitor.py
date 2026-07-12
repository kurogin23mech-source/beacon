"""Tests for the deploy-health monitor orchestration (ms-105 e-3230).

Exercises scripts/deploy-health-monitor.py's ``decide_and_alert`` with injected
IO (no network / bus / CLI). The pure detection + recipient logic it delegates
to is covered by test_deploy_health.py; here we pin the alert / dedup wiring.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))


def _load():
    path = REPO / "scripts" / "deploy-health-monitor.py"
    spec = importlib.util.spec_from_file_location("deploy_health_monitor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MON = _load()


def _sess(sid, head, user="u-owner"):
    return {"session_id": sid, "user_id": user, "live": True,
            "last_active": "2026-07-12T09:00:00Z", "git": {"head_short": head}}


def _recorder():
    sent = []

    def send(recipient, text):
        sent.append((recipient, text))
        return True
    return send, sent


def test_ok_when_prod_matches_main():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="abc1234", main_rev="abc1234def",
        main_age=10_000, owner_user_id="u-owner", live_sessions=[],
        last_alerted_rev="", send=send)
    assert v["status"] == "ok" and v["alerted"] is False
    assert sent == [], "healthy prod must not alert"


def test_deploying_within_grace_does_not_alert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="old", main_rev="new", main_age=60,
        owner_user_id="u-owner", live_sessions=[], last_alerted_rev="",
        send=send, grace_seconds=900)
    assert v["status"] == "deploying" and not sent


def test_lagging_alerts_the_deploying_session():
    send, sent = _recorder()
    sessions = [_sess("sv-dep", "newcafe")]  # session at the target rev
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", main_rev="newcafe0", main_age=1800,
        owner_user_id="u-owner", live_sessions=sessions, last_alerted_rev="",
        send=send, grace_seconds=900)
    assert v["status"] == "lagging" and v["alerted"] is True
    assert len(sent) == 1
    recipient, text = sent[0]
    assert recipient["session_id"] == "sv-dep"
    assert "遅れ" in text or "stuck" in text


def test_unreachable_alerts():
    send, sent = _recorder()
    sessions = [_sess("sv-owner", "whatever")]
    v = MON.decide_and_alert(
        reachable=False, prod_rev="", main_rev="newcafe0", main_age=1800,
        owner_user_id="u-owner", live_sessions=sessions, last_alerted_rev="",
        send=send)
    assert v["status"] == "unreachable" and v["alerted"] is True
    assert len(sent) == 1


def test_dedup_same_rev_does_not_realert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", main_rev="newcafe0", main_age=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "newcafe0")],
        last_alerted_rev="newcafe0", send=send, grace_seconds=900)
    assert v["status"] == "lagging"
    assert v["alerted"] is False and v.get("dedup") is True
    assert sent == [], "already alerted for this main_rev → no re-alert"


def test_new_rev_alerts_even_after_prior_alert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", main_rev="different", main_age=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "different")],
        last_alerted_rev="an-older-rev", send=send, grace_seconds=900)
    assert v["alerted"] is True and len(sent) == 1


def test_send_failure_marks_not_alerted():
    def send(recipient, text):
        return False  # e.g. owner offline / bus down
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", main_rev="newcafe0", main_age=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "newcafe0")],
        last_alerted_rev="", send=send, grace_seconds=900)
    assert v["status"] == "lagging"
    assert v["sent"] is False and v["alerted"] is False
