#!/usr/bin/env python3
"""user-scoped DM catch-up fetch for session-start (ms-54 e-2974, extracted ms-85 e-3178).

Previously inline in the session-start Skill (Step 1n-2) as an embedded
``python3 - <<'PY'`` heredoc. e-3178 extracts the fetch orchestration here
and the pure filter/format into ``lib/dm_pending`` so the moved logic is unit
tested (= dispatch AC: 単体テストで移設ロジックを担保).

Behavior is unchanged: query the server bus endpoint for ``dm`` channel
events since the last session-log timestamp (fallback 7 days), keep only
user-scoped events addressed to the current user, and print the catch-up
section. Prints nothing when there is nothing to show.

Fail-safe: local mode (no ``.beacon/cloud.json``) / unauthenticated /
endpoint timeout all silent-skip. Always exits 0.

Call sites:
  * /beacon-session-start Skill (Step 1n-2)
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


def _project_id() -> str:
    return str(_read_json(".beacon/cloud.json").get("project_id", "") or "")


def _user_id() -> str:
    return str(
        _read_json(os.path.expanduser("~/.beacon/auth.json")).get("user_id", "")
        or ""
    )


def _token() -> str:
    token = str(_read_json(".beacon/cloud.json").get("id_token", "") or "")
    if not token:
        token = str(
            _read_json(os.path.expanduser("~/.beacon/auth.json")).get("id_token", "")
            or ""
        )
    return token


def _since_iso() -> str:
    """Last session-log created_at, else 7 days ago (matches inline heredoc)."""
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


def main() -> int:
    project_id = _project_id()
    user_id = _user_id()
    if not project_id or not user_id:
        return 0  # local mode / unauthenticated -> silent skip

    base = os.environ.get("BEACON_API_BASE") or DEFAULT_BASE
    token = _token()
    q = urllib.parse.urlencode(
        {"channel": "dm", "since": _since_iso(), "limit": 100}
    )
    url = f"{base}/api/projects/{project_id}/bus?{q}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            events = json.loads(r.read().decode("utf-8"))
    except Exception:
        return 0  # endpoint unreachable / timeout -> silent skip

    from dm_pending import (  # noqa: E402
        filter_user_scoped_catchup,
        format_user_scoped_catchup,
    )

    rows = filter_user_scoped_catchup(events, user_id)
    section = format_user_scoped_catchup(rows)
    if section:
        print(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
