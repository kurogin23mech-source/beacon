"""Unit tests for lib/cycle.py — cycle activation judgement.

Covers:
  - hard-signal-only activation (each cycle gates on its concrete record)
  - cycle_last_action_date returns the latest known timestamp per cycle
  - list_active_cycles / list_inactive_cycles preserve canonical order
  - cycle_status_snapshot covers every cycle in CYCLES
  - unknown cycle raises ValueError (not silent False)

All tests are pure data — no I/O, no fixtures beyond plain dicts — because
cycle.py is itself pure-function code.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cycle  # noqa: E402


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def empty_project() -> dict:
    """The bare-minimum project shape with every cycle off."""
    return {
        "name": "test",
        "milestones": [],
        "summary": "",
    }


def push_record(date: str = "2026-05-01T10:00:00Z") -> dict:
    return {
        "id": "p-1",
        "branch": "main",
        "from_hash": "aaaa111",
        "to_hash": "bbbb222",
        "commit_count": 3,
        "pushed_at": date,
    }


def deploy_record(date: str = "2026-05-02T10:00:00Z", key: str = "deployments") -> dict:
    """Build a deploy record. `key` selects the project-level field name to
    place it under — used by tests verifying the legacy ``deploys`` fallback."""
    return {
        "id": "d-1",
        "service": "beacon-api-prod",
        "deployed_at": date,
    }


def operation(status: str = "open", op_id: str = "op-1",
              opened_at: str = "2026-05-03T10:00:00Z",
              entries: list | None = None) -> dict:
    return {
        "id": op_id,
        "title": "Test op",
        "status": status,
        "opened_at": opened_at if status == "open" else None,
        "schedule": {"frequency": "daily"},
        "entries": entries or [],
    }


def retro_doc(date: str = "2026-05-04T10:00:00Z") -> dict:
    return {
        "doc_id": "retro-2026-W18",
        "title": "Weekly Retro",
        "scope": "retro",
        "updated_at": date,
    }


def version_rules_doc() -> dict:
    return {
        "doc_id": "version-rules",
        "title": "Version Rules",
        "scope": "core",
        "updated_at": "2026-04-01T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Push cycle
# ---------------------------------------------------------------------------

def test_push_inactive_on_empty_project():
    """No pushes recorded — push cycle must not activate."""
    assert cycle.is_cycle_active(empty_project(), "push") is False


def test_push_active_with_one_push():
    """A single push record is enough to flip activation on (hard signal)."""
    proj = empty_project()
    proj["pushes"] = [push_record()]
    assert cycle.is_cycle_active(proj, "push") is True


def test_push_last_action_date_returns_latest():
    """When multiple pushes exist, the most recent date wins."""
    proj = empty_project()
    proj["pushes"] = [
        push_record(date="2026-05-01T10:00:00Z"),
        push_record(date="2026-05-15T10:00:00Z"),
        push_record(date="2026-05-10T10:00:00Z"),
    ]
    assert cycle.cycle_last_action_date(proj, "push") == "2026-05-15T10:00:00Z"


def test_push_last_action_falls_back_to_created_at():
    """Older push records may only have ``created_at``; pick that up too."""
    proj = empty_project()
    proj["pushes"] = [{"id": "p-x", "created_at": "2026-01-01T00:00:00Z"}]
    assert cycle.cycle_last_action_date(proj, "push") == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Deploy cycle
# ---------------------------------------------------------------------------

def test_deploy_inactive_on_empty_project():
    assert cycle.is_cycle_active(empty_project(), "deploy") is False


def test_deploy_active_via_deployments_key():
    """`deployments` is the canonical key — must activate."""
    proj = empty_project()
    proj["deployments"] = [deploy_record()]
    assert cycle.is_cycle_active(proj, "deploy") is True


def test_deploy_active_via_legacy_deploys_key():
    """Defensive fallback: older data writes ``deploys`` (short form)."""
    proj = empty_project()
    proj["deploys"] = [deploy_record(key="deploys")]
    assert cycle.is_cycle_active(proj, "deploy") is True


# ---------------------------------------------------------------------------
# Retro cycle
# ---------------------------------------------------------------------------

def test_retro_inactive_without_any_retro_doc():
    """Even with other-scope docs around, retro must require a retro doc."""
    proj = empty_project()
    docs = [
        {"doc_id": "x", "scope": "core", "updated_at": "2026-05-01"},
        {"doc_id": "y", "scope": "spec", "updated_at": "2026-05-01"},
    ]
    assert cycle.is_cycle_active(proj, "retro", documents=docs) is False


def test_retro_active_with_one_retro_doc():
    proj = empty_project()
    docs = [retro_doc()]
    assert cycle.is_cycle_active(proj, "retro", documents=docs) is True


def test_retro_documents_inline_on_project_fallback():
    """Tests may inline documents on the project dict; that must still work."""
    proj = empty_project()
    proj["documents"] = [retro_doc()]
    # No explicit `documents=` arg — function should fall back to project key.
    assert cycle.is_cycle_active(proj, "retro") is True


def test_retro_last_action_picks_latest():
    proj = empty_project()
    docs = [
        retro_doc(date="2026-04-15T00:00:00Z"),
        retro_doc(date="2026-05-30T00:00:00Z"),
        retro_doc(date="2026-05-01T00:00:00Z"),
    ]
    assert cycle.cycle_last_action_date(proj, "retro", documents=docs) == "2026-05-30T00:00:00Z"


# ---------------------------------------------------------------------------
# Operation cycle
# ---------------------------------------------------------------------------

def test_operation_inactive_when_only_todo_operations():
    """Operations in todo/in_progress do not count — only `open` is hard signal."""
    proj = empty_project()
    proj["operations"] = [
        operation(status="todo"),
        operation(status="in_progress", op_id="op-2"),
    ]
    assert cycle.is_cycle_active(proj, "operation") is False


def test_operation_active_when_one_open_operation():
    proj = empty_project()
    proj["operations"] = [operation(status="open")]
    assert cycle.is_cycle_active(proj, "operation") is True


def test_operation_last_action_from_run_records():
    """Most recent run_record timestamp wins over opened_at."""
    proj = empty_project()
    proj["operations"] = [
        operation(
            status="open",
            opened_at="2026-04-01T00:00:00Z",
            entries=[
                {"type": "run_record", "date": "2026-05-10T00:00:00Z"},
                {"type": "run_record", "date": "2026-05-20T00:00:00Z"},
            ],
        )
    ]
    assert cycle.cycle_last_action_date(proj, "operation") == "2026-05-20T00:00:00Z"


def test_operation_last_action_falls_back_to_opened_at():
    """If no run records exist yet, opened_at is the fallback."""
    proj = empty_project()
    proj["operations"] = [operation(status="open", opened_at="2026-04-01T00:00:00Z")]
    assert cycle.cycle_last_action_date(proj, "operation") == "2026-04-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Release cycle
# ---------------------------------------------------------------------------

def test_release_inactive_on_empty_project():
    assert cycle.is_cycle_active(empty_project(), "release") is False


def test_release_active_via_version_rules_field():
    """Future schema: project-level ``version_rules`` is the signal."""
    proj = empty_project()
    proj["version_rules"] = {"push": {"enabled": True}}
    assert cycle.is_cycle_active(proj, "release") is True


def test_release_active_via_version_rules_doc():
    """Current schema: the CORE doc `version-rules` opts in (legacy mechanism)."""
    proj = empty_project()
    docs = [version_rules_doc()]
    assert cycle.is_cycle_active(proj, "release", documents=docs) is True


def test_release_active_via_releases_history():
    """Released history is also a hard signal."""
    proj = empty_project()
    proj["releases"] = [{"id": "r-1", "version": "v0.1.0", "released_at": "2026-05-01"}]
    assert cycle.is_cycle_active(proj, "release") is True


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------

def test_list_active_cycles_preserves_canonical_order():
    """Active list must follow CYCLES tuple order — UI relies on this."""
    proj = empty_project()
    proj["pushes"] = [push_record()]
    proj["operations"] = [operation(status="open")]
    docs = [retro_doc()]
    active = cycle.list_active_cycles(proj, documents=docs)
    # push and retro and operation are active; deploy / release are not.
    assert active == ["push", "retro", "operation"]


def test_list_inactive_cycles_complements_active():
    proj = empty_project()
    proj["pushes"] = [push_record()]
    active = cycle.list_active_cycles(proj)
    inactive = cycle.list_inactive_cycles(proj)
    # Together they must enumerate every known cycle exactly once.
    assert sorted(active + inactive) == sorted(cycle.CYCLES)


def test_cycle_status_snapshot_covers_every_cycle():
    """Snapshot must produce one entry per CYCLES — protects against
    accidental drift between CYCLES tuple and dispatch tables."""
    snap = cycle.cycle_status_snapshot(empty_project())
    assert set(snap.keys()) == set(cycle.CYCLES)
    for c in cycle.CYCLES:
        assert snap[c]["active"] is False
        assert snap[c]["last_action_date"] is None


def test_snapshot_reflects_partial_activation():
    """Snapshot accurately mixes active / inactive entries."""
    proj = empty_project()
    proj["pushes"] = [push_record(date="2026-05-10T00:00:00Z")]
    snap = cycle.cycle_status_snapshot(proj)
    assert snap["push"] == {
        "active": True,
        "last_action_date": "2026-05-10T00:00:00Z",
    }
    assert snap["deploy"]["active"] is False


# ---------------------------------------------------------------------------
# Unknown cycle handling
# ---------------------------------------------------------------------------

def test_is_cycle_active_rejects_unknown_cycle():
    """Typos in caller code should fail loudly, not silently return False."""
    with pytest.raises(ValueError):
        cycle.is_cycle_active(empty_project(), "psh")


def test_last_action_date_rejects_unknown_cycle():
    with pytest.raises(ValueError):
        cycle.cycle_last_action_date(empty_project(), "psh")


# ---------------------------------------------------------------------------
# Monotonicity (the "never disembark" rule)
# ---------------------------------------------------------------------------

def test_snapshot_payload_serializes_to_json():
    """Ensures the snapshot dict is JSON-clean (used by `beacon cycle status --json`).

    cmd_cycle_status pipes this through json.dumps; any non-serializable type
    here would break the CLI silently in Skill contexts. Catch it at the test
    layer instead of in production logs.
    """
    import json as _json
    proj = empty_project()
    proj["pushes"] = [push_record()]
    proj["operations"] = [operation(status="open")]
    snap = cycle.cycle_status_snapshot(proj)
    # Round-trip through JSON to assert serializability.
    s = _json.dumps(snap, ensure_ascii=False)
    restored = _json.loads(s)
    assert restored["push"]["active"] is True
    assert restored["operation"]["active"] is True


def test_activation_is_monotonic_in_records():
    """Once activated, removing records is the only way to flip back.
    We model the design intent — never automatically deactivate — but we
    cannot enforce it inside is_cycle_active alone; this test serves as
    documentation that the predicate is purely a function of current state."""
    proj = empty_project()
    assert cycle.is_cycle_active(proj, "push") is False
    proj["pushes"] = [push_record()]
    assert cycle.is_cycle_active(proj, "push") is True
    # Removing all records re-flips. (Future: track "ever active" flag.)
    proj["pushes"] = []
    assert cycle.is_cycle_active(proj, "push") is False
