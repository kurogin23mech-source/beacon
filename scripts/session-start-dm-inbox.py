#!/usr/bin/env python3
"""Single-pass DM inbox fetch for session-start (ms-85 e-3180).

Consolidates the two DM detectors that session-start previously ran as
separate blocks, each re-resolving project_id / user_id / id_token and
opening its own HTTPS call:

  * Step 1n   — pending cross-user DM *action* envelopes
                (GET /api/projects/{pid}/dm/pending?receiver_user_id=...)
  * Step 1n-2 — user-scoped information DM catch-up
                (GET /api/projects/{pid}/bus?channel=dm&since=...)

e-3180 keeps the two rendered sections byte-for-byte identical but resolves
auth **once** and prints both sections from one invocation, halving the
per-DM-detector setup and giving session-start a single call site instead of
two. Server round-trip count for the DM concern is unchanged in the worst
case (the two endpoints are distinct), but the duplicated credential
resolution and the second ``python3`` process are removed.

Output contract (unchanged):
  * pending action section first (``format_pending_dm_summary``), then the
    user-scoped catch-up section (``format_user_scoped_catchup``), each
    omitted when empty. A blank line separates them only when both present.
  * local mode / unauthenticated / endpoint failure -> that section is
    silently skipped. Always exits 0.

Call sites:
  * /beacon-session-start Skill (Steps 1n + 1n-2, merged)
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

DEFAULT_BASE = "https://beacon-api-prod-2dlj7zlbiq-uc.a.run.app"


def _read_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _resolve_context() -> tuple[str, str, str, str]:
    """Return (project_id, user_id, token, base) resolved once for both fetches."""
    cloud = _read_json(".beacon/cloud.json")
    auth = _read_json(os.path.expanduser("~/.beacon/auth.json"))
    project_id = str(cloud.get("project_id", "") or "")
    user_id = str(auth.get("user_id", "") or "")
    token = str(cloud.get("id_token", "") or "") or str(auth.get("id_token", "") or "")
    base = os.environ.get("BEACON_API_BASE") or DEFAULT_BASE
    return project_id, user_id, token, base


def _get_json(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _pending_action_section(project_id, user_id, token, base) -> str:
    """Step 1n: pending cross-user DM action envelopes."""
    from dm_pending import format_pending_dm_summary
    q = urllib.parse.urlencode({"receiver_user_id": user_id})
    url = f"{base}/api/projects/{project_id}/dm/pending?{q}"
    try:
        rows = _get_json(url, token)
    except Exception:
        return ""  # endpoint unreachable -> silent skip
    try:
        return format_pending_dm_summary(rows)
    except Exception:
        return ""


def _since_iso() -> str:
    """Last session-log created_at, else 7 days ago (unchanged from Step 1n-2)."""
    try:
        r = subprocess.run(
            ["beacon", "session", "log", "list", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        logs = json.loads(r.stdout or "[]")
        if logs:
            v = str(logs[0].get("created_at", "") or "")
            if v:
                return v
    except Exception:
        pass
    return (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    ).isoformat().replace("+00:00", "Z")


def _catchup_section(project_id, user_id, token, base) -> str:
    """Step 1n-2: user-scoped information DM catch-up."""
    from dm_pending import filter_user_scoped_catchup, format_user_scoped_catchup
    q = urllib.parse.urlencode({"channel": "dm", "since": _since_iso(), "limit": 100})
    url = f"{base}/api/projects/{project_id}/bus?{q}"
    try:
        events = _get_json(url, token)
    except Exception:
        return ""  # endpoint unreachable -> silent skip
    rows = filter_user_scoped_catchup(events, user_id)
    return format_user_scoped_catchup(rows)


def main() -> int:
    project_id, user_id, token, base = _resolve_context()
    if not project_id or not user_id:
        return 0  # local mode / unauthenticated -> silent skip

    sections = [
        _pending_action_section(project_id, user_id, token, base),
        _catchup_section(project_id, user_id, token, base),
    ]
    out = "\n\n".join(s for s in sections if s)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
