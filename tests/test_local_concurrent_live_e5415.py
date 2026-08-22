"""Real multi-process concurrency over the LIVE local write path (ms-148 e-5415/e-5416).

Earlier concurrency tests exercise SqliteStore.apply directly. These drive the
*production* path — separate OS processes calling operations.apply_operation,
which routes through get_store() → SqliteStore for a local project — so they pin
the SPEC's 受入条件 on the same code a real CLI runs:

  1/2 (e-5415): N processes × M appends lose nothing and never collide on ids.
  3   (e-5415): killing a writer mid-run leaves a valid, readable store.
  5   (e-5416): a fire-and-forget auto-record (commit hook style, output/exit
       discarded) racing another writer is not silently dropped — SQLite makes
       the contender WAIT and commit, rather than error and vanish.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"


def _seed(tmp_path: Path) -> Path:
    beacon = tmp_path / ".beacon"
    beacon.mkdir(parents=True)
    pf = beacon / "project.json"
    pf.write_text(json.dumps({
        "name": "conc", "summary": "", "profession": "dev",
        "milestones": [{"id": "ms-1", "title": "m", "status": "in_progress",
                        "entries": []}],
    }), encoding="utf-8")
    return pf


# A worker that appends M entries to ms-1 through the live apply_operation path,
# allocating each id as max(existing)+1 INSIDE the transaction (so a collision is
# only avoided if the read→write is truly serialised).
_WORKER = r"""
import sys, os
sys.path.insert(0, {lib!r})
import operations
pf = {pf!r}
tag = sys.argv[1]
M = int(sys.argv[2])
def add(data):
    ms = data["milestones"][0]
    ids = [int(e["id"].split("-")[1]) for e in ms["entries"]]
    nxt = (max(ids) + 1) if ids else 1
    ms["entries"].append({{"id": "e-%d" % nxt, "type": "task",
                          "description": tag, "status": "todo"}})
    return data, None
for _ in range(M):
    operations.apply_operation("p", add, project_file=pf)
"""


def _spawn(pf: Path, tag: str, m: int) -> subprocess.Popen:
    script = _WORKER.format(lib=str(LIB), pf=str(pf))
    env = {**os.environ, "BEACON_OPERATIONS_BACKEND": "local",
           "BEACON_PROJECT_FILE": str(pf)}
    env.pop("BEACON_LOCAL_BACKEND", None)  # ensure the SQLite (default) path
    return subprocess.Popen([sys.executable, "-c", script, tag, str(m)], env=env)


def _load(pf: Path) -> dict:
    # Read the truth through the store, not the mirror.
    sys.path.insert(0, str(LIB))
    from store import get_store
    return get_store(project_file=str(pf)).load_project()


def test_concurrent_appends_no_loss_no_id_collision(tmp_path):
    pf = _seed(tmp_path)
    N, M = 6, 20
    procs = [_spawn(pf, f"w{i}", M) for i in range(N)]
    for p in procs:
        assert p.wait(timeout=120) == 0

    entries = _load(pf)["milestones"][0]["entries"]
    ids = [e["id"] for e in entries]
    assert len(ids) == N * M, f"lost updates: {len(ids)} != {N*M}"
    assert len(set(ids)) == N * M, "id collision under concurrency"


def test_kill_mid_write_leaves_valid_store(tmp_path):
    pf = _seed(tmp_path)
    # A long-running writer we will SIGKILL partway through.
    victim = _spawn(pf, "victim", 400)
    time.sleep(0.6)  # let it commit some, then kill hard (no cleanup)
    victim.send_signal(signal.SIGKILL)
    victim.wait(timeout=30)

    # The store must still be readable and internally valid — a killed writer
    # rolls back its in-flight transaction, it never leaves a torn file.
    data = _load(pf)
    entries = data["milestones"][0]["entries"]
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids)), "corruption: duplicate ids after kill"
    import core
    core.validate_project(data)  # raises if the surviving state is invalid

    # And the store still accepts new writes after the crash.
    assert _spawn(pf, "after", 3).wait(timeout=60) == 0
    assert len(_load(pf)["milestones"][0]["entries"]) == len(ids) + 3


def test_fire_and_forget_auto_record_is_not_dropped(tmp_path):
    """e-5416 / 方針5: the commit hook calls `beacon log` discarding stdout,
    stderr and exit code. Under the old JSON path a concurrent write could be
    silently lost; with SQLite the contender WAITS for the lock and commits, so
    a fire-and-forget record still lands. Model it: a steady writer runs while an
    'auto-record' worker whose output is fully discarded appends concurrently —
    every one of its records must be present afterwards."""
    pf = _seed(tmp_path)
    steady = _spawn(pf, "steady", 40)
    # Auto-record worker: output/exit intentionally discarded, like the hook.
    auto = subprocess.Popen(
        [sys.executable, "-c", _WORKER.format(lib=str(LIB), pf=str(pf)),
         "autorec", "40"],
        env={**os.environ, "BEACON_OPERATIONS_BACKEND": "local",
             "BEACON_PROJECT_FILE": str(pf)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    steady.wait(timeout=120)
    auto.wait(timeout=120)
    # A crashed writer would silently drop records (output is discarded); assert
    # exit codes so an environmental crash surfaces as a clear failure, not a
    # mysterious count mismatch.
    assert steady.returncode == 0, "steady writer crashed"
    assert auto.returncode == 0, "auto-record writer crashed"

    entries = _load(pf)["milestones"][0]["entries"]
    autorec = [e for e in entries if e["description"] == "autorec"]
    assert len(autorec) == 40, (
        f"auto-record silently dropped: only {len(autorec)}/40 landed")
    # And nothing was lost overall.
    assert len(entries) == 80
    assert len({e["id"] for e in entries}) == 80
