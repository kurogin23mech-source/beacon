"""Unit tests for lib/deploy_health.py (ms-105 e-3230).

Pure detection + recipient-resolution logic for the deploy-health monitor.
No live prod / bus is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import deploy_health as dh  # noqa: E402


# ---------------------------------------------------------------------------
# evaluate_deploy_health
# ---------------------------------------------------------------------------

def test_unreachable_is_alert():
    r = dh.evaluate_deploy_health(False, "", "abc1234", 10_000)
    assert r["status"] == dh.UNREACHABLE
    assert r["status"] in dh.ALERT_STATUSES


def test_matching_rev_is_ok():
    r = dh.evaluate_deploy_health(True, "abc1234", "abc1234def567", 10_000)
    assert r["status"] == dh.OK
    assert r["status"] not in dh.ALERT_STATUSES


def test_short_and_full_rev_match():
    # prod reports short, main is full-length — same commit, must be OK.
    assert dh._rev_matches("abc1234", "abc1234def5678901234")
    assert dh._rev_matches("abc1234def5678901234", "abc1234")


def test_fresh_mismatch_is_deploying_not_lagging():
    # main advanced 1 min ago; the pull timer hasn't caught up yet → grace.
    r = dh.evaluate_deploy_health(True, "old1234", "new5678", 60, grace_seconds=900)
    assert r["status"] == dh.DEPLOYING
    assert r["status"] not in dh.ALERT_STATUSES


def test_stale_mismatch_is_lagging():
    # main advanced 30 min ago and prod still hasn't caught up → stuck deploy.
    r = dh.evaluate_deploy_health(True, "old1234", "new5678", 1800, grace_seconds=900)
    assert r["status"] == dh.LAGGING
    assert r["status"] in dh.ALERT_STATUSES


def test_unknown_age_treated_as_past_grace():
    r = dh.evaluate_deploy_health(True, "old1234", "new5678", None)
    assert r["status"] == dh.LAGGING


# ---------------------------------------------------------------------------
# resolve_alert_recipient
# ---------------------------------------------------------------------------

def _sess(sid, head, user="u-owner", live=True, last_active="2026-07-12T00:00:00Z"):
    return {
        "session_id": sid,
        "user_id": user,
        "live": live,
        "last_active": last_active,
        "git": {"head_short": head},
    }


def test_recipient_is_session_at_deployed_rev():
    sessions = [
        _sess("sv-a", "beefcafe"),          # at the deployed rev
        _sess("sv-b", "0000000"),           # somewhere else
    ]
    r = dh.resolve_alert_recipient(sessions, "beefcafe1234", "u-owner")
    assert r["mode"] == "session"
    assert r["session_id"] == "sv-a"


def test_recipient_prefers_most_recently_active_on_tie():
    sessions = [
        _sess("sv-old", "beefcafe", last_active="2026-07-10T00:00:00Z"),
        _sess("sv-new", "beefcafe", last_active="2026-07-12T09:00:00Z"),
    ]
    r = dh.resolve_alert_recipient(sessions, "beefcafe", "u-owner")
    assert r["session_id"] == "sv-new"


def test_recipient_falls_back_to_owner_user_scope():
    # no live session sits at the deployed rev → user-scoped owner DM.
    sessions = [_sess("sv-a", "0000000"), _sess("sv-b", "1111111")]
    r = dh.resolve_alert_recipient(sessions, "beefcafe", "u-owner")
    assert r["mode"] == "user"
    assert r["user_id"] == "u-owner"


def test_recipient_ignores_non_live_sessions():
    sessions = [_sess("sv-dead", "beefcafe", live=False)]
    r = dh.resolve_alert_recipient(sessions, "beefcafe", "u-owner")
    assert r["mode"] == "user", "a dead session at the rev must not be chosen"


def test_recipient_empty_directory_is_owner():
    r = dh.resolve_alert_recipient([], "beefcafe", "u-owner")
    assert r["mode"] == "user"
    assert r["user_id"] == "u-owner"
