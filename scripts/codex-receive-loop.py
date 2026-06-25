#!/usr/bin/env python3
"""Codex receive-loop daemon (= ms-93 / e-2497 phase 1 entry point).

Runs continuously in the background while a Codex CLI session is open.
Maintains a stable session_id, heartbeats to the server so the
freshness threshold (3600s in lib/session.py) never expires, polls the
bus inbox every few seconds, and persists incoming events for the
inbox hook to inject on the next user prompt.

This is the Codex equivalent of Claude Code's ``channel/bus.mjs``
bridge — the responsibilities are the same but the delivery mechanism
(file → hook, not MCP notification) is adapted to what Codex exposes.

Usage:
    BEACON_BIN=/abs/path/to/beacon python3 scripts/codex-receive-loop.py [--cwd /repo]

Environment knobs:
    BEACON_HEARTBEAT_SECS — heartbeat tick (default 30)
    BEACON_POLL_SECS       — inbox poll tick (default 2)
    BEACON_BUS_SENDER      — exported by this script for child CLI calls

The daemon writes its pid to ``<cwd>/.beacon/codex/receive-loop.pid``
so a second invocation can detect the existing instance and refuse
gracefully (= avoid two daemons fighting for one sid).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path


def _import_modules(install_root: Path):
    """Make ``lib`` importable, return (codex_session, codex_receive_loop, api_client)."""
    lib_dir = str(install_root / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import codex_session as cs  # noqa: E402
    import codex_receive_loop as crl  # noqa: E402
    import api_client as ac  # noqa: E402
    return cs, crl, ac


def _load_cloud_config(cwd: Path) -> dict:
    """Read ``<cwd>/.beacon/cloud.json`` + locate the auth token.

    Required outputs: ``project_id``, ``api_url``, ``id_token``.

    The token resolution mirrors the bus.mjs / CLI convention so the
    receive loop works in any setup ``beacon auth login`` produces:

    1. ``BEACON_AUTH_TOKEN`` env var (= explicit override)
    2. ``<cwd>/.beacon/cloud.json`` ``id_token`` (= legacy local copy)
    3. ``~/.beacon/credentials.json`` ``token`` (= default, post auth login)
    4. The active profile's ``~/.beacon/profiles/<active>/credentials.json``
    """
    path = cwd / ".beacon" / "cloud.json"
    if not path.is_file():
        raise RuntimeError(
            f"{path} not found — receive loop needs a cloud-mode project. "
            "Run `beacon cloud setup` in this cwd first."
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ("project_id", "api_url"):
        if not data.get(key):
            raise RuntimeError(f"{path} is missing '{key}'.")

    if not data.get("id_token"):
        env_token = os.environ.get("BEACON_AUTH_TOKEN", "").strip()
        if env_token:
            data["id_token"] = env_token
    if not data.get("id_token"):
        candidates = [
            Path(os.path.expanduser("~/.beacon/credentials.json")),
        ]
        # Active-profile path: best-effort, ignore IO errors.
        try:
            machine_path = Path(os.path.expanduser("~/.beacon/machine.json"))
            if machine_path.is_file():
                with machine_path.open("r", encoding="utf-8") as f:
                    m = json.load(f)
                active = m.get("active_profile") or m.get("profile") or ""
                if active:
                    candidates.append(
                        Path(os.path.expanduser(
                            f"~/.beacon/profiles/{active}/credentials.json"
                        ))
                    )
        except Exception:
            pass
        for cand in candidates:
            if not cand.is_file():
                continue
            try:
                with cand.open("r", encoding="utf-8") as f:
                    creds = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            token = creds.get("token") or creds.get("id_token") or ""
            if token:
                data["id_token"] = token
                break

    if not data.get("id_token"):
        raise RuntimeError(
            "No auth token found. Run `beacon auth login` first "
            "(checked BEACON_AUTH_TOKEN, cloud.json, ~/.beacon/credentials.json)."
        )
    return data


def _pid_file(cwd: Path) -> Path:
    return cwd / ".beacon" / "codex" / "receive-loop.pid"


def _write_pid_file(cwd: Path) -> None:
    path = _pid_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _check_existing_daemon(cwd: Path) -> int:
    """Return the live pid of an existing daemon, or 0 if none.

    A stale pid file (= process dead) is treated as none and overwritten.
    """
    path = _pid_file(cwd)
    if not path.is_file():
        return 0
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return 0
    except PermissionError:
        return pid
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Codex receive-loop daemon (= ms-93 / e-2497).",
    )
    parser.add_argument("--cwd", default="", help="Override working directory.")
    parser.add_argument(
        "--install-root", default="",
        help="Beacon source checkout (defaults to script-relative).",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration and exit (= for tests).")
    args = parser.parse_args()

    install_root = Path(args.install_root or Path(__file__).resolve().parent.parent)
    cwd = Path(args.cwd or os.getcwd())
    cs, crl, ac = _import_modules(install_root)

    config = _load_cloud_config(cwd)
    project_id = config["project_id"]
    api_url = config["api_url"]
    id_token = config.get("id_token", "")

    api = ac.ApiClient(api_url, token=id_token)

    # Existing-daemon guard.
    existing_pid = _check_existing_daemon(cwd)
    if existing_pid and existing_pid != os.getpid():
        sys.stderr.write(
            f"codex-receive-loop: another instance running (pid={existing_pid}).\n"
            "If you believe it is stale, remove .beacon/codex/receive-loop.pid.\n"
        )
        return 2

    session = cs.acquire_codex_session(
        project_id=project_id,
        cwd=str(cwd),
        parent_pid=os.getppid(),
        pid=os.getpid(),
        beacon_bin=os.environ.get("BEACON_BIN", ""),
    )
    session_file = cs.codex_session_path(
        project_id,
        cs.derive_stable_instance_key(str(cwd), os.getppid()),
    )

    # Export for any child CLI calls the user makes alongside.
    os.environ["BEACON_BUS_SENDER"] = session.session_id

    _write_pid_file(cwd)

    print(
        f"codex-receive-loop: started "
        f"sid={session.session_id} project={project_id} cwd={cwd}",
        flush=True,
    )

    state = {"stop": False, "last_heartbeat_at": 0.0, "since": ""}

    def _on_signal(_signum, _frame):
        state["stop"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    heartbeat_secs = int(os.environ.get("BEACON_HEARTBEAT_SECS", "30") or "30")
    poll_secs = int(os.environ.get("BEACON_POLL_SECS", "2") or "2")

    actor = {
        "machine": os.uname().nodename if hasattr(os, "uname") else "",
        "agent": "codex",
        "agent_kind": "codex",
    }

    try:
        while not state["stop"]:
            now = time.time()
            if now - state["last_heartbeat_at"] >= heartbeat_secs:
                ok = crl.heartbeat_to_server(
                    api, project_id=project_id,
                    session_id=session.session_id, actor=actor,
                    poll_interval_ms=poll_secs * 1000,
                )
                if ok:
                    cs.heartbeat_codex_session(session_file)
                    print(
                        f"codex-receive-loop: heartbeat ok",
                        flush=True,
                    )
                else:
                    print(
                        f"codex-receive-loop: heartbeat failed (transport)",
                        file=sys.stderr,
                        flush=True,
                    )
                state["last_heartbeat_at"] = now
            latest, persisted = crl.poll_inbox_once(
                api, project_id=project_id,
                session_id=session.session_id,
                since=state["since"], cwd=str(cwd),
            )
            state["since"] = latest
            if persisted:
                print(
                    f"codex-receive-loop: persisted {persisted} new event(s)",
                    flush=True,
                )
            if args.once:
                break
            time.sleep(poll_secs)
    finally:
        # Stamp shutdown=true on the server (= bus.mjs equivalent of
        # graceful close) so the directory immediately stops listing
        # this Codex sid as live.
        crl.heartbeat_to_server(
            api, project_id=project_id,
            session_id=session.session_id, actor=actor,
            poll_interval_ms=poll_secs * 1000, shutdown=True,
        )
        cs.close_codex_session(session_file, reason="signal")
        try:
            _pid_file(cwd).unlink()
        except OSError:
            pass
        print(
            f"codex-receive-loop: stopped sid={session.session_id}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
