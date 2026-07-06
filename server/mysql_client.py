"""MySQL backend for the Beacon API (= profile=vps 経路の DB レイヤー、ms-96)。

`dynamodb_client.py` / `firestore_client.py` と同じ public API を提供する。
データモデルは **JSON-blob**: entity ごとに 1 テーブル、各行は (pk, sk, data)
の generic schema を持つ。DynamoDB の (PK, SK, item) を素直に MySQL に写した形
(= dynamodb_client の TABLES / TABLE_KEY_SCHEMA と 1:1 対応)。

  CREATE TABLE `beacon_{env}_{entity}` (
    pk   VARCHAR(191) NOT NULL,
    sk   VARCHAR(191) NOT NULL DEFAULT '',
    data JSON NOT NULL,
    PRIMARY KEY (pk, sk)
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

top-level entity (projects / users / treks) は sk='' の 1 行、subcollection は
sk = 各 sub-id (doc_id / event_id / session_id 等)。フィルタ・ソートは
DynamoDB 版と同じ意味論になるよう Python 側で行う (= 正しさ優先。JSON-path へ
predicate を push するのは後日の最適化)。
"""
from __future__ import annotations

import json
import os

# PyMySQL は pure-Python (build 依存なし)。BEACON_STORE_BACKEND != "mysql" 環境
# では import されないよう、boto3 と同じく import 失敗を握りつぶす。実際に関数を
# 呼んだ時にだけ RuntimeError を投げる (= 他 backend 経路にコストをかけない)。
try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Entity / table name resolution
# ---------------------------------------------------------------------------
# entity 名は dynamodb_client.TABLES のキーと一致させる (= cross-backend 整合)。
# MySQL のテーブル名はハイフンを避け、prefix を `beacon_{env}_` にする。
_ENV = os.environ.get("BEACON_ENV", "dev").replace("-", "_")
TABLE_PREFIX = f"beacon_{_ENV}"

# dynamodb_client.TABLES と同じ順序・同じ entity 集合 (19 個)。
ENTITIES = [
    # top-level (sk='')
    "projects",
    "users",
    "treks",
    # projects/{pid}/* subcollections
    "retros",
    "documents",
    "document_revisions",
    "changelog",
    "notes",
    "bus_events",
    "bus_cursors",
    "bus_nonces",
    "bus_audit",
    "bus_event_approvals",
    "sessions",
    "session_logs",
    "operation_envelopes",
    "active_claims",
    # treks/{trek_id}/* subcollections
    # firestore は treks/{tid}/logs に永続化する (ms-97 e-2603)。dynamodb_client は
    # ここを in-memory fallback で握っていて再起動で消える既知の穴があるが、MySQL は
    # 汎用 (pk, sk, data) テーブルで正しく永続化できるので trek_logs を実体化する。
    "trek_logs",
    # users/{uid}/* subcollections
    "machines",
    "session_lookup",
]

TABLES = {entity: f"{TABLE_PREFIX}_{entity}" for entity in ENTITIES}


# Subcollection の SK 名 (= dynamodb_client._SUBCOLLECTION_SK_NAMES と一致)。
# delete_project の cascade がこの集合を走査する (= dynamodb 版と同じ範囲)。
_SUBCOLLECTION_SK_NAMES = {
    "documents": "doc_id",
    "document_revisions": "revision_id",
    "changelog": "change_id",
    "notes": "note_id",
    "bus_events": "event_id",
    "bus_cursors": "cursor_id",
    "bus_nonces": "nonce",
    "bus_audit": "audit_id",
    "bus_event_approvals": "event_id",
    "sessions": "session_id",
    "session_lookup": "lookup_key",
    "session_logs": "session_id",
    "operation_envelopes": "envelope_id",
    "retros": "week",
}


def _table_name(entity: str) -> str:
    try:
        return TABLES[entity]
    except KeyError:
        raise KeyError(f"unknown entity {entity!r} (not in TABLES)")


# ---------------------------------------------------------------------------
# Connection (lazy, defensive reconnect)
# ---------------------------------------------------------------------------
# module-level に 1 本張って使い回す。PyMySQL はアイドルで切れることがあるので
# _conn() で毎回 ping(reconnect=True) して live を保証する。落ちていたら張り直す。
_CONN = None


def _env(*names: str, default: str | None = None) -> str | None:
    """最初に見つかった env var を返す (= 別名フォールバック)。

    VPS の /etc/beacon/db.env は `MYSQL_*` 名で認証情報を配る (e-2377)。ローカル
    検証や他環境では `BEACON_MYSQL_*` で上書きしたいので、両名を許容する
    (= `BEACON_MYSQL_*` を優先し、無ければ db.env の `MYSQL_*` を使う)。
    """
    for n in names:
        v = os.environ.get(n)
        if v is not None and v != "":
            return v
    return default


def _connect():
    if pymysql is None:
        raise RuntimeError("pymysql is not installed (= MySQL backend cannot be used)")
    return pymysql.connect(
        host=_env("BEACON_MYSQL_HOST", "MYSQL_HOST", default="127.0.0.1"),
        port=int(_env("BEACON_MYSQL_PORT", "MYSQL_PORT", default="3306")),
        user=_env("BEACON_MYSQL_USER", "MYSQL_USER"),
        password=_env("BEACON_MYSQL_PASSWORD", "MYSQL_PASSWORD") or "",
        database=_env("BEACON_MYSQL_DB", "MYSQL_DB", default="beacon"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _conn():
    global _CONN
    if pymysql is None:
        raise RuntimeError("pymysql is not installed (= MySQL backend cannot be used)")
    if _CONN is None:
        _CONN = _connect()
        return _CONN
    try:
        _CONN.ping(reconnect=True)
    except Exception:
        # ping ごと死んでいたら張り直す (= server 再起動 / 長時間アイドル対策)。
        _CONN = _connect()
    return _CONN


# ---------------------------------------------------------------------------
# Generic JSON-blob helpers (= DynamoDB の get/put/query/scan に相当)
# ---------------------------------------------------------------------------

def _dumps(data: dict) -> str:
    # default=str: datetime 等が万一混ざっても落とさない (= dynamodb は Decimal
    # を許すが MySQL 経路では JSON にシリアライズできる形へ寄せる)。
    return json.dumps(data, ensure_ascii=False, default=str)


def _get(entity: str, pk: str, sk: str = "") -> dict | None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT data FROM `{_table_name(entity)}` WHERE pk=%s AND sk=%s",
            (pk, sk),
        )
        row = cur.fetchone()
    if not row:
        return None
    return json.loads(row["data"])


def _put(entity: str, pk: str, data: dict, sk: str = "") -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{_table_name(entity)}` (pk, sk, data) "
            f"VALUES (%s, %s, %s) "
            f"ON DUPLICATE KEY UPDATE data=VALUES(data)",
            (pk, sk, _dumps(data)),
        )


def _insert_if_absent(entity: str, pk: str, data: dict, sk: str = "") -> bool:
    """Atomic first-write-wins INSERT. Returns True iff the row was created.

    PK 重複は IntegrityError になるので False を返す (= DynamoDB の
    ConditionExpression "attribute_not_exists" と同じ「最初の 1 回だけ True」)。
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO `{_table_name(entity)}` (pk, sk, data) "
                f"VALUES (%s, %s, %s)",
                (pk, sk, _dumps(data)),
            )
        return True
    except pymysql.err.IntegrityError:
        return False


def _delete(entity: str, pk: str, sk: str = "") -> bool:
    conn = _conn()
    with conn.cursor() as cur:
        n = cur.execute(
            f"DELETE FROM `{_table_name(entity)}` WHERE pk=%s AND sk=%s",
            (pk, sk),
        )
    return n > 0


def _delete_all(entity: str, pk: str) -> int:
    conn = _conn()
    with conn.cursor() as cur:
        return cur.execute(
            f"DELETE FROM `{_table_name(entity)}` WHERE pk=%s",
            (pk,),
        )


def _query_rows(entity: str, pk: str) -> list[tuple[str, dict]]:
    """All rows for a pk as (sk, data) tuples (= subcollection query)."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sk, data FROM `{_table_name(entity)}` WHERE pk=%s",
            (pk,),
        )
        rows = cur.fetchall()
    return [(r["sk"], json.loads(r["data"])) for r in rows]


def _query(entity: str, pk: str) -> list[dict]:
    return [data for _sk, data in _query_rows(entity, pk)]


def _scan(entity: str) -> list[dict]:
    """Whole-table scan (= list_all_projects / list_users 用)。"""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(f"SELECT data FROM `{_table_name(entity)}`")
        rows = cur.fetchall()
    return [json.loads(r["data"]) for r in rows]


def _now_iso_utc() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


# ---------------------------------------------------------------------------
# Table creation (idempotent)
# ---------------------------------------------------------------------------

def create_mysql_tables() -> int:
    """CREATE TABLE IF NOT EXISTS for every entity. Returns count created.

    generic schema (pk, sk, data JSON) を全 entity に対して張る。冪等なので
    起動のたびに走っても安全。
    """
    conn = _conn()
    created = 0
    for entity in ENTITIES:
        table = _table_name(entity)
        with conn.cursor() as cur:
            # 作成有無を数えるため事前に存在確認 (CREATE IF NOT EXISTS 自体は冪等)。
            cur.execute("SHOW TABLES LIKE %s", (table,))
            exists = cur.fetchone() is not None
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS `{table}` ("
                f"  pk   VARCHAR(191) NOT NULL,"
                f"  sk   VARCHAR(191) NOT NULL DEFAULT '',"
                f"  data JSON NOT NULL,"
                f"  PRIMARY KEY (pk, sk)"
                f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
        if not exists:
            created += 1
    return created


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def get_project(project_id: str) -> dict | None:
    return _get("projects", project_id)


def save_project(project_id: str, data: dict) -> None:
    # PK は project_id。data 側に同名キーが含まれていても上書きされるだけで害は無い。
    item = {**data, "project_id": project_id}
    _put("projects", project_id, item)


def list_projects(user_id: str | None = None,
                  include_archived: bool = False) -> list[dict]:
    """firestore_client.list_projects と同じ意味論 (ms-95 / e-2411, e-2794)。

    - user_id 指定時: owner 本人 or members に含まれる project のみ。
    - **owner 無し project は deny-by-default** (= 誰にも見せない)。旧来の
      「owner 未設定は全員に見せる」 migration fallthrough は e-2794 (2026-07-03)
      で撤廃済。別 Google ID で login すると他人の project が全部見える情報漏洩の
      原因だった。MySQL backend に古い漏洩挙動を持ち込まないよう、ここでも撤廃する。
    - 各行に owner / owner_email を additive で載せる (= beacon cloud list が
      N+1 なしで所有者を描画するため)。owner_email は per-call cache で解決。
    """
    items = _scan("projects")
    _email_cache: dict[str, str] = {}

    def _resolve_owner_email(owner_uid: str) -> str:
        if not owner_uid:
            return ""
        if owner_uid in _email_cache:
            return _email_cache[owner_uid]
        try:
            owner_doc = get_user(owner_uid) or {}
            email = owner_doc.get("email") or ""
        except Exception:
            email = ""
        _email_cache[owner_uid] = email
        return email

    result = []
    for item in items:
        if not include_archived and item.get("archived"):
            continue
        if user_id:
            owner = item.get("owner")
            # deny by default: owner が無ければ誰にも見せない (e-2794)。
            if not owner:
                continue
            members = [m.get("user_id") for m in item.get("members", [])]
            if owner != user_id and user_id not in members:
                continue
        owner_uid = item.get("owner") or ""
        result.append({
            "project_id": item.get("project_id", ""),
            "name": item.get("name", ""),
            "objective": item.get("objective", ""),
            "archived": item.get("archived", False),
            "owner": owner_uid,
            "owner_email": _resolve_owner_email(owner_uid),
        })
    return result


def list_all_projects() -> list[dict]:
    items = _scan("projects")
    return [
        {
            "project_id": item.get("project_id", ""),
            "name": item.get("name", ""),
            "owner": item.get("owner", ""),
            "member_count": len(item.get("members", [])),
            "milestone_count": len(item.get("milestones", [])),
            "updated_at": item.get("updated_at", ""),
        }
        for item in items
    ]


def delete_project(project_id: str) -> bool:
    if get_project(project_id) is None:
        return False
    # Cascade delete: project_id を PK に持つ subcollection の行を全削除する
    # (= dynamodb_client.delete_project と同じ範囲 = _SUBCOLLECTION_SK_NAMES)。
    for entity in _SUBCOLLECTION_SK_NAMES:
        _delete_all(entity, project_id)
    _delete("projects", project_id)
    return True


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> dict | None:
    return _get("users", user_id)


def get_or_create_user(user_id: str, email: str) -> dict:
    import datetime
    existing = get_user(user_id)
    if existing:
        if existing.get("email") != email:
            existing["email"] = email
            _put("users", user_id, existing)
        return existing
    user_data = {
        "user_id": user_id,
        "email": email,
        "role": "user",
        "created_at": datetime.datetime.now().isoformat(),
    }
    _put("users", user_id, user_data)
    return user_data


def list_users() -> list[dict]:
    # 各 dict は user_id を含む (= put 時に Item へ含めているため)。
    return _scan("users")


def update_user(user_id: str, updates: dict) -> bool:
    existing = get_user(user_id)
    if existing is None:
        return False
    if not updates:
        return True
    # 指定 key だけを書き換え、他は保持する (= dynamodb SET UpdateExpression 同等)。
    existing.update(updates)
    _put("users", user_id, existing)
    return True


def delete_user(user_id: str) -> bool:
    if get_user(user_id) is None:
        return False
    _delete("users", user_id)
    return True


def find_user_by_email(email: str) -> tuple[str, dict] | None:
    # v1 では専用 index を作らず全 scan + in-memory filter で代用 (= dynamodb 同挙動)。
    items = [it for it in _scan("users") if it.get("email") == email]
    if not items:
        return None
    user = items[0]
    return user.get("user_id", ""), user


# ---------------------------------------------------------------------------
# Retros (subcollection: PK=project_id, SK=week)
# ---------------------------------------------------------------------------

def list_retros(project_id: str) -> list[dict]:
    items = _query("retros", project_id)
    # week DESC で揃える (= Firestore 版が order_by("week", DESCENDING))
    items.sort(key=lambda it: it.get("week", ""), reverse=True)
    return items


def get_retro(project_id: str, week: str) -> dict | None:
    return _get("retros", project_id, sk=week)


def save_retro(project_id: str, week: str, content: str) -> None:
    import datetime
    _put("retros", project_id, {
        "project_id": project_id,
        "week": week,
        "content": content,
        "updated_at": datetime.datetime.now().isoformat(),
    }, sk=week)


# ---------------------------------------------------------------------------
# Documents (PK=project_id, SK=doc_id) + revisions (SK="{doc_id}#{rev:06d}")
# ---------------------------------------------------------------------------

def _extract_frontmatter_field(content: str, field: str, default: str = "") -> str:
    if not content.startswith("---"):
        return default
    end = content.find("\n---", 3)
    if end == -1:
        return default
    for line in content[4:end].split("\n"):
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return default


def _extract_scope(content: str) -> str:
    val = _extract_frontmatter_field(content, "scope", "memo")
    return val if val in ("core", "spec", "memo") else "memo"


def _generate_doc_id() -> str:
    # Firestore auto-id 互換の 20 文字英数字 (= 既存 UI / DB の見た目を保つ)
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def list_documents(project_id: str) -> list[dict]:
    items = _query("documents", project_id)
    result = []
    for data in items:
        if data.get("deleted"):
            continue
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        operation = data.get("operation") or _extract_frontmatter_field(
            data.get("content", ""), "operation"
        )
        entry = {
            "doc_id": data.get("doc_id", ""),
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
        if operation:
            entry["operation"] = operation
        result.append(entry)
    result.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return result


def get_document(project_id: str, doc_id: str) -> dict | None:
    return _get("documents", project_id, sk=doc_id)


def _last_revision_number(project_id: str, doc_id: str) -> int:
    prefix = f"{doc_id}#"
    last = 0
    for sk, it in _query_rows("document_revisions", project_id):
        if not sk.startswith(prefix):
            continue
        try:
            r = int(it.get("rev", 0))
        except (TypeError, ValueError):
            continue
        if r > last:
            last = r
    return last


def save_document(project_id: str, doc_id: str, title: str, content: str,
                  scope: str | None = None, updated_by: str = "unknown") -> str:
    import datetime
    resolved_scope = scope if scope in ("core", "spec", "memo") else _extract_scope(content)
    milestone = _extract_frontmatter_field(content, "milestone")
    operation = _extract_frontmatter_field(content, "operation")
    # ms-69 / e-1663: trek_id is optional (= 既存 doc 影響なし migration 不要)
    trek_id = _extract_frontmatter_field(content, "trek_id")
    now_iso = datetime.datetime.now().isoformat()

    if not doc_id:
        doc_id = _generate_doc_id()

    data = {
        "project_id": project_id,
        "doc_id": doc_id,
        "title": title,
        "content": content,
        "scope": resolved_scope,
        "updated_at": now_iso,
        "updated_by": updated_by,
    }
    if milestone:
        data["milestone"] = milestone
    if operation:
        data["operation"] = operation
    if trek_id:
        data["trek_id"] = trek_id

    # 既存があれば現行 content を revision に積んでから上書き
    existing = get_document(project_id, doc_id)
    if existing:
        next_rev = _last_revision_number(project_id, doc_id) + 1
        revision_id = f"{doc_id}#{next_rev:06d}"
        _put("document_revisions", project_id, {
            "project_id": project_id,
            "revision_id": revision_id,
            "doc_id": doc_id,
            "rev": next_rev,
            "content": existing.get("content", ""),
            "title": existing.get("title", ""),
            "ts": existing.get("updated_at", ""),
            "saved_by": existing.get("updated_by", "unknown"),
        }, sk=revision_id)

    _put("documents", project_id, data, sk=doc_id)
    return doc_id


def list_document_revisions(project_id: str, doc_id: str) -> list:
    prefix = f"{doc_id}#"
    items = [it for sk, it in _query_rows("document_revisions", project_id)
             if sk.startswith(prefix)]
    # rev DESC (= Firestore order_by("rev", DESCENDING) と一致)
    items.sort(key=lambda it: it.get("rev", 0), reverse=True)
    return [{"rev": it.get("rev"), "ts": it.get("ts"), "saved_by": it.get("saved_by")}
            for it in items]


def get_document_revision(project_id: str, doc_id: str, rev: int) -> dict | None:
    revision_id = f"{doc_id}#{int(rev):06d}"
    it = _get("document_revisions", project_id, sk=revision_id)
    if not it:
        return None
    return {
        "rev": it.get("rev"),
        "content": it.get("content", ""),
        "title": it.get("title", ""),
        "ts": it.get("ts"),
        "saved_by": it.get("saved_by"),
    }


def delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                    reason: str = "") -> bool:
    """Soft-delete a document (sets deleted flag).

    Optional ``reason`` is stored as ``trash_reason`` for audit symmetry
    with local mode's frontmatter (ms-14 e-991). Clears any prior restore
    stamps so audit fields reflect the current trash event. Returns True if
    existed.
    """
    import datetime
    existing = get_document(project_id, doc_id)
    if existing is None:
        return False
    existing["deleted"] = True
    existing["deleted_at"] = datetime.datetime.now().isoformat()
    existing["deleted_by"] = deleted_by
    # restore stamps を消す (= dynamodb REMOVE / firestore DELETE_FIELD 相当)
    for k in ("restored_at", "restored_by", "restore_reason"):
        existing.pop(k, None)
    if reason:
        existing["trash_reason"] = reason
    else:
        existing.pop("trash_reason", None)
    _put("documents", project_id, existing, sk=doc_id)
    return True


def sweep_trashed_documents(project_id: str, *, days: int = 30,
                            dry_run: bool = False) -> list[str]:
    """Hard-delete soft-deleted docs older than ``days`` (ms-14 e-991).

    Docs with ``deleted=true`` but missing ``deleted_at`` are NOT swept
    (no timestamp, no proof the window has passed). Revisions are cascaded.
    """
    import datetime
    cutoff_iso = (datetime.datetime.now()
                  - datetime.timedelta(days=max(1, days))).isoformat()
    purged: list[str] = []
    for it in _query("documents", project_id):
        if not it.get("deleted"):
            continue
        deleted_at = it.get("deleted_at", "")
        if not deleted_at or deleted_at >= cutoff_iso:
            continue
        doc_id = it.get("doc_id", "")
        if not doc_id:
            continue
        purged.append(doc_id)
        if dry_run:
            continue
        # Cascade revisions first then delete the doc itself
        prefix = f"{doc_id}#"
        for sk, _rev in _query_rows("document_revisions", project_id):
            if sk.startswith(prefix):
                _delete("document_revisions", project_id, sk=sk)
        _delete("documents", project_id, sk=doc_id)
    return purged


# ---------------------------------------------------------------------------
# Active claims (subcollection: PK=project_id, SK=claim_id)  ms-55 e-1730
# ---------------------------------------------------------------------------

def list_active_claims(project_id: str) -> list[dict]:
    out: list[dict] = []
    for it in _query("active_claims", project_id):
        data = dict(it)
        # claim_id を必ず surface する (= legacy back-fill 対策、dynamodb 同挙動)。
        data.setdefault("claim_id", data.pop("claim_id", "") or "")
        # storage PK は wire shape から落とす。
        data.pop("project_id", None)
        out.append(data)
    out.sort(key=lambda r: r.get("issued_at") or "")
    return out


def get_active_claim(project_id: str, claim_id: str) -> dict | None:
    item = _get("active_claims", project_id, sk=claim_id)
    if not item:
        return None
    data = dict(item)
    data.pop("project_id", None)
    data.setdefault("claim_id", claim_id)
    return data


def save_active_claim(project_id: str, claim_id: str, payload: dict) -> str:
    if not claim_id:
        raise ValueError("claim_id is required")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    item = {**payload, "project_id": project_id, "claim_id": claim_id}
    _put("active_claims", project_id, item, sk=claim_id)
    return claim_id


def delete_active_claim(project_id: str, claim_id: str) -> bool:
    if _get("active_claims", project_id, sk=claim_id) is None:
        return False
    _delete("active_claims", project_id, sk=claim_id)
    return True


# ---------------------------------------------------------------------------
# Changelog (subcollection: PK=project_id, SK=change_id)
# ---------------------------------------------------------------------------
# dynamodb_client では未実装 (NotImplementedError) なので firestore_client の
# 意味論を移植する: best-effort append、list は newest-first + since フィルタ。

def append_changelog(project_id: str, entry: dict) -> str:
    """Append one structured entry to the project's changelog subcollection.

    Best-effort: returns the document id on success, empty string on
    failure. Callers MUST NOT raise on failure — audit trail は non-functional
    concern であり、本来の書き込みを絶対に壊してはならない。
    """
    import datetime
    import secrets

    # ミリ秒精度 ISO + 4 hex で並び順が時系列になるようにする
    # (= 文字列 SK 辞書順 = 時系列順)。
    now = datetime.datetime.now(datetime.timezone.utc)
    change_id = now.strftime("%Y%m%dT%H%M%S.%f") + "-" + secrets.token_hex(2)

    payload = dict(entry)
    payload.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    try:
        _put("changelog", project_id, payload, sk=change_id)
        return change_id
    except Exception:  # noqa: BLE001 - best-effort write; never propagate.
        return ""


def list_changelog(project_id: str, *, since: str | None = None,
                   limit: int = 100) -> list[dict]:
    """Return changelog entries for a project, newest first.

    ``since`` (ISO8601) は ts > since でフィルタ (= incremental polling)。
    ``limit`` は 1..500 にクランプ。各 dict は firestore 版と同じく "id" キー
    (= change_id) を含む。
    """
    limit = max(1, min(500, int(limit)))
    rows = []
    for sk, data in _query_rows("changelog", project_id):
        entry = dict(data)
        entry["id"] = sk
        rows.append(entry)
    if since:
        rows = [r for r in rows if r.get("ts", "") > since]
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Notes (subcollection: PK=project_id, SK=note_id)
# ---------------------------------------------------------------------------

def add_note(project_id: str, note: dict) -> str:
    """Add a session note. Returns the generated note ID."""
    note_id = _generate_doc_id()
    _put("notes", project_id, dict(note), sk=note_id)
    return note_id


def list_notes(project_id: str) -> list[dict]:
    """List session notes ordered by timestamp."""
    items = _query("notes", project_id)
    items.sort(key=lambda it: it.get("ts", ""))
    return items


def clear_notes(project_id: str) -> None:
    """Delete all session notes for a project."""
    _delete_all("notes", project_id)


# ---------------------------------------------------------------------------
# Bus events / cursors / nonces / audit
# ---------------------------------------------------------------------------

def _mint_timestamp_id() -> str:
    """Millisecond-prefix sortable id (= "<epoch_ms:013>-<rand6>").

    辞書順 = 時系列順を保証しつつ、ms 同一書き込みが衝突しないよう後ろに 6 字の
    ランダム英数字を付ける。
    """
    import secrets
    import string
    import time
    ms = int(time.time() * 1000)
    alphabet = string.ascii_letters + string.digits
    rand = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{ms:013d}-{rand}"


def append_bus_event(project_id: str, data: dict) -> str:
    """Append a bus event (auto-id) and return the event_id.

    呼び出し側が data に created_at (ISO8601) を含めることが前提。event_id は
    ms-prefix なので SK 辞書順 = チャネル横断の時系列順になる。
    """
    event_id = _mint_timestamp_id()
    item = {**data, "project_id": project_id, "event_id": event_id}
    _put("bus_events", project_id, item, sk=event_id)
    return event_id


def get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    item = _get("bus_cursors", project_id, sk=recipient_id)
    if not item:
        return {}
    # cursor_id (= recipient_id) / project_id は内部キー、callers には返さない
    return {k: v for k, v in item.items() if k not in ("project_id", "cursor_id")}


def advance_bus_cursor(project_id: str, recipient_id: str,
                       last_seen_at: str) -> dict:
    """Forward-only cursor advance. Returns the resulting cursor.

    既存カーソルより新しい last_seen_at だけ書き込む。同一 / 古い値の場合は
    既存を返して idempotent な no-op にする (= stale クライアントの巻き戻し防止)。
    """
    existing = _get("bus_cursors", project_id, sk=recipient_id) or {}
    existing_seen = existing.get("last_seen_at", "")
    if existing_seen and last_seen_at <= existing_seen:
        return {k: v for k, v in existing.items()
                if k not in ("project_id", "cursor_id")}
    now = _now_iso_utc()
    merged = {
        **existing,
        "project_id": project_id,
        "cursor_id": recipient_id,
        "last_seen_at": last_seen_at,
        "updated_at": now,
    }
    _put("bus_cursors", project_id, merged, sk=recipient_id)
    return {"last_seen_at": last_seen_at, "updated_at": now}


def check_and_record_bus_nonce(project_id: str, nonce: str,
                               expires_at: str) -> bool:
    """Return True iff ``nonce`` is fresh; atomically record it on first use.

    PK (pk=project_id, sk=nonce) の UNIQUE 制約による INSERT で、DynamoDB の
    ConditionExpression と同じ "最初の 1 回だけ True" を実現する。競合時は
    IntegrityError となり False を返す。
    """
    now = _now_iso_utc()
    return _insert_if_absent("bus_nonces", project_id, {
        "project_id": project_id,
        "nonce": nonce,
        "expires_at": expires_at,
        "recorded_at": now,
    }, sk=nonce)


def set_bus_event_receipt(project_id: str, event_id: str, stage: str,
                          recipient_session_id: str) -> dict | None:
    """Stamp a per-event receipt timestamp (DM read receipt, e-1348).

    First-write-wins per stage (= delivered/opened)。既存 stamp があれば上書き
    せず ``already_set=True`` で返す。Returns ``None`` if the event does not exist.
    """
    if stage not in ("delivered", "opened"):
        raise ValueError(f"set_bus_event_receipt: invalid stage {stage!r}")
    ts_field = f"{stage}_at"
    by_field = f"{stage}_by"

    existing = _get("bus_events", project_id, sk=event_id)
    if not existing:
        return None

    existing_ts = existing.get(ts_field)
    if existing_ts:
        return {
            "event_id": event_id,
            "stage": stage,
            "timestamp": existing_ts,
            "by": existing.get(by_field, ""),
            "already_set": True,
        }
    now = _now_iso_utc()
    existing[ts_field] = now
    existing[by_field] = recipient_session_id
    _put("bus_events", project_id, existing, sk=event_id)
    return {
        "event_id": event_id,
        "stage": stage,
        "timestamp": now,
        "by": recipient_session_id,
        "already_set": False,
    }


def find_bus_event(project_id: str, event_id: str) -> dict | None:
    return _get("bus_events", project_id, sk=event_id)


def append_bus_audit(project_id: str, record: dict) -> str:
    audit_id = _mint_timestamp_id()
    _put("bus_audit", project_id,
         {**record, "project_id": project_id, "audit_id": audit_id},
         sk=audit_id)
    return audit_id


def list_bus_audit(project_id: str, *, since: str = "",
                   limit: int = 100) -> list[dict]:
    """List audit records ordered by received_at (most recent at end).

    since は received_at に対する > 比較。received_at は caller 任意フィールドの
    ため、SK 順ではなく received_at で明示ソートして整合を保つ。
    """
    items = _query("bus_audit", project_id)
    if since:
        items = [it for it in items if it.get("received_at", "") > since]
    items.sort(key=lambda it: it.get("received_at", ""))
    if limit:
        items = items[:limit]
    return items


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    """List bus events ordered by created_at.

    契約 (firestore / dynamodb 版と同一):
      - since: created_at > since の event のみ返す
      - channel: equality filter
      - limit: 結果件数の上限
    """
    items = _query("bus_events", project_id)
    if since:
        items = [it for it in items if it.get("created_at", "") > since]
    if channel:
        items = [it for it in items if it.get("channel") == channel]
    items.sort(key=lambda it: it.get("created_at", ""))
    if limit:
        items = items[:limit]
    return items


# ---------------------------------------------------------------------------
# Bus event approvals (sidecar: PK=project_id, SK=event_id)  ms-70 / e-1712
# ---------------------------------------------------------------------------

_BUS_EVENT_APPROVAL_STATUSES = ("pending", "approved", "denied", "auto")


def get_bus_event_approval(project_id: str, event_id: str) -> dict | None:
    """Return the sidecar approval doc for ``event_id`` or None if absent.

    None is the **legacy / auto-allow** signal — callers must treat None as
    ``approval_status="auto"`` (= sidecar 導入前の bus_events との後方互換)。
    """
    item = _get("bus_event_approvals", project_id, sk=event_id)
    if not item:
        return None
    return {k: v for k, v in item.items() if k != "project_id"}


def put_bus_event_approval(project_id: str, event_id: str, *,
                           approval_status: str,
                           sender_user_id: str,
                           receiver_user_id: str,
                           decision_by: str | None = None,
                           decision_at: str | None = None) -> dict:
    """Write or update the sidecar approval row for ``event_id``.

    最初の呼び出しで ``created_at`` をサーバ時刻に設定し、以後の呼び出し
    (= pending → approved | denied) は decision 系フィールドを更新しつつ
    ``created_at`` は保持する。Returns 7-field dict (project_id を除く)。
    """
    if approval_status not in _BUS_EVENT_APPROVAL_STATUSES:
        raise ValueError(
            f"put_bus_event_approval: invalid approval_status "
            f"{approval_status!r} (allowed: {_BUS_EVENT_APPROVAL_STATUSES})"
        )
    existing = _get("bus_event_approvals", project_id, sk=event_id) or {}
    now = _now_iso_utc()
    created_at = existing.get("created_at") or now
    item = {
        "project_id": project_id,
        "event_id": event_id,
        "approval_status": approval_status,
        "decision_by": decision_by,
        "decision_at": decision_at,
        "created_at": created_at,
        "sender_user_id": sender_user_id,
        "receiver_user_id": receiver_user_id,
    }
    _put("bus_event_approvals", project_id, item, sk=event_id)
    return {k: v for k, v in item.items() if k != "project_id"}


def list_pending_approvals(project_id: str, *,
                           receiver_user_id: str | None = None,
                           limit: int = 100) -> list[dict]:
    """List sidecar rows in ``approval_status="pending"`` ordered by created_at.

    ``receiver_user_id`` (optional): 受信者一致で絞る (in-memory)。
    ``limit``: 返却件数上限 (default 100)。receiver filter の後に適用する。
    """
    rows = []
    for it in _query("bus_event_approvals", project_id):
        if it.get("approval_status") != "pending":
            continue
        rows.append({k: v for k, v in it.items() if k != "project_id"})
    rows.sort(key=lambda it: it.get("created_at", ""))
    if receiver_user_id:
        rows = [r for r in rows if r.get("receiver_user_id") == receiver_user_id]
    if limit:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Sessions (PK=project_id, SK=session_id)
# ---------------------------------------------------------------------------

def upsert_session(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session document by session_id (merge semantics).

    指定された key だけ書く、ほかは触らない (= Firestore set(merge=True) /
    DynamoDB SET UpdateExpression 同等)。空 data は no-op。
    """
    if not data:
        return
    existing = _get("sessions", project_id, sk=session_id) or {}
    merged = {**existing, **data, "project_id": project_id,
              "session_id": session_id}
    _put("sessions", project_id, merged, sk=session_id)


def stamp_session_actor_email(project_id: str, session_id: str,
                              email: str) -> None:
    """Stamp ``actor.email`` on a session document without touching siblings
    (= ms-54 e-1349)。
    """
    if not email:
        return
    existing = _get("sessions", project_id, sk=session_id) or {}
    actor = existing.get("actor")
    if isinstance(actor, dict):
        actor = dict(actor)
        actor["email"] = email
    else:
        actor = {"email": email}
    existing["actor"] = actor
    existing["project_id"] = project_id
    existing["session_id"] = session_id
    _put("sessions", project_id, existing, sk=session_id)


def list_sessions(project_id: str) -> list[dict]:
    """List sessions for a project ordered by last_active desc."""
    items = _query("sessions", project_id)
    items.sort(key=lambda it: it.get("last_active", ""), reverse=True)
    return [{**it, "session_id": it.get("session_id", "")} for it in items]


# ---------------------------------------------------------------------------
# Machines + session minting (ms-62 / e-1509)
# ---------------------------------------------------------------------------
# machines       PK=user_id,    SK=fingerprint_id
# session_lookup PK=project_id, SK=lookup_key ("{machine_id}_{parent_pid}")

def _safe_doc_id(s: str) -> str:
    """Firestore safe id とほぼ同じ。/ を含むか長すぎる場合はハッシュ化。"""
    import hashlib
    if not s:
        return "_empty"
    if "/" in s or len(s) > 200:
        return "h-" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
    return s


def get_or_mint_machine(user_id: str, fingerprint: str, *,
                        hostname: str = "", agent: str = "") -> tuple[str, bool]:
    """Get or mint machine_id for (user_id, fingerprint).

    Returns ``(machine_id, minted)``. fingerprint は SK 用にサニタイズし、
    返す machine_id はサーバ側 nonce (= fingerprint を漏らさない)。
    """
    import secrets
    if not user_id or not fingerprint:
        raise ValueError("user_id and fingerprint are required")

    fingerprint_id = _safe_doc_id(fingerprint)
    now = _now_iso_utc()

    existing = _get("machines", user_id, sk=fingerprint_id)
    if existing:
        existing["last_seen_at"] = now
        _put("machines", user_id, existing, sk=fingerprint_id)
        return existing.get("machine_id", ""), False

    machine_id = f"mc-{secrets.token_hex(8)}"
    _put("machines", user_id, {
        "user_id": user_id,
        "fingerprint_id": fingerprint_id,
        "machine_id": machine_id,
        "fingerprint": fingerprint,
        "hostname": hostname or fingerprint,
        "agent": agent,
        "created_at": now,
        "last_seen_at": now,
    }, sk=fingerprint_id)
    return machine_id, True


def get_or_mint_session_by_tuple(project_id: str, machine_id: str,
                                 parent_pid: int, *, user_id: str,
                                 cwd: str = "",
                                 metadata: dict | None = None) -> dict:
    """Server-side identity tuple → session_id resolver (= ms-62 e-1509)。

    (project_id, machine_id, parent_pid) を session_id に解決する。lookup miss
    なら mint し、lookup doc + session doc を登録して返す。race で 2 つ mint
    されたら後勝ち (= firestore 版と同じ保証水準)。
    """
    import secrets
    import time

    if not project_id or not machine_id or not isinstance(parent_pid, int):
        raise ValueError("project_id, machine_id, parent_pid are required")

    lookup_key = _safe_doc_id(f"{machine_id}_{parent_pid}")
    now = _now_iso_utc()

    existing_lookup = _get("session_lookup", project_id, sk=lookup_key)
    if existing_lookup:
        sid = existing_lookup.get("session_id", "")
        if sid:
            # session doc 側の last_heartbeat_at + metadata を更新
            existing_session = _get("sessions", project_id, sk=sid) or {}
            heartbeat_data = {
                "last_heartbeat_at": now,
                "machine_id": machine_id,
                "parent_pid": parent_pid,
                "cwd": cwd,
            }
            if metadata:
                heartbeat_data.update(metadata)
            merged = {**existing_session, **heartbeat_data,
                      "project_id": project_id, "session_id": sid}
            _put("sessions", project_id, merged, sk=sid)
            return {
                "session_id": sid,
                "minted": False,
                "last_heartbeat_at": now,
                "created_at": existing_session.get("created_at", ""),
            }

    # Mint a fresh session_id (= Firestore 実装と同じ形式)
    epoch_ms = int(time.time() * 1000)
    prefix = machine_id[3:11] if machine_id.startswith("mc-") else machine_id[:8]
    new_sid = f"sv-{prefix}-{epoch_ms}-{secrets.token_hex(4)}"

    session_data = {
        "project_id": project_id,
        "session_id": new_sid,
        "user_id": user_id,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "cwd": cwd,
        "created_at": now,
        "last_heartbeat_at": now,
        "last_active": now,
        "source": "server_minted",
    }
    if metadata:
        session_data.update(metadata)
    _put("sessions", project_id, session_data, sk=new_sid)
    _put("session_lookup", project_id, {
        "project_id": project_id,
        "lookup_key": lookup_key,
        "session_id": new_sid,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "created_at": now,
    }, sk=lookup_key)
    return {
        "session_id": new_sid,
        "minted": True,
        "last_heartbeat_at": now,
        "created_at": now,
    }


def list_user_machines(user_id: str) -> list[dict]:
    return _query("machines", user_id)


# ---------------------------------------------------------------------------
# Session logs (ms-57 / e-1037)  PK=project_id, SK=session_id
# ---------------------------------------------------------------------------

def upsert_session_log(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session log entry. merge semantics same as upsert_session."""
    if not data:
        return
    existing = _get("session_logs", project_id, sk=session_id) or {}
    merged = {**existing, **data, "project_id": project_id,
              "session_id": session_id}
    _put("session_logs", project_id, merged, sk=session_id)


def list_session_logs(project_id: str,
                      limit: int | None = None) -> list[dict]:
    """List session logs ordered by last_aggregated_at desc."""
    items = _query("session_logs", project_id)
    items.sort(key=lambda it: it.get("last_aggregated_at", ""), reverse=True)
    if limit:
        items = items[:limit]
    return items


def get_session_log(project_id: str, session_id: str) -> dict | None:
    return _get("session_logs", project_id, sk=session_id)


# ---------------------------------------------------------------------------
# Operation envelopes (Tier 2) — ms-60 / e-1339   PK=project_id, SK=envelope_id
# ---------------------------------------------------------------------------

def get_active_operation_envelope(project_id: str,
                                  op_id: str) -> dict | None:
    items = _query("operation_envelopes", project_id)
    actives = [it for it in items
               if it.get("op_id") == op_id and it.get("status") == "active"]
    if not actives:
        return None
    if len(actives) > 1:
        actives.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return actives[0]


def issue_operation_envelope(project_id: str, op_id: str, spec_doc_id: str,
                             spec_revision_id: str, envelope_dict: dict,
                             approved_actions: list[str],
                             created_by: str) -> dict:
    """Store a freshly minted T2 envelope. Auto-revokes any prior active."""
    nonce = envelope_dict.get("nonce")
    if not nonce:
        raise ValueError("envelope_dict missing nonce")
    now = _now_iso_utc()

    # 既存 actives を revoke
    for it in _query("operation_envelopes", project_id):
        if it.get("op_id") == op_id and it.get("status") == "active":
            it["status"] = "revoked"
            it["revoked_at"] = now
            it["revoked_by"] = created_by
            it["revoke_reason"] = "superseded by new approve"
            _put("operation_envelopes", project_id, it,
                 sk=it.get("envelope_id", ""))

    new_doc = {
        "project_id": project_id,
        "envelope_id": nonce,
        "op_id": op_id,
        "spec_doc_id": spec_doc_id,
        "spec_revision_id": spec_revision_id,
        "envelope": envelope_dict,
        "approved_actions": list(approved_actions),
        "status": "active",
        "created_at": now,
        "created_by": created_by,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }
    _put("operation_envelopes", project_id, new_doc, sk=nonce)
    return new_doc


def revoke_operation_envelope(project_id: str, envelope_id: str,
                              revoked_by: str, reason: str) -> dict | None:
    """Mark envelope as revoked. Idempotent (already-revoked → return as-is)."""
    existing = _get("operation_envelopes", project_id, sk=envelope_id)
    if not existing:
        return None
    if existing.get("status") == "revoked":
        return existing
    now = _now_iso_utc()
    existing.update({
        "status": "revoked",
        "revoked_at": now,
        "revoked_by": revoked_by,
        "revoke_reason": reason,
    })
    _put("operation_envelopes", project_id, existing, sk=envelope_id)
    return existing


def list_operation_envelopes(project_id: str, op_id: str | None = None,
                             status: str | None = None) -> list[dict]:
    items = _query("operation_envelopes", project_id)
    if op_id:
        items = [it for it in items if it.get("op_id") == op_id]
    if status:
        items = [it for it in items if it.get("status") == status]
    items.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return items


def get_operation_envelope(project_id: str,
                           envelope_id: str) -> dict | None:
    return _get("operation_envelopes", project_id, sk=envelope_id)


# ---------------------------------------------------------------------------
# Treks (ms-69 / e-1652)  — top-level entity, PK=trek_id, sk=''
# ---------------------------------------------------------------------------

def get_trek(trek_id: str) -> dict | None:
    return _get("treks", trek_id)


def save_trek(trek_id: str, data: dict) -> None:
    item = {**data, "trek_id": trek_id}
    _put("treks", trek_id, item)


def list_treks(actor_id: str | None = None, *,
               status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    """List treks. See firestore_client.list_treks for semantics."""
    items = _scan("treks")
    result: list[dict] = []
    for item in items:
        if not include_archived and item.get("status") == "archived":
            continue
        if status and item.get("status") != status:
            continue
        if actor_id:
            creator = (item.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id")
                       for m in item.get("members", []) or []]
            if creator != actor_id and actor_id not in members:
                continue
        result.append(item)
    # Newest first (= matches firestore_client ordering).
    result.sort(key=lambda t: (t.get("created_at", ""), t.get("trek_id", "")),
                reverse=True)
    return result


def delete_trek(trek_id: str) -> bool:
    if get_trek(trek_id) is None:
        return False
    _delete("treks", trek_id)
    return True


# ---------------------------------------------------------------------------
# Trek structured logs (ms-97 Phase 7-C, AC26 / AC27, e-2603)
# dynamodb 版と同じく in-process dict の MVP fallback。cloud-shaped store は
# Firestore (= subcollection)。`trek_logs` テーブルが入るまでの best-effort。
# ---------------------------------------------------------------------------



def _mint_trek_log_id() -> str:
    import secrets as _secrets
    return f"log-{_secrets.token_hex(8)}"


def append_trek_log(trek_id: str, log_entry: dict) -> str:
    """treks/{trek_id}/logs に 1 件追記する (= firestore と同じ永続化)。

    dynamodb_client は in-memory fallback だが、MySQL は trek_logs テーブル
    (pk=trek_id, sk=log_id) に実体化する。log_id / trek_id は未指定なら補完。
    """
    payload = dict(log_entry or {})
    log_id = payload.get("log_id") or _mint_trek_log_id()
    payload["log_id"] = log_id
    payload["trek_id"] = trek_id
    _put("trek_logs", trek_id, payload, sk=log_id)
    return log_id


def list_trek_logs(trek_id: str, *, limit: int = 100,
                   since: str = "") -> list[dict]:
    """treks/{trek_id}/logs を created_at 昇順で返す (= firestore と同じ意味論)。

    since: ISO8601 の下限 (= created_at > since のみ)。limit: 返却上限。
    """
    rows = _query("trek_logs", trek_id)
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: (r.get("created_at", ""), r.get("log_id", "")))
    if limit and limit > 0:
        rows = rows[:limit]
    return rows
