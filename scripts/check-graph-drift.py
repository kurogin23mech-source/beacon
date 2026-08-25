#!/usr/bin/env python3
"""check-graph-drift.py — コード理解グラフの 0-drift 検証 (ms-156 e-5540)

``check-map-drift.py`` が surface (CLI/API/Skill) を「実在 vs 地図」で突くのと同じ
機械照合を、**module node と import 由来の ``depends-on`` 辺** に一般化する。
グラフ (Beacon table-doc 2枚) が現在ソースからズレていれば書き漏れ / 幽霊を出す。

真値 = REPO 配下のソース (lib/*.py・server/*.py・channel/*.mjs)。照合対象の grafo は
生きた Beacon の table-doc (既定) か、``--nodes/--edges`` で渡すローカルの table-doc
ファイル (hermetic / CI 用)。

使い方:
  python3 scripts/check-graph-drift.py                 # 生きた doc を照合
  python3 scripts/check-graph-drift.py --nodes n.md --edges e.md   # ファイルを照合
  python3 scripts/check-graph-drift.py --json          # 機械可読
exit 0 = drift 無し / 1 = drift 有り / 2 = fatal / 3 = skip (beacon 本体でない)
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import code_graph  # noqa: E402
import code_graph_derive as derive  # noqa: E402
import code_graph_store  # noqa: E402
import table_doc  # noqa: E402

SKIP_EXIT = 3


def _is_beacon_source_project(repo: str) -> bool:
    """REPO が beacon 本体か (照合の真値源が在るか) を軽く判定する。"""
    return (os.path.isfile(os.path.join(repo, "lib", "commands.py"))
            and os.path.isdir(os.path.join(repo, "lib"))
            and bool(globmod.glob(os.path.join(repo, "server", "*.py"))))


def _beacon_doc_show(doc_id: str) -> str:
    out = subprocess.run(["beacon", "doc", "show", doc_id],
                         capture_output=True, text=True, cwd=REPO)
    if out.returncode != 0:
        raise SystemExit(f"beacon doc show {doc_id} 失敗:\n{out.stderr}")
    return out.stdout


def _load_graph(node_content: str, edge_content: str) -> "code_graph.CodeGraph":
    node_table = table_doc.parse_table(node_content)
    edge_table = table_doc.parse_table(edge_content)
    return code_graph.CodeGraph.from_tables(node_table, edge_table)


def _render(diff: dict) -> str:
    if diff["clean"]:
        c = diff["counts"]
        return (f"OK: drift 無し (module node {c['graph_module_nodes']} / "
                f"depends-on 辺 {c['graph_depends_on']})")
    lines = ["DRIFT: code-graph が現在ソースとズレています"]
    if diff["missing_nodes"]:
        lines.append(f"  書き漏れ module (ソースに在るが graph に無い) {len(diff['missing_nodes'])}件:")
        lines += [f"    + {m}" for m in diff["missing_nodes"][:20]]
    if diff["phantom_nodes"]:
        lines.append(f"  幽霊 module (graph に在るがソースに無い) {len(diff['phantom_nodes'])}件:")
        lines += [f"    - {m}" for m in diff["phantom_nodes"][:20]]
    if diff["missing_edges"]:
        lines.append(f"  書き漏れ depends-on 辺 {len(diff['missing_edges'])}件 (先頭のみ):")
        lines += [f"    + {s} -> {d}" for s, d in diff["missing_edges"][:20]]
    if diff["phantom_edges"]:
        lines.append(f"  幽霊 depends-on 辺 {len(diff['phantom_edges'])}件 (先頭のみ):")
        lines += [f"    - {s} -> {d}" for s, d in diff["phantom_edges"][:20]]
    lines.append("  → seeder を再実行 (scripts/seed-code-graph.py --derive --create) して"
                 "現在ソースに揃えてください。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the code-graph for drift vs source.")
    ap.add_argument("--nodes-file", metavar="PATH", help="nodes table-doc ファイルを照合")
    ap.add_argument("--edges-file", metavar="PATH", help="edges table-doc ファイルを照合")
    ap.add_argument("--nodes-doc", default=code_graph_store.NODES_DOC_ID,
                    help="nodes の doc id (live)")
    ap.add_argument("--edges-doc", default=code_graph_store.EDGES_DOC_ID,
                    help="edges の doc id (live)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not _is_beacon_source_project(REPO):
        msg = "SKIP: このプロジェクトは beacon 本体ではないため照合しません"
        print(json.dumps({"skipped": True}) if args.json else msg)
        return SKIP_EXIT

    # PR #675 AX-2: --nodes-file / --edges-file は必ずペア。片方だけだと黙って live doc に
    # フォールバックしてローカルファイルが無視される穴を防ぐ。
    if bool(args.nodes_file) != bool(args.edges_file):
        ap.error("--nodes-file と --edges-file はペアで指定してください (片方だけは不可)")
    if args.nodes_file and args.edges_file:
        node_content = open(args.nodes_file, encoding="utf-8").read()
        edge_content = open(args.edges_file, encoding="utf-8").read()
    else:
        node_content = _beacon_doc_show(args.nodes_doc)
        edge_content = _beacon_doc_show(args.edges_doc)

    graph = _load_graph(node_content, edge_content)
    diff = derive.diff_against_source(graph, REPO)

    if args.json:
        print(json.dumps(diff, ensure_ascii=False, indent=2))
    else:
        print(_render(diff))
    return 0 if diff["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
