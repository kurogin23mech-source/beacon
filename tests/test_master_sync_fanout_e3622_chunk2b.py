"""master name 変更の inbound fan-out (ms-111 / e-3622 chunk2b).

master が更新された時、server が『同 org でその master を参照する projection doc』だけに
name を write-back することを固定する (leader refine で write amplification を bound)。
_fanout_master_name_to_projections を db モック越しに直接叩き、org filter / 参照 filter /
idempotent / write scope を検証する (full POST でなく helper を直接 = 軽量)。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import firestore_client  # noqa: E402

# app import が startup で触りうる最小限を無害化。
firestore_client.get_project = lambda pid: None
firestore_client.list_projects = lambda *a, **k: []
firestore_client.save_project = lambda pid, data: None

import app as app_module  # noqa: E402
import store_router as db_module  # noqa: E402
import master_projection as mp  # noqa: E402

ORG_A = "org-A"
ORG_B = "org-B"


def _proj(org, acc_name, master_id="macc-1"):
    acc = {"id": "a1", "name": acc_name, "label": acc_name, "contacts": []}
    mp.link_account_to_master(acc, master_id)
    return {"org_id": org, "owner": "uid-x", "accounts": [acc]}


@pytest.fixture
def store(monkeypatch):
    projects = {
        "P1": _proj(ORG_A, "Old"),          # 同 org・参照あり → 更新対象
        "P2": _proj(ORG_A, "Old"),          # 同 org・参照あり → 更新対象
        "P3": _proj(ORG_B, "Other"),        # 別 org → skip
        "P4": {"org_id": ORG_A, "owner": "uid-x", "accounts": [   # 同 org だが未参照
            {"id": "a9", "name": "Unrelated", "label": "Unrelated"}]},
    }
    saved = []

    def _list_projects(*a, **k):
        return [{"project_id": pid} for pid in projects]

    def _get_project(pid):
        return copy.deepcopy(projects.get(pid))

    def _save_project(pid, data):
        projects[pid] = copy.deepcopy(data)
        saved.append(pid)

    for name, ref in (("list_projects", _list_projects),
                      ("get_project", _get_project),
                      ("save_project", _save_project)):
        monkeypatch.setattr(db_module, name, ref, raising=False)
    return projects, saved


def test_fanout_writes_only_same_org_referencing_projects(store):
    projects, saved = store
    written = app_module._fanout_master_name_to_projections(
        master_account_id="macc-1", new_name="New", master_org=ORG_A)
    assert written == 2                       # P1, P2 のみ
    assert sorted(saved) == ["P1", "P2"]
    assert projects["P1"]["accounts"][0]["name"] == "New"
    assert projects["P2"]["accounts"][0]["label"] == "New"
    assert projects["P3"]["accounts"][0]["name"] == "Other"   # 別 org → 不変
    assert projects["P4"]["accounts"][0]["name"] == "Unrelated"  # 未参照 → 不変


def test_fanout_is_idempotent(store):
    projects, saved = store
    app_module._fanout_master_name_to_projections(
        master_account_id="macc-1", new_name="New", master_org=ORG_A)
    saved.clear()
    # 同名 re-apply → 変化なし → write ゼロ (churn 防止)。
    written = app_module._fanout_master_name_to_projections(
        master_account_id="macc-1", new_name="New", master_org=ORG_A)
    assert written == 0 and saved == []


def test_fanout_empty_org_is_noop(store):
    projects, saved = store
    assert app_module._fanout_master_name_to_projections(
        master_account_id="macc-1", new_name="New", master_org="") == 0
    assert saved == []
