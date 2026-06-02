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
  inline code.
* Windows (`os.name == "nt"`): `msvcrt.locking` with `LK_LOCK`
  (blocking) on the first byte of the file. msvcrt doesn't expose
  a shared-vs-exclusive distinction at this granularity, so the
  shared lock is implemented as exclusive (slightly more
  serialisation than POSIX but never less safety — see comment in
  `lock_shared`). Unlocking releases the same byte.
* Unknown platforms: locks become no-ops with a one-time warning to
  stderr. Beacon will work but loses lost-update protection — the
  same downgrade we'd get if a future packager strips msvcrt.
"""

from __future__ import annotations

import os
import sys
from typing import IO


# We pre-bind the platform-specific implementations at import time so the
# hot path is a single function call, not a dispatch on every lock op.

if os.name == "posix":
    import fcntl as _fcntl

    def lock_shared(f: IO) -> None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_SH)

    def lock_exclusive(f: IO) -> None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)

    def unlock(f: IO) -> None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)

elif os.name == "nt":
    import msvcrt as _msvcrt

    # msvcrt.locking takes (fd, mode, nbytes) and locks/unlocks `nbytes`
    # bytes starting at the *current* file position. We lock a single byte
    # at offset 0 — every project.json the caller wants to lock has at
    # least 1 byte (init writes the empty `{}` skeleton before any
    # operations.py call goes through the locked path), so this is safe.
    #
    # `LK_LOCK` blocks until the lock is acquired and retries internally;
    # callers see the same semantics as the POSIX flock blocking call.

    _LOCK_BYTES = 1
    _LOCK_OFFSET = 0

    def _lock(f: IO) -> None:
        pos = f.tell()
        try:
            f.seek(_LOCK_OFFSET)
            _msvcrt.locking(f.fileno(), _msvcrt.LK_LOCK, _LOCK_BYTES)
        finally:
            f.seek(pos)

    def lock_shared(f: IO) -> None:
        # msvcrt doesn't expose a shared mode at byte granularity; using the
        # blocking lock means concurrent readers serialise, which is
        # marginally less concurrent than POSIX LOCK_SH but never less safe.
        _lock(f)

    def lock_exclusive(f: IO) -> None:
        _lock(f)

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
