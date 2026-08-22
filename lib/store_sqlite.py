"""SQLite-backed local project store (ms-148 e-5411).

The local JSON file store serialises concurrent writes badly: two sessions that
read-modify-write ``.beacon/project.json`` at the same time lose each other's
changes, collide on ids, and can corrupt the file on a crash mid-write. The root
cause is that the read→modify→write window is not one atomic unit (SPEC 方針1).

This store makes the *database* the single writer: the project is stored in one
``(pk, sk, data)`` table in the v3 item-level layout (SPEC 方針2, matching the
server), and every mutation runs inside a single ``BEGIN IMMEDIATE`` transaction
in WAL mode. ``BEGIN IMMEDIATE`` takes the write lock up front, so a second
writer *waits* for the first to commit and then reads the fresh state — it never
overwrites a change it didn't see, and never has to retry. WAL keeps readers
non-blocking, and SQLite's atomic commit gives crash safety for free (a torn
write is rolled back, not left on disk).

Scope of e-5411: the store and its transactional ``apply(op)`` primitive plus a
whole-document ``load_project`` / ``save_project``. It is NOT yet wired into
``get_store()`` — that switchover, item-level row writes, and the JSON→SQLite
migration are the following tasks (e-5412 / e-5413 / e-5414). ``apply(op)`` is
the primitive those tasks build on: it is the SQLite analogue of
``operations._apply_local``, holding the lock across the whole read→op→write.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Callable, Optional, Tuple

import v3_schema

# pk namespaces in the single (pk, sk, data) table.
_PK_META = "meta"
_PK_MILESTONE = "milestone"
_PK_ENTRY = "entry"
_META_SK = ""  # the meta row is a singleton; its sort key is empty.

# Matches the file lock's 30s acquisition budget (lib/_file_lock): long enough to
# absorb heavy contention on a slow disk, short enough that a genuinely stuck
# writer surfaces as an error instead of hanging the CLI forever.
_BUSY_TIMEOUT_MS = int(os.environ.get("BEACON_SQLITE_BUSY_TIMEOUT_MS", "30000"))


class SqliteStore:
    """Store backed by a local SQLite database in the v3 item-level layout."""

    # Load-time baseline hash keyed by the db's absolute path, so a whole-doc
    # save_project() can detect a concurrent overwrite for callers that still use
    # the load→mutate→save split (the apply() primitive has no such gap). Class-
    # level for the same reason as LocalStore._save_baseline: load and save may
    # run on separate store instances within one CLI invocation.
    _save_baseline: dict[str, str] = {}

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    def is_cloud(self) -> bool:
        return False

    # -- connection / schema --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None → autocommit off only when we issue BEGIN
        # ourselves, giving explicit control over BEGIN IMMEDIATE. WAL + a busy
        # timeout make concurrent processes wait rather than fail with
        # "database is locked".
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0,
                               isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe durability/speed
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "  pk TEXT NOT NULL,"
                "  sk TEXT NOT NULL,"
                "  data TEXT NOT NULL,"
                "  PRIMARY KEY (pk, sk)"
                ")"
            )
        finally:
            conn.close()

    # -- row <-> project mapping ---------------------------------------------

    @staticmethod
    def _rows_to_project(rows: list[tuple[str, str, str]]) -> dict:
        """Assemble ``(pk, sk, data_json)`` rows into a unified project dict."""
        meta: dict = {}
        ms_rows: list[tuple[str, dict]] = []
        entry_rows: list[tuple[str, dict]] = []
        for pk, sk, data_json in rows:
            data = json.loads(data_json)
            if pk == _PK_META:
                meta = data
            elif pk == _PK_MILESTONE:
                ms_rows.append((sk, data))
            elif pk == _PK_ENTRY:
                entry_rows.append((sk, data))
        return v3_schema.assemble(meta, ms_rows, entry_rows)

    @staticmethod
    def _project_to_rows(data: dict) -> list[tuple[str, str, str]]:
        """Decompose a unified project dict into ``(pk, sk, data_json)`` rows."""
        meta, ms_map, entry_map = v3_schema.decompose(data)
        rows: list[tuple[str, str, str]] = [
            (_PK_META, _META_SK, json.dumps(meta, ensure_ascii=False)),
        ]
        for ms_id, ms_data in ms_map.items():
            rows.append((_PK_MILESTONE, ms_id,
                         json.dumps(ms_data, ensure_ascii=False)))
        for sk, entry_data in entry_map.items():
            rows.append((_PK_ENTRY, sk,
                         json.dumps(entry_data, ensure_ascii=False)))
        return rows

    def _baseline_key(self) -> str:
        return os.path.abspath(self._db_path)

    # -- reads ----------------------------------------------------------------

    def load_project(self) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT pk, sk, data FROM kv").fetchall()
        finally:
            conn.close()
        data = self._rows_to_project(rows)
        SqliteStore._save_baseline[self._baseline_key()] = _hash_project(data)
        return data

    # -- the serialised write primitive --------------------------------------

    def apply(self, op: Callable[[dict], Tuple[dict, Any]], *,
              validate: bool = True) -> Any:
        """Run ``op`` against the current project inside one write transaction.

        This is the lost-update fix: ``BEGIN IMMEDIATE`` grabs the write lock
        before the read, so the read→op→write window is serialised across every
        process on the machine. ``op(data) -> (new_data, result)`` sees fresh
        state (so id allocation inside it cannot collide), the result is written
        atomically, and a crash rolls the transaction back rather than corrupting
        the store. Returns ``op``'s ``result``.

        ``validate`` runs ``core.validate_project`` before the write (on for the
        normal path; a recovery caller may pass False to persist an
        intentionally-invalid document, mirroring save_project_unsafe).
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT pk, sk, data FROM kv").fetchall()
            data = self._rows_to_project(rows)

            new_data, result = op(data)

            if validate:
                import core  # lazy import avoids a circular dependency at load
                core.validate_project(new_data)

            self._write_all(conn, new_data)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        SqliteStore._save_baseline[self._baseline_key()] = _hash_project(new_data)
        return result

    def _write_all(self, conn: sqlite3.Connection, data: dict) -> None:
        """Replace every row with the decomposition of ``data``.

        e-5411 rewrites the whole row set for simplicity; e-5412 replaces this
        with an item-level diff so a one-field change touches one row. Both run
        inside the caller's transaction, so the swap is atomic either way.
        """
        conn.execute("DELETE FROM kv")
        conn.executemany(
            "INSERT INTO kv (pk, sk, data) VALUES (?, ?, ?)",
            self._project_to_rows(data),
        )

    # -- whole-document save (compatibility with the load→mutate→save split) --

    def save_project(self, data: dict, *, validate: bool = False) -> None:
        """Persist a whole project document, detecting a concurrent overwrite.

        Provided so a caller holding a document it mutated after load_project()
        can write it back. Because that split has a read→save gap (unlike
        apply()), this guards it the same way the file store does: if the store
        changed since our load, raise ConflictError instead of clobbering the
        other writer (detection, not rescue — the caller re-runs). Prefer
        apply(op) where possible; e-5414 moves the CLI onto it.
        """
        from store_api import ConflictError
        baseline = SqliteStore._save_baseline.get(self._baseline_key())

        def _overwrite(current: dict):
            if baseline is not None and _hash_project(current) != baseline:
                raise ConflictError(
                    "Local project store changed since it was read — aborting "
                    "to avoid overwriting another session's changes. Re-run the "
                    "command."
                )
            return data, None

        self.apply(_overwrite, validate=validate)


def _hash_project(data: dict) -> str:
    """Stable hash of a project dict for lost-update detection."""
    import hashlib
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()
