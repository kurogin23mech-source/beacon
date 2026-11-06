"""レビュー済みの印を押す操作が黙らないことの試験 (ms-166 e-6873)。

何が起きていたか
----------------
`beacon review done` はレビュー実施を記録するついでに、GitHub 側の
`beacon-review-gate` という印を success に押し替える (これが無いとどの経路からも
取り込めない)。旧実装は「best-effort / never raises」で、**3 つの経路すべてが無言**
だった: opt-in の env が立っていない / sha が取れない / script が非 0 で終わる。
呼び出し側は「レビュー実施を記録」とだけ表示するので、印が押されたかを読み手が
知る手がかりが無かった。

**本当の病理は「押せない」ではなく「間違った commit に押せてしまう」だった。**
2026-10-06 に PR #786 で実測:

    3e210c29  beacon-review-gate success 04:57:26  ← push 前の review done が押した
                                                      (= GitHub が知っている古い PR head)
    d60ca7d8  beacon-review-gate pending 04:58:12  ← push 後に CI が作り直す
              beacon-review-gate success 翌日        ← 手で叩いて解消

`gh pr view --json headRefOid` は **GitHub が知っている** PR head を返す。手元に未 push の
commit があるとそれは古い sha なので、印はそこに付く。commit status は sha 単位なので、
その後 push した head は pending のまま残り「レビューはやったのに取り込めない」状態に
なる。失敗なら報告できるが、これは成功するので異常として検知されない。

(起票時の記述は「押し替えは失敗する」としていたが、実測の結果それは誤りだった。
遡行で記述を書き換えず、ここに正しい機構を残す。)

この file が留めるもの
----------------------
押す / 押さない のどの経路でも **必ず何か言う**こと。とくに手元の HEAD が push されて
いないときは **押さずに断る** こと (古い sha に印を付ける方が、押さないより始末が悪い)。

ms-119 e-6859 で **どこが言うか** が変わった (言う義務は変わらない)
-------------------------------------------------------------------
関門が「現在の差分に対して全種別が実施済みか」を問うようになったので、
「この PR のいまの head sha」を聞く場所が ``_ci_flip_review_gate_success`` の中から
``cmd_review_done`` の先頭 (= 何も書く前、``resolve_pr_head`` 経由) へ上がった。
印を押す側は **レビューした sha を受け取る** 形になった。

そのため e-6873 の 6 つの保証は 2 箇所に分かれて住む。**どれも消していない**:

  1. 押せた sha を言う                    → flip (受け取った sha)
  2. 未 push の HEAD は押さずに断る        → flip (手元 HEAD と照合)
  3. env が無いときも黙らない              → flip
  4. script が失敗したら回復手順を出す      → flip
  5. head が取れないことを言う              → ``cmd_review_done`` (pin できなかったと言う)
                                            + flip (pin 無しでは押さないと言う)
  6. sha の問い合わせが落ちたことを言う     → 同上 (5 と同じ経路)

5 / 6 がこの file で **両方の口を測る** のが要点 —— 上流が黙れば利用者は印が
押されていないことに気づけず、下流が黙れば「なぜ押されなかったか」が分からない。
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


def _all_types_done_at(monkeypatch, sha):
    """「全必須種別が この sha に対して実施済み」を導出側に見せる (ms-119 e-6859)。

    印を押す条件に「その sha に対して全種別が充足している」が加わったので、
    この file の主題 (= 押す / 断るときに何を言うか) に到達させるために必要。

    形は共有 factory (``tests/_pr_obligations_stub.py``) から採る —— 手書きすると
    本物のキーが変わっても古い形を返し続け、この試験だけが偽の緑になる
    (独立保守性レビューが反転実験で実測した形)。
    """
    import review_spine
    import _pr_obligations_stub

    def fake(pr_number, pr_repo="", *, head_sha=None):
        if head_sha == sha:
            return _pr_obligations_stub.obligations(head_sha=sha)
        return _pr_obligations_stub.obligations(
            head_sha=head_sha or "", resolved=bool(head_sha),
            owed=review_spine.required_pr_open_review_types(), satisfied=[])

    monkeypatch.setattr(commands, "pr_review_obligations", fake)


def _run_flip(monkeypatch, capsys, *, env_on=True, remote_sha="aaaa111",
              local_sha="aaaa111", script_rc=0, raise_on=None):
    """``_ci_flip_review_gate_success`` を、外部コマンドを差し替えて走らせる。

    実際に gh / git / GitHub を叩かずに各経路を作れるようにする (この試験の対象は
    「何を出力するか」であって、外部コマンドの挙動ではない)。

    ms-119 e-6859: ``remote_sha`` は **レビューした sha として渡される** 値。
    以前は flip 自身が ``gh pr view`` で取っていたが、いまは ``cmd_review_done``
    が先に解決して渡す (``remote_sha=""`` で「pin できなかった」経路になる)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1" if env_on else "0")
    _all_types_done_at(monkeypatch, remote_sha)

    def fake_run(argv, **kw):
        if raise_on and raise_on in " ".join(argv):
            raise OSError("boom")
        if argv[:2] == ["git", "rev-parse"]:
            return types.SimpleNamespace(stdout=local_sha, stderr="", returncode=0)
        # scripts/review-gate-ci.py
        return types.SimpleNamespace(
            stdout="", stderr="" if script_rc == 0 else "403 forbidden",
            returncode=script_rc)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    commands._ci_flip_review_gate_success("786", "o/r", remote_sha)
    cap = capsys.readouterr()
    return cap.out + cap.err


def _run_done_with_unresolvable_head(monkeypatch, capsys, *, gh_error):
    """``cmd_review_done`` を、head を解決できない状態で走らせる (ms-119 e-6859)。

    5 / 6 の保証 (head が取れない / 問い合わせが落ちた) はここが上流の口。
    """
    import commands_shared as cs
    monkeypatch.setattr(cs, "resolve_pr_head",
                        lambda pr, repo="": ("", "", gh_error))
    monkeypatch.setattr(commands, "resolve_pr_head",
                        lambda pr, repo="": ("", "", gh_error))
    monkeypatch.setattr(commands, "_pr_open_review_nudge_pending_types",
                        lambda pr, repo="": ["ax"])
    monkeypatch.setattr(commands, "_clear_review_due_for_pr",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "record_review_done_across_worktrees",
                        lambda *a, **k: cs.RECORDED_SHARED)
    monkeypatch.setattr(commands, "_read_review_done_ledger", lambda: {})
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "786")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.setenv("BEACON_REVIEW_NO_FINDINGS", "1")
    monkeypatch.delenv("BEACON_REVIEWED_SHA", raising=False)
    try:
        commands.cmd_review_done()
    except SystemExit:
        pass
    cap = capsys.readouterr()
    return cap.out + cap.err


def test_the_stamp_says_which_sha_it_stamped(monkeypatch, capsys):
    out = _run_flip(monkeypatch, capsys)
    assert "✓" in out and "aaaa111" in out, (
        "押せたことと対象の sha を言っていない:\n" + out)


def test_an_unpushed_head_is_refused_not_stamped_on_the_stale_sha(monkeypatch, capsys):
    """手元の HEAD が push されていないとき、押さずに断ること。

    これが e-6873 の本体。古い sha に印を付けると、その後 push した head は pending の
    まま残り、しかも「レビューはやった」と表示されるので原因に辿る手がかりが無い。
    """
    out = _run_flip(monkeypatch, capsys, remote_sha="old1111", local_sha="new2222")
    assert "押していません" in out, "押さずに断っていない:\n" + out
    assert "old1111" in out and "new2222" in out, (
        "どちらが手元でどちらが GitHub かを示していない:\n" + out)
    # 回復経路を添えること (次の人が script の呼び出し方を自分で読解しなくて済む)
    assert "git push" in out and "review-gate-ci.py" in out, (
        "回復の手順を出していない:\n" + out)
    # 再実行を勧めないこと (採否の記録が二重になる)
    assert "二重" in out, "review done の再実行が二重記録になる旨を言っていない:\n" + out


def test_the_opt_out_path_still_says_the_gate_was_not_touched(monkeypatch, capsys):
    """env が立っていないときも黙らないこと。

    「レビュー実施を記録」だけを見た読み手が、印も押されたと思い込むのを防ぐ。
    """
    out = _run_flip(monkeypatch, capsys, env_on=False)
    assert "触っていません" in out, out
    assert "BEACON_REVIEW_GATE_CI" in out, "どうすれば押せるかを言っていない:\n" + out


def test_a_failing_stamp_script_is_reported_with_a_recovery_command(monkeypatch, capsys):
    out = _run_flip(monkeypatch, capsys, script_rc=1)
    assert "⚠" in out and "押せませんでした" in out, out
    assert "403 forbidden" in out, "失敗の理由を伝えていない:\n" + out
    assert "--sha aaaa111" in out, "手で押すコマンドを出していない:\n" + out


def test_a_missing_remote_head_is_reported_upstream(monkeypatch, capsys):
    """head が取れないことを **上流 (review done)** が言うこと (保証 5)。

    ms-119 e-6859 で問い合わせの場所が上がったので、口もそこに移った。
    上流が黙ると、利用者は印が押されていないことに気づけない。
    """
    out = _run_done_with_unresolvable_head(
        monkeypatch, capsys, gh_error="gh の応答に headRefOid がありません")
    assert "pin できませんでした" in out, out
    assert "headRefOid" in out, "取れなかった理由を伝えていない:\n" + out
    assert "--reviewed-sha" in out, "回復の手段を出していない:\n" + out


def test_the_sha_lookup_blowing_up_is_reported_upstream(monkeypatch, capsys):
    """sha の問い合わせが落ちたことを言うこと (保証 6)。"""
    out = _run_done_with_unresolvable_head(
        monkeypatch, capsys, gh_error="OSError: boom")
    assert "pin できませんでした" in out and "boom" in out, out


def test_the_stamp_refuses_and_says_so_when_no_sha_was_pinned(monkeypatch, capsys):
    """下流 (印を押す側) も、pin が無いときは押さない旨を言うこと (保証 5 の下流)。

    上流と下流で片方しか言わないと、「押されていない」か「なぜ押されていないか」の
    どちらかが欠ける。
    """
    out = _run_flip(monkeypatch, capsys, remote_sha="")
    assert "押していません" in out and "pin できていません" in out, out
    # 下流も **自分の口で** 出口を言うこと。ここで `review-gate-ci.py --sha ...` を
    # 案内しても sha が無いので押せない (= 嘘の回復経路)。正しい出口は
    # 「head を確かめられる環境で打ち直す」か `--reviewed-sha` の明示。
    assert "--reviewed-sha" in out, "回復経路を出していない:\n" + out


def test_no_path_through_the_stamp_is_silent(monkeypatch, capsys):
    """**どの経路でも何か言う** ことを 1 つのテストで横断的に留める。

    個別の試験はそれぞれ 1 経路しか見ないので、新しい早期 return を足した人が
    「その経路だけ無言」を作れてしまう。ここは全経路を回して、出力が空のものが
    無いことを確かめる (= 無言の経路を増やせない)。
    """
    cases = {
        "ok": dict(),
        "env off": dict(env_on=False),
        "unpushed": dict(remote_sha="old1111", local_sha="new2222"),
        "script failed": dict(script_rc=1),
        "no remote head": dict(remote_sha=""),
        "lookup raised": dict(raise_on="gh pr"),
    }
    silent = []
    for label, kw in cases.items():
        out = _run_flip(monkeypatch, capsys, **kw)
        if not out.strip():
            silent.append(label)
    assert not silent, (
        "無言の経路があります (読み手は印が押されたか知れません): " + repr(silent))
