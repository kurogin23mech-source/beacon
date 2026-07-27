"""Backend registration + generic primitive wiring for the master store (ms-111 / e-3620).

e-3620 の「マスター store 本体」の backend 配線を pin する。狙いは 2 つ:

1. **登録 parity** (= 3 backend で entity が揃っていること): master_accounts /
   master_contacts が mysql ENTITIES・dynamo TABLES / TABLE_KEY_SCHEMA に top-level
   entity として登録され、project subcollection (delete_project cascade 対象) には
   載っていない。過去 (ms-96) に entity 登録の非対称で「移行行が一覧から silent に
   消える」事故があったため、登録の左右対称を test で固定する。

2. **汎用プリミティブの配線**: mysql の master_get/master_put/master_scan が
   lib/master_store の MasterPersistence 契約を満たし、BeaconDefaultAdapter を
   その上で回すと round-trip する (= backend を差しても adapter の意味論が保たれる)。
   live MySQL は使わず、SQL を解釈するインメモリ fake conn で integration を回す。
"""
from __future__ import annotations

import os
import re
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import master_identity as mi  # noqa: E402
import master_store as ms  # noqa: E402
import mysql_client as mc  # noqa: E402
import dynamodb_client as dc  # noqa: E402

ORG = "org-p-u1"
T1 = "2026-07-27T01:00:00+00:00"


# ---------------------------------------------------------------------------
# 1. 登録 parity
# ---------------------------------------------------------------------------
def test_master_entities_registered_top_level_mysql():
    assert "master_accounts" in mc.ENTITIES
    assert "master_contacts" in mc.ENTITIES
    # top-level: project subcollection (delete_project cascade) には載せない
    assert "master_accounts" not in mc._SUBCOLLECTION_SK_NAMES
    assert "master_contacts" not in mc._SUBCOLLECTION_SK_NAMES
    # TABLES map (create_mysql_tables が走査する) にも現れる
    assert mc.TABLES["master_accounts"].endswith("_master_accounts")


def test_master_entities_registered_top_level_dynamo():
    for ent, key in (("master_accounts", "master_account_id"),
                     ("master_contacts", "master_contact_id")):
        assert ent in dc.TABLES
        # 不変条件: TABLES の entity は TABLE_KEY_SCHEMA にも現れる (PK only)
        assert dc.TABLE_KEY_SCHEMA[ent] == (key, None)


def test_master_entities_not_in_project_cascade():
    # dynamo の subcollection map にも載らない (= project 配下ではない)
    assert "master_accounts" not in dc._SUBCOLLECTION_SK_NAMES
    assert "master_contacts" not in dc._SUBCOLLECTION_SK_NAMES


# ---------------------------------------------------------------------------
# 2. 汎用プリミティブの配線 (mysql fake conn integration)
# ---------------------------------------------------------------------------
class _FakeCursor:
    """master_get/put/scan が emit する SQL 形だけを解釈する最小 fake。"""

    def __init__(self, store, log):
        self.store = store          # {(table, pk, sk): data_json_str}
        self.log = log
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        table = re.search(r"`([^`]+)`", s).group(1)
        if s.startswith("SELECT data FROM") and "WHERE" in s:
            pk = params[0]
            sk = params[1] if len(params) > 1 else ""
            v = self.store.get((table, pk, sk))
            self._result = [{"data": v}] if v is not None else []
        elif s.startswith("SELECT data FROM"):           # no WHERE = _scan
            self._result = [{"data": v} for k, v in self.store.items()
                            if k[0] == table]
        elif s.startswith("INSERT INTO"):
            pk, sk, data = params
            self.store[(table, pk, sk)] = data
            self.log.append(("insert", table, pk))
        return len(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store, log):
        self.store, self.log = store, log

    def cursor(self):
        return _FakeCursor(self.store, self.log)

    def autocommit(self, _v):
        pass

    def commit(self):
        pass


@pytest.fixture
def mysql_persistence(monkeypatch):
    store, log = {}, []
    monkeypatch.setattr(mc, "_conn", lambda: _FakeConn(store, log))

    class _P:
        def get(self, entity, pk):
            return mc.master_get(entity, pk)

        def put(self, entity, pk, data):
            mc.master_put(entity, pk, data)

        def scan(self, entity):
            return mc.master_scan(entity)

    return _P()


def test_beacon_default_over_mysql_roundtrip(mysql_persistence):
    adapter = ms.BeaconDefaultAdapter(mysql_persistence)
    acc = mi.new_master_account("Acme", org_id=ORG, now=T1, master_account_id="macc-1",
                                external_ref={"system": "salesforce", "ref_id": "SF-1"})
    adapter.put_account(acc, now=T1)
    # get / list(org) / external_ref 逆引きが mysql プリミティブ越しに通る
    assert adapter.get_account("macc-1")["name"] == "Acme"
    assert [r["master_account_id"] for r in adapter.list_accounts(ORG)] == ["macc-1"]
    assert adapter.resolve_by_external_ref("salesforce", "SF-1")["master_account_id"] == "macc-1"
    assert adapter.list_accounts("org-other") == []


def test_master_primitives_use_correct_tables(mysql_persistence):
    adapter = ms.BeaconDefaultAdapter(mysql_persistence)
    adapter.put_contact(
        mi.new_master_contact("Taro", org_id=ORG, master_account_id="macc-1",
                              now=T1, master_contact_id="mcon-1"), now=T1)
    got = adapter.get_contact("mcon-1")
    assert got is not None and got["name"] == "Taro"
    # 会社側テーブルには漏れていない (entity 分離)
    assert adapter.get_account("mcon-1") is None
