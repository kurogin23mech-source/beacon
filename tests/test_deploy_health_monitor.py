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

import deploy_health as dh  # noqa: E402


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


def test_ok_when_prod_matches_marker():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="abc1234", target_rev="abc1234def",
        target_age_seconds=10_000, owner_user_id="u-owner", live_sessions=[],
        last_alerted_rev="", send=send)
    assert v["status"] == "ok" and v["alerted"] is False
    assert sent == [], "healthy prod must not alert"


def test_deploying_within_grace_does_not_alert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="old", target_rev="new", target_age_seconds=60,
        owner_user_id="u-owner", live_sessions=[], last_alerted_rev="",
        send=send, grace_seconds=900)
    assert v["status"] == "deploying" and not sent


def test_lagging_alerts_the_deploying_session():
    send, sent = _recorder()
    sessions = [_sess("sv-dep", "newcafe")]  # session at the target rev
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", target_rev="newcafe0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=sessions, last_alerted_rev="",
        send=send, grace_seconds=900, prod_ancestry=dh.ANCESTRY_BEHIND)
    assert v["status"] == "lagging" and v["alerted"] is True
    assert len(sent) == 1
    recipient, text = sent[0]
    assert recipient["session_id"] == "sv-dep"
    assert "遅れ" in text or "stuck" in text


def test_ahead_is_not_an_alert():
    # prod is a *descendant* of the marker → a deploy happened but wasn't
    # recorded. Drift, not an incident — no alert, no red.
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="newrev0", target_rev="oldmark0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=[], last_alerted_rev="",
        send=send, grace_seconds=900, prod_ancestry=dh.ANCESTRY_AHEAD)
    assert v["status"] == "ahead"
    assert v["alerted"] is False and sent == []


def test_no_marker_is_not_an_alert():
    # No deploy marker yet → can't judge lag on rev; stay quiet.
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="anything", target_rev="", target_age_seconds=10_000,
        owner_user_id="u-owner", live_sessions=[], last_alerted_rev="", send=send)
    assert v["status"] == "no_target"
    assert v["alerted"] is False and sent == []


def test_unknown_direction_past_grace_is_lagging():
    # Direction couldn't be determined (shallow / diverged) → conservatively
    # treated as behind so a real stuck deploy is never dropped.
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", target_rev="newcafe0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "newcafe0")],
        last_alerted_rev="", send=send, grace_seconds=900,
        prod_ancestry=dh.ANCESTRY_UNKNOWN)
    assert v["status"] == "lagging" and v["alerted"] is True


def test_unreachable_alerts():
    send, sent = _recorder()
    sessions = [_sess("sv-owner", "whatever")]
    v = MON.decide_and_alert(
        reachable=False, prod_rev="", target_rev="newcafe0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=sessions, last_alerted_rev="",
        send=send)
    assert v["status"] == "unreachable" and v["alerted"] is True
    assert len(sent) == 1


def test_dedup_same_rev_does_not_realert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", target_rev="newcafe0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "newcafe0")],
        last_alerted_rev="newcafe0", send=send, grace_seconds=900,
        prod_ancestry=dh.ANCESTRY_BEHIND)
    assert v["status"] == "lagging"
    assert v["alerted"] is False and v.get("dedup") is True
    assert sent == [], "already alerted for this target_rev → no re-alert"


def test_new_rev_alerts_even_after_prior_alert():
    send, sent = _recorder()
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", target_rev="different", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "different")],
        last_alerted_rev="an-older-rev", send=send, grace_seconds=900,
        prod_ancestry=dh.ANCESTRY_BEHIND)
    assert v["alerted"] is True and len(sent) == 1


def test_send_failure_marks_not_alerted():
    def send(recipient, text):
        return False  # e.g. owner offline / bus down
    v = MON.decide_and_alert(
        reachable=True, prod_rev="oldrev0", target_rev="newcafe0", target_age_seconds=1800,
        owner_user_id="u-owner", live_sessions=[_sess("sv-dep", "newcafe0")],
        last_alerted_rev="", send=send, grace_seconds=900,
        prod_ancestry=dh.ANCESTRY_BEHIND)
    assert v["status"] == "lagging"
    assert v["sent"] is False and v["alerted"] is False


# ---------------------------------------------------------------------------
# IO helpers: read_deploy_marker / git_prod_ancestry (subprocess mocked so the
# branches are testable without a live git state) — maintainability review
# 2026-07-30.
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402


def _run_result(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr="")


def test_read_deploy_marker_missing_tag_returns_empty():
    # git rev-parse --verify --quiet on a missing ref: nonzero exit, empty out.
    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return _run_result(stdout="", returncode=1)
        return _run_result()
    with mock.patch.object(MON.subprocess, "run", side_effect=fake_run):
        rev, age = MON.read_deploy_marker("deployed-prod")
    assert rev == "" and age is None


def test_read_deploy_marker_present_uses_tag_date():
    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return _run_result(stdout="abc1234def\n", returncode=0)
        if "for-each-ref" in cmd:
            return _run_result(stdout="1000\n", returncode=0)  # unix ts
        return _run_result()
    with mock.patch.object(MON.subprocess, "run", side_effect=fake_run), \
            mock.patch("time.time", return_value=1900.0):
        rev, age = MON.read_deploy_marker("deployed-prod")
    assert rev == "abc1234def"
    assert age == 900.0  # 1900 - 1000


def test_git_prod_ancestry_behind():
    # prod is an ancestor of the marker → BEHIND.
    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return _run_result(returncode=0)  # both revs resolve
        if "merge-base" in cmd:
            # first call: is prod ancestor of target? yes.
            return _run_result(returncode=0)
        return _run_result()
    with mock.patch.object(MON.subprocess, "run", side_effect=fake_run):
        assert MON.git_prod_ancestry("prodrev", "markrev") == dh.ANCESTRY_BEHIND


def test_git_prod_ancestry_ahead():
    calls = {"merge_base": 0}

    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return _run_result(returncode=0)
        if "merge-base" in cmd:
            calls["merge_base"] += 1
            # 1st: prod ancestor of target? no. 2nd: target ancestor of prod? yes.
            return _run_result(returncode=1 if calls["merge_base"] == 1 else 0)
        return _run_result()
    with mock.patch.object(MON.subprocess, "run", side_effect=fake_run):
        assert MON.git_prod_ancestry("prodrev", "markrev") == dh.ANCESTRY_AHEAD


def test_git_prod_ancestry_unknown_when_rev_missing():
    def fake_run(cmd, **kw):
        if "rev-parse" in cmd:
            return _run_result(returncode=1)  # a rev doesn't resolve locally
        return _run_result()
    with mock.patch.object(MON.subprocess, "run", side_effect=fake_run):
        assert MON.git_prod_ancestry("prodrev", "markrev") == dh.ANCESTRY_UNKNOWN


def test_git_prod_ancestry_unknown_when_empty():
    assert MON.git_prod_ancestry("", "markrev") == dh.ANCESTRY_UNKNOWN
    assert MON.git_prod_ancestry("prodrev", "") == dh.ANCESTRY_UNKNOWN
