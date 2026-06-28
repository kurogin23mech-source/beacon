"""Contract pin: API surface that ms-55 depends on (ms-69 / e-1657).

ms-55 (= claim 交渉で半自律的に作業分担) is going to build on top of the
trek API delivered by ms-69. The three queries ms-55 needs to make on the
trek doc are:

  1. **member list** — "who is in this trek?" (= when can I broadcast a
     claim request to teammates? whose sessions count?)
  2. **scope list**  — "what work items are in scope?" (= when ms-55
     issues a stop/handoff, which milestones / operations / tasks does
     it apply to?)
  3. **status read** — "is this trek active and not halted?" (= claim
     issuance is only valid while active + un-halted)

These tests pin the **response shape** of those three queries so a future
trek API change cannot silently break ms-55. If you NEED to break the
contract, do it intentionally — by editing this file with a justification
comment — so ms-55 sees the breakage before deploy.

The full server response schema is documented in the ms-69 SPEC doc
(fnwI2KzzDSJdERIfcwtq) under "Contract for ms-55 (e-1657)". This file is
the mechanical enforcement of that contract.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

# Same alias trick as test_trek_api.py — keep store_router writes routed
# through whatever firestore_client.* points at after later test files
# install their own mocks.
sys.modules["firestore_client"] = app_module.db


_treks: dict[str, dict] = {}
_users_by_email: dict[str, tuple[str, dict]] = {}


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id: str, data: dict):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_list_treks(actor_id=None, *, status=None, include_archived=False):
    out = []
    for t in _treks.values():
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        if actor_id:
            creator = (t.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id") for m in t.get("members") or []]
            if creator != actor_id and actor_id not in members:
                continue
        out.append(copy.deepcopy(t))
    return out


def _mock_find_user_by_email(email: str):
    return _users_by_email.get(email)


def _mock_no_op(*args, **kwargs):
    return None


def _rebind_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_no_op),
        ("get_user", _mock_no_op),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    return prior


def _restore_db(prior):
    db_module = app_module.db
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)


client = TestClient(app_module.app)


LEADER_UID = "uid-leader"
MEMBER_UID = "uid-member"


def _impersonate(uid: str) -> None:
    def _fake_auth():
        return {"sub": uid, "email": f"{uid}@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    _users_by_email["leader@x"] = (LEADER_UID, {"sub": LEADER_UID, "email": "leader@x"})
    _users_by_email["member@x"] = (MEMBER_UID, {"sub": MEMBER_UID, "email": "member@x"})
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _treks.clear()
        _users_by_email.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior)


def _create_trek_with_member_and_scope() -> str:
    """Create a trek with LEADER + joined MEMBER + 3 scope entries (mixed
    project / milestone / operation / task refs), then start it.

    ms-97 / e-2626 — scope-add now stages a pending op (AC23); each PUT
    must be followed by an approve POST to actually commit the entry
    into ``scope[]``. The contract under test (= ``scope[*]`` shape on
    GET /api/treks/{id}) is unchanged; only the setup steps grew.
    """
    _impersonate(LEADER_UID)
    r = client.post("/api/treks", json={
        "title": "Contract pin trek",
        "creator_session_id": "sv-leader",
    })
    trek_id = r.json()["trek_id"]
    # invite MEMBER + have them join
    client.post(f"/api/treks/{trek_id}/members", json={"email": "member@x"})
    _impersonate(MEMBER_UID)
    client.post(f"/api/treks/{trek_id}/members/join")
    _impersonate(LEADER_UID)
    # 3 cross-project scope entries — each PUT stages a pending op, then
    # we approve to commit (= e-2626 staging flow).
    for entry in (
        {"project": "beacon", "milestone": "ms-69"},
        {"project": "pe", "operation": "op-12"},
        {"project": "lps", "task": "e-1234"},
    ):
        r = client.put(f"/api/treks/{trek_id}/scope", json=entry)
        pid = r.json()["pending_op"]["pending_id"]
        client.post(f"/api/treks/{trek_id}/scope/approve/{pid}")
    # start
    client.post(f"/api/treks/{trek_id}/start")
    return trek_id


# ---------------------------------------------------------------------------
# Contract 1: member list — what ms-55 needs to know about participants
# ---------------------------------------------------------------------------

class TestContract_MemberList:
    """ms-55 requirement: enumerate ``members[*]`` with at least
    ``user_id``, ``email``, ``role``, and ``joined_at`` per member.

    The full trek doc is returned by GET /api/treks/{id}; ms-55 reads
    ``members[]`` from it. We pin those four fields here. Any new field
    is allowed (= forward-compatible), but removing or renaming any of
    these breaks ms-55.
    """

    def test_get_trek_returns_members_array(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 200
        body = r.json()
        assert "members" in body
        assert isinstance(body["members"], list)
        assert len(body["members"]) == 2

    def test_each_member_has_required_fields(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        required = {"user_id", "email", "role", "joined_at", "invited_at"}
        for m in body["members"]:
            missing = required - set(m.keys())
            assert not missing, (
                f"member is missing required field(s) {missing}: {m}"
            )

    def test_role_field_is_leader_or_member(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        roles = {m["role"] for m in body["members"]}
        assert roles <= {"leader", "member"}, (
            f"role enum changed (got {roles}); ms-55 only knows leader/member"
        )

    def test_joined_at_distinguishes_joined_from_invited(self):
        """ms-55 needs to know who is *actually* present (= can be the
        target of a claim handoff). ``joined_at`` empty = invited but
        not yet on the trek."""
        trek_id = _create_trek_with_member_and_scope()
        # Add an invitee who doesn't join.
        _impersonate(LEADER_UID)
        _users_by_email["pending@x"] = ("uid-pending",
                                        {"sub": "uid-pending",
                                         "email": "pending@x"})
        client.post(f"/api/treks/{trek_id}/members",
                    json={"email": "pending@x"})
        body = client.get(f"/api/treks/{trek_id}").json()
        joined = [m for m in body["members"] if m["joined_at"]]
        invited_only = [m for m in body["members"] if not m["joined_at"]]
        assert len(joined) == 2
        assert len(invited_only) == 1


# ---------------------------------------------------------------------------
# Contract 2: scope list — work items in this trek's scope
# ---------------------------------------------------------------------------

class TestContract_ScopeList:
    """ms-55 requirement: read ``scope[*]`` from GET /api/treks/{id}.
    Each entry has ``project`` (required) and at most one of
    ``milestone`` / ``operation`` / ``task`` (= narrows the scope ref).

    ms-55's stop / claim primitives need to know which work items are
    covered when the user says "stop this trek": iterate scope, apply
    to each ref.
    """

    def test_scope_returns_list_of_dicts(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        assert isinstance(body["scope"], list)
        assert len(body["scope"]) == 3

    def test_each_scope_entry_has_project(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        for e in body["scope"]:
            assert "project" in e and e["project"], (
                f"scope entry missing project field: {e}"
            )

    def test_scope_ref_uses_known_keys(self):
        """``milestone`` / ``operation`` / ``task`` are the only narrowing
        keys ms-55 knows about. New keys are forward-compatible (ignored
        by ms-55); removing any of these three breaks ms-55."""
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        allowed_extra_keys = {"milestone", "operation", "task"}
        for e in body["scope"]:
            extra = set(e.keys()) - {"project"}
            unknown = extra - allowed_extra_keys
            # forward-compat OK: new keys allowed; backwards-compat NOT ok:
            # the three known keys must remain functional.
            # Here we just verify the entry is well-formed.
            for k in extra & allowed_extra_keys:
                assert e[k], f"scope entry has empty {k} field: {e}"

    def test_project_only_scope_entry(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        # Add a project-only scope entry (no narrowing). ms-97 / e-2626:
        # stage + approve to actually commit (= AC23 staging flow).
        r = client.put(f"/api/treks/{trek_id}/scope",
                       json={"project": "trailnode"})
        pid = r.json()["pending_op"]["pending_id"]
        client.post(f"/api/treks/{trek_id}/scope/approve/{pid}")
        body = client.get(f"/api/treks/{trek_id}").json()
        project_only = [e for e in body["scope"]
                        if "milestone" not in e and "operation" not in e
                        and "task" not in e]
        assert any(e["project"] == "trailnode" for e in project_only)


# ---------------------------------------------------------------------------
# Contract 3: status read — is the trek actionable right now?
# ---------------------------------------------------------------------------

class TestContract_StatusRead:
    """ms-55 requirement: before issuing a claim or running an action,
    check the trek's lifecycle status and halt flag.

    - ``status`` ∈ {planning, active, archived}
    - ``halt`` is null when nothing is halting; an object with
      ``issued_at`` / ``issued_by_session_id`` / ``reason`` when halted

    ms-55 only operates while ``status == "active"`` AND ``halt is None``.
    """

    def test_status_field_returned_via_get_trek(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}").json()
        assert "status" in body
        assert body["status"] in {"planning", "active", "archived"}

    def test_summary_endpoint_returns_status_and_halted_flag(self):
        """The dedicated summary endpoint is the cheap path ms-55 uses
        for "can I act?" checks without fetching the whole trek doc."""
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}/summary").json()
        for field in ("status", "halted", "halt"):
            assert field in body, f"summary missing {field}"
        assert isinstance(body["halted"], bool)

    def test_active_unhalted_summary_reports_actionable(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        body = client.get(f"/api/treks/{trek_id}/summary").json()
        assert body["status"] == "active"
        assert body["halted"] is False
        assert body["halt"] is None

    def test_halted_trek_summary_carries_halt_record(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-leader",
            "reason": "blocked on legal review",
        })
        body = client.get(f"/api/treks/{trek_id}/summary").json()
        assert body["halted"] is True
        # ms-55 reads the halt record to attribute the stop signal:
        for field in ("issued_at", "issued_by_session_id", "reason"):
            assert field in body["halt"], f"halt record missing {field}"
        assert body["halt"]["reason"] == "blocked on legal review"

    def test_archived_trek_status_reflected(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate(LEADER_UID)
        client.delete(f"/api/treks/{trek_id}")
        body = client.get(f"/api/treks/{trek_id}/summary").json()
        assert body["status"] == "archived"


# ---------------------------------------------------------------------------
# Contract 4: error envelope — what 401/403/404 look like to ms-55
# ---------------------------------------------------------------------------

class TestContract_Errors:
    """ms-55 expects:
      - 401 when unauthenticated (token absent / invalid)
      - 403 when authenticated but caller is not a trek member
      - 404 when trek_id does not exist

    The body shape is FastAPI's standard ``{"detail": "..."}`` — ms-55
    parses ``detail`` for human-readable error display.
    """

    def test_unauthenticated_returns_401(self):
        app_module.app.dependency_overrides.clear()
        r = client.get("/api/treks/tk-anything")
        assert r.status_code == 401

    def test_non_member_returns_403(self):
        trek_id = _create_trek_with_member_and_scope()
        _impersonate("uid-outsider")
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 403
        assert "detail" in r.json()

    def test_missing_trek_returns_404(self):
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-does-not-exist")
        assert r.status_code == 404
        assert "detail" in r.json()
