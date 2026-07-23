"""Beacon 2-layer claim view — read who has a Target, LIVE + persistent (ms-112).

The ms-112 SPEC (``xgoANbBympPE2TCnJThO``) diagnosis: the claim *primitives*
already exist, but the *consuming* side (session-start「次の一手」,
``/beacon-dispatch``, sales cockpit) never reads them, so it组み立てる work on a
personal "I see every target" assumption that breaks the moment a team touches
the same project. This module is the missing consuming layer: it束ねる the two
claim axes into ONE "who is working on / owns this target" view that every
consumer reads, occupation-agnostically (milestone / opportunity / account).

Two layers (SPEC 設計方針 2):

  * **LIVE session layer** — "who is sitting at this target right now". Sourced
    from the occupation claim (``target["occupation"]`` = ms-81 soft claim: a
    session ``{session_id, machine, agent, claimed_at}``). A claim can go stale
    (the session died without releasing), so when the caller supplies the set of
    currently-healthy session ids (from the bus directory) we mark each claim
    ``healthy`` and only healthy claims count as "someone working now". When the
    caller can NOT verify liveness (local mode / directory unreachable) we treat
    the claim as live (``healthy=None``) — conservative: surface the warning
    rather than silently drop it.

  * **user persistent layer** — "who is the standing assignee". Sourced from
    ``target["assignee"]`` (ms-81 e-1918), the existing members-unverified owner
    field, split on comma via the shared ``_split_assignees`` rule.

Non-exclusive by design (SPEC 設計方針 3 / AC5): this module NEVER blocks. It
returns *flags* (``live_by_others`` / ``assigned_to_others`` / ``unclaimed`` …)
that a consumer turns into a warning or a de-prioritisation — the work itself
is never stopped. And it honours the 2-layer fallback (AC6): a target with no
LIVE claimer but a persistent assignee still reads as "owned", so a consumer can
組み立てる around the assignee even when nobody is live.

Like ``work_model`` / ``work_base`` / ``core`` this module performs NO I/O:
every function is a pure transform over the values it is handed. The I/O shell
(resolve the healthy-session set from the bus directory, resolve my own
session id / identities) lives in ``commands.py`` (``cmd_claim_view``), per the
tool/skill ↔ CLI/API separation (CORE doc ``architecture-tool-skill-separation``).
"""

from __future__ import annotations

from typing import Iterable, Optional

import occupation
import work_model


# ---------------------------------------------------------------------------
# Assignee normalisation — reuse the one comma-split rule.
#
# ``core._split_assignees`` is the canonical normaliser (str | list → list of
# trimmed names, empties dropped). Importing ``core`` here would be fine (this
# module is above the occupation adapters), but ``core`` is a heavy module and
# the rule is three lines; to keep ``claim_view`` cheap to import from the CLI
# hot path we inline the identical rule. Kept byte-identical to core's so the
# two never diverge — the test suite asserts parity.
# ---------------------------------------------------------------------------

def _split_assignees(value) -> list[str]:
    """Normalise an assignee field (str or list) into a list of names.

    Mirrors ``core._split_assignees``: a single string ``"alice"`` → ``["alice"]``,
    a comma form ``"alice,bob"`` → ``["alice", "bob"]``, a list is taken as-is;
    empty / whitespace-only tokens are dropped. Returns ``[]`` for falsy input.
    """
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [s.strip() for s in items if s and s.strip()]


def _norm_identities(identities: Iterable[str]) -> set[str]:
    """Lower-case, trim, drop empties — so assignee matching is tolerant of
    ``Alice`` vs ``alice`` and of a caller passing several aliases (actor name,
    email, user_id) for "me"."""
    out: set[str] = set()
    for ident in identities or ():
        s = (ident or "").strip().lower()
        if s:
            out.add(s)
    return out


# ---------------------------------------------------------------------------
# Single-target view
# ---------------------------------------------------------------------------

def build_claim_view(
    target: dict,
    *,
    live_session_ids: Optional[Iterable[str]] = None,
    my_session_id: str = "",
    my_identities: Iterable[str] = (),
) -> dict:
    """Bundle a single Target's LIVE + persistent claim state into one view.

    Args:
      target: a raw Target record (development milestone / sales opportunity /
        account) as stored in project.json. Its ``id`` drives the occupation-
        agnostic ``target_kind`` derivation; ``occupation`` / ``assignee`` carry
        the two claim layers.
      live_session_ids: the set of session ids currently considered LIVE +
        healthy (from the bus directory). ``None`` = liveness could not be
        verified (local mode / directory unreachable) → occupation claims read
        as live with ``healthy=None`` (conservative, non-blocking). An empty
        iterable = verified and nobody is live → occupation claims read as stale.
      my_session_id: this session's id, to flag a live claim as mine.
      my_identities: aliases for "me" (actor name / email / user_id) matched
        case-insensitively against the assignee list.

    Returns a dict:
      {
        "target_id":   str,
        "target_kind": str,          # milestone / opportunity / account / ...
        "label":       str,
        "live": [                    # LIVE session layer (0..1 today: 1 claim)
          {"session_id", "machine", "agent", "claimed_at",
           "source": "occupation", "healthy": bool | None, "is_me": bool},
        ],
        "assignees": [str, ...],     # persistent layer
        "flags": {
          "live":               bool,   # anyone live (healthy or unverified)
          "live_by_me":         bool,
          "live_by_others":     bool,   # someone live who is NOT me
          "assigned":           bool,
          "assigned_to_me":     bool,
          "assigned_to_others": bool,
          "unclaimed":          bool,   # no live AND no assignee → free to grab
        },
      }

    Pure: no I/O, no mutation of ``target``.
    """
    if not isinstance(target, dict):
        target = {}

    target_id = target.get("id") or ""
    kind = work_model.target_kind(target_id)
    label = work_model.target_label(target)

    verified = live_session_ids is not None
    healthy_set = {s for s in (live_session_ids or []) if s}
    me_sid = (my_session_id or "").strip()
    me_idents = _norm_identities(my_identities)

    # --- LIVE layer: the occupation claim, liveness-checked. ---------------
    live: list[dict] = []
    occ = target.get("occupation")
    if isinstance(occ, dict) and occ.get("session_id"):
        sid = occ.get("session_id") or ""
        healthy = (sid in healthy_set) if verified else None
        live.append({
            "session_id": sid,
            "machine": occ.get("machine") or "",
            "agent": occ.get("agent") or "",
            "claimed_at": occ.get("claimed_at") or "",
            "source": "occupation",
            "healthy": healthy,
            "is_me": bool(me_sid) and sid == me_sid,
        })

    # A live entry "counts" (someone is working now) when it is healthy, or
    # when we could not verify (healthy is None → assume live, non-blocking).
    def _counts(entry: dict) -> bool:
        return entry["healthy"] is not False

    live_by_me = any(e["is_me"] and _counts(e) for e in live)
    live_by_others = any((not e["is_me"]) and _counts(e) for e in live)
    any_live = live_by_me or live_by_others

    # --- persistent layer: assignee. --------------------------------------
    assignees = _split_assignees(target.get("assignee"))
    assigned = bool(assignees)
    assigned_to_me = any(a.strip().lower() in me_idents for a in assignees) \
        if me_idents else False
    assigned_to_others = any(a.strip().lower() not in me_idents for a in assignees)

    return {
        "target_id": target_id,
        "target_kind": kind,
        "label": label,
        "live": live,
        "assignees": assignees,
        "flags": {
            "live": any_live,
            "live_by_me": live_by_me,
            "live_by_others": live_by_others,
            "assigned": assigned,
            "assigned_to_me": assigned_to_me,
            "assigned_to_others": assigned_to_others,
            "unclaimed": (not any_live) and (not assigned),
        },
    }


# ---------------------------------------------------------------------------
# All-targets view
# ---------------------------------------------------------------------------

def build_claim_views(
    data: dict,
    *,
    live_session_ids: Optional[Iterable[str]] = None,
    my_session_id: str = "",
    my_identities: Iterable[str] = (),
) -> dict[str, dict]:
    """Build the claim view for EVERY Target across occupations, keyed by
    target id. Walks ``occupation.iter_target_records`` (milestones +
    opportunities + …) so the caller gets the whole project's claim map without
    branching on profession. Targets without an ``id`` are skipped."""
    views: dict[str, dict] = {}
    for target in occupation.iter_target_records(data):
        view = build_claim_view(
            target,
            live_session_ids=live_session_ids,
            my_session_id=my_session_id,
            my_identities=my_identities,
        )
        tid = view["target_id"]
        if tid:
            views[tid] = view
    return views


# ---------------------------------------------------------------------------
# Consumer helpers — turn a view into a human line / a warning.
# ---------------------------------------------------------------------------

def format_claim_line(view: dict) -> str:
    """One-line human summary of a claim view for consumers (next-hand /
    dispatch / cockpit). Returns ``""`` when the target is completely unclaimed
    (nothing to say — a consumer prints the line only when non-empty).

    Examples:
      "🟢 LIVE: あなたが作業中"
      "🔴 LIVE: 別セッションが作業中 (machine/agent) · 担当 alice"
      "担当 alice (LIVE claimer なし)"
      "⚠ LIVE claim あり (liveness 未確認)"
    """
    if not isinstance(view, dict):
        return ""
    flags = view.get("flags") or {}
    parts: list[str] = []

    if flags.get("live_by_me"):
        parts.append("🟢 LIVE: あなたが作業中")
    elif flags.get("live_by_others"):
        who = ""
        for e in view.get("live") or []:
            if not e.get("is_me") and e.get("healthy") is not False:
                tag = "/".join(x for x in (e.get("machine"), e.get("agent")) if x)
                verified = e.get("healthy") is True
                who = f" ({tag})" if tag else ""
                if not verified:
                    parts.append(f"⚠ LIVE: 別セッションが作業中の可能性{who} (liveness 未確認)")
                else:
                    parts.append(f"🔴 LIVE: 別セッションが作業中{who}")
                break

    if flags.get("assigned"):
        assignees = ", ".join(view.get("assignees") or [])
        if flags.get("assigned_to_me") and not flags.get("assigned_to_others"):
            parts.append(f"担当 {assignees} (あなた)")
        else:
            note = "" if flags.get("live") else " (LIVE claimer なし)"
            parts.append(f"担当 {assignees}{note}")

    return " · ".join(parts)


def claim_warns_collision(view: dict) -> bool:
    """True when starting work on this target would collide with someone else —
    i.e. another session is LIVE on it OR it is assigned to someone who is not
    me. Consumers use this to decide whether to *warn* (never to block: SPEC
    設計方針 3). A target live/assigned to ME does not warn."""
    if not isinstance(view, dict):
        return False
    flags = view.get("flags") or {}
    return bool(flags.get("live_by_others") or (
        flags.get("assigned_to_others") and not flags.get("assigned_to_me")
    ))
