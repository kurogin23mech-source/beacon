"""operation-fires claim の period バケット一般化 (ms-151 / e-5477).

2 層:

1. **pure** (lib/operation_period.period_key): 日以上の粒度は日付 (= 従来キーと一致、
   backward compat)、時間以下の粒度は細かいバケット。「同日内の複数 cadence 発火で
   二重発火しない」= 同 period は同キー (dedup) / 別 period は別キー (それぞれ発火) を
   決定的な now で示す。

2. **endpoint 配線**: claim endpoint が op の schedule.frequency を読み、cadence に応じた
   period を store の claim に渡すことを、store をスタブして確認する。
"""
from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import operation_period as op_period  # noqa: E402

UTC = datetime.timezone.utc


def _dt(h, m, s=0):
    return datetime.datetime(2026, 8, 24, h, m, s, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. pure period_key
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("freq", ["", "daily", "weekdays", "weekly", "monthly",
                                  "something-unknown"])
def test_day_or_coarser_returns_date(freq):
    assert op_period.period_key(freq, _dt(9, 5)) == "2026-08-24"


def test_backward_compat_equals_old_date_key():
    # 従来キーは datetime.now().date().isoformat()。daily はそれと完全一致。
    now = _dt(23, 59, 59)
    assert op_period.period_key("daily", now) == now.date().isoformat()


def test_hourly_same_hour_same_key_different_hour_differs():
    assert op_period.period_key("hourly", _dt(9, 5)) == \
        op_period.period_key("hourly", _dt(9, 55))
    assert op_period.period_key("hourly", _dt(9, 5)) != \
        op_period.period_key("hourly", _dt(10, 5))


def test_minute_bucket_dedup_within_window_across_windows_differ():
    # 5 分 cadence: 同じ 5 分窓は同キー、別窓は別キー。
    assert op_period.period_key("5min", _dt(9, 2)) == \
        op_period.period_key("5min", _dt(9, 4))
    assert op_period.period_key("5min", _dt(9, 2)) != \
        op_period.period_key("5min", _dt(9, 7))


def test_no_double_fire_same_period_allows_across_periods():
    # AC: 同日内の複数 cadence 発火で二重発火しない。
    # 同一時間窓の 2 発火 = 同キー (2 発目は既存 claim を見て弾かれる)。
    a = op_period.period_key("hourly", _dt(9, 5))
    b = op_period.period_key("hourly", _dt(9, 40))
    assert a == b
    # 別の時間窓 = 別キー (それぞれ 1 回発火できる、= 1 日 1 回に潰れない)。
    c = op_period.period_key("hourly", _dt(11, 5))
    assert c != a


def test_second_bucket_for_circular_cadence():
    # 「循環」= 秒単位 cadence もバケット化できる。
    assert op_period.period_key("30s", _dt(9, 0, 10)) == \
        op_period.period_key("30s", _dt(9, 0, 20))
    assert op_period.period_key("30s", _dt(9, 0, 10)) != \
        op_period.period_key("30s", _dt(9, 0, 40))


def test_minute_aliases_and_zero_guard():
    for freq in ("5m", "5min", "5 minutes"):
        assert op_period.period_key(freq, _dt(9, 2)).startswith("2026-08-24-m5-")
    # 0 分は無効 → 安全側 = 日付粒度。
    assert op_period.period_key("0min", _dt(9, 2)) == "2026-08-24"


# ---------------------------------------------------------------------------
# 2. endpoint 配線 (claim が op の cadence から period を計算して store に渡す)
# ---------------------------------------------------------------------------
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

_client = TestClient(app_module.app)
PID = "beacon-b95643"


@pytest.fixture
def claim_env(monkeypatch):
    project = {
        "name": "T", "milestones": [], "owner": "u1", "members": [],
        "schema_version": 1,
        "operations": [
            {"id": "op-hourly", "status": "open",
             "schedule": {"frequency": "hourly"}, "entries": []},
            {"id": "op-daily", "status": "open",
             "schedule": {"frequency": "daily"}, "entries": []},
        ],
    }
    monkeypatch.setattr(app_module.db, "get_project", lambda pid: project)
    captured = {}

    def fake_claim(project_id, op_id, period, session_id):
        captured["period"] = period
        return {"claimed": True, "claimed_by": session_id, "claimed_at": "t"}

    monkeypatch.setattr(app_module.db, "claim_operation_fire_if_new", fake_claim)
    monkeypatch.setattr(app_module, "_auth_enabled", False)  # dev: skip role gate
    return captured


def test_endpoint_uses_hourly_bucket(claim_env):
    r = _client.post(f"/api/projects/{PID}/operation-fires/op-hourly/claim",
                     json={"session_id": "s1"})
    assert r.status_code == 200, r.text
    # hourly バケットは日付に時 (T HH) が付く。
    assert "T" in claim_env["period"]


def test_endpoint_uses_date_bucket_for_daily(claim_env):
    r = _client.post(f"/api/projects/{PID}/operation-fires/op-daily/claim",
                     json={"session_id": "s1"})
    assert r.status_code == 200, r.text
    # daily は従来通り日付キー (時刻成分なし)。
    assert "T" not in claim_env["period"]
    assert claim_env["period"].count("-") == 2  # YYYY-MM-DD
