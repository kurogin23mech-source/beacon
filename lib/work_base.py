"""Beacon work-base — occupation-agnostic primitives shared by every kind of
work instance (development milestones/tasks/commits, sales accounts/
opportunities/activities/communications, and future occupations).

This is the invariant floor of the ms-109 target-class fold (SPEC
p8bNPiWdlVW0lfjiBWqg, task e-3558). It knows only that a work record has an
``id``, a ``status``, and needs its state changes to stay traceable. It
deliberately carries NO occupation vocabulary — no phase, milestone,
opportunity, pipeline. Those semantics live in each instance's adapter
(``core.py`` for development, ``sales_entities.py`` for sales), per SPEC AC4
("基底コードは職種非依存 = the base code stays occupation-agnostic").

Extracted primitives (previously re-implemented on both sides):
  - now_iso()            — current UTC time, ISO8601 with seconds precision
  - current_actor()      — who is operating (claude / git email / OS user)
  - next_suffixed_id()   — allocate ``"<prefix><max_existing_suffix + 1>"``
  - stamp_cancel()       — soft-cancel a record (append-only, reason+actor+ts)
  - record_audit_event() — append an actor/reason/timestamp row to a log list

Like ``core.py`` this module performs no I/O: every function is a pure
transform over the values it is handed.
"""

from __future__ import annotations

import datetime as _dt


# ---------------------------------------------------------------------------
# Time / actor — the two stamps every traceable state change carries.
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as an ISO8601 string with seconds precision."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_actor() -> str:
    """Return the current operator: ``"claude"`` when running inside Claude
    Code (``BEACON_CLAUDE_CODE=1``), else the git ``user.email``, else the OS
    user name, else ``"unknown"``.

    This is the single definition of "who did this" that both the development
    and sales instances stamp onto their audit trails.
    """
    import os as _os
    if _os.environ.get("BEACON_CLAUDE_CODE") == "1":
        return "claude"
    try:
        import subprocess
        r = subprocess.run(["git", "config", "user.email"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return _os.environ.get("USER", _os.environ.get("USERNAME", "unknown"))


# ---------------------------------------------------------------------------
# ID allocation — ``<prefix><N>`` where N = max existing suffix + 1.
# ---------------------------------------------------------------------------

def next_suffixed_id(existing_ids, prefix: str) -> str:
    """Allocate the next ``"<prefix><N>"`` id given the ids already in use.

    ``N`` is the maximum integer suffix among ``existing_ids`` that carry
    ``prefix``, plus one (``1`` when none exist). Ids that don't start with
    ``prefix`` — or that do but whose suffix isn't an integer — are ignored,
    matching the historically forgiving development (``e-`` / ``op-``) and
    sales (``acc-`` / ``opp-`` / ``act-`` / ``nrt-`` / ``comm-``) allocators
    this replaces.

    The caller passes the id list; how those ids are gathered (a flat list, a
    recursive walk over nested entries) stays in the instance, because that
    shape is occupation-specific — the base doesn't know an ``opp-`` id means
    an opportunity.
    """
    if not prefix:
        raise ValueError("prefix must be a non-empty string")
    plen = len(prefix)
    max_id = 0
    for raw in existing_ids:
        if isinstance(raw, str) and raw.startswith(prefix):
            try:
                max_id = max(max_id, int(raw[plen:]))
            except ValueError:
                pass
    return f"{prefix}{max_id + 1}"


# ---------------------------------------------------------------------------
# Cancel vocabulary — correct by cancelling, never by deleting.
# ---------------------------------------------------------------------------

CANCELLED_STATUS = "cancelled"


def stamp_cancel(record: dict, *, reason: str = "", actor: str = "",
                 at: str = "") -> dict:
    """Soft-cancel a work record in place and return it.

    Sets ``status="cancelled"`` and stamps
    ``meta.cancelled_at / cancelled_by / cancel_reason``. The record is never
    physically removed — per ``data-immutability-principle`` (every state
    change stays traceable; evidence is not deleted). This is the single
    cancel vocabulary that both development (``core.task_delete``) and sales
    (activity / communication correction, task e-3537) route through, closing
    the sales-side hard-delete gap (SPEC AC3).

    ``actor`` / ``at`` fall back to ``current_actor()`` / ``now_iso()`` when
    left blank, so callers that don't thread identity still get an audited
    cancel.
    """
    record["status"] = CANCELLED_STATUS
    meta = record.setdefault("meta", {})
    meta["cancelled_at"] = at or now_iso()
    meta["cancelled_by"] = actor or current_actor()
    if reason:
        meta["cancel_reason"] = reason
    return record


def record_audit_event(log: list, *, kind: str, reason: str = "",
                       actor: str = "", at: str = "", **fields) -> dict:
    """Append one ``actor + reason + timestamp`` row to ``log`` and return it.

    A general-purpose audit row for state changes that want an event stream
    (rather than a stamp on the record itself, as ``stamp_cancel`` does).
    ``kind`` labels the change (e.g. ``"phase_change"``); ``**fields`` carry
    occupation-specific detail that the base stores verbatim without
    interpreting. ``actor`` / ``at`` fall back to ``current_actor()`` /
    ``now_iso()``. Append-only: never mutates an existing row.
    """
    if not kind:
        raise ValueError("kind must be a non-empty string")
    event = {
        "kind": kind,
        "actor": actor or current_actor(),
        "at": at or now_iso(),
    }
    if reason:
        event["reason"] = reason
    event.update(fields)
    log.append(event)
    return event
