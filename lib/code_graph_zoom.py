"""code_graph_zoom.py — 巨大モジュールを query 時に function 粒度へ動的 zoom (ms-156 e-5543)

SPEC 方針2: module 粒度では core.py(3592行)/commands.py/app.py のような巨大3-4本に
責務が潰れる。人が事前に継ぎ目を切ると維持できないので、**機械が module を default の
node に置き、巨大モジュールだけ query 時に AI が function 粒度へ zoom(拡大)する**。
粒度を固定せず「引く時に必要なだけ細分化」する。

重要 (SPEC やらない): **静的な全展開はしない**。全 module の全 function を node として
グラフに事前格納すると維持コストが爆発する。ここは query の瞬間にソースの AST を読んで
function 一覧と各 function の他 module 依存を **その場で計算して返す**(ephemeral、非格納)。

真値はソース。node 表 (格納済グラフ) は module 粒度のまま、この zoom がその 1 module を
掘り下げる補助経路 (navigate の内側)。
"""

from __future__ import annotations

import ast
import os

import code_graph_derive


def _import_alias_map(tree: ast.Module, repo: str, modules: set[str],
                      src_dir: str) -> dict[str, str]:
    """module の import を「ローカル名 → 依存先 module path」に畳む。

    ``import core`` → core→lib/core.py、``import x as y`` → y→…、
    ``from commands_shared import a, b as c`` → a/c→lib/commands_shared.py。
    この codebase は両スタイルを多用するので、function 内で使われた名前から
    「その function がどの module に依存するか」を後で引けるようにする。
    """
    lib_idx = {os.path.basename(p)[:-3]: p for p in modules
               if p.startswith("lib/") and p.endswith(".py")}
    server_idx = {os.path.basename(p)[:-3]: p for p in modules
                  if p.startswith("server/") and p.endswith(".py")}

    def resolve(bare: str) -> str | None:
        return code_graph_derive._resolve_import(bare, src_dir, lib_idx, server_idx)

    alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bare = a.name.split(".")[0]
                dst = resolve(bare)
                if dst:
                    alias[a.asname or bare] = dst
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) != 0 or not node.module:
                continue
            dst = resolve(node.module.split(".")[0])
            if not dst:
                continue
            for a in node.names:
                alias[a.asname or a.name] = dst  # from X import a  → a 経由で X に依存
    return alias


def _names_used(node: ast.AST) -> set[str]:
    """AST 部分木で参照される識別子 (Name / Attribute の起点) を集める。"""
    used: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            used.add(sub.id)
    return used


def _deps_for(node: ast.AST, alias: dict[str, str], self_path: str) -> list[str]:
    used = _names_used(node)
    deps = {alias[name] for name in used if name in alias}
    deps.discard(self_path)
    return sorted(deps)


def _doc_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def zoom_module(repo: str, module_path: str) -> dict:
    """1 module を function 粒度へ動的 zoom した断面を返す (非格納・その場計算)。

    返り値:
      - ``module`` / ``line_count`` / ``found``
      - ``symbols``: top-level の関数・クラス (+クラス直下メソッド) を出現順に。
        各要素 = ``{name, kind(function/async-function/class/method), lineno,
        end_lineno, doc(1行), depends_on(この記号が使う他 module)}``。

    ``.py`` 以外 / 構文エラー / 実在しない場合は ``found=False``。
    """
    abspath = os.path.join(repo, module_path)
    if not module_path.endswith(".py") or not os.path.isfile(abspath):
        return {"module": module_path, "found": False}
    source = open(abspath, encoding="utf-8").read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"module": module_path, "found": False}

    modules = code_graph_derive.enumerate_source_modules(repo)
    src_dir = module_path.split("/", 1)[0]
    alias = _import_alias_map(tree, repo, modules, src_dir)

    symbols: list[dict] = []

    def emit(node, kind, parent=""):
        name = getattr(node, "name", "")
        symbols.append({
            "name": f"{parent}.{name}" if parent else name,
            "kind": kind,
            "lineno": getattr(node, "lineno", 0),
            "end_lineno": getattr(node, "end_lineno", 0),
            "doc": _doc_first_line(node),
            "depends_on": _deps_for(node, alias, module_path),
        })

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            emit(node, "function")
        elif isinstance(node, ast.AsyncFunctionDef):
            emit(node, "async-function")
        elif isinstance(node, ast.ClassDef):
            emit(node, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    emit(sub, "method", parent=node.name)

    return {
        "module": module_path,
        "found": True,
        "line_count": source.count("\n") + 1,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }
