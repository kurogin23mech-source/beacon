"""Cross-project session discovery for /beacon-dm-send (ms-54 follow-up).

Walks running bus.mjs processes via ps + lsof, reads each cwd's
``.beacon/cloud.json`` for project_id, then queries each project's
``beacon bus directory`` and aggregates the results — so the picker
can see sessions across every cloud-mode project the user has a
bridge running for on this machine, not just sessions in the cwd's
project.

Why this exists:

Today's dogfood revealed that the original /beacon-dm-send Skill only
queried the cwd's project directory. When the user spun up a fresh
session in a different cloud-mode project (e.g. test-beacon), the
target session was structurally invisible to the Skill, even though
its bridge was heartbeating fine. We could find it manually via ps +
lsof; the Skill couldn't. That was a Skill-implementation gap, not a
CLI-primitive gap — bus directory already supports --project. This
module wraps the "enumerate locally-running bridges then aggregate"
recipe so the Skill can just call:

    python3 -m beacon_cli.skills_helpers.dm_discover

to get a flat candidate list with each row annotated by project_id.

Scope:

  * **same machine, cross-project**: covered (ps/lsof finds every
    bus.mjs cwd, reads its cloud.json).
  * **different machine, cross-project**: NOT covered here — needs a
    server-side cross-project directory (e-1255 reverse lookup).

This is the "short-term (a)" path agreed in today's discussion; (b)
and (c) (cloud-side aggregation, session→project resolve) are
separate tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


# ms-43 follow-up: process discovery needles.
#
# ``beacon-bus:`` (NEW): bus.mjs sets ``process.title = beacon-bus:<cwd>``
#   on startup since ms-60 / e-1466 (commit bb7e9d0). ``pgrep -f`` matches
#   on the title for these processes, so this is the live needle.
#
# ``channel/bus.mjs`` (LEGACY): older bus.mjs builds (pre-e-1466) keep the
#   raw ``node /path/to/channel/bus.mjs`` command line. Kept as a fallback
#   so a long-lived bridge still appears in cross-project discovery until
#   it is restarted.
#
# Without the NEW needle, ``pgrep -f`` against the legacy string silently
# returned 0 processes from the moment e-1466 landed — which is why the
# Skill-level cross-project enumeration started returning an empty list
# while everyone assumed it was still working.
_BUS_MJS_NEEDLES: tuple[str, ...] = ("beacon-bus:", "channel/bus.mjs")


def list_bus_bridge_processes() -> list[tuple[str, str]]:
    """Return ``[(pid, cwd), ...]`` for every locally-running bus.mjs bridge.

    Uses ``pgrep -f`` against both the new (``beacon-bus:`` process title)
    and legacy (``channel/bus.mjs`` raw command line) needles, then
    ``lsof -p`` to read each pid's cwd. Both tools are universally
    available on macOS and Linux; we don't try to be portable to Windows
    (Beacon's bus.mjs runs on macOS/Linux dev machines today).

    Failure modes are silenced (returns ``[]`` rather than raising) —
    the worst case for the caller is "discovery found nothing", which
    naturally falls back to cwd-only behavior.
    """
    pids: list[str] = []
    seen: set[str] = set()
    for needle in _BUS_MJS_NEEDLES:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", needle],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for pid in proc.stdout.strip().split("\n"):
            if pid and pid not in seen:
                seen.add(pid)
                pids.append(pid)
    out: list[tuple[str, str]] = []
    for pid in pids:
        try:
            lsof = subprocess.run(
                ["lsof", "-p", pid],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for line in lsof.stdout.split("\n"):
            parts = line.split()
            # lsof line shape: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
            # For cwd we expect FD == "cwd".
            if len(parts) > 4 and parts[3] == "cwd":
                out.append((pid, parts[-1]))
                break
    return out


def discover_local_project_ids() -> list[str]:
    """Return sorted distinct project_ids from every cwd that hosts a
    cloud-mode beacon project (i.e. has ``.beacon/cloud.json``)."""
    project_ids: set[str] = set()
    for _pid, cwd in list_bus_bridge_processes():
        cloud_path = os.path.join(cwd, ".beacon", "cloud.json")
        if not os.path.exists(cloud_path):
            continue
        try:
            with open(cloud_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        pid = (data.get("project_id") or "").strip()
        if pid:
            project_ids.add(pid)
    return sorted(project_ids)


def query_project_directory(
    project_id: str,
    *,
    healthy: bool = True,
    since_min: int = 5,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    """Run ``beacon bus directory --project <id> --json`` and return the parsed list.

    Returns ``[]`` on any CLI / parse error. ``healthy=True`` adds the
    Option C ``--healthy`` filter; if the runtime CLI doesn't support
    it (pre-PR #111 install), the caller can retry with ``healthy=False``.
    """
    argv = [
        "beacon",
        "bus",
        "directory",
        "--live",
        "--json",
        "--since-min",
        str(since_min),
        "--project",
        project_id,
    ]
    if healthy:
        argv.append("--healthy")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        # If --healthy is the cause (unsupported flag), retry without it.
        if healthy and "unrecognized" in proc.stderr.lower():
            return query_project_directory(
                project_id, healthy=False, since_min=since_min,
                timeout_seconds=timeout_seconds,
            )
        return []
    out = proc.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def discover_and_aggregate(
    *,
    healthy: bool = True,
    since_min: int = 5,
) -> list[dict[str, Any]]:
    """Cross-project session list across all locally-running bridges.

    Returns a flat list of session rows, each annotated with a
    ``project_id`` field so the picker can disambiguate cross-project
    targets. Sessions are returned in the order projects are
    discovered (sorted by project_id), and within a project in the
    order ``bus directory`` returned them.

    Empty list = "no locally-running cloud-mode bridges visible" — the
    Skill should fall back to cwd-only behavior (and warn the user
    that DM is only possible to sessions whose bridge they can see).
    """
    rows: list[dict[str, Any]] = []
    for project_id in discover_local_project_ids():
        for session in query_project_directory(
            project_id, healthy=healthy, since_min=since_min
        ):
            session["project_id"] = project_id  # annotate
            rows.append(session)
    return rows


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python3 -m beacon_cli.skills_helpers.dm_discover``.

    Prints the aggregated candidate list as a single JSON array on
    stdout. The Skill consumes this with ``jq`` or Python to render
    the picker.
    """
    if argv is None:
        argv = sys.argv[1:]
    healthy = "--no-healthy" not in argv
    since_min = 5
    for i, a in enumerate(argv):
        if a == "--since-min" and i + 1 < len(argv):
            try:
                since_min = int(argv[i + 1])
            except ValueError:
                pass
    rows = discover_and_aggregate(healthy=healthy, since_min=since_min)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
