#!/usr/bin/env python3
"""seed-code-graph.py — コード理解グラフの種を既存台帳から materialize する (ms-156 e-5539)

module 監査 (150 module) + application-map (value 文脈) を種に、node/edge schema に
沿った adjacency を組み立て、Beacon-native な table-doc 2枚 (nodes / edges) として
出力する。parse/変換の中身は純粋な ``lib/code_graph`` + ``lib/code_graph_seed`` が
所有し、このスクリプトは取得 (beacon doc show) と出力 (json / emit / create) の
orchestration に徹する (架構: architecture-tool-skill-separation)。

使い方:
  # 既存 doc を種に集計だけ (cloud read, 書き込みなし)
  python3 scripts/seed-code-graph.py --json

  # ローカル fixture から (cloud 不要・hermetic)
  python3 scripts/seed-code-graph.py --from-files INVENTORY.md APP_MAP.md --json

  # Beacon-native table-doc の内容を書き出す (格納 format の確認)
  python3 scripts/seed-code-graph.py --emit-nodes nodes.md --emit-edges edges.md

  # 生きた Beacon project に table-doc 2枚を作る (dogfood 格納, cloud write)
  python3 scripts/seed-code-graph.py --create
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import code_graph  # noqa: E402
import code_graph_seed  # noqa: E402

# 種となる既存 doc の既定 id (SPEC 関連セクション)。
DEFAULT_INVENTORY_ID = "NC7bEWi08ELyNqgS6Mz0"   # module 監査 (150 module)
DEFAULT_APP_MAP_ID = "application-map"           # 全貌マップ (value 文脈)

NODES_DOC_ID = "code-graph-nodes"
EDGES_DOC_ID = "code-graph-edges"
NODES_TITLE = "コード理解グラフ: nodes (module)"
EDGES_TITLE = "コード理解グラフ: edges (adjacency)"


def _beacon_doc_show(doc_id: str) -> str:
    """``beacon doc show <id>`` の本文を返す (frontmatter 込み raw)。"""
    out = subprocess.run(
        ["beacon", "doc", "show", doc_id],
        capture_output=True, text=True, cwd=REPO,
    )
    if out.returncode != 0:
        raise SystemExit(f"beacon doc show {doc_id} 失敗:\n{out.stderr}")
    return out.stdout


def _load_seed_text(args) -> tuple[str, str]:
    """(inventory_markdown, app_map_text) を fixture か cloud doc から取得。"""
    if args.from_files:
        if len(args.from_files) < 1:
            raise SystemExit("--from-files には少なくとも inventory を渡してください")
        inv = open(args.from_files[0], encoding="utf-8").read()
        app = ""
        if len(args.from_files) >= 2:
            app = open(args.from_files[1], encoding="utf-8").read()
        return inv, app
    inv = _beacon_doc_show(args.inventory_id)
    app = _beacon_doc_show(args.app_map_id)
    return inv, app


def _frontmatter() -> str:
    return ("---\n"
            "scope: spec\n"
            f"format: {code_graph.table_doc.TABLE_FORMAT}\n"
            "milestone: ms-156\n"
            "target: ms-156\n"
            "---\n")


def _table_doc_content(title: str, table: dict) -> str:
    body = code_graph.table_doc.serialize_table_body(title, table)
    return _frontmatter() + body + "\n"


def _summary(graph: "code_graph.CodeGraph") -> dict:
    nodes = graph.nodes()
    edges = graph.edges()
    by_type: dict[str, int] = {}
    for e in edges:
        by_type[e.type] = by_type.get(e.type, 0) + 1
    module_nodes = [n for n in nodes if not code_graph_seed.is_seam_node(n)]
    seam_nodes = [n for n in nodes if code_graph_seed.is_seam_node(n)]
    with_role = sum(1 for n in module_nodes if n.role)
    isolated = sum(1 for n in module_nodes if not graph.neighbors(n.id))
    return {
        "module_nodes": len(module_nodes),
        "seam_nodes": len(seam_nodes),
        "total_nodes": len(nodes),
        "edges": len(edges),
        "edges_by_type": by_type,
        "seams": len(graph.seams()),
        "module_nodes_with_role": with_role,
        "isolated_module_nodes": isolated,
        "sample_seam": (graph.seams()[0] if graph.seams() else None),
    }


def _create_live_docs(graph, args) -> None:
    """生きた Beacon project に nodes / edges の table-doc 2枚を作る (cloud write)."""
    sys.path.insert(0, os.path.join(REPO, "lib"))
    import commands_shared as cs

    node_content = _table_doc_content(NODES_TITLE, graph.to_node_table())
    edge_content = _table_doc_content(EDGES_TITLE, graph.to_edge_table())

    if not cs._is_cloud_mode():
        raise SystemExit("--create は cloud mode 専用です (.beacon/cloud.json が要ります)")
    client, config = cs._get_api_client()
    pid = config["project_id"]
    for title, content in ((NODES_TITLE, node_content), (EDGES_TITLE, edge_content)):
        res = client.create_document(pid, title, content)
        print(f"created: {res.get('doc_id')} ({title})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the code-understanding graph.")
    ap.add_argument("--inventory-id", default=DEFAULT_INVENTORY_ID)
    ap.add_argument("--app-map-id", default=DEFAULT_APP_MAP_ID)
    ap.add_argument("--from-files", nargs="*", metavar="MD",
                    help="INVENTORY.md [APP_MAP.md] をローカルから読む (cloud 不要)")
    ap.add_argument("--json", action="store_true", help="集計を JSON で出す")
    ap.add_argument("--emit-nodes", metavar="PATH", help="nodes table-doc 内容を書き出す")
    ap.add_argument("--emit-edges", metavar="PATH", help="edges table-doc 内容を書き出す")
    ap.add_argument("--create", action="store_true",
                    help="生きた Beacon project に table-doc 2枚を作る (cloud write)")
    args = ap.parse_args()

    inv, app = _load_seed_text(args)
    graph = code_graph_seed.build_seed_graph(inv, app)
    summary = _summary(graph)

    if args.emit_nodes:
        with open(args.emit_nodes, "w", encoding="utf-8") as f:
            f.write(_table_doc_content(NODES_TITLE, graph.to_node_table()))
        print(f"wrote {args.emit_nodes} ({summary['total_nodes']} nodes)", file=sys.stderr)
    if args.emit_edges:
        with open(args.emit_edges, "w", encoding="utf-8") as f:
            f.write(_table_doc_content(EDGES_TITLE, graph.to_edge_table()))
        print(f"wrote {args.emit_edges} ({summary['edges']} edges)", file=sys.stderr)
    if args.create:
        _create_live_docs(graph, args)

    if args.json or not (args.emit_nodes or args.emit_edges or args.create):
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
