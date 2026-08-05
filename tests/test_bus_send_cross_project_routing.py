"""Unit tests for `bus send` cross-project pre-flight
(ms-60 follow-up → ms-94 / e-2291 全面改修 2026-07-06).

## 経緯

- 2026-06-09 dogfood で silent misroute pattern が判明: sender の cwd 由来
  project_id と recipient の polling project_id が食い違うと、 event は
  wrong bus に書かれて配信されない (= delivered receipt の absent という
  negative signal でしか気付けない)。
- ms-60 の初期実装は `dm_discover` (= 同マシン bridge scan) 経由で lookup
  していたが、 remote (別マシン) / bridge 未起動 の recipient には効かず
  「セッションがいません → 静かに cwd fallback」 の footgun が残っていた。
- ms-94 / e-2291 は server-side lookup (`list_sessions` / `list_user_sessions`)
  に置換して remote / bridge 無しの recipient も見つかるようにし、 not-found
  は **hard error** に昇格 (opt-out env で soft-warn 復活可)。

## テスト対象

  * non-dm channel        → no change
  * empty recipient       → no change
  * SKIP_TO_CHECK env     → no change
  * target project 一致    → no change (target 内 lookup で hit)
  * caller の他 project 一致 → auto-route (loud warning)
  * NO_AUTO_ROUTE + 他 project 一致 → hard error (routing 拒否)
  * どこにも見つからない → hard error (new default)
  * どこにも見つからない + ALLOW_UNKNOWN=1 → soft warn + no route change (legacy)
  * lookup 自体が失敗 (network / auth) → soft warn + no route change (defensive)
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_bus  # noqa: E402  (ms-127 e-4803: bus handlers live here)


RECIPIENT = "cfgw5d79ll-1781009858157-94a72e40"
TARGET = "beacon-b95643"
OTHER = "trailnode-cd652b"


class _MockClient:
    """Mock ApiClient with configurable list_sessions / list_user_sessions."""

    def __init__(
        self,
        target_sessions: list | Callable | None = None,
        user_sessions: list | Callable | None = None,
    ):
        self._target = target_sessions if target_sessions is not None else []
        self._user = user_sessions if user_sessions is not None else []

    def list_sessions(self, project_id, *_, **__):
        if callable(self._target):
            return self._target(project_id)
        return self._target

    def list_user_sessions(self, *_, **__):
        if callable(self._user):
            return self._user()
        return self._user


def _stub_client(monkeypatch, target=None, user=None):
    """Patch _get_api_client to return a mock client + empty config."""
    mock = _MockClient(target, user)
    monkeypatch.setattr(cmd_bus, "_get_api_client", lambda: (mock, {}))
    return mock


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_SKIP_TO_CHECK", raising=False)
    monkeypatch.delenv("BEACON_BUS_NO_AUTO_ROUTE", raising=False)
    monkeypatch.delenv("BEACON_BUS_ALLOW_UNKNOWN", raising=False)


# ---------------------------------------------------------------------------
# Pass-through cases (non-DM / empty recipient / skip env)
# ---------------------------------------------------------------------------

def test_non_dm_channel_skips():
    """Broadcast channels have no fixed recipient — never re-route."""
    assert cmd_bus._validate_recipient_project(
        RECIPIENT, TARGET, "session-broadcast"
    ) == TARGET


def test_empty_recipient_skips():
    assert cmd_bus._validate_recipient_project("", TARGET, "dm") == TARGET


def test_skip_env_bypasses(monkeypatch):
    monkeypatch.setenv("BEACON_BUS_SKIP_TO_CHECK", "1")
    # Even without any API stub the bypass wins (no lookup performed).
    assert cmd_bus._validate_recipient_project(
        RECIPIENT, TARGET, "dm"
    ) == TARGET


# ---------------------------------------------------------------------------
# Found in target project (no route change)
# ---------------------------------------------------------------------------

def test_recipient_in_target_project_unchanged(monkeypatch):
    _stub_client(
        monkeypatch,
        target=[{"session_id": RECIPIENT}],
        user=[],
    )
    assert cmd_bus._validate_recipient_project(
        RECIPIENT, TARGET, "dm"
    ) == TARGET


# ---------------------------------------------------------------------------
# Auto-route case (recipient in caller's other project)
# ---------------------------------------------------------------------------

def test_recipient_in_other_project_auto_routes(monkeypatch, capsys):
    _stub_client(
        monkeypatch,
        target=[],
        user=[{"session_id": RECIPIENT, "project_id": OTHER}],
    )
    routed = cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == OTHER
    err = capsys.readouterr().err
    assert "Auto-routing" in err
    assert OTHER in err
    assert TARGET in err


def test_recipient_in_other_project_strict_mode_errors(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_BUS_NO_AUTO_ROUTE", "1")
    _stub_client(
        monkeypatch,
        target=[],
        user=[{"session_id": RECIPIENT, "project_id": OTHER}],
    )
    with pytest.raises(SystemExit) as exc:
        cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "polls project" in err
    assert OTHER in err


# ---------------------------------------------------------------------------
# Not-found case (= e-2291 新挙動、default hard error)
# ---------------------------------------------------------------------------

def test_recipient_not_found_anywhere_hard_errors_by_default(monkeypatch, capsys):
    """新 default (e-2291): どこにも見つからない = silent misroute 危険 → refuse。

    従来の soft-warn + cwd fallback は BEACON_BUS_ALLOW_UNKNOWN=1 で復活可 (=
    後方互換 opt-out)。
    """
    _stub_client(monkeypatch, target=[], user=[])
    with pytest.raises(SystemExit) as exc:
        cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "見つかりません" in err  # 日本語のエラーメッセージ
    assert "e-2291" in err
    # ヒント (b): user-scoped 経路 (--to-user) の提案を含むこと。
    assert "--to-user" in err


def test_allow_unknown_env_restores_soft_warn(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_BUS_ALLOW_UNKNOWN", "1")
    _stub_client(monkeypatch, target=[], user=[])
    routed = cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    # legacy soft-warn 経路: hard-error にならず、 target project にそのまま送る。
    assert routed == TARGET
    err = capsys.readouterr().err
    assert "not visible" in err
    assert "BEACON_BUS_ALLOW_UNKNOWN" in err


# ---------------------------------------------------------------------------
# Defensive bypass when API lookup itself fails
# ---------------------------------------------------------------------------

def test_target_lookup_api_failure_bypasses(monkeypatch, capsys):
    """target project の list_sessions が network 例外を投げた場合、
    caller の他 project lookup もスキップし、 defensive に元 target を返す
    (= 「発信自体を殺す guard」 逆転 footgun を防ぐ)。
    """
    def _raise(_pid):
        raise RuntimeError("simulated network failure")

    _stub_client(monkeypatch, target=_raise, user=[])

    routed = cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == TARGET
    err = capsys.readouterr().err
    assert "API 経由で失敗" in err


def test_user_sessions_api_failure_bypasses(monkeypatch, capsys):
    """target lookup は成功 (recipient 不在) だが list_user_sessions が失敗
    したケース: defensive fall-through で元 target。 hard error にはしない。
    """
    def _raise():
        raise RuntimeError("simulated failure")

    _stub_client(monkeypatch, target=[], user=_raise)

    routed = cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == TARGET
    err = capsys.readouterr().err
    assert "API 経由で失敗" in err


# ---------------------------------------------------------------------------
# Ordering: target match wins over other-project match
# ---------------------------------------------------------------------------

def test_target_match_wins_over_user_sessions_match(monkeypatch, capsys):
    """recipient が target project にも caller の他 project にも居るなら、
    target 内 match を優先 (= route 変更しない)。 auto-route 通知は出ない。
    """
    _stub_client(
        monkeypatch,
        target=[{"session_id": RECIPIENT}],
        user=[{"session_id": RECIPIENT, "project_id": OTHER}],
    )
    routed = cmd_bus._validate_recipient_project(RECIPIENT, TARGET, "dm")
    assert routed == TARGET
    err = capsys.readouterr().err
    assert "Auto-routing" not in err
