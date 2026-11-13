"""ms-187 e-7490: beacon を使い続けている間は、CLI のログインが自動で延長される。

以前は CLI トークン (bcli、30 日) に延長の仕組みが無く、30 日ごとに黙って切れて
いた。ここで固定すること:

- 期限まで 7 日を切ったトークンは、load_credentials のときに新しいものへ差し替わり、
  credentials.json に保存される。
- 期限が十分先なら、サーバーに問い合わせない (毎回の操作を遅くしない)。
- 延長に失敗しても、手元のトークンで続ける。失敗後 1 時間は再挑戦しない。
- 期限切れのトークンは今と同じく None (= beacon auth login の案内)。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import auth  # noqa: E402

DAY = 86400


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    cred = tmp_path / "credentials.json"
    calls: list = []
    state = {"reply": None, "raise": None}

    def _urlopen(req, timeout=None, context=None):
        calls.append({"url": req.full_url, "method": req.get_method(),
                      "auth": req.headers.get("Authorization")})
        if state["raise"]:
            raise state["raise"]
        return _Resp(json.dumps(state["reply"]).encode())

    monkeypatch.setattr(auth, "_credentials_path", lambda: cred)
    monkeypatch.setattr(auth, "_get_api_url", lambda: "https://api.example.test")
    monkeypatch.setattr(auth.urllib.request, "urlopen", _urlopen)

    def write(**kw):
        data = {"token": "bcli.old.sig", "email": "u1@example.com", "web_auth": True,
                "token_type": "beacon_cli", **kw}
        cred.write_text(json.dumps(data), encoding="utf-8")

    def saved():
        return json.loads(cred.read_text(encoding="utf-8"))

    return {"write": write, "saved": saved, "calls": calls, "state": state}


def test_token_far_from_expiry_is_used_as_is_without_asking_the_server(env):
    env["write"](token_expiry=int(time.time()) + 20 * DAY)
    creds = auth.load_credentials()
    assert creds["token"] == "bcli.old.sig"
    assert env["calls"] == []


def test_token_close_to_expiry_is_renewed_and_saved(env):
    now = int(time.time())
    env["write"](token_expiry=now + 3 * DAY)
    env["state"]["reply"] = {"status": "ok", "id_token": "bcli.new.sig",
                             "email": "u1@example.com", "token_expiry": now + 30 * DAY}
    creds = auth.load_credentials()
    assert creds["token"] == "bcli.new.sig"
    assert creds["token_expiry"] == now + 30 * DAY
    # サーバーには「今のトークン」で本人確認して頼む
    assert env["calls"] == [{"url": "https://api.example.test/api/auth/exchange-cli-token",
                             "method": "POST", "auth": "Bearer bcli.old.sig"}]
    saved = env["saved"]()
    assert saved["token"] == "bcli.new.sig" and "renew_attempted_at" not in saved
    # 差し替え後は期限が十分先なので、次の操作では問い合わせない
    auth.load_credentials()
    assert len(env["calls"]) == 1


def test_renewal_failure_keeps_the_current_token_and_backs_off(env):
    now = int(time.time())
    env["write"](token_expiry=now + 2 * DAY)
    env["state"]["raise"] = OSError("server down")
    creds = auth.load_credentials()
    assert creds is not None and creds["token"] == "bcli.old.sig"  # 操作は止めない
    assert len(env["calls"]) == 1
    # 1 時間以内は再挑戦しない
    auth.load_credentials()
    assert len(env["calls"]) == 1
    assert env["saved"]()["renew_attempted_at"] >= now


def test_renewal_retries_after_the_backoff(env):
    now = int(time.time())
    env["write"](token_expiry=now + 2 * DAY, renew_attempted_at=now - 2 * 3600)
    env["state"]["reply"] = {"id_token": "bcli.new.sig", "token_expiry": now + 30 * DAY}
    assert auth.load_credentials()["token"] == "bcli.new.sig"


def test_a_reply_that_is_not_a_fresher_cli_token_is_ignored(env):
    now = int(time.time())
    env["write"](token_expiry=now + 2 * DAY)
    env["state"]["reply"] = {"id_token": "not-a-cli-token", "token_expiry": now + 30 * DAY}
    assert auth.load_credentials()["token"] == "bcli.old.sig"
    env["write"](token_expiry=now + 2 * DAY)
    env["state"]["reply"] = {"id_token": "bcli.older.sig", "token_expiry": now + DAY}
    assert auth.load_credentials()["token"] == "bcli.old.sig"


def test_expired_token_still_asks_for_login(env, capsys):
    env["write"](token_expiry=int(time.time()) - 10)
    assert auth.load_credentials() is None
    assert "beacon auth login" in capsys.readouterr().out
    assert env["calls"] == []


def test_non_cli_tokens_are_not_touched(env):
    now = int(time.time())
    env["write"](token_type="cognito_id_token", token_expiry=now + 3600)
    assert auth.load_credentials()["token"] == "bcli.old.sig"
    assert env["calls"] == []
