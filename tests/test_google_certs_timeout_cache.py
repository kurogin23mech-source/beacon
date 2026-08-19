"""Tests for the Google cert fetch timeout + cache path (ms-145 / e-5316).

本番インシデント: ``id_token.verify_oauth2_token`` が認証のたびに googleapis の
証明書を無限待ちで取りに行き、TLS 不調で数時間ハングして本番プロセスが飢えた。
ここでは証明書取得が (1) 必ず有界タイムアウトを課すこと、(2) TTL 内はキャッシュ
で往復を消すこと、(3) fetch 失敗でも有効キャッシュがあれば stale-serve すること、
(4) キャッシュ無しの fetch 失敗は 503 で即座に閉じる (ハングしない) ことを検証する。

Strategy:
- ``google.auth.transport.requests.Request`` を、呼ばれた timeout を記録する fake に
  差し替える。実ネットワークには一切出ない。
- ``google.auth.jwt.decode`` を patch して署名検証をスキップし、claims を返す。
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


class _FakeResponse:
    def __init__(self, status=200, data=b'{"kid1": "cert1"}'):
        self.status = status
        self.data = data


class _FakeRequest:
    """Records how it was called; never touches the network."""

    calls: list[dict] = []
    responses: list = []  # queue of _FakeResponse or Exception to raise

    def __init__(self, *a, **kw):
        pass

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kw):
        _FakeRequest.calls.append({"url": url, "timeout": timeout})
        nxt = _FakeRequest.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def app_module(monkeypatch):
    """Reload app with the default (non-cognito) Google verification path."""
    pytest.importorskip("fastapi")
    pytest.importorskip("google.auth")
    monkeypatch.delenv("BEACON_AUTH_PROVIDER", raising=False)
    monkeypatch.setenv("BEACON_GOOGLE_CERTS_TIMEOUT_S", "5")
    monkeypatch.setenv("BEACON_GOOGLE_CERTS_TTL_S", "3600")
    if "app" in sys.modules:
        del sys.modules["app"]
    mod = importlib.import_module("app")
    # patch the transport Request the fetch helper imports
    import google.auth.transport.requests as gtr

    monkeypatch.setattr(gtr, "Request", _FakeRequest)
    # reset cache + fake state
    mod._google_certs_cache["certs"] = None
    mod._google_certs_cache["fetched_at"] = 0.0
    _FakeRequest.calls = []
    _FakeRequest.responses = []
    return mod


def test_fetch_passes_bounded_timeout(app_module):
    """証明書取得には必ず有界タイムアウトが渡る (ライブラリ既定に頼らない)。"""
    _FakeRequest.responses = [_FakeResponse()]
    certs = app_module._fetch_google_certs()
    assert certs == {"kid1": "cert1"}
    assert len(_FakeRequest.calls) == 1
    assert _FakeRequest.calls[0]["timeout"] == 5.0


def test_certs_are_cached_within_ttl(app_module):
    """TTL 内の 2 回目はネットワーク往復を省く (hot path から fetch を消す)。"""
    _FakeRequest.responses = [_FakeResponse()]
    app_module._fetch_google_certs()
    # 2 回目: responses キューは空 = fetch が走ればここで IndexError になる
    certs2 = app_module._fetch_google_certs()
    assert certs2 == {"kid1": "cert1"}
    assert len(_FakeRequest.calls) == 1  # まだ 1 回だけ


def test_stale_serve_on_fetch_failure(app_module):
    """一度取得済みなら、以後の fetch 失敗はキャッシュで可用性を保つ。"""
    _FakeRequest.responses = [_FakeResponse()]
    app_module._fetch_google_certs()
    # TTL を無視して force refetch させ、失敗させる
    _FakeRequest.responses = [RuntimeError("TLS hang")]
    certs = app_module._fetch_google_certs(force=True)
    assert certs == {"kid1": "cert1"}  # 古いキャッシュを stale-serve


def test_cold_fetch_failure_raises_503_not_hang(app_module):
    """キャッシュ無しの fetch 失敗は 503 で即座に閉じる (ハングしない)。"""
    from fastapi import HTTPException

    _FakeRequest.responses = [RuntimeError("connection refused")]
    with pytest.raises(HTTPException) as ei:
        app_module._fetch_google_certs()
    assert ei.value.status_code == 503


def test_non_200_without_cache_raises_503(app_module):
    from fastapi import HTTPException

    _FakeRequest.responses = [_FakeResponse(status=500, data=b"err")]
    with pytest.raises(HTTPException) as ei:
        app_module._fetch_google_certs()
    assert ei.value.status_code == 503


def test_verify_google_id_token_uses_cached_certs(app_module, monkeypatch):
    """検証本体はキャッシュ証明書を使い、issuer チェックまで通す。"""
    import google.auth.jwt as gjwt

    _FakeRequest.responses = [_FakeResponse()]
    decode_calls = {"n": 0}

    def fake_decode(token, certs=None, audience=None, **kw):
        decode_calls["n"] += 1
        assert certs == {"kid1": "cert1"}
        return {"iss": "accounts.google.com", "email": "u@example.com"}

    monkeypatch.setattr(gjwt, "decode", fake_decode)
    claims = app_module._verify_google_id_token("tok")
    assert claims["email"] == "u@example.com"
    assert decode_calls["n"] == 1


def test_verify_wrong_issuer_rejected(app_module, monkeypatch):
    from fastapi import HTTPException
    import google.auth.jwt as gjwt

    _FakeRequest.responses = [_FakeResponse()]
    monkeypatch.setattr(
        gjwt, "decode", lambda *a, **k: {"iss": "evil.example.com"}
    )
    with pytest.raises(HTTPException) as ei:
        app_module._verify_google_id_token("tok")
    assert ei.value.status_code == 401


def test_verify_retries_once_on_key_rotation(app_module, monkeypatch):
    """kid 不一致 (鍵ローテ) は証明書を強制更新して 1 回だけ再試行する。"""
    import google.auth.jwt as gjwt

    # 1回目 fetch (通常) + 2回目 fetch (force refetch)
    _FakeRequest.responses = [_FakeResponse(), _FakeResponse(data=b'{"kid2": "cert2"}')]
    state = {"n": 0}

    def flaky_decode(token, certs=None, audience=None, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("Certificate for key id kid2 not found")
        return {"iss": "https://accounts.google.com"}

    monkeypatch.setattr(gjwt, "decode", flaky_decode)
    claims = app_module._verify_google_id_token("tok")
    assert claims["iss"] == "https://accounts.google.com"
    assert state["n"] == 2  # 1 回だけ再試行
    assert len(_FakeRequest.calls) == 2  # 通常 fetch + force refetch


def test_verify_no_refetch_on_non_kid_error(app_module, monkeypatch):
    """kid 不一致でない ValueError (期限切れ等) は refetch せず即 401。

    無差別 refetch (= 不正トークンで余計な googleapis 往復) を防ぐ回帰テスト。
    """
    from fastapi import HTTPException
    import google.auth.jwt as gjwt

    _FakeRequest.responses = [_FakeResponse()]

    def expired_decode(token, certs=None, audience=None, **kw):
        raise ValueError("Token expired, as of 2020-01-01")

    monkeypatch.setattr(gjwt, "decode", expired_decode)
    with pytest.raises(HTTPException) as ei:
        app_module._verify_google_id_token("tok")
    assert ei.value.status_code == 401
    # force-refetch されていない = 証明書取得は 1 回だけ (kid 不一致でないため)
    assert len(_FakeRequest.calls) == 1


def test_is_kid_mismatch_error_predicate(app_module):
    """retry 条件の純粋判定を外部往復なしで直接テストする。"""
    assert app_module._is_kid_mismatch_error(ValueError("Certificate for key id X not found"))
    assert app_module._is_kid_mismatch_error(ValueError("no matching kid"))
    assert not app_module._is_kid_mismatch_error(ValueError("Token expired"))
    assert not app_module._is_kid_mismatch_error(ValueError("Invalid signature"))
