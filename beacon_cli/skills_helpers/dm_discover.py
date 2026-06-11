"""Cross-project session discovery for /beacon-dm-send.

Two discovery sources are supported:

1. **Server-first (cloud-first identity, ms-62 / e-1502)**: ask the cloud
   server for the user's project memberships via ``GET /api/me/projects``,
   then query each project's ``beacon bus directory`` (= live session list)
   in parallel. This is the path that solves cross-machine discoverability —
   sessions on the user's other machines (= Mac+Win etc.) become visible
   without requiring a local bus.mjs to be running for that project.

2. **Local fallback (legacy ms-54 path)**: walk running bus.mjs processes
   via ps + lsof, read each cwd's ``.beacon/cloud.json`` for project_id,
   then query each project's ``beacon bus directory``. Only sees projects
   whose bridge is locally running on the same machine.

Source selection (env ``BEACON_DISCOVERY_SOURCE``):

  * ``auto`` (default): try server, fall back to local on 404 / auth /
    network failure. This is the v0.32.x migration compat behaviour —
    new users on new server get cross-machine, old users get the legacy
    path without breaking. v0.33.0 hard-cut will drop ``local`` entirely
    (see ms-62 task e-1513 migration plan).
  * ``server``: server only, no fallback (= strict cloud-first, useful
    for testing the new path in isolation).
  * ``local``: local only (= legacy ms-54 behaviour, useful for
    debugging or for users opted out of cloud-first identity).

Invocation-scoped memo (= cache):

  Within a single Python process, the server query is performed at most
  once. Subsequent calls to ``discover_and_aggregate`` reuse the cached
  ``me/projects`` response so the picker doesn't pay multiple HTTPs in a
  single Skill invocation. Cache is process-local — a new CLI invocation
  re-queries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


_BUS_MJS_NEEDLE = "channel/bus.mjs"

# Invocation-scoped memo (= module-level cache, cleared on process exit).
# ms-62 / e-1502: the SPEC requires that within one CLI process, the server
# query for "my project memberships" runs at most once, even if
# discover_and_aggregate is called multiple times (e.g. picker render +
# live-check). A None marker means "not yet queried"; an empty list means
# "queried, server returned no projects".
_me_projects_memo: list[dict] | None = None


def _discovery_source() -> str:
    """Return the configured discovery source.

    Defaults to ``auto`` (= try server, fall back to local). The
    ``BEACON_DISCOVERY_SOURCE`` env var lets users opt in to ``server``
    (strict) or ``local`` (legacy).
    """
    raw = (os.environ.get("BEACON_DISCOVERY_SOURCE") or "").strip().lower()
    if raw in ("server", "local", "auto"):
        return raw
    return "auto"


def _reset_memo_for_tests() -> None:
    """Clear the invocation-scoped memo. Test-only helper."""
    global _me_projects_memo
    _me_projects_memo = None


def query_me_projects() -> list[dict]:
    """Fetch the user's project memberships from the server (with memo).

    Returns ``[{id, name, role}, ...]`` on success. Raises ``RuntimeError``
    on server 404 (= old server without ms-62 endpoint), ``ConnectionError``
    on network failure, other ``RuntimeError`` subclasses on auth failure.
    The caller decides whether to fall back to local discovery.

    Result is memoised in ``_me_projects_memo`` for the lifetime of this
    process so repeated calls in the same CLI invocation pay one HTTP.
    """
    global _me_projects_memo
    if _me_projects_memo is not None:
        return _me_projects_memo
    # Lazy import: commands._get_api_client requires auth credentials,
    # which we don't want to load unless we actually need a server query.
    # The import is also slow (drags in firestore_client + httplib).
    import sys as _sys
    import os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _lib = _os.path.join(_here, "..", "..", "lib")
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    from commands import _get_api_client  # type: ignore
    client, _config = _get_api_client()
    result = client.me_list_projects()
    if not isinstance(result, list):
        result = []
    _me_projects_memo = result
    return result


def discover_project_ids_via_server() -> list[str]:
    """Return distinct project_ids the user is a member of (server-side).

    Raises ``RuntimeError`` / ``ConnectionError`` on server failure — the
    caller (``discover_and_aggregate``) catches and falls back to local
    discovery under ``BEACON_DISCOVERY_SOURCE=auto``.
    """
    projects = query_me_projects()
    seen: set[str] = set()
    out: list[str] = []
    for p in projects:
        pid = (p.get("id") or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def list_bus_bridge_processes() -> list[tuple[str, str]]:
    """Return ``[(pid, cwd), ...]`` for every locally-running bus.mjs bridge.

    Uses ``pgrep -f`` to find the node processes, then ``lsof -p`` to
    read each one's cwd. Both tools are universally available on macOS
    and Linux; we don't try to be portable to Windows (Beacon's
    bus.mjs runs on macOS/Linux dev machines today).

    Failure modes are silenced (returns ``[]`` rather than raising) —
    the worst case for the caller is "discovery found nothing", which
    naturally falls back to cwd-only behavior.
    """
    try:
        proc = subprocess.run(
            ["pgrep", "-f", _BUS_MJS_NEEDLE],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    pids = [p for p in proc.stdout.strip().split("\n") if p]
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


def _annotate_with_project_meta(
    rows: list[dict[str, Any]],
    projects_meta: list[dict] | None,
) -> None:
    """Annotate each row with project name (= picker UX, ms-62 / e-1504).

    No-op when ``projects_meta`` is None (= local discovery path, no
    server response to draw names from). The picker falls back to
    rendering the bare project_id in that case.
    """
    if not projects_meta:
        return
    name_by_id = {
        (p.get("id") or ""): (p.get("name") or "")
        for p in projects_meta
    }
    for row in rows:
        pid = row.get("project_id") or ""
        if pid and pid in name_by_id:
            row["project_name"] = name_by_id[pid]
            # role too — Skill picker shows owner/editor/viewer next to name
            for p in projects_meta:
                if p.get("id") == pid:
                    row["project_role"] = p.get("role") or ""
                    break


def discover_and_aggregate(
    *,
    healthy: bool = True,
    since_min: int = 5,
) -> list[dict[str, Any]]:
    """Cross-project session list, source-selected by env (ms-62 / e-1502).

    Returns a flat list of session rows, each annotated with a
    ``project_id`` field (= picker disambiguation). Server-first paths
    additionally stamp ``project_name`` / ``project_role`` so the picker
    can render a friendlier label without a second lookup.

    Source selection respects ``BEACON_DISCOVERY_SOURCE`` (see module
    docstring). Default is ``auto`` (= server-first, fall back to local
    on server failure).
    """
    source = _discovery_source()
    rows: list[dict[str, Any]] = []
    projects_meta: list[dict] | None = None

    if source in ("server", "auto"):
        try:
            projects_meta = query_me_projects()
            project_ids = []
            seen: set[str] = set()
            for p in projects_meta:
                pid = (p.get("id") or "").strip()
                if pid and pid not in seen:
                    seen.add(pid)
                    project_ids.append(pid)
            for project_id in project_ids:
                for session in query_project_directory(
                    project_id, healthy=healthy, since_min=since_min
                ):
                    session["project_id"] = project_id
                    rows.append(session)
            _annotate_with_project_meta(rows, projects_meta)
            return rows
        except (RuntimeError, ConnectionError, ImportError) as exc:
            if source == "server":
                # Strict server mode: re-raise so the caller knows.
                raise
            # auto mode: silent fallback to local on server failure.
            # The caller (Skill) sees the same shape — just degraded scope.
            sys.stderr.write(
                f"discovery: server failed ({type(exc).__name__}: {exc}), "
                f"falling back to local\n"
            )

    # Local fallback (= legacy ms-54 path).
    for project_id in discover_local_project_ids():
        for session in query_project_directory(
            project_id, healthy=healthy, since_min=since_min
        ):
            session["project_id"] = project_id
            rows.append(session)
    _annotate_with_project_meta(rows, projects_meta)
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
