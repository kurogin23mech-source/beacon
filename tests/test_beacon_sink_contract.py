"""Contract 疎通: 参照 beacon_sink → 実 machine 直書き endpoint (ms-151 / e-5478).

docs/integrations/beacon_sink.py の BeaconSink を、実 app の TestClient に差し替え
transport で繋ぎ、PE detector Lambda が差し替えるのと同じ経路で run_record / incident
を書けることを end-to-end (in-memory store) で確認する。実機 (実 PE Lambda → 本番 cloud)
の疎通は PE 側で行う (docs/integrations/machine-write-contract.md §5)。

これは「beacon_sink 差し替えで書ける」ことのコードレベル疎通 = e-5478 の AC を満たす。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "docs", "integrations"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import machine_key as mk  # noqa: E402
from beacon_sink import BeaconSink, BeaconWriteError  # noqa: E402

_client = TestClient(app_module.app)
PID = "beacon-b95643"
NOW = "2026-08-24T12:00:00Z"


def _fresh_project():
    return {
        "name": "T", "milestones": [], "owner": "u1", "members": [],
        "schema_version": 1,
        "operations": [
            {"id": "op-5", "title": "Detectors", "status": "open", "entries": []}
        ],
    }


@pytest.fixture
def sink_env(monkeypatch):
    store = {PID: _fresh_project()}
    for mod in (firestore_client, app_module.db):
        monkeypatch.setattr(mod, "get_project", lambda pid: copy.deepcopy(store.get(pid)))
        monkeypatch.setattr(mod, "save_project",
                            lambda pid, data: store.__setitem__(pid, copy.deepcopy(data)))
    raw, record = mk.issue(PID, label="PE detector", created_by="u1", now=NOW)
    monkeypatch.setattr(
        app_module.db, "get_machine_key",
        lambda pid, kid: record if (pid == PID and kid == record["key_id"]) else None)
    monkeypatch.setattr(app_module, "_auth_enabled", True)

    # TestClient を BeaconSink の http_post に差し替える (実機の urllib の代わり)。
    def testclient_post(url, headers, body):
        resp = _client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text

    # base_url="" なので url == path で TestClient にそのまま渡せる。
    sink = BeaconSink("", raw, PID, http_post=testclient_post)
    return {"store": store, "sink": sink}


def test_sink_writes_run_record(sink_env):
    entry = sink_env["sink"].write_run_record(
        "op-5", batch="nightly", status="ok", description="all green")
    assert entry["type"] == "run_record"
    assert entry["status"] == "ok"
    op = sink_env["store"][PID]["operations"][0]
    assert any(e["type"] == "run_record" and e["batch"] == "nightly"
               for e in op["entries"])


def test_sink_writes_incident_with_opened_at(sink_env):
    entry = sink_env["sink"].write_incident(
        "op-5", title="latency spike", priority="high",
        opened_at="2026-08-24T09:00:00Z")
    assert entry["type"] == "incident"
    assert entry["opened_at"] == "2026-08-24T09:00:00Z"


def test_sink_raises_on_unknown_op(sink_env):
    with pytest.raises(BeaconWriteError) as ei:
        sink_env["sink"].write_run_record("op-NONE", batch="b", status="ok")
    assert ei.value.status_code == 404


def test_sink_raises_on_revoked_key(monkeypatch, sink_env):
    # 鍵を失効させると sink の書き込みは 401 で弾かれる (= 契約の失効挙動)。
    monkeypatch.setattr(app_module.db, "get_machine_key", lambda pid, kid: None)
    with pytest.raises(BeaconWriteError) as ei:
        sink_env["sink"].write_run_record("op-5", batch="b", status="ok")
    assert ei.value.status_code == 401
