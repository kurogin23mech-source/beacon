"""ms-106 e-3504 — server-side ``_fire_operation`` must wake the Skill named by
the Operation's ``meta.execute_skill`` (defaulting to ``beacon-operation-execute``).

This is what lets one tick primitive drive both dev monitoring
(``beacon-operation-execute``) and the sales reply-watch
(``beacon-sales-reply-watch``, detection-only) from the same seam, instead of
every Operation hard-wiring to the dev log-review Skill.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import app  # noqa: E402


def _capture_fire(monkeypatch, op):
    """Run ``_fire_operation`` with a stubbed active envelope + bus sink and
    return the captured ``bus_data`` dict (or None if nothing was appended)."""
    monkeypatch.setattr(
        app.db, "list_operation_envelopes",
        lambda pid, op_id=None, status=None: [{"envelope": {"stub": True}}],
    )
    captured = {}
    monkeypatch.setattr(
        app.db, "append_bus_event",
        lambda pid, bus_data: captured.setdefault("bus_data", bus_data),
    )
    project = {"id": "p1", "operations": [op], "documents": []}
    result = app._fire_operation("p1", project, op, "2026-07-15T00:00:00Z")
    return result, captured.get("bus_data")


def test_default_operation_targets_operation_execute(monkeypatch):
    op = {"id": "op-1", "status": "open", "meta": {"server_tick": True}}
    result, bus = _capture_fire(monkeypatch, op)
    assert result["fired"] is True
    assert bus["payload"]["execute_skill"] == "beacon-operation-execute"
    assert "/beacon-operation-execute" in bus["payload"]["message"]


def test_reply_watch_operation_targets_reply_watch_skill(monkeypatch):
    op = {"id": "op-2", "status": "open",
          "meta": {"server_tick": True,
                   "execute_skill": "beacon-sales-reply-watch"}}
    result, bus = _capture_fire(monkeypatch, op)
    assert result["fired"] is True
    assert bus["payload"]["execute_skill"] == "beacon-sales-reply-watch"
    assert "/beacon-sales-reply-watch" in bus["payload"]["message"]
    # dev-execute must NOT be the target
    assert "/beacon-operation-execute" not in bus["payload"]["message"]


def test_blank_execute_skill_falls_back_to_default(monkeypatch):
    op = {"id": "op-3", "status": "open",
          "meta": {"server_tick": True, "execute_skill": "  "}}
    result, bus = _capture_fire(monkeypatch, op)
    assert result["fired"] is True
    assert bus["payload"]["execute_skill"] == "beacon-operation-execute"
