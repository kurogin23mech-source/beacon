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
5. (PR #849 独立 AX 2 周目) 共有したことで、別の作業フォルダの再発行が自分の
   印を黙って上書きしない。発行元どうしで sha が食い違えば、done はどれかを
   選ばず止まる
   —— これは本 module の 1 / 2 の試験そのものが担う (変異は手で実測済み、
   PR 本文に記録)
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
# 発行元ごとの枠: 共有しても上書きしない / 食い違えば止まる (AX 2 周目 silent)
# ---------------------------------------------------------------------------

OTHER = "5" * 40


def test_a_reissue_in_the_other_checkout_does_not_overwrite(
        two_checkouts, monkeypatch):
    """fork の judge が走っている間に親が同じ PR を発行し直しても、fork の印は残る。"""
    main, wt = two_checkouts
    _stand_in(monkeypatch, wt)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    _stand_in(monkeypatch, main)
    cs.write_issued_bundle_sha("811", "o/r", "ax", OTHER)
    got = set(cs.issued_bundle_candidates("811", "o/r", "ax").values())
    assert got == {ISSUED, OTHER}, (
        "片方の発行がもう片方の印を上書きした: %r" % (got,))


def test_disagreeing_issuers_yield_no_default_sha(two_checkouts, monkeypatch):
    main, wt = two_checkouts
    _stand_in(monkeypatch, wt)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    _stand_in(monkeypatch, main)
    cs.write_issued_bundle_sha("811", "o/r", "ax", OTHER)
    assert cs.issued_bundle_sha("811", "o/r", "ax") is None, (
        "食い違う印のどれかを黙って既定に選んでいる")


def test_agreeing_issuers_are_used(two_checkouts, monkeypatch):
    main, wt = two_checkouts
    for root in (wt, main):
        _stand_in(monkeypatch, root)
        cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    assert cs.issued_bundle_sha("811", "o/r", "ax") == ISSUED


def test_done_stops_and_lists_candidates_when_issuers_disagree(
        two_checkouts, tmp_path, monkeypatch):
    """**本体**: 食い違えば記録せずに止まり、候補と打ち直し方を出す。gh にも落ちない。"""
    main, wt = two_checkouts
    _stand_in(monkeypatch, wt)
    cs.write_issued_bundle_sha("811", "o/r", "ax", ISSUED)
    _stand_in(monkeypatch, main)
    cs.write_issued_bundle_sha("811", "o/r", "ax", OTHER)
    out = _done_here(tmp_path, monkeypatch, wt)
    assert "記録しません" in out and "--reviewed-sha" in out, out
    assert ISSUED[:12] in out and OTHER[:12] in out, out
    assert not any("対象の差分:" in ln for ln in out.splitlines()), (
        "食い違っているのに何かの sha で記録した:\n" + out)
    assert not (tmp_path / "ledger").exists() or not any(
        (tmp_path / "ledger").iterdir()), "止まったはずが実施台帳に書いた"


# ---------------------------------------------------------------------------
# 片付けは旧い置き場も見る (独立保守性 2 周目: 旧置き場の列挙を外す変異が生き残った)
# ---------------------------------------------------------------------------

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
