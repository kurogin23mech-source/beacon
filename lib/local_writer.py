"""Single serialised, atomic, crash-safe write path for local project.json.

ms-148 e-5410 (stop-gap; the full fix is the SQLite store, e-5411+).

Before this module there were *two* independent local write paths, each with a
different safety hole:

* ``LocalStore.save_project`` — split its lock across load and save, so two
  sessions doing read-modify-write silently lost each other's changes.
* ``operations._apply_local`` — held its lock across the whole read→op→write
  window (no lost update), but wrote in place with truncate+dump, so a crash
  mid-write corrupted the file.

They also locked *different* objects once one of them switched to an atomic
``os.replace`` swap, which stops them excluding each other. ``os.replace`` swaps
the inode, and flock is bound to an inode: a writer that acquired its lock on
``project.json`` itself would strand that lock on the orphaned inode the instant
another writer replaced the file, and would then read/write through a stale fd.

``atomic_apply`` closes all of that by giving every local writer ONE protocol:

1. serialise on a *stable* lock file (``<project_file>.lock``) whose inode never
   changes, so the lock keeps serialising correctly across the atomic replace;
2. re-read ``project.json`` *by path* under that lock (never trust a fd acquired
   before the lock), so the mutation always sees the latest committed state;
3. optionally detect a lost update against a caller-supplied baseline hash
   (detection, not rescue: the losing writer is told to re-run — merge/retry is
   the SQLite store's job, e-5411);
4. write the result atomically (temp file + fsync + ``os.replace``) so a crash
   can never leave a truncated/corrupt file.

This is also the seed of the single-write-path goal (e-5414): once every caller
routes through here, the old bespoke read-modify-write code can be deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Callable, Optional, Tuple

from _file_lock import lock_exclusive, unlock


# Canonical on-disk serialisation. Every writer emits exactly these bytes so a
# hash taken at load time (LocalStore._file_hash reads the same bytes) matches a
# hash taken here — that equality is what the lost-update baseline check relies
# on. Keep this in sync with LocalStore._file_hash's read.
def state_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def hash_bytes(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _atomic_replace_bytes(project_file: str, out: bytes) -> None:
    """Write ``out`` to ``project_file`` via a temp file + os.replace so a crash
    between the write and the rename can never leave a truncated file — the
    original is untouched until the fully-written temp is renamed over it."""
    target_dir = os.path.dirname(project_file) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=target_dir,
        prefix=os.path.basename(project_file) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as tf:
            tf.write(out)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, project_file)
        tmp_path = None  # consumed by os.replace
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            # The swap did not happen (write/fsync/replace failed). Remove the
            # orphan temp so it can't accumulate in .beacon/. This is a
            # transient file this function just created — never user data — so
            # unlinking it is safe.
            os.unlink(tmp_path)


def atomic_apply(
    project_file: str,
    mutate: Callable[[dict], Tuple[dict, Any]],
    *,
    baseline: Optional[str] = None,
    validate: bool = False,
) -> Tuple[Any, str]:
    """Serialise a read-modify-write on a local project.json and write it atomically.

    Args:
      project_file: path to the local project.json.
      mutate: ``(current_data) -> (new_data, result)``. Runs under the lock with
        the freshly re-read state. Must be a pure function of ``current_data``
        (it may run against re-read state; do not close over a stale copy for
        the *content* — a whole-document overwrite caller may ignore the arg).
      baseline: md5 hex of the on-disk state captured at an earlier load. When
        given and the current on-disk state differs, raise ``ConflictError``
        instead of overwriting the concurrent change. ``None`` disables the
        check (a caller that reads fresh under the lock — e.g. apply_operation —
        has no load→save gap to guard).
      validate: run ``core.validate_project(new_data)`` before writing. On for
        normal apply_operation writes; off for the recovery/purge path, which
        must be able to persist an intentionally-invalid document.

    Returns:
      ``(result, new_state_hash)`` — the mutate result plus the md5 of the bytes
      just written, so the caller can refresh its own baseline without re-reading.
    """
    # Reuse the cloud lost-update signal so the CLI-level handler in
    # commands_shared.save_project treats local and cloud conflicts alike.
    from store_api import ConflictError

    lock_path = project_file + ".lock"
    lock_f = open(lock_path, "a+", encoding="utf-8")
    try:
        lock_exclusive(lock_f)

        # Read the current committed state by path, under the lock. A missing
        # file raises FileNotFoundError — the project file is always created by
        # `beacon init` before any write, so a missing file is a real error, not
        # a first-write case (preserves the previous "r+" open contract).
        with open(project_file, "rb") as rf:
            raw = rf.read()

        current_hash = hash_bytes(raw) if raw.strip() else None
        if (
            baseline is not None
            and current_hash is not None
            and current_hash != baseline
        ):
            raise ConflictError(
                "Local project.json changed since it was read — aborting to "
                "avoid overwriting another session's changes. Re-run the "
                "command. (Stop-gap detection; the full concurrency fix is the "
                "SQLite store, ms-148 e-5411.)"
            )

        data = json.loads(raw) if raw.strip() else {}
        new_data, result = mutate(data)

        if validate:
            import core  # lazy import avoids a circular dependency at load
            core.validate_project(new_data)

        out = state_bytes(new_data)
        _atomic_replace_bytes(project_file, out)
        return result, hash_bytes(out)
    finally:
        unlock(lock_f)
        lock_f.close()
