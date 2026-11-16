"""反転ハーネスの入口と判定が、共有部品 1 箇所で閉じていることを固定する
(ms-178 PR #818 の独立レビュー findings ax-1 / ax-2 / maint-1)。

ハーネスは共有ファイル (``bin/beacon`` は 90 本以上のテストが subprocess で叩く) を
in-place で書き換える道具なので、**入口で裁く**ことが安全装置そのもの:

* 引数を黙って捨てない — ``--help`` や打ち間違えたフラグで本物の変異実行が
  走ると、「効いた」と「捨てられた」が出力から区別できない (ax-1)。
* 同じ repo で 2 本同時に走らせない — 互いの変異を読んででたらめな結果を出し、
  復元が相手の書き込みを巻き戻す (ax-2)。
* 「赤くなった = ガードが効いた」ではない。赤の理由の判定を各ハーネスに写すと
  1 本だけ直って残りが旧判定のまま並ぶ (maint-1、実際に起きた)。

ここで見るのは共有部品 ``scripts/invert_harness.py`` の契約と、**全ハーネスが
その部品を経由している**こと (drift ガード)。ハーネス本体 (変異を当てて pytest を
回す部分) は意図的に pytest から呼ばない — CI は ``-n 4`` 並列で、隣の worker が
変異中の ``lib/`` を読むため。本体の実測は ``python3 -B scripts/invert-*.py`` を
手で直列に走らせる。
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import invert_harness as ih  # noqa: E402


def _harness_scripts():
    """``invert_harness`` を import する全ハーネス (= この契約の適用範囲)。"""
    found = []
    for p in sorted(SCRIPTS.glob("invert-*.py")):
        if "import invert_harness" in p.read_text(encoding="utf-8"):
            found.append(p)
    assert found, "ハーネスが 1 本も見つからない (glob の綴りが変わった?)"
    return found


# --- 判定 (穴 3): pytest の構造だけを見る -----------------------------------

@pytest.mark.parametrize("rc,tail,expect_red,expect_invalid", [
    (0, "12 passed in 0.3s", False, False),
    (1, "1 failed, 11 passed in 0.4s", True, False),
    (1, "2 failed in 0.1s", True, False),
    # 終了コード 1 でも要約行が error を数えていたら「捕まえた」にしない
    (1, "1 failed, 1 error in 0.2s", False, True),
    (1, "3 errors in 0.2s", False, True),
    (2, "Interrupted: 1 error during collection", False, True),
    (3, "INTERNALERROR> ...", False, True),
    (4, "ERROR: usage: ...", False, True),
    (5, "no tests ran in 0.01s", False, True),
])
def test_classify_reads_exit_code_and_summary_only(rc, tail, expect_red, expect_invalid):
    went_red, shown = ih.classify_pytest_result(SimpleNamespace(returncode=rc), tail)
    assert went_red is expect_red
    assert shown.startswith("INVALID") is expect_invalid


def test_classify_does_not_match_prose_that_merely_mentions_error():
    """旧判定 (``"error in" in 出力``) が誤反応した形: テストの docstring に
    「a syntax error in the ledger」と書いてあるだけ。要約行は普通の赤。"""
    tail = "1 failed in 0.3s"
    went_red, shown = ih.classify_pytest_result(SimpleNamespace(returncode=1), tail)
    assert went_red is True and not shown.startswith("INVALID")


# --- 入口 (穴 4-a): 引数を黙って捨てない -------------------------------------

def test_parse_cli_accepts_only_zero_arguments(capsys):
    assert ih.parse_cli(["x.py"], "USAGE") is None
    with pytest.raises(SystemExit) as e:
        ih.parse_cli(["x.py", "--help"], "USAGE TEXT")
    assert e.value.code == 0
    assert "USAGE TEXT" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        ih.parse_cli(["x.py", "--dry-run"], "USAGE")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--dry-run" in err and "触っていません" in err


def test_guarded_main_does_not_call_the_body_when_arguments_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ih.tempfile, "gettempdir", lambda: str(tmp_path))
    calls = []
    with pytest.raises(SystemExit) as e:
        ih.guarded_main(lambda: calls.append(1) or 0, ["x.py", "--bogus"], "U", tmp_path)
    assert e.value.code == 2 and calls == []
    with pytest.raises(SystemExit) as e:
        ih.guarded_main(lambda: calls.append(1) or 0, ["x.py", "-h"], "U", tmp_path)
    assert e.value.code == 0 and calls == []
    assert ih.guarded_main(lambda: calls.append(1) or 0, ["x.py"], "U", tmp_path) == 0
    assert calls == [1]


# --- 入口 (穴 4-b): 排他ロック ----------------------------------------------

def test_second_harness_on_the_same_repo_is_refused_without_running(tmp_path, monkeypatch):
    monkeypatch.setattr(ih.tempfile, "gettempdir", lambda: str(tmp_path))
    with ih.exclusive_lock(tmp_path):
        with pytest.raises(ih.LockHeld) as e:
            with ih.exclusive_lock(tmp_path):
                pass  # pragma: no cover
        assert str(os.getpid()) in str(e.value)
        # guarded_main は本体を呼ばず rc=1 で断る
        calls = []
        rc = ih.guarded_main(lambda: calls.append(1) or 0, ["x.py"], "U", tmp_path)
        assert rc == 1 and calls == []
    # 解放後は取れる
    with ih.exclusive_lock(tmp_path):
        pass


def test_lock_is_released_when_the_body_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ih.tempfile, "gettempdir", lambda: str(tmp_path))

    def boom():
        raise RuntimeError("body failed")

    with pytest.raises(RuntimeError):
        ih.guarded_main(boom, ["x.py"], "U", tmp_path)
    with ih.exclusive_lock(tmp_path):
        pass


def test_lock_lives_outside_the_repository(tmp_path):
    assert ROOT not in ih.lock_path_for(ROOT).parents


# --- 実機: 各ハーネスの入口が本当に裁いている (ax-1 を実行で確認) ----------

def _mtimes():
    return {p: p.stat().st_mtime_ns for p in
            (ROOT / "bin" / "beacon", ROOT / "lib" / "cmd_note.py",
             ROOT / "lib" / "readonly_gate.py")}


@pytest.mark.parametrize("script", [p.name for p in _harness_scripts()])
def test_each_harness_rejects_unknown_flags_before_touching_anything(script):
    before = _mtimes()
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--definitely-not-a-flag"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--definitely-not-a-flag" in r.stderr
    assert "baseline" not in r.stdout  # pytest は 1 回も走っていない
    assert time.monotonic() - t0 < 5
    assert _mtimes() == before


@pytest.mark.parametrize("script", [p.name for p in _harness_scripts()])
def test_each_harness_help_prints_usage_and_exits_zero(script):
    r = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), "--help"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "使い方" in r.stdout and "baseline" not in r.stdout


# --- drift ガード: 全ハーネスが共有部品を経由している -------------------------

def _violations(source: str) -> set:
    """ハーネス 1 本のソースに対する契約違反の集合 (空集合 = 準拠)。

    文字列の緩い一致ではなく AST で見る: 代入名 / 呼び出し先 / ``__main__``
    ブロックの中身を構造として取り出して比べる。
    """
    tree = ast.parse(source)
    out = set()
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    if "_COLLECTION_FAILURE_MARKS" in assigned:
        out.add("own-collection-marks")   # 旧判定の写しが残っている
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    if "ih.run_and_classify" not in calls and "ih.classify_pytest_result" not in calls:
        out.add("no-shared-classify")
    if "ih.run_pytest" in calls:
        out.add("bypasses-classify")      # 生の run_pytest を直接見ている
    main_blocks = [n for n in tree.body if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "__name__ == '__main__'"]
    if not main_blocks or "ih.guarded_main" not in {
            ast.unparse(c.func) for c in ast.walk(main_blocks[0]) if isinstance(c, ast.Call)}:
        out.add("no-guarded-entrypoint")
    return out


def test_every_harness_goes_through_the_shared_entrypoint_and_classifier():
    bad = {p.name: v for p in _harness_scripts()
           if (v := _violations(p.read_text(encoding="utf-8")))}
    assert bad == {}, bad


def test_the_drift_guard_itself_fails_on_each_drift():
    """test-the-test: 準拠した最小ハーネスから 1 つずつ崩して、必ず違反として
    数えられることを確かめる (緩いガードは緑のまま素通りする)。"""
    good = (
        "import invert_harness as ih\n"
        "ROOT = 1\n"
        "def run_one(t):\n    return ih.run_and_classify(t, ROOT)\n"
        "def main():\n    return 0\n"
        "if __name__ == '__main__':\n"
        "    import sys\n    sys.exit(ih.guarded_main(main, sys.argv, __doc__, ROOT))\n")
    assert _violations(good) == set()
    assert "own-collection-marks" in _violations(
        good + "_COLLECTION_FAILURE_MARKS = ('error in',)\n")
    assert "no-shared-classify" in _violations(
        good.replace("ih.run_and_classify(t, ROOT)", "(True, 'x')"))
    assert "bypasses-classify" in _violations(
        good.replace("return ih.run_and_classify(t, ROOT)",
                     "r = ih.run_pytest(t, ROOT)\n    return ih.classify_pytest_result(r[2], r[1])"))
    assert "no-guarded-entrypoint" in _violations(
        good.replace("ih.guarded_main(main, sys.argv, __doc__, ROOT)", "main()"))
