"""Tests for the review-as-merge-gate loop (ms-119 e-4060): a fired review-due
trigger structurally blocks beacon pr approve/merge until the review is run
(`beacon review done`), closing the "fires into a void, gets skipped" hole."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import commands_shared  # noqa: E402
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)
import review_spine  # noqa: E402
import _gh_head_stub  # noqa: E402  (ms-119 e-6859: head sha の問いを差し替える)

#: この試験が使う固定の head sha。関門は「**この差分** に対して実施済みか」を問う
#: ので、どの試験も自分が見せる head を宣言する (ms-119 e-6859)。
HEAD = "1111111111111111111111111111111111111111"
NEXT_HEAD = "2222222222222222222222222222222222222222"


def _ledger(tmp_path, monkeypatch):
    """実施台帳を tmp に閉じ込める (試験が互いの記録を見ないように)。"""
    d = tmp_path / "ledger"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(commands_shared, "_review_done_dir", lambda: (str(d), True))
    return d


def _record_done(tmp_path, monkeypatch, review_type, pr, sha, repo=""):
    """「この種別を この sha に対して実施した」を台帳に置く。"""
    d = _ledger(tmp_path, monkeypatch)
    (d / f"pr-{pr}-{review_type}.json").write_text(json.dumps({
        "ref": str(pr), "ref_kind": "pr", "pr": str(pr), "pr_repo": repo,
        "review_type": review_type, "reviewed_sha": sha, "sha_pinned": bool(sha),
        "recorded_by": "seed", "recorded_at": "2026-11-05T00:00:00Z",
        "recorded_in_cwd": str(tmp_path), "shared": True,
    }, ensure_ascii=False), encoding="utf-8")
    return d


def _record_all_done(tmp_path, monkeypatch, pr, sha, repo=""):
    for t in review_spine.required_pr_open_review_types():
        _record_done(tmp_path, monkeypatch, t, pr, sha, repo=repo)


def _project(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "milestones": [],
    }, ensure_ascii=False), encoding="utf-8")


def _fire(tmp_path, review_type, pr):
    tdir = tmp_path / ".beacon" / "triggers"
    tdir.mkdir(exist_ok=True)
    (tdir / f"{review_type}-review-due-{pr}.json").write_text(json.dumps({
        "name": f"{review_type}-review-due-{pr}", "kind": f"{review_type}-review-due",
        "pr_number": str(pr), "review": review_type,
    }), encoding="utf-8")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# pending detection
# ---------------------------------------------------------------------------

def test_pending_empty_when_no_triggers(proj):
    assert commands._pr_open_review_nudge_pending_types("482") == []


def test_pending_lists_fired_types(proj):
    _fire(proj, "ax", "482")
    _fire(proj, "maintainability", "482")
    _fire(proj, "ax", "999")  # other PR — must not leak in
    assert sorted(commands._pr_open_review_nudge_pending_types("482")) == \
        ["ax", "maintainability"]


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def test_gate_blocks_when_nothing_was_reviewed(proj, monkeypatch, capsys):
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _ledger(proj, monkeypatch)
    with pytest.raises(SystemExit) as e:
        cmd_pr._review_gate_check("482", action="approve")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "未実施" in err and "ax" in err


def test_gate_passes_when_all_types_reviewed_at_this_head(proj, monkeypatch):
    """ms-119 e-6859: 通るのは「**この head に対して** 全必須種別が実施済み」のとき。

    以前はトリガーファイルが 1 つも無ければ通った (= 「義務が発火していない」と
    「レビュー済み」を同一視していた)。導出に替えたので、必須種別の記録が無い PR は
    トリガーの有無に関係なく止まる。
    """
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _record_all_done(proj, monkeypatch, "482", HEAD)
    cmd_pr._review_gate_check("482", action="approve")  # no raise


def test_gate_blocks_again_after_a_commit_is_pushed(proj, monkeypatch, capsys):
    """**この課題の本体**: 実施後にコミットを積んだら関門が赤に戻る。

    PR #785 / #834 / #835 / #836 で 4 回起きた形 —— 一度 `review done` を打つと、
    その後どれだけコミットを積んでも帳簿は「実施済み」のままで、judge が一度も
    見ていない差分に対して `beacon-review-gate` が緑になった。#835 では AX の
    findings を直したコミット自身が未レビューのまま「レビュー済み」だった。
    """
    # 1) HEAD に対して全種別を実施 → 通る
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _record_all_done(proj, monkeypatch, "482", HEAD)
    cmd_pr._review_gate_check("482", action="merge")

    # 2) コミットを 1 本積む (= GitHub が答える head が変わる) → 赤に戻る
    _gh_head_stub.install(monkeypatch, {"482": NEXT_HEAD})
    with pytest.raises(SystemExit) as e:
        cmd_pr._review_gate_check("482", action="merge")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "未実施" in err
    # 「一度やったのに赤い」理由を言っていること (= 壊れたと誤解させない)
    assert "コミットが" in err and "積まれた" in err, err
    assert HEAD[:12] in err, "どの sha に対する実施だったかを言っていない\n" + err


def test_gate_blocks_when_head_cannot_be_resolved(proj, monkeypatch, capsys):
    """head が不明なら止める側に倒す (2026-11-05 user 決定)。

    「現在の差分に対して実施済み」と言えない状態で通すと偽の緑になる。
    stub を入れないので ``resolve_pr_head`` の試験ガードが「確かめられない」を返す。
    """
    _record_all_done(proj, monkeypatch, "482", HEAD)
    with pytest.raises(SystemExit) as e:
        cmd_pr._review_gate_check("482", action="approve")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "head を確かめられない" in err, err


def test_gate_returns_for_a_merged_pr(proj, monkeypatch):
    """merge / close 済みの PR に、これから課す義務は無い。

    独立レビューの名前で止めると、本当の理由 (もう解決済み) を隠すことになる。
    """
    _gh_head_stub.install(monkeypatch, {"482": HEAD}, state="MERGED")
    _ledger(proj, monkeypatch)
    cmd_pr._review_gate_check("482", action="merge")  # no raise


def test_gate_credits_a_reason_bearing_skip(proj, monkeypatch):
    """理由付きの免除 (review skip) は差し引く —— ただし開示する。"""
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _ledger(proj, monkeypatch)
    skips = proj / ".beacon" / "review-skips.jsonl"
    with open(skips, "w", encoding="utf-8") as f:
        for t in review_spine.required_pr_open_review_types():
            f.write(json.dumps({
                "kind": "review-skip", "review_type": t, "ref_kind": "pr",
                "ref": "482", "repo": "", "reason": "docs のみ",
                "skipped_at_sha": HEAD, "at": "2026-11-05T00:00:00Z",
            }, ensure_ascii=False) + "\n")
    cmd_pr._review_gate_check("482", action="approve")  # no raise


def test_gate_override_proceeds_with_audit(proj, monkeypatch, capsys):
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _ledger(proj, monkeypatch)
    monkeypatch.setenv("BEACON_PR_REVIEW_OVERRIDE", "1")
    cmd_pr._review_gate_check("482", action="merge")  # no raise
    err = capsys.readouterr().err
    assert "override" in err
    # ms-119 e-6859 (独立保守性レビュー 4 周目): **未実施一覧の中身まで見る**。
    # 以前は "override" の部分一致だけを見ていたので、監査行が空でも緑だった。
    # (「blocking を通っているか」は値では測れないので、隣の
    #  test_the_override_branch_reads_the_blocking_set が導出口の差し替えで測る。)
    for t in review_spine.required_pr_open_review_types():
        assert t in err, f"override の監査行が未実施 {t} を列挙していない:\n{err}"


def test_the_override_branch_reads_the_blocking_set(proj, monkeypatch, capsys):
    """override の監査行が ``review_blocking_types`` を **通る** こと。

    値では測れない: いまの規則では ``passed`` が偽のとき ``owed == blocking`` に
    なるので、直読みでも同じ一覧が出る (独立保守性レビュー 4 周目もそう述べている)。
    そして ``passed`` を立てると関門が手前で return して override 分岐に到達しない。
    初版の試験はこの 2 つを取り違えて、壊しても緑だった (反転ハーネスが捕まえた)。

    測れるのは「その分岐が単一の導出口を通っているか」。``cmd_pr`` 名前空間の
    ``review_blocking_types`` だけを差し替え (``format_review_gate_block_error``
    は ``commands_shared`` 側の自分のものを呼ぶので影響しない)、override の行に
    その値が現れることで確かめる。直読みに戻ると現れない。
    """
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _ledger(proj, monkeypatch)
    _fire(proj, "ax", "482")
    monkeypatch.setenv("BEACON_PR_REVIEW_OVERRIDE", "1")
    monkeypatch.setattr(cmd_pr, "review_blocking_types",
                        lambda ob: ["ONLY-FROM-THE-SINGLE-SOURCE"])
    cmd_pr._review_gate_check("482", action="merge")
    err = capsys.readouterr().err
    assert "ONLY-FROM-THE-SINGLE-SOURCE" in err, (
        "override 分岐が単一の導出口を通っていない (owed 直読み):\n" + err)


# ---------------------------------------------------------------------------
# review done — clears trigger, stamps marker, unblocks
# ---------------------------------------------------------------------------

def test_review_done_clears_and_unblocks(proj, monkeypatch, capsys):
    _gh_head_stub.install(monkeypatch, {"482": HEAD})
    _ledger(proj, monkeypatch)
    _fire(proj, "ax", "482")
    _fire(proj, "maintainability", "482")
    # ms-166 e-6997: 採否の明示が無いと関門は開かない (default-deny)。この試験の主題は
    # 「実施を記録すると関門が外れる」ことなので、「findings 0 件」を明示して満たす。
    monkeypatch.setenv("BEACON_REVIEW_NO_FINDINGS", "1")
    # run the ax review
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "482")
    commands.cmd_review_done()
    assert commands._pr_open_review_nudge_pending_types("482") == ["maintainability"]
    # still blocked (maintainability owed)
    with pytest.raises(SystemExit):
        cmd_pr._review_gate_check("482", action="approve")
    # run the maintainability review → all clear
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "maintainability")
    commands.cmd_review_done()
    assert commands._pr_open_review_nudge_pending_types("482") == []
    cmd_pr._review_gate_check("482", action="approve")  # passes now
    # done-marker stamped so the anchor won't re-fire
    assert os.path.exists(commands._pr_open_reviewed_marker_path("482"))


def test_review_done_requires_type_and_pr(proj, monkeypatch):
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "")
    monkeypatch.setenv("BEACON_PR_NUMBER", "482")
    with pytest.raises(SystemExit):
        commands.cmd_review_done()


# ---------------------------------------------------------------------------
# merge/close cleanup drops the marker
# ---------------------------------------------------------------------------

def test_clear_pr_open_removes_marker(proj):
    # simulate a fully-reviewed PR
    (proj / ".beacon" / "triggers").mkdir(exist_ok=True)
    open(commands._pr_open_reviewed_marker_path("482"), "w").close()
    cmd_pr._clear_pr_open_review_triggers("482")
    assert not os.path.exists(commands._pr_open_reviewed_marker_path("482"))


# ---------------------------------------------------------------------------
# anchor fast-skips under test mode (no gh call)
# ---------------------------------------------------------------------------

def test_anchor_skips_in_test_mode(proj, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    # must return immediately without invoking gh / raising
    commands._auto_fire_pr_open_review_triggers_for_open_prs()
