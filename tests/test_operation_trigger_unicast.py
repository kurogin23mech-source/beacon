"""Tests for ms-76 / e-1860 / e-1604 — operation-trigger unicast default.

CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier framework)
section "構造的禁止" makes default-broadcast of operation-trigger events a
禁止帯 (= forbidden zone): default delivery MUST be unicast to a registered
claimer/owner, and broadcast requires explicit SPEC opt-in via the
BEACON_OPERATION_TRIGGER_BROADCAST=1 env flag.

These tests lock in the resolver semantics:
  * claimer_session_id (= claim-based registration, e-1604) takes precedence
  * fall back to open_by (= the session that opened the Operation) when
    no explicit claim is registered (= e-1860 default behavior)
  * empty string (= legacy broadcast) only when nothing is registered or
    when BEACON_OPERATION_TRIGGER_BROADCAST=1 is explicitly set
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


@pytest.fixture
def project_with_op(tmp_path, monkeypatch):
    """A bare beacon project with an Operation that has both claimer and
    owner metadata so each precedence rule can be exercised individually."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    project = {
        "name": "test",
        "milestones": [],
        "operations": [
            {
                "id": "op-1",
                "title": "Test Op",
                "status": "open",
                "log_source": "test_log",
                "schedule": {"frequency": "weekdays",
                             "days": ["mon", "tue", "wed", "thu", "fri"]},
                "entries": [],
                "meta": {
                    "claimer_session_id": "sv-claimer-123",
                    "open_by": "sv-opener-456",
                },
            },
        ],
    }
    (beacon_dir / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_OPERATION_TRIGGER_BROADCAST", raising=False)
    return tmp_path


def _set_op_meta(project_dir, **fields):
    """Mutate the Operation's meta block in project.json directly so the
    resolver re-reads them on the next call (load_project is uncached)."""
    pf = project_dir / ".beacon" / "project.json"
    data = json.loads(pf.read_text())
    op = data["operations"][0]
    op["meta"] = {**op.get("meta", {}), **fields}
    pf.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def test_resolver_prefers_claimer_over_owner(project_with_op):
    """e-1604 explicit claim wins over default owner — that's the whole
    point of claim-based receiver registration."""
    sid = commands._resolve_operation_trigger_recipient("op-1")
    assert sid == "sv-claimer-123"


def test_resolver_falls_back_to_open_by_when_no_claim(project_with_op):
    """e-1860 default: when no explicit claim, the session that opened the
    Operation is the implicit owner / unicast target."""
    _set_op_meta(project_with_op, claimer_session_id="")
    sid = commands._resolve_operation_trigger_recipient("op-1")
    assert sid == "sv-opener-456"


def test_resolver_returns_empty_when_neither_set(project_with_op):
    """Legacy broadcast fallback: pre-ms-76 projects without any owner/
    claimer recorded keep working (= no recipient field added → fan out)."""
    _set_op_meta(project_with_op, claimer_session_id="", open_by="")
    sid = commands._resolve_operation_trigger_recipient("op-1")
    assert sid == ""


def test_resolver_honours_broadcast_env_flag(project_with_op, monkeypatch):
    """Explicit opt-in: BEACON_OPERATION_TRIGGER_BROADCAST=1 returns empty
    even when a claimer is registered. This is the ms-76 SPEC 設計方針 7
    explicit opt-in pattern for Operations that legitimately broadcast
    (= release announcement, fleet-wide health alert)."""
    monkeypatch.setenv("BEACON_OPERATION_TRIGGER_BROADCAST", "1")
    sid = commands._resolve_operation_trigger_recipient("op-1")
    assert sid == ""


def test_resolver_returns_empty_for_unknown_op(project_with_op):
    """Defensive: unknown op-id resolves to empty (= legacy broadcast).
    Failing closed (= refuse to fire) would break the entire trigger
    pipeline; failing open (= broadcast) only widens delivery."""
    sid = commands._resolve_operation_trigger_recipient("op-nonexistent")
    assert sid == ""


def test_resolver_strips_whitespace(project_with_op):
    """A stray space in the recorded claimer must not look like a valid
    target — the server-side filter compares by string equality so any
    drift would silently drop deliveries."""
    _set_op_meta(project_with_op, claimer_session_id="   ", open_by="sv-x")
    sid = commands._resolve_operation_trigger_recipient("op-1")
    assert sid == "sv-x"
