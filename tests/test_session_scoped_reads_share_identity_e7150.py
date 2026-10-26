"""セッション単位で絞る全経路が、同じ 1 つの身元を見ていること (ms-178 / e-7150).

e-7150 の受入条件 6 は「``beacon decision list --session`` が、同一セッションで
記録した判断を実際に返すこと」を要求している。これは note とは別の機能だが
**同じ原因で壊れていた**: 判断の ``who.session_id`` はサーバが
``X-Beacon-Session`` ヘッダから取り、そのヘッダは ``api_client`` が
``resolve_active_session_id()`` で詰める (``lib/api_client.py``)。``note add`` /
``note clear`` も ``commands_shared._resolve_session_id()`` 経由で同じ関数に行く。
だから身元が呼び出しごとに変われば、書いた側と絞る側が別 id になり、
``--session`` は 0 件しか返さない (= ``/beacon-session-end`` の「このセッションで
下した判断」チェックが構造的に機能しない)。

このモジュールは「1 つの身元を共有している」という不変条件そのものを固定する。
別経路が独自に身元を組み立て始めたら、ここが赤くなる — 片方だけ直して
「直った」と言えなくするための見張り。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import commands_shared  # noqa: E402
import session as S  # noqa: E402

SID = "852017d9-bdb9-4b72-b432-b66d2e06cc79"


def test_cli_resolver_delegates_to_the_session_resolver(monkeypatch):
    """``_resolve_session_id`` は自前で組み立てず、解決器に委ねている。"""
    monkeypatch.setattr(S, "resolve_active_session_id", lambda: "sv-SENTINEL")
    assert commands_shared._resolve_session_id() == "sv-SENTINEL"


def test_api_header_carries_the_resolved_identity(monkeypatch):
    """API ヘッダ (= 判断記録の who.session_id の出所) に解決済みの身元が乗る。

    サーバは ``X-Beacon-Session`` を ``who.session_id`` にそのまま書く
    (``server/routers_projects.py``)。なので ``--session`` が引ける前提は
    「このヘッダが note と同じ身元を運ぶ」こと。

    文字列検索ではなく **実際にリクエストを組ませて** ヘッダを読む。
    ``resolve_active_session_id`` という名前が残っているかを grep するだけだと、
    呼んでいるのに値が落ちている / 別の値で上書きされている形を見逃す
    (緩い一致で偽の緑を作るのは本リポジトリで繰り返した失敗)。
    """
    import urllib.request

    import api_client

    monkeypatch.delenv("BEACON_SESSION_ID", raising=False)
    monkeypatch.setattr(S, "resolve_active_session_id", lambda: "sv-SENTINEL")

    captured: dict = {}

    class _FakeRequest:
        def __init__(self, url, data=None, method=None):
            self.url, self.data, self.method = url, data, method

        def add_header(self, name, value):
            captured[name] = value

    monkeypatch.setattr(urllib.request, "Request", _FakeRequest)
    # ネットワークに出る前で止める。ヘッダは urlopen より前に詰め終わっている。
    sentinel = RuntimeError("stop before network")

    def _boom(*a, **k):
        raise sentinel

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    client = api_client.ApiClient("https://api.test", lambda: "tok")
    monkeypatch.setattr(client, "_guard_read", lambda: None)
    monkeypatch.setattr(client, "_guard_write", lambda *a, **k: None)

    try:
        client._request("GET", "/anything")
    except Exception as exc:  # noqa: BLE001 - 到達点の確認が目的
        assert exc is sentinel or isinstance(exc, Exception)

    assert captured.get("X-Beacon-Session") == "sv-SENTINEL", captured


def test_identity_is_stable_so_write_and_filter_agree(monkeypatch):
    """書いた時と絞る時で、同じ身元に解決される。

    これが AC1 (自分のメモを自分で消せる) と AC6 (自分の判断を --session で
    引ける) の共通の前提。身元を 2 回解決して一致することを見る。
    """
    monkeypatch.delenv(S.ENV_PARENT_PID, raising=False)
    monkeypatch.setenv(S.ENV_SESSION_ID, SID)
    # bridge claim / codex pointer を介在させず、mint 鎖の入口 (= 壊れていた
    # 経路) だけを見る。
    monkeypatch.setattr(S, "read_bridge_session", lambda: {})
    monkeypatch.setattr(S, "read_codex_session_pointer", lambda: "")

    at_write = S._resolve_parent_pid()
    at_filter = S._resolve_parent_pid()
    assert at_write == at_filter != 0
