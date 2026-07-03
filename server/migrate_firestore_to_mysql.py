"""Firestore → MySQL データ移行スクリプト (ms-96 e-2379)。

Firestore に入っている Beacon の全データ (projects / users / treks とその
subcollection 群) を読み出し、mysql_client の JSON-blob スキーマ
(entity ごとに (pk, sk, data) の table) へ移す。

3 フェーズを subcommand で分ける:

  export <dump.json>   Firestore を読んで JSON dump に落とす (= Firestore 読み取り
                       権限のある環境で走らせる。ADC + BEACON_GCP_PROJECT_ID 必要)。
  import <dump.json>   dump を読んで MySQL へ書き込む (= VPS 上で走らせる。MySQL は
                       localhost 限定なので import は VPS 内で実行する)。
  verify <dump.json>   dump の件数と MySQL の実件数を entity ごとに突き合わせ、
                       欠損 (= 移行漏れ) が無いか検証する。

なぜ 2 フェーズに割るか: Firestore の読み取り権限 (kurogin さんの GCP project)
と MySQL (VPS localhost) は別マシンに分かれている。JSON dump を中間成果物に
することで、export をローカル / import を VPS と分担でき、dump 自体が
レビュー・再実行可能な監査対象になる。

環境変数:
  export 側:
    BEACON_ENV=prod                     # projects/users (dev は -dev suffix)
    BEACON_GCP_PROJECT_ID=<gcp-project> # Firestore の GCP project
    (ADC: gcloud auth application-default login 済であること)
  import / verify 側:
    BEACON_ENV=prod
    BEACON_STORE_BACKEND=mysql
    MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB  (db.env)

使い方の例:
    # ローカル (Firestore 権限あり)
    BEACON_ENV=prod BEACON_GCP_PROJECT_ID=beacon-b95643 \\
        python migrate_firestore_to_mysql.py export /tmp/beacon-dump.json
    # VPS (MySQL localhost)
    BEACON_ENV=prod BEACON_STORE_BACKEND=mysql \\
        python migrate_firestore_to_mysql.py import /tmp/beacon-dump.json
    BEACON_ENV=prod BEACON_STORE_BACKEND=mysql \\
        python migrate_firestore_to_mysql.py verify /tmp/beacon-dump.json
"""
from __future__ import annotations

import json
import sys


# ---------------------------------------------------------------------------
# Firestore subcollection 名 → mysql entity 名のマップ
# ---------------------------------------------------------------------------
# key   = Firestore 上の subcollection の名前 (= CollectionReference.id)
# value = mysql_client.ENTITIES の entity 名 (= table のもと)
#
# projects/{pid}/* の subcollection に対して使う。ここに載っていない
# subcollection (= operation_fires / members / triggers 等) は
# 「未マップ」として警告し、dump にもコピーしない (= silent 消失を防ぐため
# 必ず標準エラーへ大きく出す)。
_PROJECT_SUBCOL_MAP = {
    "documents": "documents",   # 特別扱い: 配下の document_revisions も辿る
    "retros": "retros",
    "notes": "notes",
    "changelog": "changelog",
    "bus_events": "bus_events",
    "bus_cursors": "bus_cursors",
    "bus_nonces": "bus_nonces",
    "bus_audit": "bus_audit",
    "bus_event_approvals": "bus_event_approvals",
    "sessions": "sessions",
    "session_logs": "session_logs",
    "operation_envelopes": "operation_envelopes",
    "active_claims": "active_claims",
}

# treks/{trek_id}/* の subcollection マップ。firestore は logs という名前で
# treks/{tid}/logs に持つ (= mysql_client では trek_logs table)。
_TREK_SUBCOL_MAP = {
    "logs": "trek_logs",
    "trek_logs": "trek_logs",  # 名前揺れ吸収
}

# users/{uid}/* の subcollection マップ。
_USER_SUBCOL_MAP = {
    "machines": "machines",
    "session_lookup": "session_lookup",
}

# document 配下の revisions subcollection 名 (= firestore_client
# DOC_REVISIONS_SUBCOLLECTION と一致)。document_revisions は
# projects/{pid}/documents/{doc_id}/document_revisions/{rev} と 2 階層ネスト
# しており、mysql では pk=project_id, sk="{doc_id}#{rev:06d}" に畳む
# (= mysql_client.get_document_revision のキー式と一致させる)。
_DOC_REVISIONS_SUBCOL = "document_revisions"


def _rows_bucket(dump: dict, entity: str) -> list:
    return dump.setdefault("entities", {}).setdefault(entity, [])


def _add(dump: dict, entity: str, pk: str, sk: str, data: dict) -> None:
    _rows_bucket(dump, entity).append({"pk": pk, "sk": sk, "data": data})


# ===========================================================================
# export
# ===========================================================================

def do_export(outfile: str) -> None:
    import firestore_client as fs

    db = fs.get_db()
    dump: dict = {"entities": {}, "skipped": []}
    skipped_seen: set = set()

    def _skip(where: str, name: str) -> None:
        key = f"{where}:{name}"
        if key not in skipped_seen:
            skipped_seen.add(key)
            dump["skipped"].append({"parent": where, "subcollection": name})
            print(f"  ⚠ UNMAPPED subcollection {name!r} under {where} — "
                  f"NOT migrated (要手当て)", file=sys.stderr)

    # --- projects (+ subcollections) ---
    print(f"[export] scanning {fs.COLLECTION} …", file=sys.stderr)
    proj_count = 0
    for proj in db.collection(fs.COLLECTION).stream():
        pid = proj.id
        _add(dump, "projects", pid, "", proj.to_dict() or {})
        proj_count += 1
        for subcol in proj.reference.collections():
            name = subcol.id
            if name == "documents":
                # documents 本体 + 各 document の revisions を辿る
                for doc in subcol.stream():
                    _add(dump, "documents", pid, doc.id, doc.to_dict() or {})
                    for rev in doc.reference.collection(_DOC_REVISIONS_SUBCOL).stream():
                        rdata = rev.to_dict() or {}
                        rnum = rdata.get("rev")
                        if rnum is None:
                            _skip(f"documents/{doc.id}", "document_revisions(rev欠落)")
                            continue
                        sk = f"{doc.id}#{int(rnum):06d}"
                        _add(dump, "document_revisions", pid, sk, rdata)
            elif name in _PROJECT_SUBCOL_MAP:
                ent = _PROJECT_SUBCOL_MAP[name]
                for d in subcol.stream():
                    _add(dump, ent, pid, d.id, d.to_dict() or {})
            else:
                _skip(f"projects/{pid}", name)
    print(f"[export]   {proj_count} projects", file=sys.stderr)

    # --- users (+ subcollections) ---
    print(f"[export] scanning {fs.USERS_COLLECTION} …", file=sys.stderr)
    user_count = 0
    for user in db.collection(fs.USERS_COLLECTION).stream():
        uid = user.id
        _add(dump, "users", uid, "", user.to_dict() or {})
        user_count += 1
        for subcol in user.reference.collections():
            name = subcol.id
            if name in _USER_SUBCOL_MAP:
                ent = _USER_SUBCOL_MAP[name]
                for d in subcol.stream():
                    _add(dump, ent, uid, d.id, d.to_dict() or {})
            else:
                _skip(f"users/{uid}", name)
    print(f"[export]   {user_count} users", file=sys.stderr)

    # --- treks (+ logs) ---
    # treks コレクション名は env で -dev suffix が付くことがあるので firestore_client
    # の定数があればそれを使い、無ければ prod/dev を推定する。
    treks_col = getattr(fs, "TREKS_COLLECTION", None) or (
        "treks" if fs._ENV == "prod" else "treks-dev")
    print(f"[export] scanning {treks_col} …", file=sys.stderr)
    trek_count = 0
    for trek in db.collection(treks_col).stream():
        tid = trek.id
        _add(dump, "treks", tid, "", trek.to_dict() or {})
        trek_count += 1
        for subcol in trek.reference.collections():
            name = subcol.id
            if name in _TREK_SUBCOL_MAP:
                ent = _TREK_SUBCOL_MAP[name]
                for d in subcol.stream():
                    _add(dump, ent, tid, d.id, d.to_dict() or {})
            else:
                _skip(f"treks/{tid}", name)
    print(f"[export]   {trek_count} treks", file=sys.stderr)

    with open(outfile, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, default=str, indent=None)

    print("\n[export] summary (entity: rows):", file=sys.stderr)
    for ent, rows in sorted(dump["entities"].items()):
        print(f"  {ent}: {len(rows)}", file=sys.stderr)
    if dump["skipped"]:
        print(f"[export] ⚠ {len(dump['skipped'])} UNMAPPED subcollection(s) skipped "
              f"(上記参照)。", file=sys.stderr)
    print(f"[export] wrote {outfile}", file=sys.stderr)


# ===========================================================================
# import
# ===========================================================================

def do_import(infile: str) -> None:
    import mysql_client as db

    with open(infile, encoding="utf-8") as f:
        dump = json.load(f)

    created = db.create_mysql_tables()
    print(f"[import] tables ensured ({created} newly created)", file=sys.stderr)

    entities = dump.get("entities", {})
    total = 0
    for ent, rows in sorted(entities.items()):
        if ent not in db.ENTITIES:
            print(f"  ⚠ entity {ent!r} は mysql_client.ENTITIES に無い — skip",
                  file=sys.stderr)
            continue
        n = 0
        for row in rows:
            db._put(ent, row["pk"], row["data"], sk=row.get("sk", ""))
            n += 1
        total += n
        print(f"  {ent}: {n}", file=sys.stderr)
    print(f"[import] wrote {total} rows into MySQL", file=sys.stderr)


# ===========================================================================
# verify
# ===========================================================================

def do_verify(infile: str) -> int:
    import mysql_client as db

    with open(infile, encoding="utf-8") as f:
        dump = json.load(f)

    entities = dump.get("entities", {})
    conn = db._conn()
    mismatches = 0
    print("[verify] entity: dump_rows vs mysql_rows", file=sys.stderr)
    for ent, rows in sorted(entities.items()):
        if ent not in db.ENTITIES:
            print(f"  ⚠ {ent}: dump にあるが mysql_client.ENTITIES に無い "
                  f"({len(rows)} rows 未反映)", file=sys.stderr)
            mismatches += 1
            continue
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM `{db._table_name(ent)}`")
            got = cur.fetchone()["c"]
        want = len(rows)
        flag = "OK" if got == want else "MISMATCH"
        if got != want:
            mismatches += 1
        print(f"  {ent}: {want} vs {got}  [{flag}]", file=sys.stderr)

    if mismatches == 0:
        print("[verify] ✅ 全 entity 件数一致 (欠損なし)", file=sys.stderr)
    else:
        print(f"[verify] ⚠ {mismatches} entity で不一致。要調査。", file=sys.stderr)
    return mismatches


# ===========================================================================
# main
# ===========================================================================

def main(argv: list) -> int:
    if len(argv) < 2 or argv[0] not in ("export", "import", "verify"):
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    if cmd == "export":
        do_export(path)
        return 0
    if cmd == "import":
        do_import(path)
        return 0
    if cmd == "verify":
        return 0 if do_verify(path) == 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
