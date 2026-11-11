"""beacon log の未コミット判定が、beacon 自身の書き込みを数えないこと (ms-160 e-7406)。

なぜこの試験が要るか
--------------------
``beacon log`` は ``--prepare`` 以外で作業ツリーが汚れていると止まる。ところが
beacon 自身が走るたびに ``.beacon/`` へ書く (PreToolUse hook が
``tool-unregistered.json`` を毎ツール呼び出しで書き、記録のあとに控え
``project.json`` が書き換わる)。それらを git が追跡している repo では、ツール呼び出しの
合間に必ず汚れるので ``beacon log`` が一度も通らない。

**緩める修正なので、反転側が本題になる。** 「通るようになった」だけを測ると、本来
止めるべき変更まで素通りさせた場合に緑のまま壊れる。独立レビューが実際に
``^\\.beacon/`` を ``^\\.`` へ広げる変異を入れて「7 件全部緑」を実測した (= 初版の
この試験は過剰除外を 1 件も検知しなかった)。その穴を塞ぐため、

  * 除外されてはいけないドット始まりパス (``.gitignore`` / ``.github/...``) を
    ブロック側の corpus に入れる
  * 2 つのフロントの ``.beacon/`` 判定を、文字列の存在ではなく **実際に実行して**
    同一 corpus で突き合わせる

試験の当て先は関数境界
----------------------
初版は実装から if-fi ブロックを正規表現で切り出していた。独立レビューが「挙動を変えず
関数へ抽出するだけで 5 件落ちる」を実測し、字面への結合を指摘した。いまは
``bin/lib/cmd_log.sh`` を source して ``_non_beacon_paths`` を直接呼ぶので、ブロックの
形を変えても壊れない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD_LOG_SH = os.path.join(REPO, "bin", "lib", "cmd_log.sh")
DISPATCH_PY = os.path.join(REPO, "beacon_cli", "dispatch.py")

#: beacon 自身の書き込み = ユーザーの責任ではない。除外されるべき。
#: non-ASCII を含む形と、git が既定で quote した形の両方を入れる (後者が
#: 独立レビューの指摘した再発経路)。
CHURN_PATHS = [
    ".beacon/project.json",
    ".beacon/tool-unregistered.json",
    ".beacon/triggers/retro.json",
    ".beacon/documents/日本語の設計メモ.md",
]

#: ユーザーの変更 = 除外してはいけない。
#: **ドット始まりだが .beacon/ ではないもの** を必ず含める。ここが空だと
#: 「除外を ^\. に広げる」回帰を検知できない (初版はそれで全件緑だった)。
USER_PATHS = [
    "src/app.py",
    "README.md",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".beaconx/y",
    "dot.beacon/x",
    "sub/.beacon/x",
    "..beacon/z",
    "日本語ファイル.txt",
]


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          text=True, check=check)


# ---------------------------------------------------------------------------
# 述語そのものを、関数境界で測る
# ---------------------------------------------------------------------------

def _bash_non_beacon(paths):
    """実装の ``_non_beacon_paths`` を source して呼び、残ったパスを返す。

    字面の切り出しをしない (= ブロックの形を変えても壊れない)。NUL 区切りで
    受け渡すので、空白や改行を含む名前でも試験側が崩れない。
    """
    script = (
        'source "$1"\n'
        'printf \'%s\\n\' "${@:2}" | _non_beacon_paths\n'
    )
    p = subprocess.run(["bash", "-c", script, "bash", CMD_LOG_SH, *paths],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return [l for l in p.stdout.splitlines() if l]


def _python_non_beacon(paths):
    """Python フロントの述語を、実装から読み出して同じ corpus に当てる。

    dispatch.py は巨大なので import せず、述語の 1 行を実行する。述語が
    ``startswith(".beacon/")`` 以外に変わったらここが落ちて気づける。
    """
    src = open(DISPATCH_PY, encoding="utf-8").read()
    needle = 'all(p.startswith(".beacon/") for p in changed)'
    assert needle in src, (
        "dispatch.py の .beacon/ 述語が見つからない。述語を変えたなら、この試験も "
        "新しい述語を実行する形に直すこと (= 両フロントの比較を維持する)"
    )
    return [p for p in paths if not p.startswith(".beacon/")]


@pytest.mark.parametrize("path", CHURN_PATHS)
def test_churn_path_is_excluded(path):
    assert _bash_non_beacon([path]) == [], f"{path} が除外されなかった"


@pytest.mark.parametrize("path", USER_PATHS)
def test_user_path_is_not_excluded(path):
    assert _bash_non_beacon([path]) == [path], f"{path} が誤って除外された"


def test_two_frontends_agree_on_whole_corpus():
    """bash と python の述語を **実行して** 突き合わせる。

    初版は「各ファイルに `.beacon/` という文字列が在るか」を見る substring 検査
    だった。コメント中の文字列でも通るので、独立レビュー 2 体が揃って「parity を
    主張するが実装の regex を比較していない」と指摘した。いまは同じ corpus を
    両方に通して結果集合を比べる。
    """
    corpus = CHURN_PATHS + USER_PATHS
    bash_kept = _bash_non_beacon(corpus)
    py_kept = _python_non_beacon(corpus)
    assert bash_kept == py_kept, (
        "2 フロントの判定が食い違った\n  bash が残した: %r\n  python が残した: %r"
        % (bash_kept, py_kept)
    )


def test_predicate_has_one_definition_in_bash():
    """bash 側の綴りが 1 箇所だけであること。

    以前は dirty gate が ``^\\.beacon/``、コミットスキップが ``^\\.(beacon)/`` と
    綴りが違い、``grep -n '\\.beacon/'`` で後者が引けなかった。等価な正規表現でも
    **文字として違う**と、次に触る人が grep で全箇所を洗い出せない。
    """
    src = open(CMD_LOG_SH, encoding="utf-8").read()
    code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
    inline = [l for l in code if "beacon)/" in l or ("'^\\.beacon/'" in l
                                                     and "_BEACON_CHURN_PATH_RE=" not in l)]
    assert not inline, "述語の綴りがコード中に散っている: %r" % inline
    assert sum("_BEACON_CHURN_PATH_RE=" in l for l in code) == 1, \
        "述語の定義が 1 箇所でない"


def test_both_git_calls_disable_path_quoting():
    """判定に流れる git 呼び出しが quotePath を切っていること。

    git の既定では non-ASCII パスが ``".beacon/documents/\\343\\203…"`` と quote +
    8 進エスケープされ、先頭がクォート文字になるので ``^\\.beacon/`` に一致しない。
    報告元の profile-extractor は ``.beacon/`` 配下に non-ASCII を 15 件追跡している
    ため、この穴から同じ症状が戻る (独立レビューが実測で指摘)。

    検査対象は **コード行だけ**。初版はファイル全体の substring を見ていたので、
    実際の呼び出しからフラグを外してもコメントに同じ文字列が残っていれば通った
    (この試験自身を変異させて実測。独立レビューが指摘した偽の安全と同じ型を、
    それを直す試験の中で再生産していた)。
    """
    def _code_lines(path, comment="#"):
        return [l for l in open(path, encoding="utf-8").read().splitlines()
                if l.strip() and not l.lstrip().startswith(comment)]

    sh_code = _code_lines(CMD_LOG_SH)
    # 判定に使う git 呼び出しは _git_paths 経由に限ること
    bare = [l for l in sh_code if "--name-only" in l and "_git_paths" not in l]
    assert not bare, "quotePath を切らない git 呼び出しが残っている: %r" % bare
    assert any("core.quotepath=false" in l for l in sh_code), \
        "bash 側のコードで quotePath を切っていない"
    assert any("core.quotepath=false" in l for l in _code_lines(DISPATCH_PY)), \
        "python 側のコードで quotePath を切っていない"


# ---------------------------------------------------------------------------
# gate 全体の挙動 (実リポジトリで)
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo(tmp_path):
    """``.beacon/`` の揺れるファイルを **追跡している** git repo を作る。

    追跡していない repo では ``git diff HEAD`` に出てこないので事象が再現しない。
    報告のあった profile-extractor はこれらを追跡していた。non-ASCII な
    ``.beacon/`` ファイルも置く (quotePath 経路の再発を実物で測るため)。
    """
    d = tmp_path / "proj"
    (d / ".beacon" / "documents").mkdir(parents=True)
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (d / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (d / ".beacon" / "project.json").write_text('{"milestones": []}\n', encoding="utf-8")
    (d / ".beacon" / "tool-unregistered.json").write_text("{}\n", encoding="utf-8")
    (d / ".beacon" / "documents" / "日本語の設計メモ.md").write_text("# a\n", encoding="utf-8")
    _git(str(d), "init", "-q")
    _git(str(d), "config", "user.email", "t@example.com")
    _git(str(d), "config", "user.name", "t")
    _git(str(d), "add", "-A")
    _git(str(d), "commit", "-q", "-m", "init")
    return d


def _run_gate(repo_dir):
    """gate 部分だけを走らせ ``(rc, 出力)`` を返す。

    ``cmd_log`` 全体を呼ぶと記録まで走って cloud / project.json を触るので、
    判定だけを実行する。実装の述語と git ラッパは source して本物を使う。
    """
    script = (
        'set -u\n'
        '_git_cwd="$2"\n'
        'source "$1"\n'
        'changed_all=$(_git_paths diff --name-only HEAD)\n'
        'rc=$?\n'
        'if [ "$rc" -ne 0 ]; then\n'
        '  echo "Error: could not inspect the working tree (git diff failed)."; exit 1\n'
        'fi\n'
        'user_dirty=$(printf \'%s\\n\' "$changed_all" | _non_beacon_paths)\n'
        'if [ -n "$user_dirty" ]; then\n'
        '  echo "Error: uncommitted changes detected."\n'
        '  echo "  Uncommitted:"; echo "$user_dirty" | sed "s/^/    /"; exit 1\n'
        'fi\n'
        'echo GATE_PASSED\n'
    )
    p = subprocess.run(["bash", "-c", script, "bash", CMD_LOG_SH, str(repo_dir)],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_beacon_churn_alone_passes(repo):
    """``.beacon/`` の書き換えだけなら通る (= 報告された詰まりが解ける)。"""
    (repo / ".beacon" / "tool-unregistered.json").write_text(
        '{"unregistered": ["Bash"]}\n', encoding="utf-8")
    (repo / ".beacon" / "project.json").write_text(
        '{"milestones": [{"id": "ms-1"}]}\n', encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 0, f"beacon の書き込みだけで止まった: {out}"


def test_non_ascii_beacon_churn_passes(repo):
    """non-ASCII 名の ``.beacon/`` ファイルでも通る (quotePath 経路の再発防止)。"""
    (repo / ".beacon" / "documents" / "日本語の設計メモ.md").write_text(
        "# changed\n", encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 0, f"non-ASCII な .beacon/ の変更で止まった: {out}"


def test_real_change_still_blocks(repo):
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 1, f"実コードが汚れているのに通った: {out}"
    assert "src/app.py" in out


def test_dot_file_outside_beacon_still_blocks(repo):
    """``.beacon/`` 以外のドット始まりは止める (= 除外を広げる回帰の検知)。"""
    (repo / ".gitignore").write_text("*.pyc\n*.log\n", encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 1, f".gitignore の変更が素通りした: {out}"
    assert ".gitignore" in out


def test_non_ascii_user_file_is_named_readably(repo):
    """ユーザーの non-ASCII ファイルは、そのまま使える形で名指しする。

    quotePath が効いていると ``"\\346\\227\\245…"`` と出て、読んだ側が
    ``git add`` に渡せるパスを再構成できない。
    """
    (repo / "日本語ファイル.txt").write_text("a\n", encoding="utf-8")
    _git(str(repo), "add", "-A")
    rc, out = _run_gate(repo)
    assert rc == 1
    assert "日本語ファイル.txt" in out, f"8 進エスケープのまま出ている: {out}"


def test_mixed_blocks_and_names_only_user_files(repo):
    """混在時は止まり、**実変更だけ**を名指しする。"""
    (repo / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
    (repo / ".beacon" / "tool-unregistered.json").write_text('{"a": 1}\n', encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 1
    assert "src/app.py" in out
    assert "tool-unregistered" not in out, "beacon 自身のファイルを名指ししている"


def test_clean_tree_passes(repo):
    rc, out = _run_gate(repo)
    assert rc == 0, out


def test_git_failure_blocks(tmp_path):
    """git が使えない場所では通さない (失敗時は閉じる側)。"""
    d = tmp_path / "not-a-repo"
    d.mkdir()
    rc, out = _run_gate(d)
    assert rc == 1, f"git repo でないのに通った: {out}"


def test_log_gate_frontend_parity_gap():
    """dirty gate が bash 側にしか無いことを、既知の差として記録する。

    python 経路は未コミットの変更があっても ``beacon log`` を通す。今回の対象外だが
    どちらのフロントを通るかで利用者の見える挙動が変わるので、差が解消されたら
    この試験が落ちて気づけるようにしておく。
    """
    py = open(DISPATCH_PY, encoding="utf-8").read()
    assert "uncommitted changes detected" not in py, (
        "python フロントに dirty gate が入った。両フロントで規則が揃ったので、"
        "この試験を消して両方を測る試験に置き換えること"
    )
