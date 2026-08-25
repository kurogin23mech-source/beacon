"""code_graph_derive.py — 機械層 (構造) をソースから自動導出する (ms-156 e-5540)

SPEC 方針3: 導出できる構造を手で書くと二重管理で腐る。import / call / route の
構造は機械が全自動で導出し、0-drift 検証する。人が書くのは「隣人への契約・なぜこの
継ぎ目か」だけ (curated 層 = e-5542)。

このモジュールは **ソース (lib/*.py・server/*.py・channel/*.mjs) を真値** として、
機械層の辺を導出する純粋関数群 (e-5540 で import→depends-on、e-5558 で call グラフ・
route・cli surfaces-as・.mjs import に拡張):

- ``depends-on`` (機械): Python import (``python_import_edges``) + 関数呼び出し
  (``call_edges``) + channel の .mjs import (``mjs_import_edges``)。
- ``surfaces-as`` (機械): server 経路→定義 module (``route_surfaces``) + CLI verb→
  実装 module (``cli_surfaces``、application-map の楔と同じ id 体系)。

``check-map-drift.py`` が surface (CLI/API/Skill) を対象に「実在 vs 地図」を突くのと
同じ機械照合を、ここでは **module node と import 辺** に一般化する
(``scripts/check-graph-drift.py`` が照合の入口)。導出は REPO 配下のソースに固定
されているので、beacon 本体以外で走らせても意味を持たない (呼び出し側が文脈ガード)。
"""

from __future__ import annotations

import ast
import glob as globmod
import os
import re

from code_graph import CodeGraph, Edge, Node

# module universe を成す source ディレクトリ (module 監査 150 module と同じ範囲)。
PY_DIRS = ("lib", "server")
MJS_DIRS = ("channel",)

# module でない node の id 接頭辞 (継ぎ目 seam / surface = CLI/API)。module node は
# ``lib/`` ``server/`` ``channel/`` で始まるパス形なので、この 1 点で確実に分離する。
NON_MODULE_PREFIXES = ("seam:", "cli:", "api:", "skill:", "route:")
MODULE_DIR_PREFIXES = ("lib/", "server/", "channel/")


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


def import_alias_map(tree: ast.Module, modules: set[str], src_dir: str) -> dict[str, str]:
    """module の import を「ローカル名 → 依存先 module path」に畳む (両 import スタイル)。

    ``import core`` → core→lib/core.py、``import x as y`` → y→…、
    ``from commands_shared import a, b as c`` → a/c→lib/commands_shared.py。
    call グラフ (関数内で使う名前 → その名前が指す module) と zoom の依存 attribute が
    共有する解決表。
    """
    lib_idx = name_index(modules, "lib")
    server_idx = name_index(modules, "server")
    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bare = a.name.split(".")[0]
                dst = resolve_import(bare, src_dir, lib_idx, server_idx)
                if dst:
                    alias[a.asname or bare] = dst
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) != 0 or not node.module:
                continue
            dst = resolve_import(node.module.split(".")[0], src_dir, lib_idx, server_idx)
            if not dst:
                continue
            for a in node.names:
                alias[a.asname or a.name] = dst
    return alias


def call_edges(repo: str, modules: set[str]) -> list[Edge]:
    """関数呼び出し (AST) から module 間 ``depends-on`` 辺を導出する (e-5558)。

    各 py module の全 Call の呼び先起点名 (``core.save()`` の ``core`` / ``foo()`` の
    ``foo``) を import alias 表で module へ解決する。呼ぶには import が要るので大半は
    import 由来 depends-on と重なる (graph 側で dedup)。call グラフを別導出として持つ
    ことで「呼び出しで結ばれた依存」を辺として明示し、0-drift 対象に含める。
    """
    edges: list[Edge] = []
    for path in sorted(m for m in modules if m.endswith(".py")):
        src_dir = path.split("/", 1)[0]
        try:
            tree = ast.parse(open(os.path.join(repo, path), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        alias = import_alias_map(tree, modules, src_dir)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            base = None
            if isinstance(func, ast.Name):
                base = func.id
            elif isinstance(func, ast.Attribute):
                inner = func.value
                if isinstance(inner, ast.Name):
                    base = inner.id
            dst = alias.get(base) if base else None
            if dst and dst != path:
                edges.append(Edge(src=path, dst=dst, type="depends-on"))
    return edges


# JS の import / require の指定子を拾う (相対 module 解決用)。
_MJS_SPEC_RE = re.compile(
    r"""(?:from|import)\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)""")


def mjs_import_edges(repo: str, modules: set[str]) -> list[Edge]:
    """channel/*.mjs の相対 import / require を ``depends-on`` 辺に導出する (e-5558)。

    ``import x from './y.mjs'`` / ``import './y.mjs'`` / ``require('./y.mjs')`` の相対
    指定子を channel/ 内 module に解決する。node 組み込み / npm など相対でない指定子は
    module 集合に無いので辺を張らない。
    """
    edges: list[Edge] = []
    for path in sorted(m for m in modules if m.endswith(".mjs")):
        try:
            src = open(os.path.join(repo, path), encoding="utf-8").read()
        except OSError:
            continue
        for m1, m2 in _MJS_SPEC_RE.findall(src):
            spec = m1 or m2
            if not spec.startswith("."):
                continue  # 相対でなければ外部依存 → 張らない
            base = os.path.basename(spec)
            if not base.endswith(".mjs"):
                base += ".mjs"
            dst = f"channel/{base}"
            if dst in modules and dst != path:
                edges.append(Edge(src=path, dst=dst, type="depends-on"))
    return edges


def _norm_path(p: str) -> str:
    """API path の ``{param}`` を ``{}`` に潰して比較安定化 (check-map-drift と同型)。"""
    n = re.sub(r"\{[^}]+\}", "{}", p.rstrip("/"))
    return n or "/"


def route_surfaces(repo: str, modules: set[str]) -> list[tuple[str, str]]:
    """server の API 経路を ``(定義 module, 'api:METHOD path')`` に導出する (e-5558)。

    ``@app.<method>`` は server/app.py、``@router.<method>`` は routers_*.py (完全 path)
    と trailnode*.py (mount prefix 補完)。経路を「定義している module が surface として
    露出している」= surfaces-as 辺の源にする (check-map-drift の enumerate_api と同型)。
    """
    out: list[tuple[str, str]] = []
    app_rel = "server/app.py"
    app_fp = os.path.join(repo, "server", "app.py")
    if app_rel in modules and os.path.isfile(app_fp):
        txt = open(app_fp, encoding="utf-8").read()
        for meth, path in re.findall(
                r'@app\.(get|post|put|patch|delete|websocket)\(\s*["\']([^"\']+)["\']', txt):
            out.append((app_rel, f"api:{meth.upper()} {_norm_path(path)}"))
    scan: list[tuple[str, str]] = [
        (os.path.join(repo, "server", "trailnode.py"), "/api/trailnode"),
        (os.path.join(repo, "server", "trailnode_orgs.py"), "/api/trailnode/orgs"),
    ]
    scan += [(fp, "") for fp in sorted(globmod.glob(os.path.join(repo, "server", "routers_*.py")))]
    for fp, prefix in scan:
        rel = os.path.relpath(fp, repo).replace(os.sep, "/")
        if rel not in modules or not os.path.isfile(fp):
            continue
        txt = open(fp, encoding="utf-8").read()
        for meth, path in re.findall(
                r'@router\.(get|post|put|patch|delete|websocket)\(\s*["\']([^"\']*)["\']', txt):
            out.append((rel, f"api:{meth.upper()} {_norm_path(prefix + path)}"))
    return out


def cli_surfaces(repo: str, modules: set[str]) -> list[tuple[str, str]]:
    """CLI verb を ``(実装 module, 'cli:beacon <verb>')`` に導出する (e-5558)。

    lib/commands.py の dispatch dict ``commands = {"verb": handler}`` から verb→handler、
    同ファイルの import から handler→module を解いて verb→module を得る。verb の ``_`` は
    空白に開いて application-map の楔 (``cli:beacon task done``) と同じ id 体系にする。
    """
    cpath = os.path.join(repo, "lib", "commands.py")
    if "lib/commands.py" not in modules or not os.path.isfile(cpath):
        return []
    src = open(cpath, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    # handler 名 → module (commands.py の import から)。
    handler_to_mod = import_alias_map(tree, modules, "lib")
    # dispatch dict `commands = {...}` から "verb": handler_name を抜く。
    m = re.search(r"\n    commands = \{(.*?)\n    \}", src, re.S)
    out: list[tuple[str, str]] = []
    if not m:
        return out
    for verb, handler in re.findall(r'"([a-z0-9_]+)"\s*:\s*([A-Za-z_][A-Za-z0-9_]*)', m.group(1)):
        mod = handler_to_mod.get(handler)
        if mod:
            out.append((mod, f"cli:beacon {verb.replace('_', ' ')}"))
    return out


def all_depends_on_edges(repo: str, modules: set[str]) -> list[Edge]:
    """機械層の ``depends-on`` 全部 (import + call + .mjs)。graph 側で dedup。"""
    return (python_import_edges(repo, modules)
            + call_edges(repo, modules)
            + mjs_import_edges(repo, modules))


def all_surface_pairs(repo: str, modules: set[str]) -> list[tuple[str, str]]:
    """機械層の surfaces-as 源 (module, surface_id) 全部 (route + cli)。"""
    return route_surfaces(repo, modules) + cli_surfaces(repo, modules)


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
    for e in all_depends_on_edges(repo, modules):  # import + call + .mjs
        if graph.has_node(e.src) and graph.has_node(e.dst):
            if graph.add_edge(e):
                edges_added += 1

    # surfaces-as: module → その module が露出する CLI / API surface (e-5558)。
    surfaces_added = 0
    for module_id, surface_id in all_surface_pairs(repo, modules):
        if not graph.has_node(module_id):
            continue
        if not graph.has_node(surface_id):
            kind = "API" if surface_id.startswith("api:") else "CLI"
            graph.add_node(Node(id=surface_id, role=f"surface ({kind}): {surface_id}"))
        if graph.add_edge(Edge(src=module_id, dst=surface_id, type="surfaces-as")):
            surfaces_added += 1

    return {"nodes_added": nodes_added, "edges_added": edges_added,
            "surfaces_added": surfaces_added}


def module_node_ids(graph: CodeGraph) -> set[str]:
    """graph の中で **module** を指す node id (seam / surface node を除く)。

    module node の id は ``lib/`` ``server/`` ``channel/`` で始まるパス形。seam
    (``seam:``) や surface (``cli:`` / ``api:``) は別接頭辞なので接頭辞で確実に分離する。
    """
    return {n.id for n in graph.nodes() if n.id.startswith(MODULE_DIR_PREFIXES)}


def diff_against_source(graph: CodeGraph, repo: str) -> dict:
    """stored graph を現在ソースと照合し drift (書き漏れ / 幽霊) を返す。

    ``check-map-drift`` の 2 方向照合を、module node ・機械層の ``depends-on`` 辺
    (import + call + .mjs) ・``surfaces-as`` 辺 (route + cli) に一般化する:

    - **missing_nodes / phantom_nodes**: ソース実在 module と graph の module node の差。
    - **missing_edges / phantom_edges**: ソース由来 ``depends-on`` と graph の差。
    - **missing_surfaces / phantom_surfaces**: ソース由来 ``surfaces-as`` (module→surface)
      と graph の差。

    seam node と ``shares-seam`` / ``implements-contract`` (台帳・人手由来で source から
    導出しない層) は照合対象外。``clean`` は全差分が空かどうか。
    """
    modules = enumerate_source_modules(repo)
    gm = module_node_ids(graph)
    missing_nodes = sorted(modules - gm)
    phantom_nodes = sorted(gm - modules)

    ie = {(e.src, e.dst) for e in all_depends_on_edges(repo, modules)}
    ge = {(e.src, e.dst) for e in graph.edges() if e.type == "depends-on"}
    missing_edges = sorted(ie - ge)
    phantom_edges = sorted(ge - ie)

    src_surf = {(m, s) for m, s in all_surface_pairs(repo, modules)}
    g_surf = {(e.src, e.dst) for e in graph.edges() if e.type == "surfaces-as"}
    missing_surfaces = sorted(src_surf - g_surf)
    phantom_surfaces = sorted(g_surf - src_surf)

    clean = not (missing_nodes or phantom_nodes or missing_edges or phantom_edges
                 or missing_surfaces or phantom_surfaces)
    return {
        "clean": clean,
        "missing_nodes": missing_nodes,
        "phantom_nodes": phantom_nodes,
        "missing_edges": missing_edges,
        "phantom_edges": phantom_edges,
        "missing_surfaces": missing_surfaces,
        "phantom_surfaces": phantom_surfaces,
        "counts": {
            "source_modules": len(modules),
            "graph_module_nodes": len(gm),
            "source_depends_on": len(ie),
            "graph_depends_on": len(ge),
            "source_surfaces_as": len(src_surf),
            "graph_surfaces_as": len(g_surf),
        },
    }
