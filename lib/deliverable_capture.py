"""Capture a completed target's produced value into the root deliverable-changelog
(ms-161 e-5823 / SPEC 方針1 + 受入条件1).

WHY a separate module. The append primitive (``deliverable_changelog``) is a pure
leaf that knows only the abstract entry schema. Deciding WHAT a *completed target*
contributes — its ``category`` (the class-declared deliverable token), its title,
the ``ref`` to drill into — needs the target-class declarations, which live in
``occupation.resolve_deliverable``. This bridge sits ABOVE ``occupation``
(``occupation`` imports ``core``; nothing imports THIS except the CLI command
layer), so it composes the two without a cycle: ``core.milestone_done`` stays
occupation-free, and the occupation-aware capture is invoked one layer up at the
CLI completion seams.

Firing point (ms-161 e-5823). The SPEC wanted to ride ms-159's FR-6 domain events,
but those are unbuilt (ms-159 is todo). Instead the capture is invoked at the THREE
seams that reach ``core.milestone_done`` — a milestone reaches 完遂 through any of:

  1. direct CLI ``milestone done`` — ``cmd_milestone.cmd_milestone_done``
  2. review-gated CLI ``target approve`` — ``cmd_target._apply_transition``
  3. the web/API done endpoint — ``server.routers_projects.done_milestone`` (the op)

so the produced value is recorded exactly once regardless of path (the idempotent
dedup below makes multiple seams safe). ``core.milestone_done`` itself CANNOT host
the capture (it is a low module that ``occupation`` imports; this bridge sits above
``occupation``, so wiring it into ``core`` would cycle), which is why each seam that
CALLS ``milestone_done`` invokes the capture one layer up. When a domain-event bus
later lands, these call sites collapse behind one handler and this module is
unchanged. If a FOURTH completion path is added, it must call this too — the guard
tests (``test_deliverable_capture``) assert each known seam references it.
"""
from __future__ import annotations

import deliverable_changelog as _dc
import occupation as _occ
import work_model as _wm


def _clean(value) -> str:
    """Return a stripped string, or ``""`` for a non-string / None field."""
    return value.strip() if isinstance(value, str) else ""


def capture_target_completion(data: dict, target: dict, *,
                              reason: str = "",
                              at: str | None = None,
                              actor: str | None = None) -> dict | None:
    """Append the produced-value entry for a just-completed ``target`` to the root
    deliverable-changelog and return it — or ``None`` when there is nothing to
    capture.

    Returns ``None`` (no append) when:

    - ``target`` has no id (defensive), OR
    - the target's class declares NO deliverable — additive-only: a class without a
      deliverable slot completes exactly as before. ``occupation.resolve_deliverable``
      returns ``None`` for opportunity / operation / task today, so only
      milestone→機能 is captured (sales 成約 is a follow-up), OR
    - an ACTIVE entry for this ``(target, category)`` already exists — idempotent, so
      re-running ``done``, or an observe→done sequence, never double-counts the same
      produced value.

    Field derivation (auto, from the completed target):
      - ``source`` = the target's id + kind (attribution).
      - ``category`` = the class's DECLARED deliverable token (``decl["kind"]``, e.g.
        ``"feature-map"`` for milestone). Class-declared, NOT hardcoded, so the
        schema stays profession-independent (受入条件5).
      - ``title`` = the target's label; ``summary`` = the completion ``reason`` (its
        best "何を生んだか" signal at completion time), falling back to the target's
        description / motivation, then the label. ``ref`` = the class's declared
        drill-down pointer (milestone → ``application-map``).

    IN-MEMORY only: mutates ``data`` via ``append_deliverable``; the CALLER owns
    persistence (the completion command's existing ``save_project``), matching the
    rest of this subsystem's write discipline."""
    tid = _clean((target or {}).get("id"))
    if not tid:
        return None
    kind = _wm.target_kind(tid)
    decl = _occ.resolve_deliverable(data, kind)
    if not decl:
        return None
    category = _clean(decl.get("kind"))
    # Idempotent: a live entry for this target+category means the produced value is
    # already recorded and current — do not append a duplicate.
    if _dc.read_deliverables(data, status=_dc.STATUS_ACTIVE,
                             category=category, source_target=tid):
        return None
    label = _wm.target_label(target)
    summary = _clean(reason) or _clean(target.get("description")) \
        or _clean(target.get("motivation")) or label
    entry = {
        "source": {"target_id": tid, "kind": kind},
        "category": category,
        "title": label,
        "summary": summary,
        "ref": _clean(decl.get("ref")),
        # Mark this as a coarse OUTCOME-granularity completion (ms-161 e-5902): the
        # auto seam cannot enumerate the milestone's surfaces, so the dev map render
        # holds these out of the surface index (shown in a "未 index 化の完遂"
        # section) and the map stays a surface-単位 index, not a list of 完了理由.
        "tags": [_dc.AUTO_COMPLETION_TAG],
    }
    return _dc.append_deliverable(data, entry, at=at, actor=actor)
