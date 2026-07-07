"""High-contention stress tests for lib/_file_lock.py.

Regression coverage for e-855 (Windows: local mode file lock raises
OSError(36) "Resource deadlock avoided" under high concurrency). The
existing test_operations.py::test_local_apply_no_lost_updates_under_concurrency
checks correctness (no lost updates) at 50×4 = 200 ops; this file
focuses specifically on the *deadlock avoidance* side: we crank
contention up further and verify no transient EDEADLK / EAGAIN bubbles
out of the lock layer.

Why a separate file: the operations.py test is about the
read-modify-write semantics. Here we want a pure lock-layer
stress test so a regression in _file_lock.py shows up with a
descriptive failure name, not buried inside an apply_operation
behaviour test.

Run locally:

    python3 -m pytest tests/test_local_file_lock_concurrency.py -q

The tests are designed to finish in <5s on a typical dev laptop;
they're safe to keep in the default CI run.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import operations  # noqa: E402
from _file_lock import lock_exclusive, lock_shared, unlock  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def local_project(monkeypatch):
    """Tmp project.json + BEACON_PROJECT_FILE wired up for local backend.

    Identical to the fixture in test_operations.py — duplicated here so
    this file is self-contained and can be run in isolation.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump({"name": "test", "milestones": [], "summary": ""}, tmp)
    tmp.close()
    monkeypatch.setenv("BEACON_PROJECT_FILE", tmp.name)
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    # ms-95 e-2438: monkeypatch.delitem auto-restores firestore_client.
    monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
    yield tmp.name
    Path(tmp.name).unlink(missing_ok=True)


@pytest.fixture
def lock_file():
    """A throwaway file usable for direct lock_exclusive/unlock calls."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".lock", delete=False, encoding="utf-8"
    )
    # Lock primitives need at least 1 byte to lock at offset 0 on Windows.
    tmp.write("x")
    tmp.close()
    yield tmp.name
    Path(tmp.name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Direct lock-layer stress (not going through operations.py)
# ---------------------------------------------------------------------------

def test_lock_exclusive_handles_high_contention(lock_file):
    """Hammer lock_exclusive from many threads; no OSError must escape.

    Pre-fix this test would fail on Windows with
    OSError(36, "Resource deadlock avoided") because msvcrt.locking with
    LK_LOCK gives up after ~10s of internal retry. The retry wrapper in
    _file_lock.py absorbs that and re-tries up to BEACON_LOCK_TIMEOUT_S.

    We deliberately keep the critical section tiny so the workload is
    dominated by lock acquire/release contention, which is what
    triggered e-855 in the first place.
    """
    THREADS = 30
    OPS_PER_THREAD = 20

    errors: list[BaseException] = []
    counter = {"value": 0}
    counter_lock = threading.Lock()  # purely for the in-memory assertion below

    def worker():
        try:
            for _ in range(OPS_PER_THREAD):
                with open(lock_file, "r+", encoding="utf-8") as f:
                    lock_exclusive(f)
                    try:
                        with counter_lock:
                            counter["value"] += 1
                    finally:
                        unlock(f)
        except BaseException as e:  # pragma: no cover - test failure path
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No OSError(EDEADLK) — the whole point of e-855
    edeadlk = [e for e in errors if isinstance(e, OSError) and e.errno == errno.EDEADLK]
    assert not edeadlk, (
        f"OSError(EDEADLK) leaked from lock layer despite retry wrapper: {edeadlk}"
    )
    # And no other errors either
    assert errors == [], f"Unexpected worker errors: {errors}"
    # Sanity: every worker completed its full quota — if a thread had
    # silently bailed early the lock layer would have under-served us.
    assert counter["value"] == THREADS * OPS_PER_THREAD


def test_lock_shared_handles_high_contention(lock_file):
    """lock_shared must not raise EDEADLK even when many readers pile up.

    On POSIX shared locks are non-exclusive among themselves so this is
    expected to be fast; on Windows shared collapses to exclusive and
    becomes the same contention pattern as lock_exclusive.
    """
    THREADS = 30
    OPS_PER_THREAD = 20

    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(OPS_PER_THREAD):
                with open(lock_file, "r", encoding="utf-8") as f:
                    lock_shared(f)
                    try:
                        # Touch the file so the read isn't optimised away by
                        # an aggressive JIT (defensive — CPython doesn't, but
                        # PyPy might).
                        _ = f.read(1)
                    finally:
                        unlock(f)
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    edeadlk = [e for e in errors if isinstance(e, OSError) and e.errno == errno.EDEADLK]
    assert not edeadlk, f"OSError(EDEADLK) from shared lock: {edeadlk}"
    assert errors == [], f"Unexpected worker errors: {errors}"


# ---------------------------------------------------------------------------
# End-to-end stress via apply_operation (closer to real-world workload)
# ---------------------------------------------------------------------------

def test_apply_operation_no_edeadlk_under_extreme_concurrency(local_project):
    """20 threads × 10 ops via apply_operation — no EDEADLK, no lost updates.

    This is intentionally heavier than the canonical
    test_local_apply_no_lost_updates_under_concurrency (50×4 = 200 too)
    in terms of ops/thread, which keeps each thread *holding* the lock
    less often but acquiring it more aggressively — the exact pattern
    that hit the msvcrt 10-retry ceiling on Windows.
    """
    THREADS = 20
    OPS_PER_THREAD = 10

    def init(data):
        data["counter"] = 0
        return data, None
    operations.apply_operation("p-test", init, actor="tester")

    def increment(data):
        data["counter"] = data.get("counter", 0) + 1
        return data, data["counter"]

    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(OPS_PER_THREAD):
                operations.apply_operation(
                    "p-test", increment, op_name="counter.inc", actor="tester"
                )
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Surface EDEADLK separately so a regression has an obvious failure
    # message rather than a generic "Worker errors: [...]"
    edeadlk = [e for e in errors if isinstance(e, OSError) and e.errno == errno.EDEADLK]
    assert not edeadlk, (
        f"e-855 regression: OSError(EDEADLK) escaped operations._apply_local "
        f"despite the _file_lock retry wrapper. Errors: {edeadlk}"
    )
    assert errors == [], f"Worker errors: {errors}"

    with open(local_project) as f:
        final = json.load(f)
    assert final["counter"] == THREADS * OPS_PER_THREAD, (
        f"Lost updates: expected {THREADS * OPS_PER_THREAD}, "
        f"got {final['counter']}"
    )


# ---------------------------------------------------------------------------
# Retry policy: pathological "lock never releases" still surfaces an error
# ---------------------------------------------------------------------------

def test_retry_lock_absorbs_transient_edeadlk():
    """A function that raises EDEADLK twice then succeeds must be retried.

    Direct unit test of the _retry_lock helper. We don't go through the
    OS lock primitives because we want to assert the retry behaviour
    deterministically — POSIX flock's actual blocking semantics vary
    across kernels (Linux/macOS/BSD) in ways that make integration-style
    tests of "retry on EDEADLK" flaky.
    """
    import _file_lock as fl

    calls = {"n": 0}

    def acquire():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        # third attempt succeeds

    fl._retry_lock(acquire)
    assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"


def test_retry_lock_absorbs_transient_eagain_and_eintr():
    """EAGAIN (Windows lock contention) and EINTR (signal interruption)
    should also be retried, not surfaced as hard failures.
    """
    import _file_lock as fl

    for code in (errno.EAGAIN, errno.EINTR, errno.EACCES):
        calls = {"n": 0}

        def acquire(_code=code):
            calls["n"] += 1
            if calls["n"] < 2:
                raise OSError(_code, f"transient errno {_code}")

        fl._retry_lock(acquire)
        assert calls["n"] == 2, f"errno {code}: expected 2 attempts, got {calls['n']}"


def test_retry_lock_propagates_non_transient_errno():
    """Errors that aren't lock-contention (EBADF, ENOSPC, ...) must
    propagate immediately — retrying them would just delay a real bug.
    """
    import _file_lock as fl

    calls = {"n": 0}

    def acquire():
        calls["n"] += 1
        raise OSError(errno.EBADF, "Bad file descriptor")

    with pytest.raises(OSError) as exc_info:
        fl._retry_lock(acquire)
    assert exc_info.value.errno == errno.EBADF
    assert calls["n"] == 1, "non-transient errno must not trigger retry"


def test_retry_lock_gives_up_after_budget(monkeypatch):
    """If a transient errno keeps coming back, retry budget must bound
    the wait time and surface the last error rather than hang forever.

    This guards against the opposite failure mode of the e-855 fix:
    making the retry loop so generous that real deadlocks become
    invisible. We override _LOCK_TOTAL_TIMEOUT_S to 0.3s so the test
    completes quickly.
    """
    import time

    import _file_lock as fl

    monkeypatch.setattr(fl, "_LOCK_TOTAL_TIMEOUT_S", 0.3)

    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    start = time.monotonic()
    with pytest.raises(OSError) as exc_info:
        fl._retry_lock(always_fails)
    elapsed = time.monotonic() - start

    assert exc_info.value.errno == errno.EDEADLK, (
        "Last transient errno should be re-raised verbatim"
    )
    # The budget is 0.3s; allow some slack for backoff overshoot but
    # require completion well under 1s — otherwise the loop isn't
    # respecting the budget.
    assert 0.2 < elapsed < 1.5, (
        f"Retry loop didn't respect 0.3s budget (elapsed={elapsed:.2f}s)"
    )
    # At least a handful of retries should have happened before giving
    # up — otherwise the backoff is so aggressive it isn't really
    # retrying.
    assert calls["n"] >= 3, f"too few retries before giving up: {calls['n']}"
