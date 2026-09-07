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

import attention
from commands_shared import _get_api_client, _resolve_bus_project_id


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


def cmd_attention():
    all_projects = os.environ.get("BEACON_ATTENTION_ALL_PROJECTS", "") == "1"
    json_out = os.environ.get("BEACON_JSON", "") == "1"

    client, config = _get_api_client()
    sessions = _fetch_sessions(client, config, all_projects)
    rows = attention.filter_attention(sessions)
    now = datetime.datetime.now(datetime.timezone.utc)

    if json_out:
        print(json.dumps(rows, ensure_ascii=False))
        return

    if not rows:
        scope = "全プロジェクト" if all_projects else "このプロジェクト"
        print(f"{scope}に、今あなたを待っているセッションはありません。")
        return

    print(f"{len(rows)} 件のセッションがあなたを待っています (待機の長い順):")
    for r in rows:
        state = r.get("state") or "?"
        sid = r.get("session_id") or "?"
        waited = attention.format_wait(r.get("state_since"), now)
        project = r.get("project_name") or r.get("project_id") or ""
        proj = f"[{project}] " if project else ""
        ident = _row_identity(r)
        tail = f"  {ident}" if ident else ""
        print(f"  {state:<14} {sid}  待機 {waited}  {proj}{tail}".rstrip())
