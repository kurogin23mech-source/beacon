"""Tests for scripts/bcodex-watcher.py (= ms-93 / e-2534 + e-2535).

The watcher keeps the Beacon Codex receive daemon alive so DMs reach the
session. These tests drive it with a fake ``beacon-codex-bridge`` that records
every subaction invocation and can be forced to fail — so we assert on the
robustness the old inline watcher lacked: pull-only start at t0, upgrade on
thread, retry with backoff, output logged (not swallowed), and a user-visible
warning when the daemon does not come up.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import textwrap
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER = REPO_ROOT / "scripts" / "bcodex-watcher.py"


def _make_fake_bridge(path: Path, calls_path: Path, control_path: Path) -> None:
    """A fake bridge that records argv and obeys a JSON control file.

    control: {"start_fail_times": N, "restart_fail_times": N, "daemon_alive": bool}
    Each failing call decrements its counter (persisted back to the control
    file) so ``start_fail_times: 2`` fails twice then succeeds on the third.
    ``status`` prints the daemon line based on ``daemon_alive``.
    """
    path.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            calls = Path({str(calls_path)!r})
            control = Path({str(control_path)!r})
            argv = sys.argv[1:]
            with calls.open("a", encoding="utf-8") as f:
                f.write(json.dumps(argv) + "\\n")
            action = argv[0] if argv else ""
            cfg = json.loads(control.read_text()) if control.is_file() else {{}}
            if action == "status":
                alive = cfg.get("daemon_alive", True)
                print("hook:    installed ✓")
                if alive:
                    print("daemon:  running ✓ (pid=1234)")
                else:
                    print("daemon:  not running")
                sys.exit(0)
            key = action + "_fail_times"
            remaining = int(cfg.get(key, 0))
            if remaining > 0:
                cfg[key] = remaining - 1
                control.write_text(json.dumps(cfg))
                print(f"{{action}}: simulated failure", file=sys.stderr)
                sys.exit(1)
            print(f"{{action}}: ok")
            sys.exit(0)
            """
        ).strip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _make_state_db(codex_home: Path, cwd: Path, tid: str = "thread-abc",
                   filename: str = "state_5.sqlite") -> None:
    """Write a state_<N>.sqlite with one fresh thread row for ``cwd``."""
    codex_home.mkdir(parents=True, exist_ok=True)
    db = codex_home / filename
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "create table if not exists threads "
            "(id text, cwd text, source text, updated_at integer)"
        )
        con.execute(
            "insert into threads (id, cwd, source, updated_at) values (?,?,?,?)",
            (tid, str(cwd.resolve()), "cli", int(time.time()) + 60),
        )
        con.commit()
    finally:
        con.close()


def _setup(tmp_path: Path, control: dict) -> dict:
    project = tmp_path / "project"
    (project / ".beacon" / "codex").mkdir(parents=True)
    calls_path = tmp_path / "bridge_calls.jsonl"
    calls_path.write_text("", encoding="utf-8")
    control_path = tmp_path / "control.json"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    bridge = tmp_path / "beacon-codex-bridge"
    _make_fake_bridge(bridge, calls_path, control_path)
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    return {
        "project": project,
        "calls_path": calls_path,
        "bridge": bridge,
        "codex_home": codex_home,
    }


def _run_watcher(ctx: dict, *, armed: bool = False, deadline: str = "2",
                 poll: str = "0.05", retries: str = "3") -> subprocess.CompletedProcess:
    args = [
        "python3", str(WATCHER),
        "--cwd", str(ctx["project"]),
        "--url", "ws://127.0.0.1:39990",
        "--bridge", str(ctx["bridge"]),
        "--beacon-bin", "/fake/beacon",
        "--install-root", str(REPO_ROOT),
        "--deadline-seconds", deadline,
        "--poll-interval", poll,
        "--retries", retries,
    ]
    if armed:
        args.append("--armed")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(ctx["codex_home"])
    env["BCODEX_WATCHER_BACKOFF"] = "0"  # keep retries instant in tests
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)


def _calls(ctx: dict) -> list[list[str]]:
    return [
        json.loads(line)
        for line in ctx["calls_path"].read_text().splitlines()
        if line.strip()
    ]


def _watcher_log(ctx: dict) -> str:
    log = ctx["project"] / ".beacon" / "codex" / "bcodex-watcher.log"
    return log.read_text(encoding="utf-8") if log.is_file() else ""


def test_pull_only_started_at_t0(tmp_path):
    # No Codex thread exists yet: the watcher must still bring the daemon up
    # pull-only so DMs are delivered before the first turn.
    ctx = _setup(tmp_path, {"daemon_alive": True})
    proc = _run_watcher(ctx, deadline="0.4")
    assert proc.returncode == 0, proc.stderr
    calls = _calls(ctx)
    start_calls = [c for c in calls if c and c[0] == "start"]
    assert start_calls, f"expected a pull-only start, got {calls}"
    # Pull-only means NO --app-server on the initial start.
    assert "--app-server" not in start_calls[0]


def test_upgrade_on_thread(tmp_path):
    # Once a thread appears, the watcher upgrades to app-server dispatch.
    ctx = _setup(tmp_path, {"daemon_alive": True})
    _make_state_db(ctx["codex_home"], ctx["project"], tid="thread-xyz")
    proc = _run_watcher(ctx, deadline="2")
    assert proc.returncode == 0, proc.stderr
    calls = _calls(ctx)
    restart_calls = [c for c in calls if c and c[0] == "restart"]
    assert restart_calls, f"expected an upgrade restart, got {calls}"
    r = restart_calls[0]
    assert "--app-server" in r
    assert "--codex-thread-id" in r and "thread-xyz" in r
    assert "--app-server-url" in r
    assert "--armed" not in r


def test_upgrade_on_thread_state_version_bump(tmp_path):
    # e-2533: a Codex schema bump names the db state_6.sqlite. The watcher must
    # still find the thread (glob for state_*.sqlite, pick the highest version)
    # instead of silently failing on a hard-coded state_5.sqlite.
    ctx = _setup(tmp_path, {"daemon_alive": True})
    _make_state_db(ctx["codex_home"], ctx["project"], tid="thread-v6",
                   filename="state_6.sqlite")
    proc = _run_watcher(ctx, deadline="2")
    assert proc.returncode == 0, proc.stderr
    restart_calls = [c for c in _calls(ctx) if c and c[0] == "restart"]
    assert restart_calls, f"expected an upgrade restart, got {_calls(ctx)}"
    r = restart_calls[0]
    assert "--codex-thread-id" in r and "thread-v6" in r


def test_upgrade_prefers_highest_state_version(tmp_path):
    # When both state_5 and state_6 exist (mid-migration), the watcher must
    # read the newer schema (state_6) so it sees the current thread.
    ctx = _setup(tmp_path, {"daemon_alive": True})
    _make_state_db(ctx["codex_home"], ctx["project"], tid="thread-old",
                   filename="state_5.sqlite")
    _make_state_db(ctx["codex_home"], ctx["project"], tid="thread-new",
                   filename="state_6.sqlite")
    proc = _run_watcher(ctx, deadline="2")
    assert proc.returncode == 0, proc.stderr
    restart_calls = [c for c in _calls(ctx) if c and c[0] == "restart"]
    assert restart_calls, f"expected an upgrade restart, got {_calls(ctx)}"
    assert "thread-new" in restart_calls[0]


def test_upgrade_passes_armed(tmp_path):
    ctx = _setup(tmp_path, {"daemon_alive": True})
    _make_state_db(ctx["codex_home"], ctx["project"])
    proc = _run_watcher(ctx, armed=True, deadline="2")
    assert proc.returncode == 0, proc.stderr
    restart_calls = [c for c in _calls(ctx) if c and c[0] == "restart"]
    assert restart_calls and "--armed" in restart_calls[0]


def test_start_retries_on_failure(tmp_path):
    # Bridge start fails twice then succeeds: the watcher must retry, not give
    # up after one shot (the exact bug that silently killed DM receive).
    ctx = _setup(tmp_path, {"daemon_alive": True, "start_fail_times": 2})
    proc = _run_watcher(ctx, deadline="0.4", retries="4")
    assert proc.returncode == 0, proc.stderr
    start_calls = [c for c in _calls(ctx) if c and c[0] == "start"]
    assert len(start_calls) >= 3, f"expected retries, got {len(start_calls)} starts"
    log = _watcher_log(ctx)
    assert "attempt" in log  # retries are recorded, not swallowed


def test_start_failure_warns_user(tmp_path):
    # Start fails all attempts: surface an explicit warning instead of dying
    # silently.
    ctx = _setup(tmp_path, {"daemon_alive": True, "start_fail_times": 99})
    proc = _run_watcher(ctx, deadline="0.4", retries="2")
    assert proc.returncode == 0, proc.stderr
    assert "could not start DM receive daemon" in proc.stderr
    assert "FAILED" in _watcher_log(ctx)


def test_daemon_not_alive_after_start_warns(tmp_path):
    # start returns ok but `status` reports the daemon is down: the post-start
    # liveness check must catch it and warn (= proposal 4).
    ctx = _setup(tmp_path, {"daemon_alive": False})
    proc = _run_watcher(ctx, deadline="0.4")
    assert proc.returncode == 0, proc.stderr
    assert "not running" in proc.stderr
    status_calls = [c for c in _calls(ctx) if c and c[0] == "status"]
    assert status_calls, "expected a liveness status check"
