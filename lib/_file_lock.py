"""Cross-platform file locking primitives.

Beacon's local backend (store_local.py + operations.py) relies on
fcntl-style advisory file locks to serialise read-modify-write
cycles on `.beacon/project.json` and prevent lost updates
(CORE doc `data-immutability-principle`).

`fcntl` is Unix-only, which makes the bundled-lib path crash at
import time on Windows. This module hides the platform difference
behind three blocking primitives — `lock_shared` / `lock_exclusive`
/ `unlock` — that behave identically to the previous fcntl calls
from the caller's point of view.

Implementation notes
--------------------

* POSIX (`os.name == "posix"`): real `fcntl.flock` with `LOCK_SH`
  / `LOCK_EX` / `LOCK_UN`. Bit-for-bit equivalent to the previous
  inline code, plus a retry loop that absorbs spurious
  `OSError(EDEADLK)` raised by kernels that perform conservative
  deadlock detection under high lock-acquire contention (seen on
  macOS and Linux with 30+ concurrent threads/processes).
* Windows (`os.name == "nt"`): `msvcrt.locking` with `LK_LOCK`
  (blocking) on the first byte of the file. msvcrt doesn't expose
  a shared-vs-exclusive distinction at this granularity, so the
  shared lock is implemented as exclusive (slightly more
  serialisation than POSIX but never less safety — see comment in
  `lock_shared`). `LK_LOCK` retries internally only 10 times at
  ~1-second intervals before raising `OSError(EDEADLK)`; that ~10s
  ceiling is too short under high contention (e-855: 50 threads
  × 4 increments reliably trip it). We wrap the call in our own
  retry loop with jittered backoff so callers see a true blocking
  semantics matching POSIX.
* Unknown platforms: locks become no-ops with a one-time warning to
  stderr. Beacon will work but loses lost-update protection — the
  same downgrade we'd get if a future packager strips msvcrt.

Retry policy
------------

Both POSIX and Windows implementations retry on transient
acquisition errors (EDEADLK / EAGAIN / EACCES). The total wait
budget is bounded by `_LOCK_TOTAL_TIMEOUT_S` (default 30s) — long
enough to absorb 50-way contention on a slow disk, short enough
that a true deadlock (e.g. a panicked process holding the lock
forever) still surfaces as a clear error rather than hanging the
CLI indefinitely.
"""

from __future__ import annotations

import errno
import os
import random
import sys
import time
from typing import IO


# ---------------------------------------------------------------------------
# Retry policy (shared across platforms)
# ---------------------------------------------------------------------------

# Total time budget for a single lock acquisition. After this elapses we
# give up and re-raise the last OSError to the caller. 30s comfortably
# covers a 50-thread × 4-op stress test on a typical dev laptop; longer
# would mask genuine bugs.
_LOCK_TOTAL_TIMEOUT_S = float(
    os.environ.get("BEACON_LOCK_TIMEOUT_S", "30")
)

# Backoff bounds. Start very short to keep latency low when the lock is
# usually free; cap so a flapping process can't busy-loop.
_LOCK_BACKOFF_MIN_S = 0.001  # 1ms
_LOCK_BACKOFF_MAX_S = 0.05   # 50ms

# Errno values that mean "lock is currently held by someone else, try
# again later" rather than "the lock will never succeed". We treat
# EDEADLK as transient under contention because POSIX kernels raise it
# defensively when the lock acquisition graph *might* deadlock, not only
# when it definitely will. Under heavy concurrent acquire/release this
# false-positives often enough to be worth absorbing.
_TRANSIENT_LOCK_ERRNOS = frozenset({
    errno.EDEADLK,   # 11/35 — "Resource deadlock avoided" (the e-855 symptom)
    errno.EAGAIN,    # 35/11 — "Resource temporarily unavailable"
    errno.EACCES,    # 13    — Windows often reports this for lock contention
    errno.EINTR,     # 4     — system call interrupted by a signal; safe to retry
})


def _sleep_backoff(attempt: int) -> None:
    """Jittered exponential backoff bounded by [_MIN, _MAX]."""
    # 2^attempt * MIN with full jitter, capped at MAX. Full jitter
    # (random.uniform(0, cap)) is the AWS-recommended pattern: it spreads
    # retry storms more effectively than fixed exponential because every
    # waiter rolls an independent delay.
    cap = min(_LOCK_BACKOFF_MAX_S, _LOCK_BACKOFF_MIN_S * (2 ** attempt))
    time.sleep(random.uniform(_LOCK_BACKOFF_MIN_S, cap))


def _retry_lock(acquire) -> None:
    """Call `acquire()` with backoff until it succeeds or budget exhausts.

    `acquire` must be a zero-arg callable that either returns (lock
    acquired) or raises OSError. Transient errnos (see
    `_TRANSIENT_LOCK_ERRNOS`) trigger a retry; anything else propagates
    unchanged.

    Raises:
      OSError — the last transient OSError seen, if the total time
        budget elapses without acquiring the lock. A non-transient
        OSError is re-raised immediately on first occurrence.
    """
    deadline = time.monotonic() + _LOCK_TOTAL_TIMEOUT_S
    attempt = 0
    last_err: OSError | None = None
    while True:
        try:
            acquire()
            return
        except OSError as e:
            if e.errno not in _TRANSIENT_LOCK_ERRNOS:
                # Non-transient: re-raise so callers see the real error
                # (e.g. EBADF from a closed fd, ENOSPC, etc).
                raise
            last_err = e
            if time.monotonic() >= deadline:
                # Budget exhausted. Re-raise the last transient error so
                # the caller still sees a meaningful errno and message
                # instead of a generic timeout exception.
                raise
            _sleep_backoff(attempt)
            attempt += 1


# We pre-bind the platform-specific implementations at import time so the
# hot path is a single function call, not a dispatch on every lock op.

if os.name == "posix":
    import fcntl as _fcntl

    def lock_shared(f: IO) -> None:
        _retry_lock(lambda: _fcntl.flock(f.fileno(), _fcntl.LOCK_SH))

    def lock_exclusive(f: IO) -> None:
        _retry_lock(lambda: _fcntl.flock(f.fileno(), _fcntl.LOCK_EX))

    def unlock(f: IO) -> None:
        # Unlock should never fail under contention; release it directly
        # without the retry wrapper. If the kernel still returns EINTR
        # (signal during syscall) we retry once — losing the unlock would
        # strand the file locked for the lifetime of the fd.
        try:
            _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except OSError as e:
            if e.errno == errno.EINTR:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
            else:
                raise

elif os.name == "nt":
    import msvcrt as _msvcrt

    # msvcrt.locking takes (fd, mode, nbytes) and locks/unlocks `nbytes`
    # bytes starting at the *current* file position. We lock a single byte
    # at offset 0 — every project.json the caller wants to lock has at
    # least 1 byte (init writes the empty `{}` skeleton before any
    # operations.py call goes through the locked path), so this is safe.
    #
    # `LK_LOCK` blocks until the lock is acquired and retries internally
    # — but only 10 times at ~1-second intervals before giving up with
    # OSError(EDEADLK, "Resource deadlock avoided"). Under high contention
    # (50 threads × 4 ops in test_local_apply_no_lost_updates_under_concurrency)
    # this 10-second internal budget reliably runs out. We wrap with our
    # own retry loop and a `LK_NBLCK` (non-blocking) probe so the outer
    # backoff can yield the GIL between attempts instead of pinning a
    # single thread inside msvcrt's blocking call.

    _LOCK_BYTES = 1
    _LOCK_OFFSET = 0

    def _try_acquire(f: IO) -> None:
        """Single non-blocking lock attempt; raises OSError on contention."""
        pos = f.tell()
        try:
            f.seek(_LOCK_OFFSET)
            # LK_NBLCK = non-blocking; raises immediately with EACCES /
            # EDEADLK if another holder has the byte. Combined with the
            # outer _retry_lock backoff this gives the same observable
            # behavior as POSIX flock(LOCK_EX) under contention.
            _msvcrt.locking(f.fileno(), _msvcrt.LK_NBLCK, _LOCK_BYTES)
        finally:
            f.seek(pos)

    def lock_shared(f: IO) -> None:
        # msvcrt doesn't expose a shared mode at byte granularity; using the
        # blocking lock means concurrent readers serialise, which is
        # marginally less concurrent than POSIX LOCK_SH but never less safe.
        _retry_lock(lambda: _try_acquire(f))

    def lock_exclusive(f: IO) -> None:
        _retry_lock(lambda: _try_acquire(f))

    def unlock(f: IO) -> None:
        pos = f.tell()
        try:
            f.seek(_LOCK_OFFSET)
            _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, _LOCK_BYTES)
        finally:
            f.seek(pos)

else:
    # Exotic platforms (Jython, BeOS, …). Issue a one-shot warning and
    # degrade to unsafe no-op locks so the process still functions.
    _warned = False

    def _warn_once() -> None:
        global _warned
        if not _warned:
            sys.stderr.write(
                f"[beacon] WARNING: no file-lock implementation for os.name="
                f"{os.name!r}. Lost-update protection is disabled.\n"
            )
            _warned = True

    def lock_shared(f: IO) -> None:  # type: ignore[misc]
        _warn_once()

    def lock_exclusive(f: IO) -> None:  # type: ignore[misc]
        _warn_once()

    def unlock(f: IO) -> None:  # type: ignore[misc]
        _warn_once()


__all__ = ["lock_shared", "lock_exclusive", "unlock"]
