#!/usr/bin/env python3
"""bcodex DM-wake watcher (= ms-93 / e-2534 + e-2535 daemon lifecycle 堅牢化).

`bin/bcodex` launches this in the background after starting the Codex
app-server + TUI. Its job is to keep the Beacon Codex receive daemon alive so
DMs reach the session, using the two-phase design agreed by two AI bodies
(memo ``Odd5UclF3fXXJTNMSjnB``):

1. **pull-only start at t0** — before any Codex thread exists, start the
   receive daemon in pull-only mode (no ``--app-server``, no thread id needed).
   DMs are delivered from the very first moment, closing the pre-first-turn
   drop window (= proposal 3, the 本命).
2. **upgrade on thread** — once the Codex TUI thread appears in
   ``state_5.sqlite``, ``restart`` the daemon with ``--app-server`` +
   ``--codex-thread-id`` so pushed DMs *wake* Codex, not just queue.

Robustness (the whole point of this rewrite — the old inline watcher fired a
single ``restart``, swallowed its output to ``DEVNULL``, ignored the return
code, never retried, and never verified the daemon actually came up):

- **log, don't swallow** — every bridge call's output goes to
  ``<cwd>/.beacon/codex/bcodex-watcher.log`` (= proposal 1).
- **retry with backoff** — a non-zero bridge return code is retried a few
  times before giving up (= proposal 2).
- **verify liveness** — after start/restart, confirm the daemon is actually
  running via ``bridge status``; if not, surface an explicit warning to the
  user's stderr instead of dying silently (= proposal 4).

The watcher exits when: the wrapper process (``--wrapper-pid``) is gone, the
deadline elapses, or the upgrade to app-server dispatch succeeds (after which
the detached daemon runs independently and the watcher has no more work).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Watcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.cwd = str(Path(args.cwd).resolve())
        self.url = args.url
        self.bridge = args.bridge
        self.beacon_bin = args.beacon_bin
        self.install_root = args.install_root
        self.armed = bool(args.armed)
        self.wrapper_pid = int(args.wrapper_pid) if args.wrapper_pid else 0
        self.deadline_seconds = float(args.deadline_seconds)
        self.poll_interval = float(args.poll_interval)
        self.retries = int(args.retries)
        # Backoff between bridge retries. Exponential (2**attempt, capped 8s) by
        # default; a fixed override via BCODEX_WATCHER_BACKOFF keeps tests fast.
        override = os.environ.get("BCODEX_WATCHER_BACKOFF", "").strip()
        self.backoff_override = float(override) if override else None

        codex_home = Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        )
        self.db = codex_home / "state_5.sqlite"

        self.log_path = Path(self.cwd) / ".beacon" / "codex" / "bcodex-watcher.log"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.started = int(time.time())
        self.seen: set[str] = set()

    # -- logging / stderr ---------------------------------------------------

    def log(self, msg: str) -> None:
        line = f"{_now()} {msg}"
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def warn_user(self, msg: str) -> None:
        """User-visible warning: silent failure is exactly what we're killing."""
        print(f"[bcodex] {msg}", file=sys.stderr)
        self.log(f"USER-WARNING: {msg}")

    # -- bridge plumbing ----------------------------------------------------

    def _env(self) -> dict:
        return {
            **os.environ,
            "BEACON_BIN": self.beacon_bin,
            "BEACON_INSTALL_ROOT": self.install_root,
        }

    def run_bridge(self, action: str, *extra: str) -> bool:
        """Run a bridge subaction with retry + backoff, output captured to log.

        Returns True on the first zero return code, False after all retries.
        """
        argv = [sys.executable, self.bridge, action, "--cwd", self.cwd, *extra]
        for attempt in range(1, self.retries + 1):
            try:
                proc = subprocess.run(
                    argv,
                    env=self._env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception as exc:  # pragma: no cover - defensive
                self.log(f"{action} attempt {attempt}/{self.retries} raised: {exc}")
            else:
                out = (proc.stdout or "").strip()
                if proc.returncode == 0:
                    self.log(f"{action} ok (attempt {attempt}/{self.retries}): {out}")
                    return True
                self.log(
                    f"{action} attempt {attempt}/{self.retries} "
                    f"rc={proc.returncode}: {out}"
                )
            if attempt < self.retries:
                delay = (
                    self.backoff_override
                    if self.backoff_override is not None
                    else min(2 ** attempt, 8)
                )
                time.sleep(delay)
        self.log(f"{action} FAILED after {self.retries} attempts")
        return False

    def daemon_alive(self) -> bool:
        """Ask the bridge whether the receive-loop daemon is actually running.

        Closes the gap where ``cmd_start`` returns 0 on the slow-start path
        (= pidfile not yet visible) even though the daemon may not be up.
        """
        argv = [sys.executable, self.bridge, "status", "--cwd", self.cwd]
        try:
            proc = subprocess.run(
                argv,
                env=self._env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self.log(f"status raised: {exc}")
            return False
        return "daemon:  running" in (proc.stdout or "")

    def latest_thread(self) -> str:
        if not self.db.is_file():
            return ""
        try:
            con = sqlite3.connect(str(self.db))
        except sqlite3.Error as exc:
            self.log(f"sqlite connect failed: {exc}")
            return ""
        try:
            rows = con.execute(
                "select id, updated_at from threads where cwd=? "
                "and source in ('vscode','cli') order by updated_at desc limit 8",
                (self.cwd,),
            ).fetchall()
        except sqlite3.Error as exc:
            self.log(f"sqlite query failed: {exc}")
            return ""
        finally:
            con.close()
        for tid, updated in rows:
            if tid in self.seen:
                continue
            if int(updated or 0) >= self.started - 5:
                return str(tid)
        return ""

    # -- phases -------------------------------------------------------------

    def start_pull_only(self) -> None:
        """Phase 1: bring the receive daemon up before any thread exists."""
        if self.run_bridge("start"):
            if not self.daemon_alive():
                self.warn_user(
                    "DM receive daemon reported start but is not running; "
                    f"DMs may not arrive. See {self.log_path}"
                )
            else:
                self.log("pull-only daemon up (DMs delivered from t0)")
        else:
            self.warn_user(
                "could not start DM receive daemon (pull-only). "
                f"See {self.log_path}"
            )

    def upgrade(self, tid: str) -> bool:
        """Phase 2: restart the daemon with app-server dispatch on ``tid``."""
        extra = [
            "--app-server",
            "--app-server-url",
            self.url,
            "--codex-thread-id",
            tid,
        ]
        if self.armed:
            extra.append("--armed")
        if not self.run_bridge("restart", *extra):
            self.warn_user(
                "failed to upgrade DM bridge to wake mode; DMs still received "
                f"(pull-only). See {self.log_path}"
            )
            return False
        if not self.daemon_alive():
            self.warn_user(
                "bridge upgrade returned ok but daemon is not running; retrying. "
                f"See {self.log_path}"
            )
            return False
        self.log(f"upgraded to app-server dispatch (thread={tid})")
        return True

    def wrapper_gone(self) -> bool:
        if not self.wrapper_pid:
            return False
        try:
            os.kill(self.wrapper_pid, 0)
        except OSError:
            return True
        return False

    def run(self) -> int:
        self.start_pull_only()

        deadline = time.time() + self.deadline_seconds
        while time.time() < deadline:
            if self.wrapper_gone():
                self.log("wrapper process gone; watcher exiting")
                return 0
            tid = self.latest_thread()
            if tid and self.upgrade(tid):
                # Daemon now runs detached with wake dispatch; nothing left to do.
                return 0
            time.sleep(self.poll_interval)
        self.log("deadline reached; watcher exiting")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="bcodex DM-wake watcher.")
    parser.add_argument("--cwd", required=True, help="Project cwd.")
    parser.add_argument("--url", required=True, help="app-server ws:// URL.")
    parser.add_argument("--bridge", required=True, help="Path to beacon-codex-bridge.")
    parser.add_argument("--beacon-bin", default="", help="Resolved beacon binary.")
    parser.add_argument("--install-root", default="", help="Beacon checkout root.")
    parser.add_argument("--armed", action="store_true", help="Pass --armed on upgrade.")
    parser.add_argument("--wrapper-pid", default="", help="Exit when this pid is gone.")
    parser.add_argument("--deadline-seconds", default="3600", help="Max watch time.")
    parser.add_argument("--poll-interval", default="1", help="Thread poll interval (s).")
    parser.add_argument("--retries", default="3", help="Bridge call retry count.")
    args = parser.parse_args(argv)
    return Watcher(args).run()


if __name__ == "__main__":
    sys.exit(main())
