"""DynamoDB backend parity: list_projects denies ownerless projects (ms-158 / e-5773).

Firestore closed the "ownerless project visible to every user" leak on
2026-07-03 (ms-95 / e-2794) and mysql_client denies too, but the DynamoDB
backend was the stale outlier — its ``list_projects`` still fell through for a
missing ``owner`` with a comment falsely claiming parity with firestore. On an
AWS/DynamoDB-backed deployment that re-opened the exact cross-user visibility
hole. These tests pin the deny-by-default on the DynamoDB path.

The filter logic is exercised directly by stubbing ``_scan_all`` (the only I/O
seam), so no moto table / AWS credentials are needed.
"""
from __future__ import annotations

import os
import sys

import pytest

# ``dynamodb_client`` imports boto3 at module load; CI does not install boto3
# (it is only needed for the AWS/DynamoDB backend). Skip the whole module when
# boto3 is absent rather than erroring the run. The other DynamoDB test modules
# gate on moto the same way (pytest.importorskip).
pytest.importorskip("boto3")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


@pytest.fixture
def ddb():
    # Import the module directly and exercise ``list_projects`` with ``_scan_all``
    # (the only I/O seam) stubbed. Deliberately does NOT touch sys.modules or
    # BEACON_STORE_BACKEND: mutating global module/env state here pollutes other
    # tests in the same process (e.g. the firestore-mocked /api/me tests start
    # attempting real connections). Stubbing the one seam is enough — no AWS
    # credentials, moto table, or module reload needed.
    import dynamodb_client
    return dynamodb_client


_ROWS = [
    {"project_id": "p-own", "name": "Owned by alice", "owner": "alice", "members": []},
    {"project_id": "p-mem", "name": "Alice is member", "owner": "bob",
     "members": [{"user_id": "alice", "role": "editor"}]},
    {"project_id": "p-other", "name": "Bob's private", "owner": "bob", "members": []},
    {"project_id": "p-orphan", "name": "Ownerless", "members": []},  # no owner
    {"project_id": "p-orphan2", "name": "Ownerless blank", "owner": "", "members": []},
]


def test_list_projects_excludes_ownerless_for_user(ddb, monkeypatch):
    monkeypatch.setattr(ddb, "_scan_all", lambda *_a, **_k: list(_ROWS))
    ids = {p["project_id"] for p in ddb.list_projects(user_id="alice")}
    # alice sees only what she owns or is a member of.
    assert ids == {"p-own", "p-mem"}
    # The ownerless projects must NOT leak into her listing (e-5773).
    assert "p-orphan" not in ids
    assert "p-orphan2" not in ids
    # Nor another user's private project.
    assert "p-other" not in ids


def test_list_projects_no_user_returns_all(ddb, monkeypatch):
    # Admin-style unfiltered scan (user_id=None) still returns everything,
    # including ownerless rows, so the admin ownerless-audit path can see them.
    monkeypatch.setattr(ddb, "_scan_all", lambda *_a, **_k: list(_ROWS))
    ids = {p["project_id"] for p in ddb.list_projects()}
    assert ids == {"p-own", "p-mem", "p-other", "p-orphan", "p-orphan2"}
