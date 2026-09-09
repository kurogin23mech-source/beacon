"""ms-159 / e-6246 — `beacon attention`: which sessions are waiting on you, now.

The MS's payoff surface. Reads the session directory (whose rows now carry the
canonical ``state`` + ``state_since`` from e-6245), keeps only the human-attention
states, and prints them oldest-waiting first — so the human sees "the work that
has been starved longest" without opening a single terminal.

Scope:
  * default            — sessions of the current (cwd) project;
  * ``--all-projects`` — every project the caller belongs to (the full
    cross-project "one face" view; the whole point of the MS at scale).

Read-only. The filter/sort/duration logic is the pure ``lib/attention`` module;
this file is just the CLI shell (fetch → filter → render), mirroring
``cmd_bus_directory``.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import attention
from commands_shared import (
    _get_api_client, _resolve_bus_project_id, _read_credentials_for_identity)


def _fetch_sessions(client, config, all_projects: bool):
    """Fetch directory rows (cross-project or cwd). No ``live_only`` filter: a
    ``terminated:failed`` session is not live but is attention-worthy, and the
    pure filter drops everything non-attention anyway (a stale live-less session
    projects to ``unknown``/``terminated`` and is folded out)."""
    if all_projects:
        return client.list_user_sessions() or []
    project_id = _resolve_bus_project_id(config)
    return client.list_sessions(project_id) or []


def _row_identity(row: dict) -> str:
    """Compact 'who/where' suffix for a row (best-effort, never raises)."""
    actor = row.get("actor") or {}
    ident = actor.get("email") or actor.get("machine") or actor.get("agent") or ""
    cwd = row.get("cwd") or ""
    parts = [p for p in (ident, cwd) if p]
    return "  ".join(parts)


def _sid_short(sid: str) -> str:
    """Trailing 8 chars of a session id (the part humans eyeball)."""
    sid = sid or "?"
    return sid[-8:] if len(sid) > 8 else sid


def _render_attention_flat(rows, now):
    """The C+A 'who is waiting on me' view (--attention-only): a flat list of
    要対応 sessions, longest-waiting first."""
    if not rows:
        print("今あなたを待っているセッションはありません。")
        return
    print(f"{len(rows)} 件のセッションがあなたを待っています (待機の長い順):")
    for r in rows:
        state = r.get("state") or "?"
        waited = attention.format_wait(r.get("state_since"), now)
        activity = r.get("activity") or ""
        tgt = attention.target_label(r)
        act = f"  「{activity}」" if activity else ""
        print(f"  {state:<14} {tgt:<16} {_sid_short(r.get('session_id'))}  "
              f"待機 {waited}{act}".rstrip())


def _render_roster(groups, now):
    """The D-slice roster: all sessions grouped by root target, each row showing
    作業 target / 状態 / activity / 待機."""
    total = sum(len(items) for _, items in groups)
    if total == 0:
        print("表示するセッションがありません。")
        return
    print(f"セッション名簿 ({total} 件、root target ごと / 状態順):")
    for label, items in groups:
        print(f"\n▸ {label}  ({len(items)})")
        for r in items:
            state = r.get("state") or "?"
            tgt = attention.target_label(r)
            waited = attention.format_wait(r.get("state_since"), now)
            activity = r.get("activity") or ""
            act = f"  「{activity}」" if activity else ""
            print(f"    {state:<14} {tgt:<16} {_sid_short(r.get('session_id'))}"
                  f"  待機 {waited}{act}".rstrip())


def cmd_attention():
    """The ops面 (ms-159). Default: a roster of the caller's sessions grouped by
    root target (作業 target / 状態 / activity / 待機). ``--attention-only`` narrows
    to 要対応 (awaiting_human / blocked / terminated:failed) = the C+A view."""
    all_projects = os.environ.get("BEACON_ATTENTION_ALL_PROJECTS", "") == "1"
    json_out = os.environ.get("BEACON_JSON", "") == "1"
    attention_only = os.environ.get("BEACON_ATTENTION_ATTENTION_ONLY", "") == "1"
    # scope=self (既定) shows only my sessions; scope=team shows everyone in the
    # project (器: 方針3 — multi-user model, self-default view).
    scope = os.environ.get("BEACON_ATTENTION_SCOPE", "").strip().lower() or "self"
    # ms-159 review #739 (AX high): reject an out-of-vocab --scope instead of
    # letting it fall through to "team" — otherwise `--scope all` (or a typo)
    # silently ESCALATES scope (shows everyone), a silent no-op/over-share.
    if scope not in ("self", "team"):
        print(f"Error: --scope must be 'self' or 'team' (got '{scope}')",
              file=sys.stderr)
        sys.exit(2)
    # root filter (= which root target / project). Named --root (not --target) to
    # disambiguate from `session working --target <kind:id>` — see #739 AX.
    root_filter = os.environ.get("BEACON_ATTENTION_ROOT", "").strip()

    client, config = _get_api_client()
    _uid, my_email = _read_credentials_for_identity()
    sessions = _fetch_sessions(client, config, all_projects)
    rows = attention.filter_roster(
        sessions, my_identity=my_email, scope=scope,
        attention_only=attention_only, root_id=root_filter or None)
    now = datetime.datetime.now(datetime.timezone.utc)

    if json_out:
        print(json.dumps(rows, ensure_ascii=False))
        return

    if attention_only:
        _render_attention_flat(sorted(rows, key=attention.attention_sort_key), now)
    else:
        _render_roster(attention.group_by_root(rows), now)
