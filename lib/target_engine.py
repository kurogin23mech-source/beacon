"""Beacon generic target engine — create / advance-phase / close / list a
descriptor-defined target-class (ms-122 e-3956).

``target_descriptor`` defines the SHAPE of a data-defined target-class (name,
type, fields, phases). This module is the generic MECHANICS that operate on an
instance of such a class: allocate an id, build the record, move it through its
declared phases, close it, list them. It reads the descriptor for VOCABULARY
(id prefix, phase order, field names) and delegates every occupation-agnostic
primitive to ``work_base`` / ``work_model`` (id allocation, actor/time stamps,
audit rows, the done stamp). "機構は基底 / 語彙は記述子" made concrete: nothing
here knows what a contract or an evaluation *is* — only that a target has an id,
a label, a status, an ordered phase list, and an append-only phase history.

Records for a data-defined class live under the descriptor's ``collection`` key
in project.json (e.g. ``contracts``). Reads are tolerant (missing collection =
no targets); writes append to the collection so the schema-evolution compat
contract (memo pnhATs37xgIxEkpFI8uR) holds. This module performs no I/O: it
mutates the ``data`` dict it is handed; persistence (``save_project``) is the
CLI layer's job.

The ms-119 review gate (``beacon target review-request/approve``) is a SEPARATE
surface that gates a target's *completion* transition through human approval;
it is orthogonal to these mechanics and is not invoked here. Wiring these verbs
into the occupation registry (so ``project_targets`` projects data-defined
targets alongside milestones / opportunities) is task e-3957.
"""

from __future__ import annotations

from typing import Optional

import work_base
import work_model
import target_descriptor as td


class TargetEngineError(ValueError):
    """Raised when a generic target operation cannot proceed (unknown field,
    unknown phase, already at final phase, target not found). Carries a
    human-facing message; the CLI prints it and exits non-zero."""


# ---------------------------------------------------------------------------
# Collection access — tolerant reads, create-on-write.
# ---------------------------------------------------------------------------

def _collection(data: dict, desc: dict, *, create: bool = False) -> list:
    coll = (desc.get("collection") or "").strip()
    if not coll:
        raise TargetEngineError(
            f"記述子 '{desc.get('kind')}' に collection が未設定です")
    if create:
        existing = data.get(coll)
        if not isinstance(existing, list):
            existing = []
            data[coll] = existing
        return existing
    got = data.get(coll)
    return got if isinstance(got, list) else []


def list_targets(data: dict, desc: dict) -> list:
    """Return every target record of this class (raw records from the
    descriptor's collection, in stored order). Empty when the collection is
    absent."""
    return list(_collection(data, desc))


def find_target(data: dict, desc: dict, target_id: str) -> Optional[dict]:
    """Return the target record with ``target_id`` in this class's collection,
    or ``None``."""
    want = (target_id or "").strip()
    for rec in _collection(data, desc):
        if isinstance(rec, dict) and rec.get("id") == want:
            return rec
    return None


# ---------------------------------------------------------------------------
# Create.
# ---------------------------------------------------------------------------

def create_target(data: dict, desc: dict, *, label: str,
                  fields: Optional[dict] = None, actor: str = "",
                  at: str = "") -> dict:
    """Create a target of this descriptor's class and append it to the
    collection. Allocates the next id under the descriptor's ``id_prefix``,
    builds the generic skeleton via ``work_model.new_target`` (id / label /
    status / created_at / created_by), sets the initial phase to the first
    declared phase (if any), and stores the given field values.

    ``fields`` values are accepted only for keys the descriptor declares (a base
    field, or a field of the initial phase); an undeclared key raises. A base
    field marked ``required`` that is missing also raises — the descriptor's
    structure is enforced at the point of creation, not by prompt convention."""
    if not (label or "").strip():
        raise TargetEngineError("label は必須です")
    coll = _collection(data, desc, create=True)
    ids = [r.get("id", "") for r in coll if isinstance(r, dict)]
    new_id = work_base.next_suffixed_id(ids, desc.get("id_prefix", ""))

    phases = td.phase_keys(desc)
    initial_phase = phases[0] if phases else ""
    declared = {f.get("key") for f in td.fields_at_phase(desc, initial_phase)}

    field_vals: dict = {}
    for key, val in (fields or {}).items():
        if key not in declared:
            raise TargetEngineError(
                f"未知の field '{key}' です (記述子 '{desc.get('kind')}' に宣言が"
                f"ありません)")
        field_vals[key] = val

    # Required BASE fields must be present at create (per-phase required fields
    # are checked when their phase is reached, not here).
    for f in td.base_fields(desc):
        if f.get("required") and not (field_vals.get(f.get("key")) or "") \
                and field_vals.get(f.get("key")) not in (0, False):
            raise TargetEngineError(
                f"必須 field '{f.get('key')}' ({f.get('label') or f.get('key')}) "
                f"が未指定です")

    extra: dict = {"kind": desc.get("kind")}
    if initial_phase:
        extra["phase"] = initial_phase
    extra.update(field_vals)

    rec = work_model.new_target(new_id, label, created_by=actor, created_at=at,
                                **extra)
    rec["phase_history"] = []
    coll.append(rec)
    return rec


# ---------------------------------------------------------------------------
# Advance phase.
# ---------------------------------------------------------------------------

def current_phase(rec: dict) -> str:
    """Return a target record's current phase key, or ``""``."""
    return (rec.get("phase") or "") if isinstance(rec, dict) else ""


def advance_target(data: dict, desc: dict, target_id: str, *,
                  to_phase: str = "", actor: str = "",
                  reason: str = "") -> tuple:
    """Move a target to its next declared phase (or to ``to_phase`` when given)
    and record the change on its append-only ``phase_history``. Returns
    ``(record, old_phase, new_phase)``.

    Without ``to_phase`` the target advances to the phase immediately after its
    current one in declaration order; advancing past the final phase raises
    (the target is complete — use ``close_target``). ``to_phase`` must be a
    declared phase; it may move forward OR back (a phase can be re-opened, e.g.
    a contract kicked back from 締結 to 弁護士レビュー) — the engine records the
    transition rather than policing direction, matching Beacon's "transitions
    are permissive, the human is the master" stance."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    phases = td.phase_keys(desc)
    if not phases:
        raise TargetEngineError(
            f"記述子 '{desc.get('kind')}' は phase を持たないため phase 進行できません")

    old = current_phase(rec) or phases[0]
    if to_phase:
        want = to_phase.strip()
        if want not in phases:
            raise TargetEngineError(
                f"未知の phase '{want}' です (宣言済: {' / '.join(phases)})")
        new = want
    else:
        try:
            idx = phases.index(old)
        except ValueError:
            idx = -1
        if idx >= len(phases) - 1:
            raise TargetEngineError(
                f"{target_id} は既に最終 phase '{old}' です "
                f"(完了は beacon target close)")
        new = phases[idx + 1]

    rec["phase"] = new
    history = rec.setdefault("phase_history", [])
    work_base.record_audit_event(history, kind="phase_change", actor=actor,
                                 reason=reason, **{"from": old, "to": new})
    return rec, old, new


def is_terminal_phase(desc: dict, phase_key: str) -> bool:
    """True when ``phase_key`` is a phase the descriptor flags ``terminal`` —
    reaching it means the target's work is finished (the CLI can suggest
    ``close`` at that point)."""
    return phase_key in td.terminal_phase_keys(desc)


# ---------------------------------------------------------------------------
# Close.
# ---------------------------------------------------------------------------

def close_target(data: dict, desc: dict, target_id: str, *, actor: str = "",
                 reason: str = "") -> dict:
    """Mark a target done (via the shared ``work_model.mark_done`` — stamps
    status=done + done_at + done_by/done_reason). Idempotent-safe: closing an
    already-done target re-stamps the done metadata. Returns the record."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    work_model.mark_done(rec, actor=actor, reason=reason)
    return rec


# ---------------------------------------------------------------------------
# Projection — the shared-frame shape a descriptor-defined target presents.
# ---------------------------------------------------------------------------

def project_target(desc: dict, rec: dict) -> dict:
    """Return the occupation-agnostic shared-frame projection of one record:
    ``id`` / ``label`` / ``status`` / ``kind`` / ``phase``. This mirrors the
    shape ``core.project_targets`` / ``sales_entities.project_targets`` emit, so
    when e-3957 wires data-defined classes into the registry the shared frame
    (session-start / status) can show them beside milestones / opportunities
    without special-casing."""
    return {
        "id": rec.get("id", ""),
        "label": work_model.target_label(rec),
        "status": work_model.work_item_status(rec),
        "kind": rec.get("kind") or desc.get("kind", ""),
        "phase": current_phase(rec),
    }
