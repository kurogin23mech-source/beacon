"""Tests for trek.session_history (ms-86 / e-2225).

Covers the persistent session-join record added on the Trek doc, plus the
backfill helpers used by the migration script and the lazy hydration in
``trek_store.load_trek``.

The MEMBERS & AGENTS table UI rewrite is exercised separately in
``test_treks_tab_ui.py`` so the schema layer can stay UI-independent.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402
from lib import trek_store  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_trek():
    """A planning trek with one leader/member, session_history seeded."""
    return trek.new_trek(
        title="T", creator_user_id="u-1", creator_email="creator@x",
        creator_session_id="sv-leader-1",
    )


@pytest.fixture
def tmp_trek_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_TREKS_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# new_trek seeds session_history with the creator session
# ---------------------------------------------------------------------------

def test_new_trek_seeds_session_history_with_creator(base_trek):
    history = base_trek.get("session_history") or []
    assert len(history) == 1, (
        "session_history should start with one entry for the creator session"
    )
    entry = history[0]
    assert entry["session_id"] == "sv-leader-1"
    assert entry["user_id"] == "u-1"
    assert entry["email"] == "creator@x"
    assert entry["role_at_join"] == "leader"
    assert entry["joined_at"]  # non-empty ISO timestamp


# ---------------------------------------------------------------------------
# build_session_history_entry
# ---------------------------------------------------------------------------

def test_build_session_history_entry_basic():
    e = trek.build_session_history_entry(
        session_id="sv-x", user_id="u-2", email="b@x",
        joined_at="2026-01-01T00:00:00Z", role_at_join="member",
    )
    assert e == {
        "session_id": "sv-x", "user_id": "u-2", "email": "b@x",
        "joined_at": "2026-01-01T00:00:00Z", "role_at_join": "member",
    }


def test_build_session_history_entry_requires_session_id():
    with pytest.raises(ValueError, match="session_id"):
        trek.build_session_history_entry(
            session_id="", user_id="u", email="e@x",
            joined_at="2026-01-01T00:00:00Z", role_at_join="member",
        )


def test_build_session_history_entry_requires_identity():
    with pytest.raises(ValueError, match="user_id and email"):
        trek.build_session_history_entry(
            session_id="sv-x", user_id="", email="e@x",
            joined_at="2026-01-01T00:00:00Z", role_at_join="member",
        )


def test_build_session_history_entry_validates_role():
    with pytest.raises(ValueError, match="invalid"):
        trek.build_session_history_entry(
            session_id="sv-x", user_id="u", email="e@x",
            joined_at="2026-01-01T00:00:00Z", role_at_join="bogus",
        )


# ---------------------------------------------------------------------------
# upsert_session_history
# ---------------------------------------------------------------------------

def test_upsert_session_history_appends_new(base_trek):
    trek.upsert_session_history(
        base_trek, session_id="sv-2", user_id="u-2", email="b@x",
        role_at_join="member",
    )
    history = base_trek["session_history"]
    assert len(history) == 2
    assert history[1]["session_id"] == "sv-2"
    assert history[1]["role_at_join"] == "member"


def test_upsert_session_history_idempotent_per_session_id(base_trek):
    # creator already in history; re-upserting the same session_id is a no-op
    trek.upsert_session_history(
        base_trek, session_id="sv-leader-1", user_id="u-1",
        email="creator@x", role_at_join="leader",
    )
    assert len(base_trek["session_history"]) == 1


def test_upsert_session_history_no_op_on_empty_session_id(base_trek):
    # Empty session_id is tolerated as a no-op (= some legacy callers may
    # not have a session id available; we don't want to raise).
    trek.upsert_session_history(
        base_trek, session_id="", user_id="u-1", email="creator@x",
        role_at_join="leader",
    )
    assert len(base_trek["session_history"]) == 1


# ---------------------------------------------------------------------------
# accept_invitation now writes session_history
# ---------------------------------------------------------------------------

def test_accept_invitation_records_session_history(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    trek.accept_invitation(base_trek, user_id="u-2", session_id="sv-2")
    history = base_trek["session_history"]
    assert len(history) == 2
    new_entry = history[-1]
    assert new_entry["session_id"] == "sv-2"
    assert new_entry["user_id"] == "u-2"
    assert new_entry["email"] == "b@x"
    assert new_entry["role_at_join"] == "member"


def test_accept_invitation_history_idempotent_on_same_session(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    trek.accept_invitation(base_trek, user_id="u-2", session_id="sv-2")
    trek.accept_invitation(base_trek, user_id="u-2", session_id="sv-2")
    history = [h for h in base_trek["session_history"] if h["session_id"] == "sv-2"]
    assert len(history) == 1


def test_accept_invitation_history_records_new_session_for_same_user(base_trek):
    # Same user joining from a second session should add a new entry —
    # session_history is at session grain, not user grain.
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    trek.accept_invitation(base_trek, user_id="u-2", session_id="sv-2a")
    trek.accept_invitation(base_trek, user_id="u-2", session_id="sv-2b")
    entries = [h for h in base_trek["session_history"]
               if h["user_id"] == "u-2"]
    assert {e["session_id"] for e in entries} == {"sv-2a", "sv-2b"}


def test_accept_invitation_no_session_id_is_backward_compatible(base_trek):
    """Callers that don't pass session_id (= legacy) still set joined_at."""
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    trek.accept_invitation(base_trek, user_id="u-2")
    # member.joined_at populated, history unchanged
    assert trek.find_member(base_trek, "u-2")["joined_at"]
    assert len(base_trek["session_history"]) == 1


# ---------------------------------------------------------------------------
# backfill_session_history
# ---------------------------------------------------------------------------

def _legacy_trek_doc():
    """Build a trek doc shaped like one persisted before e-2225 landed.

    Three legacy session_id locations are populated:
      - leader_session_id = sv-leader
      - halt.issued_by_session_id = sv-halt (an external session)
      - task_states[].updated_by_session_id = sv-worker
    """
    return {
        "trek_id": "tk-legacy",
        "members": [
            {"user_id": "u-1", "email": "creator@x", "role": "leader",
             "invited_at": "2026-01-01T00:00:00Z",
             "joined_at": "2026-01-01T00:00:00Z", "invited_by": "u-1"},
            {"user_id": "u-2", "email": "b@x", "role": "member",
             "invited_at": "2026-01-02T00:00:00Z",
             "joined_at": "2026-01-02T00:00:00Z", "invited_by": "u-1"},
        ],
        "leader_session_id": "sv-leader",
        "halt": {"issued_by_session_id": "sv-halt",
                 "issued_at": "2026-01-03T00:00:00Z",
                 "reason": "test"},
        "task_states": {
            "e-100": {"state": "working",
                      "updated_by_session_id": "sv-worker",
                      "updated_at": "2026-01-04T00:00:00Z"},
            "e-101": {"state": "todo"},  # no session id, should be ignored
        },
        "scope": [],
        "status": "active",
        "type": "persistent",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-04T00:00:00Z",
    }


def test_backfill_session_history_derives_all_three_locations():
    doc = _legacy_trek_doc()
    added = trek.backfill_session_history(doc)
    assert added == 3
    sids = {e["session_id"] for e in doc["session_history"]}
    assert sids == {"sv-leader", "sv-halt", "sv-worker"}


def test_backfill_session_history_assigns_leader_role_to_leader_sid():
    doc = _legacy_trek_doc()
    trek.backfill_session_history(doc)
    leader = next(e for e in doc["session_history"]
                  if e["session_id"] == "sv-leader")
    assert leader["role_at_join"] == "leader"
    # Non-leader entries default to member.
    worker = next(e for e in doc["session_history"]
                  if e["session_id"] == "sv-worker")
    assert worker["role_at_join"] == "member"


def test_backfill_session_history_idempotent():
    doc = _legacy_trek_doc()
    first = trek.backfill_session_history(doc)
    second = trek.backfill_session_history(doc)
    assert first == 3
    assert second == 0
    assert len(doc["session_history"]) == 3


def test_backfill_session_history_skips_empty_task_state_ids():
    doc = _legacy_trek_doc()
    # Drop the populated task_states entry; only the empty one remains.
    doc["task_states"] = {"e-101": {"state": "todo"}}
    added = trek.backfill_session_history(doc)
    # Leader + halt = 2; worker was the only task_states sid and it's gone.
    assert added == 2
    sids = {e["session_id"] for e in doc["session_history"]}
    assert sids == {"sv-leader", "sv-halt"}


def test_backfill_session_history_uses_task_state_timestamp():
    doc = _legacy_trek_doc()
    trek.backfill_session_history(doc)
    worker = next(e for e in doc["session_history"]
                  if e["session_id"] == "sv-worker")
    assert worker["joined_at"] == "2026-01-04T00:00:00Z"


def test_backfill_session_history_uses_halt_timestamp():
    doc = _legacy_trek_doc()
    trek.backfill_session_history(doc)
    halt = next(e for e in doc["session_history"]
                if e["session_id"] == "sv-halt")
    assert halt["joined_at"] == "2026-01-03T00:00:00Z"


# ---------------------------------------------------------------------------
# trek_store lazy hydration
# ---------------------------------------------------------------------------

def test_load_trek_lazy_hydrates_legacy_doc(tmp_trek_dir):
    # Write a legacy doc directly (no session_history) and confirm
    # load_trek returns it hydrated.
    legacy = _legacy_trek_doc()
    legacy["trek_id"] = "tk-load-test"
    path = tmp_trek_dir / "tk-load-test.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = trek_store.load_trek("tk-load-test")
    assert loaded is not None
    history = loaded.get("session_history") or []
    assert len(history) == 3
    assert {e["session_id"] for e in history} == {
        "sv-leader", "sv-halt", "sv-worker",
    }


def test_load_trek_does_not_persist_hydration(tmp_trek_dir):
    """Read-only hydration: on-disk file should still be the legacy shape
    until something explicitly saves it back."""
    legacy = _legacy_trek_doc()
    legacy["trek_id"] = "tk-noperist"
    path = tmp_trek_dir / "tk-noperist.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    trek_store.load_trek("tk-noperist")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "session_history" not in on_disk or not on_disk["session_history"]


def test_load_trek_preserves_existing_history(tmp_trek_dir):
    """If session_history is already populated, lazy hydration is a no-op."""
    doc = {
        "trek_id": "tk-already",
        "members": [],
        "leader_session_id": "sv-leader",
        "session_history": [{
            "session_id": "sv-leader", "user_id": "u-1",
            "email": "creator@x",
            "joined_at": "2026-01-01T00:00:00Z",
            "role_at_join": "leader",
        }],
        "task_states": {},
        "scope": [],
        "status": "planning",
        "type": "persistent",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    path = tmp_trek_dir / "tk-already.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    loaded = trek_store.load_trek("tk-already")
    assert loaded is not None
    assert len(loaded["session_history"]) == 1
    assert loaded["session_history"][0]["session_id"] == "sv-leader"


# ---------------------------------------------------------------------------
# find_session_history
# ---------------------------------------------------------------------------

def test_find_session_history_returns_entry(base_trek):
    entry = trek.find_session_history(base_trek, "sv-leader-1")
    assert entry is not None
    assert entry["role_at_join"] == "leader"


def test_find_session_history_missing(base_trek):
    assert trek.find_session_history(base_trek, "sv-nope") is None


def test_find_session_history_empty_session_id(base_trek):
    assert trek.find_session_history(base_trek, "") is None
