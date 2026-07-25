"""Unit tests for lib/tick_health.py (e-1391 / ms-66).

Pin the tick-liveness cadence math and the alert/dedup decisions without a
live prod / bus. Also exercises scripts/tick-health-monitor.decide_and_alert
with injected IO (mirrors test_deploy_health_monitor's shape).
"""
import datetime
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "scripts"))

import tick_health as th  # noqa: E402


NOW = datetime.datetime(2026, 7, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- evaluate_tick_health ---------------------------------------------------

def test_ok_when_recent():
    last = _iso(NOW - datetime.timedelta(seconds=30))
    h = th.evaluate_tick_health(last, NOW)
    assert h["status"] == "ok"
    assert 29 <= h["seconds_since"] <= 31


def test_stale_when_beyond_threshold():
    last = _iso(NOW - datetime.timedelta(seconds=700))
    h = th.evaluate_tick_health(last, NOW, stale_after_seconds=600)
    assert h["status"] == "stale"
    assert h["seconds_since"] > 600


def test_boundary_exactly_at_threshold_is_ok():
    last = _iso(NOW - datetime.timedelta(seconds=600))
    h = th.evaluate_tick_health(last, NOW, stale_after_seconds=600)
    # strictly greater-than is stale; exactly at threshold stays ok
    assert h["status"] == "ok"


def test_never_when_empty():
    h = th.evaluate_tick_health("", NOW)
    assert h["status"] == "never"
    assert h["seconds_since"] is None
    assert h["last_tick_at"] == ""


def test_never_within_boot_grace_stays_never():
    # Server just booted (uptime < grace) and hasn't ticked yet → not an alert.
    h = th.evaluate_tick_health("", NOW, uptime_seconds=60, never_grace_seconds=300)
    assert h["status"] == "never"
    assert th.should_alert(h, reachable=True) is False


def test_never_past_grace_promotes_to_stale_and_alerts():
    # H1: server up past the grace window but STILL never ticked = driver not
    # installed / dead (the migration failure). Must promote to stale + alert.
    h = th.evaluate_tick_health("", NOW, uptime_seconds=1000, never_grace_seconds=300)
    assert h["status"] == "stale"
    assert th.should_alert(h, reachable=True) is True
    # message must name the "never fired at all" nature, not a stale duration.
    msg = th.format_alert_message(True, h, "https://x")
    assert "一度も発火していません" in msg


def test_never_without_uptime_stays_conservative():
    # Unknown uptime → can't tell boot from dead → stay never (no false alert).
    h = th.evaluate_tick_health("", NOW, uptime_seconds=None)
    assert h["status"] == "never"
    assert th.should_alert(h, reachable=True) is False


def test_never_when_unparseable():
    h = th.evaluate_tick_health("not-a-timestamp", NOW)
    assert h["status"] == "never"


def test_naive_timestamp_treated_as_utc():
    # A timestamp without tz should not blow up (assumed UTC).
    h = th.evaluate_tick_health("2026-07-24T11:59:30.000000", NOW)
    assert h["status"] == "ok"


# --- should_alert -----------------------------------------------------------

def test_should_alert_on_unreachable():
    assert th.should_alert({"status": "ok"}, reachable=False) is True


def test_should_alert_on_stale():
    assert th.should_alert({"status": "stale"}, reachable=True) is True


def test_no_alert_on_ok():
    assert th.should_alert({"status": "ok"}, reachable=True) is False


def test_no_alert_on_never_reachable():
    # A just-restarted box reports never; do not false-positive every deploy.
    assert th.should_alert({"status": "never"}, reachable=True) is False


# --- dedup key --------------------------------------------------------------

def test_dedup_key_distinguishes_outage_nature():
    assert th.alert_dedup_key(False, {}) == "unreachable"
    assert th.alert_dedup_key(True, {"status": "stale"}) != "unreachable"


# --- decide_and_alert (script glue, injected IO) ---------------------------

def _make_send(record):
    def _send(recipient, text):
        record.append((recipient, text))
        return True
    return _send


def _load_monitor():
    # tick-health-monitor.py has a hyphenated name; import it by path.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tick_health_monitor", REPO / "scripts" / "tick-health-monitor.py")
    mon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mon)
    return mon


def test_decide_alerts_once_then_dedups():
    mon = _load_monitor()

    sent = []
    payload = {"last_tick_at": _iso(NOW - datetime.timedelta(seconds=900))}
    live = [{"live": True, "user_id": "owner", "session_id": "sv-1",
             "last_active": "2026-07-24T11:59:00Z"}]

    v1 = mon.decide_and_alert(
        reachable=True, payload=payload, now=NOW, prod_url="https://x",
        owner_user_id="owner", live_sessions=live, last_alerted_key="",
        send=_make_send(sent))
    assert v1["alerted"] is True
    assert len(sent) == 1

    # Same outage nature → deduped, no second send.
    v2 = mon.decide_and_alert(
        reachable=True, payload=payload, now=NOW, prod_url="https://x",
        owner_user_id="owner", live_sessions=live,
        last_alerted_key=v1["dedup_key"], send=_make_send(sent))
    assert v2.get("dedup") is True
    assert len(sent) == 1


def test_decide_no_alert_when_ok():
    mon = _load_monitor()

    sent = []
    payload = {"last_tick_at": _iso(NOW - datetime.timedelta(seconds=30))}
    v = mon.decide_and_alert(
        reachable=True, payload=payload, now=NOW, prod_url="https://x",
        owner_user_id="owner", live_sessions=[], last_alerted_key="",
        send=_make_send(sent))
    assert v["alerted"] is False
    assert sent == []
