#!/usr/bin/env python3
"""cmd_sessions.py — the `beacon sessions *` command family (ms-127 e-4321).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`;
family-private helpers are NOT re-exported (patch them at cmd_sessions.<name>).
"""

import json
import os
import sys

from commands_shared import _extract_token, _resolve_active_api_url


def cmd_sessions_list():
    """Cross-project session directory (ms-54 / e-1587).

    Unlike ``bus directory`` which is cwd-scoped to the current project, this
    command lists the calling user's live sessions across *all* their projects.
    Use it when the picker needs to see DM recipients in projects you are not
    currently cd'd into, or to diagnose heartbeat-stop incidents
    (e.g. e-1579-style auth-fail-cascade) without cd-ing through each candidate.

    Bootstraps the API client cwd-independently via the profile resolver
    (ms-64 / e-1458). Auth credentials come from the active profile too
    (e-1457). The resolver already honors the env > cwd cloud.json >
    profile.json > default precedence chain, so no extra cloud.json read
    is needed.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    api_url = _resolve_active_api_url()

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    sessions = client.list_user_sessions(
        live_only=os.environ.get("BEACON_SESSIONS_LIVE", "") == "1",
        since_minutes=int(os.environ.get("BEACON_SESSIONS_SINCE_MIN", "5") or "5"),
        healthy_only=os.environ.get("BEACON_SESSIONS_HEALTHY", "") == "1",
        machine=os.environ.get("BEACON_SESSIONS_MACHINE", "").strip(),
        agent=os.environ.get("BEACON_SESSIONS_AGENT", "").strip(),
    )

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(sessions, ensure_ascii=False))
        return

    if not sessions:
        print("(no matching sessions across your projects)")
        return

    for s in sessions:
        actor = s.get("actor") or {}
        sid = s.get("session_id", "?")
        email = actor.get("email", "")
        machine_s = actor.get("machine", "")
        agent_s = actor.get("agent", "")
        last = (s.get("last_active") or "")[:19]
        ident = " / ".join(p for p in (email, machine_s, agent_s) if p) or "(anon)"
        pid = s.get("project_id", "?")
        pname = s.get("project_name", "") or pid

        ph = s.get("poll_health") or {}
        healthy = ph.get("healthy")
        age = ph.get("age_seconds")
        shutdown = ph.get("shutdown")
        if shutdown:
            health_tag = "  health=shutdown"
        elif healthy is True:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=ok({age_str})"
        elif healthy is False:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=stale({age_str})"
        else:
            health_tag = "  health=unknown"
        print(f"  [{pname}]  {sid}  {ident}  last_active={last}{health_tag}")
