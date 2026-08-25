#!/usr/bin/env python3
"""graph-curate.py — コード理解グラフの curated 層 (契約 / 意図) を人手で書く口 (ms-156 e-5542)

SPEC 方針3/4: 機械が導出する構造 (seam / depends-on) は触らず、人は「隣人への契約・
この module の意図 (role)・守るべき挙動テスト (guard_test)」だけを書く。ここはその
書き込み口 (生きた node 表ドキュメントの該当 module 行を append-only で更新)。

判定 / model 変換は純粋な ``lib/code_graph_curate`` が持ち、ここは取得 (beacon doc
show) と保存 (update_document) の orchestration に徹する。

使い方:
  python3 scripts/graph-curate.py --module lib/auth.py --show
  python3 scripts/graph-curate.py --module lib/auth.py \
      --contract "全ログイン経路で id_token/JWT を発行する。machine executor は対象外" \
      --role "認証の綻び点 (spine §2)" --guard-test tests/test_auth.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import code_graph_curate  # noqa: E402
import code_graph_store  # noqa: E402
import table_doc  # noqa: E402

NODES_DOC_ID = code_graph_store.NODES_DOC_ID
NODES_TITLE = code_graph_store.NODES_TITLE


def _beacon_doc_show(doc_id: str) -> str:
    out = subprocess.run(["beacon", "doc", "show", doc_id],
                         capture_output=True, text=True, cwd=REPO)
    if out.returncode != 0:
        raise SystemExit(f"beacon doc show {doc_id} 失敗:\n{out.stderr}")
    return out.stdout


def _frontmatter() -> str:
    return ("---\nscope: spec\n"
            f"format: {table_doc.TABLE_FORMAT}\n"
            "milestone: ms-156\ntarget: ms-156\n---\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Curate the code-graph (contract/intent).")
    ap.add_argument("--module", required=True, help="対象 module (例 lib/auth.py)")
    ap.add_argument("--contract", help="隣人への契約 (curated)")
    ap.add_argument("--role", help="この module の意図 / 責務 (curated)")
    ap.add_argument("--guard-test", help="挙動を固定する characterization test path")
    ap.add_argument("--show", action="store_true", help="現在の curated セルを表示")
    ap.add_argument("--nodes-doc", default=NODES_DOC_ID)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    node_table = table_doc.parse_table(_beacon_doc_show(args.nodes_doc))

    if args.show or not (args.contract or args.role or args.guard_test):
        view = code_graph_curate.curated_view(node_table, args.module)
        print(json.dumps(view, ensure_ascii=False, indent=2) if args.json
              else "\n".join(f"{k}: {v!r}" for k, v in view.items()))
        return 0

    updates = {}
    if args.contract is not None:
        updates["contract"] = args.contract
    if args.role is not None:
        updates["role"] = args.role
    if args.guard_test is not None:
        updates["guard_test"] = args.guard_test

    changed = code_graph_curate.set_curated(
        node_table, args.module, updates, actor="graph-curate", at="")
    if not changed:
        print(f"変更なし (同値): {args.module}")
        return 0

    # 生きた doc を更新 (append-only 履歴つき)。
    import commands_shared as cs
    if not cs._is_cloud_mode():
        raise SystemExit("cloud mode 専用です (.beacon/cloud.json が要ります)")
    client, config = cs._get_api_client()
    body = table_doc.serialize_table_body(NODES_TITLE, node_table)
    content = _frontmatter() + body + "\n"
    client.update_document(config["project_id"], args.nodes_doc, NODES_TITLE, content)
    print(f"更新: {args.module} ({', '.join(changed)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
