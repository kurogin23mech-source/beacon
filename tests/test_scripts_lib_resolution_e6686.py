"""``scripts/`` の lib 配置規則は 1 箇所に在る、を構造で固定する (ms-133 e-6686).

``scripts/_install_paths.py`` は「install root 配下の lib を探す規則」の唯一の定義
として作られた (PR #738 の保守性レビュー由来)。目的は、規則を変える人が 1 箇所だけ
直して出荷し、**残りの写しが黙って古い規則のまま動く**事故を防ぐこと。ところが
``codex-inbox-hook.py`` と ``codex-halt-check-hook.py`` には逐語の写しが残っていた
(e-6686 で発見・解消)。散文の注意書きでは戻ってくるので、ここで機械的に止める。

あわせて e-6686 の**判断そのもの**も固定する: ``resolve_lib_dir`` は中身の検証を
しない (= ディレクトリの存在だけを見る)。これは未決の先送りではなく確定した判断で、
理由は関数の docstring に書いてある。判断を覆すなら docstring を書き換えることになり、
その時にこのテストが「契約を変えた」と言ってくれる。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SHARED = SCRIPTS / "_install_paths.py"

# 配置規則の中身 = lib が置かれうるサブディレクトリ名。これが共有モジュール以外の
# コードに literal で現れたら、それは規則の写し。
LAYOUT_LITERALS = {"lib", "_bundled_lib"}


def _func_defs(path: Path, name: str) -> "list[ast.FunctionDef]":
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]


def _body_without_docstring(fn: ast.FunctionDef) -> "list[ast.stmt]":
    """関数本体から docstring だけを外す。

    docstring は ``Constant`` なので、素の文字列走査だと説明文中の
    ``_bundled_lib`` を「規則の写し」と誤検知する (= 偽陽性でガードが無効化される)。
    """
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _delegates_to_shared(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module == "_install_paths":
            if any(a.name == "resolve_lib_dir" for a in node.names):
                return True
    return False


def _layout_literals_in_code(fn: ast.FunctionDef) -> "set[str]":
    found: set[str] = set()
    for stmt in _body_without_docstring(fn):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in LAYOUT_LITERALS:
                    found.add(node.value)
    return found


def _scripts_defining_resolver() -> "list[Path]":
    out = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path == SHARED:
            continue
        if _func_defs(path, "_resolve_lib_dir"):
            out.append(path)
    return out


def test_some_script_defines_the_wrapper():
    """テスト自身が空振りしていないこと (= 対象 0 件なら以下は無意味に緑)。"""
    found = _scripts_defining_resolver()
    assert found, "scripts/ に _resolve_lib_dir を定義するスクリプトが 1 つも無い"


@pytest.mark.parametrize(
    "script", _scripts_defining_resolver(), ids=lambda p: p.name
)
def test_wrapper_delegates_and_keeps_no_private_copy(script: Path):
    """各 ``_resolve_lib_dir`` は共有リゾルバに委譲し、規則を自前に持たない。"""
    (fn,) = _func_defs(script, "_resolve_lib_dir")
    assert _delegates_to_shared(fn), (
        f"{script.name}:_resolve_lib_dir が _install_paths.resolve_lib_dir へ委譲して "
        "いません。配置規則を自前に書くと、規則変更時にここだけ古い挙動で残ります"
    )
    leaked = _layout_literals_in_code(fn)
    assert not leaked, (
        f"{script.name}:_resolve_lib_dir のコードに配置規則の literal {sorted(leaked)} が "
        "残っています (docstring での言及は可)。規則は _install_paths だけが持ちます"
    )


def test_shared_resolver_owns_the_layout_literals():
    """規則の literal は共有モジュール側に在る (= 消えて空振りしていない)。"""
    src = SHARED.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(SHARED))
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert LAYOUT_LITERALS <= literals, (
        "_install_paths が配置規則の literal を持っていません。"
        f"不足: {sorted(LAYOUT_LITERALS - literals)}"
    )


def test_resolver_still_takes_directory_existence_only():
    """e-6686 の確定判断: 中身の検証はしない (ディレクトリの存在だけを見る)。

    空のディレクトリでもそれを返す = 目印ファイルを要求しない、が現行契約。
    検証を入れるなら docstring の判断を覆すことになるので、このテストが先に落ちる。
    """
    import sys
    sys.path.insert(0, str(SCRIPTS))
    try:
        from _install_paths import resolve_lib_dir
    finally:
        sys.path.remove(str(SCRIPTS))
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "lib").mkdir()  # 空 = commands.py も codex_session.py も無い
        assert resolve_lib_dir(root) == root / "lib"
        # 候補が無ければ第 1 候補を返す (例外を投げない)
        empty = root / "nothing"
        assert resolve_lib_dir(empty) == empty / "lib"


def test_guard_goes_red_on_a_reintroduced_private_copy(tmp_path):
    """test-the-test: 写しを書き戻したらこのガードが赤くなること。

    緩い部分文字列照合だと docstring の言及で素通りしたり、逆に説明文を誤検知して
    無効化されたりする。両方向を実際に注入して確かめる。
    """
    copy_back = tmp_path / "fake-hook.py"
    copy_back.write_text(
        "from pathlib import Path\n"
        "def _resolve_lib_dir(install_root: Path) -> Path:\n"
        '    """規則を自前に持つ悪い形。"""\n'
        '    lib_dir = install_root / "lib"\n'
        "    if lib_dir.is_dir():\n"
        "        return lib_dir\n"
        '    return install_root / "_bundled_lib"\n',
        encoding="utf-8",
    )
    (bad,) = _func_defs(copy_back, "_resolve_lib_dir")
    assert not _delegates_to_shared(bad), "委譲していない形を委譲済みと誤判定した"
    assert _layout_literals_in_code(bad) == LAYOUT_LITERALS

    docstring_only = tmp_path / "good-hook.py"
    docstring_only.write_text(
        "from pathlib import Path\n"
        "def _resolve_lib_dir(install_root: Path) -> Path:\n"
        '    """lib と _bundled_lib の違いを説明する docstring。"""\n'
        "    from _install_paths import resolve_lib_dir\n"
        "    return resolve_lib_dir(install_root)\n",
        encoding="utf-8",
    )
    (good,) = _func_defs(docstring_only, "_resolve_lib_dir")
    assert _delegates_to_shared(good)
    assert _layout_literals_in_code(good) == set(), (
        "docstring 中の言及を規則の写しと誤検知した (= 偽陽性でガードが使われなくなる)"
    )
