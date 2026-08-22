"""Beacon LocalStore - File-based project storage.

Wraps the existing JSON file I/O pattern, implementing the Store protocol.
"""

from __future__ import annotations

import hashlib
import json
import os

from _file_lock import lock_shared, unlock


def _read_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-like frontmatter parser for document files.

    Returns ``(metadata_dict, body_text)``. Handles only the single-line
    ``key: value`` form used by ``.beacon/documents/*.md`` (scope /
    milestone / operation / trek_id / status / trashed_*); inline / block
    list values are ignored (= not used by document frontmatter). Kept
    inline here to avoid importing ``commands._parse_frontmatter`` and
    creating a circular dependency. The richer parser in commands.py
    handles complex envelope frontmatter; documents are simple key:value.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in header.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        val = val.strip()
        if val:
            meta[key.strip()] = val
    return meta, body


class LocalStore:
    """Store implementation backed by a local JSON file."""

    # Load-time hash keyed by the project file's *absolute* path. save_project()
    # compares against this to detect that another writer changed the file
    # between our load and our save (lost-update detection). It is class-level —
    # not instance-level — because get_store() hands load_project() and
    # save_project() *separate* LocalStore instances within a single CLI
    # invocation, so an instance attribute would not survive the round trip.
    # This mirrors StoreApi._load_baseline (cloud lost-update guard, e-841).
    _save_baseline: dict[str, str] = {}

    def __init__(self, project_file: str):
        self._project_file = project_file
        self._last_hash: str | None = None

    @property
    def project_file(self) -> str:
        return self._project_file

    def _baseline_key(self) -> str:
        """Absolute-path key so two LocalStore instances that reference the
        same file (one relative, one absolute) share a baseline entry."""
        return os.path.abspath(self._project_file)

    def load_project(self) -> dict:
        with open(self._project_file, "r", encoding="utf-8") as f:
            lock_shared(f)
            data = json.load(f)
            unlock(f)
        h = self._file_hash()
        self._last_hash = h
        # Record the load-time baseline so a later save() — possibly on a
        # different LocalStore instance — can detect a concurrent overwrite.
        if h is not None:
            LocalStore._save_baseline[self._baseline_key()] = h
        return data

    def save_project(self, data: dict, *, validate: bool = False) -> None:
        """Persist the whole project document through the shared atomic writer.

        ``validate`` mirrors ``SqliteStore.save_project`` so the two local stores
        share one signature (the e-5414 switchover reads them interchangeably).
        It defaults to False because the normal path already validated in
        commands_shared.save_project and the recovery/purge path must be able to
        persist an intentionally-invalid document.

        This is ms-148 e-5410 (stop-gap; the full fix is the SQLite store,
        e-5411). Routes through ``local_writer.atomic_apply`` — the single
        serialised, atomic, crash-safe local write path shared with
        ``operations._apply_local`` — so it

        (a) can't corrupt the file on a crash mid-write (atomic os.replace), and
        (b) *detects* a concurrent overwrite (the hash captured at
            load_project() no longer matches on disk) and raises ConflictError
            rather than silently clobbering it. commands_shared.save_project
            catches ConflictError and turns it into a clean "re-run" message.

        This is detection, not rescue: the losing writer's change is dropped,
        not merged. ``validate=False`` because the normal path already
        validated in commands_shared.save_project, and the recovery/purge path
        (save_project_unsafe → here) must be able to persist an
        intentionally-invalid document.
        """
        import local_writer
        baseline = LocalStore._save_baseline.get(self._baseline_key())
        _, new_hash = local_writer.atomic_apply(
            self._project_file,
            lambda _current: (data, None),
            baseline=baseline,
            validate=validate,
        )
        self._last_hash = new_hash
        LocalStore._save_baseline[self._baseline_key()] = new_hash

    def apply(self, op, *, validate: bool = True):
        """Serialised read→op→write for the JSON backend.

        Gives the Store protocol a uniform ``apply`` so callers never have to
        branch on backend (ms-148 e-5414: SqliteStore.apply is the SQLite twin).
        Routes through ``local_writer.atomic_apply``, which holds one lock across
        the whole read→op→write window — the same guarantee the former
        ``operations._apply_local`` gave — so a concurrent writer is serialised,
        not lost. ``op(data) -> (new_data, result)``; returns ``result``.
        """
        import local_writer
        result, new_hash = local_writer.atomic_apply(
            self._project_file, op, baseline=None, validate=validate)
        self._last_hash = new_hash
        LocalStore._save_baseline[self._baseline_key()] = new_hash
        return result

    def has_changed(self) -> bool:
        """Check if the file has changed since last load/save.

        Returns True on first call (before any load/save) to trigger initial load.
        """
        current = self._file_hash()
        if self._last_hash is None:
            self._last_hash = current
            return True
        if current != self._last_hash:
            self._last_hash = current
            return True
        return False

    def is_cloud(self) -> bool:
        return False

    def list_documents(self) -> list:
        """List documents from local .beacon/documents/.

        Returns entries matching the cloud API shape so the CLI does not
        branch on backend (= ms-84 Phase 2 受入条件 10、 _read_local_doc が
        commands.py に持っていた parse 経路の Store 化)。 各 entry は
        doc_id / title / scope plus optional milestone / operation /
        trek_id / status / trashed_at / trashed_by / trash_reason /
        updated_at を持つ。
        """
        import glob as g
        doc_dir = os.path.join(os.path.dirname(self._project_file), "documents")
        if not os.path.isdir(doc_dir):
            return []
        results = []
        for fpath in sorted(g.glob(os.path.join(doc_dir, "*.md"))):
            fname = os.path.basename(fpath)
            doc_id = fname[:-3]
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = f.read()
            except (IOError, UnicodeDecodeError):
                continue
            meta, body = _read_frontmatter(raw)
            scope = meta.get("scope", "memo")
            title = doc_id
            for line in body.strip().splitlines():
                line = line.strip()
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
                if line:
                    break
            try:
                import datetime
                stat = os.stat(fpath)
                updated_at = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
            except OSError:
                updated_at = ""
            entry = {
                "doc_id": doc_id,
                "title": title,
                "scope": scope,
                "updated_at": updated_at,
            }
            for k in ("format", "target", "milestone", "operation", "trek_id",
                      "status", "trashed_at", "trashed_by", "trash_reason"):
                if meta.get(k):
                    entry[k] = meta[k]
            results.append(entry)
        return results

    def get_document(self, doc_id: str) -> dict:
        """Get a single document from local .beacon/documents/.

        Returns full parsed shape (doc_id / title / scope / content /
        updated_at plus optional milestone / operation / trek_id /
        status / trashed_*) matching the cloud GET /documents/{id} shape
        so the CLI does not branch on backend.
        """
        doc_dir = os.path.join(os.path.dirname(self._project_file), "documents")
        fpath = os.path.join(doc_dir, f"{doc_id}.md")
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except (FileNotFoundError, IOError):
            return {}
        meta, body = _read_frontmatter(content)
        title = doc_id
        for line in body.strip().splitlines():
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                break
            if line:
                break
        try:
            import datetime
            stat = os.stat(fpath)
            updated_at = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
        except OSError:
            updated_at = ""
        result = {
            "doc_id": doc_id,
            "title": title,
            "scope": meta.get("scope", "memo"),
            "content": content,
            "updated_at": updated_at,
        }
        for k in ("format", "milestone", "operation", "trek_id", "status",
                  "trashed_at", "trashed_by", "trash_reason"):
            if meta.get(k):
                result[k] = meta[k]
        return result

    def list_treks(self, *, actor_id: str | None = None,
                   status: str = "", include_archived: bool = False,
                   all_actors: bool = False) -> list[dict]:
        """List trek docs from the local trek_store directory.

        ``all_actors`` is honored by the cloud backend (= admin view) and
        ignored locally because there is no remote registry to broaden
        beyond the actor filter.
        """
        import trek_store
        return trek_store.list_treks(
            actor_id=actor_id,
            status=status or None,
            include_archived=include_archived,
        )

    def get_trek(self, trek_id: str) -> dict:
        """Load a trek doc from the local trek_store directory.

        Raises ``ValueError`` when the trek is unknown so the CLI side
        can show the same ``trek '<id>' not found`` message regardless
        of backend (= matches StoreApi.get_trek contract)。
        """
        import trek_store
        doc = trek_store.load_trek(trek_id)
        if doc is None:
            raise ValueError(f"trek '{trek_id}' not found")
        return doc

    # ms-118 / e-4231 — Organizations (local file store, ~/.beacon/orgs/)

    def create_org(self, *, name: str, creator_user_id: str = "",
                   creator_email: str = "") -> dict:
        """Build a team org doc and persist it to the local org_store."""
        import datetime
        import org as org_mod
        import org_store
        if not creator_user_id:
            raise ValueError(
                "creator_user_id is required to create an org in local mode "
                "(= no auth token to resolve the owner from)")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        doc = org_mod.new_org(
            name,
            creator_user_id=creator_user_id,
            creator_email=creator_email,
            now=now,
        )
        org_store.save_org(doc)
        return doc

    def list_orgs(self, *, user_id: str | None = None) -> list[dict]:
        """List org docs from the local org_store directory."""
        import org_store
        return org_store.list_orgs(user_id=user_id)

    def get_org(self, org_id: str) -> dict:
        """Load an org doc from the local org_store directory.

        Raises ``ValueError`` when unknown so the CLI shows a uniform
        ``org '<id>' not found`` message regardless of backend.
        """
        import org_store
        doc = org_store.load_org(org_id)
        if doc is None:
            raise ValueError(f"org '{org_id}' not found")
        return doc

    def add_org_member(self, org_id: str, *, email: str,
                       role: str = "member") -> dict:
        """Add a member to a local org (participation-only, touches no project).

        Local mode is single-user and has no user directory, so the email is
        used as the member identifier. Only the org doc is written — no
        project participation is granted. Add-only: re-adding an existing
        member is rejected (= role の silent 上書き / 降格を防ぐ)。
        """
        import datetime
        import org as org_mod
        import org_store
        if not email:
            raise ValueError("email is required to add an org member")
        if "@" not in email:
            # user-id を渡された誤診を防ぐ (add-member は email を受ける)。
            raise ValueError("add-member takes an email address, not a user-id")
        org_mod.validate_invitable_role(role)  # 値域を local/cloud で一致させる
        org = org_store.load_org(org_id)
        if org is None:
            raise ValueError(f"org '{org_id}' not found")
        # local: email を識別子として使う (単一ユーザー機なので user directory が無い)。
        if org_mod.is_org_member(org, email):
            raise ValueError(
                f"{email} is already a member of {org_id} "
                "(role の変更は add-member ではなく別操作で行います)")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        org_mod.add_org_member(org, email, role=role, email=email,
                               added_by=email, now=now)
        org_store.save_org(org)
        return org

    def remove_org_member(self, org_id: str, *, target: str) -> dict:
        """Remove a member (by user_id or email) from a local org."""
        import org as org_mod
        import org_store
        org = org_store.load_org(org_id)
        if org is None:
            raise ValueError(f"org '{org_id}' not found")
        member = org_mod.find_org_member(org, target)
        if not member:
            raise ValueError(f"org member '{target}' not found in {org_id}")
        # last-owner 保護は remove_org_member 内 (= ValueError)。owner-only 厳格化は e-4234。
        org_mod.remove_org_member(org, member.get("user_id"))
        org_store.save_org(org)
        return org

    def delete_org(self, org_id: str) -> dict:
        """Delete a team org from the local org_store (ms-118 / e-4234).

        Local mode is single-user, so the caller is always the owner of their
        own store — the owner-only *authorization* is enforced on cloud (via the
        auth token). Here we still enforce the *structural* guard: personal orgs
        (= 自動生成の個人組織) cannot be deleted, and unknown ids raise so the CLI
        shows a uniform not-found message regardless of backend.
        """
        import org as org_mod
        import org_store
        org = org_store.load_org(org_id)
        if org is None:
            raise ValueError(f"org '{org_id}' not found")
        org_mod.assert_org_deletable(org)  # personal org は消せない
        org_store.delete_org(org_id)
        return {"org_id": org_id, "deleted": True}

    def rehome_project(self, project_id: str, *, target_org_id: str) -> dict:
        """Re-home the local project into ``target_org_id`` (ms-118 / e-4233).

        Local mode is single-project: this store front-ends exactly one
        ``project.json``, so re-home only accepts the project id that file
        carries (mismatched ids raise ``ValueError`` rather than silently
        re-homing the wrong project). The target org must already exist in the
        local org_store (= 存在しない org に project を吸わせない). Only the
        ``org_id`` link is rewritten; identity and history are untouched.
        """
        import org as org_mod
        import org_store
        if org_store.load_org(target_org_id) is None:
            raise ValueError(f"org '{target_org_id}' not found")
        data = self.load_project()
        current_id = data.get("project_id") or data.get("id")
        if project_id and current_id and project_id != current_id:
            raise ValueError(
                f"project '{project_id}' not found in this workspace "
                f"(local mode manages only '{current_id}')")
        previous = org_mod.rehome_project(data, target_org_id)
        self.save_project(data)
        return {
            "project_id": current_id,
            "org_id": target_org_id,
            "previous_org_id": previous,
        }

    def start_watching(self) -> None:
        pass

    def stop_watching(self) -> None:
        pass

    # ms-84 Phase 2 — fine-grained mutation (purge family)

    def purge_entry(self, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        """Match StoreApi.purge_entry shape, applied to the local file."""
        import core
        data = self.load_project()
        purged = core.entry_purge(data, entry_id, reason=reason, index=index)
        dup_report = core.find_duplicate_ids(data)
        self.save_project(data)
        return {
            "purged": purged,
            "still_dirty": any(dup_report.values()),
            "dup_report": dup_report,
        }

    def purge_operation(self, op_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Match StoreApi.purge_operation shape, applied to the local file."""
        import core
        data = self.load_project()
        purged = core.operation_purge(data, op_id, reason=reason, index=index)
        dup_report = core.find_duplicate_ids(data)
        self.save_project(data)
        return {
            "purged": purged,
            "still_dirty": any(dup_report.values()),
            "dup_report": dup_report,
        }

    def upsert_session_log(self, session_id: str, body: dict) -> bool:
        """No-op for local mode (= session log cloud subcollection 不在)。

        The local cache is written separately via
        ``commands._write_local_session_log``; this method exists for API
        symmetry with StoreApi so the CLI caller does not need to branch.
        """
        return False

    def list_session_ids(self) -> list[str]:
        """No-op for local mode (= remote session registry 不在)。

        Returns ``[]`` so the caller can do an unconditional set-union with
        locally-known ids.
        """
        return []

    def get_session_log(self, session_id: str) -> dict | None:
        """No-op for local mode. The local session log cache lives next to
        project.json and is read separately via _read_local_session_log."""
        return None

    def list_session_logs(self, limit: int = 0) -> list[dict]:
        """No-op for local mode. The CLI walks the local
        ``.beacon/session_logs/`` directory directly because the on-disk
        layout (= one file per session) does not match the cloud row shape."""
        return []

    def purge_milestone(self, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Match StoreApi.purge_milestone shape, applied to the local file.

        Wraps ``core.milestone_purge`` (= the actual mutation) with the
        load / save book-keeping that cmd_milestone_purge previously had
        inline. ``save_project`` is the bare file-write path that does not
        run ``validate_project`` — purge intentionally has to function on
        a project document that is already invalid (= the recovery flow's
        whole purpose), and the still-dirty case is surfaced in the return
        value so the CLI can warn without an extra retry path.
        """
        import core
        data = self.load_project()
        purged = core.milestone_purge(data, ms_id, reason=reason, index=index)
        dup_report = core.find_duplicate_ids(data)
        still_dirty = any(dup_report.values())
        self.save_project(data)
        return {
            "purged": purged,
            "still_dirty": still_dirty,
            "dup_report": dup_report,
        }

    # ms-84 Phase 1 — fine-grained reads

    def get_milestone(self, ms_id: str) -> dict:
        """Match StoreApi.get_milestone shape, sourced from the local file."""
        import core
        data = self.load_project()
        matches = core.find_milestones(data, ms_id)
        if not matches:
            raise ValueError(f"Milestone '{ms_id}' not found")
        ms = matches[0]
        entries = ms.get("entries", []) or []
        total, done = core.count_task_status(entries)
        return {
            **ms,
            "total_tasks": total,
            "done_tasks": done,
            "entries": core.entries_to_json(entries),
        }

    # ms-84 Phase 2 — trek read passthrough.

    def get_trek(self, trek_id: str) -> dict:
        """Match StoreApi.get_trek shape, sourced from the local trek store."""
        import trek_store
        t = trek_store.load_trek(trek_id)
        if t is None:
            raise ValueError(f"trek '{trek_id}' not found")
        return t

    def _file_hash(self) -> str | None:
        try:
            with open(self._project_file, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except FileNotFoundError:
            return None
