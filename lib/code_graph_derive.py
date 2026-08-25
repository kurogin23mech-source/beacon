"""code_graph_derive.py — 機械層 (構造) をソースから自動導出する (ms-156 e-5540)

SPEC 方針3: 導出できる構造を手で書くと二重管理で腐る。import / call / route の
構造は機械が全自動で導出し、0-drift 検証する。人が書くのは「隣人への契約・なぜこの
継ぎ目か」だけ (curated 層 = e-5542)。

このモジュールは **ソース (lib/*.py・server/*.py・channel/*.mjs) を真値** として、
機械層の辺を導出する純粋関数群。最小スライスでは **Python の import から
``depends-on`` 辺** を導出する (= 変更の伝播を辿る土台の依存構造)。call グラフ /
route / .mjs の import は後続 (surfaces-as も含め) に回す。

``check-map-drift.py`` が surface (CLI/API/Skill) を対象に「実在 vs 地図」を突くのと
同じ機械照合を、ここでは **module node と import 辺** に一般化する
(``scripts/check-graph-drift.py`` が照合の入口)。導出は REPO 配下のソースに固定
されているので、beacon 本体以外で走らせても意味を持たない (呼び出し側が文脈ガード)。
"""

from __future__ import annotations

import ast
import glob as globmod
import os

from code_graph import CodeGraph, Edge, Node

# module universe を成す source ディレクトリ (module 監査 150 module と同じ範囲)。
PY_DIRS = ("lib", "server")
MJS_DIRS = ("channel",)


def enumerate_source_modules(repo: str) -> set[str]:
    """現在のソースに実在する module の相対パス集合 (真値)。

    ``lib/*.py`` ・ ``server/*.py`` ・ ``channel/*.mjs`` を対象に、パッケージ marker
    の ``__init__.py`` は除く (module 監査が扱う単位に合わせる)。この集合が graph の
    node 台帳とズレていれば drift (= 監査 snapshot が古い / module 追加削除)。
    """
    mods: set[str] = set()
    for d in PY_DIRS:
        for fp in globmod.glob(os.path.join(repo, d, "*.py")):
            base = os.path.basename(fp)
            if base == "__init__.py":
                continue
            mods.add(f"{d}/{base}")
    for d in MJS_DIRS:
        for fp in globmod.glob(os.path.join(repo, d, "*.mjs")):
            mods.add(f"{d}/{os.path.basename(fp)}")
    return mods


def name_index(modules: set[str], directory: str) -> dict[str, str]:
    """``{bare_name: path}`` を 1 ディレクトリ分。``lib/core.py`` → ``core``。"""
    out: dict[str, str] = {}
    for path in modules:
        if path.startswith(directory + "/") and path.endswith(".py"):
            out[os.path.basename(path)[:-3]] = path
    return out


def resolve_import(name: str, src_dir: str,
                    lib_idx: dict[str, str], server_idx: dict[str, str]) -> str | None:
    """bare import 名を module パスへ。同ディレクトリ優先で lib/server を探す。

    このリポは lib/server を ``sys.path`` に載せるので import は ``import core`` の
    ような bare 形。同名が lib/server 双方に在りうるので、まず自分と同じ側を優先し、
    無ければ他方を見る (stdlib / 3rd-party は index に無いので None = 辺を張らない)。
    """
    if src_dir == "lib" and name in lib_idx:
        return lib_idx[name]
    if src_dir == "server" and name in server_idx:
        return server_idx[name]
    if name in lib_idx:
        return lib_idx[name]
    if name in server_idx:
        return server_idx[name]
    return None


def _module_imports(source: str) -> list[str]:
    """Python ソースの import から bare な top-level 名を集める (関数内 import も含む)。

    ``import a.b`` → ``a`` / ``from a.b import c`` (絶対) → ``a``。相対 import
    (``from . import x``、level>0) はこのリポの module では使われないので無視。
    構文エラーのファイルは空扱い (drift checker が別途 surface する)。
    """
    names: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) == 0 and node.module:
                names.append(node.module.split(".")[0])
    return names


def python_import_edges(repo: str, modules: set[str]) -> list[Edge]:
    """Python module 間の ``import`` を ``depends-on`` 辺 (有向 src→dst) に導出する。

    ``modules`` 集合の中で解決できた import だけ辺にする (外部依存は張らない)。
    自己 import は落とす。返り値は未 dedup (呼び出し側 / CodeGraph が dedup)。
    """
    lib_idx = name_index(modules, "lib")
    server_idx = name_index(modules, "server")
    edges: list[Edge] = []
    for path in sorted(modules):
        if not path.endswith(".py"):
            continue
        src_dir = path.split("/", 1)[0]
        try:
            source = open(os.path.join(repo, path), encoding="utf-8").read()
        except OSError:
            continue
        for name in _module_imports(source):
            dst = resolve_import(name, src_dir, lib_idx, server_idx)
            if dst and dst != path:
                edges.append(Edge(src=path, dst=dst, type="depends-on"))
    return edges


def augment_with_machine_layer(graph: CodeGraph, repo: str) -> dict:
    """graph に現在ソース由来の機械層 (module node ＋ ``depends-on`` 辺) を足し込む。

    - **node**: ソースに実在するが graph に無い module を bare node で追加する
      (監査 snapshot に載っていなかった裏方も現在地に含める)。既存 node は温存
      (seam / governs / role の curated セルを消さない)。
    - **edge**: import から ``depends-on`` を導出し、両端が node として在るものだけ
      張る。

    追加数 (``{"nodes_added", "edges_added"}``) を返す。graph を現在ソースに揃える
    ことで、直後の ``check-graph-drift`` は 0-drift になる (以降ソースが動くと drift)。
    """
    modules = enumerate_source_modules(repo)
    nodes_added = 0
    for m in modules:
        if not graph.has_node(m):
            graph.add_node(Node(id=m, path=m))
            nodes_added += 1
    edges_added = 0
    for e in python_import_edges(repo, modules):
        if graph.has_node(e.src) and graph.has_node(e.dst):
            if graph.add_edge(e):
                edges_added += 1
    return {"nodes_added": nodes_added, "edges_added": edges_added}


def module_node_ids(graph: CodeGraph) -> set[str]:
    """graph の中で **module** を指す node id (継ぎ目 seam node を除く)。

    module node の id はパス形 (``lib/x.py`` / function zoom は ``lib/x.py:span``) で
    必ず ``/`` を含む。seam node は ``seam:<axis>`` で ``/`` を含まないので、この 1 点で
    分離できる。
    """
    return {n.id for n in graph.nodes() if "/" in n.id}


def diff_against_source(graph: CodeGraph, repo: str) -> dict:
    """stored graph を現在ソースと照合し drift (書き漏れ / 幽霊) を返す。

    ``check-map-drift`` の 2 方向照合を module node と ``depends-on`` 辺に一般化する:

    - **missing_nodes**: ソースに実在するが graph に無い module (「地図に足せ」)。
    - **phantom_nodes**: graph に在るがソースに実在しない module (「地図から消せ」)。
    - **missing_edges / phantom_edges**: import から導いた ``depends-on`` と graph の
      ``depends-on`` の差。

    seam node と ``shares-seam`` 辺 (= 台帳由来で source からは導出しない層) は照合
    対象外 (source を真値にできないため)。``clean`` は全 4 差分が空かどうか。
    """
    modules = enumerate_source_modules(repo)
    gm = module_node_ids(graph)
    missing_nodes = sorted(modules - gm)
    phantom_nodes = sorted(gm - modules)

    ie = {(e.src, e.dst) for e in python_import_edges(repo, modules)}
    ge = {(e.src, e.dst) for e in graph.edges() if e.type == "depends-on"}
    missing_edges = sorted(ie - ge)
    phantom_edges = sorted(ge - ie)

    clean = not (missing_nodes or phantom_nodes or missing_edges or phantom_edges)
    return {
        "clean": clean,
        "missing_nodes": missing_nodes,
        "phantom_nodes": phantom_nodes,
        "missing_edges": missing_edges,
        "phantom_edges": phantom_edges,
        "counts": {
            "source_modules": len(modules),
            "graph_module_nodes": len(gm),
            "source_depends_on": len(ie),
            "graph_depends_on": len(ge),
        },
    }
