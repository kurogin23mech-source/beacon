"""code_graph_query.py — 継ぎ目を指定して部分グラフを引く navigate 入口 (ms-156 e-5541)

SPEC 方針5: グラフを丸ごと load せず、変更ごとに「その継ぎ目 (seam) の部分グラフ
(所属 module ＋ 契約 ＋ guard test ＋ 隣接)」を引く (= change-start)。移行の fork が
変更前にこの query で navigate する入口 (受入条件4/6、最初の顧客 = ms-150 移行)。

このモジュールは ``CodeGraph`` から **部分グラフ** を切り出す純粋関数群。丸ごと
load しない真の部分 load (graph primitive) は後続 (方針6: まず table adjacency)。
ここでは load 済 graph から必要な断面だけを構造化して返す形で「引く体験」を成立
させる。
"""

from __future__ import annotations

from code_graph import CodeGraph, Node


def _normalize_seam(seam: str) -> str:
    """``seam:exec-auth`` でも ``exec-auth`` でも受ける。"""
    return seam[len("seam:"):] if seam.startswith("seam:") else seam


def _is_module(node: Node) -> bool:
    """module node (継ぎ目 seam node を除く)。id はパス形で ``/`` を含む。"""
    return "/" in node.id


def _member_view(graph: CodeGraph, node: Node) -> dict:
    """1 module の navigate 断面: 契約 / guard test / 統べる spine / 隣接。"""
    depends_on = sorted(
        dst for dst, _ in graph.neighbors(node.id, edge_type="depends-on"))
    depended_on_by = sorted(
        src for src, _ in graph.predecessors(node.id, edge_type="depends-on"))
    surfaces_as = sorted(
        dst for dst, _ in graph.neighbors(node.id, edge_type="surfaces-as"))
    return {
        "id": node.id,
        "path": node.path,
        "role": node.role,
        "contract": node.contract,     # curated (e-5542 で埋まる)
        "guard_test": node.guard_test,
        "governs": node.governs,
        "seams": node.seams(),
        "depends_on": depends_on,             # この module が使う先 (out)
        "depended_on_by": depended_on_by,     # この module に依存する元 (in)
        "surfaces_as": surfaces_as,           # この module が露出する CLI/API 入口 (e-5558)
    }


def subgraph_for_seam(graph: CodeGraph, seam: str) -> dict:
    """継ぎ目 (cluster) を指定して、その部分グラフを返す (受入条件4)。

    返り値:
      - ``seam``: 正規化した継ぎ目名。
      - ``members``: 所属 module の断面 (契約 / guard test / 隣接) を id 順に。
      - ``internal_dependencies``: 継ぎ目の内側で閉じる depends-on 辺。
      - ``boundary_dependencies``: 継ぎ目の内外をまたぐ depends-on 辺 (伝播の境界)。

    継ぎ目が存在しない (member ゼロ) 場合も空の断面を返す (呼び出し側が判定)。
    """
    seam = _normalize_seam(seam)
    members = sorted((n for n in graph.nodes_in_seam(seam) if _is_module(n)),
                     key=lambda n: n.id)
    member_ids = {n.id for n in members}
    member_views = [_member_view(graph, n) for n in members]

    internal: list[dict] = []
    boundary: list[dict] = []
    for e in graph.edges():
        if e.type != "depends-on":
            continue
        src_in, dst_in = e.src in member_ids, e.dst in member_ids
        if src_in and dst_in:
            internal.append({"src": e.src, "dst": e.dst})
        elif src_in or dst_in:
            boundary.append({"src": e.src, "dst": e.dst,
                             "direction": "out" if src_in else "in"})

    return {
        "seam": seam,
        "member_count": len(members),
        "members": member_views,
        "internal_dependencies": internal,
        "boundary_dependencies": boundary,
    }


def seam_coverage(graph: CodeGraph, cluster_names: list[str]) -> dict:
    """移行台帳の cluster 群が、実際に navigate で引ける部分グラフを持つか照合する。

    受入条件6 (顧客結合の証明): このグラフが投機的な over-engineering でなく移行の
    役に立つことを、移行台帳 (paradigm-migration-ledger) の各 cluster を変更前に
    navigate できるかで示す。cluster 名 (= seam の axis) ごとに:

      - ``covered``: seam として存在し member module が 1 つ以上ある (navigate 可能)。
      - ``member_count`` / ``with_contract`` / ``with_guard_test``: curate の進捗。

    ``covered=False`` の cluster は「移行が navigate に使えない継ぎ目」= グラフが
    その顧客をまだ載せていない gap。全 cluster が covered なら疎通確認 OK。
    """
    rows = []
    for name in cluster_names:
        name = _normalize_seam(name)
        members = [n for n in graph.nodes_in_seam(name) if _is_module(n)]
        rows.append({
            "cluster": name,
            "covered": len(members) > 0,
            "member_count": len(members),
            "with_contract": sum(1 for n in members if n.contract),
            "with_guard_test": sum(1 for n in members if n.guard_test),
        })
    covered = sum(1 for r in rows if r["covered"])
    return {
        "clusters": rows,
        "cluster_count": len(rows),
        "covered_count": covered,
        "all_covered": covered == len(rows) and len(rows) > 0,
    }


def neighborhood_for_module(graph: CodeGraph, module_id: str) -> dict:
    """1 module を起点に navigate する断面 (変更起点が module のとき)。

    その module の断面に加え、共有する継ぎ目の名前と、依存の直近 1 hop を返す。
    seam query の補完 (変更を module から始めるときの入口)。
    """
    node = graph.get_node(module_id)
    if node is None:
        return {"module": module_id, "found": False}
    view = _member_view(graph, node)
    view["found"] = True
    view["module"] = module_id
    return view
