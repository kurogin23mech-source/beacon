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

from typing import Any


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
