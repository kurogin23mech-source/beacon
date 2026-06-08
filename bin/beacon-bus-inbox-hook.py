#!/usr/bin/env python3
"""beacon-bus-inbox-hook — surface unread bus events into AI context.

ms-54 / e-1140: the receiver-side daemon that closes the gap "unless the user
tells me to check the bus, I don't know there's a DM."

Activation
----------
Wire as a SessionStart and UserPromptSubmit hook in ~/.claude/settings.json.
Each fire:
  1. Find the project root (walk up from cwd looking for .beacon/project.json).
  2. Resolve the local session_id from .beacon/session.json.
  3. Pull events newer than the recipient's cursor via the cloud bus.
  4. Split events by `delivery`:
        propose-to-ai     → injected as additionalContext so the AI sees them
        auto-execute      → today, treated as propose-to-ai (opt-in enforcement
                            for auto-run is a follow-up — see "Roadmap" below)
        notify-user-only  → appended to .beacon/bus-inbox.log; NEVER injected
                            into the AI context (preserves the ms-31 "force is
                            never the default" principle for context inject)
  5. Atomically advance the per-recipient cursor so the same event never
     surfaces twice (e-998 forward-only ack semantics).

Hook protocol
-------------
Stdin: JSON the harness gave us (cwd, session_id, hook_event_name, ...).
Stdout (when there is something to surface): a single JSON object
        {"hookSpecificOutput": {"hookEventName": "<event>",
                                "additionalContext": "<markdown>"}}
Stdout (silent path): empty — silence is the default; no event ⇒ no output ⇒
        no context inject.

The script never raises to the harness: any error is swallowed and reported on
stderr so a flaky cloud call can't block the user's prompt from going through.

Roadmap — the "truly autonomous DM" picture
-------------------------------------------
The user's ideal is "even without ongoing conversation, the agent reacts to a
DM and replies, under a known turn budget." That needs three pieces, layered:

  * **e-1140 (this hook)** — pull events into context the next time the agent
    is already running a turn. Reactive, but tied to *something* triggering
    the agent (user prompt, session-start, post-tool-use).

  * **e-999 Monitor integration** — the agent arms a Monitor that long-polls
    `beacon bus listen --auto-ack`. Each incoming event becomes a notification
    that **reactivates** the agent on its own initiative, without a user
    prompt. This hook can suggest arming the Monitor in its inject body, and
    once armed the experience becomes push-driven rather than poll-driven.

  * **e-1000 budget gate** — the policy layer the agent obeys when it's
    proactively responding. Without a budget the autonomous loop can spin
    forever on token. With a budget the agent surfaces "X turns left" and
    halts before exhaustion.

Today this script delivers the first piece — events arrive in context the
next time the agent runs. The path to the full picture is in the inject
itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Project + session discovery
# ---------------------------------------------------------------------------

def _find_beacon_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a .beacon/project.json marker."""
    cur = start.resolve()
    while True:
        if (cur / ".beacon" / "project.json").exists():
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_session_id(root: Path, hook_input: dict) -> str:
    """Find the active session_id for the receiver-side filter.

    Prefer .beacon/session.json (always tagged with the current session); fall
    back to the hook input's session_id (the harness's view) so the script
    still works on the very first turn before session.json exists.
    """
    sess = _read_json(root / ".beacon" / "session.json")
    return sess.get("session_id") or hook_input.get("session_id") or ""


def _load_cloud_config(root: Path) -> tuple[str, str]:
    cloud = _read_json(root / ".beacon" / "cloud.json")
    return cloud.get("api_url", ""), cloud.get("project_id", "")


# ---------------------------------------------------------------------------
# Auth — minimal duplication of lib/auth.load_credentials.
# Calling beacon's full auth module from a hook costs ~80ms of import time and
# triggers SessionStart heartbeat upserts that recurse. We just need the OAuth
# id_token, so we read it directly.
# ---------------------------------------------------------------------------

def _load_id_token() -> str:
    creds_path = Path.home() / ".config" / "beacon" / "credentials.json"
    if not creds_path.exists():
        # Older layout some users might still have.
        alt = Path.home() / ".beacon" / "credentials.json"
        if alt.exists():
            creds_path = alt
        else:
            return ""
    data = _read_json(creds_path)
    return data.get("id_token") or data.get("token") or ""


# ---------------------------------------------------------------------------
# Cloud calls
# ---------------------------------------------------------------------------

def _api_get(api_url: str, path: str, token: str) -> object:
    req = urllib.request.Request(f"{api_url.rstrip('/')}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_post(api_url: str, path: str, body: dict, token: str) -> object:
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _list_unread(api_url: str, project_id: str, recipient_id: str,
                 token: str, limit: int = 50) -> list[dict]:
    qs = urllib.parse.urlencode({"recipient_id": recipient_id, "limit": limit})
    return _api_get(api_url, f"/api/projects/{project_id}/bus/unread?{qs}",
                    token) or []  # type: ignore[return-value]


def _ack_cursor(api_url: str, project_id: str, recipient_id: str,
                last_seen_at: str, token: str) -> None:
    _api_post(
        api_url,
        f"/api/projects/{project_id}/bus/cursors/"
        f"{urllib.parse.quote(recipient_id, safe='')}",
        {"last_seen_at": last_seen_at},
        token,
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _format_event(ev: dict) -> str:
    """Compact, readable one-event block for the AI context.

    A ``_downgraded_from`` marker on the event (set by main when an
    auto-execute event lands in a channel that isn't in the allowlist) is
    surfaced inline so the human auditing the inject can see at a glance
    that the safety net fired.
    """
    eid = ev.get("event_id", "?")
    channel = ev.get("channel", "?")
    sender = ev.get("sender_session_id", "?")
    payload = ev.get("payload") or {}
    delivery = ev.get("delivery", "propose-to-ai")
    downgraded_from = ev.get("_downgraded_from", "")
    when = (ev.get("created_at") or "")[:19]
    payload_pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    delivery_line = f"  - [{eid}] channel={channel} delivery={delivery}"
    if downgraded_from:
        delivery_line += (
            f"  [auto-execute downgraded from '{downgraded_from}'"
            " — channel not in bus_auto_execute_channels]"
        )
    return (
        f"{delivery_line}\n"
        f"    from={sender}  at={when}\n"
        f"    payload:\n"
        + "\n".join(f"      {line}" for line in payload_pretty.splitlines())
    )


def _read_bus_budget(root: Path) -> dict | None:
    """Read .beacon/bus-budget.json if it exists. Same schema as the CLI's
    _read_bus_budget — duplicated here to avoid importing the heavy
    lib/commands module from the hot path."""
    path = root / ".beacon" / "bus-budget.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_auto_execute_channels(root: Path) -> list[str]:
    """Read the project's auto-execute allowlist from .beacon/project.json.

    Fail-closed: any read error (missing file, corrupt JSON, wrong type) is
    treated as "no channels armed". The receiver-side downgrade in main() is
    the safety net — an unreadable allowlist must NOT be confused for "allow
    everything". This mirrors the bus_budget gate's posture.
    """
    path = root / ".beacon" / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("bus_auto_execute_channels")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def _format_budget_line(budget: dict | None) -> str:
    """Render the budget summary line for the inject body, or empty when not
    armed. Surfacing the remaining count is the "残りターン X" piece of the
    user's autonomous-DM vision — without it, the agent has no way to know
    when to slow down."""
    if budget is None:
        return ""
    total = int(budget.get("total", 0) or 0)
    used = int(budget.get("used", 0) or 0)
    if total <= 0:
        return ""
    remaining = max(total - used, 0)
    state = "exhausted" if remaining == 0 else "armed"
    return (
        f"BUDGET ({state}): {used}/{total} used, **{remaining} 回送信可能**. "
        "返信して send したらそのぶん残数が減る。0 になると "
        "`beacon bus send` が refuse する。"
    )


def _render_context(events: list[dict], notify_only_count: int,
                    monitor_suggested: bool,
                    budget: dict | None = None,
                    auto_execute_downgraded_count: int = 0) -> str:
    """Build the additionalContext markdown for AI inject."""
    parts: list[str] = []
    parts.append("BEACON BUS INBOX — 新着 event があります")
    parts.append("")
    budget_line = _format_budget_line(budget)
    if budget_line:
        parts.append(budget_line)
        parts.append("")
    parts.append(f"AI コンテキスト inject 対象: {len(events)} 件")
    if notify_only_count:
        parts.append(
            f"(notify-user-only として log にだけ流した event: {notify_only_count} 件 "
            "— `.beacon/bus-inbox.log` を参照)"
        )
    if auto_execute_downgraded_count:
        parts.append(
            f"(安全側降格: auto-execute → propose-to-ai に変換された event: "
            f"{auto_execute_downgraded_count} 件 — channel が "
            "`bus_auto_execute_channels` allowlist に無いため)"
        )
    parts.append("")
    for ev in events:
        parts.append(_format_event(ev))
        parts.append("")
    parts.append("--- 取り扱いガイド ---")
    parts.append(
        "- 返信する場合: `beacon bus send --channel <ch> --payload '<json>' "
        "--in-reply-to <event_id>` を使う。"
    )
    parts.append(
        "  **`--in-reply-to <event_id>` を必ず付ける**: これが付くと AI 返答と認定され "
        "budget gate (e-1000) が効く。budget 未付与の状態では refuse される "
        "(default は人間承認必須)。"
    )
    parts.append(
        "- 送信元 session_id を `--sender` で指定すれば DM の継続になる。"
    )
    parts.append(
        "- auto-execute: default OFF。`beacon bus auto-execute add --channel <ch>` "
        "で channel を allowlist に登録した場合のみ auto-execute として扱う。"
        "それ以外は自動的に propose-to-ai に降格 (人間 audit 可能、上記 "
        "`[auto-execute downgraded ...]` マーカー参照)。"
    )
    parts.append(
        "- notify-user-only: AI context には流していない (この一覧にも含まれない)。"
        "対応はユーザーが端末/UI から行う前提。"
    )
    if monitor_suggested:
        parts.append("")
        parts.append("--- 真の autonomous 化への提案 ---")
        parts.append(
            "現状は user prompt / session start を契機に inject される pull 駆動。"
            "Claude Code の Monitor を以下のように armed しておくと、"
            "新着 event 1 件ごとに通知が立って、prompt 無しでも気付ける push 駆動になる:"
        )
        parts.append("    beacon bus listen --auto-ack")
        parts.append(
            "1 セッション 1 Monitor で十分。budget gate (`bus budget grant <N>`) を "
            "先に grant しておくと N 往復で必ず止まる。"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _emit(hook_event_name: str, context: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": context,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


def _log(message: str) -> None:
    print(f"[bus-inbox-hook] {message}", file=sys.stderr)


def _append_to_inbox_log(root: Path, events: list[dict]) -> None:
    """Persist notify-user-only events so the user can review from the
    terminal / UI without polluting the AI context."""
    inbox_path = root / ".beacon" / "bus-inbox.log"
    try:
        with inbox_path.open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception as exc:
        _log(f"failed to write inbox log: {exc}")


def main() -> None:
    # Never let an exception escape — the harness must not block on the bus.
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_input = {}

    hook_event_name = hook_input.get("hook_event_name", "UserPromptSubmit")

    cwd = Path(hook_input.get("cwd") or os.getcwd())
    root = _find_beacon_root(cwd)
    if root is None:
        return  # not a beacon project — silent no-op

    session_id = _load_session_id(root, hook_input)
    if not session_id:
        return

    api_url, project_id = _load_cloud_config(root)
    if not api_url or not project_id:
        return

    token = _load_id_token()
    if not token:
        # No credentials → silent. Surfacing an error here would spam every
        # prompt; the user will see auth issues via normal beacon CLI calls.
        return

    started = time.monotonic()
    try:
        unread = _list_unread(api_url, project_id, session_id, token)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        _log(f"unread fetch failed ({exc.__class__.__name__}): {exc}")
        return
    except Exception as exc:
        _log(f"unexpected unread failure: {exc.__class__.__name__}: {exc}")
        return

    if not isinstance(unread, list) or not unread:
        return

    # Read the receiver-side auto-execute allowlist. Default-empty ⇒ every
    # auto-execute event gets downgraded to propose-to-ai before it reaches
    # the AI context (e-1145). The downgrade is annotated on the event itself
    # so _format_event can surface the safety action inline.
    allowlist = _read_auto_execute_channels(root)

    # Split by delivery. Unknown values fall back to propose-to-ai (matches
    # server-side coercion in BusEventCreate; defense in depth).
    inject: list[dict] = []
    notify_only: list[dict] = []
    downgraded_count = 0
    downgraded_audit: list[dict] = []
    for ev in unread:
        delivery = ev.get("delivery") or "propose-to-ai"
        if delivery == "auto-execute":
            channel = ev.get("channel") or ""
            if channel not in allowlist:
                # Mutate a copy so we don't change the upstream object the
                # cursor-advance step still inspects for created_at.
                ev = {**ev, "delivery": "propose-to-ai",
                       "_downgraded_from": "auto-execute"}
                downgraded_count += 1
                downgraded_audit.append(ev)
                delivery = "propose-to-ai"
        if delivery == "notify-user-only":
            notify_only.append(ev)
        else:
            inject.append(ev)

    if notify_only:
        _append_to_inbox_log(root, notify_only)
    if downgraded_audit:
        # The inbox log doubles as the audit trail: every downgrade is recorded
        # there with its full payload so a human can review what was forced
        # into propose-to-ai even when the AI handled it inline.
        _append_to_inbox_log(root, downgraded_audit)

    # Always advance the cursor to the last seen event so neither inject nor
    # notify-only events are replayed; the cursor is delivery-agnostic.
    last_seen = unread[-1].get("created_at", "")
    if last_seen:
        try:
            _ack_cursor(api_url, project_id, session_id, last_seen, token)
        except Exception as exc:
            _log(f"cursor advance failed: {exc}")
            # Even if ack fails, surface the events so the user isn't left
            # unaware. The next run will replay them (idempotent on context
            # if not on action).

    if not inject:
        return  # only notify-user-only this round → no context inject

    # The "suggest a Monitor" line is most useful at SessionStart, when the
    # session is fresh and the user might not have armed anything yet. On
    # later prompts it's noise.
    monitor_suggested = hook_event_name == "SessionStart"

    # Read the budget gate state so the AI knows how many sends it has left
    # before refuse. The hook never mutates the budget — only `bus send`
    # decrements; the hook is read-only.
    budget = _read_bus_budget(root)

    _emit(hook_event_name, _render_context(inject, len(notify_only),
                                            monitor_suggested, budget,
                                            downgraded_count))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    _log(f"surfaced {len(inject)} event(s) ({elapsed_ms} ms)")


if __name__ == "__main__":
    main()
