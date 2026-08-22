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
import sys
from typing import Any, Callable, Optional, Tuple

import v3_schema

# pk namespaces in the single (pk, sk, data) table.
_PK_META = "meta"
_PK_MILESTONE = "milestone"
_PK_ENTRY = "entry"
_META_SK = ""  # the meta row is a singleton; its sort key is empty.


def db_has_data(db_path: str) -> bool:
    """True if ``db_path`` is a SQLite store that already holds project rows.

    Used by get_store to decide whether a migration is still needed: a missing
    db, a db with no ``kv`` table, or an empty ``kv`` all count as "no data yet"
    so a freshly-created empty db can never shadow an existing project.json (the
    init-ordering race)."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT 1 FROM kv LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def sqlite_db_path_for(project_file: str) -> str:
    """Derive the SQLite db path that sits beside a project.json.

    ``.beacon/project.json`` → ``.beacon/project.db``. A path that already ends
    in ``.db`` is returned unchanged (lets tests pass a db path directly); any
    other path gets ``.db`` appended.
    """
    if project_file.endswith(".json"):
        return project_file[:-len(".json")] + ".db"
    if project_file.endswith(".db"):
        return project_file
    return project_file + ".db"

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

    def __init__(self, project_file: str, *, db_path: str | None = None):
        # ``project_file`` is the .beacon/project.json path. The SQLite db lives
        # beside it. The JSON file is kept as a best-effort read-only *mirror*
        # (written after each commit) so the Tauri desktop app — which still
        # reads project.json directly — keeps working until it is rewired to read
        # SQLite (the e-5417 follow-up MS). The mirror is a projection, not a
        # second concurrency-controlled write path.
        self._project_file = project_file
        self._db_path = db_path or sqlite_db_path_for(project_file)
        self._last_hash: str | None = None
        # Delegate the file-based artifacts (documents / treks / orgs / session
        # logs / watching) to LocalStore unchanged — those live in their own
        # files/dirs, not in project.json, so their storage is unaffected.
        from store_local import LocalStore
        self._files = LocalStore(project_file)
        self._ensure_schema()

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def project_file(self) -> str:
        return self._project_file

    def is_cloud(self) -> bool:
        return False

    # -- file-based artifacts: delegate to LocalStore (storage unchanged) ------

    def list_documents(self):
        return self._files.list_documents()

    def get_document(self, doc_id: str) -> dict:
        return self._files.get_document(doc_id)

    def list_treks(self, **kwargs):
        return self._files.list_treks(**kwargs)

    def get_trek(self, trek_id: str) -> dict:
        return self._files.get_trek(trek_id)

    def create_org(self, **kwargs) -> dict:
        return self._files.create_org(**kwargs)

    def list_orgs(self, **kwargs) -> list:
        return self._files.list_orgs(**kwargs)

    def get_org(self, org_id: str) -> dict:
        return self._files.get_org(org_id)

    def add_org_member(self, org_id: str, **kwargs) -> dict:
        return self._files.add_org_member(org_id, **kwargs)

    def remove_org_member(self, org_id: str, **kwargs) -> dict:
        return self._files.remove_org_member(org_id, **kwargs)

    def delete_org(self, org_id: str) -> dict:
        return self._files.delete_org(org_id)

    def upsert_session_log(self, session_id: str, body: dict) -> bool:
        return self._files.upsert_session_log(session_id, body)

    def list_session_ids(self) -> list:
        return self._files.list_session_ids()

    def get_session_log(self, session_id: str):
        return self._files.get_session_log(session_id)

    def list_session_logs(self, limit: int = 0) -> list:
        return self._files.list_session_logs(limit=limit)

    def start_watching(self) -> None:
        self._files.start_watching()

    def stop_watching(self) -> None:
        self._files.stop_watching()

    # -- connection / schema --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None puts Python's sqlite3 in full autocommit: it
        # injects no implicit BEGIN before writes, so WE own the transaction
        # boundaries by issuing an explicit BEGIN IMMEDIATE in apply(). WAL + a
        # busy timeout make concurrent processes wait rather than fail with
        # "database is locked".
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0,
                               isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe durability/speed
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _ensure_schema(self) -> None:
        # CREATE TABLE is DDL; SQLite auto-commits it immediately even in the
        # autocommit connection above, so no explicit BEGIN/COMMIT is needed
        # here (unlike the DML writes in apply(), which we wrap in BEGIN
        # IMMEDIATE). busy_timeout is set per connection in _connect().
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

    def populate_if_empty(self, data: dict) -> bool:
        """Populate the store from ``data`` only if it has no rows yet, atomically.

        This is the concurrency-safe core of migrate-on-first-use (e-5415): when
        several processes start on a fresh (json, no db) project at once, they
        all try to migrate. Without serialisation a second migration overwrites
        the first migration's early appends, losing an update. Here the emptiness
        check and the populate run inside ONE ``BEGIN IMMEDIATE`` transaction, so
        exactly one process populates and the rest see rows and skip. Returns
        True iff this call did the populate.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM kv LIMIT 1").fetchone() is not None:
                conn.execute("COMMIT")
                return False
            self._write_diff(conn, [], data)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        SqliteStore._save_baseline[self._baseline_key()] = _hash_project(data)
        return True

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

            self._write_diff(conn, rows, new_data)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        SqliteStore._save_baseline[self._baseline_key()] = _hash_project(new_data)
        self._write_mirror(new_data)
        return result

    def _write_mirror(self, data: dict) -> None:
        """Best-effort read-only projection of the committed state into
        project.json, so the Tauri desktop app (which reads project.json
        directly) keeps working until it is rewired to read SQLite (e-5417).

        This is NOT a second source of truth or a concurrency path: SQLite is the
        sole writer, and this only overwrites the mirror with the latest
        committed state via an atomic replace. Any failure is swallowed — a flaky
        mirror must never break a command whose real write already succeeded.
        Skipped when this store isn't backed by a real project.json (e.g. tests
        that pass a bare .db path)."""
        if not self._project_file.endswith(".json"):
            return
        try:
            import tempfile
            out = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
            target_dir = os.path.dirname(self._project_file) or "."
            fd, tmp = tempfile.mkstemp(dir=target_dir,
                                       prefix=os.path.basename(self._project_file) + ".",
                                       suffix=".mirror")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(out)
                os.replace(tmp, self._project_file)
                tmp = None
            finally:
                if tmp is not None and os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            # Best-effort: a flaky mirror must not break a command whose SQLite
            # write already succeeded. But do NOT vanish silently — a persistent
            # failure (permissions, full disk) means the Tauri desktop app reads
            # stale data, so leave a trace on stderr rather than a hidden divergence.
            print(f"[beacon] warning: could not refresh the project.json mirror "
                  f"({e}); the SQLite store is current but the desktop app may "
                  f"show stale data until the next successful write.",
                  file=sys.stderr)

    def _write_diff(self, conn: sqlite3.Connection,
                    old_rows: list[tuple[str, str, str]], data: dict) -> None:
        """Write only the rows that changed between ``old_rows`` and ``data``.

        e-5412: a mutation that touches one milestone or one entry rewrites one
        row, not the whole document (受入条件5『該当行だけ書き換え』, 受入条件7 の
        性能根拠). The diff runs inside the caller's transaction, so the update is
        still atomic. Correctness is identical to a full rewrite because the row
        set is keyed by ``(pk, sk)``; only unchanged rows are skipped.
        """
        upserts, deletes = diff_rows(old_rows, self._project_to_rows(data))
        if deletes:
            conn.executemany("DELETE FROM kv WHERE pk = ? AND sk = ?", deletes)
        if upserts:
            conn.executemany(
                "INSERT OR REPLACE INTO kv (pk, sk, data) VALUES (?, ?, ?)",
                upserts,
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

    # -- project.json-content reads/mutations (SQLite-backed) -----------------
    # These mirror the LocalStore implementations but route through this store's
    # load_project / apply, so the CLI gets identical behaviour on SQLite.

    def has_changed(self) -> bool:
        """True if the store changed since the last call (first call → True).

        Used by the dashboard poll loop. Compares a content hash of the current
        project so an external write (another process) is noticed."""
        current = _hash_project(self.load_project())
        if self._last_hash is None or current != self._last_hash:
            self._last_hash = current
            return True
        return False

    def get_milestone(self, ms_id: str) -> dict:
        import core
        data = self.load_project()
        matches = core.find_milestones(data, ms_id)
        if not matches:
            raise ValueError(f"Milestone '{ms_id}' not found")
        ms = matches[0]
        entries = ms.get("entries", []) or []
        total, done = core.count_task_status(entries)
        return {**ms, "total_tasks": total, "done_tasks": done,
                "entries": core.entries_to_json(entries)}

    def _purge(self, purge_fn) -> dict:
        """Shared body for the purge family: run ``purge_fn(data)`` inside one
        SQLite transaction (validate=False — purge must work on already-invalid
        data, its whole purpose) and report the removed record + residual dups."""
        import core
        captured: dict = {}

        def op(data):
            captured["purged"] = purge_fn(data)
            captured["dup_report"] = core.find_duplicate_ids(data)
            return data, None

        self.apply(op, validate=False)
        return {
            "purged": captured["purged"],
            "still_dirty": any(captured["dup_report"].values()),
            "dup_report": captured["dup_report"],
        }

    def purge_entry(self, entry_id: str, *, reason: str,
                    index: int | None = None) -> dict:
        import core
        return self._purge(
            lambda data: core.entry_purge(data, entry_id, reason=reason, index=index))

    def purge_operation(self, op_id: str, *, reason: str,
                        index: int | None = None) -> dict:
        import core
        return self._purge(
            lambda data: core.operation_purge(data, op_id, reason=reason, index=index))

    def purge_milestone(self, ms_id: str, *, reason: str,
                        index: int | None = None) -> dict:
        import core
        return self._purge(
            lambda data: core.milestone_purge(data, ms_id, reason=reason, index=index))

    def rehome_project(self, project_id: str, *, target_org_id: str) -> dict:
        import org as org_mod
        import org_store
        if org_store.load_org(target_org_id) is None:
            raise ValueError(f"org '{target_org_id}' not found")
        captured: dict = {}

        def op(data):
            current_id = data.get("project_id") or data.get("id")
            if project_id and current_id and project_id != current_id:
                raise ValueError(
                    f"project '{project_id}' not found in this workspace "
                    f"(local mode manages only '{current_id}')")
            captured["previous"] = org_mod.rehome_project(data, target_org_id)
            captured["current"] = current_id
            return data, None

        self.apply(op, validate=True)
        return {
            "project_id": captured["current"],
            "org_id": target_org_id,
            "previous_org_id": captured["previous"],
        }


def diff_rows(
    old_rows: list[tuple[str, str, str]],
    new_rows: list[tuple[str, str, str]],
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Compute the row-level delta between two ``(pk, sk, data)`` row sets.

    Returns ``(upserts, deletes)``:
      * ``upserts`` — ``(pk, sk, data)`` for rows that are new or whose data
        changed. Unchanged rows are omitted (that is the whole point: a one-field
        edit yields one upsert).
      * ``deletes`` — ``(pk, sk)`` for rows present before but gone now.

    Pure and side-effect free so it can be unit-tested without a database.
    """
    old = {(pk, sk): data for pk, sk, data in old_rows}
    new = {(pk, sk): data for pk, sk, data in new_rows}
    upserts = [
        (pk, sk, data)
        for (pk, sk), data in new.items()
        if old.get((pk, sk)) != data
    ]
    deletes = [key for key in old if key not in new]
    return upserts, deletes


def _hash_project(data: dict) -> str:
    """Stable hash of a project *dict* for this store's lost-update detection.

    NOTE: this is a store-internal baseline only — it is compared against other
    hashes produced by THIS same function, never across stores. It intentionally
    hashes the sorted-key dict, not the on-disk bytes, so it is NOT comparable
    with the file store's byte-level hash (local_writer.hash_bytes /
    LocalStore._file_hash). Do not copy one store's baseline into the other's
    slot (a footgun for the e-5414 get_store switchover).
    """
    import hashlib
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()
