"""Beacon Store - Storage abstraction layer.

Provides a protocol for project data storage and a factory function
to select the appropriate backend (local JSON or cloud API).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Protocol for beacon project data storage."""

    def load_project(self) -> dict:
        """Load the full project data."""
        ...

    def save_project(self, data: dict) -> None:
        """Save the full project data."""
        ...

    def has_changed(self) -> bool:
        """Check if project data has changed since last load.

        Used by the dashboard for refresh detection.
        """
        ...

    def is_cloud(self) -> bool:
        """Return True if this store is cloud-backed."""
        ...

    def start_watching(self) -> None:
        """Start receiving push notifications for changes.

        Cloud stores use WebSocket; local stores may no-op.
        """
        ...

    def stop_watching(self) -> None:
        """Stop receiving push notifications."""
        ...

    # ms-84 Phase 1 — fine-grained reads.
    # The legacy ``load_project()`` returns the whole project document and
    # the dashboard and CLI both pivot on it. Fine-grained reads let CLI
    # branches that currently call ``client.get(...)`` or scan ``data`` go
    # through the Store, which is what ms-84 Phase 2 then exploits to
    # collapse the 27+ ``_is_cloud_mode()`` branches into a single Store
    # call site.

    def get_milestone(self, ms_id: str) -> dict:
        """Fetch a single milestone (with task counts + entries).

        The returned dict carries ``total_tasks`` / ``done_tasks`` and a
        JSON-serialised ``entries`` list, matching the cloud
        ``GET /milestones/{ms_id}`` shape. Raises ``ValueError`` when the
        milestone is unknown so callers can show a CLI-friendly error
        without distinguishing local vs cloud.
        """
        ...

    # ms-84 Phase 2 — fine-grained mutation (purge family).
    # The cmd_milestone_purge cloud branch currently delegates to a
    # dedicated _cloud_milestone_purge helper; folding it into Store
    # lets the CLI drop the _is_cloud_mode branch entirely (= 受入条件 10
    # で要請される直接呼びの削減)。

    def purge_entry(self, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        """Hard-delete an entry record (= タスク / コミット / ノート等の物理削除)。

        Same contract as purge_milestone — returns ``{purged, still_dirty,
        dup_report}`` and translates HTTP / value errors uniformly so the
        CLI does not have to branch on backend.
        """
        ...

    def purge_operation(self, op_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Hard-delete an operation record (= 運用ジョブの物理削除)。

        Same contract as purge_milestone / purge_entry.
        """
        ...

    def upsert_session_log(self, session_id: str, body: dict) -> bool:
        """Upsert a session log document by session_id (= セッション集約の保存)。

        Returns True on successful persistence, False on failure or no-op.
        LocalStore returns False (= the cloud session log subcollection is
        a cloud-only artifact; local sessions persist via _write_local_session_log
        on the caller side). StoreApi calls the upsert endpoint with the
        same failure-swallowing contract as the legacy
        ``_push_session_log_to_cloud`` (= network / auth / 4xx all → False),
        so the caller does not need to branch on backend.
        """
        ...

    def list_session_ids(self) -> list[str]:
        """List session_ids visible to the backend (= cloud registry の列挙)。

        Used by the session rescue path to discover other sessions whose
        entries the local cache may have missed (= cross-machine orphans).
        LocalStore returns ``[]`` because there is no remote registry to
        consult; StoreApi calls the API and swallows transport failures,
        same best-effort contract as ``upsert_session_log``. The caller
        merges this result with locally-known ids without checking backend.
        """
        ...

    def get_session_log(self, session_id: str) -> dict | None:
        """Fetch a persisted session log document by session_id, or None
        if the backend has no record (= 404 / local 不在 / transport 失敗)。

        Used by the session aggregation path so the merge-with-remote step
        is uniform regardless of backend. LocalStore returns None
        (= session_log subcollection is cloud-only); StoreApi calls the
        API and translates 404 / transport failures to None so the caller
        can just check truthiness.
        """
        ...

    def list_session_logs(self, limit: int = 0) -> list[dict]:
        """List persisted session log documents (= most-recent-first)。

        LocalStore returns ``[]`` because session log persistence in local
        mode is per-file on disk and the caller handles directory listing
        directly (cmd_session_log_list keeps this fallback). StoreApi calls
        the API and returns the rows, swallowing transport failures.
        """
        ...

    # ms-84 Phase 2 — document read passthrough.
    # cmd_doc_list / cmd_doc_show / _spec_exists_for_ms all carry their own
    # ``_is_cloud_mode()`` branch today. Exposing these on Store lets the
    # CLI call ``store.list_documents()`` / ``store.get_document(doc_id)``
    # once and drop the branch (= 受入条件 10 の direct-call 削減 にカウント)。

    def list_documents(self) -> list[dict]:
        """List document metadata (= doc_id / title / scope / milestone /
        operation / trek_id / status / updated_at の一覧)。

        Both LocalStore and StoreApi return the same shape so the CLI can
        post-filter (scope / ms / op / trek / include_trashed) without
        branching on backend. LocalStore parses ``.beacon/documents/*.md``
        frontmatter; StoreApi calls the cloud API and swallows transport
        failures (= 空 list を返す best-effort、 cmd_doc_list 既存挙動と整合)。
        """
        ...

    def get_document(self, doc_id: str) -> dict:
        """Fetch a single document body + metadata by doc_id.

        Returns a dict with at least ``doc_id`` and ``content`` keys plus
        frontmatter fields (scope / milestone / operation / trek_id /
        status / updated_at) when available. Returns ``{}`` when not found
        (= LocalStore: ファイル不在、 StoreApi: 404 / transport 失敗)。
        """
        ...

    # ms-84 Phase 2 — trek read passthrough.
    # cmd_trek_show / cmd_trek_timeline / cmd_trek_aggregate 等が
    # ``_is_cloud_mode()`` で client.get_trek / trek_store.load_trek を
    # 切り替えている。Store に集約することで CLI から分岐を消す。

    def list_treks(self, *, actor_id: str | None = None,
                   status: str = "", include_archived: bool = False,
                   all_actors: bool = False) -> list[dict]:
        """List trek docs visible to the caller.

        Both backends honor ``status`` filter and ``include_archived``
        switch. ``all_actors`` is server-side only (= admin view) — local
        backend ignores it because there is no remote registry; ``actor_id``
        is local-only (= cloud server resolves the caller from auth token).
        Cloud transport / 403 errors propagate as RuntimeError, matching
        the legacy cloud branch behavior.
        """
        ...

    def get_trek(self, trek_id: str) -> dict:
        """Fetch a single trek doc by id.

        Both backends return the trek dict (members / scope / halt /
        leader_session_id 等を含む完全な doc)。Raises ``ValueError`` when the
        trek is unknown so CLI sites can show a uniform ``trek 'X' not
        found`` message regardless of backend (= LocalStore: ファイル不在、
        StoreApi: API 404)。Other transport / auth errors propagate as
        ``RuntimeError`` (= 既存の cloud path の挙動と一致)。
        """
        ...

    # ------------------------------------------------------------------
    # Organizations (ms-118 / e-4231) — top-level tenancy entity.
    # trek と同型: org は project.json の外に住む cross-project entity なので、
    # local backend は ~/.beacon/orgs/ の file、cloud backend は /api/orgs を
    # 経由する。CLI から backend 分岐を消すため Store に集約する。
    # ------------------------------------------------------------------

    def create_org(self, *, name: str, creator_user_id: str = "",
                   creator_email: str = "") -> dict:
        """team org (= 明示的に立てる組織) を作り、作成者を owner にする。

        ``creator_user_id`` / ``creator_email`` は **local backend のみ**が使う
        (= ローカルには auth token が無いので呼び出し側の identity を渡す)。cloud
        backend はこれを無視し、作成者を server が Bearer token から解決する (=
        client が owner を詐称できない)。戻り値は作られた org doc。
        """
        ...

    def list_orgs(self, *, user_id: str | None = None) -> list[dict]:
        """caller が member の org を一覧する。

        ``user_id`` は **local backend のみ**の可視性 filter (= cloud は server が
        token から解決)。``None`` は local では全件 (= admin view)。cloud transport /
        403 は RuntimeError として伝播する (= 既存 cloud path と一致)。
        """
        ...

    def get_org(self, org_id: str) -> dict:
        """org を id で 1 件取る。存在しない org は ``ValueError`` (CLI が backend
        非依存に ``org 'X' not found`` を出せるように)。transport / auth error は
        ``RuntimeError`` として伝播する。

        **membership の強制は cloud (server) の責務**: StoreApi 経由 (cloud) では、
        member でない実在 org も 404 → ``ValueError`` になる (= 存在を漏らさない、
        trailnode get_org と同方針)。一方 **LocalStore (local mode) は単一ユーザーの
        store** (= ``~/.beacon/orgs/`` はその端末の本人だけの器) なので、show では
        membership を強制せず id 一致だけで返す。この非対称は「local = 単一ユーザー、
        開示境界の真値は cloud」という設計の帰結であり、silent な穴ではない
        (list は local でも user_id filter を持つが、show は single-user 前提)。
        """
        ...

    def add_org_member(self, org_id: str, *, email: str,
                       role: str = "member") -> dict:
        """org に社員を「所属」させる (= org member にする、ms-118 / e-4232)。

        CLI では ``beacon org add-member`` (別名 ``invite``)。承諾フローは無く即時に
        member になる (= project 側の token+accept 招待とは別物)。

        **所属だけを与え、アクセスは付けない (participation-only)**: この操作は org doc の
        members[] にしか触れず、どの project の participation (= 参加 = アクセス) も変えない。
        追加された社員は org の member になるが、必要な project に別途参加させるまで
        どの project も見えない。

        **add-only**: 既に member の相手を再追加すると ``ValueError`` (= role の silent
        上書き / 降格を防ぐ)。role は member / admin のみ (owner は add-member で作らない、
        ``validate_invitable_role`` が値域を local/cloud で一致させる)。入力は email
        (user-id は不可)。cloud では server が email を実 user に解決し、未登録なら
        ``ValueError``。戻り値は更新後の org doc。transport / auth / 権限 / 既存 member
        (409) は ``RuntimeError`` として伝播する。
        """
        ...

    def remove_org_member(self, org_id: str, *, target: str) -> dict:
        """org から member を外す (ms-118 / e-4232)。``target`` は user_id か email。

        最後の owner は外せない (= org を owner 不在にしない安全弁、``ValueError``)。
        cloud では owner / admin だけが実行できる (= 破壊的操作の owner-only 厳格化と
        org 削除との統一ガードは e-4234)。存在しない member は ``ValueError``。戻り値は
        更新後の org doc。transport / auth / 権限エラーは ``RuntimeError``。
        """
        ...

    def delete_org(self, org_id: str) -> dict:
        """team org を削除する (ms-118 / e-4234)。破壊的操作なので **owner のみ**。

        戻り値は ``{"org_id": ..., "deleted": True}``。personal org (= 個人組織) は
        削除できない (``ValueError``、= 自動生成の器を消させない)。存在しない org も
        ``ValueError``。cloud では owner でない caller は 403 (= ``RuntimeError``)。
        member 削除と同じ owner-only ガード (``org.is_destructive_allowed``) を
        共有し、片方だけ緩い穴を作らない。transport / auth / 権限は ``RuntimeError``。
        """
        ...

    def rehome_project(self, project_id: str, *, target_org_id: str) -> dict:
        """project の所属 org を ``target_org_id`` へ張り替える (re-home、ms-118 / e-4233)。

        project の identity (project_id) と履歴は保ったまま ``org_id`` リンクだけを
        差し替える。開示は移動後の org 基準で即座に再評価される (= ms-113 の開示は
        現在の org_id を live 参照するのでキャッシュ無し)。戻り値は更新後の結果 dict::

            {"project_id": ..., "org_id": <new>, "previous_org_id": <old>}

        ``target_org_id`` が実在しない org を指す場合は ``ValueError`` (= 存在しない
        org に project を吸わせない)。cloud では project owner かつ target org の
        member でなければ拒否される (= owner-only の統一厳格化は e-4234)。transport /
        auth / 権限エラーは ``RuntimeError`` として伝播する。
        """
        ...

    def purge_milestone(self, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Hard-delete a milestone record (= 物理削除、duplicate-ID 回復用、Issue #14)。

        Returns a dict shaped::

            {
                "purged": {...the removed milestone fields...},
                "still_dirty": bool,    # True iff residual duplicates remain (local only)
                "dup_report": dict,     # find_duplicate_ids output (local only; {} in cloud)
            }

        Cloud-backed implementations return ``still_dirty=False`` + empty
        ``dup_report`` because the server enforces single-record purge per
        request and re-validates the project document afterwards. Raises
        ``ValueError`` on invalid input (missing reason, unknown id,
        out-of-range index, etc.) so the CLI can branch uniformly.
        """
        ...


def get_store(project_file: str | None = None) -> Store:
    """Return the appropriate Store instance.

    If .beacon/cloud.json exists alongside the project file,
    returns a StoreApi (cloud API). Otherwise returns LocalStore.
    """
    if project_file is None:
        project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")

    # Check for cloud config alongside the project file
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    cloud_config = os.path.join(beacon_dir, "cloud.json")

    # e-1861 (ms-61): cloud.json existence is the sole source of truth.
    # The legacy ``config.json["mode"] == "cloud"`` dual-check was retired
    # because a sub-agent rewriting config.json to ``{"mode": "local"}``
    # would silently flip every subsequent CLI call back to the stale
    # LocalStore branch, producing apparent user data loss (2026-06-15
    # incident). BEACON_CLOUD=1 still forces cloud for test harnesses.
    cloud_mode = (
        os.environ.get("BEACON_CLOUD") == "1"
        or os.path.exists(cloud_config)
    )

    if cloud_mode and os.path.exists(cloud_config):
        import json
        with open(cloud_config, "r", encoding="utf-8") as f:
            cloud_data = json.load(f)
        project_id = cloud_data.get("project_id")
        if not project_id:
            raise ValueError("cloud.json must contain 'project_id'")
        # ms-64 / e-1458: route api_url through the profile resolver so the
        # env > cwd cloud.json > profile.json > default precedence chain is
        # the single source of truth. Falls back to the bare cloud.json read
        # if profile.py is unimportable for any reason.
        try:
            import profile as _profile  # type: ignore[import-not-found]
            api_url = _profile.resolve_active_profile().api_url
        except Exception:
            api_url = cloud_data.get("api_url") or "https://beacon-ai.dev"
        from store_api import StoreApi

        def _token_provider():
            from auth import load_credentials
            creds = load_credentials()
            if not creds:
                return ""
            # Web auth mode: creds is a dict with id_token
            if isinstance(creds, dict):
                return creds.get("token", "")
            return (creds.id_token or creds.token) if creds else ""

        # ms-95 / e-746: pass the local project.json path so StoreApi can
        # mirror each cloud read/write back to the local cache. This keeps
        # ``.beacon/project.json`` fresh as a read-only mirror so Tauri's
        # ``load_project_json`` no longer renders a stale ms-22-era world
        # for the first few seconds before the WS push arrives (e-723).
        # The write-back is best-effort and never raises.
        return StoreApi(
            api_url, project_id, _token_provider,
            local_cache_path=project_file,
        )

    # ms-148 e-5414: local project state lives in SQLite (serialised writes,
    # crash-safe, no lost updates). An existing project.json is migrated into
    # SQLite on first use, verified before we trust it. project.json is kept as
    # a read-only mirror for the Tauri desktop app (rewired in a follow-up MS).
    #
    # BEACON_LOCAL_BACKEND=json forces the legacy JSON store — a rollback lever
    # if the SQLite path ever misbehaves in the field.
    if os.environ.get("BEACON_LOCAL_BACKEND", "sqlite").lower() == "json":
        from store_local import LocalStore
        return LocalStore(project_file)

    from store_sqlite import SqliteStore, sqlite_db_path_for, db_has_data
    db_path = sqlite_db_path_for(project_file)
    if not db_has_data(db_path) and os.path.exists(project_file):
        import store_migrate
        report = store_migrate.migrate_json_to_sqlite(project_file, db_path)
        # Raise only when THIS process migrated and the verification failed. A
        # migrated=False means another concurrent process already populated the
        # db (populate_if_empty) — that is success, not a reason to abort.
        if report.get("migrated") and not report.get("verified"):
            issues = (report.get("verification") or {}).get("issues")
            raise RuntimeError(
                f"SQLite migration verification failed for {project_file}: "
                f"{issues}. The JSON file was left untouched; inspect and "
                f"re-run (or set BEACON_LOCAL_BACKEND=json to stay on JSON)."
            )
    return SqliteStore(project_file, db_path=db_path)
