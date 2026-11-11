"""git の読み取りが「叩いた場所」の文脈を見ること (ms-160 / push record の誤記録)。

なにが起きていたか
------------------
``bin/beacon`` は ``lib/commands.py`` を呼ぶ前に、プロジェクトのルート (= ``.beacon/``
が在る親リポジトリ) へ移動する。``lib/git_read_port.py`` は ``git`` に ``-C`` を渡さず
プロセスの cwd をそのまま使っていたため、**worktree から叩いても親のブランチ・HEAD を
読んでいた**。

実測 (2026-11-10): worktree で ``fix/log-gate-ignores-beacon-churn`` の f163f2cc を
push した直後に ``beacon push record`` を叩くと、別セッションが作業中の
``ms-185-residue-cleanup 0a3ba117..0a3ba117 (0 commits)`` が記録された。push の記録が
別ブランチの、コミット 0 件として残る。

``bin/beacon`` は呼び出し元の cwd を ``BEACON_INVOCATION_CWD`` に export していて、
``bin/lib/cmd_log.sh`` は ``git -C "$_git_cwd"`` でそれを使っている (e-1227 / ms-17)。
python 側の読み取りだけがその規約から外れていた。

試験の形
--------
2 つの git リポジトリを作り、**プロセスの cwd を A に置いたまま**
``BEACON_INVOCATION_CWD`` を B に向ける。port が B の事実を返せば直っている。
A を返したら、プロセス cwd を見ている (= 直っていない)。

port の 6 メソッド全部を測る。1 つでも ``cwd`` を渡し忘れると、その口だけが親を
読み続けるため (呼び出し側は 12 箇所あり、どれがどのメソッドを使うかは変わる)。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import git_read_port  # noqa: E402


def _init_repo(path, branch, subject, user_name):
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], capture_output=True, check=True)
    run("init", "-q", "-b", branch)
    run("config", "user.email", "t@example.com")
    run("config", "user.name", user_name)
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", subject)
    return path


@pytest.fixture()
def two_repos(tmp_path, monkeypatch):
    """A = プロセスの cwd (= bin/beacon が移動した先に相当), B = 叩いた場所。"""
    a = _init_repo(tmp_path / "parent", "parent-branch", "parent commit", "Parent User")
    b = _init_repo(tmp_path / "worktree", "worker-branch", "worker commit", "Worker User")
    monkeypatch.chdir(a)
    return a, b


def test_all_six_methods_read_the_invocation_cwd(two_repos, monkeypatch):
    """6 メソッドすべてが B (叩いた場所) の事実を返す。"""
    a, b = two_repos
    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(b))

    assert git_read_port.current_branch() == "worker-branch"
    assert git_read_port.branch_show_current() == "worker-branch"

    b_head = subprocess.check_output(
        ["git", "-C", str(b), "rev-parse", "--short", "HEAD"], text=True).strip()
    assert git_read_port.rev_parse_short("HEAD") == b_head

    assert git_read_port.log_subjects(5) == ["worker commit"]
    assert git_read_port.config_user_name() == "Worker User"

    commits = git_read_port.log_commits("", b_head, 50)
    assert commits, "B の履歴が読めていない"
    assert any("worker commit" in str(c) for c in commits), commits


def test_without_the_env_var_the_process_cwd_is_used(two_repos, monkeypatch):
    """環境変数が無ければプロセスの cwd を継承する (既存の挙動を変えない)。

    ``bin/beacon`` を経由しない直接 import や、cwd を自分で管理している試験が
    壊れないようにするための契約。
    """
    a, b = two_repos
    monkeypatch.delenv("BEACON_INVOCATION_CWD", raising=False)
    assert git_read_port.current_branch() == "parent-branch"
    assert git_read_port.config_user_name() == "Parent User"


def test_nonexistent_invocation_cwd_falls_back(two_repos, monkeypatch, tmp_path):
    """指された先が無ければプロセスの cwd に落ちる (例外で止めない)。

    環境変数が古い / 消えた worktree を指していることはある。そこで git が
    「そんなディレクトリは無い」で落ちると、push 記録そのものが失敗する。
    """
    a, b = two_repos
    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(tmp_path / "gone"))
    assert git_read_port.current_branch() == "parent-branch"


def test_every_subprocess_call_passes_cwd():
    """port 内の git 呼び出しが 1 つも cwd を渡し忘れていないこと。

    呼び出し側は 12 箇所あり、どのメソッドを使うかは口ごとに違う。1 つ渡し忘れると
    その口だけが親を読み続け、しかも他の口が直っているので気づきにくい。

    初版は「呼び出し行から 8 行」の窓で ``cwd=`` を探していた。窓が隣のメソッドへ
    はみ出すので、**1 メソッドだけ渡し忘れる変異を検知できなかった** (実測: 変異を
    入れても 4 件すべて緑)。行の窓ではなく構文木で、その呼び出し自身の引数を見る。
    """
    import ast

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "lib", "git_read_port.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute)
                and fn.attr in ("check_output", "run")
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "subprocess"):
            continue
        if not any(kw.arg == "cwd" for kw in node.keywords):
            missing.append(node.lineno)
    assert missing == [], (
        "cwd を渡していない subprocess 呼び出しが lib/git_read_port.py の行 %r にある。"
        "1 つ渡し忘れると、その口だけが親リポジトリを読み続ける" % missing
    )
    # 試験自身が何も見ていない緑でないことの確認 (呼び出しは 6 つある)
    found = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("check_output", "run")
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
    )
    assert found >= 6, f"git 呼び出しを {found} 件しか見つけられていない (構造が変わった?)"
