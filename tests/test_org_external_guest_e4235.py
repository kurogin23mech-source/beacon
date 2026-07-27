"""Unit tests for external-guest visibility — ms-118 / e-4235.

External guest = someone who participates in a project but is NOT a member of
the team org that project belongs to (ms-113 / e-3735). The invite operation
already exists (`beacon member invite`); this task adds the *visibility* surface
so admins can tell org members apart from external guests.

Covers:
  - org.external_guest_user_ids : pure classifier (team-org only)
  - commands._annotate_external_guests : best-effort annotation on member rows
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import org as org_mod  # noqa: E402


NOW = "2026-07-27T00:00:00+00:00"


# ---------------------------------------------------------------------------
# org.external_guest_user_ids — pure classifier
# ---------------------------------------------------------------------------

def _team_org(members):
    return {"org_id": "org-t-abcd", "personal": False,
            "members": [{"user_id": u, "role": r} for u, r in members]}


def test_team_org_flags_non_members_as_guests():
    org = _team_org([("owner", "owner"), ("emp", "member")])
    guests = org_mod.external_guest_user_ids(org, ["owner", "emp", "guest1", "guest2"])
    assert guests == {"guest1", "guest2"}


def test_all_org_members_no_guests():
    org = _team_org([("owner", "owner"), ("emp", "member")])
    assert org_mod.external_guest_user_ids(org, ["owner", "emp"]) == set()


def test_personal_org_has_no_guests():
    # 個人 project の共同編集者は org スコープの guest ではない → 空集合。
    personal = org_mod.build_personal_org("owner", "o@x", now=NOW)
    assert org_mod.external_guest_user_ids(personal, ["owner", "collab"]) == set()


def test_none_or_empty_inputs():
    assert org_mod.external_guest_user_ids(None, ["a"]) == set()
    assert org_mod.external_guest_user_ids(_team_org([("o", "owner")]), []) == set()
    assert org_mod.external_guest_user_ids(_team_org([("o", "owner")]), [""]) == set()


# ---------------------------------------------------------------------------
# commands._annotate_external_guests — best-effort row annotation
# ---------------------------------------------------------------------------

@pytest.fixture()
def commands_mod():
    import importlib
    mod = importlib.import_module("commands")
    return mod


class _FakeStore:
    def __init__(self, org):
        self._org = org

    def get_org(self, org_id):
        if self._org is None or self._org.get("org_id") != org_id:
            raise ValueError(f"org '{org_id}' not found")
        return self._org


def _patch_store(commands_mod, monkeypatch, org):
    monkeypatch.setattr(commands_mod, "get_store", lambda *a, **k: _FakeStore(org))


def test_annotate_flags_guests_for_team_org(commands_mod, monkeypatch):
    org = _team_org([("owner", "owner"), ("emp", "member")])
    _patch_store(commands_mod, monkeypatch, org)
    project = {"project_id": "p1", "owner": "owner", "org_id": "org-t-abcd"}
    members = [
        {"user_id": "owner", "role": "owner"},
        {"user_id": "emp", "role": "editor"},
        {"user_id": "guest1", "role": "viewer"},
    ]
    commands_mod._annotate_external_guests(project, members)
    flags = {m["user_id"]: m["external_guest"] for m in members}
    assert flags == {"owner": False, "emp": False, "guest1": True}


def test_annotate_personal_org_no_guests(commands_mod, monkeypatch):
    # personal org (org_id 未設定 → owner 導出) は team org でないので誰も guest でない。
    _patch_store(commands_mod, monkeypatch, None)
    project = {"project_id": "p1", "owner": "owner"}  # no org_id → personal
    members = [{"user_id": "owner", "role": "owner"},
               {"user_id": "collab", "role": "editor"}]
    commands_mod._annotate_external_guests(project, members)
    assert all(m["external_guest"] is False for m in members)


def test_annotate_org_load_failure_is_non_fatal(commands_mod, monkeypatch):
    # team org を指すが store が引けない → crash せず全員 False。
    _patch_store(commands_mod, monkeypatch, None)  # get_org raises for any id
    project = {"project_id": "p1", "owner": "owner", "org_id": "org-t-zzzz"}
    members = [{"user_id": "owner", "role": "owner"},
               {"user_id": "guest", "role": "viewer"}]
    commands_mod._annotate_external_guests(project, members)  # must not raise
    assert all(m["external_guest"] is False for m in members)


def test_annotate_local_id_schema(commands_mod, monkeypatch):
    # local member rows use `id` (not user_id); classifier must still match.
    org = _team_org([("owner", "owner")])
    _patch_store(commands_mod, monkeypatch, org)
    project = {"project_id": "p1", "owner": "owner", "org_id": "org-t-abcd"}
    members = [{"id": "owner", "role": "owner"}, {"id": "guest", "role": "viewer"}]
    commands_mod._annotate_external_guests(project, members)
    assert members[0]["external_guest"] is False
    assert members[1]["external_guest"] is True
