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
# Sourced from the occupation-agnostic single source of truth (ms-142 T2) so this
# and ``target_state`` cannot drift on the ball field key.
BALL_KEY = work_model.BALL_FIELD


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

    declared_by_key = {f.get("key"): f
                       for f in td.fields_at_phase(desc, initial_phase)}
    field_vals: dict = {}
    for key, val in (fields or {}).items():
        if key not in declared:
            raise TargetEngineError(
                f"未知の field '{key}' です (記述子 '{desc.get('kind')}' に宣言が"
                f"ありません)")
        # ms-146 e-5338: a declared choice list is enforced at every write path,
        # so a value the mechanism later has to reason over cannot arrive as a
        # near-miss spelling.
        problem = td.check_field_value(declared_by_key.get(key) or {}, val)
        if problem:
            raise TargetEngineError(problem)
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
    extra[BALL_KEY] = work_model.BALL_SELF
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
    phase's own extension) may be set; an undeclared key raises. Every field the
    phase's OWN extension marks ``required`` must hold a value (from ``fields``
    or already on the record) or the advance raises.

    Validate-before-mutate (ms-124 e-4089 maintainability review): all checks run
    against the incoming ``fields`` + current record BEFORE anything is written,
    so a rejected advance leaves ``rec`` untouched — no partial mutation a caller
    could accidentally persist."""
    visible_fields = td.fields_at_phase(desc, new_phase)
    visible = {f.get("key") for f in visible_fields}
    by_key = {f.get("key"): f for f in visible_fields}
    for key in fields:
        if key not in visible:
            raise TargetEngineError(
                f"phase '{new_phase}' で未知の field '{key}' です "
                f"(この phase で有効: {', '.join(sorted(k for k in visible if k))})")
        problem = td.check_field_value(by_key.get(key) or {}, fields[key])
        if problem:
            raise TargetEngineError(problem)
    phase = td.get_phase(desc, new_phase) or {}
    for f in phase.get("fields") or []:
        if not isinstance(f, dict) or not f.get("required"):
            continue
        key = (f.get("key") or "").strip()
        # required is satisfied by an incoming value OR one already on the record
        val = fields[key] if key in fields else rec.get(key)
        if not (val or "") and val not in (0, False):
            raise TargetEngineError(
                f"phase '{new_phase}' に入るには必須 field '{key}' "
                f"({f.get('label') or key}) が必要です "
                f"(--field {key}=... で指定してください)")
    # All checks passed — now write.
    for key, val in fields.items():
        rec[key] = val


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

def _apply_child_fields(declared: list, item: dict, fields: dict, *,
                        what: str, kind: str) -> None:
    """Set ``fields`` on a CHILD record (a WorkItem or an Evidence) after checking
    them against what the descriptor declares for that arm (ms-146 e-5344).

    Mirrors ``_apply_phase_fields`` deliberately: only declared keys may be set
    (an undeclared key raises rather than silently landing a value nothing reads),
    every declared ``required`` key must hold a value, and — validate-before-mutate
    — nothing is written until all checks pass, so a rejected call leaves the item
    untouched and the caller can abandon it without a half-written record.

    A class that declares NO fields for the arm keeps its previous behaviour
    exactly: ``fields`` is empty on every pre-existing call site, both loops do
    nothing, and passing one anyway raises with the reason (the arm carries no
    declaration) instead of accepting a value nothing will ever read."""
    visible = {(f.get("key") or "").strip()
               for f in declared if isinstance(f, dict)}
    visible.discard("")
    by_key = {(f.get("key") or "").strip(): f
              for f in declared if isinstance(f, dict)}
    for key in fields:
        if key not in visible:
            detail = (f"この arm で有効: {', '.join(sorted(visible))}" if visible
                      else f"記述子 '{kind}' は {what} の field を宣言していません")
            raise TargetEngineError(
                f"{what} に未知の field '{key}' です ({detail})")
        problem = td.check_field_value(by_key.get(key) or {}, fields[key])
        if problem:
            raise TargetEngineError(f"{what}: {problem}")
    for f in declared:
        if not isinstance(f, dict) or not f.get("required"):
            continue
        key = (f.get("key") or "").strip()
        val = fields[key] if key in fields else item.get(key)
        if not (val or "") and val not in (0, False):
            raise TargetEngineError(
                f"{what} には必須 field '{key}' "
                f"({f.get('label') or key}) が必要です "
                f"(--field {key}=... で指定してください)")
    # All checks passed — now write.
    for key, val in fields.items():
        item[key] = val


def list_work_items(rec: dict) -> list:
    """Return the target's WorkItems (raw records, stored order). Empty when the
    arm is absent (tolerant read — a target created before this feature reads as
    no work items)."""
    got = rec.get(WORK_ITEMS_KEY) if isinstance(rec, dict) else None
    return got if isinstance(got, list) else []


def add_work_item(data: dict, desc: dict, target_id: str, description: str, *,
                  fields: Optional[dict] = None, actor: str = "",
                  at: str = "") -> dict:
    """Append a WorkItem to a target and return it. The id is allocated under
    ``<target_id>-w`` so it is unambiguously a child of this target. Built via
    the shared ``work_model.new_work_item`` skeleton so a descriptor WorkItem is
    the same shape as a development task / sales activity.

    ``fields`` carries the per-item values the descriptor declares in
    ``work_item_fields`` (ms-146 e-5344) — an executive class's 時間予算, a
    deadline, whatever the class names. Only declared keys are accepted and
    declared ``required`` keys must be present, so a per-item declaration is a
    real promise rather than a convention. Omitting ``fields`` on a class that
    declares none is the pre-existing behaviour, unchanged."""
    if not (description or "").strip():
        raise TargetEngineError("WorkItem の description は必須です")
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    new_id = work_base.next_suffixed_id(
        work_model.collect_ids(list_work_items(rec)),
        f"{rec.get('id', target_id)}-w")
    item = work_model.new_work_item(new_id, description, created_at=at)
    if actor:
        item["created_by"] = actor
    # Validate + stamp BEFORE the item joins the record, so a rejected call
    # leaves the target with no half-written child.
    _apply_child_fields(td.work_item_fields(desc), item, fields or {},
                        what="WorkItem", kind=desc.get("kind", ""))
    rec.setdefault(WORK_ITEMS_KEY, []).append(item)
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


def cancel_work_item(data: dict, desc: dict, target_id: str, item_id: str, *,
                     actor: str = "", reason: str = "") -> dict:
    """Cancel one of a target's WorkItems and return it (ms-146 e-5348).

    Routes through the SHARED cancel vocabulary ``work_base.stamp_cancel``, the
    one a development task and a sales activity already use: the item is never
    physically removed — its status becomes ``cancelled`` and who / when / why is
    stamped on ``meta`` — per the ``data-immutability-principle`` CORE doc (every
    state change stays traceable; a record that vanishes cannot be audited).

    WHY a descriptor class needs this at all: ``done`` was its only exit, so an
    item added by mistake, or one the owner DECIDED NOT TO DO, could only be
    finished or left open forever. For a class whose whole point is deciding what
    NOT to do, "no way to drop an item" is not a missing convenience — it
    contradicts the class. ``reason`` is what makes a cancel readable later;
    the CLI requires it."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    item = work_model.find_by_id(list_work_items(rec), (item_id or "").strip())
    if item is None:
        raise TargetEngineError(
            f"WorkItem が見つかりません: {item_id} (target {target_id})")
    if work_model.is_cancelled(item):
        raise TargetEngineError(
            f"WorkItem '{item_id}' は既に取り消し済みです")
    work_base.stamp_cancel(item, reason=reason, actor=actor)
    return item


def list_evidence(rec: dict) -> list:
    """Return the target's Evidence records (raw, stored order). Empty when the
    arm is absent (tolerant read)."""
    got = rec.get(EVIDENCE_KEY) if isinstance(rec, dict) else None
    return got if isinstance(got, list) else []


def add_evidence(data: dict, desc: dict, target_id: str, *, summary: str = "",
                 linked_id: str = "", fields: Optional[dict] = None,
                 actor: str = "", at: str = "") -> dict:
    """Append an Evidence record to a target and return it. When ``linked_id``
    names one of the target's WorkItems the evidence is tied to it (mirroring a
    commit closing a task); an unknown ``linked_id`` raises so a typo can't
    silently orphan the evidence. Id allocated under ``<target_id>-ev``.

    ``fields`` carries the values the descriptor declares in ``evidence_fields``
    (ms-146 e-5344). This is what lets an evidence record hold a STRUCTURED
    observation — 効いたか (did this move the objective), 消費時間 — instead of only
    free-text prose, so a mechanism can reason over the pile rather than a human
    re-reading it. Same contract as ``add_work_item``: declared keys only,
    required keys enforced, validate-before-mutate."""
    rec = find_target(data, desc, target_id)
    if rec is None:
        raise TargetEngineError(f"target が見つかりません: {target_id}")
    linked = (linked_id or "").strip()
    if linked and work_model.find_by_id(list_work_items(rec), linked) is None:
        raise TargetEngineError(
            f"指定された WorkItem '{linked}' が見つかりません "
            f"(target {target_id} の work-item list で確認してください)")
    new_id = work_base.next_suffixed_id(
        work_model.collect_ids(list_evidence(rec)),
        f"{rec.get('id', target_id)}-ev")
    ev = work_model.new_evidence(new_id, linked_id=linked, created_at=at)
    if summary:
        ev["summary"] = summary
    if actor:
        ev["created_by"] = actor
    _apply_child_fields(td.evidence_fields(desc), ev, fields or {},
                        what="Evidence", kind=desc.get("kind", ""))
    rec.setdefault(EVIDENCE_KEY, []).append(ev)
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


def _num(value) -> float:
    """Best-effort numeric read of a stored field value. Field values arrive from
    the CLI as strings, so a budget of ``"2"`` and one of ``2`` must count the
    same; anything unparseable counts as 0 rather than raising, because a single
    malformed note must not make the whole overrun read fail."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def budget_status(desc: dict, rec: dict) -> dict:
    """Return how much of a target's declared time budget has been consumed
    (ms-146 e-5337), or ``{}`` when the class declares no budget tracking.

    DECLARATION-DRIVEN on purpose: the class names which of ITS fields hold the
    budget and the spend —

        "budget_tracking": {"target_budget_field": "time_budget_h",
                            "work_item_budget_field": "budget_h",
                            "evidence_spend_field": "spent_h"}

    — so this stays occupation-agnostic ("機構は基底 / 語彙は記述子") and any class
    that wants the same arithmetic gets it by declaring, not by an edit here. A
    class that declares nothing is untouched.

    Spend is attributed to a work item through the evidence's ``linked_id``
    (the same link that ties a commit to a task); evidence linked to nothing
    still counts toward the TARGET total, because time spent with no item named
    is still time spent."""
    cfg = desc.get("budget_tracking")
    if not isinstance(cfg, dict) or not isinstance(rec, dict):
        return {}
    spend_key = (cfg.get("evidence_spend_field") or "").strip()
    item_key = (cfg.get("work_item_budget_field") or "").strip()
    target_key = (cfg.get("target_budget_field") or "").strip()

    spent_by_item: dict = {}
    spent_total = 0.0
    for ev in list_evidence(rec):
        if not isinstance(ev, dict) or not spend_key:
            continue
        amount = _num(ev.get(spend_key))
        if not amount:
            continue
        spent_total += amount
        linked = (ev.get("linked_id") or "").strip()
        if linked:
            spent_by_item[linked] = spent_by_item.get(linked, 0.0) + amount

    items = []
    for it in list_work_items(rec):
        if not isinstance(it, dict) or work_model.is_cancelled(it):
            continue          # a cancelled item's budget is not owed any more
        budget = _num(it.get(item_key)) if item_key else 0.0
        spent = spent_by_item.get(it.get("id"), 0.0)
        items.append({"id": it.get("id"),
                      "description": it.get("description", ""),
                      "budget": budget, "spent": spent,
                      "over": bool(budget) and spent > budget})

    target_budget = _num(rec.get(target_key)) if target_key else 0.0
    return {
        "target_budget": target_budget,
        "spent_total": spent_total,
        "over_target": bool(target_budget) and spent_total > target_budget,
        "items": items,
        "over_items": [i for i in items if i["over"]],
    }


def is_over_budget(desc: dict, rec: dict) -> bool:
    """True when the target as a whole, or any of its live work items, has spent
    more than it declared it would (ms-146 e-5337). False for a class that
    declares no budget tracking — an absent declaration is not an overrun."""
    status = budget_status(desc, rec)
    if not status:
        return False
    return bool(status["over_target"] or status["over_items"])


def stall_status(desc: dict, rec: dict) -> dict:
    """Return how many times IN A ROW the most recent evidence says the work is
    no longer moving the objective (ms-146 e-5338), or ``{}`` when the class
    declares no stall signal.

    DECLARED, not hardcoded — the class names the field, the value that counts as
    "not moving", and how many in a row are enough::

        "stall_signal": {"evidence_field": "moved", "value": "効いてない",
                         "threshold": 2}

    WHY a STREAK and not a total: diminishing returns is a shape over time, not a
    tally. One ineffective session inside otherwise-productive work means nothing;
    two in a row means the last thing that worked has stopped working. Counting
    totals would flag a target that struggled early and then found its footing —
    exactly the case where you should NOT stop.

    The streak is read from the END of the evidence list backwards, so a later
    "効いた" resets it. Evidence with no value for the field is SKIPPED rather
    than treated as a reset: a note that says nothing about effect is silence, and
    silence is not evidence that things started working again."""
    cfg = desc.get("stall_signal")
    if not isinstance(cfg, dict) or not isinstance(rec, dict):
        return {}
    field = (cfg.get("evidence_field") or "").strip()
    marker = cfg.get("value")
    if not field or marker in (None, ""):
        return {}
    try:
        threshold = int(cfg.get("threshold") or 2)
    except (TypeError, ValueError):
        threshold = 2
    threshold = max(1, threshold)

    streak = 0
    for ev in reversed(list_evidence(rec)):
        if not isinstance(ev, dict):
            continue
        val = ev.get(field)
        if val in (None, ""):
            continue                      # silence, not a reset
        if str(val) == str(marker):
            streak += 1
            continue
        break                             # something worked — streak resets
    return {"field": field, "value": marker, "threshold": threshold,
            "streak": streak, "stalled": streak >= threshold}


def stop_signals(desc: dict, rec: dict) -> list:
    """Return the reasons this target should probably be wrapped up — the
    machine-side half of "もう十分では？" (ms-146 e-5339's data source).

    Two independent signals, both declaration-driven: the target (or one of its
    live work items) has spent more time than it said it would, and the recent
    evidence says the work has stopped moving the objective. Each entry is
    ``{kind, message}``; an empty list means nothing suggests stopping yet.

    This REPORTS, it never acts: the ms-146 SPEC 設計方針2 ruling is that the
    mechanism surfaces the signal and the human decides. Forcing a stop was
    considered and deliberately left for later."""
    out: list = []
    budget = budget_status(desc, rec)
    if budget:
        if budget["over_target"]:
            out.append({
                "kind": "budget_target",
                "message": (f"宣言した時間予算 {budget['target_budget']:g}h に対し "
                            f"{budget['spent_total']:g}h 使っています")})
        for item in budget["over_items"]:
            out.append({
                "kind": "budget_work_item",
                "message": (f"[{item['id']}] {item['description']} が "
                            f"予算 {item['budget']:g}h に対し "
                            f"{item['spent']:g}h 使っています")})
    stall = stall_status(desc, rec)
    if stall and stall["stalled"]:
        out.append({
            "kind": "stall",
            "message": (f"直近の証跡が {stall['streak']} 回連続で "
                        f"「{stall['value']}」です")})
    return out


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
    # ms-146 e-5347: a CANCELLED item is out of the denominator. Deciding NOT to
    # do something is the point of this class, and a count that does not shrink
    # when you drop work tells the owner their decision changed nothing — the
    # number contradicts the very act the cancel verb exists to support. The item
    # is not deleted (``work-item list`` still shows it with its reason); it is
    # only no longer OWED. ``work_items_done`` keeps its meaning exactly: items
    # actually finished, never inflated by cancellations.
    items = [w for w in list_work_items(rec)
             if not work_model.is_cancelled(w)]
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
            "evidence_total": len(list_evidence(rec)),
        },
    }
