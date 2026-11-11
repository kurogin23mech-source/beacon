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

import git_cwd  # noqa: E402
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
    git_cwd.reset_cache()
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


PORTS = ("git_read_port.py", "git_write_port.py")


@pytest.mark.parametrize("port", PORTS)
def test_every_subprocess_call_passes_cwd(port):
    """**両ポート**の git 呼び出しが 1 つも cwd を渡し忘れていないこと。

    初版は読みポートだけを見ていた。独立保守性レビューが実機で、姉妹の
    ``git_write_port`` が取り残されていることを見つけた — ``cmd_deploy_record()``
    は同じ関数の中で両方を呼ぶので、読んだコミット範囲は worktree 基準なのに
    ``git tag`` は親の HEAD に付く。判定が自分と矛盾し、例外も警告も出ない。

    初版はもう 1 つ弱く、「呼び出し行から 8 行」の窓で ``cwd=`` を探していた。窓が
    隣のメソッドへはみ出すので、**1 メソッドだけ渡し忘れる変異を検知できなかった**
    (実測で 4 件すべて緑)。行の窓ではなく構文木で、その呼び出し自身の引数を見る。
    """
    import ast

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib", port)
    tree = ast.parse(open(src_path, encoding="utf-8").read())

    def _git_calls(t):
        for node in ast.walk(t):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, ast.Attribute)
                    and fn.attr in ("run", "check_output", "check_call")
                    and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
                yield node

    calls = list(_git_calls(tree))
    missing, wrong_value = [], []
    for n in calls:
        kw = next((k for k in n.keywords if k.arg == "cwd"), None)
        if kw is None:
            missing.append(n.lineno)
            continue
        # AX レビューの指摘: キーワードの **存在** だけを見ていたので、
        # `cwd=_git_cwd()` を `cwd=None` に変える退行 (= 渡し忘れと機能的に同じ) を
        # 緑のまま通していた。値が共有の解決の呼び出しであることまで見る。
        v = kw.value
        ok = (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
              and v.func.id in ("_git_cwd", "git_cwd"))
        if not ok:
            wrong_value.append((n.lineno, ast.dump(v)[:60]))
    assert missing == [], (
        "%s の行 %r が cwd を渡していない。1 つ渡し忘れると、その口だけが親リポジトリを"
        "読み書きし続ける" % (port, missing))
    assert wrong_value == [], (
        "%s の行 %r で cwd の値が共有の解決 (_git_cwd()) の呼び出しでない。"
        "キーワードは在るが中身が違う形は、渡し忘れと同じ結果になる。"
        "なお `**{...}` のような展開で渡す書き方に変えた場合もここが落ちる — "
        "その時はこの検査を新しい書き方に合わせて直すこと (落ちるのは正しい挙動)"
        % (port, wrong_value))
    # 試験自身が何も見ていない緑でないことの確認
    assert len(calls) >= 3, f"{port} の git 呼び出しを {len(calls)} 件しか見つけられていない"


@pytest.mark.parametrize("port", PORTS)
def test_ports_share_one_resolver(port):
    """両ポートが ``git_cwd`` の 1 つの定義を使っていること。

    片方のポートの中に解決を置くと「もう片方に写す」余地が残り、実際に写し忘れた。
    自前実装が復活したらここが落ちる。
    """
    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib", port)
    src = open(src_path, encoding="utf-8").read()
    assert "from git_cwd import" in src, f"{port} が共有の解決を import していない"
    code = [l for l in src.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert not any("def _git_cwd" in l or "def git_cwd" in l for l in code), \
        f"{port} に解決の自前実装が戻っている"


def test_python_frontend_passes_the_env_var_to_the_child():
    """python フロントが子プロセスに ``BEACON_INVOCATION_CWD`` を渡すこと。

    bash フロント (``bin/beacon``) は relocate の前に export する。python フロント
    (``beacon_cli/dispatch.py``、bash 不在の pipx / Windows では唯一の経路) には
    それが無く、独立レビューが実機で「子の env に載らない」ことを確認した。載らないと
    ``git_cwd()`` は None を返し、worktree から叩いた push / deploy が親のブランチを
    記録し続ける (= e-7423 がまさに直そうとしたバグ)。

    全サブコマンドが通る ``_run_commands_py`` で見る。ここに無いと、新しい verb が
    追加されたときに渡し忘れが再発する。
    """
    import ast

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "beacon_cli", "dispatch.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_run_commands_py"), None)
    assert fn is not None, "_run_commands_py が見つからない (構造が変わった?)"
    body = ast.dump(fn)
    assert "BEACON_INVOCATION_CWD" in body, (
        "_run_commands_py が BEACON_INVOCATION_CWD を子へ渡していない。"
        "bash フロントだけが渡す状態に戻っている")
    assert "get_invocation_cwd" in body, (
        "環境変数の値が get_invocation_cwd() から来ていない。別の真実源が増えている")


def test_non_git_directory_is_not_used(two_repos, monkeypatch, tmp_path, capsys):
    """存在するが git でないディレクトリは使わない (6 メソッドの非対称を消す)。

    独立 AX レビューの実測: ``git config user.name`` はリポジトリ外でもグローバル設定へ
    カスケードするので、``config_user_name()`` だけが例外を投げず **実在する値** を
    返していた。``cmd_push`` の try/except はそれを成功と見なし、無関係な identity を
    記録の ``pushed_by`` に書く。他の 5 つは例外になるので、1 つだけが自分の契約
    (「失敗したら raise する」) を黙って破る形だった。
    """
    a, b = two_repos
    notgit = tmp_path / "notgit"
    notgit.mkdir()
    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(notgit))
    git_cwd.reset_cache()

    # 親 (プロセスの cwd) に落ちる = 6 メソッドが揃った挙動になる
    assert git_read_port.current_branch() == "parent-branch"
    assert git_read_port.config_user_name() == "Parent User", \
        "非 git ディレクトリのグローバル設定を拾っている (非対称が残っている)"


def test_set_but_unusable_is_surfaced(two_repos, monkeypatch, tmp_path, capsys):
    """設定されているのに使えない場合は 1 行出す / 未設定では黙る。

    「override する気が無い」と「override に失敗した」が呼び出し側から区別できないと、
    この修正が直したはずの症状 (叩いた場所と違う場所の git 事実を読む) が別の入力から
    再現し、しかも黙っている。
    """
    a, b = two_repos

    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(tmp_path / "gone"))
    git_cwd.reset_cache()
    git_cwd.git_cwd()
    assert "git の作業ツリーとして使えません" in capsys.readouterr().err

    monkeypatch.delenv("BEACON_INVOCATION_CWD", raising=False)
    git_cwd.reset_cache()
    git_cwd.git_cwd()
    assert capsys.readouterr().err == "", "未設定で警告を出している"


def test_tilde_and_trailing_slash_resolve(two_repos, monkeypatch):
    """``~`` と末尾スラッシュを受ける (呼び出し側が整形しなくてよい)。"""
    a, b = two_repos
    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(b) + "/")
    git_cwd.reset_cache()
    assert git_read_port.current_branch() == "worker-branch"


def test_validation_runs_once_per_value(two_repos, monkeypatch):
    """検証は 1 プロセス 1 回 (6 メソッド x 呼び出し回数ぶん subprocess を増やさない)。"""
    a, b = two_repos
    monkeypatch.setenv("BEACON_INVOCATION_CWD", str(b))
    git_cwd.reset_cache()
    calls = {"n": 0}
    orig = git_cwd._is_work_tree

    def counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(git_cwd, "_is_work_tree", counting)
    for _ in range(5):
        git_cwd.git_cwd()
    assert calls["n"] == 1, f"検証が {calls['n']} 回走っている (記憶が効いていない)"
