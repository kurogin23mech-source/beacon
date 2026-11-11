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
    # **差し替え先は cs** —— 供給源の決定を commands_shared へ切り出した結果、
    # この名前は commands_shared の globals で解決される。`commands` 側に当てても
    # 効かず、本物の git が架空 sha に走って落ちる (切り出しで stub の住所が
    # 動く、の実例)。
    monkeypatch.setattr(cs, "resolve_full_commit_sha", lambda v: (v, ""))

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


# ---------------------------------------------------------------------------
# 3. 明示した sha が PR の履歴から辿れるか (独立 AX レビュー 7 周目 silent)
# ---------------------------------------------------------------------------

class _FakeRun:
    """``git merge-base --is-ancestor`` の終了コードだけを演じる。"""

    def __init__(self, code):
        self.code = code

    def __call__(self, argv, **kw):
        import types
        return types.SimpleNamespace(returncode=self.code, stdout="", stderr="")


@pytest.mark.parametrize("code,expected", [
    (0, True),     # 辿れる
    (1, False),    # 辿れない
    (128, None),   # 比較できない (fetch していない / 浅いクローン)
    (2, None),     # その他のエラーも「比較できない」に倒す
])
def test_is_ancestor_translates_exit_codes_into_three_values(monkeypatch, code,
                                                             expected):
    """終了コードを **事実に翻訳** する。1 と 128 を畳まないことが要点。

    畳むと、fetch していないだけの環境で「無関係な sha だ」と断定してしまう。
    """
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", _FakeRun(code))
    assert cs.sha_is_ancestor_of("a" * 40, "b" * 40) is expected


def test_an_unreachable_explicit_sha_is_disclosed_not_refused(monkeypatch,
                                                              tmp_path):
    """辿れない sha は **拒否せず、言う**。

    関門は完全一致で比べるので辿れない sha で関門が開くことはない (害は記録の
    誤りだけ)。拒否にすると rebase 後の履歴や PR head でない範囲の正当な記録を
    塞ぐので、この MS の原則どおり「隠せなくする」側で閉じる。
    """
    monkeypatch.setattr(cs, "resolve_full_commit_sha", lambda v: (v, ""))
    monkeypatch.setattr(cs, "resolve_pr_head",
                        lambda *a, **k: ("c" * 40, "OPEN", ""))
    monkeypatch.setattr(cs, "sha_is_ancestor_of", lambda *a, **k: False)
    out = cs.resolve_reviewed_sha_for_done(
        explicit=EXPLICIT, pr_number="838", pr_repo="o/r", review_type="ax")
    assert not out["fatal"], "辿れないだけで拒否している: %r" % (out["fatal"],)
    assert out["sha"] == EXPLICIT and out["source"] == "explicit"
    assert any("辿れません" in n for n in out["notes"]), out["notes"]


def test_an_undeterminable_ancestry_says_nothing(monkeypatch):
    """比較できないときは **黙る** (断定できないことを断定しない)。"""
    monkeypatch.setattr(cs, "resolve_full_commit_sha", lambda v: (v, ""))
    monkeypatch.setattr(cs, "resolve_pr_head",
                        lambda *a, **k: ("c" * 40, "OPEN", ""))
    monkeypatch.setattr(cs, "sha_is_ancestor_of", lambda *a, **k: None)
    out = cs.resolve_reviewed_sha_for_done(
        explicit=EXPLICIT, pr_number="838", pr_repo="o/r", review_type="ax")
    assert not any("辿れません" in n for n in out["notes"]), (
        "比較できていないのに「辿れません」と断定した: %r" % (out["notes"],))


# ---------------------------------------------------------------------------
# 4. 印の寿命 (独立保守性レビュー 7 周目 medium)
# ---------------------------------------------------------------------------

def test_the_stamps_are_cleared_when_the_pr_is_resolved(tmp_path, monkeypatch):
    """PR が merge / close されたら印も片付く。

    初版は新しい永続状態を足しておきながら、兄弟 (reviewed marker) の寿命管理の
    **真横**でそこに繋いでいなかった。読み手は「PR スコープの印はここで片付く」と
    正しく読むので、片方だけ漏れていると気づけない (ms-80 e-1829 で
    release-marker が蓄積した前例と同じ型)。
    """
    tdir = _triggers(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, "forget_review_done_across_worktrees",
                        lambda *a, **k: None)
    cs.write_issued_bundle_sha("838", "o/r", "ax", ISSUED)
    cs.write_issued_bundle_sha("838", "o/r", "maintainability", ISSUED)
    cs.write_reviewed_marker("838", "o/r", ISSUED)
    assert cs.issued_bundle_sha_paths("838"), "前提: 印が在る"

    cs._clear_pr_open_review_triggers("838", "o/r")

    leftover = {p for p in cs.issued_bundle_sha_paths("838")}
    assert not leftover, (
        "PR が解決したのに印が残っている (trigger dir が育ち続ける): %r"
        % (sorted(leftover),))


def test_the_stamp_sweep_covers_retired_review_types(tmp_path, monkeypatch):
    """台帳に無い (= 引退した) 種別の印も片付ける。

    片付けの網羅性を『いま必須の種別』から組み立てると、種別を増減したあとに
    古い印が取り残される —— しかも誰も参照しないので気づけない。だから住所は
    ディレクトリの接頭辞で拾う。
    """
    tdir = _triggers(tmp_path, monkeypatch)
    monkeypatch.setattr(cs, "forget_review_done_across_worktrees",
                        lambda *a, **k: None)
    cs.write_issued_bundle_sha("838", "o/r", "a-retired-review-type", ISSUED)
    assert cs.issued_bundle_sha_paths("838"), "前提: 引退種別の印が在る"

    cs._clear_pr_open_review_triggers("838", "o/r")

    assert not cs.issued_bundle_sha_paths("838"), (
        "引退した種別の印が取り残された (registry から組み立てている)")
