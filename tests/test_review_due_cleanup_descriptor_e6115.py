"""Regression: review-due trigger cleanup covers descriptor-class targets, not
just milestones + operations (ms-164 e-6115).

`_fire_review_due_trigger` (commands_shared) fires a `review-due-<target_id>.json`
for a milestone / operation AND a data-defined (descriptor) target reaching a
terminal phase. But `cmd_trigger._cleanup_review_due_triggers` built its
`status_by_id` / `pending_targets` from a hardcoded
`data["milestones"] + data["operations"]`, so a descriptor target's id was
absent — its still-live review-due trigger was mis-read as "target gone" and
removed spuriously (the AC3 blind spot). This locks in that the cleanup now
enumerates via `occupation.iter_target_records` (all target classes), so a
descriptor target's trigger survives while the target is live, and is still
removed once the target genuinely disappears.
"""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import cmd_trigger  # noqa: E402

CONTRACT = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
    "phases": [
        {"key": "drafting", "label": "起草"},
        {"key": "signed", "label": "締結", "terminal": True},
    ],
}


def _setup(tmp_path, monkeypatch, *, target_exists=True, pending=True):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    contracts = []
    if target_exists:
        entries = []
        if pending:
            entries.append({
                "type": "target-transition-approval",
                "meta": {"approval_status": "pending", "target_id": "ctr-1"},
            })
        contracts = [{"id": "ctr-1", "status": "signed", "entries": entries}]
    (beacon / "project.json").write_text(json.dumps({
        "name": "t", "milestones": [], "operations": [],
        "target_classes": [CONTRACT], "contracts": contracts,
    }, ensure_ascii=False))
    (beacon / "triggers").mkdir()
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    return beacon


def _write_review_due(beacon, target_id, *, gated=True, created_at=None):
    trig = {
        "name": f"review-due-{target_id}", "kind": "review-due",
        "target_id": target_id, "gated": gated,
        "created_at": created_at or datetime.datetime.now().isoformat(),
    }
    (beacon / "triggers" / f"review-due-{target_id}.json").write_text(
        json.dumps(trig, ensure_ascii=False))


def _exists(beacon, target_id):
    return (beacon / "triggers" / f"review-due-{target_id}.json").exists()


def test_descriptor_target_review_due_survives_while_live(tmp_path, monkeypatch):
    """A gated review-due for a LIVE descriptor target with a pending approval
    must NOT be swept — the pre-e-6115 hardcode dropped it as 'target gone'."""
    beacon = _setup(tmp_path, monkeypatch, target_exists=True, pending=True)
    _write_review_due(beacon, "ctr-1", gated=True)
    cmd_trigger._cleanup_review_due_triggers()
    assert _exists(beacon, "ctr-1"), (
        "descriptor target's in-flight review-due was swept — cleanup did not "
        "enumerate the descriptor class")


def test_descriptor_target_review_due_removed_when_target_gone(tmp_path, monkeypatch):
    """The genuine 'target gone' case still clears the trigger (the fix must not
    over-correct into never-removing)."""
    beacon = _setup(tmp_path, monkeypatch, target_exists=False)
    _write_review_due(beacon, "ctr-1", gated=True)
    cmd_trigger._cleanup_review_due_triggers()
    assert not _exists(beacon, "ctr-1")
