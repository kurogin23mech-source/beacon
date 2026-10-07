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
"""

from __future__ import annotations

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


def _run_flip(monkeypatch, capsys, *, env_on=True, remote_sha="aaaa111",
              local_sha="aaaa111", script_rc=0, raise_on=None):
    """``_ci_flip_review_gate_success`` を、外部コマンドを差し替えて走らせる。

    実際に gh / git / GitHub を叩かずに 4 経路を作れるようにする (この試験の対象は
    「何を出力するか」であって、外部コマンドの挙動ではない)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1" if env_on else "0")

    def fake_run(argv, **kw):
        if raise_on and raise_on in " ".join(argv):
            raise OSError("boom")
        if argv[:2] == ["gh", "pr"]:
            return types.SimpleNamespace(stdout=remote_sha, stderr="", returncode=0)
        if argv[:2] == ["git", "rev-parse"]:
            return types.SimpleNamespace(stdout=local_sha, stderr="", returncode=0)
        # scripts/review-gate-ci.py
        return types.SimpleNamespace(
            stdout="", stderr="" if script_rc == 0 else "403 forbidden",
            returncode=script_rc)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    commands._ci_flip_review_gate_success("786")
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


def test_a_missing_remote_head_is_reported(monkeypatch, capsys):
    out = _run_flip(monkeypatch, capsys, remote_sha="")
    assert "押せませんでした" in out and "head" in out, out


def test_the_sha_lookup_blowing_up_is_reported(monkeypatch, capsys):
    out = _run_flip(monkeypatch, capsys, raise_on="gh pr")
    assert "押せませんでした" in out, out
    assert "review-gate-ci.py" in out, "回復経路を出していない:\n" + out


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
