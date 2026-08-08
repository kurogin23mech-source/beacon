"""cross-project 顧客(Account) read エンドポイント (ms-111 / e-3872).

現在のプロジェクト P に開示された、同じ組織の他プロジェクトの Account を横断して
返す。ms-113 の開示モデル (project_links + can_disclose) の read 側配線を pin する:

  - P に link された他プロジェクトの Account が返る
  - link されていない Account は返らない (fail-closed)
  - 別 org のプロジェクトの Account は、たとえ P に link されていても返らない
    (org 境界を越えない)
  - link を外すと返らなくなる (剥奪即時 = 現在の project_links で評価)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402
# ms-127 e-4871 (PR1): get_disclosed_accounts moved into the /api/projects/*
# router (nested in make_router), so it is no longer importable as
# app.get_disclosed_accounts. We build the router with a delegating
# _load_meta_only and invoke the extracted endpoint directly. db is the same
# shared store_router module in both, so monkeypatching app_module.db still
# reaches the handler.
import routers_projects as rp  # noqa: E402

# Per-test hook the injected _load_meta_only delegates to (set in _setup).
_STATE: dict = {}


def _stub(*_a, **_k):
    return None


def _disclosed_accounts_endpoint():
    router = rp.make_router(
        _stub,
        _load=_stub,
        _load_meta_only=lambda pid, user: _STATE["load_meta"](pid, user),
        _require_project_role=_stub,
        _require_write=_stub,
        _require_owner=_stub,
        _apply_op_and_broadcast=_stub,
        _resolve_author=_stub,
        _save=_stub,
        _broadcast_project_after_write=_stub,
        _broadcast_document_change=_stub,
        require_envelope_for_action=lambda *_a, **_k: _stub,
        is_auth_enabled=lambda: True,
    )
    for r in router.routes:
        if (getattr(r, "path", None)
                == "/api/projects/{project_id}/disclosed-accounts"
                and "GET" in getattr(r, "methods", set())):
            return r.endpoint
    raise LookupError("disclosed-accounts route not found")


def _acc(acc_id, links):
    return {"id": acc_id, "name": acc_id, "project_links": list(links)}


def _setup(monkeypatch, sales_accounts, *, other_org_accounts=None):
    # lens P = proj-dev, org = org-p-u1 (personal org of u1).
    _STATE["load_meta"] = lambda pid, user: {"owner": "u1", "org_id": "org-p-u1"}
    projects = {
        "proj-sales": {"owner": "u1", "org_id": "org-p-u1", "name": "営業",
                       "accounts": sales_accounts},
        "proj-other": {"owner": "u2", "org_id": "org-p-u2", "name": "別org",
                       "accounts": other_org_accounts or []},
        "proj-dev": {"owner": "u1", "org_id": "org-p-u1", "name": "開発",
                     "accounts": []},
    }
    monkeypatch.setattr(app_module.db, "list_projects",
                        lambda uid: [{"project_id": k} for k in projects])
    monkeypatch.setattr(app_module.db, "get_project",
                        lambda pid: projects.get(pid))


def _call():
    return _disclosed_accounts_endpoint()("proj-dev", {"sub": "u1"})


def test_linked_account_is_returned_with_home_annotation(monkeypatch):
    _setup(monkeypatch, [_acc("acc-1", ["proj-dev"]), _acc("acc-2", [])])
    out = _call()
    ids = [a["id"] for a in out["disclosed_accounts"]]
    assert ids == ["acc-1"]              # acc-2 は未link → 出ない (fail-closed)
    assert out["disclosed_accounts"][0]["home_project_id"] == "proj-sales"
    assert out["disclosed_accounts"][0]["home_project_name"] == "営業"


def test_unlinked_account_is_failclosed(monkeypatch):
    _setup(monkeypatch, [_acc("acc-1", [])])
    assert _call()["disclosed_accounts"] == []


def test_other_org_account_not_crossed_even_if_linked(monkeypatch):
    # 別 org のプロジェクトに proj-dev へ link された Account があっても越境しない。
    _setup(monkeypatch, [], other_org_accounts=[_acc("acc-x", ["proj-dev"])])
    assert _call()["disclosed_accounts"] == []


def test_revocation_immediate_via_current_links(monkeypatch):
    # link を外した状態 = 現在の project_links で評価 → 返らない (剥奪即時)。
    _setup(monkeypatch, [_acc("acc-1", ["proj-sales-internal"])])  # dev に link 無し
    assert _call()["disclosed_accounts"] == []


def test_lens_project_itself_is_skipped(monkeypatch):
    # 自分自身 (proj-dev) は home read が担うので cross gather からは除外。
    _setup(monkeypatch, [_acc("acc-1", ["proj-dev"])])
    # proj-dev の accounts は空なので、proj-sales の acc-1 のみ。二重計上しない。
    assert [a["id"] for a in _call()["disclosed_accounts"]] == ["acc-1"]
