"""Stable-recipient-identity resolve (ms-93 / e-2520).

`session_id` (sid) is an ephemeral *route token*, not an identity: a bridge
restart or a Codex daemon re-mint produces a fresh sid, so a sender that
remembers a raw sid ends up addressing a session that died a minute ago
(observed repeatedly 2026-07-07: a DNS-cutover DM and a bug-report DM both
went to a stale / wrong sid and never surfaced to the intended human).

The fix is to route by a *sid-independent* coarse key —
``(machine, cwd, agent_kind)`` — and resolve it to the current live sid at
send time. Two concurrent sessions in the same cwd are separated by
``agent_kind`` and, failing that, by the disambiguation fields (``parent_pid``
/ ``started_at``) surfaced to the human picker.

This module is deliberately a set of **pure functions over already-fetched
directory rows** (the shape returned by ``beacon bus directory --json`` /
``GET /api/projects/{id}/sessions``). The server-side ``cwd`` / ``agent_kind``
filter (e-2520 Phase 1, server/app.py) is a fetch-narrowing optimization; the
authoritative resolve logic lives here so it is testable without a live server
and reusable by the CLI, the /beacon-dm-send picker, and future callers.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


# A session that has not polled the bus within this many seconds is treated
# as stale (= not "healthy live"). The Codex daemon and Claude Code's bus.mjs
# both heartbeat on a ~5s cadence, so 30s is 6 missed beats — comfortably past
# any single slow poll, but tight enough to catch a sid that died between the
# picker showing it and the actual send (= ms-93 / e-2519 AC 5, §8-G FINDING #3:
# stale-sid DMs silently vanished twice on 2026-07-07).
LIVENESS_THRESHOLD_S = 30.0


def _parse_iso(ts: str):
    """Parse an ISO8601 timestamp (with ``Z`` or offset) to an aware datetime.

    Returns ``None`` when empty / unparseable. Naive strings are assumed UTC
    (the wire format both ends emit)."""
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def poll_age_seconds(row: dict[str, Any], *, now_iso: str) -> float | None:
    """Seconds since this session last polled, relative to ``now_iso``.

    ``None`` when the row has no parseable ``last_poll_at`` / ``last_active``
    (= we cannot vouch for its liveness) or ``now_iso`` is unparseable. A
    negative age (= clock skew where the poll is "in the future") clamps to 0.
    """
    now = _parse_iso(now_iso)
    last = _parse_iso(_poll_ts(row))
    if now is None or last is None:
        return None
    return max(0.0, (now - last).total_seconds())


def is_fresh(
    row: dict[str, Any],
    *,
    now_iso: str,
    threshold_s: float = LIVENESS_THRESHOLD_S,
) -> bool:
    """True when the session polled within ``threshold_s`` of ``now_iso``.

    Unknown age (= missing/garbled ``last_poll_at``) is treated as NOT fresh:
    absence of proof of life is proof of stale for send-gating purposes."""
    age = poll_age_seconds(row, now_iso=now_iso)
    return age is not None and age <= threshold_s


def agent_kind_of(row: dict[str, Any]) -> str:
    """Return the structural agent kind (claude-code / codex) for a session row.

    This is ``agent.kind`` — NOT ``actor.agent``, which is only a machine
    label (e.g. both fields read "CFGW5D79LL" on a Mac). Falls back to the sid
    prefix so rows minted before the ``agent`` block existed still classify:
    ``codex-...`` sids are codex, ``sv-...`` sids are claude-code.
    """
    agent = row.get("agent")
    if isinstance(agent, dict):
        kind = (agent.get("kind") or "").strip()
        if kind:
            return kind
    sid = str(row.get("session_id") or "")
    if sid.startswith("codex-"):
        return "codex"
    if sid.startswith("sv-"):
        return "claude-code"
    return ""


def machine_of(row: dict[str, Any]) -> str:
    """Return the machine label. Prefers ``machine_id`` (server identity) and
    falls back to ``actor.machine`` (human-readable hostname)."""
    mid = str(row.get("machine_id") or "").strip()
    if mid:
        return mid
    actor = row.get("actor")
    if isinstance(actor, dict):
        return str(actor.get("machine") or "").strip()
    return ""


def stable_identity_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """The sid-independent identity tuple for a session row.

    ``(project_id, machine, cwd, agent_kind)``. Stable across sid re-mint, so
    two directory snapshots taken before and after a bridge restart yield the
    same key for the same logical session.
    """
    return (
        str(row.get("project_id") or ""),
        machine_of(row),
        str(row.get("cwd") or ""),
        agent_kind_of(row),
    )


def _poll_ts(row: dict[str, Any]) -> str:
    """Sort key for "most recently alive". ISO8601 strings compare
    lexicographically (same UTC wire format on both ends), so a plain string
    max picks the freshest poll. Empty sorts last."""
    return str(row.get("last_poll_at") or row.get("last_active") or "")


def resolve_stable_identity(
    rows: list[dict[str, Any]],
    *,
    machine: str = "",
    cwd: str = "",
    agent_kind: str = "",
    project_id: str = "",
) -> list[dict[str, Any]]:
    """Filter directory ``rows`` to the coarse identity key and rank them.

    Every supplied (non-empty) component must match. The result is sorted by
    ``last_poll_at`` descending, so ``result[0]`` is the current live sid for
    that identity — the value a sender should route to. When more than one row
    survives (genuinely concurrent sessions sharing the coarse key), the caller
    disambiguates via :func:`describe_candidate` (parent_pid / started_at).

    Pure function: no I/O. ``rows`` is whatever the directory endpoint already
    returned (optionally pre-filtered server-side with the same key).
    """
    def _match(row: dict[str, Any]) -> bool:
        if project_id and str(row.get("project_id") or "") != project_id:
            return False
        if machine and machine_of(row) != machine:
            return False
        if cwd and str(row.get("cwd") or "") != cwd:
            return False
        if agent_kind and agent_kind_of(row) != agent_kind:
            return False
        return True

    matched = [r for r in rows if _match(r)]
    matched.sort(key=_poll_ts, reverse=True)
    return matched


def current_sid(
    rows: list[dict[str, Any]],
    *,
    machine: str = "",
    cwd: str = "",
    agent_kind: str = "",
    project_id: str = "",
) -> str:
    """Convenience: the single most-recently-live sid for the identity, or ""
    when nothing matches."""
    ranked = resolve_stable_identity(
        rows, machine=machine, cwd=cwd, agent_kind=agent_kind,
        project_id=project_id,
    )
    return str(ranked[0].get("session_id") or "") if ranked else ""


def current_live_sid(
    rows: list[dict[str, Any]],
    *,
    now_iso: str,
    stale_threshold_s: float = LIVENESS_THRESHOLD_S,
    machine: str = "",
    cwd: str = "",
    agent_kind: str = "",
    project_id: str = "",
) -> tuple[str, bool]:
    """Resolve the identity to its freshest sid AND flag staleness.

    Returns ``(sid, stale)``:

    * ``sid`` — the most-recently-live sid for the coarse identity (same as
      :func:`current_sid`), or ``""`` when nothing matches the key.
    * ``stale`` — ``True`` when even that best sid last polled more than
      ``stale_threshold_s`` ago (= it looks dead), so the caller should NOT
      blindly send to it. ``True`` too when the age is unknown. ``False``
      only when the sid is proven fresh.

    This is the send-gate primitive (= e-2519 AC 5): the picker resolves an
    sid, then the send path calls this again right before firing and refuses /
    re-resolves when ``stale`` — closing the "sid died between show and send"
    hole that silently dropped DMs.
    """
    ranked = resolve_stable_identity(
        rows, machine=machine, cwd=cwd, agent_kind=agent_kind,
        project_id=project_id,
    )
    if not ranked:
        return "", True
    best = ranked[0]
    sid = str(best.get("session_id") or "")
    stale = not is_fresh(best, now_iso=now_iso, threshold_s=stale_threshold_s)
    return sid, stale


def describe_candidate(row: dict[str, Any]) -> str:
    """One-line human label for a picker row, keyed on stable identity plus the
    disambiguation fields. e.g.::

        codex on mc-77e8… in /Users/…/beacon (started 2026-07-07T05:44, pid 23353)
    """
    kind = agent_kind_of(row) or "session"
    machine = machine_of(row) or "?"
    cwd = str(row.get("cwd") or "?")
    started = str(row.get("created_at") or "")[:16]
    pid = row.get("parent_pid")
    tail = []
    if started:
        tail.append(f"started {started}")
    if pid:
        tail.append(f"pid {pid}")
    suffix = f" ({', '.join(tail)})" if tail else ""
    return f"{kind} on {machine} in {cwd}{suffix}"


def label_rows(
    rows: list[dict[str, Any]],
    *,
    now_iso: str = "",
    stale_threshold_s: float = LIVENESS_THRESHOLD_S,
) -> list[dict[str, Any]]:
    """Annotate each directory row with its stable-identity label + key for the
    /beacon-dm-send picker. Keeps the raw ``session_id`` so the human sees a
    person/workspace first and the route token only as sub-info.

    When ``now_iso`` is supplied, each row also carries ``poll_age_s`` and a
    ``stale`` flag (= polled more than ``stale_threshold_s`` ago) so the picker
    can grey out dead sessions instead of silently offering them."""
    out = []
    for row in rows:
        project_id, machine, cwd, agent_kind = stable_identity_key(row)
        entry = {
            "session_id": str(row.get("session_id") or ""),
            "label": describe_candidate(row),
            "project_id": project_id,
            "machine": machine,
            "cwd": cwd,
            "agent_kind": agent_kind,
            "last_poll_at": _poll_ts(row),
        }
        if now_iso:
            age = poll_age_seconds(row, now_iso=now_iso)
            entry["poll_age_s"] = age
            entry["stale"] = not is_fresh(
                row, now_iso=now_iso, threshold_s=stale_threshold_s
            )
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python3 -m beacon_cli.skills_helpers.identity_resolve``.

    Reads directory rows (the JSON array from ``beacon bus directory --json``)
    on stdin and, per subcommand, emits JSON on stdout:

      * ``label``  → array of {session_id, label, project_id, machine, cwd,
        agent_kind, last_poll_at} for the picker.
      * ``resolve --machine M --cwd C --agent-kind K [--project P]`` →
        {current_sid, current_sid_stale, stale_threshold_s, now, candidates:[...]}.
        ``current_sid`` is the most recently live sid for that coarse identity
        (the value a sender should route to at send time, so a churned sid never
        sends to a dead session); empty when nothing matches.
        ``current_sid_stale`` is ``true`` when even that best sid last polled
        more than ``--stale-threshold`` seconds ago (= looks dead) — the send
        path must refuse / re-resolve rather than fire into the void (e-2519
        AC 5). ``candidates`` holds all ranked matches (each annotated with
        ``poll_age_s`` + ``stale``) so the Skill can disambiguate / grey out
        dead sessions.
    """
    import argparse
    parser = argparse.ArgumentParser(prog="identity_resolve")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("label")
    rp = sub.add_parser("resolve")
    rp.add_argument("--machine", default="")
    rp.add_argument("--cwd", default="")
    rp.add_argument("--agent-kind", default="")
    rp.add_argument("--project", default="")
    rp.add_argument(
        "--now",
        default="",
        help="ISO8601 'now' for staleness (default: current UTC).",
    )
    rp.add_argument(
        "--stale-threshold",
        type=float,
        default=LIVENESS_THRESHOLD_S,
        help=f"Seconds since last poll to call a sid stale (default {LIVENESS_THRESHOLD_S:g}).",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    raw = sys.stdin.read().strip()
    rows = json.loads(raw) if raw else []
    if not isinstance(rows, list):
        rows = []

    if args.mode == "label":
        print(json.dumps(label_rows(rows), ensure_ascii=False, indent=2))
        return 0

    now_iso = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ranked = resolve_stable_identity(
        rows, machine=args.machine, cwd=args.cwd,
        agent_kind=args.agent_kind, project_id=args.project,
    )
    sid, stale = current_live_sid(
        rows, now_iso=now_iso, stale_threshold_s=args.stale_threshold,
        machine=args.machine, cwd=args.cwd, agent_kind=args.agent_kind,
        project_id=args.project,
    )
    result = {
        "current_sid": sid,
        "current_sid_stale": stale,
        "stale_threshold_s": args.stale_threshold,
        "now": now_iso,
        "candidates": label_rows(
            ranked, now_iso=now_iso, stale_threshold_s=args.stale_threshold
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
