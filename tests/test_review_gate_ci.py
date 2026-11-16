"""CI-side review gate scaffold (ms-119 / e-4073).

The scaffold makes the review gate path-independent via a `beacon-review-gate`
commit status. These tests pin the PURE logic (which reviews the gate requires,
the status payload shape) and that the local flip is OFF by default so nothing
posts to GitHub or spends anything until a maintainer opts in.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review-gate-ci.py"
sys.path.insert(0, str(ROOT / "lib"))
import commands  # noqa: E402
import _pr_obligations_stub  # noqa: E402


def _load_script_module():
    spec = importlib.util.spec_from_file_location("review_gate_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_script_module()


# --- pure logic -------------------------------------------------------------

def test_required_review_types_is_the_pr_open_pair():
    got = set(gate.required_review_types())
    assert "ax" in got and "maintainability" in got


def test_status_payload_pending_shape():
    p = gate.status_payload("pending", pr="42")
    assert p["context"] == "beacon-review-gate"
    assert p["state"] == "pending"
    assert p["pr"] == "42"
    assert "ax" in p["required_reviews"]
    assert len(p["description"]) <= 140


def test_status_payload_success_description():
    p = gate.status_payload("success")
    assert p["state"] == "success"
    assert "記録済み" in p["description"]


def test_plan_subcommand_prints_payload():
    r = subprocess.run([sys.executable, str(SCRIPT), "plan", "--pr", "7"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["state"] == "pending" and out["pr"] == "7"


# --- the local flip follows the REPOSITORY, not the operator's shell -------
#
# ms-181 e-7103 changed the question this asks. It used to pin "no env var →
# touch nothing at all", which protected repositories that do not use the gate
# but ALSO meant an operator who ran `beacon review done` without remembering
# `BEACON_REVIEW_GATE_CI=1` left the check pending — measured twice (ms-185's
# fork 2026-10-19, PR #811 2026-10-20) and the second half of e-7103's
# two-sided hole. The protected property is kept: a repository that has not
# opted in is still left alone. What changed is who is asked — the repo
# variable the workflow itself is gated on, instead of a shell the operator has
# to remember.

def test_ci_flip_leaves_a_repo_that_has_not_opted_in_alone(monkeypatch):
    """No env var + repo variable unset → no status is pushed.

    This is what the old "no-op by default" test was really protecting, and it
    must keep holding: deriving the opt-in must not start stamping commit
    statuses onto repositories that never asked for the gate.
    """
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    calls = []

    class _Unset:
        returncode = 1
        stdout = ""

    def rec(argv, *a, **k):
        calls.append(argv)
        return _Unset()

    monkeypatch.setattr(commands.subprocess, "run", rec)
    commands._ci_flip_review_gate_success("42")
    # It may ASK (one `gh variable get`), but it must not go on to resolve a
    # head sha or invoke the gate script.
    assert all("headRefOid" not in " ".join(c) for c in calls), calls
    assert all("review-gate-ci.py" not in " ".join(c) for c in calls), calls


def test_ci_flip_needs_no_env_var_when_the_repo_uses_the_gate(monkeypatch):
    """受入条件 5: the stamp lands without anyone remembering a variable."""
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)
    calls = []

    class R:
        returncode = 0

        def __init__(self, argv):
            # `gh variable get` answers "1"; the sha probes answer a sha.
            self.stdout = "1\n" if "variable" in argv else "abc123sha\n"

    def rec(argv, *a, **k):
        calls.append(argv)
        return R(argv)

    monkeypatch.setattr(commands.subprocess, "run", rec)
    # ms-119 e-6859 で契約が変わった: 印は **レビューした sha** に押すので、
    # 関数が自分で head を問い合わせる経路は無くなり、呼び出し側が sha を渡す。
    # 受入条件 5 が主張するのは「env を覚えていなくても押せる」ことなので、
    # 見るべきは repo 変数に訊いたことと、実際に印を押しにいったこと。
    commands._ci_flip_review_gate_success("42", reviewed_sha="abc123sha")
    # repo 変数に訊いた = env を覚えていなくても判断できている
    assert any("variable" in " ".join(c) for c in calls), calls
    # env が無いことを理由に降りていない (降りていればここまで来ない)。
    # この先で止まるのは別の理由 (その sha に対する必須種別が未実施) なので、
    # 受入条件 5 が主張する範囲はここまで。
    assert any("rev-parse" in " ".join(c) for c in calls), calls


def test_ci_flip_can_still_be_turned_off_explicitly(monkeypatch):
    """`BEACON_REVIEW_GATE_CI=0` asks nothing and pushes nothing.

    Keeps an escape hatch for a repository that has the variable set but a
    context where stamping is wrong (a probe, a replay, someone else's PR).
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "0")

    def boom(*a, **k):
        raise AssertionError("an explicit 0 must not consult gh at all")

    monkeypatch.setattr(commands.subprocess, "run", boom)
    commands._ci_flip_review_gate_success("42")


SHA = "abc123sha"


def _stub_subprocess(monkeypatch, local_head=SHA):
    """``git rev-parse HEAD`` を決め打ちし、呼ばれた argv を集める。"""
    calls = []

    class R:
        returncode = 0
        stderr = ""

        def __init__(self, stdout=""):
            self.stdout = stdout

    def rec(argv, *a, **k):
        calls.append(argv)
        if "rev-parse" in argv:
            return R(local_head + "\n")
        return R("")

    monkeypatch.setattr(commands.subprocess, "run", rec)
    return calls


def _all_types_done_at(monkeypatch, sha):
    """「全必須種別が この sha に対して実施済み」を導出側に見せる。

    分岐も文面も共有 stub に 1 箇所だけ置いてある (ms-119 e-6859、独立保守性
    レビュー 2 周目: この helper が 2 つの試験ファイルに逐語で重複しており、
    片方だけ直して「直した」と判断できる形だった)。
    """
    return _pr_obligations_stub.install_satisfied_at(monkeypatch, commands, sha)


def test_ci_flip_pushes_the_mark_to_the_reviewed_sha(monkeypatch):
    """印は **レビューした差分** に押す (ms-119 e-6859)。

    以前は「いまの PR head」に押していたので、別種別が古い sha しか見ていなくても
    1 つの ``review done`` が今の head を緑にできた (PR #785 の事故)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    _all_types_done_at(monkeypatch, SHA)
    calls = _stub_subprocess(monkeypatch)
    commands._ci_flip_review_gate_success("42", "o/r", SHA)
    pushes = [c for c in calls
              if "review-gate-ci.py" in " ".join(c) and "success" in c]
    assert pushes, f"印を押していない: {calls}"
    assert SHA in pushes[0], f"レビューした sha に押していない: {pushes[0]}"


def test_ci_flip_targets_the_reviewed_sha_not_the_local_head(monkeypatch, capsys):
    """押す先は **レビューした sha**。手元 HEAD を流用しないこと。

    **反転ハーネスが捕まえた偽の緑** (2026-11-05): 上の試験は stub の手元 HEAD と
    レビューした sha を同じ値にしていたので、``--sha reviewed_sha`` を
    ``--sha local_sha`` に書き換えても通った = 2 つを区別していなかった。

    区別が出るのは ``git rev-parse HEAD`` が **空を返す** とき (commit が 1 つも
    無いリポジトリ等。非 0 終了でも例外は飛ばないのでここに到達する)。手元 HEAD を
    流用していると空の sha に押しに行く。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    _all_types_done_at(monkeypatch, SHA)
    calls = _stub_subprocess(monkeypatch, local_head="")
    commands._ci_flip_review_gate_success("42", "o/r", SHA)
    pushes = [c for c in calls
              if "review-gate-ci.py" in " ".join(c) and "success" in c]
    assert pushes, f"印を押していない: {calls}"
    i = pushes[0].index("--sha")
    assert pushes[0][i + 1] == SHA, (
        f"手元 HEAD (空) を押す先に流用した: {pushes[0]}")


def test_ci_flip_refuses_without_a_reviewed_sha(monkeypatch, capsys):
    """どの差分に対する実施か pin できていないなら押さない。

    押すと「judge が見ていない差分」を緑にする。印は sha 単位なので、
    pin できない状態の success は構造的に嘘になる。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    calls = _stub_subprocess(monkeypatch)
    commands._ci_flip_review_gate_success("42", "o/r", "")
    assert not any("review-gate-ci.py" in " ".join(c) for c in calls), calls
    assert "pin できていません" in capsys.readouterr().err


def test_ci_flip_refuses_while_another_type_is_still_owed(monkeypatch, capsys):
    """**全必須種別が同じ差分に対して** 実施されるまで緑にしない。

    1 種別の実施で緑を押せてしまう形が PR #785 の事故だった (AX が古い sha しか
    見ていないのに、保守性の ``review done`` が今の head を success にした)。
    """
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "1")
    _all_types_done_at(monkeypatch, "some-other-sha")   # この sha では未充足
    calls = _stub_subprocess(monkeypatch)
    commands._ci_flip_review_gate_success("42", "o/r", SHA)
    assert not any("review-gate-ci.py" in " ".join(c) for c in calls), calls
    assert "未実施です" in capsys.readouterr().err


# --- activation plan builders (ms-160 e-5805) ------------------------------
# Pure functions: they turn a gate_status() snapshot into the exact gh argv,
# so we can pin the two repo-admin steps without mutating a live repo.

def _status(*, var=None, protected=False, checks=None):
    checks = checks or []
    return {
        "branch": "main",
        "variable_BEACON_REVIEW_GATE_CI": var,
        "branch_protected": protected,
        "required_checks": checks,
        "gate_required": gate.GATE_CONTEXT in checks,
        "active": var == "1" and gate.GATE_CONTEXT in checks,
    }


def test_activate_plan_from_scratch_sets_var_and_creates_protection():
    """Unprotected main + unset variable → both steps, and the protection PUT
    requires exactly the gate check (minimal policy by default)."""
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    labels = " | ".join(s["label"] for s in plan)
    assert "BEACON_REVIEW_GATE_CI=1" in labels
    assert "create branch protection" in labels
    put = [s for s in plan if "--method" in s["argv"] and "PUT" in s["argv"]][0]
    body = json.loads(put["stdin"])
    assert body["required_status_checks"]["contexts"] == [gate.GATE_CONTEXT]
    # Minimal by default: no admin enforcement, no PR-review requirement.
    assert body["enforce_admins"] is False
    assert body["required_pull_request_reviews"] is None


def test_activate_plan_is_additive_when_already_protected():
    """Existing protection with an unrelated check → PATCH that KEEPS the other
    check and adds ours (never clobbers existing required checks)."""
    plan = gate.build_activate_plan(
        _status(var="1", protected=True, checks=["test"]))
    # variable already 1 → only the context-add step remains.
    assert len(plan) == 1
    patch = plan[0]
    assert "PATCH" in patch["argv"]
    body = json.loads(patch["stdin"])
    assert set(body["contexts"]) == {"test", gate.GATE_CONTEXT}


def test_activate_plan_empty_when_already_active():
    plan = gate.build_activate_plan(
        _status(var="1", protected=True, checks=[gate.GATE_CONTEXT]))
    assert plan == []


def test_activate_plan_honors_stricter_policy_flags():
    plan = gate.build_activate_plan(
        _status(var="1", protected=False),
        enforce_admins=True, require_pr_reviews=2)
    put = [s for s in plan if "PUT" in s["argv"]][0]
    body = json.loads(put["stdin"])
    assert body["enforce_admins"] is True
    assert body["required_pull_request_reviews"][
        "required_approving_review_count"] == 2


def test_deactivate_plan_sets_var_zero_and_drops_context_only():
    plan = gate.build_deactivate_plan(
        _status(var="1", protected=True, checks=["test", gate.GATE_CONTEXT]))
    labels = " | ".join(s["label"] for s in plan)
    assert "BEACON_REVIEW_GATE_CI=0" in labels
    patch = [s for s in plan if "PATCH" in s["argv"]][0]
    body = json.loads(patch["stdin"])
    # the unrelated check survives; only the gate context is dropped.
    assert body["contexts"] == ["test"]


def test_execute_plan_dry_run_never_shells_out(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run must not execute gh")
    monkeypatch.setattr(gate.subprocess, "run", boom)
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    results = gate._execute_plan(plan, dry_run=True)
    assert results and all(r["dry_run"] for r in results)


def test_execute_plan_stops_at_first_failure(monkeypatch):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "boom" if rc else ""

    def rec(argv, *a, **k):
        calls.append(argv)
        return R(1)  # first step fails

    monkeypatch.setattr(gate.subprocess, "run", rec)
    plan = gate.build_activate_plan(_status(var=None, protected=False))
    assert len(plan) == 2
    results = gate._execute_plan(plan, dry_run=False)
    # stopped after the first (failing) step — the second never ran.
    assert len(results) == 1 and results[0]["ok"] is False
    assert len(calls) == 1


# --- 「使っていない」と「判定できない」を畳まない -----------------------------
#
# 独立 AX レビュー (2026-11-17、実測): gh が無い / 認証切れ / 到達不能の端末でも
# 「このリポジトリはレビュー関門を使っていません」と出ていた。実際には GitHub 側で
# 有効なのに「何もしなくてよい」と読めるので、レビュー済みなのに印が pending の
# まま残り取り込めない。**この課題が env 忘れとして直した症状が、gh の到達性という
# 別の経路から同じ顔で戻ってくる形**だった。

def test_unreadable_is_not_reported_as_opted_out(monkeypatch, capsys):
    monkeypatch.delenv("BEACON_REVIEW_GATE_CI", raising=False)

    def boom(*a, **k):
        raise OSError("gh: command not found")

    monkeypatch.setattr(commands.subprocess, "run", boom)
    in_use, why = commands._review_gate_state()
    assert (in_use, why) == (False, "unreadable")

    commands._ci_flip_review_gate_success("42", reviewed_sha="abc123sha")
    err = capsys.readouterr().err
    assert "判定できませんでした" in err, err
    assert "使っていません" not in err.split("判定できませんでした")[0], (
        "判定できないのに『使っていない』と言い切っている")


def test_explicit_opt_out_still_says_so(monkeypatch, capsys):
    """0 を明示した人には、従来どおり『使っていません』と言う。"""
    monkeypatch.setenv("BEACON_REVIEW_GATE_CI", "0")
    assert commands._review_gate_state() == (False, "env")
    commands._ci_flip_review_gate_success("42", reviewed_sha="abc123sha")
    assert "使っていません" in capsys.readouterr().err
