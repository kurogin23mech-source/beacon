"""「judge に渡した差分」の印が作業フォルダをまたいで見えること (ms-119 e-7527)。

**何が起きていた** (2026-11-18 実測): ``review done`` の既定の sha は 3 段
(明示 > judge に渡した差分の印 > gh の「いまの PR head」)。段の順は正しかったが、
2 段目の印の置き場が ``_get_triggers_dir()`` (= チェックアウトごとに別) だった。
worktree で ``review context`` を発行し、本体で ``review done`` を打つと印が
見つからず 3 段目に落ち、judge が一度も読んでいない差分に「実施済み」が付いた。
PR #811 で実際にそうなり、本体の ``.beacon/triggers/`` に印は 0 件だった。

この module は **本物の git (主チェックアウト + linked worktree)** で測る。
置き場の解決を差し替えて測ると、差し替えた関数が「共有」を名乗るだけで緑になり、
本物の ``git rev-parse --git-common-dir`` が worktree 間で同じ値を返すことを
誰も確かめない (= この課題が塞いだ型の偽の緑)。

固定する契約 (e-7527 の受入条件)
-------------------------------
1. worktree で発行 → 本体で done: judge に渡した sha が記録される
2. 逆向き (本体で発行 → worktree で done) も成り立つ
3. 印が本当に無いときは「いまの head」に落ち、落ちたことが出力に出る
4. 置き場を分裂した形 (cwd の triggers) に戻すと 1 / 2 が赤くなる
   —— これは本 module の 1 / 2 の試験そのものが担う (変異は手で実測済み、
   PR 本文に記録)

作業フォルダごとの印から sha を決める契約 (担う試験は下の「どの印を使うか」
「片付け」節。推し量りを持たない 3 つの規則 —— _settle_issued_sha の docstring):

5. 別の作業フォルダの再発行が自分の印を黙って上書きしない
6. 自分の印があればそれを使う。ただし他に自分より新しい別 sha の印があれば止まる。
   自分の印が無く、他が 1 種類ならそれを使う。2 種類以上なら発行元と時刻を並べて止まる
7. done は **使った印 1 件だけ** を片付ける (使っていない印は同じ sha でも消さない)
"""

import io
import json
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout

import pytest

import commands
import commands_shared as cs

pytestmark = pytest.mark.real_issued_bundle_dir

ISSUED = "3" * 40
CUR_HEAD = "4" * 40


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def two_checkouts(tmp_path):
    """主チェックアウトと linked worktree。それぞれ自分の ``.beacon/triggers`` を持つ。"""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "wt", str(wt))
    for root in (main, wt):
        (root / ".beacon" / "triggers").mkdir(parents=True)
    return main, wt


def _stand_in(monkeypatch, root):
    """``root`` のチェックアウトで作業している状態にする。

    cwd (git が共有置き場を答える根拠) と、作業記録ファイルの親 (旧い置き場) の
    **両方** を移す。片方だけ移すと「本当は同じフォルダに居る」状態で測ることになる。
    """
    monkeypatch.chdir(root)
    triggers = str(root / ".beacon" / "triggers")
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: triggers)
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: triggers)


def _done_here(tmp_path, monkeypatch, root):
    """``root`` で ``review done`` を 1 回打ち、(出力, 記録された sha) を返す。"""
    _stand_in(monkeypatch, root)
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    monkeypatch.setattr(cs, "resolve_full_commit_sha", lambda v: (v, ""))
    import _gh_head_stub
    _gh_head_stub.install(monkeypatch, {"811": CUR_HEAD})
    (root / ".beacon" / "triggers" / cs.review_due_filename("ax", "811", "o/r")
     ).write_text(json.dumps({"review": "ax", "pr_repo": "o/r"}),
                  encoding="utf-8")
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "811")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.setenv("BEACON_REVIEW_NO_FINDINGS", "1")
    monkeypatch.delenv("BEACON_REVIEWED_SHA", raising=False)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            commands.cmd_review_done()
    except SystemExit:
        pass
    return out.getvalue() + err.getvalue()


# ---------------------------------------------------------------------------
# 前提: 本物の git が worktree 間で同じ置き場を答えること
# ---------------------------------------------------------------------------

def test_both_checkouts_resolve_the_same_place(two_checkouts, monkeypatch):
    main, wt = two_checkouts
    _stand_in(monkeypatch, main)
    from_main = cs._issued_bundle_dir()
    _stand_in(monkeypatch, wt)
    from_wt = cs._issued_bundle_dir()
    assert from_main == from_wt, (
        "主チェックアウトと worktree で印の置き場が割れている: %r / %r"
        % (from_main, from_wt))
    assert str(main / ".beacon") not in from_main, (
        "置き場が作業記録ファイルの隣 (チェックアウトごとに別) のまま")


# ---------------------------------------------------------------------------
# 受入条件 1 / 2: 双方向
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("issue_in, done_in", [("wt", "main"), ("main", "wt")])
def test_a_stamp_issued_in_one_checkout_is_read_in_the_other(
        two_checkouts, monkeypatch, issue_in, done_in):
    roots = dict(zip(("main", "wt"), two_checkouts))
    _stand_in(monkeypatch, roots[issue_in])
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    _stand_in(monkeypatch, roots[done_in])
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED, (
        "%s で発行した印が %s から見えない" % (issue_in, done_in))


def test_done_in_main_records_what_the_worktree_handed_the_judge(
        two_checkouts, tmp_path, monkeypatch):
    """**本体**: worktree で発行 → 本体で done → いまの head ではなく印の sha。"""
    main, wt = two_checkouts
    _stand_in(monkeypatch, wt)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    out = _done_here(tmp_path, monkeypatch, main)
    # 見るのは「何を記録したか」の 1 行だけ。出力の続きは関門の状態として
    # いまの head を正しく名指すので、全文に head が無いことを要求すると偽の赤になる。
    recorded = [ln for ln in out.splitlines() if "対象の差分:" in ln]
    assert recorded, out
    assert ISSUED[:12] in recorded[0], (
        "judge に渡した sha でなく別の sha が記録された:\n" + out)
    assert "judge に渡した bundle の差分" in recorded[0], out


# ---------------------------------------------------------------------------
# 受入条件 3: 印が本当に無いときは落ち、落ちたことを言う
# ---------------------------------------------------------------------------

def test_without_any_stamp_it_falls_back_and_says_why(
        two_checkouts, tmp_path, monkeypatch):
    main, _wt = two_checkouts
    out = _done_here(tmp_path, monkeypatch, main)
    recorded = [ln for ln in out.splitlines() if "対象の差分:" in ln]
    assert recorded and CUR_HEAD[:12] in recorded[0], out
    assert "印が見つからなかった" in recorded[0], (
        "gh に落ちたことが出力から読めない:\n" + out)


# ---------------------------------------------------------------------------
# 寿命: 別のフォルダで発行した印も、PR の解決で片付く
# ---------------------------------------------------------------------------

def test_a_stamp_from_the_other_checkout_is_swept_on_resolve(
        two_checkouts, monkeypatch):
    main, wt = two_checkouts
    _stand_in(monkeypatch, wt)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    _stand_in(monkeypatch, main)
    monkeypatch.setattr(cs, "forget_review_done_across_worktrees",
                        lambda *a, **k: None)
    assert cs.issued_bundle_sha_paths("811"), "前提: 本体から印が見える"
    cs._clear_pr_open_review_triggers("811", "o/r")
    assert not cs.issued_bundle_sha_paths("811")


# ---------------------------------------------------------------------------
# 移行: 修正前に cwd に書かれた印も、その cwd からは読める
# ---------------------------------------------------------------------------

def test_a_legacy_stamp_in_the_cwd_is_still_read(two_checkouts, monkeypatch):
    main, _wt = two_checkouts
    _stand_in(monkeypatch, main)
    legacy = cs._issued_bundle_sha_path(
        "811", "o/r", "ax", directory=str(main / ".beacon" / "triggers"))
    with open(legacy, "w", encoding="utf-8") as f:
        f.write(ISSUED + "\n")
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED


# ---------------------------------------------------------------------------
# git の外: 共有が取れなければ cwd の triggers (= 従来どおり)
# ---------------------------------------------------------------------------

def test_outside_git_it_stays_in_the_cwd_triggers(tmp_path, monkeypatch):
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    monkeypatch.setattr(cs, "_worktree_shared_dir", lambda: None)
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    assert cs._issued_bundle_dir() == str(triggers)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED



# ---------------------------------------------------------------------------
# どの印を使うか: 推し量らない 3 つの規則 (_settle_issued_sha の docstring)
#
# 時刻で決まる規則があるので、印の時刻は os.utime で明示する (同じ秒に 2 回書くと
# 前後が決まらず、試験が環境で揺れる)。
# ---------------------------------------------------------------------------

OTHER = "5" * 40
THIRD = "6" * 40


def _stamp(monkeypatch, root, sha, at, repo="o/r"):
    """``root`` の作業フォルダとして sha の印を書き、時刻を ``at`` に固定する。"""
    _stand_in(monkeypatch, root)
    cs.write_issued_bundle_sha("811", repo, "ax", sha)
    path = cs._issued_bundle_sha_path("811", repo, "ax")
    os.utime(path, (at, at))
    return path


def _recorded_sha_line(out):
    lines = [ln for ln in out.splitlines() if "対象の差分:" in ln]
    return lines[0] if lines else ""


def test_a_reissue_in_the_other_checkout_does_not_overwrite(
        two_checkouts, monkeypatch):
    """別の作業フォルダの発行が、自分の印を黙って上書きしない。"""
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _stamp(monkeypatch, main, OTHER, 2000)
    got = {m["sha"] for m in cs.issued_bundle_candidates("811", "o/r", "ax").values()}
    assert got == {ISSUED, OTHER}, got


def test_rule1_own_mark_wins_when_nothing_newer_elsewhere(two_checkouts, monkeypatch):
    """規則 1: 自分の印があり、他に自分より新しい別 sha が無ければ自分の印。"""
    main, wt = two_checkouts
    _stamp(monkeypatch, main, OTHER, 1000)
    _stamp(monkeypatch, wt, ISSUED, 2000)
    _stand_in(monkeypatch, wt)
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED


def test_rule1_stops_when_a_newer_different_mark_exists_elsewhere(
        two_checkouts, monkeypatch):
    """規則 1 の但し書き: 他に自分より新しい別 sha があれば止まる。"""
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _stamp(monkeypatch, main, OTHER, 2000)
    _stand_in(monkeypatch, wt)
    with pytest.raises(cs.IssuedBundleConflict):
        cs.issued_bundle_sha("811", "o/r", "ax")


def test_rule1_reissue_of_an_already_done_sha_is_used(
        two_checkouts, tmp_path, monkeypatch):
    """done 済みと同じ sha を自分が発行し直した印も、新しければそのまま使う。

    以前の版は「done 記録済みの sha」を候補から外していたので、この生きた印が
    外れて別の作業フォルダの印が黙って記録された (切り出し後の独立 AX、高確度)。
    """
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _done_here(tmp_path, monkeypatch, wt)            # ISSUED を記録 (印は片付く)
    _stamp(monkeypatch, main, OTHER, 2000)           # 親が別の head で発行
    _stamp(monkeypatch, wt, ISSUED, 3000)            # fork が同じ sha で再発行
    out = _done_here(tmp_path, monkeypatch, wt)
    assert ISSUED[:12] in _recorded_sha_line(out), out


def test_rule2_the_only_mark_elsewhere_is_used(two_checkouts, tmp_path, monkeypatch):
    """規則 2 (e-7527 本来の経路): 自分の印が無く、他が 1 種類ならそれを使う。"""
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    out = _done_here(tmp_path, monkeypatch, main)
    assert ISSUED[:12] in _recorded_sha_line(out), out


def test_rule3_two_shas_elsewhere_stop_with_paths_and_times(
        two_checkouts, tmp_path, monkeypatch):
    """規則 3: 自分の印が無く、他が 2 種類以上なら止まり、発行元と時刻を並べる。"""
    main, wt = two_checkouts
    third = tmp_path / "wt2"
    _git(main, "worktree", "add", "-q", "-b", "wt2", str(third))
    (third / ".beacon" / "triggers").mkdir(parents=True)
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _stamp(monkeypatch, third, OTHER, 2000)
    out = _done_here(tmp_path, monkeypatch, main)
    assert "記録しません" in out and not _recorded_sha_line(out), out
    assert str(wt) in out and str(third) in out, out
    assert "古い発行" in out, out          # 同時発行と断定しない


def test_a_worktree_without_a_triggers_folder_is_not_treated_as_gone(
        two_checkouts, tmp_path, monkeypatch):
    """生死を推し量らない: triggers フォルダの無い作業フォルダの印も外さず止まる。

    以前の版は「発行元の triggers フォルダが無い = 消えた」と読んで、生きた
    作業フォルダの印を黙って候補から外した (切り出し後の独立 AX、高確度)。
    """
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    os.rename(str(wt / ".beacon" / "triggers"), str(wt / ".beacon" / "t-moved"))
    _stamp(monkeypatch, main, OTHER, 2000)
    _stand_in(monkeypatch, wt)
    with pytest.raises(cs.IssuedBundleConflict):
        cs.issued_bundle_sha("811", "o/r", "ax")


# ---------------------------------------------------------------------------
# 片付け: done は使った印 1 件だけを消す
# ---------------------------------------------------------------------------

def test_done_consumes_the_mark_it_used_in_another_checkout(
        two_checkouts, tmp_path, monkeypatch):
    """別の作業フォルダの印を使った done は、その印を片付ける。

    以前の版は自分の印しか消さなかったので、worktree で発行 → 本体で done の
    印が残り、2 巡目以降に食い違いで止まった (切り出し後の独立 AX、高確度)。
    """
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _done_here(tmp_path, monkeypatch, main)
    for sha, at in ((OTHER, 2000), (THIRD, 3000)):
        _stamp(monkeypatch, main, sha, at)
        out = _done_here(tmp_path, monkeypatch, main)
        assert sha[:12] in _recorded_sha_line(out), out


def test_done_does_not_consume_a_mark_it_did_not_use(
        two_checkouts, tmp_path, monkeypatch):
    """同じ sha でも、使っていない別の作業フォルダの印は消さない (AX 4 周目)。"""
    main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    _stamp(monkeypatch, main, ISSUED, 2000)
    _done_here(tmp_path, monkeypatch, main)          # 自分 (main) の印を使う
    shas_left = {m["sha"] for m in cs.issued_bundle_candidates("811", "o/r", "ax").values()}
    assert shas_left == {ISSUED}, shas_left          # wt の印は残る


def test_an_explicit_sha_leaves_the_marks_alone(two_checkouts, tmp_path, monkeypatch):
    """--reviewed-sha で明示した done は印を使っていないので、何も消さない。"""
    _main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000)
    monkeypatch.setenv("BEACON_REVIEWED_SHA", OTHER)
    out = _done_here_explicit(tmp_path, monkeypatch, wt, OTHER)
    assert OTHER[:12] in _recorded_sha_line(out) and "明示" in _recorded_sha_line(out), out
    _stand_in(monkeypatch, wt)
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED


def _done_here_explicit(tmp_path, monkeypatch, root, sha):
    """``_done_here`` と同じだが ``--reviewed-sha`` を明示する。"""
    real_delenv = monkeypatch.delenv

    def _keep(name, raising=True):
        if name == "BEACON_REVIEWED_SHA":
            return
        real_delenv(name, raising=raising)
    monkeypatch.delenv = _keep
    try:
        return _done_here(tmp_path, monkeypatch, root)
    finally:
        monkeypatch.delenv = real_delenv


def test_done_consumes_its_own_repoless_and_legacy_marks(
        two_checkouts, tmp_path, monkeypatch):
    """リポジトリ名無しの印、旧い置き場の印も、使えば片付く (保守性 4 周目 / 切り出し後)。"""
    _main, wt = two_checkouts
    _stamp(monkeypatch, wt, ISSUED, 1000, repo="")
    _done_here(tmp_path, monkeypatch, wt)
    _stand_in(monkeypatch, wt)
    assert cs.issued_bundle_candidates("811", "o/r", "ax") == {}
    legacy = cs._issued_bundle_sha_path(
        "811", "o/r", "ax", directory=str(wt / ".beacon" / "triggers"))
    with open(legacy, "w", encoding="utf-8") as f:
        f.write(OTHER + "\n")
    out = _done_here(tmp_path, monkeypatch, wt)
    assert OTHER[:12] in _recorded_sha_line(out), out
    assert not os.path.exists(legacy), "旧い置き場の印が使った後も残った"


def test_a_legacy_stamp_is_swept_on_resolve(two_checkouts, monkeypatch):
    main, _wt = two_checkouts
    _stand_in(monkeypatch, main)
    monkeypatch.setattr(cs, "forget_review_done_across_worktrees",
                        lambda *a, **k: None)
    legacy = cs._issued_bundle_sha_path(
        "811", "o/r", "ax", directory=str(main / ".beacon" / "triggers"))
    with open(legacy, "w", encoding="utf-8") as f:
        f.write(ISSUED + "\n")
    cs._clear_pr_open_review_triggers("811", "o/r")
    assert not os.path.exists(legacy), "旧い置き場の印が片付かずに残った"


def test_sweep_continues_past_a_file_it_cannot_remove(two_checkouts, monkeypatch):
    """1 件の削除失敗 (権限等) で残りを黙って取り残さない。"""
    main, _wt = two_checkouts
    _stand_in(monkeypatch, main)
    monkeypatch.setattr(cs, "forget_review_done_across_worktrees",
                        lambda *a, **k: None)
    for rt in ("ax", "maintainability", "inspection"):
        cs.write_issued_bundle_sha("811", "o/r", rt, ISSUED)
    real_remove = os.remove
    blocked = sorted(cs.issued_bundle_sha_paths("811"))[0]

    def _remove(path):
        if path == blocked:
            raise PermissionError(path)
        real_remove(path)
    monkeypatch.setattr(cs.os, "remove", _remove)
    cs._clear_pr_open_review_triggers("811", "o/r")
    assert cs.issued_bundle_sha_paths("811") == {blocked}


def test_an_unknown_sha_source_is_named_not_disguised(
        two_checkouts, tmp_path, monkeypatch):
    """供給源の表に無い値は「表示文が未定義」と名乗る (gh の文面に化けない)。"""
    main, _wt = two_checkouts
    monkeypatch.setattr(
        commands, "resolve_reviewed_sha_for_done",
        lambda **k: {"sha": ISSUED, "source": "a-new-source", "fatal": [],
                     "notes": [], "sha_error": "", "mark_path": ""})
    out = _done_here(tmp_path, monkeypatch, main)
    line = _recorded_sha_line(out)
    assert line and "表示文が未定義" in line, out
    assert "印が見つからなかった" not in line, out
