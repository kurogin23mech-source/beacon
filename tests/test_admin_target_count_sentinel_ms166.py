"""Admin ownerless-audit target_count: unknown vs zero (ms-166 e-6073).

PR#710 fixed the live target_count silent-0 (a sales project's list card read empty
because len(milestones)==0). The independent review of PR#710 (2026-09-03) found the
admin ownerless-audit endpoint RE-INTRODUCED the same silent-0 via a router-level fallback:
``p.get("target_count", p.get("milestone_count", 0))`` — when a summary carried no
target_count, it fell back to milestone_count (0 for a sales project), making "target_count
unknown (pre-migration summary)" indistinguishable from "genuinely 0 targets".

The store layer (list_all_projects in all 3 backends) always computes target_count, so the
fix drops the milestone_count fallback: absent → None (a visible "unknown" sentinel), a real
count → itself (including a genuine 0). This pins that unknown and zero stay distinct.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers_admin  # noqa: E402


class _FakeDB:
    """Minimal admin-endpoint store stub. The ownerless rows exercise the target_count
    cases: a real positive count, a genuine 0, and an ABSENT field (a pre-migration
    summary — must surface as None, not 0)."""

    def __init__(self, rows):
        self._rows = rows

    def list_all_projects(self):
        return list(self._rows)

    def get_project(self, pid):
        return {"name": f"full-{pid}"}


def _call_ownerless(monkeypatch, rows):
    # Build a fresh router with the admin gate passing (the real gate reads role from the
    # store); swap the module-global db so the body never touches a real backend — the
    # harness test_app_router_admin_e4869 uses this exact pattern for gated endpoints.
    probe = FastAPI()
    probe.include_router(
        routers_admin.make_router(
            require_auth=lambda: {"sub": "admin1", "email": "admin1@example.com"},
            require_admin=lambda user: None,   # admin passes
            apply_op_and_broadcast=lambda *a, **k: {},
        )
    )
    monkeypatch.setattr(routers_admin, "db", _FakeDB(rows))
    r = TestClient(probe).get("/api/admin/projects/ownerless")
    assert r.status_code == 200, (r.status_code, r.text)
    return {p["project_id"]: p for p in r.json()["projects"]}


def test_target_count_unknown_is_none_not_zero(monkeypatch):
    # A pre-migration summary with NO target_count must read as None (unknown), NOT be
    # silently coerced to milestone_count's 0 (the re-introduced conflation e-6073 closes).
    rows = [
        {"project_id": "p-sales", "milestone_count": 0},  # no target_count → unknown
    ]
    out = _call_ownerless(monkeypatch, rows)
    assert out["p-sales"]["target_count"] is None, (
        "an absent target_count must surface as None (unknown), not 0 — else a sales "
        "project's pre-migration card is indistinguishable from a truly empty project")


def test_genuine_zero_stays_zero(monkeypatch):
    # A summary that DID compute target_count and got 0 stays 0 (a real empty project) —
    # the sentinel change must not turn a real 0 into None.
    rows = [{"project_id": "p-empty", "milestone_count": 0, "target_count": 0}]
    out = _call_ownerless(monkeypatch, rows)
    assert out["p-empty"]["target_count"] == 0


def test_real_count_passes_through(monkeypatch):
    # target_count is occupation-generic (not len(milestones)): a project with 3 targets but
    # 0 milestones must report 3, independent of milestone_count.
    rows = [{"project_id": "p-opps", "milestone_count": 0, "target_count": 3}]
    out = _call_ownerless(monkeypatch, rows)
    assert out["p-opps"]["target_count"] == 3
    # milestone_count is still exposed verbatim for back-compat (not conflated with targets).
    assert out["p-opps"]["milestone_count"] == 0
