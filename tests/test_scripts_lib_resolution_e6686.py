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


# ---------------------------------------------------------------------------
# resolve_lib_dir が検証しないことの前提 = 呼び出し元の失敗挙動を実測で固定する。
# ---------------------------------------------------------------------------
#
# 「検証を入れない」判断の根拠は docstring に書いてあるが、その根拠は *別ファイルの
# 呼び出し元がどう失敗するか* に依存している。散文だけだと、遠くの実装が変わった
# ときに根拠が黙って崩れる (実際に崩れていた: codex-inbox-hook.py の先頭 import
# だけが無防備で、docstring の「hook 2 本は silent no-op」は事実と違った。PR #773
# の独立レビュー 2 体が合意で指摘)。ここで実行して固定する。

HOOKS_THAT_MUST_NEVER_RAISE = ("codex-inbox-hook.py", "codex-halt-check-hook.py")


def _run_hook_with_broken_lib(hook_name: str, tmp_path: Path):
    """空の ``lib/`` だけを持つ install を作り、そこから hook を実行する。"""
    import shutil
    import subprocess
    import sys

    fake = tmp_path / "install"
    (fake / "scripts").mkdir(parents=True)
    (fake / "lib").mkdir()  # 存在するが空 = resolve_lib_dir はこれを返す
    for f in ("_install_paths.py", hook_name):
        shutil.copy(SCRIPTS / f, fake / "scripts" / f)
    return subprocess.run(
        [sys.executable, str(fake / "scripts" / hook_name), "--cwd", str(fake)],
        input="{}", capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("hook_name", HOOKS_THAT_MUST_NEVER_RAISE)
def test_hook_degrades_silently_on_a_broken_lib(hook_name, tmp_path):
    """空/壊れた lib を掴んでも hook は Codex へ例外を抜かない。

    ``bin/hook_bootstrap.py`` が全 hook に課す fail-safe 原則
    ("must degrade to a silent no-op, never raise into the harness") の実行確認。
    これが崩れると ``resolve_lib_dir`` の「検証しない」判断の根拠も崩れる。
    """
    r = _run_hook_with_broken_lib(hook_name, tmp_path)
    assert r.returncode == 0, (
        f"{hook_name} は空 lib で exit {r.returncode} になりました。"
        f"stderr 末尾: {(r.stderr.strip().splitlines() or [''])[-1]}"
    )
    assert "Traceback" not in r.stderr, (
        f"{hook_name} が生の traceback を Codex 側へ出しています:\n{r.stderr[-400:]}"
    )


def test_daemon_is_deliberately_loud_on_a_broken_lib():
    """daemon 側は逆に loud に落ちる — それが docstring の根拠の片側。

    ``codex-receive-loop.py:_import_modules`` が import を包まないことを構造で
    確認する。ここが将来 try/except で包まれたら、docstring の「daemon は読める
    エラーで落ちる」側の根拠が変わるので、その時にこのテストが知らせる。
    """
    daemon = SCRIPTS / "codex-receive-loop.py"
    (fn,) = _func_defs(daemon, "_import_modules")
    guarded = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for s in ast.walk(node):
                if isinstance(s, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(s))
    bare = [
        n for n in ast.walk(fn)
        if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in guarded
    ]
    assert bare, (
        "codex-receive-loop._import_modules の import が全て try/except で包まれて "
        "います。daemon が silent になったなら resolve_lib_dir の docstring の根拠 "
        "(daemon は読めるエラーで落ちる) を書き直してください"
    )
