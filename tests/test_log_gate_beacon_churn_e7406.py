"""beacon log の未コミット判定が、beacon 自身の書き込みを数えないこと (ms-160 e-7406)。

なぜこの試験が要るか
--------------------
``beacon log`` は ``--prepare`` 以外で作業ツリーが汚れていると止まる。ところが
beacon 自身が走るたびに ``.beacon/`` へ書く (PreToolUse hook が
``tool-unregistered.json`` を毎ツール呼び出しで書き、記録のあとに控え
``project.json`` が書き換わる)。それらを git が追跡している repo では、ツール呼び出しの
合間に必ず汚れるので ``beacon log`` が一度も通らない。2026-11-10 に
profile-extractor のセッションから報告され、そこでは台帳の取り込みと finalize を
1 コマンドに畳んで回避していた。

**緩める修正なので、反転側が本題になる。** 「通るようになった」だけを測ると、
本来止めるべき変更まで素通りさせた場合に緑のまま壊れる。実コードの変更を 1 件
混ぜたときに止まることを、同じ強さで測る。

2 つのフロント
--------------
``beacon log`` は ``bin/lib/cmd_log.sh`` (bash) と ``beacon_cli/dispatch.py``
(python) の両方に在る。``.beacon/`` の判定も両方に写しが在るので、両者が同じ答えを
返すことを固定する。なお **dirty gate 自体は bash 側にしか無い** (python 経路は
未コミットでも通る)。これは今回の修正とは別の食い違いなので、ここでは判定規則の
一致だけを固定し、gate の有無の差は test_log_gate_frontend_parity_gap が明示的に
記録する (= 直したつもりで放置しない)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD_LOG_SH = os.path.join(REPO, "bin", "lib", "cmd_log.sh")
DISPATCH_PY = os.path.join(REPO, "beacon_cli", "dispatch.py")


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                          text=True, check=check)


@pytest.fixture()
def repo(tmp_path):
    """``.beacon/`` の揺れるファイルを **追跡している** git repo を作る。

    追跡していない repo では ``git diff HEAD`` に出てこないので、そもそも事象が
    再現しない。報告のあった profile-extractor はこれらを追跡していた。
    """
    d = tmp_path / "proj"
    (d / ".beacon").mkdir(parents=True)
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (d / ".beacon" / "project.json").write_text('{"milestones": []}\n', encoding="utf-8")
    (d / ".beacon" / "tool-unregistered.json").write_text("{}\n", encoding="utf-8")
    _git(str(d), "init", "-q")
    _git(str(d), "config", "user.email", "t@example.com")
    _git(str(d), "config", "user.name", "t")
    _git(str(d), "add", "-A")
    _git(str(d), "commit", "-q", "-m", "init")
    return d


def _run_gate(repo_dir):
    """cmd_log.sh の gate 部分だけを切り出して走らせ、(rc, stdout) を返す。

    ``beacon log`` 全体を呼ぶと記録まで走って cloud / project.json を触るので、
    判定だけを取り出す。切り出しが実装とズレないよう、**ファイルから実際の行を
    読み出して** 実行する (ここに規則を書き写すと、実装を直しても試験が古い規則を
    守り続ける)。
    """
    src = open(CMD_LOG_SH, encoding="utf-8").read()
    m = re.search(
        r'(    if \[ "\$mode" != "prepare" \]; then\n.*?\n    fi\n)',
        src, re.S,
    )
    assert m, "cmd_log.sh から gate ブロックを切り出せなかった (実装の形が変わった?)"
    block = m.group(1)
    script = (
        "set -u\n"
        '_git_cwd="$1"\n'
        'mode="normal"\n'
        + block
        + 'echo GATE_PASSED\n'
    )
    p = subprocess.run(["bash", "-c", script, "bash", str(repo_dir)],
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
    assert "GATE_PASSED" in out


def test_real_change_still_blocks(repo):
    """実コードの未コミット変更は従来どおり止まる (= 緩めすぎていない)。

    これが緑でなければ修正は「全部通す」に退化している。
    """
    (repo / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 1, f"実コードが汚れているのに通った: {out}"
    assert "uncommitted changes detected" in out
    assert "src/app.py" in out, "止めた理由のファイル名が出ていない"


def test_mixed_blocks_and_names_only_user_files(repo):
    """beacon の書き込みと実変更が混在したら止まり、**実変更だけ**を名指しする。

    beacon 側のファイルまで列挙すると、読む人は「何をコミットすればいいのか」を
    判断できない (コミットしても次のツール呼び出しで再び汚れるため)。
    """
    (repo / "src" / "app.py").write_text("x = 3\n", encoding="utf-8")
    (repo / ".beacon" / "tool-unregistered.json").write_text('{"a": 1}\n', encoding="utf-8")
    rc, out = _run_gate(repo)
    assert rc == 1
    assert "src/app.py" in out
    assert "tool-unregistered" not in out, "beacon 自身のファイルを名指ししている"


def test_clean_tree_passes(repo):
    rc, out = _run_gate(repo)
    assert rc == 0, out
    assert "GATE_PASSED" in out


def test_git_failure_blocks(tmp_path):
    """git が使えない場所では通さない (= 失敗時は閉じる側)。

    名前を取り出す形にすると「出力が空」が clean と区別できないので、rc を別に
    測っている。ここが緩むと、git の無い環境で gate が常に素通りする。
    """
    d = tmp_path / "not-a-repo"
    d.mkdir()
    rc, out = _run_gate(d)
    assert rc == 1, f"git repo でないのに通った: {out}"
    assert "git diff failed" in out


# --------------------------------------------------------------------------
# 2 フロントの一致
# --------------------------------------------------------------------------

_BEACON_PATHS = [
    ".beacon/project.json",
    ".beacon/tool-unregistered.json",
    ".beacon/triggers/retro.json",
    ".beacon/changelog.jsonl",
]
_USER_PATHS = [
    "src/app.py",
    "beacon_cli/dispatch.py",
    "README.md",
    "dot.beacon/x",       # 似ているが別物
    "sub/.beacon/x",      # 先頭一致ではない
]


def test_frontends_agree_on_beacon_path_predicate():
    """bash の正規表現と python の述語が、同じ経路集合で同じ答えを返す。

    両方に写しが在るので、片方だけ直したときに食い違う。実物から規則を読み出して
    突き合わせる (試験側に規則を書き写さない)。
    """
    sh = open(CMD_LOG_SH, encoding="utf-8").read()
    py = open(DISPATCH_PY, encoding="utf-8").read()
    assert "'^\\.beacon/'" in sh or "'^\\.(beacon)/'" in sh, \
        "cmd_log.sh に .beacon/ 判定が見当たらない"
    assert 'startswith(".beacon/")' in py, \
        "dispatch.py に .beacon/ 判定が見当たらない"

    for p in _BEACON_PATHS:
        bash_says = subprocess.run(
            ["bash", "-c", "printf '%s\\n' \"$1\" | grep -qE '^\\.beacon/'", "bash", p]
        ).returncode == 0
        py_says = p.startswith(".beacon/")
        assert bash_says is True and py_says is True, f"{p}: bash={bash_says} py={py_says}"

    for p in _USER_PATHS:
        bash_says = subprocess.run(
            ["bash", "-c", "printf '%s\\n' \"$1\" | grep -qE '^\\.beacon/'", "bash", p]
        ).returncode == 0
        py_says = p.startswith(".beacon/")
        assert bash_says is False and py_says is False, f"{p}: bash={bash_says} py={py_says}"


def test_log_gate_frontend_parity_gap():
    """dirty gate が bash 側にしか無いことを、既知の差として記録する。

    直したつもりで放置しないための印。python 経路 (``beacon_cli/dispatch.py``) は
    未コミットの変更があっても ``beacon log`` を通す。今回の修正対象ではないが、
    どちらのフロントを通るかで利用者の見える挙動が変わるので、差が解消されたら
    この試験が落ちて気づけるようにしておく。
    """
    py = open(DISPATCH_PY, encoding="utf-8").read()
    has_dirty_gate = ("uncommitted changes detected" in py)
    assert not has_dirty_gate, (
        "python フロントに dirty gate が入った。両フロントで規則が揃ったので、"
        "この試験を消して両方を測る試験に置き換えること"
    )
