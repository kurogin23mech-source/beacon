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

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Lazy lib/stop_signal import (ms-55 e-1721 receive-side halt protocol)
# ---------------------------------------------------------------------------
#
# The inbox hook deliberately avoids importing lib/* at module load to
# keep startup snappy. The stop-signal processor only needs to run when
# the bus returned at least one event, so we defer the import + sys.path
# injection until then.

def _import_stop_signal():
    """Import lib/stop_signal lazily from the beacon source tree.

    Tries (in order):
      * neighbour ``lib/stop_signal.py`` relative to this script
        (= source checkout / editable install)
      * walk up from the script dir looking for a ``lib/stop_signal.py``
        (= installed alongside the bin/ tree)

    Returns the module on success, ``None`` on failure. Failures are
    silent — the receive-side halt is a layer over the existing inject,
    and the user still sees the stop event as a regular bus event.
    """
    candidates = []
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "lib")
    candidates.append(here.parent.parent / "lib")
    for lib_dir in candidates:
        candidate = lib_dir / "stop_signal.py"
        if candidate.exists():
            sys.path.insert(0, str(lib_dir))
            try:
                import stop_signal as _stop  # type: ignore[import-not-found]
            except Exception:
                return None
            return _stop
    return None


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


def _read_session_focus(root: Path) -> tuple[str, str]:
    """Return (ms_id, task_id) for this session.

    Reads `.beacon/session.json` for ``ms_id`` / ``task_id`` / focus
    fields. Either field may be empty when the session hasn't pinned
    its work yet; the stop-signal processor treats empty as "no match"
    which means scoped halts on the matching kind won't bind.
    """
    data = _read_json(root / ".beacon" / "session.json")
    ms_id = data.get("ms_id") or data.get("milestone_id") or ""
    task_id = data.get("task_id") or data.get("entry_id") or ""
    return str(ms_id or ""), str(task_id or "")


def _refresh_session_heartbeat(root: Path) -> None:
    """No-op since ms-54 e-1319 — retained as a deprecation tombstone.

    Originally (e-1189) this fire-and-forget'd ``beacon session id`` to bump
    ``.beacon/session.json.last_active`` and push to the cloud sessions/
    subcollection on every SessionStart / UserPromptSubmit. The intent was
    to keep long-running idle sessions visible in
    ``beacon bus directory --live``.

    Post Option C (PR #111 / commit 78048b6) the bridge's own poll loop
    stamps ``last_active`` + ``last_poll_at`` per iteration, which is the
    structural truth source — if the poll loop dies, the directory
    correctly stops advertising the session as receive-capable. A duplicate
    CLI-side write let a session appear "alive" while its bridge was dead,
    so DMs accumulated server-side unread.

    The function is intentionally left here (not removed) so external
    callers — should any exist — keep working. It is a no-op. Call sites
    inside this hook were stripped by e-1319; new code should not call it.
    """
    return


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


def _ensure_cursor_primed(api_url: str, project_id: str, recipient_id: str,
                          token: str) -> bool:
    """First-run cursor catchup for brand-new sessions.

    ms-95 e-2446 / 2026-06-25: /beacon-session-fork で作られた新規 session_id
    にはサーバー側 bus cursor が未設定のため、 ``/bus/unread`` を叩くと過去
    全イベント (= 古い operation-trigger / trek-trigger / test 系イベント等)
    が返り、 inbox-hook がそれを順次 AI context に inject して **完全暴走**
    する経路があった。 ``channel/bus.mjs`` には既に ``ensureCursorPrimed``
    があり同じ問題を bridge 側で防いでいたが、 inbox-hook 側にはなかった。

    cursor が未設定 (= 空 / epoch / 404) なら NOW に prime して True を返す
    (= caller は今回の poll を skip)。 cursor が既存値なら False (= 通常 poll
    を続行)。
    """
    cursor_path = (f"/api/projects/{project_id}/bus/cursors/"
                   f"{urllib.parse.quote(recipient_id, safe='')}")
    try:
        cur = _api_get(api_url, cursor_path, token)
        last = ""
        if isinstance(cur, dict):
            last = cur.get("last_seen_at") or ""
        is_unset = (not last) or last.startswith("1970-") or last == "0"
        if is_unset:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _ack_cursor(api_url, project_id, recipient_id, now, token)
            _log(f"first-run: cursor primed to {now} (no historical dispatch)")
            return True
        return False
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            try:
                _ack_cursor(api_url, project_id, recipient_id, now, token)
                _log(f"first-run (404): cursor primed to {now}")
            except Exception as ack_exc:
                _log(f"first-run prime ack failed: {ack_exc}")
            return True
        _log(f"cursor prime check HTTP {exc.code}: {exc}")
        return False
    except Exception as exc:
        _log(f"cursor prime check failed: {exc.__class__.__name__}: {exc}")
        return False


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


def _format_autonomous_action_block(events: list[dict]) -> str:
    """Render the "AUTONOMOUS ACTION" inject for armed operation-trigger events
    (ms-60 / e-1340 Phase B = e-1384).

    Each event in ``events`` is an opted-in auto-execute event on the
    ``operation-trigger`` channel. The block tells the AI to run
    ``/beacon-operation-execute`` **autonomously, without asking the user
    first**, and lifts the op_id / spec_doc_id / trigger_name out of the
    payload so the Skill can pick them up without re-parsing the raw event
    list below.

    Visually distinct from the generic propose-to-ai event list (different
    header, called out with `## AUTONOMOUS ACTION`) so the AI's instruction
    parser doesn't conflate the two.
    """
    if not events:
        return ""
    lines: list[str] = []
    lines.append("## AUTONOMOUS ACTION — operation autonomy active")
    lines.append("")
    lines.append(
        "以下の operation-trigger event は `bus_auto_execute_channels` "
        "allowlist に登録済 (= 人間 opt-in 済) のため、"
        "**確認を取らず autonomous に実行する**。"
    )
    lines.append("Run this autonomously without asking the user first.")
    lines.append("")
    for ev in events:
        payload = ev.get("payload") or {}
        op_id = payload.get("op_id", "?")
        spec_doc_id = payload.get("spec_doc_id", "")
        trigger_name = payload.get("trigger_name", "")
        eid = ev.get("event_id", "?")
        lines.append(f"- event_id: {eid}")
        lines.append(f"  - op_id: {op_id}")
        if spec_doc_id:
            lines.append(f"  - spec_doc_id: {spec_doc_id}")
        if trigger_name:
            lines.append(f"  - trigger_name: {trigger_name}")
        cmd = f"/beacon-operation-execute {op_id}"
        lines.append(f"  - launch: `{cmd}`  (no confirmation prompt)")
    lines.append("")
    lines.append(
        "budget が枯渇していたら Skill 側 (Step 4.5) が自動で停止 → incident "
        "open → note 残しに降格する。Skill の責務なのでこの inject 側で "
        "事前判定はしない。"
    )
    lines.append("")
    return "\n".join(lines)


def _format_trek_action_block(events: list[dict]) -> str:
    """Render the "TREK ACTION" inject for armed trek-trigger events
    (ms-75 / e-1870).

    Twin of ``_format_autonomous_action_block`` but scoped to a Trek
    (= 缶詰の徹夜作業部屋, the pre-approved AI autonomy workspace) rather
    than a single Operation. Each event in ``events`` is an opted-in
    auto-execute event on the ``trek-trigger`` channel. The block tells
    the AI to run ``/beacon-trek-execute <trek-id>`` **autonomously,
    without asking the user first**, and lifts the trek_id /
    trigger_name out of the payload so the Skill starts immediately.

    Visually distinct from the operation block so the AI's launcher does
    not mix the two (different header, different Skill name).
    """
    if not events:
        return ""
    lines: list[str] = []
    lines.append("## TREK ACTION — trek autonomy active")
    lines.append("")
    lines.append(
        "以下の trek-trigger event は `bus_auto_execute_channels` "
        "allowlist に登録済 (= 人間 opt-in 済) で、**Trek scope 内の "
        "事前承認に基づき確認なしで autonomous に実行する**。"
    )
    lines.append(
        "Trek scope (= MS / task / Operation) の作業は user 確認なしで "
        "進めてよい。**例外: デプロイ / リリース** (= 本番環境を触る "
        "アクション) のみ user 承認境界として escalate する。"
    )
    lines.append("Run this autonomously without asking the user first.")
    lines.append("")
    for ev in events:
        payload = ev.get("payload") or {}
        trek_id = payload.get("trek_id", "?")
        trigger_name = payload.get("trigger_name", "")
        eid = ev.get("event_id", "?")
        lines.append(f"- event_id: {eid}")
        lines.append(f"  - trek_id: {trek_id}")
        if trigger_name:
            lines.append(f"  - trigger_name: {trigger_name}")
        cmd = f"/beacon-trek-execute {trek_id}"
        lines.append(f"  - launch: `{cmd}`  (no confirmation prompt)")
    lines.append("")
    lines.append(
        "budget が枯渇していたら Skill 側が自動で停止 → incident open → "
        "note 残しに降格する。Skill の責務なのでこの inject 側で事前判定はしない。"
    )
    lines.append("")
    return "\n".join(lines)


def _render_context(events: list[dict], notify_only_count: int,
                    monitor_suggested: bool,
                    budget: dict | None = None,
                    auto_execute_downgraded_count: int = 0,
                    autonomous_actions: list[dict] | None = None,
                    trek_actions: list[dict] | None = None) -> str:
    """Build the additionalContext markdown for AI inject.

    ``autonomous_actions`` is the subset of ``events`` that survived the
    auto-execute allowlist gate AND landed on the ``operation-trigger``
    channel (ms-60 / e-1340 Phase B). When non-empty, an "AUTONOMOUS ACTION"
    block is emitted ABOVE the generic event list so the AI sees the
    instruction before the noise.

    ``trek_actions`` is the analogous subset for the ``trek-trigger``
    channel (ms-75 / e-1870). When non-empty, a "TREK ACTION" block is
    emitted right after the operation block.
    """
    parts: list[str] = []
    parts.append("BEACON BUS INBOX — 新着 event があります")
    parts.append("")
    autonomous_block = _format_autonomous_action_block(autonomous_actions or [])
    if autonomous_block:
        parts.append(autonomous_block)
    trek_block = _format_trek_action_block(trek_actions or [])
    if trek_block:
        parts.append(trek_block)
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

_DEBUG_LOG_PATH = Path("/tmp/beacon-bus-inbox-hook.log")


def _persist_debug(line: str) -> None:
    try:
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _emit(hook_event_name: str, context: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": context,
        }
    }
    payload = json.dumps(out, ensure_ascii=False)
    _persist_debug(
        f"=== emit ts={time.time():.3f} hook={hook_event_name} "
        f"ctx_len={len(context)} json_len={len(payload)} ===\n{payload}\n\n"
    )
    print(payload)


def _log(message: str) -> None:
    _persist_debug(f"[log ts={time.time():.3f}] {message}\n")
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

    # ms-54 / e-1319: the CLI heartbeat refresh was retired post Option C.
    # The bridge's poll loop now stamps both last_active and last_poll_at
    # per iteration (PR #111 / commit 78048b6), so a parallel CLI write would
    # only create ambiguity ("which signal is truth?"). The helper is left
    # as a no-op tombstone in case any external script still imports it.
    # _refresh_session_heartbeat(root)  # intentionally not called

    api_url, project_id = _load_cloud_config(root)
    if not api_url or not project_id:
        return

    token = _load_id_token()
    if not token:
        # No credentials → silent. Surfacing an error here would spam every
        # prompt; the user will see auth issues via normal beacon CLI calls.
        return

    started = time.monotonic()

    # ms-95 e-2446: Prime cursor for brand-new sessions (= /beacon-session-fork)
    # so we don't inject historical events. Mirrors channel/bus.mjs
    # ensureCursorPrimed. On first run, advance cursor to NOW and skip this
    # poll entirely — subsequent polls see only events created after this point.
    try:
        if _ensure_cursor_primed(api_url, project_id, session_id, token):
            return
    except Exception as exc:
        _log(f"cursor prime wrapper failed: {exc.__class__.__name__}: {exc}")
        # Non-fatal: fall through to the normal poll path.

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

    # ms-55 e-1721: process stop-signal channel events first so the
    # halt-request.json is on disk before the PostToolUse hook fires
    # again. Failures here are non-fatal — the regular event inject
    # still tells the AI "STOP signal arrived" even if file write is
    # broken.
    _stop = _import_stop_signal()
    if _stop is not None:
        try:
            ms_id, task_id = _read_session_focus(root)
            _stop.process_inbox_events(
                unread,
                session_id=session_id,
                ms_id=ms_id,
                task_id=task_id,
                beacon_dir=str(root / ".beacon"),
            )
        except Exception as exc:
            _log(f"stop-signal processor failed: {exc}")

    # Read the receiver-side auto-execute allowlist. Default-empty ⇒ every
    # auto-execute event gets downgraded to propose-to-ai before it reaches
    # the AI context (e-1145). The downgrade is annotated on the event itself
    # so _format_event can surface the safety action inline.
    allowlist = _read_auto_execute_channels(root)

    # Split by delivery. Unknown values fall back to propose-to-ai (matches
    # server-side coercion in BusEventCreate; defense in depth).
    inject: list[dict] = []
    notify_only: list[dict] = []
    autonomous_actions: list[dict] = []
    trek_actions: list[dict] = []
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
            elif channel == "operation-trigger":
                # ms-60 / e-1340 Phase B (e-1384): opted-in operation-trigger
                # events get a structured "AUTONOMOUS ACTION" block ABOVE the
                # generic event list, so the AI launches
                # /beacon-operation-execute without scanning the noise. The
                # event itself still appears in the regular list (for audit
                # parity) but the block carries the instruction.
                autonomous_actions.append(ev)
            elif channel == "trek-trigger":
                # ms-75 / e-1870: opted-in trek-trigger events get a
                # structured "TREK ACTION" block so the AI launches
                # /beacon-trek-execute <trek-id> without scanning the noise.
                # Same pattern as operation-trigger, twin scope-level entry.
                trek_actions.append(ev)
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
        # ms-95 / e-2710 — also persist a diagnostic frame so the next
        # post-mortem can read "what the hook saw at downgrade time" without
        # re-deriving it. The 2026-06-29 LPS dogfood post-mortem stalled for
        # hours on the question "did the local allowlist actually contain the
        # trek channels at the moment the hook fired?" — this frame answers
        # it inline so future cases close in minutes.
        try:
            pj_path = root / ".beacon" / "project.json"
            mtime = pj_path.stat().st_mtime if pj_path.exists() else None
            diag = {
                "_diag": "auto_execute_downgrade",
                "downgraded_count": downgraded_count,
                "downgraded_channels": sorted({
                    str(e.get("channel", "")) for e in downgraded_audit}),
                "allowlist_at_downgrade": list(allowlist),
                "project_json_path": str(pj_path),
                "project_json_mtime": mtime,
                "session_id": session_id,
            }
            _append_to_inbox_log(root, [diag])
        except Exception as exc:
            _log(f"diag frame write failed: {exc}")

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
                                            downgraded_count,
                                            autonomous_actions,
                                            trek_actions))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    _log(f"surfaced {len(inject)} event(s) ({elapsed_ms} ms)")


if __name__ == "__main__":
    main()
