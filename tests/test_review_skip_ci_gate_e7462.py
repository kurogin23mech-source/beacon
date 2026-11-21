"""``review skip`` も CI の印 (beacon-review-gate) を同じ経路で押すこと (ms-119 e-7462)。

**何が起きていた** (2026-11-12, PR #846): ``beacon review skip`` は「人間が理由を
書いてレビューを省く一級の経路」と宣言され、関門 (``review status``) は「全必須
種別が実施済み。approve / merge を通過できます」と言うのに、GitHub の必須チェック
``beacon-review-gate`` を success にする経路は ``review done`` にしか無かった。
PR は BLOCKED のまま取り込めず、手で ``scripts/review-gate-ci.py set`` を叩いた。

固定する契約 (e-7462 の受入条件)
-------------------------------
1. skip で全必須種別が充足した sha に対して、CI の印が success になる
2. 印の説明文が「レビュー済み」でなく「人間の理由付き免除」を名指す
3. 手でスクリプトを叩かずに済む (= skip 1 回で印まで進む)
4. ``BEACON_REVIEW_GATE_CI`` が未設定なら従来どおり印を触らない

印を押すかの判断 (env / 手元 HEAD との照合 / 全必須種別の充足) は ``review done``
と同じ関数に任せている。ここでは **関門の導出は本物** (``pr_review_obligations``)
を通し、外の世界 (gh / git / スクリプト) だけを差し替える。
"""

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

import commands
import commands_shared as cs

SHA = "a" * 40


class _R:
    returncode = 0
    stderr = ""

    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    """PR #3 (o/r)。maintainability は SHA に対して実施済み、ax が未実施で review-due。"""
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    (triggers / cs.review_due_filename("ax", "3", "o/r")).write_text(
        json.dumps({"review": "ax", "pr_repo": "o/r"}), encoding="utf-8")
    for mod in (cs, commands):
        monkeypatch.setattr(mod, "_get_triggers_dir", lambda: str(triggers),
                            raising=False)
        monkeypatch.setattr(mod, "_review_skips_log_path",
                            lambda: str(tmp_path / "skips.jsonl"), raising=False)
        monkeypatch.setattr(mod, "resolve_pr_head",
                            lambda pr, repo="": (SHA, "OPEN", ""), raising=False)
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    monkeypatch.setattr(commands, "_resolve_review_pr_repo",
                        lambda explicit, gaps: explicit or "o/r")
    cs.record_review_done_across_worktrees("maintainability", "3", "o/r",
                                           reviewed_sha=SHA)

    calls = []

    def run(argv, *a, **k):
        calls.append(list(argv))
        if "rev-parse" in argv:
            return _R(SHA + "\n")
        return _R("")
    monkeypatch.setattr(commands.subprocess, "run", run)

    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "3")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.delenv("BEACON_TARGET_ID", raising=False)
    monkeypatch.setenv("BEACON_REASON", "PR の範囲が文書だけなので AX は省く")
    monkeypatch.setenv("BEACON_SESSION_KIND", "human")
    monkeypatch.delenv("BEACON_JSON", raising=False)
    return calls


def _skip():
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            commands.cmd_review_skip()
    except SystemExit as e:
        code = e.code or 0
    return code, out.getvalue(), err.getvalue()


def _gate_pushes(calls):
    return [c for c in calls if any("review-gate-ci.py" in str(x) for x in c)]


def test_skip_pushes_the_gate_when_every_type_is_covered(world, monkeypatch):
    """**本体 (受入 1・3)**: skip 1 回で印が success になる。手で叩かなくてよい。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    code, out, err = _skip()
    assert code == 0, (out, err)
    pushes = _gate_pushes(world)
    assert pushes, "skip で全種別が充足したのに印を押していない:\n" + out + err
    p = pushes[0]
    assert "success" in p and p[p.index("--sha") + 1] == SHA, p


def test_the_gate_description_names_the_waiver(world, monkeypatch):
    """受入 2: 説明文は「人間の理由付き免除」を名指し、実施した種別と分ける。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    _skip()
    p = _gate_pushes(world)[0]
    desc = p[p.index("--desc") + 1]
    assert "人間の理由付き免除" in desc and "ax" in desc.split("免除")[1], desc
    assert "実施: maintainability" in desc, desc


def test_without_the_opt_in_the_gate_is_left_alone(world, monkeypatch):
    """受入 4: BEACON_REVIEW_GATE_CI が無いリポジトリでは従来どおり印を触らない。"""
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    code, _out, err = _skip()
    assert code == 0
    assert not _gate_pushes(world), world
    assert "BEACON_REVIEW_GATE_CI=1 が無いため" in err, err


def test_a_skip_that_leaves_another_type_owed_does_not_push(world, monkeypatch,
                                                            tmp_path):
    """skip した種別以外がまだ要るなら押さない (1 種別で緑にしない、PR #785 の型)。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "empty-ledger"), True))
    _skip()
    assert not _gate_pushes(world), world


def test_json_mode_also_pushes_and_stays_parseable(world, monkeypatch):
    """--json でも印を押す (押さないと「JSON で打つと黙って進まない」穴になる)。

    印の処理が出す行は JSON を壊さないよう記録の gate_ci_notes に入る。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    code, out, err = _skip()
    assert code == 0, (out, err)
    record = json.loads(out)
    assert _gate_pushes(world), world
    assert any("success" in n for n in record.get("gate_ci_notes", [])), record


def test_description_for_an_all_done_pr_does_not_mention_a_waiver():
    """免除が無ければ説明文に免除と書かない (純粋関数)。"""
    desc = commands.review_gate_ci_description(
        {"satisfied": ["ax", "maintainability"], "skipped": []})
    assert "免除" not in desc and "ax" in desc and "maintainability" in desc, desc


# --- skip 固有の「押さない」条件 (PR #853 独立 AX) -------------------------

def test_an_unguarded_ai_skip_does_not_push_the_gate(world, monkeypatch):
    """人間と申告していないセッションの免除では GitHub の必須チェックを緑にしない。

    以前は AI の skip は監査ログに残るだけで GitHub は pending のまま = 人の目を
    通る最後の一枚だった。それを AI の 1 コマンドで緑にでき、しかも説明文が
    「人間の理由付き免除」と名乗っていた (独立 AX が実測)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    monkeypatch.delenv("BEACON_REVIEW_SKIP_USER_OVERRIDE", raising=False)
    code, _out, err = _skip()
    assert code == 0
    assert not _gate_pushes(world), world
    assert "人間と申告していない" in err, err


def test_an_explicit_user_override_still_pushes(world, monkeypatch):
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    monkeypatch.setenv("BEACON_REVIEW_SKIP_USER_OVERRIDE", "1")
    _skip()
    assert _gate_pushes(world), world


def test_a_skip_that_waived_nothing_does_not_push(world, monkeypatch, tmp_path):
    """一致する義務が無い skip (PR 番号の打ち間違い等) で、その PR を緑にしない。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    for f in (tmp_path / "triggers").iterdir():
        f.rename(tmp_path / f.name)                  # review-due を退避
    code, _out, err = _skip()
    assert code == 3, err
    assert not _gate_pushes(world), world


def test_json_mode_reports_whether_the_gate_was_pushed(world, monkeypatch):
    """--json では押せたかを構造化して返す (notes の文字列を読まなくても分かる)。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    _code, out, _err = _skip()
    assert json.loads(out)["gate_ci_pushed"] is True
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    _code, out, _err = _skip()
    rec = json.loads(out)
    assert rec["gate_ci_pushed"] is False, rec
    assert any("人間と申告していない" in n for n in rec["gate_ci_notes"]), rec


def test_the_manual_push_hint_carries_the_waiver_description(world, monkeypatch):
    """押せなかったときの「手で押す」案内に説明文が付く。

    付けずに案内すると、スクリプト既定の「記録済み」が付き、免除がレビュー済みに
    見える (この PR が防いだ状態を手動経路で再現してしまう)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")

    def run(argv, *a, **k):
        world.append(list(argv))
        if "rev-parse" in argv:
            return _R(SHA + "\n")
        r = _R("")
        r.returncode = 1
        r.stderr = "gh: network down"
        return r
    monkeypatch.setattr(commands.subprocess, "run", run)
    _code, _out, err = _skip()
    hint = [ln for ln in err.splitlines() if "手で押す" in ln]
    assert hint and "--desc" in hint[0] and "免除" in hint[0], err
