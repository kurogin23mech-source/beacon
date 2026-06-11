"""Helpers for the /beacon-dm-send Skill (ms-54 follow-up).

This module is **pure** — it only does data transformation. Talking to the
network (calling `beacon bus directory`, `beacon bus send`, etc.) is the
Skill's job via the Bash tool. The helpers take parsed JSON outputs and
produce either:

  * a list of candidate recipients (for the Skill to render and let the
    user pick), or
  * a fully-built argv list (for the Skill to run with Bash).

Separating "compose" from "execute" means the tests can lock down the
exact CLI invocation that gets produced, without needing live bus state.

Background — why this is a Skill at all:

Today's dogfood (2026-06-09) surfaced four hand-rolled mistakes that kept
recurring when DMs were sent by-hand:

  1. **Wrong recipient project_id** — picked a session from the other
     machine's project listing but forgot to pass `--project <id>`, so
     the event landed in the local project where the target isn't
     listening.
  2. **Forgot `--to <session_id>`** — without it the server (post e-1209)
     drops the dm event entirely.
  3. **Budget gate surprise mid-send** — `--in-reply-to` was set but no
     prior `bus budget grant`, so the send refuses and the operator
     scrambles to grant + retry.
  4. **Payload JSON hand-rolled** — typing `--payload '{"text":"..."}'`
     by hand fat-fingers quotes, embeds raw newlines, etc.

By having the Skill compose the argv via these helpers, all four failure
modes become structurally impossible (mistakes (1)-(2) become "pick from
a list" instead of "remember a flag"; (3) is auto-handled by the reply
Skill; (4) is JSON-encoded from a Python string here).
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Candidate listing
# ---------------------------------------------------------------------------


def _format_age_seconds(age: Optional[float]) -> str:
    """Render a poll-health age in the picker line."""
    if age is None:
        return "age=?"
    if age < 0:
        # future-stamped poll? shouldn't happen, but don't crash the picker.
        return "age=0s"
    if age < 60:
        return f"age {int(age)}s"
    if age < 3600:
        return f"age {int(age // 60)}m"
    return f"age {int(age // 3600)}h"


def build_member_index(members: Iterable[dict]) -> dict[str, dict]:
    """Index `beacon member list --json` output by member id and by email.

    Each member entry can be matched against either the session's
    ``actor.email`` (most reliable when both sides set it) or against the
    session's ``actor.machine`` if a member is registered with their
    machine name as id.
    """
    by_email: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for m in members or []:
        mid = (m.get("id") or "").strip()
        email = (m.get("email") or "").strip().lower()
        if mid:
            by_id[mid] = m
        if email:
            by_email[email] = m
    return {"by_email": by_email, "by_id": by_id}


def resolve_member_for_session(session: dict, index: dict[str, dict]) -> Optional[dict]:
    """Best-effort: which member is this session's actor?

    Match priority: actor.email (case-insensitive) → actor.machine matches
    member id. Returns None if no confident match.
    """
    actor = session.get("actor") or {}
    email = (actor.get("email") or "").strip().lower()
    if email:
        m = index["by_email"].get(email)
        if m:
            return m
    machine = (actor.get("machine") or "").strip()
    if machine:
        m = index["by_id"].get(machine)
        if m:
            return m
    return None


def build_candidates(
    sessions: list[dict],
    members: Optional[list[dict]] = None,
) -> list[dict]:
    """Take the raw `bus directory --json` output and produce picker rows.

    Each row is a dict with keys:

      * ``index``: 1-based number the user types to pick this row
      * ``session_id``: the value to pass as ``--to`` on send
      * ``machine``: actor.machine (for display)
      * ``agent``: actor.agent (for display)
      * ``email``: actor.email or member.email if cross-referenced
      * ``member_role``: optional role string ("editor" / "owner" / ...)
      * ``healthy``: True if a heartbeat field is present and recent;
        defaults to True for backwards compat with pre-Option-C servers
      * ``age_seconds``: numeric age of last poll, or None
      * ``age_str``: pre-rendered "age 3s" / "age 5m"
      * ``project_id``: actor.project_id if present (cross-project hint)

    Option C integration: when the server publishes ``poll_health`` per
    session (the parallel agent's true-heartbeat work), we read it. When
    it's missing we fall through to ``last_active`` and treat the session
    as healthy.
    """
    index = build_member_index(members or [])
    rows: list[dict] = []
    for i, s in enumerate(sessions or [], start=1):
        actor = s.get("actor") or {}
        poll = s.get("poll_health") or {}
        # Option C: poll_health.age_seconds + poll_health.healthy
        age = poll.get("age_seconds")
        healthy = poll.get("healthy")
        if healthy is None:
            # Pre-Option-C server: trust last_active presence as "live".
            healthy = bool(s.get("last_active"))
        member = resolve_member_for_session(s, index)
        email = (actor.get("email") or "").strip()
        member_email = (member or {}).get("email", "") if member else ""
        # ms-54 / e-1369: pull through the new 4 + 1 layer fields when the
        # session document carries them. All optional — a session predating
        # e-1369 simply omits them and the picker line silently drops the
        # corresponding bits.
        agent_meta = s.get("agent") or {}
        git_meta = s.get("git") or {}
        focus_meta = (s.get("focus") or {}).get("milestone") or {}
        intent_meta = s.get("intent") or {}
        rows.append({
            "index": i,
            "session_id": s.get("session_id", ""),
            "machine": actor.get("machine", ""),
            "agent": actor.get("agent", ""),
            "email": email or member_email,
            "member_id": (member or {}).get("id", "") if member else "",
            "member_role": (member or {}).get("role", "") if member else "",
            "healthy": bool(healthy),
            "age_seconds": age,
            "age_str": _format_age_seconds(age),
            # project_id: prefer the dm_discover server-first annotation
            # (set directly on the session dict by ms-62 / e-1502) over the
            # legacy actor.project_id field. Falls through to the actor
            # value when only the local discovery path was taken.
            "project_id": s.get("project_id") or actor.get("project_id", ""),
            # ms-62 / e-1504: project_name + project_role come from
            # GET /api/me/projects via dm_discover's invocation-scoped
            # memo. The picker renders them inline so the user can tell
            # cross-project candidates apart at a glance.
            "project_name": s.get("project_name", ""),
            "project_role": s.get("project_role", ""),
            # Layer 0 — Identity
            "agent_kind": agent_meta.get("kind", ""),
            "agent_version": agent_meta.get("version", ""),
            # Layer 1 — Where
            "cwd": s.get("cwd", ""),
            "git_branch": git_meta.get("branch", ""),
            "git_head_short": git_meta.get("head_short", ""),
            # Layer 2 — What
            "focus_milestone": focus_meta.get("id", "")
                if focus_meta else "",
            "focus_milestone_title": focus_meta.get("title", "")
                if focus_meta else "",
            # Layer 4 — Intent (Layer 3 reach is consulted separately by
            # the Skill when deciding whether the session can receive)
            "intent_text": intent_meta.get("text", "") or "",
            "attention_required": bool(intent_meta.get("attention_required")),
        })
    return rows


def render_candidate_line(row: dict) -> str:
    """Format one candidate row as a single picker line.

    The Skill prints these lines verbatim so the user can copy the
    session_id or type the index. ms-54 / e-1369 adds bridge-stamped
    Layer 0-3 metadata (cwd basename, git branch, focus.milestone) and
    AI-authored Layer 4 intent — all rendered when present, all silent
    when absent so legacy (pre-e-1369) sessions still surface cleanly.
    """
    parts = []
    parts.append(f"{row['index']}.")
    bits = []
    if row.get("machine"):
        bits.append(f"machine={row['machine']}")
    if row.get("agent") and row.get("agent") != row.get("machine"):
        bits.append(f"agent={row['agent']}")
    if bits:
        parts.append("[" + ", ".join(bits) + "]")
    parts.append(f"session={row['session_id'][:8]}…")
    parts.append("healthy" if row.get("healthy") else "stale")
    if row.get("age_seconds") is not None:
        parts.append(f"({row['age_str']})")
    # ms-54 / e-1369 Layer 1 + 2: where (cwd basename + branch) and what
    # (focus.milestone). The cwd basename alone is the most useful
    # disambiguator on a single machine — "/tools/beacon" vs
    # "/tools/beacon/.worktrees/ms-43" tells you immediately which
    # session is the parent and which is the dispatch sub-agent.
    cwd = row.get("cwd", "")
    git_branch = row.get("git_branch", "")
    if cwd or git_branch:
        loc_bits = []
        if cwd:
            loc_bits.append(_short_cwd(cwd))
        if git_branch:
            loc_bits.append(f"@{git_branch}")
        parts.append("[" + " ".join(loc_bits) + "]")
    focus_ms = row.get("focus_milestone", "")
    if focus_ms:
        parts.append(f"focus={focus_ms}")
    # Layer 0: agent.version when present — useful for "this session is on
    # v0.25.x so receipts won't fire" diagnosis.
    av = row.get("agent_version", "")
    if av:
        parts.append(f"v={av}")
    # Layer 4: AI-authored intent. Last in the line because it's the most
    # human-readable bit and the reader's eye lands here last.
    intent_text = row.get("intent_text", "")
    if intent_text:
        truncated = intent_text if len(intent_text) <= 60 \
            else intent_text[:57] + "…"
        parts.append(f"intent=\"{truncated}\"")
    if row.get("attention_required"):
        parts.append("⚠ attention")
    # ms-62 / e-1504: project annotation (= cross-project picker
    # disambiguation). Rendered as "project=<name>" before the email so
    # the reader sees identity first, then human label. Falls through to
    # bare project_id when the name isn't available (e.g. local-only
    # discovery, or the project endpoint returned empty name).
    proj_name = row.get("project_name", "")
    proj_id = row.get("project_id", "")
    if proj_name:
        proj_label = proj_name
        if row.get("project_role"):
            proj_label += f"/{row['project_role']}"
        parts.append(f"project={proj_label}")
    elif proj_id:
        parts.append(f"project={proj_id}")
    if row.get("email"):
        tail = row["email"]
        if row.get("member_role"):
            tail += f" ({row['member_role']})"
        parts.append("→ " + tail)
    elif row.get("member_id"):
        parts.append(f"→ member={row['member_id']}")
    return " ".join(parts)


def _short_cwd(cwd: str) -> str:
    """Render a cwd compactly for the picker line.

    Drops home prefix to `~/`, then keeps the last 2 path segments so the
    line stays scannable even when cwd is buried deep under the home dir.
    """
    import os
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    parts = cwd.rstrip("/").split("/")
    if len(parts) <= 3:
        return cwd
    return ".../" + "/".join(parts[-2:])


# ---------------------------------------------------------------------------
# CLI argv composition
# ---------------------------------------------------------------------------


def build_directory_cmd(
    *,
    since_min: int = 5,
    healthy: bool = True,
    project_id: str = "",
) -> list[str]:
    """Argv for `beacon bus directory --live --healthy --since-min N --json`.

    ``healthy=True`` adds the Option C filter; the Skill should fall back
    to ``healthy=False`` if the runtime CLI rejects ``--healthy`` (the
    parallel agent's PR may not have landed yet).
    """
    argv = ["beacon", "bus", "directory", "--live", "--json", "--since-min", str(since_min)]
    if healthy:
        argv.append("--healthy")
    if project_id:
        argv += ["--project", project_id]
    return argv


def build_send_cmd(
    *,
    recipient_session_id: str,
    text: str,
    channel: str = "dm",
    project_id: str = "",
    actions: Optional[list[str]] = None,
    in_reply_to: str = "",
    no_envelope: bool = False,
    json_output: bool = True,
) -> list[str]:
    """Compose `beacon bus send …` argv from typed inputs.

    All four dogfood failure modes are eliminated here:

      * ``recipient_session_id`` is required and rendered as ``--to``,
        so a missing recipient becomes a Python error before any send.
      * ``project_id`` (cross-project case) is rendered as ``--project``,
        defaulting to omitted (= cwd's project) when empty.
      * payload is JSON-encoded from ``text`` (and optional
        ``in_reply_to`` is stamped on the payload by the CLI itself, not
        by us, but ``--in-reply-to`` flag is wired through).
      * envelope auto-issue is on by default (channel=dm sends default
        to T1 envelope per e-1290); pass ``no_envelope=True`` only for
        debugging the legacy lane.
    """
    if not recipient_session_id:
        raise ValueError("recipient_session_id is required for dm send")
    if not isinstance(text, str):
        raise TypeError("text must be a string (will be JSON-encoded into payload.text)")

    payload = {"text": text}
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    argv = [
        "beacon", "bus", "send",
        "--channel", channel,
        "--to", recipient_session_id,
        "--payload", payload_json,
    ]
    if project_id:
        argv += ["--project", project_id]
    for action in (actions or []):
        action = action.strip()
        if not action:
            continue
        argv += ["--action", action]
    if in_reply_to:
        argv += ["--in-reply-to", in_reply_to]
    if no_envelope:
        argv.append("--no-envelope")
    if json_output:
        argv.append("--json")
    return argv


def quote_argv(argv: list[str]) -> str:
    """Render argv as a copy-pasteable shell line (for confirmation prompts)."""
    return " ".join(shlex.quote(a) for a in argv)


# ---------------------------------------------------------------------------
# Cross-project detection
# ---------------------------------------------------------------------------


def needs_cross_project_prompt(
    *,
    selected_session_project_id: str,
    cwd_project_id: str,
) -> bool:
    """Return True when the picked session belongs to a different project.

    Both arguments may be empty strings; the prompt only fires when both
    are non-empty and they disagree (so the typical same-project case
    stays silent).
    """
    a = (selected_session_project_id or "").strip()
    b = (cwd_project_id or "").strip()
    if not a or not b:
        return False
    return a != b


# ---------------------------------------------------------------------------
# Healthy fallback
# ---------------------------------------------------------------------------


def fallback_directory_cmd_if_no_healthy(
    *,
    sessions_with_healthy_filter: list[dict],
    since_min: int = 5,
    project_id: str = "",
) -> Optional[list[str]]:
    """Decide whether to re-query without ``--healthy``.

    The Option C contract says ``bus directory --healthy --json`` returns
    only sessions whose last poll proved liveness. If that list is empty,
    the Skill should re-run the directory query without ``--healthy`` so
    the user can still see live-but-not-poll-confirmed sessions (with a
    clear warning that delivery is uncertain).

    Returns the fallback argv list, or None if no fallback is needed.
    """
    if sessions_with_healthy_filter:
        return None
    return build_directory_cmd(
        since_min=since_min,
        healthy=False,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# Local-mode pre-flight check (ms-54 e-1330)
# ---------------------------------------------------------------------------


def check_local_mode_error(cwd_has_cloud_json: bool) -> Optional[str]:
    """Return a friendly error string when cwd is a local-mode project.

    Local-mode projects (``.beacon/cloud.json`` absent) cannot use the bus
    at all — the bridge fail-safe-aborts because there is no project_id
    to write events to. Rather than letting /beacon-dm-send proceed and
    fail at send time, the Skill should detect this up front and explain
    the prerequisite + how to switch to cloud mode.

    Args:
        cwd_has_cloud_json: True iff ``cwd/.beacon/cloud.json`` exists.

    Returns:
        None when DM is possible (cloud mode), else a multi-line
        explanation suitable for the Skill to print to the user.
    """
    if cwd_has_cloud_json:
        return None
    return (
        "このプロジェクトは local mode (cloud sync 無し) なので、bus DM は使えません。\n"
        "  理由: bus は cloud project_id を必要とします (DM は cloud 経由で配信されるため)\n"
        "  cloud mode に切り替えるには:\n"
        "    1. beacon auth login            # beacon-ai.dev で認証\n"
        "    2. beacon cloud setup           # cloud project を作成 / リンク\n"
        "  local CLI / hook 等の操作は cloud mode 切替後も従来通り使えます。"
    )
