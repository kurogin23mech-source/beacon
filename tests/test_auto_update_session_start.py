"""ms-103: SessionStart 自動アップデート hook のテスト。

ネットワークにも実インストールにも触れず、版比較 / opt-out / 1 日 1 回 cache gate /
熟成待ち (公開後 24h) / up-to-date no-op / smoke 失敗時ロールバックの分岐を検証する。
"""
from __future__ import annotations

import io
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from beacon_cli.hooks import session_start as ss  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # cache / log をテスト用 tmp に隔離し、stdin drain を無害化する。
    monkeypatch.setattr(ss, "_cache_path", lambda: tmp_path / "auto-update.json")
    monkeypatch.setattr(ss, "_log_path", lambda: tmp_path / "auto-update.log")
    monkeypatch.setattr(ss.sys, "stdin", io.StringIO(""))
    # opt-out はデフォルト無効 (=更新有効) にしておく。
    monkeypatch.delenv("BEACON_AUTO_UPDATE", raising=False)


# --- semver 比較 -----------------------------------------------------------

def test_is_newer_semver():
    assert ss._is_newer("0.57.0", "0.56.0") is True
    assert ss._is_newer("0.56.1", "0.56.0") is True
    assert ss._is_newer("0.56.0", "0.56.0") is False
    assert ss._is_newer("0.9.0", "0.10.0") is False   # 文字列比較でなく数値
    assert ss._is_newer("1.0.0", "0.99.99") is True


# --- main() の分岐: spawn されたか (= 更新が発火したか) で判定 ---------------

def _spawn_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(ss, "_spawn_apply", lambda latest, prev="": calls.append((latest, prev)))
    return calls


def test_optout_disables(monkeypatch):
    monkeypatch.setenv("BEACON_AUTO_UPDATE", "0")
    calls = _spawn_spy(monkeypatch)
    monkeypatch.setattr(ss, "_current_version", lambda: "0.56.0")
    monkeypatch.setattr(ss, "_pypi_latest_info", lambda: ("0.99.0", 0))  # 新版だが
    assert ss.main([]) == 0
    assert calls == []          # opt-out なので更新しない


def test_cache_gate_skips_within_a_day(monkeypatch):
    calls = _spawn_spy(monkeypatch)
    # 直近にチェック済み → PyPI を見ずに no-op。
    ss._write_cache({"last_check": time.time() - 3600})
    monkeypatch.setattr(ss, "_current_version", lambda: "0.56.0")
    monkeypatch.setattr(ss, "_pypi_latest_info",
                        lambda: pytest.fail("should not hit PyPI within cache window"))
    assert ss.main([]) == 0
    assert calls == []


def test_up_to_date_no_update(monkeypatch):
    calls = _spawn_spy(monkeypatch)
    monkeypatch.setattr(ss, "_current_version", lambda: "0.56.0")
    monkeypatch.setattr(ss, "_pypi_latest_info", lambda: ("0.56.0", 0))  # 同版
    assert ss.main([]) == 0
    assert calls == []


def test_maturity_gate_holds_fresh_release(monkeypatch):
    calls = _spawn_spy(monkeypatch)
    monkeypatch.setattr(ss, "_current_version", lambda: "0.56.0")
    # 新版だが公開 1 時間前 (<24h) → 熟成待ちで見送る。
    monkeypatch.setattr(ss, "_pypi_latest_info", lambda: ("0.57.0", time.time() - 3600))
    assert ss.main([]) == 0
    assert calls == []


def test_newer_and_mature_triggers_update(monkeypatch):
    calls = _spawn_spy(monkeypatch)
    monkeypatch.setattr(ss, "_current_version", lambda: "0.56.0")
    # 新版 + 公開 48h 前 (熟成済) → 更新発火。
    monkeypatch.setattr(ss, "_pypi_latest_info", lambda: ("0.57.0", time.time() - 48 * 3600))
    assert ss.main([]) == 0
    assert calls == [("0.57.0", "0.56.0")]


# --- (a) smoke 失敗時に直前状態へロールバックする ---------------------------

def test_apply_rolls_back_on_smoke_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "_detect_method", lambda: "pip")
    applied = []
    monkeypatch.setattr(ss, "_run", lambda cmd, log: applied.append(cmd) or 0)
    # 更新は走るが smoke が False → ロールバックが呼ばれる。
    monkeypatch.setattr(ss, "_smoke_check", lambda: False)
    rollbacks = []
    monkeypatch.setattr(ss, "_rollback",
                        lambda method, root, prev, log: rollbacks.append((method, prev)))
    ss._apply_update("0.57.0", "0.56.0")
    assert any("pip" in c[0] or "install" in c for c in [" ".join(a) for a in applied])
    assert rollbacks == [("pip", "0.56.0")]   # 直前 version へ戻す


def test_apply_no_rollback_on_smoke_ok(monkeypatch):
    monkeypatch.setattr(ss, "_detect_method", lambda: "pip")
    monkeypatch.setattr(ss, "_run", lambda cmd, log: 0)
    monkeypatch.setattr(ss, "_smoke_check", lambda: True)
    rollbacks = []
    monkeypatch.setattr(ss, "_rollback",
                        lambda *a, **k: rollbacks.append(a))
    ss._apply_update("0.57.0", "0.56.0")
    assert rollbacks == []   # smoke OK なのでロールバックしない
