"""ms-145 e-5371 — store backend 間の再エクスポート乖離を loud に検知する。

`server/store_router.py` は firestore / dynamodb / mysql の 3 backend から、
app.py が `db.<name>` で使う関数を **手で** re-export する。ある backend にだけ
関数 (= 対策 / mitigation) を足して別 backend の branch に足し忘れると、その別
backend にデプロイした瞬間その関数が silent に消える。

2026-08 の本番 OOM (最大12時間停止) の遠因はまさにこの形だった: ある backend
にだけ入っていた scan の対策が MySQL 移行で無言に失効していた (session memo
`tjDPP7Pp9ZtdShWA9ETT` §1)。この「backend を移ると前の対策が黙って無効になる」
class を、re-export 集合の非対称として検出し、意図的な非対称だけを allowlist に
理由付きで残すことで、**将来の新たな乖離**を CI で赤くする (ratchet)。

注意 (この test が catch する層): backend 間の *シンボル契約* の乖離 (= ある
backend にある関数が別 backend に無い) を捕まえる。同名関数の *内部挙動* が
backend 間で違う (= 引数の渡し忘れ等、上記 §1 の scan バグそのもの) 意味的乖離は
別レイヤーの conformance test が要る (e-6164 に切り出し)。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "server" / "store_router.py"
BACKEND_MODULES = {"firestore_client", "dynamodb_client", "mysql_client"}

# 意図的な非対称 (name -> なぜ 1 backend だけに在ってよいか)。
# ここに載せてよいのは「app 側がその非対称を吸収する」ことが確認できたものだけ:
#   (a) app.py が getattr(db, name, None) で optional 扱いし、無い backend では
#       従来経路に fallback する (= 直接 db.name( を unconditional に呼ばない)、または
#   (b) その backend の実装詳細に固有で他 backend では意味を持たない (escape hatch)。
# 新しく非対称を足すときは、上のどちらかを満たすことを確認してから理由を書くこと。
ALLOWED_ASYMMETRIES = {
    # --- firestore 固有の escape hatch (firestore.Client singleton 前提) ---
    # test が `import firestore_client as db` して db._db / db.get_db を patch する。
    # 他 backend には該当する singleton が無いので non-portable で正しい (router 251-264)。
    "_db": "firestore 固有: test が db._db を patch する escape hatch",
    "get_db": "firestore 固有: test が db.get_db を patch する escape hatch",
    "COLLECTION": "firestore 固有: collection 名定数",
    "USERS_COLLECTION": "firestore 固有: users collection 名定数",
    "PROJECT_ID": "firestore 固有: GCP project id 定数",
    # --- mysql 固有 (VPS 本番経路)。app / operations が吸収する ---
    "SCHEMA_V3_ENTRY": "mysql v3 schema 定数。operations.py が backend+schema を見て v3 経路に分岐 (直呼びしない)",
    "get_project_v3": "mysql v3 entry-split。operations.py dispatch が backend 判定して呼ぶ (firestore/dynamodb は v3 未対応)",
    "save_project_v3": "mysql v3 entry-split。operations.py dispatch 経由 (同上)",
    "apply_project_op_v3": "mysql v3 entry-split。operations.py dispatch 経由 (同上)",
    "replace_project_v3": "mysql v3 entry-split。operations.py dispatch 経由 (同上)",
    "get_project_meta": "mysql 固有 最適化 (whole-doc meta の軽量取得)。他 backend は get_project で代替",
    "list_tick_candidate_project_ids": "mysql 固有 最適化。app.py が getattr guard で optional 扱い、無ければ全 project scan に fallback",
    "find_bus_event_by_client_id": "mysql 固有 client-id 冪等 helper。app.py が getattr guard で optional 扱い",
}


def _exports_per_backend() -> dict[str, set[str]]:
    """store_router.py の各 `from <backend> import (...)` から名前集合を AST で抽出。

    backend の実モジュールを import しない (= boto3 / google.cloud / mysql 依存や
    env に依らず hermetic に走る)。各 backend module は router 内で 1 branch =
    1 ImportFrom なので module -> names で一意に取れる。
    """
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    exports: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in BACKEND_MODULES:
            exports.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
    return exports


def test_all_three_backends_present_in_router():
    """router が想定の 3 backend を全て branch として持つ (前提の固定)。"""
    exports = _exports_per_backend()
    assert set(exports) == BACKEND_MODULES, (
        f"store_router.py が想定した 3 backend を re-export していません: "
        f"見つかったのは {sorted(exports)}"
    )


def test_store_router_backend_parity():
    """backend 間で非対称な re-export は、全て allowlist に理由付きで在ること。

    ここが赤くなったら = ある backend にだけ関数を足した (= 対策を 1 backend に
    しか配っていない)。別 backend にデプロイすると silent に失効する恐れ。
    """
    exports = _exports_per_backend()
    common = set.intersection(*exports.values())
    every = set().union(*exports.values())
    asymmetric = every - common
    unexplained = sorted(n for n in asymmetric if n not in ALLOWED_ASYMMETRIES)

    detail = []
    for name in unexplained:
        present = sorted(m for m, names in exports.items() if name in names)
        missing = sorted(BACKEND_MODULES - set(present))
        detail.append(f"  - {name}: {present} にあり {missing} に無い")

    assert not unexplained, (
        "store backend 間で re-export が非対称なのに理由が記録されていません:\n"
        + "\n".join(detail)
        + "\n→ 対策なら他 backend にも同じ関数を re-export する (全 backend に配る)。\n"
        "  意図的な非対称なら ALLOWED_ASYMMETRIES に『どの backend 専用か・app 側が\n"
        "  どう吸収するか (getattr guard / escape hatch)』を書くこと。\n"
        "背景: 2026-08 の本番 OOM は、1 backend にだけ入れた対策が別 backend へ移った\n"
        "瞬間 silent 失効したのが遠因 (ms-145 / e-5371)。"
    )


def test_allowlist_has_no_stale_entries():
    """allowlist に、もう非対称でない/存在しない名前を残さない (rot 防止)。

    ある mysql 固有関数を後で firestore/dynamodb にも配って対称化したら、その
    allowlist entry は不要になる。残すと「非対称の免罪符」が形骸化するので消す。
    """
    exports = _exports_per_backend()
    common = set.intersection(*exports.values())
    every = set().union(*exports.values())
    asymmetric = every - common
    stale = sorted(set(ALLOWED_ASYMMETRIES) - asymmetric)
    assert not stale, (
        f"ALLOWED_ASYMMETRIES に、もう非対称でない/router に存在しない名前が残って"
        f"います: {stale} — 対称化されたなら allowlist から削除してください。"
    )
