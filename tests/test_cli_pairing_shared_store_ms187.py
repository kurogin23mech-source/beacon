"""ms-187 e-7488: CLI ログインの合言葉 (ペアリングコード) を全サーバー台で共有する。

起きていたこと: 本番 (Cloud Run, 最大 3 台) で ``beacon auth login`` の承認が
3 回に 2 回ほど「Invalid or expired code」で失敗した。合言葉の表が各台のメモリに
あり、合言葉を発行した台と承認が届いた台が違うと、届いた台は合言葉を知らなかった。

ここで固定すること:

1. **台をまたいで通る**: 発行 (start) / 承認 (approve) / 受け取り (poll) が全部
   別の台に届いても、同じ保存先を見ていればログインが完了する。
2. **安全性は以前と同じ**: 5 分で失効 / 受け取りは 1 回きり / 別の人の承認を
   上書きで乗っ取れない / ブラウザのトークンを保存しない。
3. **本番の配線**: 実アプリの /api/auth/* が ``store_router`` の
   ``master_get`` / ``master_put`` を通る (= メモリに戻っていない)。
4. **全 backend に置き場所がある**: DynamoDB / MySQL にも ``cli_pairing`` が登録
   されていて、DynamoDB が受け付けない float を書かない。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cli_pairing  # noqa: E402
import routers_auth  # noqa: E402


class _SharedStore:
    """全サーバー台が共有する保存先の代役。master_get / master_put の形をそのまま持つ。"""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def master_get(self, entity, pk):
        row = self.rows.get((entity, pk))
        return dict(row) if row is not None else None

    def master_put(self, entity, pk, data):
        self.rows[(entity, pk)] = dict(data)


class _Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


def _instance(store, *, user=None, clock=None, minted=None):
    """1 台分のサーバー (= 自分の router を持つが、保存先は共有)。"""
    user = user or {"sub": "u1", "email": "u1@example.com"}
    table = cli_pairing.PairingTable(
        cli_pairing.StorePersistence(store), clock=clock or _Clock()
    )

    def _mint(sub, email):
        if minted is not None:
            minted.append((sub, email))
        return (f"bcli.{sub}", 4102444800)

    app = FastAPI()
    app.include_router(routers_auth.make_router(
        require_auth=lambda: user,
        make_cli_token=_mint,
        get_local_dev_enabled=lambda: False,
        get_auth_provider=lambda: "firebase",
        pairing_table=table,
    ))
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. 台をまたいで通る
# ---------------------------------------------------------------------------
def test_login_completes_when_every_step_lands_on_a_different_instance():
    store = _SharedStore()
    minted: list = []
    a, b, c = (_instance(store, minted=minted) for _ in range(3))

    code = a.post("/api/auth/cli-start").json()["code"]
    # 承認前の問い合わせは、どの台でも「待ち」になる (= 以前は発行した台以外が 404)
    assert b.get("/api/auth/cli-poll", params={"code": code}).json() == {"status": "pending"}
    assert c.get("/api/auth/cli-poll", params={"code": code}).json() == {"status": "pending"}

    # 承認が発行した台と別の台に届いても通る (= 今回の不具合の本体)
    r = b.post("/api/auth/cli-approve", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "approved", "email": "u1@example.com"}

    # 受け取りがさらに別の台に届いても、承認を見てトークンを渡す
    got = c.get("/api/auth/cli-poll", params={"code": code})
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "approved"
    assert got.json()["id_token"] == "bcli.u1"
    assert minted == [("u1", "u1@example.com")]


def test_code_is_matched_case_insensitively_across_instances():
    store = _SharedStore()
    a, b = _instance(store), _instance(store)
    code = a.post("/api/auth/cli-start").json()["code"]
    assert b.post("/api/auth/cli-approve", json={"code": code.lower()}).status_code == 200
    assert a.get("/api/auth/cli-poll", params={"code": code.lower()}).json()["status"] == "approved"


# ---------------------------------------------------------------------------
# 2. 安全性
# ---------------------------------------------------------------------------
def test_poll_is_one_time_across_instances():
    store = _SharedStore()
    a, b = _instance(store), _instance(store)
    code = a.post("/api/auth/cli-start").json()["code"]
    a.post("/api/auth/cli-approve", json={"code": code})
    assert a.get("/api/auth/cli-poll", params={"code": code}).json()["status"] == "approved"
    # 別の台で同じ合言葉を使い回しても、二度目は渡さない
    replay = b.get("/api/auth/cli-poll", params={"code": code})
    assert replay.status_code == 404, replay.text
    # 受け取り済みの合言葉は、もう承認もできない
    assert b.post("/api/auth/cli-approve", json={"code": code}).status_code == 404


def test_code_expires_after_ttl_on_every_instance():
    store = _SharedStore()
    clock = _Clock()
    a, b = _instance(store, clock=clock), _instance(store, clock=clock)
    code = a.post("/api/auth/cli-start").json()["code"]
    clock.t += cli_pairing.TTL_SECONDS + 1
    assert b.post("/api/auth/cli-approve", json={"code": code}).status_code == 410
    assert b.get("/api/auth/cli-poll", params={"code": code}).status_code == 410


def test_unknown_code_is_404():
    store = _SharedStore()
    a = _instance(store)
    assert a.get("/api/auth/cli-poll", params={"code": "NOPE1234"}).status_code == 404
    assert a.get("/api/auth/cli-poll", params={"code": ""}).status_code == 404
    assert a.post("/api/auth/cli-approve", json={"code": "NOPE1234"}).status_code == 404


def test_another_account_cannot_take_over_an_approved_code():
    store = _SharedStore()
    owner = _instance(store, user={"sub": "u1", "email": "u1@example.com"})
    intruder = _instance(store, user={"sub": "u2", "email": "u2@example.com"})
    code = owner.post("/api/auth/cli-start").json()["code"]
    assert owner.post("/api/auth/cli-approve", json={"code": code}).status_code == 200
    r = intruder.post("/api/auth/cli-approve", json={"code": code})
    assert r.status_code == 409, r.text
    # 押し直し (同じ人) は成功のまま。人が Authorize を何度押しても困らない
    assert owner.post("/api/auth/cli-approve", json={"code": code}).status_code == 200
    # 受け取れるのは最初に承認した人のトークン
    assert owner.get("/api/auth/cli-poll", params={"code": code}).json()["id_token"] == "bcli.u1"


def test_approve_without_sub_is_rejected():
    store = _SharedStore()
    a = _instance(store, user={"email": "nosub@example.com"})
    code = a.post("/api/auth/cli-start").json()["code"]
    assert a.post("/api/auth/cli-approve", json={"code": code}).status_code == 400


def test_stored_record_keeps_no_browser_token_and_only_integer_numbers():
    """保存するのは誰が承認したかだけ。数値は整数 (DynamoDB は float を受け付けない)。"""
    store = _SharedStore()
    a = _instance(store)
    code = a.post("/api/auth/cli-start").json()["code"]
    a.post("/api/auth/cli-approve", json={"code": code},
           headers={"Authorization": "Bearer browser-id-token-must-not-be-stored"})
    row = store.rows[(cli_pairing.ENTITY, code)]
    assert "id_token" not in row
    assert "browser-id-token-must-not-be-stored" not in repr(row)
    assert row["status"] == "approved" and row["sub"] == "u1"
    for k, v in row.items():
        assert not isinstance(v, float), (k, v)


def test_code_is_consumed_before_the_token_is_minted():
    """トークン発行で落ちても、合言葉は受け取り済みのまま (= 失敗時に二重発行へ倒れない)。"""
    store = _SharedStore()
    table = cli_pairing.PairingTable(cli_pairing.StorePersistence(store))
    code = table.start()
    table.approve(code, sub="u1", email="u1@example.com")
    assert table.poll(code)["status"] == "approved"
    assert store.rows[(cli_pairing.ENTITY, code)]["status"] == cli_pairing.STATUS_CONSUMED


def test_start_does_not_reuse_a_live_code():
    store = _SharedStore()
    codes = iter(["SAME0000", "SAME0000", "OTHER000"])
    table = cli_pairing.PairingTable(cli_pairing.StorePersistence(store),
                                     new_code=lambda: next(codes))
    assert table.start() == "SAME0000"
    assert table.start() == "OTHER000"  # 生きている合言葉は上書きしない


# ---------------------------------------------------------------------------
# 3. 本番の配線
# ---------------------------------------------------------------------------
def test_make_router_refuses_a_missing_or_wrong_pairing_table():
    with pytest.raises(TypeError):
        routers_auth.make_router(
            require_auth=lambda: {"sub": "x"},
            make_cli_token=lambda s, e: ("t", 0),
            get_local_dev_enabled=lambda: False,
            get_auth_provider=lambda: "firebase",
            pairing_table={},  # 台ごとのメモリ表へ戻す配線は、組み立て時に落とす
        )
    with pytest.raises(TypeError):
        routers_auth.make_router(  # type: ignore[call-arg]
            require_auth=lambda: {"sub": "x"},
            make_cli_token=lambda s, e: ("t", 0),
            get_local_dev_enabled=lambda: False,
            get_auth_provider=lambda: "firebase",
        )


def test_store_persistence_requires_master_primitives():
    with pytest.raises(TypeError):
        cli_pairing.StorePersistence(object())


def test_real_app_routes_go_through_the_shared_store(monkeypatch):
    """実アプリの /api/auth/cli-* が store_router の master_get / master_put を通る。"""
    import app as app_module
    import store_router

    store = _SharedStore()
    monkeypatch.setattr(store_router, "master_get", store.master_get, raising=False)
    monkeypatch.setattr(store_router, "master_put", store.master_put, raising=False)
    monkeypatch.setattr(app_module, "_auth_enabled", False)
    client = TestClient(app_module.app)

    code = client.post("/api/auth/cli-start").json()["code"]
    assert (cli_pairing.ENTITY, code) in store.rows, store.rows
    assert client.get("/api/auth/cli-poll", params={"code": code}).json() == {"status": "pending"}


# ---------------------------------------------------------------------------
# 4. 全 backend に置き場所がある
# ---------------------------------------------------------------------------
def test_cli_pairing_is_registered_on_every_backend():
    import dynamodb_client as dc
    import mysql_client as mc

    assert cli_pairing.ENTITY in mc.ENTITIES
    assert mc.TABLES[cli_pairing.ENTITY].endswith("_cli_pairing")
    assert cli_pairing.ENTITY not in mc._SUBCOLLECTION_SK_NAMES

    assert cli_pairing.ENTITY in dc.TABLES
    assert dc.TABLE_KEY_SCHEMA[cli_pairing.ENTITY] == ("code", None)
    assert dc._MASTER_KEY_ATTR[cli_pairing.ENTITY] == "code"
    assert cli_pairing.ENTITY not in dc._SUBCOLLECTION_SK_NAMES
