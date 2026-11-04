"""ログインの合言葉の問い合わせが、承認を待てること (ms-160 e-7305).

## なぜこの試験が在るか

2026-11-04、`beacon auth login` が **承認する間もなく** 「Invalid code.」で落ちた。
3 回連続で再現し、利用者の操作では回避できなかった。ログインは他の全操作の前提
なので、ここで死ぬと beacon の読み書きが全部止まる (実際にそうなり、セッションの
引き継ぎが一時書けなくなった)。

機構: サーバの合言葉の表 (`server/routers_auth.py` の `_cli_pending`) は
プロセス内メモリで、しかも `make_router` の中で作られる **router インスタンスごとの
ローカル辞書**。合言葉を発行した側と、問い合わせに答えた側が違えば、答えた側は
合言葉を知らないので 404 を返す。クライアントはそれを致命的として即終了していた。

**404 は「この合言葉は無効」ではなく「答えた側が知らない」でも起きる。** 問い合わせは
3 秒間隔なので、数回のうちに必ず別インスタンスへ振られ、ブラウザでサインインを
終える前に死ぬ。だから「404 で終了しない」ことを固定する。

根本はサーバ側で合言葉の表を共有ストアに置くこと (e-7305 の受入条件)。この試験は
**それが入るまで利用者がログインできる状態を保つ受け皿**を守る。
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import types
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import auth  # noqa: E402


class _Resp(io.BytesIO):
    """``urlopen`` の戻り値の最小形 (context manager + read)。"""

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *_a):  # noqa: D105
        return False


def _drive(monkeypatch, tmp_path, poll_script):
    """``login_web()`` を、問い合わせの返りを台本で決めて走らせる。

    ``poll_script`` は ``"404"`` / ``"410"`` / ``"pending"`` / ``"approved"`` の列。
    足りなくなったら最後の要素を繰り返す (= その状態が続く)。

    戻り値は ``(結果, 出力, 問い合わせ回数, 資格情報が書かれたか)``。
    結果は ``"returned"`` か ``"exit <code>"``。
    """
    polls = {"n": 0}
    creds = tmp_path / "credentials.json"

    def fake_urlopen(req, timeout=None, context=None):
        url = req if isinstance(req, str) else req.full_url
        if "cli-start" in url:
            return _Resp(json.dumps(
                {"code": "TESTCODE", "url": "https://example.invalid/x"}).encode())
        if "cli-poll" in url:
            i = polls["n"]
            polls["n"] += 1
            kind = poll_script[i] if i < len(poll_script) else poll_script[-1]
            if kind in ("404", "410"):
                raise urllib.error.HTTPError(url, int(kind), "err", {}, None)
            if kind == "pending":
                return _Resp(json.dumps({"status": "pending"}).encode())
            return _Resp(json.dumps({
                "status": "approved", "email": "x@example.com",
                "id_token": "bcli.fake", "token_expiry": 9999999999}).encode())
        raise AssertionError(f"unexpected url: {url}")

    # 時間はこちらが進める (本物の sleep を待たない)。1 回の sleep で 40 秒進める
    # ので、締め切り (300 秒) までに 8 回ほど問い合わせる計算になる。
    clock = {"t": 1000.0}
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(auth, "time", types.SimpleNamespace(
        time=lambda: clock["t"],
        sleep=lambda s: clock.__setitem__("t", clock["t"] + 40.0)))
    monkeypatch.setattr(auth, "_credentials_path", lambda: creds)
    monkeypatch.setattr(auth, "webbrowser",
                        types.SimpleNamespace(open=lambda _u: None))
    monkeypatch.setattr(auth, "_get_api_url", lambda: "https://example.invalid")

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    outcome = "returned"
    try:
        auth.login_web()
    except SystemExit as exc:
        outcome = f"exit {exc.code}"
    monkeypatch.undo()
    return outcome, buf.getvalue(), polls["n"], creds.exists()


def test_a_404_does_not_end_the_login(monkeypatch, tmp_path):
    """**これが本体**: 合言葉を知らないインスタンスに当たっても終了しない。

    以前はここで `Invalid code.` を出して即 `exit 1` していた。問い合わせは 3 秒
    間隔なので、ブラウザでサインインを終える前に死ぬ —— 利用者の操作では回避
    できない。2026-11-04 に 3 回連続で再現した経路そのもの。
    """
    outcome, out, polls, creds = _drive(
        monkeypatch, tmp_path, ["404", "404", "pending", "404", "approved"])
    assert outcome == "returned", f"404 で終了した: {outcome}\n{out}"
    assert polls >= 5, f"問い合わせを続けていない (polls={polls})"
    assert creds, "承認に到達したのに資格情報が書かれていない"
    assert "Invalid code" not in out
    assert "approved!" in out and "Logged in as" in out


def test_a_code_no_instance_ever_knew_fails_with_the_reason(monkeypatch, tmp_path):
    """どのインスタンスも一度も知らないままなら、理由を言って落ちる。

    黙ってタイムアウトさせない —— 原因が分からないまま 5 分待たされるのが
    一番たちが悪い。「本当に無効」と「相手が知らなかった」を区別して伝える。
    """
    outcome, out, _polls, creds = _drive(monkeypatch, tmp_path, ["404"])
    assert outcome == "exit 1", outcome
    assert "never recognised it" in out, out
    assert not creds


def test_waiting_for_a_human_is_reported_as_a_timeout(monkeypatch, tmp_path):
    """知っている側は居たが承認が来ない = 人が承認していない。別の原因なので別の文面。

    2 つを同じ沈黙に畳むと、利用者は「サーバがおかしい」のか「自分が承認していない」
    のかを切り分けられない。
    """
    outcome, out, _polls, creds = _drive(monkeypatch, tmp_path, ["pending"])
    assert outcome == "exit 1", outcome
    assert "Timeout" in out, out
    assert "never recognised" not in out
    assert not creds


def test_a_genuinely_expired_code_still_stops_at_once(monkeypatch, tmp_path):
    """410 (期限切れ) は従来どおり即終了。待っても変わらないので待たせない。"""
    outcome, out, polls, creds = _drive(monkeypatch, tmp_path, ["410"])
    assert outcome == "exit 1", outcome
    assert "Code expired" in out, out
    assert polls == 1, f"期限切れで待ち続けている (polls={polls})"
    assert not creds


@pytest.mark.parametrize("script,want_contains", [
    (["approved"], "approved!"),
    (["pending", "approved"], "approved!"),
])
def test_the_happy_paths_are_unchanged(monkeypatch, tmp_path, script, want_contains):
    """404 を挟まない従来の経路は、挙動を変えていないこと。"""
    outcome, out, _polls, creds = _drive(monkeypatch, tmp_path, script)
    assert outcome == "returned", f"{outcome}\n{out}"
    assert want_contains in out
    assert creds
