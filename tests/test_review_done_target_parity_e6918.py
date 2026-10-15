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


@pytest.mark.parametrize("setup,code,needle", [
    # トリガーは在るが別の種別だけ発火している
    ("other-type", 3, "含まれていません"),
    # トリガーが無い (節目が発火していない / target id が違う)
    ("absent", 2, "トリガーがありません"),
    # トリガーが在るが読めない (破損)
    ("unreadable", 4, "読めませんでした"),
])
def test_done_distinguishes_why_nothing_was_owed(tmp_path, setup, code, needle):
    """「義務が無い」の **3 つの事情を分ける** こと (独立 AX レビュー misleading)。

    初版は 3 つを同じ文面にまとめ、唯一の対処として「--type / --target の綴りを
    確認」と案内していた。本当の原因がファイルの破損や存在しない target id の
    とき、利用者は綴りを疑って本当の原因に辿り着けない。
    """
    if setup == "other-type":
        proj = _project(tmp_path, bindings=("attainment",))
    elif setup == "absent":
        proj = _project(tmp_path, bindings=None)
    else:
        proj = _project(tmp_path, bindings=None)
        (proj / ".beacon" / "triggers" / "review-due-ms-9.json").write_text(
            "not json", encoding="utf-8")
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert proc.returncode == code, (proc.returncode, proc.stdout + proc.stderr)
    assert needle in proc.stderr, proc.stderr
    # stdout だけを拾う呼び出し側が成功と読まないこと (見出しは分岐の後に出す)
    assert "review-due 解消" not in proc.stdout, proc.stdout


def test_a_call_that_cleared_nothing_does_not_poison_the_ledger(tmp_path):
    """解消していない呼び出しが台帳に「実施済み」を残さないこと。

    **実機で自分で踏んだ**: 採否の判断記録と台帳への書き込みを分岐より前に置いて
    いたため、--type の取り違えで exit 3 になった呼び出しでも台帳に記録が残り、
    次の呼び出しが「冪等な打ち直し」と判定して exit 0 を返した = 一度も解消して
    いないのに実施済みの記録が立つ。順序は「測る → 決める → 書く」。
    """
    proj = _project(tmp_path, bindings=("attainment",))
    first = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert first.returncode == 3, first.stdout + first.stderr
    second = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert second.returncode == 3, (
        "失敗した呼び出しが台帳を汚し、2 回目が成功扱いになった:\n"
        + second.stdout + second.stderr)


def test_an_idempotent_repeat_stays_exit_zero(tmp_path):
    """同じ呼び出しの打ち直しは 0 で終えること (独立 AX レビュー misleading)。

    PR 側は台帳を見て冪等な打ち直しを 0 に保っていたのに、target 側はそれが無く
    **2 回目が exit 3 + 「--type / --target の綴りを確認」** になった。指定は
    正しいのに間違っていると言われる形。台帳の鍵を一般化して同じ扱いにした。
    """
    proj = _project(tmp_path)
    first = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert first.returncode == 0, first.stdout + first.stderr
    again = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "既に記録済み" in again.stdout, again.stdout


def test_done_refuses_repo_with_target(tmp_path):
    """``--repo`` を ``--target`` と一緒に受け取って黙って捨てないこと。

    独立 AX レビュー high: 初版は受け取って形まで検査しながら黙って捨て、成功を
    名乗っていた (= silent no-op)。兄弟の `review skip` は同じ組み合わせを拒否して
    おり、まさにこの課題が閉じようとした done/skip の非対称を自分で作っていた。
    """
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9",
                "--repo", "o/r")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--repo は --pr と一緒に" in proc.stderr, proc.stderr


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


def test_the_cross_worktree_ledger_covers_targets_too(tmp_path):
    """横断台帳が target も載せること (独立 AX レビュー misleading への対応)。

    初版は台帳の鍵を PR 番号のままにし、「fork で打った target のレビューは親から
    見えない」を **限界として開示する** に留めていた。だがその結果、冪等な打ち直しを
    判定する手がかりも無く 2 回目が失敗扱いになった。鍵を ``<種別>-<ref>`` に
    一般化して両方を同じ機構に載せた (``pr-`` 始まりの既存ファイルはそのままの形
    なので移行で読めなくならない)。
    """
    import commands_shared as cs
    proj = _project(tmp_path)
    proc = _run(proj, "done", "--type", "philosophy", "--target", "ms-9")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    name = cs._review_done_marker_name("philosophy", "ms-9", cs.REF_KIND_TARGET)
    assert name == "target-ms-9-philosophy.json", name
    assert "この作業フォルダ限り" not in proc.stderr, (
        "解消した限界をまだ名乗っている:\n" + proc.stderr)


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


def test_the_bare_positional_uses_the_purpose_built_guard(tmp_path):
    """非 flag の位置引数には **専用の helper** を使うこと。

    初版は共有の ``_guard_positional`` を「非 flag でも使えるよう」広げたが、
    **その用途の helper は既に在った** (``_guard_no_positional``)。しかもその関数の
    コメントに「以前 _guard_positional を兼用しようとして 4 箇所を壊した」経緯が
    書いてある。独立 AX レビュー medium。既存のものを使い、広げた分は戻した。
    """
    proj = _project(tmp_path)
    bare = _run(proj, "done", "ms-166")
    assert bare.returncode != 0, bare.stdout
    assert "unexpected argument 'ms-166'" in bare.stderr, bare.stderr
    assert "looks like a flag" not in bare.stderr, (
        "非 flag の語に「flag のようだ」と事実でない診断を返した:\n" + bare.stderr)
    # flag 風トークンは従来の文面 (共有 helper の本来の用途) を保つ
    flagish = _run(proj, "done", "--bogus")
    assert flagish.returncode != 0
    assert ("not a valid flag" in flagish.stderr
            or "looks like a flag" in flagish.stderr), flagish.stderr


def test_the_shared_flag_guard_was_not_widened():
    """共有 ``_guard_positional`` が flag 専用のまま (docstring の前提どおり) であること。

    広げると「flag 風」と「非 flag」の 2 つの仕事が token の形で切り替わる 1 関数に
    なり、この repo が過去に壊した兼用の形に戻る。
    """
    with open(BEACON, encoding="utf-8") as f:
        src = f.read()
    fn = src[src.index("_guard_positional() {"):]
    fn = fn[:fn.index("\n}") + 2]

    # 性質で見る: 分岐の対象が **加工していない** ``"$1"`` であること。
    #
    # 最初の版はこの下の文言 ("位置引数ですが") の不在だけを見ていた。だがそれは
    # 「私が過去に書いた広げ方の綴り」を 1 つ覚えているだけで、**同じ広げ方を別の
    # 書き方でやると素通りする**。反転ハーネスが実測した: case の対象を
    # ``"${1#-}"`` に変えてパターンを ``h|help`` にすると、guard は裸の語にも
    # 反応するようになるのに試験は緑だった (= 偽の安全)。
    #
    # token を剥いだり畳んだりした瞬間に、この guard は「flag 風の token だけを
    # 止める」という docstring の前提を失う。だから加工の有無で測る。
    case_lines = [ln.strip() for ln in fn.splitlines()
                  if ln.strip().startswith("case ")]
    assert case_lines == ['case "$1" in'], (
        "共有 helper の分岐対象が加工された値になっている (flag 専用の前提が"
        "崩れる):\n" + "\n".join(case_lines))
    assert "位置引数ですが" not in fn, (
        "共有 helper に非 flag 用の分岐が残っている:\n" + fn)


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


def test_the_target_branch_measures_before_it_writes():
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
        if name == "_target_review_due_state" and peek_at is None:
            peek_at = node.lineno
        if (name in ("_remove_binding_from_target_review_due",
                     "record_review_done_across_worktrees",
                     "_record_review_adjudication_decision")
                and clear_at is None):
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


def test_the_decision_evidence_tags_a_target_as_a_target(monkeypatch):
    """判断記録の evidence が **対象の種別に合った** タグを持つこと。

    独立 AX レビュー high: target (ms-XX / op-X) を
    ``_record_review_adjudication_decision`` の ``pr_number`` 引数に渡していたため、
    milestone のレビューの採否が ``pr:ms-9`` と **PR 番号のように** 記録されていた。
    ``pr:<n>`` で引く consumer は milestone の記録まで拾い、``target:<id>`` で引く
    consumer はこれらを 1 件も拾えない。兄弟の ``review skip`` は最初から ref_kind を
    持っていた。

    cloud 経路なので API client を差し替えて **渡される record を捕まえる**
    (= CLI の出力には出ない欠陥なので、関数を直接駆動して測る)。
    """
    import commands
    captured = {}

    class _Client:
        def record_decision(self, pid, payload):
            captured["pid"] = pid
            captured["payload"] = payload

    import commands_shared as cs
    monkeypatch.setattr(cs, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cs, "_get_api_client",
                        lambda: (_Client(), {"project_id": "p"}))
    commands._record_review_adjudication_decision(
        "philosophy", "ms-9", "採否の総括", [], ref_kind="target")
    ev = (captured.get("payload") or {}).get("evidence") or []
    assert "target:ms-9" in ev, (
        "target の採否が target: タグで記録されていない: " + repr(ev))
    assert "pr:ms-9" not in ev, (
        "target id が PR 番号のように記録されている: " + repr(ev))

    # PR 側は従来どおり pr: のまま (ref_kind の既定が変わっていないこと)
    captured.clear()
    commands._record_review_adjudication_decision(
        "ax", "798", "総括", [], "kurogin23mech-source/beacon")
    ev = (captured.get("payload") or {}).get("evidence") or []
    assert "pr:798" in ev, ev
    assert "repo:kurogin23mech-source/beacon" in ev, ev


def test_the_usage_error_is_rendered_from_the_help_registry(tmp_path):
    """Usage の文字列が **registry 由来** であること (文法の写しを増やさない)。

    独立 AX レビュー medium: 初版は文法を cmd_review_done / help registry /
    bin/beacon の 3 箇所に別々に手で書いており、しかも registry 側は ``--pr`` /
    ``--target`` を独立した任意旗として並べていて **排他でも必須でもないように
    読めた**。registry を単一ソースにして、Usage エラーも ``--help`` もそこから出す。
    """
    import ast as _ast
    import commands
    # (1) cmd_review_done が registry の組み立て器を通ること (手書きに戻さない)
    tree = _ast.parse(inspect.getsource(commands.cmd_review_done))
    calls = {(getattr(c.func, "id", None) or getattr(c.func, "attr", ""))
             for c in _ast.walk(tree) if isinstance(c, _ast.Call)}
    assert "_registry_usage_line" in calls, (
        "Usage を手で再掲している (registry が単一ソースになっていない): "
        + repr(sorted(calls)))
    # (2) registry の行が排他と必須を表していること
    line = commands._registry_usage_line("review done")
    assert "(--pr" in line and "| --target" in line, (
        "registry の文法が排他 / 必須を表していない: " + line)
    # (3) 実際の Usage エラーがその行と一致すること
    proj = _project(tmp_path)
    proc = subprocess.run([BEACON, "review", "done", "--type", "ax"],
                          capture_output=True, text=True, cwd=str(proj))
    assert proc.returncode != 0
    assert line in (proc.stdout + proc.stderr), (
        "Usage エラーが registry の行と一致しない:\n" + proc.stdout + proc.stderr)


def test_the_target_call_site_passes_the_target_ref_kind():
    """**呼び出し元の配線** を固定すること (独立保守性レビュー medium)。

    evidence タグの試験は ``_record_review_adjudication_decision`` を直接
    ``ref_kind="target"`` 付きで呼ぶので、**呼び出し元がそれを渡すのをやめても
    緑のまま** だった —— 直したはずの「milestone の採否が pr:ms-9 と記録される」が
    同じ形で再発しても検出できない。呼び出し側の keyword を構文木で固定する。
    """
    import ast as _ast
    import commands
    tree = _ast.parse(inspect.getsource(commands._emit_review_done_for_target))
    calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)
             and (getattr(n.func, "id", None) or getattr(n.func, "attr", ""))
             == "_record_review_adjudication_decision"]
    assert len(calls) == 1, f"採否の記録の呼び出しが 1 つでない: {len(calls)}"
    kw = {k.arg: _ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("ref_kind") == '"target"' or kw.get("ref_kind") == "'target'", (
        "target の経路が ref_kind='target' を渡していない (既定の 'pr' に戻ると "
        "milestone の採否が PR 番号のように記録される): " + repr(kw))


def test_every_ledger_reader_keys_by_kind():
    """台帳の読み手 **全員** が (種別, ref) で引くこと。

    dict の形を変えたとき読み手 3 箇所のうち 1 箇所 (PR 側の冪等判定) を直し忘れ、
    PR 側の冪等性を壊したのを自分の試験が捕まえた。平らな鍵に戻す / 1 箇所だけ
    直し忘れる、のどちらも構文木で止める。
    """
    import ast as _ast
    import importlib
    offenders = []
    for modname in ("commands", "commands_shared"):
        mod = importlib.import_module(modname)
        tree = _ast.parse(inspect.getsource(mod))
        for node in _ast.walk(tree):
            # `_read_review_done_ledger().get(<key>)` の <key> が Tuple であること
            if not isinstance(node, _ast.Call):
                continue
            if getattr(node.func, "attr", "") != "get":
                continue
            inner = getattr(node.func, "value", None)
            if not (isinstance(inner, _ast.Call)
                    and (getattr(inner.func, "id", None)
                         or getattr(inner.func, "attr", ""))
                    == "_read_review_done_ledger"):
                continue
            key = node.args[0] if node.args else None
            if not isinstance(key, _ast.Tuple):
                offenders.append(
                    f"{modname}:{node.lineno}: key="
                    + (_ast.unparse(key) if key is not None else "(なし)"))
    assert offenders == [], (
        "台帳を種別なしの鍵で引いている読み手がある (PR と target が同じ文字列"
        "空間を共有する): " + repr(offenders))


def test_the_unknown_subverb_fallback_renders_from_the_registry(tmp_path):
    """知らない sub-verb の案内も **registry 由来** であること。

    独立保守性レビュー high: 「文法は registry を単一ソースにした」と言いながら、
    すぐ隣の `review)` の catch-all に **第 3 の手書きコピー** が残っていた。
    しかも done の行には ``--target`` も ``(--pr | --target)`` の排他表記も無く、
    下の skip の行だけ正しいという兄弟間の食い違いになっていた。この文言は不正な
    sub-verb を打ったときだけ出るので目視でも気づきにくい。
    """
    import commands
    proj = _project(tmp_path)
    proc = subprocess.run([BEACON, "review", "bogus"],
                          capture_output=True, text=True, cwd=str(proj))
    assert proc.returncode != 0, proc.stdout
    out = proc.stdout + proc.stderr
    assert "bogus" in out, "何が知らない語か名指ししていない"
    # registry の done の行 (= 排他表記を含む) がそのまま出ること
    line = commands._registry_usage_line("review done")
    head = line.split(" [")[0]          # 旗の列挙より前 = 文法の本体
    assert head in out, (
        "catch-all が registry の文法を出していない (手書きの写しが残っている):\n"
        + out)
    # bin/beacon に done の文法の手書きが残っていないこと
    with open(BEACON, encoding="utf-8") as f:
        src = f.read()
    handwritten = [ln for ln in src.splitlines()
                   if "beacon review done --type" in ln and "echo" in ln]
    assert handwritten == [], (
        "bin/beacon に review done の文法の手書きが残っている:\n"
        + "\n".join(handwritten))


def test_forgetting_a_pr_does_not_touch_a_target_with_the_same_ref(tmp_path,
                                                                   monkeypatch):
    """台帳からの忘却が **種別で絞る** こと。

    独立保守性レビュー low: 鍵を一般化したとき、ここのマッチだけを「両方の種別を
    走査する」形に広げてしまった —— 名前 (``pr_number``) と docstring は PR 専用を
    予告しているのに、実装はそれより広い **呼び出し元も試験も無い** 分岐を持つ、
    という不一致だった。種別を呼び出し側に明示させる形に直した。
    """
    import commands_shared as cs
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    monkeypatch.setattr(cs, "_review_done_dir", lambda: (str(ledger), True))
    for kind in (cs.REF_KIND_PR, cs.REF_KIND_TARGET):
        (ledger / cs._review_done_marker_name("ax", "9", kind)).write_text(
            json.dumps({"ref": "9", "ref_kind": kind, "review_type": "ax"}),
            encoding="utf-8")
    cs.forget_review_done_across_worktrees("9")          # 既定 = PR
    left = sorted(p.name for p in ledger.iterdir())
    assert left == ["target-9-ax.json"], (
        "PR の忘却が同じ ref を持つ target の記録まで消した: " + repr(left))
