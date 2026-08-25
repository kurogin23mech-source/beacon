#!/usr/bin/env python3
"""graph-navigate.py — 変更前に「その継ぎ目の部分グラフ」を引く入口 (ms-156 e-5541)

SPEC 方針5 の change-start: コードを丸ごと読まず、変更する継ぎ目 (cluster) だけの
部分グラフ (所属 module ＋ 契約 ＋ guard test ＋ 隣接) を引いて navigate する。
移行の fork が変更前にこれを叩く (受入条件6、最初の顧客 = ms-150 移行)。

真値 = 生きた Beacon の code-graph table-doc 2枚 (既定)、または ``--nodes/--edges``
で渡すローカルファイル。

使い方:
  python3 scripts/graph-navigate.py --seam exec-auth          # 継ぎ目の部分グラフ
  python3 scripts/graph-navigate.py --module lib/auth.py      # module 起点の近傍
  python3 scripts/graph-navigate.py --list-seams              # 継ぎ目一覧
  python3 scripts/graph-navigate.py --seam exec-auth --json   # 機械可読
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
import code_graph_query as query  # noqa: E402
import code_graph_store  # noqa: E402
import code_graph_zoom as zoom  # noqa: E402
import table_doc  # noqa: E402


def _beacon_doc_show(doc_id: str) -> str:
    out = subprocess.run(["beacon", "doc", "show", doc_id],
                         capture_output=True, text=True, cwd=REPO)
    if out.returncode != 0:
        raise SystemExit(f"beacon doc show {doc_id} 失敗:\n{out.stderr}")
    return out.stdout


def _load_graph(args) -> "code_graph.CodeGraph":
    # PR #675 AX-2: --nodes-file / --edges-file は必ずペア (片方だけで live doc への
    # silent フォールバックを防ぐ)。
    if bool(args.nodes_file) != bool(args.edges_file):
        raise SystemExit("--nodes-file と --edges-file はペアで指定してください (片方だけは不可)")
    if args.nodes_file and args.edges_file:
        nc = open(args.nodes_file, encoding="utf-8").read()
        ec = open(args.edges_file, encoding="utf-8").read()
    else:
        nc = _beacon_doc_show(args.nodes_doc)
        ec = _beacon_doc_show(args.edges_doc)
    return code_graph.CodeGraph.from_tables(
        table_doc.parse_table(nc), table_doc.parse_table(ec))


def _render_seam(sub: dict) -> str:
    lines = [f"継ぎ目 (seam): {sub['seam']} — 所属 module {sub['member_count']}件"]
    for m in sub["members"]:
        lines.append(f"\n▸ {m['id']}")
        if m["role"]:
            lines.append(f"    役割: {m['role']}")
        if m["governs"]:
            lines.append(f"    統べる spine: {m['governs']}")
        lines.append(f"    契約: {m['contract'] or '(未記入 — curated 層は e-5542)'}")
        lines.append(f"    guard test: {m['guard_test'] or '(未紐付け)'}")
        if m["depends_on"]:
            lines.append(f"    依存する先 ({len(m['depends_on'])}): "
                         + ", ".join(m["depends_on"][:8])
                         + (" …" if len(m["depends_on"]) > 8 else ""))
        if m["depended_on_by"]:
            lines.append(f"    依存される元 ({len(m['depended_on_by'])}): "
                         + ", ".join(m["depended_on_by"][:8])
                         + (" …" if len(m["depended_on_by"]) > 8 else ""))
    lines.append(f"\n内側で閉じる依存: {len(sub['internal_dependencies'])}件 / "
                 f"継ぎ目をまたぐ依存 (伝播境界): {len(sub['boundary_dependencies'])}件")
    return "\n".join(lines)


def _render_zoom(z: dict) -> str:
    if not z.get("found"):
        return f"zoom できません (module でない/構文エラー): {z['module']}"
    lines = [f"zoom: {z['module']} — {z['line_count']}行 / {z['symbol_count']} symbol"]
    for s in z["symbols"]:
        head = f"  [{s['kind']}] {s['name']}  (L{s['lineno']}-{s['end_lineno']})"
        lines.append(head)
        if s["doc"]:
            lines.append(f"      {s['doc']}")
        if s["depends_on"]:
            lines.append(f"      依存: {', '.join(s['depends_on'][:8])}"
                         + (" …" if len(s["depends_on"]) > 8 else ""))
    return "\n".join(lines)


def _render_module(view: dict) -> str:
    if not view.get("found"):
        return f"module が見つかりません: {view['module']}"
    lines = [f"module: {view['id']}"]
    if view["role"]:
        lines.append(f"  役割: {view['role']}")
    lines.append(f"  所属継ぎ目: {', '.join(view['seams']) or '(なし)'}")
    lines.append(f"  統べる spine: {view['governs'] or '(なし)'}")
    lines.append(f"  契約: {view['contract'] or '(未記入)'}")
    lines.append(f"  guard test: {view['guard_test'] or '(未紐付け)'}")
    lines.append(f"  依存する先 ({len(view['depends_on'])}): "
                 + (", ".join(view["depends_on"]) or "(なし)"))
    lines.append(f"  依存される元 ({len(view['depended_on_by'])}): "
                 + (", ".join(view["depended_on_by"]) or "(なし)"))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Navigate the code-understanding graph.")
    ap.add_argument("--seam", help="継ぎ目 (cluster) を指定してその部分グラフを引く")
    ap.add_argument("--module", help="module を起点に近傍を引く")
    ap.add_argument("--zoom", metavar="MODULE",
                    help="巨大 module を function 粒度へ動的 zoom する (非格納・その場計算)")
    ap.add_argument("--list-seams", action="store_true", help="継ぎ目の一覧を出す")
    ap.add_argument("--nodes-file", metavar="PATH", help="nodes table-doc ファイル")
    ap.add_argument("--edges-file", metavar="PATH", help="edges table-doc ファイル")
    ap.add_argument("--nodes-doc", default=code_graph_store.NODES_DOC_ID)
    ap.add_argument("--edges-doc", default=code_graph_store.EDGES_DOC_ID)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not (args.seam or args.module or args.list_seams or args.zoom):
        ap.error("--seam / --module / --zoom / --list-seams のいずれかを指定してください")

    # zoom はソース直読み (格納グラフ不要・cloud 不要) なので先に処理する。
    if args.zoom:
        z = zoom.zoom_module(REPO, args.zoom)
        print(json.dumps(z, ensure_ascii=False, indent=2) if args.json
              else _render_zoom(z))
        return 0 if z.get("found") else 1

    graph = _load_graph(args)

    if args.list_seams:
        seams = graph.seams()
        print(json.dumps(seams, ensure_ascii=False) if args.json
              else "継ぎ目一覧:\n" + "\n".join(f"  - {s}" for s in seams))
        return 0

    if args.seam:
        result = query.subgraph_for_seam(graph, args.seam)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["member_count"] == 0:
            # PR #675 AX-3: member ゼロを無出力 exit 1 で返さず、登録済み継ぎ目を示して
            # 回復経路を出す (誤った継ぎ目名を叩いたときに --list-seams へ誘導)。
            print(f"継ぎ目 '{result['seam']}' に所属 module がありません。"
                  f"登録済み継ぎ目: {', '.join(graph.seams()) or '(なし)'}")
        else:
            print(_render_seam(result))
        return 0 if result["member_count"] else 1

    view = query.neighborhood_for_module(graph, args.module)
    print(json.dumps(view, ensure_ascii=False, indent=2) if args.json
          else _render_module(view))
    return 0 if view.get("found") else 1


if __name__ == "__main__":
    raise SystemExit(main())
