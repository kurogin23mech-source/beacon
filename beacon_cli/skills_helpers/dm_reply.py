"""Helpers for the /beacon-dm-reply Skill (ms-54 follow-up).

Same separation-of-concerns as ``dm_send``: the Skill markdown drives
the conversation (list recent DMs → user picks → user types text), and
this module turns the structured choices into the exact CLI invocations.

The reply path has one extra wrinkle vs. fresh send: the **budget gate**.
``beacon bus send --in-reply-to <event_id>`` is treated as an AI-authored
reply and refuses unless ``beacon bus budget grant N`` has been run
first. When the user just wants to reply once, they shouldn't see
"Error: auto-reply budget exhausted" — the Skill auto-grants a small
budget and retries. The helpers expose enough state for the Skill to do
this silently and report what it did.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Optional

from . import dm_send as _dm_send

# ---------------------------------------------------------------------------
# Inbox parsing
# ---------------------------------------------------------------------------


def parse_inbox_events(raw: str) -> list[dict]:
    """Parse stdout from ``beacon bus listen --once --auto-ack=0``.

    The listen CLI prints one JSON object per line. This helper accepts
    either:

      * the raw multi-line stdout text (newline-separated JSON objects), or
      * a single JSON array string (some test paths return that shape).

    Lines that fail to parse are silently dropped — a malformed event in
    the middle of a batch shouldn't blow up the inbox picker.
    """
    if not raw:
        return []
    text = raw.strip()
    if not text:
        return []
    # JSON-array path
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict)]
        except json.JSONDecodeError:
            pass
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def build_inbox_rows(events: list[dict], limit: int = 10) -> list[dict]:
    """Most-recent-first picker rows for received DM events.

    Each row carries enough state for the Skill to build the reply argv:

      * ``index`` (1-based)
      * ``event_id`` — used as ``--in-reply-to``
      * ``from_session`` — used as ``--to``
      * ``from_project`` — used as ``--project`` when cross-project
      * ``channel`` — typically ``dm``
      * ``created_at`` — for display
      * ``text_preview`` — first 80 chars of payload.text
    """
    # Sort newest first by created_at; fall back to incoming order if missing.
    def _key(e: dict) -> tuple[int, str]:
        ts = e.get("created_at") or ""
        return (1 if ts else 0, ts)

    ordered = sorted(events, key=_key, reverse=True)[:limit]
    rows: list[dict] = []
    for i, ev in enumerate(ordered, start=1):
        payload = ev.get("payload") or {}
        text = payload.get("text") or ""
        preview = text.replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        created = (ev.get("created_at") or "")[:19]
        rows.append({
            "index": i,
            "event_id": ev.get("event_id", ""),
            "from_session": ev.get("from_session", ""),
            "from_project": ev.get("from_project", ""),
            "channel": ev.get("channel", "dm"),
            "created_at": created,
            "text_preview": preview,
        })
    return rows


def render_inbox_line(row: dict) -> str:
    """Format one inbox row as a single picker line."""
    sender = (row.get("from_session") or "")[:8]
    chan = row.get("channel") or "dm"
    created = row.get("created_at") or ""
    bits = [f"{row['index']}.", f"[{created}]", f"from={sender}…", f"channel={chan}:"]
    preview = row.get("text_preview", "")
    if preview:
        bits.append(f"\"{preview}\"")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Budget gate
# ---------------------------------------------------------------------------


def parse_budget_show(raw: str) -> dict:
    """Parse ``beacon bus budget show --json`` output.

    Returns the parsed dict (passes through ``armed``, ``total``, ``used``,
    ``remaining``). On parse failure or empty input returns
    ``{"armed": False}`` — i.e. treat as "no budget" so the Skill grants.
    """
    if not raw or not raw.strip():
        return {"armed": False}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"armed": False}
    if not isinstance(data, dict):
        return {"armed": False}
    return data


def remaining_from_budget(budget: dict) -> int:
    """Return remaining auto-reply turns. 0 when unarmed or fully spent."""
    if not budget.get("armed"):
        return 0
    if "remaining" in budget:
        try:
            return max(int(budget["remaining"]), 0)
        except (TypeError, ValueError):
            pass
    try:
        total = int(budget.get("total", 0))
        used = int(budget.get("used", 0))
    except (TypeError, ValueError):
        return 0
    return max(total - used, 0)


def needs_auto_grant(budget: dict) -> bool:
    """True iff the reply Skill should silently grant before sending."""
    return remaining_from_budget(budget) <= 0


def build_budget_grant_cmd(turns: int = 3) -> list[str]:
    """Argv for the auto-grant the reply Skill issues when budget is 0.

    Default = 3: one for the in-flight reply, two spare in case the user
    keeps replying in the same dialog. Small enough that no autonomous
    loop can run away on a single grant.
    """
    if turns <= 0:
        raise ValueError("turns must be > 0")
    return ["beacon", "bus", "budget", "grant", "--turns", str(turns)]


def build_budget_show_cmd() -> list[str]:
    """Argv for `beacon bus budget show --json`."""
    return ["beacon", "bus", "budget", "show", "--json"]


# ---------------------------------------------------------------------------
# Reply send composition
# ---------------------------------------------------------------------------


def build_reply_cmd(
    *,
    parent_event: dict,
    text: str,
    cwd_project_id: str = "",
    json_output: bool = True,
) -> list[str]:
    """Compose `beacon bus send …` argv for a reply to ``parent_event``.

    ``parent_event`` is one of the inbox event dicts (as returned by
    ``parse_inbox_events`` / a row from ``build_inbox_rows`` annotated
    back to the event). We use:

      * ``parent.event_id`` → ``--in-reply-to`` (this is what makes the
        budget gate fire — forgetting it was one of today's dogfood pain
        points)
      * ``parent.from_session`` → ``--to`` (so the dm goes back to the
        sender, not broadcast)
      * ``parent.from_project`` → ``--project`` when different from
        ``cwd_project_id`` (cross-project replies)
      * ``parent.channel`` → ``--channel`` (default ``dm``)

    All three "remember to set this flag" failure modes from the dogfood
    session are taken away from the human here.
    """
    event_id = (parent_event.get("event_id") or "").strip()
    from_session = (parent_event.get("from_session") or "").strip()
    from_project = (parent_event.get("from_project") or "").strip()
    channel = (parent_event.get("channel") or "dm").strip() or "dm"

    if not event_id:
        raise ValueError("parent_event.event_id is required for a reply")
    if not from_session:
        raise ValueError("parent_event.from_session is required (cannot reply to broadcast)")
    if not isinstance(text, str):
        raise TypeError("text must be a string (will be JSON-encoded into payload.text)")

    project_id = ""
    if from_project and cwd_project_id and from_project != cwd_project_id:
        project_id = from_project

    return _dm_send.build_send_cmd(
        recipient_session_id=from_session,
        text=text,
        channel=channel,
        project_id=project_id,
        in_reply_to=event_id,
        json_output=json_output,
    )


def quote_argv(argv: list[str]) -> str:
    """Render argv as a copy-pasteable shell line."""
    return " ".join(shlex.quote(a) for a in argv)


# ---------------------------------------------------------------------------
# Inbox-listing CLI
# ---------------------------------------------------------------------------


def build_inbox_listen_cmd(
    *,
    recipient: str = "",
    channel: str = "dm",
    project_id: str = "",
) -> list[str]:
    """Argv for `beacon bus listen --once` (NO --auto-ack).

    We intentionally omit ``--auto-ack`` so peeking at the inbox does
    NOT consume events — the user may want to handle them in a different
    session or via the MCP hook. The Skill should also use the JSON line
    format that ``listen`` emits by default.

    ``recipient`` may be empty: the CLI resolves it from the current
    session_id, which is the right default for the reply Skill.
    """
    argv = ["beacon", "bus", "listen", "--once", "--channel", channel]
    if recipient:
        argv += ["--recipient", recipient]
    if project_id:
        argv += ["--project", project_id]
    return argv
