"""生存確認の best-effort 経路が ``SystemExit`` で送信ごと死なないことを測る
(ms-185 e-7069 の後始末)。

**実害**: ``_resolve_recipient_live`` は「宛先セッションが生きているか」を確認する
best-effort の一段で、失敗しても送信は続ける設計になっている (失敗時は identity
だけ解決して返す guard が 2 つ在る)。ところがその奥で ``_get_api_client`` が
「ログインしていない」「cloud.json が無い」を **``sys.exit(1)``** で返す。
``SystemExit`` は ``Exception`` を継承しないので素の ``except Exception`` を
**素通りし、送信そのものがプロセスごと死ぬ**。

cloud を設定していない利用者が ``beacon bus send`` を打つと、安全な fallback が
在るのに hard exit する — 「落ちても送信は続ける」という設計意図と正反対。

CI の全件検査で 4 件が ``SystemExit: 1`` で落ちて見つかった。ローカルでは cloud が
設定済なので再現しない (= 環境差で隠れていた)。このテストは**環境に依らず**
再現させる: 探索ヘルパーが ``SystemExit`` を投げる状況を直接作る。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import commands_shared  # noqa: E402


@pytest.fixture(autouse=True)
def _dm_channel(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_NO_LIVE_CHECK", raising=False)


def _raise_system_exit(*_a, **_kw):
    raise SystemExit(1)


def test_discovery_exiting_does_not_kill_the_send(monkeypatch):
    """探索が ``sys.exit`` しても、生存確認は諦めて呼び出し元に返る。"""
    import importlib
    helpers = importlib.import_module("beacon_cli.skills_helpers.dm_discover")
    monkeypatch.setattr(helpers, "discover_and_aggregate", _raise_system_exit)
    monkeypatch.setattr(commands_shared,
                        "_resolve_recipient_email_via_self_sessions",
                        lambda *_a, **_k: "")

    recipient, notice, email = commands_shared._resolve_recipient_live(
        "sv-bob", "dm", advise=None)

    assert recipient == "sv-bob"
    assert notice is None
    assert email == ""


def test_the_import_guard_also_survives_an_exiting_helper(monkeypatch):
    """ヘルパーの import 自体が ``sys.exit`` しても同じ (guard は 2 つある)。

    片方だけ直すと、もう片方の経路で同じ hard exit が残る。
    """
    import importlib
    monkeypatch.setattr(importlib, "import_module", _raise_system_exit)
    monkeypatch.setattr(commands_shared,
                        "_resolve_recipient_email_via_self_sessions",
                        lambda *_a, **_k: "")

    recipient, notice, email = commands_shared._resolve_recipient_live(
        "sv-bob", "dm", advise=None)
    assert (recipient, notice, email) == ("sv-bob", None, "")


def test_both_guards_name_system_exit_explicitly():
    """``except Exception`` に戻されたら赤くなる (= 直したことの裏返し)。

    振る舞いの試験 2 本は monkeypatch で経路を作っているので、guard の書き方が
    戻ったことは上で捕まる。ここはそれに加えて、**2 つある guard の両方**が
    明示していることを見る (片方だけ戻す回帰を名指しで捕まえるため)。
    """
    src = (REPO / "lib" / "commands_shared.py").read_text(encoding="utf-8")
    i = src.index("def _resolve_recipient_live(")
    body = src[i:i + 4000]
    assert body.count("except (Exception, SystemExit):") >= 2, (
        "生存確認の best-effort guard が SystemExit を捕まえなくなっている — "
        "cloud 未設定の利用者の送信がプロセスごと死ぬ")
