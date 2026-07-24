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


# The child-arm keys a descriptor-driven target record carries as part of the
# thick cognitive frame (ms-124 e-4089). WorkItems are the unit of doing (a
# task / activity equivalent); Evidence is the append-only record of something
# that happened (a commit / communication equivalent). Both are ordinary
# collections declared as ``decomposition.arms`` so the storage layer can split
# them the same way it splits milestone.entries / opportunity.activities.
WORK_ITEMS_KEY = "work_items"
EVIDENCE_KEY = "evidence"
BALL_KEY = "who_has_the_ball"


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

    # Required BASE fields must be present at create. Required PHASE fields are
    # enforced separately, at advance_target when entering that phase (ms-124
    # e-4090) — you can't supply a later phase's field before you reach it.
    for f in td.base_fields(desc):
        if f.get("required") and not (field_vals.get(f.get("key")) or "") \
                and field_vals.get(f.get("key")) not in (0, False):
            raise TargetEngineError(
                f"必須 field '{f.get('key')}' ({f.get('label') or f.get('key')}) "
                f"が未指定です")

    extra: dict = {"kind": desc.get("kind")}
    if initial_phase:
        extra["phase"] = initial_phase
    # Thick-frame inheritance (ms-124 e-4089): a fresh target starts with the
    # ball in OUR court (there is a move we owe) and empty WorkItem / Evidence
    # arms. These are the same cognitive primitives a milestone or an
    # opportunity carries; a data-defined class inherits them rather than
    # projecting hardcoded zeros.
    extra["who_has_the_ball"] = work_model.BALL_SELF
    extra.update(field_vals)

    rec = work_model.new_target(new_id, label, created_by=actor, created_at=at,
                                **extra)
    rec["phase_history"] = []
    rec[WORK_ITEMS_KEY] = []
    rec[EVIDENCE_KEY] = []
    coll.append(rec)
    return rec


# ---------------------------------------------------------------------------
# Advance phase.
# ---------------------------------------------------------------------------

def current_phase(rec: dict) -> str:
    """Return a target record's current phase key, or ``""``."""
    return (rec.get("phase") or "") if isinstance(rec, dict) else ""


def advance_target(data: dict, desc: dict, target_id: str, *,
                  to_phase: str = "", fields: Optional[dict] = None,
                  actor: str = "", reason: str = "") -> tuple:
    """Move a target to its next declared phase (or to ``to_phase`` when given)
    and record the change on its append-only ``phase_history``. Returns
    ``(record, old_phase, new_phase)``.

    Without ``to_phase`` the target advances to the phase immediately after its
    current one in declaration order; advancing past the final phase raises
    (the target is complete — use ``close_target``). ``to_phase`` must be a
    declared phase; it may move forward OR back (a phase can be re-opened, e.g.
    a contract kicked back from 締結 to 弁護士レビュー) — the engine records the
    transition rather than policing direction, matching Beacon's "transitions
    are permissive, the human is the master" stance.

    ``fields`` supplies the values a phase surfaces (SPEC §4 per-phase fields):
    a contract entering 法務レビュー can record its「レビュー依頼先」/「想定リスク」
    only once it reaches that phase (ms-124 e-4090). Only keys the descriptor
    declares as visible at the NEW phase (its base fields + that phase's own)
    are accepted; an undeclared key raises. Any field the new phase declares
    ``required`` must then hold a value — supplied here or already on the record
    — else the advance raises, so a required phase field is a real promise, not
    a silent no-op."""
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

    # Apply per-phase field values (validated against what the NEW phase makes
    # visible), then enforce that new phase's required fields are satisfied.
    _apply_phase_fields(desc, rec, new, fields or {})

    rec["phase"] = new
    history = rec.setdefault("phase_history", [])
    work_base.record_audit_event(history, kind="phase_change", actor=actor,
                                 reason=reason, **{"from": old, "to": new})
    return rec, old, new


def _apply_phase_fields(desc: dict, rec: dict, new_phase: str,
                        fields: dict) -> None:
    """Set ``fields`` on ``rec`` for the phase being entered and enforce that
    phase's required fields. Only keys visible at ``new_phase`` (base + the
    phase's own extension) may be set; an undeclared key raises. After applying,
    every field the phase's OWN extension marks ``required`` must hold a value
    (from ``fields`` or already on the record)."""
    visible = {f.get("key") for f in td.fields_at_phase(desc, new_phase)}
    for key, val in fields.items():
        if key not in visible:
            raise TargetEngineError(
                f"phase '{new_phase}' で未知の field '{key}' です "
                f"(この phase で有効: {', '.join(sorted(k for k in visible if k))})")
        rec[key] = val
    phase = td.get_phase(desc, new_phase) or {}
    for f in phase.get("fields") or []:
        if not isinstance(f, dict) or not f.get("required"):
            continue
        key = (f.get("key") or "").strip()
        val = rec.get(key)
        if not (val or "") and val not in (0, False):
            raise TargetEngineError(
                f"phase '{new_phase}' に入るには必須 field '{key}' "
                f"({f.get('label') or key}) が必要です "
                f"(--field {key}=... で指定してください)")


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
# Thick cognitive frame (ms-124 e-4089) — WorkItems, Evidence, ball, next-move.
# A milestone carries tasks/commits and a whose-turn sense; an opportunity
# carries activities/communications and who_has_the_ball. A data-defined class
# inherits the SAME primitives here instead of projecting hardcoded zeros: it
# can hold WorkItems (units of doing), Evidence (append-only records of what
# happened), a ball (whose court the move is in) and derive its own next move.
# All are pure transforms over the record; every id is allocated under the
# target's own id so children are unambiguously scoped to their target.
# ---------------------------------------------------------------------------

def list_work_items(rec: dict) -> list:
    """Return the target's WorkItems (raw records, stored order). Empty when the
    arm is absent (tolerant read — a target created before this feature reads as
    no work items)."""
    got = rec.get(WORK_ITEMS_KEY) if isinstance(rec, dict) else None
    return got if isinstance(got, list) else []


def add_work_item(data: dict, desc: dict, target_id: str, description: str, *,
                  actor: str = "", at: str = "") -> dict:
    """Append a WorkItem to a target and return it. The id is allocated under
    ``<target_id>-w`` so it is unambiguously a child of this target. Built via
    the shared ``work_model.new_work_item`` skeleton so a descriptor WorkItem is
    the same shape as a development task / sales activity."""
    if not (description or "").strip():
        raise TargetEngineError("WorkItem の description は必須です")
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    items = rec.setdefault(WORK_ITEMS_KEY, [])
    new_id = work_base.next_suffixed_id(work_model.collect_ids(items),
                                        f"{rec.get('id', target_id)}-w")
    item = work_model.new_work_item(new_id, description, created_at=at)
    if actor:
        item["created_by"] = actor
    items.append(item)
    return item


def complete_work_item(data: dict, desc: dict, target_id: str, item_id: str, *,
                       actor: str = "", reason: str = "") -> dict:
    """Mark one of a target's WorkItems done (shared ``work_model.mark_done``)
    and return it. Raises when the target or the item is unknown."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    item = work_model.find_by_id(list_work_items(rec), (item_id or "").strip())
    if item is None:
        raise TargetEngineError(
            f"WorkItem が見つかりません: {item_id} (target {target_id})")
    work_model.mark_done(item, actor=actor, reason=reason)
    return item


def list_evidence(rec: dict) -> list:
    """Return the target's Evidence records (raw, stored order). Empty when the
    arm is absent (tolerant read)."""
    got = rec.get(EVIDENCE_KEY) if isinstance(rec, dict) else None
    return got if isinstance(got, list) else []


def add_evidence(data: dict, desc: dict, target_id: str, *, summary: str = "",
                 linked_id: str = "", actor: str = "", at: str = "") -> dict:
    """Append an Evidence record to a target and return it. When ``linked_id``
    names one of the target's WorkItems the evidence is tied to it (mirroring a
    commit closing a task); an unknown ``linked_id`` raises so a typo can't
    silently orphan the evidence. Id allocated under ``<target_id>-ev``."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    linked = (linked_id or "").strip()
    if linked and work_model.find_by_id(list_work_items(rec), linked) is None:
        raise TargetEngineError(
            f"linked_id '{linked}' に一致する WorkItem がありません "
            f"(target {target_id})")
    evs = rec.setdefault(EVIDENCE_KEY, [])
    new_id = work_base.next_suffixed_id(work_model.collect_ids(evs),
                                        f"{rec.get('id', target_id)}-ev")
    ev = work_model.new_evidence(new_id, linked_id=linked, created_at=at)
    if summary:
        ev["summary"] = summary
    if actor:
        ev["created_by"] = actor
    evs.append(ev)
    return ev


def set_ball(data: dict, desc: dict, target_id: str, ball: str, *,
             actor: str = "", reason: str = "") -> dict:
    """Set whose court the target's next move is in and record the change on the
    append-only phase_history. ``ball`` must be a recognised state
    (``self`` / ``counterpart``); ``none`` clears it. Returns the record."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    want = "" if (ball or "").strip().lower() in ("none", "") else ball.strip()
    if want and want not in work_model.VALID_BALL:
        raise TargetEngineError(
            f"未知の ball '{ball}' です "
            f"(有効: {work_model.BALL_SELF} / {work_model.BALL_COUNTERPART} / none)")
    old = rec.get(BALL_KEY, "")
    rec[BALL_KEY] = want
    history = rec.setdefault("phase_history", [])
    work_base.record_audit_event(history, kind="ball_change", actor=actor,
                                 reason=reason, **{"from": old, "to": want})
    return rec


def infer_next_move(desc: dict, rec: dict) -> str:
    """Derive the target's "次の一手" — a one-line suggestion of what advances it
    next. Occupation-agnostic reasoning over the thick frame:

    - a done / cancelled target has no next move (``""``);
    - an open WorkItem is the most concrete next move (finish it);
    - otherwise, a target mid-phase advances to its next phase, and one at a
      terminal phase is ready to close.

    This is inference, not policy — it never mutates, and the human stays free
    to do something else. It exists so the shared frame can show a data-defined
    target the same "what now" hint milestones/opportunities get."""
    if not isinstance(rec, dict):
        return ""
    if work_model.is_done(rec) or work_model.is_cancelled(rec):
        return ""
    for w in list_work_items(rec):
        if work_model.is_open(w):
            return f"WorkItem を進める: {w.get('description', '')}".rstrip()
    phases = td.phase_keys(desc)
    cur = current_phase(rec)
    if phases and cur in phases:
        if is_terminal_phase(desc, cur):
            return f"完了する (beacon target close --class {desc.get('kind', '')})"
        nxt = phases[phases.index(cur) + 1]
        label = (td.get_phase(desc, nxt) or {}).get("label") or nxt
        return f"次フェーズへ進める: {label}"
    return ""


# ---------------------------------------------------------------------------
# Projection — the shared-frame shape a descriptor-defined target presents.
# ---------------------------------------------------------------------------

def project_target(desc: dict, rec: dict) -> dict:
    """Return the occupation-agnostic shared-frame projection of one record.

    Matches the shape ``core.project_targets`` / ``sales_entities.project_targets``
    emit — ``id`` / ``label`` / ``status`` / ``kind`` / ``work_items_total`` /
    ``work_items_done`` / ``detail`` — so the shared frame (session-start /
    status) shows a data-defined target beside milestones / opportunities
    without special-casing.

    The WorkItem counts are derived from the target's own WorkItem arm (ms-124
    e-4089 — no longer hardcoded 0), and ``detail`` carries the rest of the
    thick frame: phase, descriptor type, whose-turn ball, and the inferred next
    move, so a data-defined target presents the same cognitive surface a
    milestone or an opportunity does."""
    items = list_work_items(rec)
    total = len(items)
    done = sum(1 for w in items if work_model.is_done(w))
    return {
        "id": rec.get("id", ""),
        "label": work_model.target_label(rec),
        "status": work_model.work_item_status(rec) or "todo",
        "kind": rec.get("kind") or desc.get("kind", ""),
        "work_items_total": total,
        "work_items_done": done,
        "detail": {
            "phase": current_phase(rec),
            "type": desc.get("type", ""),
            "who_has_the_ball": work_model.normalize_ball(rec.get(BALL_KEY, "")),
            "next_move": infer_next_move(desc, rec),
        },
    }
