"""レビュー実施の記録が PR のない作業にもできることの試験 (ms-166 e-6918)。

何が起きていたか
----------------
レビュー実施を記録して承認・取り込みの関門を外す動詞 ``beacon review done`` が
**PR 番号しか受け取らなかった**。gate を扱う 2 兄弟で対象の指定方法が割れていた:

* ``review context`` は ``--pr`` / ``--diff-ref`` / ``--target``
* ``review skip``   は ``--pr`` / ``--target``
* ``review done``   は ``--pr`` のみ

つまり PR を作る前に branch でレビューを走らせると、**関門を外す経路が存在しない**。
別プロジェクトのセッションが実際にこれを踏み、後から PR を作って帳尻を合わせた。
使う側は「skip ならターゲット指定できるのに done はできない」という理由のない
非対称に当たる。

調査中にこちらで 2 件を追加で実測した:

1. ``bin/beacon`` の ``review done`` の引数処理が ``*) shift ;;`` で位置引数を黙って
   捨てるため ``beacon review done ms-166`` が無言で何もしなかった (兄弟の
   ``context`` / ``skip`` はどちらも ``_guard_positional`` を持つ)。既出の e-6762
   (``beacon phase list`` が余分な位置引数を黙って飲む) と同じ型。
2. ``beacon review done --help`` は「run 'beacon review --help' for usage」と案内する
   のに、``beacon review --help`` は context の 1 行しか出さず done も skip も載せ
   なかった。**案内に従うと情報が無い場所に着く閉じたループ**。

この file が留めるもの
----------------------
1. ``review done`` が ``--pr`` / ``--target`` の両方で対象を指せること (AC1)
2. 余分な位置引数を黙って捨てず拒否すること (AC2)
3. help の単一ソースが done / skip を載せ、案内の行き先に実際に情報が在ること (AC3)
4. 上の 3 点が戻したときに赤くなる形で機械に測られること (AC4) —— とくに
   「help が宣伝する旗」と「実パーサが受け付ける旗」を **構造抽出して集合で比べる**
   (部分一致では help 文や JSON タグで偽の緑になる)
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

BEACON = os.path.join(ROOT, "bin", "beacon")


def _project(tmp_path, bindings=("philosophy", "attainment"), target="ms-9"):
    """使い捨てプロジェクト + target の review-due トリガーを置く。

    cwd をこの repo にしないこと: ローカルには ``.beacon/`` が在るが CI の
    チェックアウトには無いので、``ensure_project`` が先に落ちて測りたい経路に
    到達しない (e-5980 で実際に踏んだ非 hermetic の形)。
    """
    (tmp_path / ".beacon" / "triggers").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "next_entry_id": 1,
        "milestones": [{"id": target, "title": "t", "status": "in_progress",
                        "progress": 0, "entries": []}],
    }), encoding="utf-8")
    if bindings is not None:
        (tmp_path / ".beacon" / "triggers"
         / f"review-due-{target}.json").write_text(json.dumps({
             "name": f"review-due-{target}", "kind": "review-due",
             "target_id": target, "bindings": list(bindings), "message": "m",
         }), encoding="utf-8")
    return tmp_path


def _run(proj, *args):
    return subprocess.run([BEACON, "review", *args],
                          capture_output=True, text=True, cwd=str(proj))


# --- AC1: 対象を --pr / --target のどちらでも指せる ------------------------

def test_done_clears_a_targets_obligation(tmp_path):
    """``--target`` で target の義務を解消できること (= PR を作らずに関門を外せる)。"""
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "義務を解消しました" in proc.stdout, proc.stdout


def test_done_clears_only_the_named_type(tmp_path):
    """1 種別だけ解消し、同じ節目の他の binding を巻き込まないこと。

    target の review-due トリガーは **1 ファイルに全 binding** を持つので、
    ファイルごと消すと他の義務も黙って免除される (e-4124 で skip 側が踏んだ形)。
    """
    proj = _project(tmp_path, bindings=("philosophy", "attainment"))
    _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    left = json.loads((proj / ".beacon" / "triggers"
                       / "review-due-ms-9.json").read_text(encoding="utf-8"))
    assert left["bindings"] == ["attainment"], left


def test_done_says_so_when_the_target_owed_nothing(tmp_path):
    """義務が元から無ければ、解消したと名乗らず非 0 で終えること。

    「何も起きなかった」を「関門が外れた」と取り違えさせない (PR 側と同じ規律)。
    """
    proj = _project(tmp_path, bindings=("attainment",))
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "元から在りませんでした" in proc.stderr, proc.stderr
    assert "義務を解消しました" not in proc.stdout, proc.stdout


def test_done_refuses_both_pr_and_target(tmp_path):
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "ax", "--pr", "1", "--target", "ms-9")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "同時に指定できません" in proc.stderr, proc.stderr


def test_done_refuses_an_unknown_type_for_a_target(tmp_path):
    """typo が義務を消さないのに成功に見える silent no-op を作らないこと。"""
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "bogus", "--target", "ms-9")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "未知の review type" in proc.stderr, proc.stderr


def test_done_discloses_that_the_cross_worktree_ledger_is_pr_scoped(tmp_path):
    """残る限界を名乗ること。

    作業フォルダ横断の台帳 (ms-166 e-6899) は PR 番号を鍵にしているので、fork で
    打った target のレビューは親から実施済みとして見えない。黙って PR 側と同等の
    ように見せない。
    """
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert "この作業フォルダ限り" in proc.stderr, proc.stderr


# --- AC2: 余分な位置引数を黙って捨てない -----------------------------------

@pytest.mark.parametrize("verb", ["done", "skip", "context"])
def test_a_stray_positional_is_refused_by_every_sibling(tmp_path, verb):
    """3 兄弟すべてが位置引数を拒否すること。

    ``review done`` だけ ``*) shift ;;`` で黙って捨てており、
    ``beacon review done ms-166`` が無言で何もしなかった。兄弟と揃える。
    """
    proj = _project(tmp_path)
    proc = _run(proj, verb, "ms-166")
    assert proc.returncode != 0, f"{verb}: 位置引数を黙って受けた"
    combined = proc.stdout + proc.stderr
    assert "ms-166" in combined, f"{verb}: 何が余分だったか名指ししていない"


def test_the_positional_guard_does_not_call_a_bare_word_a_flag(tmp_path):
    """非 flag の位置引数に「flag のようだ」と **事実でない診断** を返さないこと。

    共有の ``_guard_positional`` は docstring 上「``-?*`` に一致した flag 風
    トークン専用」だが、review 3 兄弟は非 flag の位置引数にもこれを使う。その経路で
    「looks like a flag」と出すと、利用者は旗の綴りを疑い本当の原因 (= このコマンドは
    flag のみを取る) に辿り着けない。
    """
    proj = _project(tmp_path)
    bare = _run(proj, "done", "ms-166")
    assert "looks like a flag" not in bare.stderr, bare.stderr
    assert "位置引数" in bare.stderr, bare.stderr
    # flag 風トークンでは従来の文面が正しいので、そちらは保つ
    flagish = _run(proj, "done", "--bogus")
    assert flagish.returncode != 0
    assert ("not a valid flag" in flagish.stderr
            or "looks like a flag" in flagish.stderr), flagish.stderr


# --- AC3: 案内の行き先に実際に情報が在る -----------------------------------

@pytest.mark.parametrize("verb", ["context", "done", "skip"])
def test_the_family_help_lists_every_sibling(verb):
    """``beacon review --help`` が 3 兄弟すべてを載せること。

    done / skip が載っていなかったため、``review done --help`` の
    「`beacon review --help` を見よ」という案内が **情報の無い場所** を指していた。
    """
    proc = subprocess.run([BEACON, "review", "--help"],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"beacon review {verb}" in proc.stdout, (
        f"review --help に {verb} が載っていない:\n" + proc.stdout)


@pytest.mark.parametrize("verb", ["context", "done", "skip"])
def test_each_sibling_help_explains_itself(verb):
    """各 sub-verb の ``--help`` が自分の行を出すこと (親に丸投げしない)。"""
    proc = subprocess.run([BEACON, "review", verb, "--help"],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "run 'beacon review --help' for usage" not in combined, (
        f"{verb} が親に丸投げしている (案内の行き先に情報が無い形):\n" + combined)
    assert f"beacon review {verb}" in combined, combined


# --- AC4: help が宣伝する旗と実パーサが受ける旗を集合で比べる ---------------

def _registry_flags(sub_verb: str) -> set:
    import commands
    rows = [r for r in commands._help_registry()
            if r["command"].split()[:3] == ["beacon", "review", sub_verb]]
    assert len(rows) == 1, f"registry の {sub_verb} の行が 1 つでない: {len(rows)}"
    # "--pr <n>" → "--pr"。旗の名前だけを語として取り出す。
    out = {f.split()[0] for f in rows[0]["flags"] if f.startswith("--")}
    # command 欄に埋め込まれた必須旗 (例 "--type <...>") も宣伝に含まれる
    out |= {t for t in rows[0]["command"].split() if t.startswith("--")}
    return out


def _case_pattern_flags(block: str) -> set:
    """``case`` のパターン行 (``--xxx)`` / ``--a|--b)``) から旗名だけを拾う。

    部分一致ではなくパターン行に限定する。help 文 / Usage 文字列 / コメントに
    現れる旗名に反応しない (この repo で何度も踏んだ緩い一致を避ける)。
    """
    flags = set()
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^((?:--[A-Za-z0-9-]+\|?)+)\)", stripped)
        if m:
            flags |= {t for t in m.group(1).split("|") if t.startswith("--")}
    return flags


def _bash_accepted_flags(sub_verb: str) -> set:
    """その sub-verb の実パーサが受ける旗を **構造で** 取る。

    **委譲を追う**: ``bin/beacon`` の arm は ``context)`` のように
    ``cmd_review_context "$@"`` へ委譲していることがあり、その場合 旗の ``case`` は
    ``bin/lib/cmd_review.sh`` 側に在る。bin/beacon だけを読む版は context の旗を
    1 つも拾えず、**空集合 ⊇ 宣伝** で偽の緑になるところだった (母集団が空でない
    ことを assert していたので捕まった)。
    """
    with open(BEACON, encoding="utf-8") as f:
        src = f.read()
    start = src.index(f"\n            {sub_verb})")
    # arm は **その行から** 始める (`context)  shift 2; cmd_review_context "$@" ;;`
    # のような 1 行 arm は委譲が同じ行に在るので、次行から切ると空になる)。
    # 次の arm の検索だけ 2 行目以降から行う (自分の行に一致しないように)。
    head = src.index("\n", start + 1) + 1
    nxt = re.search(r"^            [a-z*][a-z0-9|_*-]*\)", src[head:], re.M)
    arm = src[start + 1: head + nxt.start()] if nxt else src[start + 1:]
    flags = _case_pattern_flags(arm)
    if flags:
        return flags
    # 委譲先 (sourced の cmd_review_<verb>) を読む
    m = re.search(r"cmd_review_([a-z_]+)", arm)
    assert m, f"{sub_verb}: 旗も委譲も見つからない:\n" + arm[:300]
    fn = f"cmd_review_{m.group(1)}"
    with open(os.path.join(ROOT, "bin", "lib", "cmd_review.sh"),
              encoding="utf-8") as f:
        sh = f.read()
    fn_at = sh.index(f"{fn}() {{")
    return _case_pattern_flags(sh[fn_at:])


@pytest.mark.parametrize("sub_verb", ["context", "done", "skip"])
def test_the_help_advertises_exactly_the_flags_the_parser_accepts(sub_verb):
    """help が宣伝する旗 ⊆ 実パーサが受ける旗 であること (AC4)。

    **集合で比べる。** 部分一致で「help の文中に旗名が在るか」を見る版は、help 文や
    JSON タグに同じ綴りが出るだけで素通りする (この repo で何度も踏んだ形)。
    宣伝していて受け付けない旗は、利用者が「指定したのに効かない」に当たる
    (既存の check-cli-help-drift が同じ観点で全 CLI を見ているので、ここは review
    family の 3 兄弟に絞った近接の試験)。
    """
    advertised = _registry_flags(sub_verb)
    accepted = _bash_accepted_flags(sub_verb)
    assert accepted, f"{sub_verb} の arm から旗を取れていない (抽出の誤り)"
    phantom = advertised - accepted
    assert phantom == set(), (
        f"review {sub_verb}: help が宣伝しているのにパーサが受けない旗 "
        f"{sorted(phantom)} (受ける旗: {sorted(accepted)})")


def test_done_and_skip_accept_the_same_target_selectors():
    """gate を扱う 2 兄弟が **同じ形** で対象を指せること (AC1 の構造側)。

    「skip ならターゲット指定できるのに done はできない」という理由のない非対称を
    塞いだことを、旗の集合で固定する。
    """
    done = _bash_accepted_flags("done")
    skip = _bash_accepted_flags("skip")
    for selector in ("--pr", "--target"):
        assert selector in done, f"done が {selector} を受けない"
        assert selector in skip, f"skip が {selector} を受けない"


def test_the_python_side_reads_the_target_the_bash_side_passes():
    """bash が渡す env と python が読む env が一致すること。

    片方だけ直すと旗を受け取って黙って無視する形 (= silent no-op) になる。
    """
    with open(BEACON, encoding="utf-8") as f:
        src = f.read()
    arm_at = src.index("\n            done)")
    arm = src[arm_at: src.index("review_done ;;", arm_at)]
    assert "BEACON_TARGET_ID" in arm, "bash が BEACON_TARGET_ID を渡していない"
    import commands
    py = inspect.getsource(commands.cmd_review_done)
    assert "BEACON_TARGET_ID" in py, "python が BEACON_TARGET_ID を読んでいない"


def test_the_target_branch_measures_the_obligation_before_clearing_it():
    """義務の有無を **消す前に** 数えていること。

    後で数えると「元から無かった」と「いま解消した」を区別できず、報告が嘘になる
    (PR 側で一度踏んだ形)。構文木で呼び出しの順序を固定する。
    """
    import commands
    tree = ast.parse(inspect.getsource(commands._emit_review_done_for_target))
    peek_at = clear_at = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", "")
        if name == "_target_review_due_has_binding" and peek_at is None:
            peek_at = node.lineno
        if name == "_remove_binding_from_target_review_due" and clear_at is None:
            clear_at = node.lineno
    assert peek_at is not None and clear_at is not None, (peek_at, clear_at)
    assert peek_at < clear_at, (
        f"義務の有無を消した後に数えている (peek 行 {peek_at} > clear 行 {clear_at})")


def test_no_subprocess_here_runs_in_the_repo_itself():
    """この file の subprocess が使い捨てプロジェクトか読み取り専用の help だけで
    この repo を cwd にしないこと。

    e-5980 で踏んだ非 hermetic (ローカル緑 / CI 赤) の再発防止。``--help`` は
    project を要求しない読み取り専用なので ROOT を許す。
    """
    with open(__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    SPAWN = {"run", "call", "check_call", "check_output", "Popen"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") not in SPAWN:
            continue
        cwd = next((k.value for k in node.keywords if k.arg == "cwd"), None)
        if cwd is None:
            offenders.append(f"line {node.lineno}: cwd 未指定")
            continue
        names = {n.id for n in ast.walk(cwd) if isinstance(n, ast.Name)}
        if names <= {"proj", "tmp_path", "str"}:
            continue
        # ROOT を使うのは --help (project 不要の読み取り) に限る
        args = node.args[0] if node.args else None
        argv = ast.unparse(args) if args is not None else ""
        if names == {"ROOT"} and "--help" in argv:
            continue
        offenders.append(f"line {node.lineno}: cwd={ast.unparse(cwd)} argv={argv}")
    assert offenders == [], (
        "使い捨てプロジェクト以外を cwd にして beacon を叩いている: "
        + repr(offenders))
