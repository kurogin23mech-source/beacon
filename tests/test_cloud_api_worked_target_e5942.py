"""Regression: the cloud API preserves worked-Target attribution on session
logs and notes (ms-164 e-5942 / e-5943, cloud API leg — SPEC 実装順序2).

The CLI stamps `target_ids` / `target_id` / `target_source` on the session log
(session_log.aggregate_session) and `target_ids` / `target_id` on a note
(cmd_note) and POSTs the body verbatim. The server Pydantic models
(`SessionLogUpsert` / `NoteCreate`) did NOT declare those fields, and with the
default `extra="ignore"` they were silently dropped before persistence — so a
cloud-mode project's session log / note was reachable from the root but NOT from
each worked child Target (AC1 dead in cloud). This locks in that the models now
declare the attribution fields (so `upsert_session_log`'s `model_dump()` payload
carries them) and that the empty-list "no active target" case is representable.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import routers_projects as rp  # noqa: E402


def _session_payload(model):
    """Mirror the handler's persist projection (upsert_session_log line ~3194)."""
    return {k: v for k, v in model.model_dump().items() if v is not None}


def test_session_log_upsert_carries_worked_target_attribution():
    m = rp.SessionLogUpsert(
        summary="s", target_ids=["ms-1", "opp-2"], target_id="ms-1",
        target_source="fork")
    payload = _session_payload(m)
    assert payload["target_ids"] == ["ms-1", "opp-2"]
    assert payload["target_id"] == "ms-1"
    assert payload["target_source"] == "fork"


def test_session_log_upsert_omits_attribution_when_absent():
    """A partial body (rescue / legacy client) that sends no attribution must
    not inject empty attribution keys (merge=True in the store)."""
    m = rp.SessionLogUpsert(summary="s")
    payload = _session_payload(m)
    assert "target_ids" not in payload
    assert "target_id" not in payload
    assert "target_source" not in payload


def test_note_create_declares_worked_target_fields():
    m = rp.NoteCreate(text="hi", target_ids=["ms-1"], target_id="ms-1")
    dumped = m.model_dump()
    assert dumped["target_ids"] == ["ms-1"]
    assert dumped["target_id"] == "ms-1"


def test_note_create_defaults_have_no_attribution():
    """Older clients / notes with no active target: target_ids defaults to None
    so the add_note handler's `if body.target_ids` guard drops the field (a doc
    is either tagged or has no field at all, symmetric with session_id)."""
    m = rp.NoteCreate(text="hi")
    assert m.target_ids is None
    # target_id defaults to None (unified with SessionLogUpsert.target_id — the
    # PR#718 AX+maintainability consensus finding: one zero-value across siblings).
    assert m.target_id is None
