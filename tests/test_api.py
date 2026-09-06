"""API integration tests using FastAPI TestClient with in-memory Firestore mock."""

import sys
import os
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route operations.apply_operation through the in-memory mock instead of real
# Firestore. Must be set BEFORE importing operations / app.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

# Mock firestore_client before importing app
import firestore_client

_store: dict[str, dict] = {}


def mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_projects():
    return [
        {"project_id": pid, "name": data.get("name", ""), "objective": data.get("objective", "")}
        for pid, data in _store.items()
    ]


firestore_client.get_project = mock_get_project
firestore_client.save_project = mock_save_project
firestore_client.list_projects = mock_list_projects

# ms-43 e-631: documents are queried by the search endpoint via list_documents.
_docs_store: dict[str, list[dict]] = {}


def mock_list_documents(project_id: str):
    return copy.deepcopy(_docs_store.get(project_id, []))


firestore_client.list_documents = mock_list_documents

from fastapi.testclient import TestClient
import app as app_module

# ms-95 e-2407: Also mirror project mocks onto store_router (= app.py's db).
# app.py does `import store_router as db`, and store_router does
# `from firestore_client import get_project` at its import time. If a sibling
# test file (e.g. test_bus_transport.py) sets `sys.modules["firestore_client"]
# = store_router` and then rebinds `firestore_client.get_project = noop_lambda`
# at module-scope, that noop OVERWRITES store_router.get_project — wiping out
# the mocks this file installed at lines 37-49. The fixture below re-applies
# our mocks per test, but only on the ORIGINAL firestore_client; store_router
# stays polluted unless we mirror here too.
_store_router_module = app_module.db
_store_router_module.get_project = mock_get_project
_store_router_module.save_project = mock_save_project
_store_router_module.list_projects = mock_list_projects
_store_router_module.list_documents = mock_list_documents

# Disable auth for functional tests (auth tested separately below)
app_module._auth_enabled = False

app = app_module.app
client = TestClient(app)

PROJECT_ID = "test-project"
SEED_PROJECT = {
    "name": "Test",
    "milestones": [
        {
            "id": "ms-1", "title": "First milestone", "status": "in_progress",
            "progress": 20, "target_date": "2026-06-01", "commits": [],
            "entries": [
                {
                    "id": "e-1", "type": "task", "description": "Task one",
                    "status": "todo", "date": "2026-05-11",
                    "created_at": "2026-05-11", "done_at": None, "meta": {},
                },
            ],
        },
        {
            "id": "ms-2", "title": "Second milestone", "status": "todo",
            "progress": 0, "target_date": "", "commits": [], "entries": [],
        },
    ],
}


@pytest.fixture(autouse=True)
def reset_store():
    # Re-apply firestore_client mocks every test in case another test module's
    # module-level patches have replaced them during collection. Specifically,
    # tests/test_bus_directory.py:43-45 and tests/test_bus_transport.py:87-89
    # assign lambdas to firestore_client.get_project / save_project /
    # list_projects at import time (= pytest collection time), which silently
    # stomps the mocks we set up at our own module load (lines 37-39 above).
    # Without this re-application, get_project("new-project") would return
    # {"name": "test", "milestones": []} from the leaked lambda and
    # test_create_project would fail with 409 (project already exists).
    firestore_client.get_project = mock_get_project
    firestore_client.save_project = mock_save_project
    firestore_client.list_projects = mock_list_projects
    firestore_client.list_documents = mock_list_documents
    # ms-95 e-2407: mirror on store_router (= app.py's db); see module-scope
    # comment above the `_store_router_module.X = mock_X` block for why.
    _store_router_module.get_project = mock_get_project
    _store_router_module.save_project = mock_save_project
    _store_router_module.list_projects = mock_list_projects
    _store_router_module.list_documents = mock_list_documents
    _store.clear()
    _store[PROJECT_ID] = copy.deepcopy(SEED_PROJECT)
    _docs_store.clear()
    yield
    _store.clear()
    _docs_store.clear()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# OAuth client_id: env is the single source of truth, loud on empty in prod
# (ms-96 e-3196 / e-3197). The hardcoded default was removed so a missing env
# can't be silently masked — a firebase-provider production deploy without
# BEACON_OAUTH_CLIENT_ID must fail the /health check (curl -fsS in
# vps-pull-deploy.sh) instead of silently shipping a dead login button.
# ---------------------------------------------------------------------------

def test_health_degraded_when_prod_firebase_missing_client_id(monkeypatch):
    monkeypatch.setenv("BEACON_ENV", "prod")
    monkeypatch.setenv("BEACON_AUTH_PROVIDER", "firebase")
    monkeypatch.delenv("BEACON_OAUTH_CLIENT_ID", raising=False)
    r = client.get("/health")
    assert r.status_code == 503
    assert "BEACON_OAUTH_CLIENT_ID" in r.json()["detail"]


def test_health_ok_when_prod_firebase_has_client_id(monkeypatch):
    monkeypatch.setenv("BEACON_ENV", "prod")
    monkeypatch.setenv("BEACON_AUTH_PROVIDER", "firebase")
    monkeypatch.setenv(
        "BEACON_OAUTH_CLIENT_ID", "some-id.apps.googleusercontent.com"
    )
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_ok_in_dev_even_without_client_id(monkeypatch):
    # dev は Google ではなくローカルログインフォーム経路なので client_id 空でも
    # 200。deploy を落とす gate は本番 (BEACON_ENV=prod) のみ。
    monkeypatch.setenv("BEACON_ENV", "dev")
    monkeypatch.setenv("BEACON_AUTH_PROVIDER", "firebase")
    monkeypatch.delenv("BEACON_OAUTH_CLIENT_ID", raising=False)
    r = client.get("/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 宣言的 readiness (ms-105 e-3312): /health の手書き if を、本番必須設定を回す
# 純関数 evaluate_prod_readiness に置き換えた。新しい必須設定はリストへの追記で
# 効く (= endpoint 本体を触らない) ことを固定する。
# ---------------------------------------------------------------------------

def test_prod_readiness_reports_missing_client_id():
    fails = app_module.evaluate_prod_readiness("prod", "firebase", {})
    assert len(fails) == 1
    assert "BEACON_OAUTH_CLIENT_ID" in fails[0]


def test_prod_readiness_clean_when_client_id_present():
    env = {"BEACON_OAUTH_CLIENT_ID": "some-id.apps.googleusercontent.com"}
    assert app_module.evaluate_prod_readiness("prod", "firebase", env) == []


def test_prod_readiness_lax_outside_prod_firebase():
    # dev / 非 firebase provider は applies が偽なので、client_id 空でも失敗ゼロ。
    assert app_module.evaluate_prod_readiness("dev", "firebase", {}) == []
    assert app_module.evaluate_prod_readiness("prod", "cognito", {}) == []


def test_prod_readiness_is_declarative_new_check_picked_up(monkeypatch):
    # AC: 新しい本番必須設定は「リストへの 1 行追記」だけで /health に効く
    # (= health() 本体を編集しない)。一時 check を差し込んで検証する。
    extra = {
        "env_var": "BEACON_MADE_UP_REQUIRED",
        "applies": lambda env, provider: env == "prod",
        "detail": "BEACON_MADE_UP_REQUIRED is unset",
    }
    monkeypatch.setattr(
        app_module, "_PROD_READINESS_CHECKS",
        app_module._PROD_READINESS_CHECKS + [extra],
    )
    fails = app_module.evaluate_prod_readiness("prod", "firebase", {})
    assert any("BEACON_MADE_UP_REQUIRED" in f for f in fails)


def test_auth_config_firebase_client_id_comes_from_env_not_hardcode(monkeypatch):
    # e-3196: ハードコード default を撤去したので、env がそのまま真値源になる。
    monkeypatch.setattr(app_module, "_AUTH_PROVIDER", "firebase", raising=False)
    monkeypatch.setenv(
        "BEACON_OAUTH_CLIENT_ID", "explicit.apps.googleusercontent.com"
    )
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["client_id"] == "explicit.apps.googleusercontent.com"

    monkeypatch.delenv("BEACON_OAUTH_CLIENT_ID", raising=False)
    r2 = client.get("/api/auth/config")
    assert r2.json()["client_id"] == "", (
        "hardcoded default must be gone: empty env → empty client_id"
    )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

def test_create_project():
    r = client.post("/api/projects/new-project",
                    json={"name": "New Project", "objective": "Test"})
    assert r.status_code == 200
    assert r.json()["status"] == "created"
    assert "new-project" in _store
    # New projects default to schema_version=2 (β subcollection) so concurrent
    # writes to different milestones can proceed in parallel. See e-632.
    assert _store["new-project"].get("schema_version") == 2


def test_existing_project_remains_legacy_v1():
    # The SEED_PROJECT fixture does NOT set schema_version, which mirrors
    # how pre-existing Firestore projects look on disk. apply_operation
    # must still route them through the legacy path without errors.
    assert "schema_version" not in _store[PROJECT_ID]
    # A simple mutation should succeed and not implicitly upgrade the schema.
    # e-1040 retired the /summary PATCH endpoint (no-op), so use a milestone
    # title mutation which still exercises apply_operation on v1.
    ms_id = _store[PROJECT_ID]["milestones"][0]["id"]
    r = client.patch(
        f"/api/projects/{PROJECT_ID}/milestones/{ms_id}",
        json={"title": "still v1"},
    )
    assert r.status_code == 200
    assert _store[PROJECT_ID]["milestones"][0]["title"] == "still v1"
    assert "schema_version" not in _store[PROJECT_ID]


def test_create_project_duplicate():
    r = client.post(f"/api/projects/{PROJECT_ID}",
                    json={"name": "Dupe"})
    assert r.status_code == 409


def test_get_project():
    r = client.get(f"/api/projects/{PROJECT_ID}")
    assert r.status_code == 200
    assert r.json()["name"] == "Test"
    assert len(r.json()["milestones"]) == 2


def test_get_project_enriches_task_counts():
    # ms-46 e-756: REST must return enriched milestones (total_tasks /
    # done_tasks) so that focus-reconnect on Tauri doesn't blank the counts
    # by overwriting WS-enriched state with raw REST data.
    r = client.get(f"/api/projects/{PROJECT_ID}")
    assert r.status_code == 200
    ms_by_id = {m["id"]: m for m in r.json()["milestones"]}
    # ms-1 has 1 todo task
    assert ms_by_id["ms-1"]["total_tasks"] == 1
    assert ms_by_id["ms-1"]["done_tasks"] == 0
    # ms-2 has no entries
    assert ms_by_id["ms-2"]["total_tasks"] == 0
    assert ms_by_id["ms-2"]["done_tasks"] == 0


def test_get_project_not_found():
    r = client.get("/api/projects/nonexistent")
    assert r.status_code == 404


def test_get_project_slim_drops_entries(client_for_slim=None):
    # ms-84 / e-2326 — ?slim=true returns the project with entries[] omitted
    # from each milestone, but keeps the computed total_tasks / done_tasks so
    # the dashboard card summary stays accurate. Web UI uses this on initial
    # mount so the WS frame limit (1 MiB) can't be tripped on first paint.
    r = client.get(f"/api/projects/{PROJECT_ID}?slim=true")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Test"
    for ms in body["milestones"]:
        assert "entries" not in ms, f"milestone {ms['id']} leaked entries in slim mode"
        assert "total_tasks" in ms
        assert "done_tasks" in ms


def test_get_project_slim_drops_tab_scoped_arrays():
    # ms-84 / e-2326 follow-up — slim must also drop top-level pushes /
    # deployments / worktree_sessions so the WS frame stays under Cloud Run's
    # effective WS tolerance. These are fetched via tab-specific endpoints
    # when the user switches to Releases / Worktree tab.
    r = client.get(f"/api/projects/{PROJECT_ID}?slim=true")
    assert r.status_code == 200
    body = r.json()
    for tab_scoped in ("pushes", "deployments", "worktree_sessions"):
        assert tab_scoped not in body, (
            f"slim leaked tab-scoped array '{tab_scoped}' "
            f"(WS frame will be inflated for projects that have any history)"
        )


def test_get_project_pushes_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/pushes")
    assert r.status_code == 200
    body = r.json()
    assert "pushes" in body
    assert isinstance(body["pushes"], list)


def test_get_project_deployments_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/deployments")
    assert r.status_code == 200
    body = r.json()
    assert "deployments" in body
    assert isinstance(body["deployments"], list)


def test_get_project_worktree_sessions_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/worktree-sessions")
    assert r.status_code == 200
    body = r.json()
    assert "worktree_sessions" in body
    assert isinstance(body["worktree_sessions"], list)


def test_get_milestone_entries_returns_full_tree():
    # ms-84 / e-2326 — pair endpoint to the slim broadcast. Web UI calls this
    # per-MS when the user expands a card; payload shape matches the legacy
    # nested entries[] tree from _enrich_project so the SHARED render code
    # keeps working unchanged.
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries")
    assert r.status_code == 200
    body = r.json()
    assert body["milestone_id"] == "ms-1"
    assert isinstance(body["entries"], list)
    # ms-1 fixture has 1 todo task → exactly one entry in the tree.
    assert len(body["entries"]) >= 1


def test_get_milestone_entries_missing_milestone():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-nonexistent/entries")
    assert r.status_code == 404


def test_put_project():
    new_data = {"name": "Updated", "milestones": []}
    r = client.put(f"/api/projects/{PROJECT_ID}", json=new_data)
    assert r.status_code == 200
    assert _store[PROJECT_ID]["name"] == "Updated"


def test_put_project_invalid():
    r = client.put(f"/api/projects/{PROJECT_ID}", json={"bad": "data"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

def test_create_milestone():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones",
                    json={"title": "New MS", "target_date": "2026-12-31", "priority": "medium"})
    assert r.status_code == 200
    assert r.json()["ms_id"] == "ms-3"
    assert len(_store[PROJECT_ID]["milestones"]) == 3


def test_create_milestone_with_description():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones",
                    json={"title": "Desc MS", "description": "A goal", "priority": "medium"})
    assert r.status_code == 200
    ms_id = r.json()["ms_id"]
    ms = next(m for m in _store[PROJECT_ID]["milestones"] if m["id"] == ms_id)
    assert ms["description"] == "A goal"


def test_get_milestone():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-1")
    assert r.status_code == 200
    assert r.json()["title"] == "First milestone"
    assert r.json()["total_tasks"] == 1


def test_get_milestone_not_found():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-99")
    assert r.status_code == 404


def test_update_milestone():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"title": "Updated title", "progress": "50"})
    assert r.status_code == 200
    assert r.json()["title"] == "Updated title"
    assert r.json()["progress"] == 50


def test_update_milestone_description():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"description": "Updated desc"})
    assert r.status_code == 200
    ms = _store[PROJECT_ID]["milestones"][0]
    assert ms["description"] == "Updated desc"


def test_update_milestone_invalid_status():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"status": "bogus"})
    assert r.status_code == 400


def test_done_milestone():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/done")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_delete_milestone():
    r = client.delete(f"/api/projects/{PROJECT_ID}/milestones/ms-1")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def test_create_entry():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries",
                    json={"description": "New task", "date": "2026-05-11", "priority": "medium"})
    assert r.status_code == 200
    assert r.json()["entry_id"] == "e-2"


def test_create_entry_with_detail():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries",
                    json={"description": "Detailed task", "detail": "Some details", "priority": "medium"})
    assert r.status_code == 200
    entries = _store[PROJECT_ID]["milestones"][0]["entries"]
    assert entries[-1]["detail"] == "Some details"


def test_update_entry():
    r = client.patch(f"/api/projects/{PROJECT_ID}/entries/e-1",
                     json={"description": "Updated task"})
    assert r.status_code == 200
    assert r.json()["description"] == "Updated task"


def test_update_entry_not_found():
    r = client.patch(f"/api/projects/{PROJECT_ID}/entries/e-99",
                     json={"description": "nope"})
    assert r.status_code == 400


def test_done_entry():
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-1/done")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_done_entry_records_decision_arm_event(monkeypatch):
    # ms-154 e-5592: done judgment must be CAPTURED as a decision-arm event
    # (who/why/evidence), not just leave the mechanism in place.
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: captured.append((pid, rec)))
    # give the task a done_reason so it flows into rationale (= why).
    _store[PROJECT_ID]["milestones"][0]["entries"][0]["meta"] = {
        "done_reason": "AC 全達成と判断"}
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-1/done")
    assert r.status_code == 200
    arm = [rec for (_pid, rec) in captured if rec.get("kind") == "task-done"]
    assert len(arm) == 1
    rec = arm[0]
    assert rec["decision"] == "done"                     # what
    assert rec["rationale"] == "AC 全達成と判断"           # why (done_reason)
    assert rec["decided_by"] == "autonomous-AI"           # default for CLI done
    # ms-154 e-5650: no commit references e-1 in this store, so evidence is
    # honestly empty (the self-reference is NOT fabricated). related carries e-1.
    assert rec["evidence"] == []
    assert rec["related"]["task_id"] == "e-1"


def test_done_entry_respects_decided_by_override(monkeypatch):
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: captured.append(rec))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/entries/e-1/done?decided_by=human-delegated")
    assert r.status_code == 200
    assert captured[0]["decided_by"] == "human-delegated"


def test_done_entry_rejects_bad_decided_by(monkeypatch):
    # ms-154 e-5649 (AX HIGH): an out-of-vocab decided_by must 400, not silently
    # coerce to autonomous-AI (which would corrupt the audit attribution and drop
    # the write into the wrong decider undetectably). Symmetric with POST /decisions.
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: captured.append(rec))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/entries/e-1/done?decided_by=the-vibes")
    assert r.status_code == 400
    assert "decided_by" in r.json()["detail"]
    # the task is NOT marked done and no decision is recorded — rejected up front.
    assert captured == []


def test_done_milestone_rejects_bad_decided_by(monkeypatch):
    # ms-154 e-5649: same guard on the milestone-done route.
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: captured.append(rec))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/milestones/ms-1/done?decided_by=the-vibes")
    assert r.status_code == 400
    assert "decided_by" in r.json()["detail"]
    assert captured == []


def test_done_milestone_records_completion_verdict(monkeypatch):
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: captured.append(rec))
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/done")
    assert r.status_code == 200
    arm = [rec for rec in captured if rec.get("kind") == "completion-verdict"]
    assert len(arm) == 1
    assert arm[0]["decision"] == "done"
    assert arm[0]["decided_by"] == "AI-proposed-human-chose"  # human-approved default
    # ms-154 e-5650: milestone done gathers no real evidence link → honestly empty
    # (self-reference target:ms-1 is NOT fabricated; related carries it).
    assert arm[0]["evidence"] == []
    assert arm[0]["related"]["target_id"] == "ms-1"


def test_delete_entry():
    r = client.delete(f"/api/projects/{PROJECT_ID}/entries/e-1")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Decision arm — generic decisions write口 (ms-154 e-5593)
# ---------------------------------------------------------------------------

def test_record_decision_appends_to_stream(monkeypatch):
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: (captured.append((pid, rec)) or "dec-x"))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/decisions",
        json={
            "kind": "review-adjudication", "decision": "approve",
            "rationale": "指摘 2 件を精査し受容",
            "decided_by": "autonomous-AI",
            "evidence": ["pr:e-42", "finding:N+1"],
            "related": {"task_id": "e-42"},
        },
        headers={"X-Beacon-Session": "sv-abc"},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "review-adjudication"
    assert len(captured) == 1
    _pid, rec = captured[0]
    assert rec["decision"] == "approve"
    assert rec["decided_by"] == "autonomous-AI"
    assert rec["evidence"] == ["pr:e-42", "finding:N+1"]
    # who is server-stamped from the token/session, not client-provided.
    assert rec["who"]["session_id"] == "sv-abc"
    assert rec["related"]["task_id"] == "e-42"


def test_record_decision_accepts_decided_by_with_empty_evidence(monkeypatch):
    # ms-154 e-5650: the "decided_by → evidence 非空必須" invariant is relaxed.
    # A decision with decided_by but no evidence is now ACCEPTED and recorded with
    # empty evidence — the honest "no physical backing" audit signal, not a 400.
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: (captured.append(rec) or "dec-y"))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/decisions",
        json={"kind": "review-adjudication", "decision": "approve",
              "decided_by": "autonomous-AI"},  # no evidence → honest empty, accepted
    )
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0]["decided_by"] == "autonomous-AI"
    assert captured[0]["evidence"] == []


def test_record_decision_rejects_unknown_related_key(monkeypatch):
    # ms-166 e-5996: related の許可キー外は 400 (無言 drop で 200 成功に見せない)。
    # 旧挙動は未知キーを silent に落とし decision_id を返した = write accepted /
    # field lost の silent 非機能 (dogfood: review-adjudication の pr_number 消失)。
    # append が呼ばれてしまえば「壊れた related のまま保存」なので、呼ばれないことも確認。
    captured = []
    monkeypatch.setattr(_store_router_module, "append_decision_event",
                        lambda pid, rec: (captured.append(rec) or "dec-z"))
    r = client.post(
        f"/api/projects/{PROJECT_ID}/decisions",
        json={"kind": "review-adjudication", "decision": "approve",
              "related": {"task_id": "e-42", "pr_number": "710"}},  # pr_number は許可外
    )
    assert r.status_code == 400
    assert "pr_number" in r.json().get("detail", "")
    assert captured == []  # 弾かれたので永続化層には一切届かない


def test_record_decision_rejects_empty_kind():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/decisions",
        json={"kind": "", "decision": "approve"})
    assert r.status_code == 400


def test_record_decision_unknown_project_404():
    r = client.post(
        "/api/projects/no-such-project/decisions",
        json={"kind": "log-backstop", "decision": "noted"})
    assert r.status_code == 404


def test_list_decisions_reads_stream(monkeypatch):
    # ms-154 e-5595: the read side the independent-verification path uses.
    rows = [
        {"decision_id": "dec-1", "kind": "task-done", "decision": "done",
         "decided_by": "autonomous-AI", "evidence": ["task:e-1"], "rationale": "AC met"},
        {"decision_id": "dec-2", "kind": "review-adjudication", "decision": "approve",
         "decided_by": "autonomous-AI", "evidence": ["pr:e-2"]},
    ]
    # ms-166 e-5970 / ms-164 e-6030: kind/session/target/limit/since are pushed INTO
    # the store read (the store owns the newest-window + filters now), so the mock
    # signature carries them too.
    monkeypatch.setattr(_store_router_module, "list_decision_events",
                        lambda pid, kind="", limit=100, since="", session="",
                        target="": list(rows))
    r = client.get(f"/api/projects/{PROJECT_ID}/decisions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert [d["decision_id"] for d in body["decisions"]] == ["dec-1", "dec-2"]


def test_list_decisions_filters_by_kind(monkeypatch):
    rows = [
        {"decision_id": "dec-1", "kind": "task-done", "decision": "done"},
        {"decision_id": "dec-2", "kind": "review-adjudication", "decision": "approve"},
    ]
    # ms-166 e-5970: the kind filter is the STORE's responsibility now (applied before
    # the limit window), not a route-level post-filter. The route must PASS kind through;
    # the mock mirrors the real store by filtering on it.
    def _fake(pid, kind="", limit=100, since="", session="", target=""):
        return [r for r in rows if not kind or r.get("kind") == kind]
    monkeypatch.setattr(_store_router_module, "list_decision_events", _fake)
    r = client.get(f"/api/projects/{PROJECT_ID}/decisions?kind=task-done")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["decisions"][0]["kind"] == "task-done"


def test_list_decisions_forwards_session_and_target(monkeypatch):
    # ms-164 e-6030: the route must PASS session/target through to the store so the
    # filter is applied BEFORE the limit window (session-end reconciliation).
    seen = {}

    def _fake(pid, kind="", limit=100, since="", session="", target=""):
        seen["session"] = session
        seen["target"] = target
        return []
    monkeypatch.setattr(_store_router_module, "list_decision_events", _fake)
    r = client.get(
        f"/api/projects/{PROJECT_ID}/decisions?session=sv-abc&target=ms-9")
    assert r.status_code == 200
    assert seen == {"session": "sv-abc", "target": "ms-9"}


def test_list_decisions_unknown_project_404():
    r = client.get("/api/projects/no-such-project/decisions")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Deliverable arm — produced-value read口 (ms-162 e-5837)
# The UI read path that lets a reader see WHAT VALUE the project produced (the
# 発端: deliverables were CLI-readable but the UI showed only the arm label).
# ---------------------------------------------------------------------------

import deliverable_resolve as _dr_mod


def test_list_deliverables_wraps_resolver(monkeypatch):
    # The route resolves each adopted-class deliverable pointer and wraps the rows
    # with the same {deliverables,count,all_resolved,unresolved} discriminator the
    # CLI --resolve --json path emits. Patch the resolver to a known shape and
    # assert the route hands it back verbatim + computes the top-level signals.
    rows = [
        {"target_class": "milestone", "kind": "feature-map", "label": "機能",
         "projector": "changelog", "ref": "",
         "resolved": {"strategy": "changelog", "found": True,
                      "count_active": 3,
                      "categories": [{"category": "A1", "count": 2},
                                     {"category": "A2", "count": 1}]}},
    ]
    monkeypatch.setattr(_dr_mod, "resolve_project_deliverables",
                        lambda data: list(rows))
    r = client.get(f"/api/projects/{PROJECT_ID}/deliverables")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["all_resolved"] is True
    assert body["unresolved"] == []
    d = body["deliverables"][0]
    assert d["label"] == "機能"
    assert d["resolved"]["count_active"] == 3
    assert [c["category"] for c in d["resolved"]["categories"]] == ["A1", "A2"]


def test_list_deliverables_reports_unresolved(monkeypatch):
    # An unresolved pointer must surface at the top level (all_resolved False +
    # listed in unresolved) so a consumer detects partial failure without walking
    # every row — symmetric with the CLI's AX contract.
    rows = [
        {"target_class": "milestone", "kind": "feature-map", "label": "機能",
         "projector": "changelog", "ref": "map:app",
         "resolved": {"strategy": "changelog", "found": False,
                      "error": "changelog missing"}},
    ]
    monkeypatch.setattr(_dr_mod, "resolve_project_deliverables",
                        lambda data: list(rows))
    r = client.get(f"/api/projects/{PROJECT_ID}/deliverables")
    assert r.status_code == 200
    body = r.json()
    assert body["all_resolved"] is False
    assert body["unresolved"] == ["map:app"]


def test_list_deliverables_unknown_project_404():
    r = client.get("/api/projects/no-such-project/deliverables")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

def test_log_commit():
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "logged"


def test_log_duplicate():
    client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    assert r.json()["status"] == "duplicate"


def test_log_with_progress():
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1", "progress": "40",
    })
    assert r.status_code == 200
    assert _store[PROJECT_ID]["milestones"][0]["progress"] == 40


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_update_summary_is_deprecated_no_op():
    """e-1040 completed: PATCH /summary writes are no-op.

    The endpoint returns 200 with the previously-stored value (so legacy
    callers don't crash) and a Deprecation header so machine consumers
    can detect the contract change.
    """
    # Seed an existing value to verify it's preserved through the no-op.
    _store[PROJECT_ID]["summary"] = "previous value"

    r = client.patch(f"/api/projects/{PROJECT_ID}/summary",
                     json={"text": "would-be new summary"})

    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "previous value"  # untouched
    assert body.get("write_ignored") is True
    assert body.get("deprecated_since") == "e-1040"
    # HTTP deprecation signals present.
    assert r.headers.get("Deprecation") == "true"
    assert "Sunset" in r.headers
    # Storage was NOT mutated.
    assert _store[PROJECT_ID]["summary"] == "previous value"


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def test_auth_required_when_enabled():
    """When auth is enabled, requests without a token should get 401."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/api/projects")
        assert r.status_code == 401
    finally:
        app_module._auth_enabled = original


def test_auth_invalid_token():
    """When auth is enabled, an invalid token should get 401."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/api/projects",
                       headers={"Authorization": "Bearer invalid-token"})
        assert r.status_code == 401
    finally:
        app_module._auth_enabled = original


def test_health_no_auth():
    """Health endpoint should work without auth regardless."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
    finally:
        app_module._auth_enabled = original


# ---------------------------------------------------------------------------
# ms-43 e-631: unified cross-entity search endpoint
# ---------------------------------------------------------------------------

def test_search_endpoint_returns_unified_results():
    """/api/projects/{id}/search should return milestones, tasks, commits,
    documents, etc. in a single response with facets."""
    # Add a document so we can verify cross-entity coverage.
    _docs_store[PROJECT_ID] = [
        {"doc_id": "spec-1", "title": "First milestone SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Design doc for first milestone.",
         "updated_at": "2026-05-11T00:00:00Z"},
    ]

    r = client.get(f"/api/projects/{PROJECT_ID}/search", params={"q": "first"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert "facets" in body
    assert "total" in body

    types = {r["entity_type"] for r in body["results"]}
    # "First milestone" matches the milestone title; "First milestone SPEC"
    # matches the document title. Both must appear.
    assert "milestone" in types, types
    assert "document" in types, types


def test_search_endpoint_type_filter():
    """type=document should restrict to documents only."""
    _docs_store[PROJECT_ID] = [
        {"doc_id": "spec-1", "title": "Auth SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Auth design", "updated_at": "2026-05-11T00:00:00Z"},
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/search",
                   params={"q": "auth", "type": "document"})
    assert r.status_code == 200, r.text
    body = r.json()
    types = {x["entity_type"] for x in body["results"]}
    assert types <= {"document"}, types


def test_search_endpoint_empty_query_returns_recent():
    """Empty q + no filters should return recent activity (≤ limit), not 400."""
    r = client.get(f"/api/projects/{PROJECT_ID}/search")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 0
    assert isinstance(body["results"], list)


def test_search_endpoint_facets_present():
    """Facets must be returned so the Web UI can render filter chips."""
    r = client.get(f"/api/projects/{PROJECT_ID}/search", params={"q": "milestone"})
    assert r.status_code == 200
    body = r.json()
    assert "type" in body["facets"]
    assert "status" in body["facets"]


def test_search_endpoint_scope_filter_documents():
    """e-616: scope filter restricts documents to a single scope."""
    _docs_store[PROJECT_ID] = [
        {"doc_id": "core-1", "title": "Memory layer principle",
         "scope": "core", "content": "Memory layer is passive",
         "updated_at": "2026-05-01T00:00:00Z"},
        {"doc_id": "spec-1", "title": "Auth SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Auth design", "updated_at": "2026-05-11T00:00:00Z"},
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/search",
                   params={"type": "document", "scope": "spec"})
    assert r.status_code == 200, r.text
    body = r.json()
    scopes = {x.get("scope") for x in body["results"]}
    assert scopes == {"spec"}, scopes
