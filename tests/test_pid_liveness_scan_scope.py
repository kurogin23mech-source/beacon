"""`check-pid-liveness.py` が見る範囲の試験 (ms-170 e-7249)。

**なぜ要るのか**: この検査はリポジトリ全体を歩く唯一のガードで、道具 (Claude Code)
の作業場所 `.claude/` まで降りていた。独立レビュアーを走らせると
`.claude/worktrees/agent-*` に作業コピーが残り、そこはリポジトリの**過去の状態**
そのものなので、今は直してある古い書き方がそのまま在る。歩くと「直したのに
指摘される」ので、毎回ノイズが出て、やがてこの検査の出力ごと読み流される
(= 緑も赤も信用されなくなる)。実際に 2026-11-01 に誤検知 3 件が出た。

**線引きの考え方**: 「レビュアーの作業コピーを除く」ではなく **リポジトリが実際に
運んでいるコードだけを見る**。前者だと置き場所が変わった瞬間に漏れるが、後者なら
道具が何を置いても関係ない。`.claude/` は git 管理下に 1 ファイルも無いので、
この線引きで落ちるのは「リポジトリのコードではないもの」だけになる。

ここで固定するのは 3 つ:

1. `.claude` が除外一覧に在ること
2. **その前提** — `.claude` が git 管理下でないこと (追跡され始めたら、除外は
   製品コードの盲点に変わるので、この試験が気づく)
3. **除外が実際に効くこと** — 同じ違反を除外先と走査先に置いて、前者だけが
   報告されないことを実測する (一覧に名前が在るだけでは、効いている証拠にならない)
"""

from __future__ import annotations

import re
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "check-pid-liveness.py"


def _load_guard():
    """ハイフン入りファイル名なので import 文では読めない。直接読み込む。"""
    spec = importlib.util.spec_from_file_location("check_pid_liveness", GUARD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_pid_liveness"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_tool_workspace_is_excluded():
    """`.claude` (道具の作業場所) は走査から外れている。"""
    mod = _load_guard()
    assert ".claude" in mod.SKIP_DIRS


def test_the_premise_holds_claude_dir_is_not_tracked():
    """**除外してよい前提**: `.claude/` は git 管理下に無い。

    追跡され始めたら、この除外は製品コードの盲点に変わる。そのときここが赤く
    なって、除外を狭めるか allowlist に移すかを判断させる。
    """
    out = subprocess.run(["git", "ls-files", ".claude"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    tracked = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert tracked == [], (
        ".claude/ が git 管理下に入りました。走査から外したままだと製品コードの"
        f"盲点になります。追跡されているもの: {tracked[:5]}")


# この検査が見るのは「signal が 0 でないと **証明できない**」呼び出し。
# literal 1 は証明できるので対象外 — 最初その形で書いて、下の『走査先なら
# 報告される』が赤くなって気づいた (試料が外れていると、除外の試験が
# 「何も検出できていないだけ」になる)。
VIOLATION = (
    "import os\n"
    "\n"
    "def probe(pid):\n"
    "    os.kill(pid, 0)\n"
    "    return True\n"
)


def _run_guard(root: pathlib.Path):
    """`--root` で任意の木を走査させ ``(rc, 出力)`` を返す。"""
    r = subprocess.run(
        [sys.executable, str(GUARD), "--strict", "--root", str(root)],
        capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_a_violation_in_a_scanned_directory_is_reported(tmp_path):
    """走査先に置いた違反は報告される (= 試験そのものが効いていることの確認)。

    これが緑でないと、下の「除外先では報告されない」は『除外が効いた』ではなく
    『そもそも何も検出できていない』を意味してしまう。
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc != 0, f"走査先の違反を見逃しました:\n{out}"
    assert "lib/probe.py" in out


def test_the_same_violation_under_the_tool_workspace_is_not_reported(tmp_path):
    """**同じ違反**を `.claude/` の下に置くと報告されない (除外が実際に効く)。

    一覧に名前が在ることと、歩かないことは別。上の試験と同じ中身を置き場所だけ
    変えて測るので、「検出できる違反を、置き場所の理由で見ていない」ことが言える。
    """
    nested = tmp_path / ".claude" / "worktrees" / "agent-deadbeef" / "lib"
    nested.mkdir(parents=True)
    (nested / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc == 0, f".claude/ の下を歩いています:\n{out}"
    assert "agent-deadbeef" not in out


@pytest.mark.parametrize("skipped", sorted({".claude", ".worktrees", ".trash"}))
def test_each_excluded_directory_actually_stops_the_walk(tmp_path, skipped):
    """除外一覧の各ディレクトリで、歩かないことを 1 件ずつ測る。

    一覧はただの集合なので、1 つだけ効いていて残りが効かない形もありうる
    (実際に `.claude` が抜けていた)。名前ごとに実測する。
    """
    nested = tmp_path / skipped / "deep" / "lib"
    nested.mkdir(parents=True)
    (nested / "probe.py").write_text(VIOLATION, encoding="utf-8")
    rc, out = _run_guard(tmp_path)
    assert rc == 0, f"{skipped}/ の下を歩いています:\n{out}"


def test_repo_root_itself_stays_clean():
    """実リポジトリでこの検査が緑であること (回帰の受け皿)。"""
    rc, out = _run_guard(ROOT)
    assert rc == 0, out
    assert "ok" in out


# --- 盤の受入確認の手順 (ms-170 e-7252) -------------------------------------
#
# 手順そのものは実機のセッションが要るので、ここで固定するのは **手順が在ること**
# と、**過去に踏んだ罠に戻っていないこと**。実測 (前後 3 通りの判定が区別できる)
# は scripts/verify-on-running-board.sh の docstring と e-7252 の完了理由に在る。

_VERIFY = ROOT / "scripts" / "verify-on-running-board.sh"


def _verdict(before_fails, after_fails, before_second_fails):
    """手順スクリプトの **本物の判定** を固定の数値で動かす。

    `--verdict` は試験用の口で、本番の経路と同じ `verdict()` 関数を呼ぶ (写しでは
    ない)。だから判定を変えると、ここが赤くなる。
    """
    r = subprocess.run(
        ["bash", str(_VERIFY), "--verdict",
         str(before_fails), str(after_fails), str(before_second_fails)],
        capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def test_running_board_verification_procedure_exists():
    """手順が在り、実行できる形で置かれている (受入条件 3)。"""
    assert _VERIFY.is_file(), f"手順がありません: {_VERIFY}"
    assert os.access(_VERIFY, os.X_OK), "実行できる形になっていません"


# --- 判定を **実行して** 確かめる (文字列の有無では守れなかった) -------------
#
# 最初はスクリプト本文に特定の語が含まれるかだけを assert していた。独立レビューが
# 変異で実証したところ、判定を「常に直った」に固定しても (語はコメントに残るので)
# 試験は全件緑のままだった。**判定ロジックを無意味にする変更が「試験が通ったから
# 安全」として通る**形で、これはこの手順が防ごうとしている「弱い根拠で閉じる」
# そのものだった。いま試験は本物の判定を数値で動かし、終了コードと文面を見る。

def test_verdict_says_not_fixed_when_the_new_side_fails():
    """後の側に失敗が在れば「直っていない」(他の条件より先に効く)。"""
    rc, out = _verdict(0, 4, 0)
    assert rc == 1, out
    assert "直っていません" in out


def test_verdict_says_fixed_only_with_a_failure_that_order_cannot_explain():
    """後から周を回った側でも失敗が出ていれば「直った」。"""
    rc, out = _verdict(3, 0, 2)
    assert rc == 0, out
    assert "直ったと言えます" in out


def test_verdict_refuses_when_the_old_side_never_failed():
    """前の側が 1 件も失敗しなければ、差が出ないので証拠にならない。"""
    rc, out = _verdict(0, 0, 0)
    assert rc == 3, out
    assert "証拠になりません" in out


def test_verdict_refuses_a_difference_that_order_can_explain():
    """**これが肝**: 前の側の失敗が「先に回ったとき」だけなら証拠にしない。

    問い合わせ自体が窓の並びを変えるので、先に回った側は不利になる。総数の差だけ
    で判定すると、前後に同じ実行ファイルを渡しても「直った」と出る (実測した)。
    """
    rc, out = _verdict(3, 0, 0)
    assert rc == 3, out
    assert "順序で説明できて" in out
    assert "直ったと言えます" not in out


def test_verdict_entry_rejects_wrong_argument_counts():
    """数を間違えたら黙って既定で走らない (何を測ったか分からなくなる)。"""
    r = subprocess.run(["bash", str(_VERIFY), "--verdict", "1"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "usage:" in (r.stdout + r.stderr)


def test_the_verdict_tests_would_catch_a_neutered_judgement(tmp_path):
    """test-the-test: 判定を「常に直った」に固定した写しを作り、上の試験が

    捕まえられることを確かめる。偽の安全を自分で作らないための反転。
    """
    mutated = tmp_path / "mutated.sh"
    text = _VERIFY.read_text(encoding="utf-8")
    broken = text.replace(
        'if [ "$before_second_fails" -gt 0 ]; then',
        'if true; then  # before_second_fails / 順序では説明できない', 1)
    assert broken != text, "変異を当てる目印が見つからない (判定が動いた?)"
    mutated.write_text(broken, encoding="utf-8")
    # 壊した判定では「順序で説明できる差」を直ったと言ってしまう
    r = subprocess.run(["bash", str(mutated), "--verdict", "3", "0", "0"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "直ったと言えます" in r.stdout, (
        "変異が効いていない = この反転は何も確かめていない")


def test_procedure_queries_the_running_board_not_a_standalone_artifact():
    """**動いている盤に問い合わせる**形であること (受入条件 1)。

    生成物を単体で走らせた結果を根拠にしたのが元の事故なので、手順が盤の口を
    叩いていることを押さえる。
    """
    text = _VERIFY.read_text(encoding="utf-8")
    assert "/api/jump" in text, "盤に問い合わせていない"
    assert "/api/state" in text, "盤が応えるようになるのを待っていない"
    assert "/api/sessions" in text, "的を盤自身から取っていない"


def test_procedure_compares_before_and_after():
    """前と後の両方を立てて同じ問い合わせを投げる形であること (受入条件 2)。"""
    text = _VERIFY.read_text(encoding="utf-8")
    for token in ("PORT_BEFORE", "PORT_AFTER", "sweep"):
        assert token in text, f"前後の比較の骨格が無い: {token}"


def test_procedure_does_not_use_mapfile():
    """`mapfile` を使わない (macOS 同梱の bash 3.2 に無い)。

    最初これで落ちた。可搬性の回帰の受け皿。
    """
    text = _VERIFY.read_text(encoding="utf-8")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("mapfile" in ln for ln in code), (
        "mapfile は macOS の bash 3.2 に無い")




# --- 盤に渡す旗が Go 側に実在するか (ms-170 e-7147 保守性 review) ------------

_FLAG_PARITY = ROOT / "scripts" / "check-viewer-flag-parity.py"


def test_viewer_flag_parity_guard_passes_on_the_real_tree():
    """いま実リポジトリで揃っていること。"""
    r = subprocess.run([sys.executable, str(_FLAG_PARITY)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


def test_viewer_flag_parity_guard_goes_red_on_drift(tmp_path):
    """**drift で赤くなること** (test-the-test)。

    Go 側の旗名を変えた写しを作り、ガードが非 0 で落ちることを確かめる。
    緩い一致で緑のまま通るガードは、無いより悪い (このリポジトリが何度も
    踏んだ形)。
    """
    import shutil

    tree = tmp_path / "repo"
    (tree / "viewer").mkdir(parents=True)
    (tree / "lib").mkdir()
    (tree / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check-viewer-flag-parity.py",
                tree / "scripts" / "check-viewer-flag-parity.py")
    shutil.copy(ROOT / "lib" / "cmd_view.py", tree / "lib" / "cmd_view.py")
    go = (ROOT / "viewer" / "main.go").read_text(encoding="utf-8")
    broken = go.replace('flag.Bool("no-reuse"', 'flag.Bool("force-new"', 1)
    assert broken != go, "変異を当てる目印が無い (旗の定義の書き方が変わった?)"
    (tree / "viewer" / "main.go").write_text(broken, encoding="utf-8")

    r = subprocess.run(
        [sys.executable, str(tree / "scripts" / "check-viewer-flag-parity.py")],
        cwd=tree, capture_output=True, text=True, timeout=300)
    assert r.returncode == 1, (
        "Go 側の旗名を変えてもガードが緑のままでした:\n"
        f"{r.stdout}\n{r.stderr}")
    assert "no-reuse" in (r.stdout + r.stderr)


def test_viewer_flag_parity_guard_is_wired_into_ci():
    """CI の束から呼ばれていること (置いただけで走らない形にしない)。"""
    ci = (ROOT / "scripts" / "ci-strict-drift-guards.sh").read_text(
        encoding="utf-8")
    assert "check-viewer-flag-parity.py" in ci


# --- 盤を畳む既定の説明が 3 箇所で揃っているか (ms-170 保守性 review medium) ---
#
# 既定は Go (viewer/idle.go) が持ち、その説明が help 台帳 (lib/commands.py) と
# viewer/README.md にも書かれている。独立レビューが「README は直したのに help
# 台帳が『無条件に 30 分で畳む』のまま残り、既存の drift ガードは旗の**名前**しか
# 見ないので緑だった」と指摘した。値そのものを 1 箇所に寄せるのは言語を跨ぐので、
# ここでは **モード依存であることが 3 箇所すべてで述べられているか** を見る。

def _help_view_description():
    r = subprocess.run([str(ROOT / "bin" / "beacon"), "view", "--help"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


def test_the_idle_default_is_declared_once_in_go_as_a_readable_literal():
    """既定値の持ち主は Go の 1 箇所で、**リテラルとして読める**こと。

    1 箇所であることは昔から要求していた (写しが増えると片方だけ直る)。
    リテラルであることを足したのは、文面との突き合わせ
    (scripts/check-idle-default-claims.py) が **定数を読めないときは「判定不能」で
    中立に終わる** 設計だから。`time.Duration(0)` のような等価な書き換えをすると、
    あの検査は赤くならずに黙って無効化され、文面が旧既定を宣言したまま残っても
    誰も気付かない。読めることを要求するのはこの試験の仕事で、読めた値と文面の
    一致はあちらの仕事 —— ここで値と文面の照合までやると、同じ規則が 2 箇所に
    分かれて片方だけ古くなる。

    **値そのもの (0) を固定しているのはここではない**: viewer/idle_test.go が
    `defaultIdleTimeout != 0` と `idleTimeoutFor(0, false) != 0` の両方を見ている。
    独立レビューが「この試験だけ読むと 0 が here で守られていると信じうる」と
    指摘したので、分担を明記する —— 形 (読めること) はこの試験、値は Go の単体
    試験、値と文面の一致は check-idle-default-claims.py。3 つが揃って初めて
    「既定が変わったのに文面が古い」を全方向で捕まえる。
    """
    idle = (ROOT / "viewer" / "idle.go").read_text(encoding="utf-8")
    assert idle.count("defaultIdleTimeout = ") == 1, (
        "既定値の宣言が 1 箇所でない (写しが増えると片方だけ直る)")
    m = re.search(r"const defaultIdleTimeout = (.+)", idle)
    assert m, "既定値が `const defaultIdleTimeout = <literal>` の形で宣言されていない"
    literal = m.group(1).split("//")[0].strip()
    assert re.fullmatch(r"0|\d+ \* time\.(Minute|Second|Hour)", literal), (
        f"既定値が機械で読めない形になっている: {literal!r} —— "
        "文面との突き合わせ (check-idle-default-claims.py) は読めないと中立で "
        "終わるので、この形が崩れると検査が黙って無効になる")


def test_no_copy_says_the_default_depends_on_the_mode():
    """**3 箇所すべて**が「既定では畳まない」と述べ、モード依存を述べないこと。

    e-7264 で既定を反転した (手元で開いた盤を 30 分で畳む → 畳まない) とき、
    **モードという入力そのものが要らなくなった** —— idleTimeoutFor は引数から
    `exposed` を落としてある。以前この試験は逆に「3 箇所すべてがモード依存を
    述べていること」を要求していた (旧世界では手元だけ畳んだため)。反転の際に
    直し忘れ、取り込み検査で初めて落ちた。

    いま守りたいのは「どこか 1 箇所に旧世界が残っていないか」。1 箇所でも
    「既定で畳む」「モードによって違う」と読める状態だと、それを読んだ人が
    畳まれる前提で後片付けを省く / 既定と同じ値の旗を「畳まれないように」と称して
    付ける、といった誤りをする。
    """
    help_text = _help_view_description()
    readme = (ROOT / "viewer" / "README.md").read_text(encoding="utf-8")
    idle = (ROOT / "viewer" / "idle.go").read_text(encoding="utf-8")

    # help: 自分では畳まないこと + 畳ませる口が在ることの両方を述べている
    assert "does not close itself" in help_text, (
        f"help 台帳が「既定では畳まない」と述べていない:\n{help_text}")
    assert "--idle-timeout" in help_text, (
        f"help 台帳に畳ませる口が出ていない (閉じ方が分からない):\n{help_text}")
    # README: 手元も設置済みも同じ既定 (= モードで変わらない) と書いてある
    assert "盤は自ら畳みません" in readme, "README が「既定では畳まない」と述べていない"
    assert "設置した盤も自ら畳みません" in readme, (
        "README がモード依存の消滅を述べていない (設置側だけ別扱いに読める)")
    # 実装: 反転の理由がコードのそばに在る
    assert "モードに依らず畳まない" in idle, "実装側に反転の理由が無い"


def test_the_help_text_does_not_claim_an_unconditional_close():
    """「常に 30 分で畳む」と読める言い方が help に残っていない。"""
    help_text = _help_view_description()
    assert "closes after 30m with nobody watching —" not in help_text, (
        "help がモードに関係なく畳むと述べている (実際は --expose で畳まない)")
