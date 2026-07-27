"""lib/org_store.py — local file-based organization storage (ms-118 / e-4231).

Organization は project をまたぐ top-level tenancy entity (= 1 つの org が複数
project を束ねる) なので、どの ``project.json`` の中にも入れない。**local mode**
では org は 1 org = 1 JSON file として ``~/.beacon/orgs/<org_id>.json`` に住む。
**cloud mode** では CLI は server の HTTP API (``/api/orgs``) 経由で
``server/store_router`` (= firestore_client / mysql_client) に届く (= StoreApi)。

本モジュールは local mode のみを担う (= trek_store と同型の分離)。cloud mode は
StoreApi + api_client が担当する。純関数の org doc 組み立ては lib/org.py。

Test override: ``BEACON_ORGS_DIR`` を tmp dir に向ければ差し替えられる。
"""
from __future__ import annotations

import json
import os


def get_org_dir() -> str:
    """org JSON file を置くディレクトリを返す。

    既定は ``~/.beacon/orgs/``。テストは ``BEACON_ORGS_DIR`` で差し替える。
    ディレクトリは最初の save 時に lazy に作られる。
    """
    custom = os.environ.get("BEACON_ORGS_DIR")
    if custom:
        return os.path.expanduser(custom)
    return os.path.expanduser("~/.beacon/orgs")


def _path_for(org_id: str) -> str:
    return os.path.join(get_org_dir(), f"{org_id}.json")


def load_org(org_id: str) -> dict | None:
    """org doc を id で読む。無ければ None。"""
    if not org_id:
        return None
    path = _path_for(org_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_org(org: dict) -> None:
    """org doc を永続化する。``org["org_id"]`` が file key (= 真値)。"""
    org_id = org.get("org_id")
    if not org_id:
        raise ValueError("org doc missing org_id")
    os.makedirs(get_org_dir(), exist_ok=True)
    with open(_path_for(org_id), "w", encoding="utf-8") as f:
        json.dump(org, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_orgs(*, user_id: str | None = None) -> list[dict]:
    """org を一覧する。``user_id`` 指定時は「その user が member の org」だけ返す。

    可視性 (= user_id) は server.firestore_client.list_orgs_for_user と同じモデル:
    ``None`` は全件 (= admin view)、指定時は members に user が居る org のみ。
    participation-only の開示境界とは別レイヤー (= org 所属の一覧であって、org が
    束ねる project の可視性ではない)。
    """
    d = get_org_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                org = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # 読めない / 壊れた file は skip (best-effort listing)
        if user_id:
            members = [m.get("user_id") for m in org.get("members", []) or []]
            if user_id not in members:
                continue
        out.append(org)
    # 新しい順 (= server 側の並びと合わせる)
    out.sort(key=lambda x: (x.get("created_at", ""), x.get("org_id", "")),
             reverse=True)
    return out
