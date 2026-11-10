"""``review done`` の既定の sha が「judge に渡した差分」であることの固定 (ms-119)。

**何が起きていた** (独立 AX レビュー 6 周目 silent, 2026-11-10): この課題の中心的な
主張は「judge が見た差分に対してだけ関門が開く」。``--reviewed-sha`` を明示すれば
そうなるが、**既定** は ``review done`` を打った時点で gh に聞いた「いまの PR head」
だった。bundle を発行してから done を打つまでに誰かが push すると、judge が一度も
見ていない差分に対して関門が緑になる —— PR #785 / #834〜836 で実際に起きた欠陥
クラスそのものが、新設した機構を **使わないだけで** 再現する。

しかも judge を回す唯一の実運用経路 (``/beacon-review-run`` Skill) は
``--reviewed-sha`` を渡す手順を持っていなかったので、**既定経路では必ずこの穴を
通っていた**。Skill の散文に「渡せ」と書くだけでは、この MS が何度も構造で閉じて
きた「お願い依存」をレビュー機構自身に作ることになる。

**構造**: bundle を発行する側 (``review context``) が差分の sha を刻み、記録する側
(``review done``) がそれを既定の供給源として読む。印が無ければ従来どおり gh に
問い合わせる (印の無い環境で退行しない)。

この module が固定する契約
--------------------------
* 刻んだ sha が読み戻せる (リポジトリ別 / 旧式の両方の住所)。
* 形が壊れた印は **使わない** (無意味な値を「記録しました」と名乗らない)。
* ``--reviewed-sha`` 無しの ``review done`` は、印が在れば **gh を一度も聞かずに**
  その sha を記録する。
* 明示された ``--reviewed-sha`` は印より強い (明示が最優先)。
* 印が無ければ gh に落ちる (従来の挙動)。
"""

import io
import json
from contextlib import redirect_stdout, redirect_stderr

import pytest

import commands
import commands_shared as cs
import review_spine as rs

ISSUED = "3333333333333333333333333333333333333333"
CUR_HEAD = "4444444444444444444444444444444444444444"
EXPLICIT = "5555555555555555555555555555555555555555"


def _triggers(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    tdir.mkdir(exist_ok=True)
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(tdir))
    monkeypatch.setattr(commands, "_get_triggers_dir", lambda: str(tdir))
    return tdir


# ---------------------------------------------------------------------------
# 1. 印そのもの (純粋に近い入出力)
# ---------------------------------------------------------------------------

def test_the_issued_sha_round_trips(tmp_path, monkeypatch):
    _triggers(tmp_path, monkeypatch)
    cs.write_issued_bundle_sha("838", "o/r", "ax", ISSUED)
    assert cs.issued_bundle_sha("838", "o/r", "ax") == ISSUED


def test_the_issued_sha_is_per_review_type(tmp_path, monkeypatch):
    """種別ごとに分ける (束で 2 種別を発行したあと片方だけ再発行された場合)。"""
    _triggers(tmp_path, monkeypatch)
    cs.write_issued_bundle_sha("838", "o/r", "ax", ISSUED)
    assert cs.issued_bundle_sha("838", "o/r", "maintainability") is None, (
        "ax の印を maintainability が拾っている (種別で分かれていない)")


def test_a_repoless_stamp_is_still_read(tmp_path, monkeypatch):
    """読み取り側は旧式 (リポジトリ無し) の住所も見る (reviewed_marker と同じ約束)。"""
    _triggers(tmp_path, monkeypatch)
    cs.write_issued_bundle_sha("838", "", "ax", ISSUED)
    assert cs.issued_bundle_sha("838", "o/r", "ax") == ISSUED


def test_a_malformed_stamp_is_not_used(tmp_path, monkeypatch):
    """形が壊れた印は使わない。

    無意味な値を既定で拾うと、``review done`` が「この sha に対する実施として
    記録しました」と成功を名乗りながら、関門は永久に一致しない記録を残す
    (= 正直に記録した人が黙って損をする)。
    """
    tdir = _triggers(tmp_path, monkeypatch)
    path = cs._issued_bundle_sha_path("838", "o/r", "ax")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not-a-sha\n")
    assert cs.issued_bundle_sha("838", "o/r", "ax") is None
    assert tdir.exists()


# ---------------------------------------------------------------------------
# 2. review done が既定でそれを使うか (= この課題の本体)
# ---------------------------------------------------------------------------

def _run_done(tmp_path, monkeypatch, *, stamp=None, explicit=None,
              gh_heads=None):
    """``review done`` を 1 回走らせて (出力, gh への問い合わせ記録) を返す。"""
    tdir = _triggers(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    # 架空の sha なので git への解決は差し替える (主題は供給源の選択)。
    monkeypatch.setattr(commands, "resolve_full_commit_sha", lambda v: (v, ""))

    # **canonical な stub 位置を使う** (tests/_gh_head_stub.install)。
    # 初版は adapter を手で差し替え、しかも `gh_port.set_adapter` を monkeypatch を
    # 通さずに呼んでいたので、**後続の試験に漏れた** —— 同じ repo の
    # `test_resolve_pr_head_does_not_touch_live_github_under_test`
    # (試験が生きた GitHub を叩かないことの番人) を、この file を先に走らせたときだけ
    # 落とした。install の docstring がまさにそれを禁じている。
    import _gh_head_stub
    fake = _gh_head_stub.install(monkeypatch, gh_heads or {})

    if stamp:
        cs.write_issued_bundle_sha("838", "o/r", "ax", stamp)
    (tdir / cs.review_due_filename("ax", "838", "o/r")).write_text(
        json.dumps({"review": "ax", "pr_repo": "o/r"}), encoding="utf-8")

    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "838")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.setenv("BEACON_REVIEW_NO_FINDINGS", "1")
    monkeypatch.delenv("BEACON_REVIEWED_SHA", raising=False)
    if explicit:
        monkeypatch.setenv("BEACON_REVIEWED_SHA", explicit)

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            commands.cmd_review_done()
    except SystemExit:
        pass
    return out.getvalue() + err.getvalue(), fake


def test_done_records_the_issued_diff_not_the_current_head(tmp_path, monkeypatch):
    """**本体**: 印が在れば、いまの head ではなく印の sha を記録する。

    修正前はここで ``CUR_HEAD`` (= judge が一度も見ていない差分) が記録され、
    関門が緑になった。
    """
    combined, fake = _run_done(tmp_path, monkeypatch, stamp=ISSUED,
                               gh_heads={"838": CUR_HEAD})
    assert ISSUED[:12] in combined, (
        "印の sha を記録していない (いまの head を拾っている):\n" + combined)
    assert CUR_HEAD[:12] not in combined.split("関門がまだ")[0], (
        "記録した差分として、judge が見ていない head を名乗っている:\n" + combined)
    assert "judge に渡した bundle の差分" in combined, (
        "供給源を正しく名乗っていない (『いまの PR head』と誤って呼んでいる):\n"
        + combined)


def test_the_recorded_sha_does_not_depend_on_gh_being_reachable(tmp_path,
                                                               monkeypatch):
    """印が在るなら、**gh が答えられなくても** その sha を記録する。

    初版のこの試験は「印が在れば gh を一度も聞かない」と書いていたが、それは
    契約の読み違いだった: ``review done`` は記録のあとに **関門の判定** (いまの
    head に対してこの種別はまだ要るか) を出すので、そこでは gh に聞くのが正しい。
    実測すると問い合わせは 1 回在り、その 1 回は判定のためだった (記録された sha は
    印のものだったので、供給源としては使っていない)。

    測るべきは「記録する sha が外部状態に依存しないこと」なので、gh が答えられない
    状態で印の sha が記録されるかを見る。こちらなら、供給源が gh に戻った瞬間に
    赤くなる。
    """
    combined, fake = _run_done(tmp_path, monkeypatch, stamp=ISSUED,
                               gh_heads={})  # どの PR も答えられない
    assert ISSUED[:12] in combined, (
        "gh が答えられないと印の sha を記録できていない "
        "(記録が外部状態に依存している):\n" + combined)
    assert "judge に渡した bundle の差分" in combined, combined


def test_explicit_reviewed_sha_still_wins_over_the_stamp(tmp_path, monkeypatch):
    """明示が最優先 (印は既定の供給源にすぎない)。"""
    combined, _fake = _run_done(tmp_path, monkeypatch, stamp=ISSUED,
                                explicit=EXPLICIT,
                                gh_heads={"838": CUR_HEAD})
    assert EXPLICIT[:12] in combined, combined
    assert "--reviewed-sha で明示" in combined, combined


def test_without_a_stamp_it_falls_back_to_gh(tmp_path, monkeypatch):
    """印が無ければ従来どおり gh に問い合わせる (印の無い環境で退行しない)。"""
    combined, fake = _run_done(tmp_path, monkeypatch, stamp=None,
                               gh_heads={"838": CUR_HEAD})
    assert CUR_HEAD[:12] in combined, combined
    assert fake.calls, "印が無いのに gh を聞いていない (fallback が死んでいる)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
