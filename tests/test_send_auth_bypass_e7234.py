"""ログインしていないとき、別セッションへの送信が死なずに迂回すること (e-7234)。

送信側の生存確認は「ネットワークやログイン状態の都合で失敗したら飛ばして先へ進む」
設計で、``lib/commands_shared._resolve_recipient_live`` のコメントが迂回する対象として
**auth を名指し**している。ところが未ログインの失敗は例外ではなく ``sys.exit(1)`` で
来るため、``except Exception`` では受け止められない (``SystemExit`` は ``BaseException``
直系で ``Exception`` の子ではない)。結果、設計意図とは逆に送信そのものが死んでいた。

ここは**挙動**で固定する (文字列一致ではない)。受け止める範囲を ``Exception`` だけに
狭めると、下の 1 本目が ``SystemExit`` を拾って赤くなる。
"""

import sys

import pytest

import commands_shared


class _Boom(Exception):
    pass


def _stub_discovery(monkeypatch, raiser):
    """discover_and_aggregate を差し替える (送信側が実際に呼ぶ継ぎ目)。

    **解決は被検側と同じ ``importlib.import_module`` で行う**。素の
    ``import beacon_cli.skills_helpers.dm_discover`` で取ったオブジェクトに差し替えても、
    被検側が別オブジェクトを解決していると届かない (``beacon_cli`` が repo 側と
    インストール済みの両方から辿れる場合に起きる)。単体では通るのに suite の中では
    stub が無視される、という形で実際に踏んだ。

    周辺の環境変数も落とす。``BEACON_BUS_NO_LIVE_CHECK`` が残っていると生存確認
    そのものが省かれて、この試験が「迂回したから通った」のか「最初から呼ばれて
    いない」のか区別できなくなる。
    """
    import importlib
    for var in ("BEACON_BUS_NO_LIVE_CHECK", "BEACON_BUS_NO_AUTO_SWAP",
                "BEACON_BUS_REFUSE_STALE", "BEACON_DISCOVERY_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    helpers = importlib.import_module("beacon_cli.skills_helpers.dm_discover")
    monkeypatch.setattr(helpers, "discover_and_aggregate", raiser)
    # 差し替えが被検側に届いていることをその場で確認する (届かないまま
    # 「通った」と読むのを防ぐ)。
    assert importlib.import_module(
        "beacon_cli.skills_helpers.dm_discover").discover_and_aggregate is raiser


def test_sys_exit_from_discovery_is_absorbed(monkeypatch):
    """未ログインの ``sys.exit(1)`` が送信を殺さず、生存確認だけが飛ばされること。

    **これが e-7234 の反転テスト**: ``except (Exception, SystemExit)`` を
    ``except Exception`` に戻すと ``SystemExit`` が外へ出て、この関数は
    ``pytest.raises`` でも拾われない形で落ちる = 赤くなる。
    """
    def _exits(**_kw):
        print("Not logged in. Run: beacon auth login", file=sys.stderr)
        sys.exit(1)

    _stub_discovery(monkeypatch, _exits)
    monkeypatch.setattr(commands_shared,
                        "_resolve_recipient_email_via_self_sessions",
                        lambda sid: "")
    notices = []
    out = commands_shared._resolve_recipient_live(
        "sv-bob", "dm", advise=lambda m, **k: notices.append(m))

    assert out == ("sv-bob", None, ""), (
        f"迂回して宛先をそのまま返すはずが {out!r} を返した")
    assert notices, "生存確認を省いたことが利用者に伝わっていない (黙って挙動が変わる)"
    assert "生存確認を省きました" in notices[0], notices


def test_ordinary_exception_from_discovery_stays_silent(monkeypatch):
    """従来からの経路 (network など普通の例外) は迂回し、かつ**無言のまま**であること。

    この経路の無言は設計で、すぐ上の import 失敗の迂回も同じ。一過性・環境起因で
    利用者に打てる手が無く、毎送信で 1 行出れば雑音になる。
    tests/test_bus_send_live_check.py::test_discovery_raises_bypasses が同じ清潔さを
    別角度から固定している。SystemExit を足した拍子にここを騒がしくしていないかの
    逆方向の固定 (最初の実装で実際に破り、その既存テストが赤くなって気付いた)。
    """
    def _raises(**_kw):
        raise _Boom("network down")

    _stub_discovery(monkeypatch, _raises)
    monkeypatch.setattr(commands_shared,
                        "_resolve_recipient_email_via_self_sessions",
                        lambda sid: "")
    notices = []
    out = commands_shared._resolve_recipient_live(
        "sv-bob", "dm", advise=lambda m, **k: notices.append(m))
    assert out == ("sv-bob", None, "")
    assert notices == [], f"普通の例外で騒いでいる: {notices}"


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    """利用者が中断したときは飲み込まないこと。

    受け止める範囲を ``BaseException`` まで広げると Ctrl-C も飲み込んでしまい、
    止めたいのに止まらない形になる。``(Exception, SystemExit)`` に限っている理由を
    ここで固定する。
    """
    def _interrupt(**_kw):
        raise KeyboardInterrupt

    _stub_discovery(monkeypatch, _interrupt)
    with pytest.raises(KeyboardInterrupt):
        commands_shared._resolve_recipient_live(
            "sv-bob", "dm", advise=lambda m, **k: None)


def test_absorbed_bypass_leaves_both_streams_clean(monkeypatch, capsys):
    """迂回したとき、stdout も stderr も汚れず、理由は注記で届くこと (e-7234)。

    ここは**出力先の選択ではなく要件**を固定する。生存確認の先の
    ``_get_api_client`` は未ログイン等で素の ``print`` を出してから終了する。
    終了していた頃は後続の JSON が無いので無害だったが、``SystemExit`` を吸収して
    処理を続けるようにした以上、その出力は ``--json`` の結果に割り込む。
    stderr へ移しても駄目で、Bash ツールが 2 つの流れを結合するため stdout の JSON の
    前に merge されて解析を壊す (壊れた解析は重複送信を誘発する)。
    最初の修正でそこを踏み、tests/test_bus_send_live_check.py の
    「stderr を汚さない」固定が赤くなって気付いた。

    よって正しい形は「どちらの流れにも出さず、注記の受け皿へ回す」。
    """
    def _exits(**_kw):
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    _stub_discovery(monkeypatch, _exits)
    monkeypatch.setattr(commands_shared,
                        "_resolve_recipient_email_via_self_sessions",
                        lambda sid: "")
    notices = []
    out = commands_shared._resolve_recipient_live(
        "sv-bob", "dm", advise=lambda m, **k: notices.append(m))

    assert out == ("sv-bob", None, "")
    cap = capsys.readouterr()
    assert cap.out == "", f"stdout を汚している: {cap.out!r}"
    assert cap.err == "", f"stderr を汚している: {cap.err!r}"
    assert notices, "理由がどこにも届いていない (黙って迂回している)"
    assert "Not logged in" in notices[0], (
        f"飲み込んだ診断が注記に載っていない: {notices[0]!r}")
