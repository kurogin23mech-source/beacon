#!/usr/bin/env python3
"""graph-migration-coverage.py — 移行台帳の cluster が navigate で引けるか疎通確認 (ms-156 e-5544)

SPEC 受入条件6 (顧客結合の証明): コード理解グラフが投機的な over-engineering でなく、
最初の顧客 = target 中心移行 (ms-150) の役に立つことを、**移行台帳
(paradigm-migration-ledger) の各 cluster (継ぎ目) を変更前に navigate で引ける**か
で示す。移行の fork は cluster を消化する前にこの疎通で「所属 module ＋ 契約 ＋
guard test ＋ 隣接」を引き、丸ごと読まずに navigate できる。

このスクリプトは台帳の cluster 群を取得し、code-graph の seam として navigate 可能かを
横断照合する。全 cluster が covered なら疎通 OK (exit 0)、未 cover の cluster が在れば
「グラフがその移行顧客をまだ載せていない gap」として exit 1。

使い方:
  python3 scripts/graph-migration-coverage.py            # 生きた台帳 + グラフを照合
  python3 scripts/graph-migration-coverage.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

import code_graph  # noqa: E402
import code_graph_query as query  # noqa: E402
import code_graph_store  # noqa: E402
import table_doc  # noqa: E402

LEDGER_DOC_ID = code_graph_store.LEDGER_DOC_ID
NODES_DOC_ID = code_graph_store.NODES_DOC_ID
EDGES_DOC_ID = code_graph_store.EDGES_DOC_ID


def _beacon_doc_show(doc_id: str) -> str:
    out = subprocess.run(["beacon", "doc", "show", doc_id],
                         capture_output=True, text=True, cwd=REPO)
    if out.returncode != 0:
        raise SystemExit(f"beacon doc show {doc_id} 失敗:\n{out.stderr}")
    return out.stdout


def ledger_cluster_axes(ledger_content: str) -> list[str]:
    """移行台帳 (table-doc) から cluster の axis を初出順・重複除去で返す。"""
    model = table_doc.parse_table(ledger_content)
    axes: list[str] = []
    for row in table_doc.active_rows(model):
        axis = (row.get("cells", {}).get("axis") or "").strip()
        if axis and axis not in axes:
            axes.append(axis)
    return axes


def _load_graph() -> "code_graph.CodeGraph":
    nc = _beacon_doc_show(NODES_DOC_ID)
    ec = _beacon_doc_show(EDGES_DOC_ID)
    return code_graph.CodeGraph.from_tables(
        table_doc.parse_table(nc), table_doc.parse_table(ec))


def _render(report: dict) -> str:
    lines = [f"移行 cluster の navigate 疎通: {report['covered_count']}/{report['cluster_count']} covered"]
    for r in report["clusters"]:
        mark = "✅" if r["covered"] else "❌ (グラフ未収載)"
        lines.append(f"  {mark} {r['cluster']}: 所属 {r['member_count']} module "
                     f"(契約 {r['with_contract']} / guard test {r['with_guard_test']})")
    if report["all_covered"]:
        lines.append("→ 全 cluster が変更前に navigate で引ける (顧客結合 OK)。")
    else:
        lines.append("→ 未 cover の cluster はグラフがまだ載せていない移行顧客。seed を見直してください。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify migration clusters are navigable.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    axes = ledger_cluster_axes(_beacon_doc_show(LEDGER_DOC_ID))
    graph = _load_graph()
    report = query.seam_coverage(graph, axes)

    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
          else _render(report))
    return 0 if report["all_covered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
