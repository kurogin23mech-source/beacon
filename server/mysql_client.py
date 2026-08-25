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
import threading

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
    # ms-113 / e-3731: Organization (組織) top-level entity, pk=org_id, sk=''。
    # 起動時 create_mysql_tables が CREATE TABLE IF NOT EXISTS で作る (schema が
    # DDL を追い越さない = 無停止 retrofit)。
    "organizations",
    # ms-111 / e-3620: 共有マスター identity の top-level entity。
    # master_accounts pk=master_account_id / master_contacts pk=master_contact_id、
    # どちらも sk=''。project 配下ではない (= org 単位で cross-project 共有される
    # 真値源) ので _SUBCOLLECTION_SK_NAMES には載せず delete_project cascade の
    # 対象外にする (organizations と同じ扱い)。起動時 create_mysql_tables が作る。
    "master_accounts",
    "master_contacts",
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
    # ms-151 / e-5474: machine API key (headless machine 認証の鍵) の project 配下
    # テーブル。pk=project_id, sk=key_id。起動時 create_mysql_tables が
    # CREATE TABLE IF NOT EXISTS で作る (schema が DDL を追い越さない = 無停止 retrofit)。
    "machine_keys",
    # ms-95 / e-5477: operation-fires claim (定期発火の二重駆動を防ぐ first-write-wins)。
    # firestore_client にしか無く store_router 未 re-export だったため MySQL/DynamoDB
    # backend では claim 経路が存在せず dedup が silent に無効だった (e-5477 で発見)。
    # pk=project_id, sk="{op_id}_{period}"。
    "operation_fires",
    "sessions",
    "session_logs",
    "operation_envelopes",
    "active_claims",
    # ms-96 v3 (entry-level split): milestones と entries をそれぞれ独立行にすることで、
    #   task_done / commit 追加といった実際の write 単位で 1 行の update に閉じる。
    #   v1 (whole-doc) だと 3.6 MiB を毎回書き直し、v2 (milestone-level) でも 424 KB
    #   になっていた書き込み増幅を、深さ 2 のツリーを subtree JSON 内包で表現する
    #   entry-level 分割で数 KB に圧縮する (= iruca3 提案、2026-07-06 合意)。
    #   pk=project_id, sk=milestone_id / entries は sk="{ms_id}#{entry_id}" の composite。
    "milestones",
    "entries",
    # ms-109 e-3591 (SPEC F7mdrDA4djd3byyDbZAv): sales Target collections +
    # their fat child arms get the same row-split as milestones/entries, so a
    # busy sales project's opportunities/communications no longer bloat the
    # single projects row. Declared here (static, for create_mysql_tables); the
    # authoritative "which collections/arms decompose" registry lives in
    # occupation.TARGET_DECOMPOSITION and a drift test pins ENTITIES ⊇ it.
    "opportunities",
    "accounts",
    "activities",
    "communications",
    "nurturings",
    # ms-115 e-3786: 顧客獲得ターゲット (取引先の無い獲得・準備作業の器)。arms=() で
    # 子テーブルは増やさず、Target 行だけを独立テーブルに持つ。起動時 create_mysql_tables
    # が CREATE TABLE IF NOT EXISTS で作る (schema が DDL を追い越さない)。
    "acquisitions",
    # treks/{trek_id}/* subcollections
    # firestore は treks/{tid}/logs に永続化する (ms-97 e-2603)。dynamodb_client は
    # ここを in-memory fallback で握っていて再起動で消える既知の穴があるが、MySQL は
    # 汎用 (pk, sk, data) テーブルで正しく永続化できるので trek_logs を実体化する。
    "trek_logs",
    # projects/{pid}/decision_events (ms-90 e-3242): 意思決定の統一ストリーム。
    # pk=project_id, sk=decision_id。append-only。
    "decision_events",
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
    # ms-151 / e-5474: machine key subcollection (sk=key_id)。delete_project の
    # cascade 対象 (project を消したら鍵も消える = orphan な認証経路を残さない)。
    "machine_keys": "key_id",
    # ms-95 / e-5477: operation-fires claim (sk="{op_id}_{period}")。
    "operation_fires": "fire_key",
    "sessions": "session_id",
    "session_lookup": "lookup_key",
    "session_logs": "session_id",
    "operation_envelopes": "envelope_id",
    "retros": "week",
    # ms-96 v3: milestones / entries も project 配下 subcollection。
    # delete_project の cascade を成立させるため必ずこの map に入れる。
    "milestones": "milestone_id",
    "entries": "entry_composite_sk",  # sk = "{ms_id}#{entry_id}"
    # ms-109 e-3591: sales Target rows (sk = target_id) + arm-qualified child
    # rows (sk = "{target_id}#{arm}#{child_id}"). delete_project cascade walks
    # these keys, same as milestones/entries.
    "opportunities": "opportunity_id",
    "accounts": "account_id",
    "activities": "target_child_composite_sk",
    "communications": "target_child_composite_sk",
    "nurturings": "target_child_composite_sk",
    # ms-90 e-3242: project 配下 subcollection。delete_project の cascade 対象。
    "decision_events": "decision_id",
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
#
# ★ 並行性 (ms-96 cutover 後の本番インシデント): PyMySQL の Connection は
#   スレッド安全でない。uvicorn は sync エンドポイントを anyio のスレッドプール
#   (デフォルト上限 40) で並行実行するため、1 本のグローバル接続を共有すると、
#   あるスレッドがソケットを読んでいる最中に別スレッドが操作して socket が None に
#   なり `'NoneType' object has no attribute 'settimeout'` でハング/500 する。
#   → 接続を **thread-local** にして各スレッド専用の Connection を持たせる。
#   スレッドは使い回されるので接続数はプール上限 (≒40) に収まり、毎回張り直さない。
_LOCAL = threading.local()

# ms-96 / e-3052 — 総 DB 接続数の観測。PR #349 で thread-local 化して並行破損
# (`'NoneType'...settimeout`) を消したが、これが再発 (= 誰かが module-global 共有に
# 戻す等) したら気付けるようにする。live 接続を thread ident でレジストリに載せ、
# `connection_stats()` で「今この process が保持している接続数」と「累計 open 数」を
# 公開する。thread は使い回されるので live 数は anyio スレッドプール上限 (≒40) に
# 収まるのが健全 = これを大きく超えたら並行/リーク異常のシグナル。
_CONN_LOCK = threading.Lock()
_LIVE_CONNS: dict[int, object] = {}
_OPENED_TOTAL = 0


def _register_conn(conn: object) -> None:
    """このスレッド専用接続をレジストリに登録し、累計 open 数を増やす。"""
    global _OPENED_TOTAL
    with _CONN_LOCK:
        _LIVE_CONNS[threading.get_ident()] = conn
        _OPENED_TOTAL += 1


def connection_stats() -> dict:
    """DB 接続の観測値を返す (ms-96 / e-3052).

    - ``live_connections``: 今この process が保持している thread-local 接続数
      (= 概ね稼働スレッド数、健全なら anyio プール上限 ≒40 以内)。
    - ``opened_total``: プロセス起動以降に張った接続の累計 (= 再接続が多いほど増える)。
    - ``backend``: "mysql" (= 呼び出し側が backend を判別できるように)。

    pymysql 不在 (= MySQL backend 未使用) でも例外を投げず 0 を返す (fail-safe)。
    """
    with _CONN_LOCK:
        return {
            "backend": "mysql",
            "live_connections": len(_LIVE_CONNS),
            "opened_total": _OPENED_TOTAL,
        }


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
    if pymysql is None:
        raise RuntimeError("pymysql is not installed (= MySQL backend cannot be used)")
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        conn = _connect()
        _LOCAL.conn = conn
        _register_conn(conn)  # ms-96 / e-3052: 接続数レジストリに登録
        return conn
    try:
        conn.ping(reconnect=True)
    except Exception:
        # ping ごと死んでいたら張り直す (= server 再起動 / 長時間アイドル対策)。
        # 壊れた接続は捨てて、このスレッド専用に新しく張り直す。
        try:
            conn.close()
        except Exception:
            pass
        conn = _connect()
        _LOCAL.conn = conn
        _register_conn(conn)  # ms-96 / e-3052: 張り直しも累計 open として記録
    return conn


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


def _scan(entity: str, id_field: str | None = None) -> list[dict]:
    """Whole-table scan (= list_all_projects / list_users 用)。

    ms-96: ``id_field`` を渡すと、各行の PK カラムを ``data[id_field]``
    に stamp して返す。これは Firestore → MySQL 移行行が抱える silent なデータ
    欠落を塞ぐための parity 回復。

    背景: Firestore は id を doc.id として保持し ``data`` blob には含めない
    (``firestore_client.save_project`` は ``.set(data)`` で id を data に入れない)。
    そのため移行行の ``data`` には ``project_id`` フィールドが無く、
    ``list_projects`` が ``item.get("project_id")`` で読むと **空文字列** になる。
    下流の cross-project directory (``/api/me/sessions``) は ``if not pid: continue``
    で空 id の project を丸ごと落とすため、移行済 project がセッション directory・
    dm 宛先ピッカー・**disclosure 境界の真値** (``list_projects``) から不可視化する。
    ``firestore_client.list_projects`` は ``doc.id`` を使うので、こちらも PK を
    真値にして parity を回復する。

    MySQL-native の write (``save_project``) は既に ``data`` へ id を注入するので、
    PK == ``data[id_field]`` となり overwrite しても無害。``id_field=None`` (既定)
    は従来挙動そのまま (= 呼び出し元非互換なし)。

    同種の潜在バグは ``users`` / ``treks`` / ``organizations`` の scan にもあるが
    (移行行があれば id が空になる)、本 fix は確認済みの ``projects`` 経路に絞る。
    それらは各 id フィールド名を渡して同様に opt-in できる。
    """
    conn = _conn()
    with conn.cursor() as cur:
        cols = "pk, data" if id_field else "data"
        cur.execute(f"SELECT {cols} FROM `{_table_name(entity)}`")
        rows = cur.fetchall()
    out: list[dict] = []
    for r in rows:
        d = json.loads(r["data"])
        if id_field:
            # PK を真値として stamp (移行行は data に id を持たない)。
            d[id_field] = r["pk"]
        out.append(d)
    return out


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


# 2026-08-20 本番停止の修正。心拍 (PUT /api/projects/{pid}/sessions/{sid}) は
# _load_meta_only → operations.load_project_meta_only → get_project と辿り、
# owner / members を読むためだけに ~8MB の JSON blob を SELECT + parse していた
# (認可判定 server/app.py:_get_role が見るのはこの 2 フィールドのみ)。CPython は
# 断片化した arena を OS に返さないため RSS が cgroup 上限まで上がり続け、ホストが
# swap を食い潰して 502 に至った。同症状の ms-98 (e-3836) 対策は Firestore の
# milestones subcollection stream を避けるものだけで、文書を 1 行で持つ MySQL
# backend (ms-96 e-2378) では無言で無効だった。これがその MySQL 版。
_PROJECT_HEAVY_PATHS = (
    "$.milestones", "$.pushes", "$.deployments", "$.releases",
    "$.operations", "$.undertakings", "$.worktree_sessions", "$.invitations",
)


def get_project_meta(project_id: str) -> dict | None:
    """重い配列を MySQL 側で落としてから返す project の meta 読み取り。

    whitelist ではなく blacklist (JSON_REMOVE) にしてあるのは、この経路を使う
    他の高頻度 endpoint (bus/unread、カーソル前進、session intent、per-event
    ack) が meta の別フィールドを読んでいても壊さないため。``milestones`` は
    呼び出し側 (load_project_meta_only) が [] に潰す契約なので落として問題ない。
    """
    paths = ", ".join(f"'{_p}'" for _p in _PROJECT_HEAVY_PATHS)
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT JSON_REMOVE(data, {paths}) AS data "
            f"FROM `{_table_name('projects')}` WHERE pk=%s AND sk=%s",
            (project_id, ""),
        )
        row = cur.fetchone()
    if not row or row.get("data") is None:
        return None
    return json.loads(row["data"])


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
    items = _scan("projects", id_field="project_id")
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


# 2026-08-20 / e-5367 — 毎分の tick が読むべき project を SQL 側で絞る。
# 従来 _fire_due_scheduled は list_all_projects → 各 project に get_project を掛け、
# 全 project の文書を丸ごとメモリへ展開していた (本番実測 75 件。コード内のコメント
# 自身が "Scale note: iterates all projects each tick — fine at dogfood scale" と
# 認めていた)。締切も server_tick 付き Operation も持たない project は読む意味が無い。
#
# 判定は「入れ子のどこかに締切キーがあるか」「server_tick 付き Operation があるか」。
# $** は再帰探索なので取りこぼさず、余分に含む方向にだけ誤る (= 発火が黙って止まる
# 事故を作らない安全側)。本番実測で 75 件 → 20 件。
_TICK_CANDIDATE_PATHS = (
    "$**.deadline",                        # 汎用の締切 (task / activity)
    "$**.target_date",                     # dev milestone の legacy 締切
    "$.operations[*].meta.server_tick",    # server-tick を opt-in した Operation
)


def list_tick_candidate_project_ids() -> list[str]:
    """発火対象を持ちうる project の id だけを返す (毎分の tick 用)。"""
    placeholders = ", ".join(["%s"] * len(_TICK_CANDIDATE_PATHS))
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT pk FROM `{_table_name('projects')}` "
            f"WHERE sk=%s AND JSON_CONTAINS_PATH(data, 'one', {placeholders})",
            ("",) + _TICK_CANDIDATE_PATHS,
        )
        rows = cur.fetchall()
    return [r["pk"] for r in rows if r.get("pk")]


def list_all_projects() -> list[dict]:
    items = _scan("projects", id_field="project_id")
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
    # ms-96 v3: milestones / entries も _SUBCOLLECTION_SK_NAMES に登録済なので
    # このループで一緒に消える (= 追加コード不要)。
    for entity in _SUBCOLLECTION_SK_NAMES:
        _delete_all(entity, project_id)
    _delete("projects", project_id)
    return True


# ---------------------------------------------------------------------------
# v3 schema (entry-level split) — ms-96 e-2379
#
# v1 (whole-doc) と v2 (milestone-level subcollection) の中間ではなく、 更に一歩
# 進めた設計 (= iruca3 提案 / kurogin 合意、 2026-07-06 DM `Yh9JnCBfj4aVjd71gjKc`)。
#
# レイアウト:
#   beacon_prod_projects    pk=project_id, sk=''                data={meta のみ, schema_version=3}
#   beacon_prod_milestones  pk=project_id, sk=milestone_id      data={ms meta のみ, entries[] は無し}
#   beacon_prod_entries     pk=project_id, sk="{ms_id}#{entry_id}"   data={entry + 子 entries[] を JSON 内包}
#
# 深さ 2 (task -> 子 commit 最大 16 個) のツリーは entries 行の data 内に subtree JSON
# として内包する (= 子 entry のための追加行を作らない)。 これで task_done / commit
# 追加といった実 write 単位が entries 行 1 行の update に閉じる (= 数 KB / <10ms 想定)。
#
# 経緯: Firestore v2 の milestone-level 分割は 1 doc 1 MiB 上限が動機。 MySQL には
# その制約が無く、 row-level lock も cheap なので entry-level まで踏み込める。 詳細は
# CORE / SPEC doc 未起票 (= 実装 land 後に 「MySQL 側 v3 の設計判断」 として書き起こす予定)。
# ---------------------------------------------------------------------------

# operations.py の SCHEMA_V3_ENTRY と同値。 mysql_client -> operations の import は
# 循環になるので、 定数はここでも 3 を明示 (= 変更時は両方揃える)。
SCHEMA_V3_ENTRY = 3


def _v3_sig(data: dict) -> str:
    """Compute a stable content signature for change detection (= v2 apply の
    _ms_sig と同じ役割)。 JSON stable-dump の md5 で 「変わってないなら書かない」
    判定に使う。 md5 は暗号強度不要な等値比較なので選定 (= 高速 + 短い)。
    """
    import hashlib
    return hashlib.md5(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _v3_entry_sort_key(entry: dict) -> tuple:
    """Order entries within a milestone by (created_at, id).

    Firestore v2 は milestones/{ms}/entries[] を doc 内配列で order を保持していたが、
    v3 は entries 行を独立させたので order を明示的に決める必要がある。 created_at
    がある entry を古い順、 無い entry は末尾。 tie-break は entry_id 文字列比較。
    """
    ts = entry.get("created_at", "") or ""
    eid = entry.get("id", "") or ""
    # tuple 先頭要素: created_at が空なら "￿" で末尾へ寄せる
    return (ts or "￿", eid)


def _v3_assemble(meta: dict, ms_rows: list[tuple[str, dict]],
                 entry_rows: list[tuple[str, dict]]) -> dict:
    """meta + milestone 行 + entry 行 を unified project dict に組み立てる。

    ms_rows:    [(milestone_id, ms_data_without_entries), ...]
    entry_rows: [(sk="{ms_id}#{entry_id}", entry_data_with_children_inline), ...]

    返り値は v1/v2 の hydrate 済 shape と等価 (= op() や load_project_consistent
    が同じ形で扱える)。 meta 側に "milestones" key が残っていたら防御的に落とす。
    """
    # 防御: meta 側の "milestones" は無視 (= 過去 v1 residue や load 上の noise 対策)。
    result = {k: v for k, v in (meta or {}).items() if k != "milestones"}

    # milestone_id で entry_rows を group 化。
    entries_by_ms: dict[str, list[dict]] = {}
    for sk, entry_data in entry_rows:
        ms_id, _, _entry_id = sk.partition("#")
        if not ms_id:
            continue
        entries_by_ms.setdefault(ms_id, []).append(entry_data)

    # ms_rows から milestone を組み立て、entries を注入。
    milestones = []
    for ms_id, ms_data in ms_rows:
        ms_dict = dict(ms_data or {})
        # ms_data 側に "entries" が残っていたら防御的に上書き。
        ms_dict.pop("entries", None)
        ms_children = entries_by_ms.get(ms_id, [])
        ms_children.sort(key=_v3_entry_sort_key)
        ms_dict["entries"] = ms_children
        milestones.append(ms_dict)

    # ms-1, ms-2, ... の数値順 (= migrate script の _ms_sort_key と同じ規則)。
    def _ms_key(ms: dict):
        import re
        mid = str(ms.get("id", ""))
        m = re.match(r"ms-(\d+)$", mid)
        return (0, int(m.group(1))) if m else (1, mid)

    milestones.sort(key=_ms_key)
    result["milestones"] = milestones
    return result


def _v3_decompose(data: dict) -> tuple[dict, dict, dict]:
    """unified project dict を (meta, ms_map, entry_map) に分解する。

    meta:      milestones[] を除いた project meta (schema_version=3 を stamp)。
    ms_map:    {milestone_id: milestone_dict_without_entries}
    entry_map: {"{ms_id}#{entry_id}": entry_dict_with_children_inline}

    子 entry (= 深さ 2 の commit 等) は親 entry の "entries" field 内に JSON として
    残す (= 独立行を作らない、 iruca3 提案の要点)。 これにより write 単位が親 entry 行
    1 行の update に閉じる。
    """
    meta = {k: v for k, v in (data or {}).items() if k != "milestones"}
    meta["schema_version"] = SCHEMA_V3_ENTRY

    ms_map: dict = {}
    entry_map: dict = {}
    for ms in (data or {}).get("milestones", []) or []:
        ms_id = ms.get("id", "")
        if not ms_id:
            continue
        # milestone 行には entries[] を含めない (= 実 write 単位から切り離す)。
        ms_meta = {k: v for k, v in ms.items() if k != "entries"}
        ms_map[ms_id] = ms_meta
        for entry in ms.get("entries", []) or []:
            entry_id = entry.get("id", "")
            if not entry_id:
                continue
            sk = f"{ms_id}#{entry_id}"
            # entry data は子 entries[] を含めたまま (= subtree JSON 内包)。
            entry_map[sk] = dict(entry)
    return meta, ms_map, entry_map


# ---------------------------------------------------------------------------
# Generic registry-driven decomposition (ms-109 e-3591 / SPEC F7mdrDA4djd3byyDbZAv)
#
# The milestone-specific _v3_decompose / _v3_assemble above are the DEVELOPMENT
# instance of a general pattern: split a Target collection's fat arms into child
# rows. These generic forms read ``occupation.TARGET_DECOMPOSITION`` so the SAME
# code decomposes development milestones AND sales opportunities/accounts,
# satisfying SPEC AC2 ("dev も同機構の1インスタンス"). They reproduce the
# milestone split byte-for-byte (pinned by test) and round-trip sales Targets.
#
# NOT yet wired into the live atomic I/O path (apply/get/save_project_v3): that
# switchover needs the child tables added to ENTITIES + a MySQL integration test
# (Phase 2d harness) before it can touch the production write path. These pure
# functions are the proven core that switchover will adopt.
# ---------------------------------------------------------------------------

# Collections that assemble emits even when empty. Only "milestones" — it is the
# one Target collection core.validate_project requires as a top-level key, so a
# sales project (milestones: []) still validates. Kept minimal on purpose.
_ALWAYS_EMIT_COLLECTIONS = {"milestones"}


def _target_sort_key(collection: str, target: dict):
    """Sort key for a Target within its collection. Milestones keep the numeric
    ms-N order (matching _v3_assemble); other collections order by created_at
    then id (stable, deterministic). Keys are only ever compared within one
    collection, so the per-collection tuple shapes never mix."""
    import re  # noqa: PLC0415
    tid = str(target.get("id", ""))
    if collection == "milestones":
        m = re.match(r"ms-(\d+)$", tid)
        return (0, int(m.group(1)), "") if m else (1, 0, tid)
    return (0, target.get("created_at", "") or "￿", tid)


def decompose_project_targets(data: dict) -> tuple[dict, dict, dict]:
    """Split a unified project dict into ``(meta, target_maps, child_maps)``,
    registry-driven across occupations.

    - ``meta``: everything except the Target collections, stamped
      ``schema_version=3``.
    - ``target_maps``: ``{collection: {target_id: target_row}}`` where
      target_row is the Target minus its fat arms (bounded arms stay inline).
    - ``child_maps``: ``{arm_table: {sk: child_dict}}`` — one entry per fat-arm
      item, ``sk`` per the D2 rule (2-seg for single-arm collections like
      milestones, 3-seg ``{tid}#{arm}#{cid}`` otherwise). Children nested inside
      a fat-arm item stay inline in that item's dict (mirrors a dev commit nested
      in its task's entry row)."""
    import occupation  # noqa: PLC0415
    colls = occupation.TARGET_DECOMPOSITION
    meta = {k: v for k, v in (data or {}).items() if k not in colls}
    meta["schema_version"] = SCHEMA_V3_ENTRY
    target_maps: dict = {}
    child_maps: dict = {}
    for coll, spec in colls.items():
        arms = spec["arms"]
        single = len(arms) == 1
        id_field = spec.get("id_field", "id")
        tmap: dict = {}
        for target in (data or {}).get(coll, []) or []:
            tid = target.get(id_field, "")
            if not tid:
                continue
            tmap[tid] = {k: v for k, v in target.items() if k not in arms}
            for arm in arms:
                for child in target.get(arm, []) or []:
                    cid = child.get("id", "")
                    if not cid:
                        continue
                    sk = f"{tid}#{cid}" if single else f"{tid}#{arm}#{cid}"
                    child_maps.setdefault(arm, {})[sk] = dict(child)
        target_maps[coll] = tmap
    return meta, target_maps, child_maps


def assemble_project_targets(meta: dict, target_maps: dict,
                             child_maps: dict) -> dict:
    """Inverse of ``decompose_project_targets``: rebuild the unified project
    dict. Reattaches each Target's fat arms from ``child_maps`` (grouping by
    target id + arm parsed from the sk), reproducing the nested shape CLI/UI
    expect. Arm children are ordered by ``_v3_entry_sort_key``; Target
    collections by ``_target_sort_key``."""
    import occupation  # noqa: PLC0415
    colls = occupation.TARGET_DECOMPOSITION
    result = {k: v for k, v in (meta or {}).items() if k not in colls}
    for coll, spec in colls.items():
        arms = spec["arms"]
        single = len(arms) == 1
        by_target_arm: dict = {}
        for arm in arms:
            for sk, child in (child_maps.get(arm, {}) or {}).items():
                if single:
                    tid, _, _cid = sk.partition("#")
                    a = arm
                else:
                    parts = sk.split("#", 2)
                    if len(parts) != 3:
                        continue
                    tid, a, _cid = parts
                by_target_arm.setdefault((tid, a), []).append(child)
        targets = []
        for tid, t_meta in (target_maps.get(coll, {}) or {}).items():
            t = {k: v for k, v in t_meta.items() if k not in arms}
            for arm in arms:
                kids = sorted(by_target_arm.get((tid, arm), []),
                              key=_v3_entry_sort_key)
                t[arm] = kids
            targets.append(t)
        targets.sort(key=lambda x, _c=coll: _target_sort_key(_c, x))
        if targets:
            result[coll] = targets
        elif meta and meta.get(coll):
            # ms-109 e-3591 read-through fallback: this project has NOT been
            # migrated yet — its collection is still stored inline in the
            # projects meta and no child rows exist. Read it through so no data
            # is lost during the rollout window; the next write (decompose reads
            # data.get(coll)) splits it into rows and strips it from meta
            # (write-through migration). Once migrated, target_maps is non-empty
            # and wins, so a stale inline copy is never preferred.
            result[coll] = meta[coll]
        elif coll in _ALWAYS_EMIT_COLLECTIONS:
            # "milestones" must be present — core.validate_project requires it.
            # Other empty collections are omitted so a development project's
            # hydrated shape is byte-for-byte the milestone-specific output
            # (sales code reads with .get/.setdefault, so absence == []).
            result[coll] = targets
    return result


# ms-109 e-3591: change-detection diff helpers for the generic write paths.

def _diff_map(old_map: dict, new_map: dict) -> tuple[dict, list]:
    """Given the current and new {key: row} maps for one table, return
    (upserts, deletes): upserts is the subset of new rows whose content changed
    (or is new), deletes is the keys present before but gone now. Uses _v3_sig
    so unchanged rows are skipped (= targeted writes, no full-table rewrite)."""
    upserts = {k: v for k, v in new_map.items()
               if k not in old_map or _v3_sig(old_map[k]) != _v3_sig(v)}
    deletes = [k for k in old_map if k not in new_map]
    return upserts, deletes


def _v3_plan_writes(before_by_table: dict, new_data: dict) -> tuple[dict, dict, dict]:
    """Pure planner for the atomic write path: given the current rows per table
    (``{table: {sk: data}}``) and the new unified project dict, return
    ``(meta, upserts_by_table, deletes_by_table)``. Occupation-agnostic — it
    decomposes ``new_data`` via the registry and diffs every Target collection
    table + child table. The ``projects`` meta is returned separately (always
    upserted as the transaction anchor). Fully unit-testable without a DB."""
    import occupation  # noqa: PLC0415
    meta, target_maps, child_maps = decompose_project_targets(new_data)
    upserts: dict = {}
    deletes: dict = {}
    for coll in occupation.TARGET_DECOMPOSITION:
        up, dl = _diff_map(before_by_table.get(coll, {}), target_maps.get(coll, {}))
        if up:
            upserts[coll] = up
        if dl:
            deletes[coll] = dl
    for table in occupation.target_child_tables():
        up, dl = _diff_map(before_by_table.get(table, {}), child_maps.get(table, {}))
        if up:
            upserts[table] = up
        if dl:
            deletes[table] = dl
    return meta, upserts, deletes


def _v3_read_target_state(project_id: str) -> tuple | None:
    """Read the projects meta + every Target collection row + child row into
    ``(meta, target_maps, child_maps)`` via the row primitives (each table's
    rows keyed by sk). Returns None when the project meta is absent."""
    import occupation  # noqa: PLC0415
    meta = _get("projects", project_id)
    if meta is None:
        return None
    target_maps = {coll: {sk: d for sk, d in _query_rows(coll, project_id)}
                   for coll in occupation.TARGET_DECOMPOSITION}
    child_maps = {table: {sk: d for sk, d in _query_rows(table, project_id)}
                  for table in occupation.target_child_tables()}
    return meta, target_maps, child_maps


def get_project_v3(project_id: str) -> dict | None:
    """v3 project を read して unified dict shape で返す。

    v1/v2 の get_project(pid) と異なり、 Target collection (milestones /
    opportunities / accounts) とその子 (entries / activities / communications /
    nurturings) を hydrate して 「1 project = 1 大きな dict」 の shape に戻す
    (ms-109 e-3591 で registry 駆動に一般化)。 apply_operation の read 側や
    load_project_consistent (= operations.py) から使う。 見つからなければ None。
    """
    state = _v3_read_target_state(project_id)
    if state is None:
        return None
    meta, target_maps, child_maps = state
    return assemble_project_targets(meta, target_maps, child_maps)


def save_project_v3(project_id: str, data: dict) -> None:
    """v3 shape で project 全体を書き込む (= migration script や explicit save 用)。

    replace_project_v3 と違い、 既存行との diff を取らず まっさら書き込みを想定
    (= migration 中の初回 insert / test fixture 準備等)。 transaction も張らない
    (= 呼び出し側が保証)。 通常運用の書き込みは apply_project_op_v3 or
    replace_project_v3 を使うこと。 ms-109 e-3591 で registry 駆動に一般化
    (Target collection + 子テーブルを一律 upsert)。
    """
    meta, target_maps, child_maps = decompose_project_targets(data)
    _put("projects", project_id, {**meta, "project_id": project_id})
    for coll, tmap in target_maps.items():
        for tid, tdata in tmap.items():
            _put(coll, project_id, tdata, sk=tid)
    for table, cmap in child_maps.items():
        for sk, cdata in cmap.items():
            _put(table, project_id, cdata, sk=sk)


def apply_project_op_v3(project_id: str, op) -> "any":  # type: ignore[valid-type]
    """v3 project に対して op(data) -> (new_data, result) を atomic に適用。

    Firestore transaction 相当を MySQL row-level lock で実現する:
      1. autocommit を off にして BEGIN
      2. projects[pid] 行を SELECT ... FOR UPDATE で pessimistic lock (= meta 行に
         書き込みを serialize するアンカー)
      3. milestones / entries を read (= 同じ transaction 内なので snapshot)
      4. op(hydrated_data) -> (new_data, result)
      5. 前後の sig で milestone / entry の diff を検出し、 変わった行のみ upsert、
         消えた ID の行のみ delete
      6. COMMIT (or 例外時 ROLLBACK)、 autocommit を戻す

    Firestore との差:
      - Firestore は「read → op → conflict なら 5 回まで retry」 という抽象化 だが、
        MySQL は 「SELECT ... FOR UPDATE で 他 writer を block」 する pessimistic。
        op() が pure でなくても呼ばれるのは 1 回のみ (= 副作用のある op でも安全)。
      - op() の contract (= pure / side-effect free) は v1/v2 と同じ要求で維持する
        (= 後日 optimistic 化した時に壊れないため)。
    """
    import occupation  # noqa: PLC0415
    target_colls = tuple(occupation.TARGET_DECOMPOSITION.keys())
    child_tables = occupation.target_child_tables()
    conn = _conn()
    conn.autocommit(False)
    try:
        before_by_table: dict = {}
        with conn.cursor() as cur:
            # 1. meta 行を lock (= 存在しない project は LookupError)
            cur.execute(
                f"SELECT data FROM `{_table_name('projects')}` "
                f"WHERE pk=%s AND sk='' FOR UPDATE",
                (project_id,),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError(f"Project '{project_id}' not found")
            meta = json.loads(row["data"])

            # 2. 全 Target collection + 子テーブルを同 transaction 内で read
            #    (ms-109 e-3591: milestones/entries 固定でなく registry 駆動)。
            for t in (*target_colls, *child_tables):
                cur.execute(
                    f"SELECT sk, data FROM `{_table_name(t)}` WHERE pk=%s",
                    (project_id,),
                )
                before_by_table[t] = {
                    r["sk"]: json.loads(r["data"]) for r in cur.fetchall()
                }

        # 3. hydrate (= registry 駆動で再合成)。
        target_maps = {c: before_by_table.get(c, {}) for c in target_colls}
        child_maps = {t: before_by_table.get(t, {}) for t in child_tables}
        data = assemble_project_targets(meta, target_maps, child_maps)

        # 4. op を実行 (= v1/v2 と同じ contract)。
        new_data, result = op(data)

        # 5. validate (= transaction 内で invariant を確認、v1/v2 と同じ)。
        import core  # noqa: PLC0415
        core.validate_project(new_data)

        # 6. pure planner で targeted writes を算出 (テスト可能な純関数)。
        new_meta, upserts, deletes = _v3_plan_writes(before_by_table, new_data)
        new_meta["project_id"] = project_id

        with conn.cursor() as cur:
            # meta 行を書き戻し (= Target collection を除いた slim shape、anchor)。
            cur.execute(
                f"INSERT INTO `{_table_name('projects')}` (pk, sk, data) "
                f"VALUES (%s, %s, %s) "
                f"ON DUPLICATE KEY UPDATE data=VALUES(data)",
                (project_id, "", _dumps(new_meta)),
            )
            # 変わった行のみ upsert (target row は sk=target_id、child は composite sk)。
            for table, up in upserts.items():
                for key, d in up.items():
                    cur.execute(
                        f"INSERT INTO `{_table_name(table)}` (pk, sk, data) "
                        f"VALUES (%s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE data=VALUES(data)",
                        (project_id, key, _dumps(d)),
                    )
            # 消えた行のみ delete。
            for table, dl in deletes.items():
                for key in dl:
                    cur.execute(
                        f"DELETE FROM `{_table_name(table)}` "
                        f"WHERE pk=%s AND sk=%s",
                        (project_id, key),
                    )

        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit(True)


def replace_project_v3(project_id: str, new_data: dict) -> None:
    """v3 project を whole-replace する。

    apply_project_op_v3 と違って op() を挟まず、 caller が最終 shape を持っている
    ケース (= admin write / cloud push / migration) 用。 meta + milestone + entry
    に分解して upsert + 既存 - 新規 の行を DELETE。 diff sig は取らず全行を書き直す
    (= caller が「これで置換」意図なので targeted 化のメリットが薄い)。

    Firestore v2 の _replace_cloud_v2 と同じ semantics だが、 transaction 境界と
    lock は MySQL 流 (= SELECT ... FOR UPDATE + BEGIN/COMMIT)。
    """
    conn = _conn()
    conn.autocommit(False)
    try:
        with conn.cursor() as cur:
            # meta 行を lock (= 存在しなくても新規作成する、 upsert 想定)。
            cur.execute(
                f"SELECT sk FROM `{_table_name('projects')}` "
                f"WHERE pk=%s AND sk='' FOR UPDATE",
                (project_id,),
            )
            _ = cur.fetchone()

            # 既存 Target collection + 子行の sk を retrieve (= 削除対象算出用)。
            # ms-109 e-3591: milestones/entries 固定でなく registry 駆動。
            import occupation  # noqa: PLC0415
            all_tables = (*occupation.TARGET_DECOMPOSITION.keys(),
                          *occupation.target_child_tables())
            existing_sks: dict = {}
            for t in all_tables:
                cur.execute(
                    f"SELECT sk FROM `{_table_name(t)}` WHERE pk=%s",
                    (project_id,),
                )
                existing_sks[t] = {r["sk"] for r in cur.fetchall()}

            new_meta, target_maps, child_maps = decompose_project_targets(new_data)
            new_meta["project_id"] = project_id

            cur.execute(
                f"INSERT INTO `{_table_name('projects')}` (pk, sk, data) "
                f"VALUES (%s, %s, %s) "
                f"ON DUPLICATE KEY UPDATE data=VALUES(data)",
                (project_id, "", _dumps(new_meta)),
            )

            # 新行を upsert / 消えた行を delete (全 table 一律)。target row は
            # sk=target_id、child は composite sk。
            new_rows_by_table: dict = {
                coll: target_maps.get(coll, {})
                for coll in occupation.TARGET_DECOMPOSITION
            }
            for table in occupation.target_child_tables():
                new_rows_by_table[table] = child_maps.get(table, {})
            for table, rows in new_rows_by_table.items():
                for key, d in rows.items():
                    cur.execute(
                        f"INSERT INTO `{_table_name(table)}` (pk, sk, data) "
                        f"VALUES (%s, %s, %s) "
                        f"ON DUPLICATE KEY UPDATE data=VALUES(data)",
                        (project_id, key, _dumps(d)),
                    )
                for key in existing_sks.get(table, set()) - set(rows.keys()):
                    cur.execute(
                        f"DELETE FROM `{_table_name(table)}` "
                        f"WHERE pk=%s AND sk=%s",
                        (project_id, key),
                    )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit(True)


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
    # treks と同じ移行由来の id 欠落 (実測 5 件)。同上。
    return _scan("users", id_field="user_id")


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
    items = [it for it in _scan("users", id_field="user_id")
             if it.get("email") == email]
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
    # _query_rows で (sk, data) を取る。sk が doc_id の真値。Firestore→MySQL 移行
    # (ms-96) で入った古い doc は payload (data JSON) に doc_id フィールドを持たず、
    # doc_id は sk 列にしか無い。以前は _query (SELECT data のみ、sk を返さない) を
    # 使い data.get("doc_id","") で拾っていたため、移行済み doc 全ての doc_id が空に
    # なり `beacon doc show` が効かなくなっていた (= 過去ドキュメントが読めない)。
    # sk を doc_id の fallback にすることで全 doc が再び addressable になる。
    rows = _query_rows("documents", project_id)
    result = []
    for sk, data in rows:
        if data.get("deleted"):
            continue
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        operation = data.get("operation") or _extract_frontmatter_field(
            data.get("content", ""), "operation"
        )
        # ms-109 e-3754 — surface the canonical target-class-agnostic linkage
        # (``acc-1`` / ``opp-3`` / …) so ``doc list --account`` etc. work in
        # cloud mode. ``trek_id`` is extracted alongside for the same reason
        # (it had been omitted here, so the tolerant fallback was cloud-blind).
        target = data.get("target") or _extract_frontmatter_field(
            data.get("content", ""), "target"
        )
        trek_id = data.get("trek_id") or _extract_frontmatter_field(
            data.get("content", ""), "trek_id"
        )
        entry = {
            "doc_id": data.get("doc_id") or sk,
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
        if operation:
            entry["operation"] = operation
        if target:
            entry["target"] = target
        if trek_id:
            entry["trek_id"] = trek_id
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
    # 2026-08-20 / e-5370 — 以前は _query_rows で project の全 changelog を Python へ
    # 読み込んでから since / 並べ替え / limit を適用していた。docstring 自身が
    # incremental polling を謳う経路なのに、毎回の polling が全件 json.loads を
    # 伴っていた (本番 9,135 行 / 4.5MB)。同日 list_bus_events で本番を落とした
    # のと同じ形。規模テスト (tests/test_scale_contract_mysql.py) が検出した 3 例目。
    limit = max(1, min(500, int(limit)))
    ts_expr = "JSON_UNQUOTE(JSON_EXTRACT(data, '$.ts'))"
    where = ["pk=%s"]
    params: list = [project_id]
    if since:
        where.append(f"{ts_expr} > %s")
        params.append(since)
    params.append(int(limit))
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT sk, data FROM `{_table_name('changelog')}` "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {ts_expr} DESC LIMIT %s",
            tuple(params),
        )
        fetched = cur.fetchall()
    rows = []
    for r in fetched:
        entry = dict(json.loads(r["data"]))
        entry["id"] = r["sk"]
        rows.append(entry)
    return rows


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


def find_bus_event_by_client_id(project_id: str, client_event_id: str,
                                channel: str = "") -> dict | None:
    """再送の重複チェック用に、client_event_id で 1 件だけ引く (e-5369)。

    2026-08-20 の調査で判明した実バグの修正。従来 server/app.py の
    _find_bus_event_by_client_id は list_bus_events(limit=100) を since 無しで
    呼んでいたが、この関数の契約は「古い順に先頭 limit 件」なので、返っていたのは
    最新 100 件ではなく **最古 100 件** だった。本番 beacon-b95643 では最古 100 件に
    含まれる最新の created_at が 2026-06-09、実際の最新は 2026-08-20 で、2 ヶ月半に
    わたり重複チェックが一度も機能していなかった (client_event_id 付きイベントは
    483 件あり、機能自体は使われている)。ms-140 / ms-141 が誤送信 event
    1786254891861-3OOjSB を発端に「二重送信を防ぐ」ために立てた MS の中核部分。

    窓を推測して走査するのではなく名指しで引く。sk (= event_id) は ms 精度の
    タイムスタンプ接頭辞で PRIMARY KEY (pk, sk) の一部なので、ORDER BY sk DESC が
    そのまま「新しい順」になり索引が効く。
    """
    if not client_event_id:
        return None
    where = ["pk=%s",
             "JSON_UNQUOTE(JSON_EXTRACT(data, '$.client_event_id')) = %s"]
    params: list = [project_id, client_event_id]
    if channel:
        where.append("JSON_UNQUOTE(JSON_EXTRACT(data, '$.channel')) = %s")
        params.append(channel)
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT data FROM `{_table_name('bus_events')}` "
            f"WHERE {' AND '.join(where)} ORDER BY sk DESC LIMIT 1",
            tuple(params),
        )
        row = cur.fetchone()
    if not row or row.get("data") is None:
        return None
    return json.loads(row["data"])


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    """List bus events ordered by created_at.

    契約 (firestore / dynamodb 版と同一):
      - since: created_at > since の event のみ返す
      - channel: equality filter
      - limit: 結果件数の上限
    """
    # 2026-08-20 本番停止の主因。以前はここで _query が project の bus_events を
    # **全件** Python に読み込み、そのあとで since / channel / limit を適用していた。
    # beacon-b95643 は 98,943 件 / 72MB あり、bus ポーリング (実測 毎秒約1回) の
    # たびに全件 json.loads するため 1 回で数百 MB を確保していた。CPython は断片化
    # した arena を OS に返さないので RSS が cgroup 上限に張り付き、OOM ループに
    # 陥った。since はまさに「新着だけ取る」ためのカーソルなので、絞り込み・整列・
    # 件数制限を SQL に押し下げる。MySQL は pk で絞った範囲を C で走査し、Python に
    # 渡るのは limit 件だけになる。返り値の形は _query と等価 (stamp 等はしない)。
    where = ["pk=%s"]
    params: list = [project_id]
    if since:
        where.append("JSON_UNQUOTE(JSON_EXTRACT(data, '$.created_at')) > %s")
        params.append(since)
    if channel:
        where.append("JSON_UNQUOTE(JSON_EXTRACT(data, '$.channel')) = %s")
        params.append(channel)
    sql = (
        f"SELECT data FROM `{_table_name('bus_events')}` "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY JSON_UNQUOTE(JSON_EXTRACT(data, '$.created_at'))"
    )
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [json.loads(r["data"]) for r in rows]


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


def list_decided_approvals(project_id: str, *, limit: int = 50) -> list[dict]:
    """List sidecar rows in approval_status in {"approved","denied"} (= ms-70 / e-1718).

    MySQL mirror of :func:`firestore_client.list_decided_approvals` and
    :func:`dynamodb_client.list_decided_approvals`. Same contract: pending /
    auto excluded, newest-first by ``decision_at`` (fallback ``created_at``),
    cap at ``limit``. Backs the Web UI "DM 承認履歴" (DM approval history)
    audit view via ``GET`` in app.py.

    This method was missing from the MySQL backend after the ms-96 VPS
    re-platform (= 移植漏れ), so the audit endpoint 500'd in production once
    MySQL became the store. Implemented here to restore parity with the
    Firestore / DynamoDB backends.
    """
    rows = []
    for it in _query("bus_event_approvals", project_id):
        if it.get("approval_status") not in ("approved", "denied"):
            continue
        rows.append({k: v for k, v in it.items() if k != "project_id"})
    rows.sort(
        key=lambda r: r.get("decision_at") or r.get("created_at") or "",
        reverse=True,
    )
    if limit:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Machine API keys (ms-151 / e-5474). PK=project_id, SK=key_id.
# ---------------------------------------------------------------------------
# headless machine 認証の鍵を project 配下に持つ。record は machine_key.build_record
# が組み立てた secret_hash-only 形。verify は token 由来の (project_id, key_id) で
# get_machine_key を直接引く (scan 不要)。

def save_machine_key(project_id: str, record: dict) -> dict:
    """発行済み machine key レコードを保存する (key_id で upsert)。"""
    item = {**record, "project_id": project_id}
    _put("machine_keys", project_id, item, sk=record["key_id"])
    return record


def get_machine_key(project_id: str, key_id: str) -> dict | None:
    """(project_id, key_id) の key レコードを返す。無ければ None。

    ``project_id`` を **落とさない** (bus_event_approvals とは異なる)。machine_key
    .verify_token が別 project すり替え検知にこの field を読むため surface に残す。
    """
    item = _get("machine_keys", project_id, sk=key_id)
    if not item:
        return None
    return dict(item)


def list_machine_keys(project_id: str) -> list[dict]:
    """project の全 machine key を新しい順 (created_at 降順) で返す。"""
    rows = [dict(it) for it in _query("machine_keys", project_id)]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def revoke_machine_key(project_id: str, key_id: str,
                       revoked_at: str) -> dict | None:
    """key を失効させる (revoked_at を刻む)。無ければ None を返す。"""
    item = _get("machine_keys", project_id, sk=key_id)
    if not item:
        return None
    # e-5502 AX review A: 冪等。既に失効済みなら最初の revoked_at を保持する。
    if item.get("revoked_at"):
        return dict(item)
    item["revoked_at"] = revoked_at
    _put("machine_keys", project_id, item, sk=key_id)
    return dict(item)


# ---------------------------------------------------------------------------
# Operation-fires claim (PK=project_id, SK="{op_id}_{period}"). ms-95 / e-5477.
# ---------------------------------------------------------------------------
# 定期発火の二重駆動 (Beacon tick + 外部 self-drive) を防ぐ first-write-wins な
# 発火権先取り。firestore の claim_operation_fire_if_new と同契約。period は
# operation_period.period_key が cadence から算出したバケット文字列。

def claim_operation_fire_if_new(project_id: str, op_id: str, period: str,
                                session_id: str) -> dict:
    """"この project で op_id を period に最初に発火する" を原子的に主張する。

    最初の caller は行を作り ``{"claimed": True, ...}`` を返す。以後の caller は
    既存行を見て ``{"claimed": False, "claimed_by": <first>, ...}`` を返す。
    原子性は ``_insert_if_absent`` (PK 重複=IntegrityError→False) が担う。
    """
    fire_key = f"{op_id}_{period}"
    record = {
        "project_id": project_id,
        "op_id": op_id,
        "period": period,
        "session_id": session_id,
        "claimed_at": _now_iso_utc(),
    }
    created = _insert_if_absent("operation_fires", project_id, record, sk=fire_key)
    if created:
        return {"claimed": True, "claimed_by": session_id,
                "claimed_at": record["claimed_at"]}
    existing = _get("operation_fires", project_id, sk=fire_key) or {}
    return {
        "claimed": False,
        "claimed_by": existing.get("session_id", ""),
        "claimed_at": existing.get("claimed_at", ""),
    }


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
    # 2026-08-20 本番停止の直接原因。_scan の docstring が「同種の潜在バグは
    # users / treks / organizations にもある」と予告していたそのもの。Firestore
    # から移行した trek 行は data に trek_id を持たないため、ここが空文字の
    # trek を返し、tick の escalation が save_trek("") で pk 空文字の行に書き
    # 込み続けた (実測 18 件中 15 件が欠落)。PK を真値として stamp する。
    items = _scan("treks", id_field="trek_id")
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
# Organizations (ms-113 / e-3731) — top-level entity, PK=org_id, sk=''.
# See firestore_client for semantics; MySQL persists via the generic
# (pk, sk, data JSON) row model.
# ---------------------------------------------------------------------------

def get_org(org_id: str) -> dict | None:
    return _get("organizations", org_id)


def save_org(org_id: str, data: dict) -> None:
    item = {**data, "org_id": org_id}
    _put("organizations", org_id, item)


def list_orgs_for_user(user_id: str | None = None) -> list[dict]:
    """List organizations. See firestore_client.list_orgs_for_user semantics."""
    items = _scan("organizations")
    result: list[dict] = []
    for item in items:
        if user_id:
            members = [m.get("user_id") for m in item.get("members", []) or []]
            if user_id not in members:
                continue
        result.append(item)
    result.sort(key=lambda o: (o.get("created_at", ""), o.get("org_id", "")),
                reverse=True)
    return result


def delete_org(org_id: str) -> bool:
    if get_org(org_id) is None:
        return False
    _delete("organizations", org_id)
    return True


# ---------------------------------------------------------------------------
# Master identity store (ms-111 / e-3620) — 汎用プリミティブ。
# 問い合わせロジック (org 束縛での絞り込み・external_ref 逆引き・master 権威の
# 書き込み) は lib/master_store.BeaconDefaultAdapter が持つので、backend は
# entity 単位の raw get/put/scan だけを提供する (= 3 backend で重複させない)。
# pk = master record の canonical id、sk='' の top-level entity。
# ---------------------------------------------------------------------------

def master_get(entity: str, pk: str) -> dict | None:
    return _get(entity, pk)


def master_put(entity: str, pk: str, data: dict) -> None:
    _put(entity, pk, data)


def master_scan(entity: str) -> list[dict]:
    return _scan(entity)


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


# ---------------------------------------------------------------------------
# Decision events (ms-90 e-3242): 意思決定の統一 append-only ストリーム。
# pk=project_id, sk=decision_id。schema builder は server/decision_event.py。
# ---------------------------------------------------------------------------

def _mint_decision_event_id() -> str:
    import secrets as _secrets
    return f"dec-{_secrets.token_hex(8)}"


def append_decision_event(project_id: str, data: dict) -> str:
    """projects/{project_id}/decision_events に 1 件追記する。返り値は decision_id。

    decision_id / created_at は未指定なら補完する。``outcome`` 系の禁止
    フィールドが混入していたら書き込み前に ValueError で弾く (= SPEC 不変条件)。
    """
    try:
        from decision_event import assert_no_outcome
        assert_no_outcome(data or {})
    except ImportError:
        pass
    payload = dict(data or {})
    decision_id = payload.get("decision_id") or _mint_decision_event_id()
    payload["decision_id"] = decision_id
    payload.setdefault("created_at", _now_iso_utc())
    _put("decision_events", project_id, payload, sk=decision_id)
    return decision_id


def list_decision_events(project_id: str, *, limit: int = 100,
                         since: str = "") -> list[dict]:
    """decision_events を created_at 昇順で返す (= 1 本のストリームとして読む)。

    since: ISO8601 の下限 (= created_at > since のみ)。limit: 返却上限。
    """
    rows = _query("decision_events", project_id)
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: (r.get("created_at", ""), r.get("decision_id", "")))
    if limit and limit > 0:
        rows = rows[:limit]
    return rows
