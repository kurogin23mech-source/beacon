"""Unit tests for ms-97 / e-2571 Migration + Archive helpers (lib/trek.py).

Covers:
- ``stamp_legacy_project_wide_scope`` + ``unstamp_legacy_project_wide_scope``
  + ``is_legacy_project_wide_scope`` round-trip (= AC8 + AC31)
- ``find_unhealthy_members`` for partial migration (= AC34 + Migration
  リスク + Rollback 戦略 §5 alarming surface)

These pin the marker contract so the CLI / Web UI / future cloud-mode
lazy-migration path can pivot on it without re-walking scope on every
read.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# AC8 + AC31 — stamp_legacy_project_wide_scope / unstamp / is_legacy_…
# ---------------------------------------------------------------------------

def test_stamp_legacy_scope_stamps_when_project_wide_entry_present():
    doc = {
        "scope": [
            {"project": "p1"},
            {"project": "p2", "milestone": "ms-1"},
        ],
    }
    changed = trek.stamp_legacy_project_wide_scope(doc)
    assert changed is True
    assert doc["meta"][trek.LEGACY_PROJECT_WIDE_SCOPE_META_KEY] is True
    assert trek.is_legacy_project_wide_scope(doc) is True


def test_stamp_legacy_scope_noop_when_all_narrow():
    doc = {
        "scope": [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "task": "e-9"},
        ],
    }
    changed = trek.stamp_legacy_project_wide_scope(doc)
    assert changed is False
    assert "meta" not in doc or trek.LEGACY_PROJECT_WIDE_SCOPE_META_KEY \
        not in doc.get("meta", {})
    assert trek.is_legacy_project_wide_scope(doc) is False


def test_stamp_legacy_scope_is_idempotent():
    """Stamping a doc that already carries the marker is a no-op."""
    doc = {"scope": [{"project": "p1"}]}
    trek.stamp_legacy_project_wide_scope(doc)
    first_ts = doc["updated_at"]
    changed = trek.stamp_legacy_project_wide_scope(doc)
    assert changed is False
    # updated_at must not be bumped on the no-op re-stamp.
    assert doc["updated_at"] == first_ts


def test_unstamp_removes_marker_and_bumps_timestamp():
    doc = {"scope": [{"project": "p1"}]}
    trek.stamp_legacy_project_wide_scope(doc)
    first_ts = doc["updated_at"]
    # Ensure timestamps differ (utcnow_iso has microsecond resolution but
    # we sleep zero — instead clear the marker and rely on a fresh stamp).
    changed = trek.unstamp_legacy_project_wide_scope(doc)
    assert changed is True
    assert trek.is_legacy_project_wide_scope(doc) is False
    # On unstamp the meta key is removed; if meta becomes empty it is
    # also removed so the on-disk schema stays tight.
    assert "meta" not in doc or trek.LEGACY_PROJECT_WIDE_SCOPE_META_KEY \
        not in doc.get("meta", {})
    # updated_at must change (different from the stamp timestamp).
    assert doc["updated_at"] != first_ts or first_ts == ""


def test_unstamp_is_noop_when_marker_absent():
    doc = {"scope": [{"project": "p1", "milestone": "ms-1"}]}
    changed = trek.unstamp_legacy_project_wide_scope(doc)
    assert changed is False
    assert "meta" not in doc


def test_unstamp_preserves_other_meta_fields():
    """Unstamp must not nuke unrelated meta keys."""
    doc = {
        "scope": [{"project": "p1"}],
        "meta": {"cadence_minutes": 5, "other_marker": True},
    }
    trek.stamp_legacy_project_wide_scope(doc)
    trek.unstamp_legacy_project_wide_scope(doc)
    assert doc["meta"].get("cadence_minutes") == 5
    assert doc["meta"].get("other_marker") is True


def test_is_legacy_returns_false_for_empty_doc():
    assert trek.is_legacy_project_wide_scope({}) is False
    assert trek.is_legacy_project_wide_scope({"meta": {}}) is False


# ---------------------------------------------------------------------------
# AC34 + Migration リスク §5 — find_unhealthy_members alarming surface
# ---------------------------------------------------------------------------

def test_find_unhealthy_members_empty_for_clean_doc():
    doc = {"members": [
        {"user_id": "u1", "session_id": "s1"},
        {"user_id": "u2", "session_id": "s2"},
    ]}
    assert trek.find_unhealthy_members(doc) == []


def test_find_unhealthy_members_surfaces_partial_migration():
    """Members without session_id are flagged (= partial migration)."""
    doc = {"members": [
        {"user_id": "u1", "session_id": "s1"},
        {"user_id": "u-orphan"},  # no session_id
    ]}
    unhealthy = trek.find_unhealthy_members(doc)
    assert len(unhealthy) == 1
    assert unhealthy[0]["user_id"] == "u-orphan"


def test_find_unhealthy_members_surfaces_stale_session():
    """Members whose session is no longer live are flagged."""
    doc = {"members": [
        {"user_id": "u1", "session_id": "s-live"},
        {"user_id": "u2", "session_id": "s-stale"},
    ]}
    unhealthy = trek.find_unhealthy_members(
        doc, live_session_ids=["s-live"],
    )
    assert len(unhealthy) == 1
    assert unhealthy[0]["user_id"] == "u2"


def test_find_unhealthy_members_combines_partial_and_stale():
    doc = {"members": [
        {"user_id": "u1", "session_id": "s-live"},
        {"user_id": "u2", "session_id": "s-stale"},
        {"user_id": "u-orphan"},
    ]}
    unhealthy = trek.find_unhealthy_members(
        doc, live_session_ids=["s-live"],
    )
    flagged = {m["user_id"] for m in unhealthy}
    assert flagged == {"u2", "u-orphan"}


def test_find_unhealthy_members_empty_for_empty_members():
    assert trek.find_unhealthy_members({}) == []
    assert trek.find_unhealthy_members({"members": []}) == []


def test_find_unhealthy_members_preserves_order():
    """Iteration order must match members[] order so CLI output is stable."""
    doc = {"members": [
        {"user_id": "u-a"},
        {"user_id": "u-b", "session_id": "s-b"},
        {"user_id": "u-c"},
    ]}
    unhealthy = trek.find_unhealthy_members(doc)
    assert [m["user_id"] for m in unhealthy] == ["u-a", "u-c"]
