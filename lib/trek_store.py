"""Trek のローカル記録を**読む**だけのモジュール (ms-185 e-7070)。

この機能は畳まれた。e-7069 が両フロントの `beacon trek` と Skill 4 本を落とし、
e-7079 が作成経路と画面の入口を落とし、e-7070 が本体 (``lib/trek.py`` /
``lib/cmd_trek.py`` / ``lib/trek_scheduler.py`` / ``trek_slots.py`` /
``trek_status.py`` / サーバの定期集約) を落とした。

**残っているのは ``list_treks`` だけで、呼び出し元は 1 つしか無い** ——
``lib/cmd_bus.py`` の送信予算の迂回判定 (= 同じ Trek に参加している相手への DM は
予算の上限を使わない) が ``store.list_treks()`` 経由でここに来る。その迂回の撤去は
**e-7080** の仕事として割り当てられている (``cmd_bus.py`` の当該コメント参照)。
e-7080 が済めばこのモジュールは呼び出し元ゼロになるので、そこで丸ごと落とす。

落としたもの (cmd_trek の消滅で呼び出し元ゼロになった):
  * ``load_trek`` / ``save_trek`` / ``update_trek`` — 単体読み書き。読み手は
    ``beacon trek show`` だけで、それは e-7069 で落ちた。``save`` / ``update`` は
    新しい記録が生まれない以上、誰も呼ばない。
  * ``session_history`` の遅延 hydration — ``load_trek`` の中で
    ``trek.backfill_session_history`` を呼んでいた。画面の MEMBERS & AGENTS を
    描くためのもので、画面も ``lib/trek.py`` も無くなった。

**ローカルの記録ファイル (``~/.beacon/treks/*.json``) は消していない。** 素の JSON
として残るので、手で開けば読める。製品面として pin されている読み出しは**クラウド側**
—— HTTP の ``GET /api/treks`` / ``GET /api/treks/{id}`` /
``GET /api/treks/{id}/logs.jsonl`` と、両 backend (firestore_client /
dynamodb_client) の ``get_trek`` / ``list_treks`` / ``list_trek_logs`` ——
であり、``tests/test_trek_record_read_surface_e7081.py`` が 1 つずつ固定している
(ms-185 SPEC 方針5 = 記録を後から読めなくしない)。

**ここに書き込みを戻さないこと。** 作成経路が無いので新しい記録は生まれない。

Test override: set ``BEACON_TREKS_DIR`` to point at a tmp dir.
"""
from __future__ import annotations

import json
import os


def get_trek_dir() -> str:
    """Return the directory holding trek JSON files.

    Default: ``~/.beacon/treks/``. Tests override with ``BEACON_TREKS_DIR``.
    """
    custom = os.environ.get("BEACON_TREKS_DIR")
    if custom:
        return os.path.expanduser(custom)
    return os.path.expanduser("~/.beacon/treks")


def list_treks(*, actor_id: str | None = None,
               status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    """List treks, optionally filtered by visibility + status.

    Visibility (= actor_id) follows the same model as ``server.firestore_client``:
    ``None`` returns all (admin), set returns only treks where the actor is
    creator OR appears in the members list.
    """
    d = get_trek_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # skip unreadable / malformed files (best-effort listing)
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        if actor_id:
            creator = (t.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id") for m in t.get("members", []) or []]
            if creator != actor_id and actor_id not in members:
                continue
        out.append(t)
    # Newest first (= matches server-side ordering)
    out.sort(key=lambda x: (x.get("created_at", ""), x.get("trek_id", "")),
             reverse=True)
    return out
