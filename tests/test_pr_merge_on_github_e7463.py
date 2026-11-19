"""``beacon pr merge`` が GitHub で実際に取り込み、確かめてから記録すること (ms-119 e-7463)。

**何が起きていた** (2026-11-12, PR #846): ``beacon pr merge`` は動詞名も出力も
「Merged」と言うのに、実体は beacon の記録を merged にするだけで GitHub では何も
起きなかった。記録は merged、GitHub は OPEN / mergeCommit=null のまま exit 0。

固定する契約 (e-7463 の受入条件、方針は user 判断「実際に取り込みまで行う」)
-----------------------------------------------------------------------
1. 動詞名と出力が実態と一致する: GitHub で取り込んでから「取り込みました」と言う
2. 記録と GitHub が食い違ったまま成功を返さない: 取り込めない / 確かめられない
   ときは記録を変えずに非 0 で止まる
3. 取り込みは ``gh pr merge --merge`` (merge commit)。``--auto`` は付けない

gh は ``gh_port`` の adapter を差し替える (試験中は生きた GitHub を叩かない)。
"""

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

import cmd_pr
import gh_port

URL = "https://github.com/o/r/pull/7"
SHA = "c" * 40


class FakeGh:
    """PR の状態を順に返し、``gh pr merge`` の呼び出しを記録する adapter。"""

    def __init__(self, states, merge_rc=0, merge_err=""):
        self.states = list(states)
        self.merge_rc = merge_rc
        self.merge_err = merge_err
        self.runs = []

    def pr_head(self, ref, repo=""):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"headRefOid": SHA, "state": state, "url": URL}

    def run(self, argv):
        self.runs.append(list(argv))

        class R:
            pass
        r = R()
        r.returncode = self.merge_rc
        r.stdout = ""
        r.stderr = self.merge_err
        return r


def _project():
    return {"milestones": [{"id": "ms-1", "title": "t", "status": "in_progress",
                            "entries": [{"id": "e-1", "type": "pr",
                                         "description": "pr", "status": "in_review",
                                         "meta": {"url": URL, "pr_status": "approved",
                                                  "pr_repo": "o/r"}}]}]}


@pytest.fixture
def merge(monkeypatch):
    """``beacon pr merge e-1`` を走らせ (exit, out, err, saved, fake) を返す関数。"""
    saved = []

    def run(fake):
        data = _project()
        monkeypatch.setattr(gh_port, "_adapter", fake, raising=False)
        monkeypatch.setattr(cmd_pr, "load_project", lambda: data)
        monkeypatch.setattr(cmd_pr, "save_project", lambda d: saved.append(d))
        monkeypatch.setattr(cmd_pr, "_review_gate_check", lambda *a, **k: None)
        monkeypatch.setattr(cmd_pr, "_clear_pr_open_review_triggers",
                            lambda *a, **k: None)
        monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
        monkeypatch.setenv("BEACON_SESSION_KIND", "human")
        monkeypatch.delenv("BEACON_JSON", raising=False)
        out, err = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                cmd_pr.cmd_pr_merge()
        except SystemExit as e:
            code = e.code or 0
        return code, out.getvalue(), err.getvalue(), saved, fake
    return run


def test_an_open_pr_is_merged_on_github_then_recorded(merge):
    """**本体 (受入 1)**: OPEN の PR を gh で取り込み、MERGED を確かめてから記録する。"""
    code, out, err, saved, fake = merge(FakeGh(["OPEN", "MERGED"]))
    assert code == 0, err
    assert fake.runs == [["gh", "pr", "merge", "7", "--merge",
                          "--match-head-commit", SHA, "--repo", "o/r"]], fake.runs
    assert saved, "取り込んだのに記録していない"
    assert "GitHub で取り込みました" in out, out


def test_auto_merge_is_never_requested(merge):
    """``--auto`` は CI を待たずに即座に取り込まれうるので付けない (受入 3)。"""
    _code, _out, _err, _saved, fake = merge(FakeGh(["OPEN", "MERGED"]))
    assert all("--auto" not in argv for argv in fake.runs), fake.runs


def test_a_failed_github_merge_records_nothing(merge):
    """受入 2: gh の取り込みが失敗したら、記録を変えずに非 0。"""
    code, _out, err, saved, _fake = merge(
        FakeGh(["OPEN"], merge_rc=1, merge_err="Pull request is not mergeable"))
    assert code != 0
    assert not saved, "取り込めていないのに merged と記録した"
    assert "not mergeable" in err and "記録は変えていません" in err, err


def test_a_merge_that_did_not_land_records_nothing(merge):
    """gh が成功を返しても GitHub がまだ OPEN なら記録しない (予約になった等)。"""
    code, _out, err, saved, _fake = merge(FakeGh(["OPEN", "OPEN"]))
    assert code != 0
    assert not saved
    assert "まだ" in err, err


def test_an_already_merged_pr_is_just_recorded(merge):
    """GitHub で既に取り込み済みなら、gh を打たずに記録だけ揃える。"""
    code, out, _err, saved, fake = merge(FakeGh(["MERGED"]))
    assert code == 0
    assert fake.runs == [], fake.runs
    assert saved and "既に取り込み済み" in out, out


def test_a_closed_pr_is_refused(merge):
    code, _out, err, saved, fake = merge(FakeGh(["CLOSED"]))
    assert code != 0 and not saved and not fake.runs
    assert "閉じられています" in err, err


def test_an_unknown_state_is_refused_not_guessed(merge, monkeypatch):
    """GitHub の状態を確かめられないときは、取り込みも記録もしない。"""
    class Broken(FakeGh):
        def pr_head(self, ref, repo=""):
            raise RuntimeError("gh: not logged in")
    code, _out, err, saved, fake = merge(Broken(["OPEN"]))
    assert code != 0 and not saved and not fake.runs
    assert "確かめられませんでした" in err, err


def test_json_mode_says_what_happened_on_github(monkeypatch):
    """--json では GitHub で何が起きたか (取り込んだ / 既に取り込み済み) を返す。"""
    data_fake = FakeGh(["OPEN", "MERGED"])
    import cmd_pr as _c
    saved = []
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(gh_port, "_adapter", data_fake, raising=False)
    monkeypatch.setattr(_c, "load_project", lambda: _project())
    monkeypatch.setattr(_c, "save_project", lambda d: saved.append(d))
    monkeypatch.setattr(_c, "_review_gate_check", lambda *a, **k: None)
    monkeypatch.setattr(_c, "_clear_pr_open_review_triggers", lambda *a, **k: None)
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_SESSION_KIND", "human")
    out = io.StringIO()
    with redirect_stdout(out):
        _c.cmd_pr_merge()
    payload = json.loads(out.getvalue())
    assert payload["github"] == "merged-now" and payload["pr_status"] == "merged", payload


# --- PR #855 独立レビューで足した契約 ------------------------------------------

def _merge_with_gate(monkeypatch, fake, gate):
    saved = []
    data = _project()
    monkeypatch.setattr(gh_port, "_adapter", fake, raising=False)
    monkeypatch.setattr(cmd_pr, "load_project", lambda: data)
    monkeypatch.setattr(cmd_pr, "save_project", lambda d: saved.append(d))
    monkeypatch.setattr(cmd_pr, "_review_gate_check", gate)
    monkeypatch.setattr(cmd_pr, "_clear_pr_open_review_triggers",
                        lambda *a, **k: None)
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_SESSION_KIND", "human")
    monkeypatch.delenv("BEACON_JSON", raising=False)
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            cmd_pr.cmd_pr_merge()
    except SystemExit as e:
        code = e.code or 0
    return code, err.getvalue(), saved


def test_a_head_that_moved_after_the_gate_is_not_merged(monkeypatch):
    fake = FakeGh(["OPEN"])                          # いまの head は SHA (= "c"*40)
    code, err, saved = _merge_with_gate(
        monkeypatch, fake, lambda *a, **k: "a" * 40)  # 関門は別の版を判定した
    assert code != 0 and not saved and not fake.runs, (fake.runs, err)
    assert "動きました" in err, err


def test_the_gate_runs_before_anything_touches_github(monkeypatch):
    """関門で止まったら gh を 1 回も打たない (取り込みは取り消せない、保守性)。"""
    fake = FakeGh(["OPEN", "MERGED"])
    order = []

    def gate(*a, **k):
        order.append(("gate", len(fake.runs)))
        raise SystemExit(2)
    code, _err, saved = _merge_with_gate(monkeypatch, fake, gate)
    assert code == 2 and not saved
    assert fake.runs == [] and order == [("gate", 0)], (order, fake.runs)


def test_an_ai_session_never_reaches_github(monkeypatch):
    fake = FakeGh(["OPEN", "MERGED"])
    monkeypatch.setattr(gh_port, "_adapter", fake, raising=False)
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    monkeypatch.delenv("BEACON_PR_MERGE_USER_OVERRIDE", raising=False)
    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cmd_pr.cmd_pr_merge()
    assert fake.runs == [], fake.runs


def test_an_unknown_repository_is_not_filled_in_from_the_cwd(monkeypatch):
    """リポジトリを読めない記録は取り込まない (cwd の同番号 PR を取り込まない、AX 実測)。"""
    fake = FakeGh(["OPEN", "MERGED"])
    saved = []
    data = _project()
    entry = data["milestones"][0]["entries"][0]
    entry["meta"] = {"url": "https://example.com/x/pull/7", "pr_status": "approved"}
    monkeypatch.setattr(gh_port, "_adapter", fake, raising=False)
    monkeypatch.setattr(cmd_pr, "load_project", lambda: data)
    monkeypatch.setattr(cmd_pr, "save_project", lambda d: saved.append(d))
    monkeypatch.setattr(cmd_pr, "_review_gate_check", lambda *a, **k: SHA)
    monkeypatch.setattr(cmd_pr, "_recorded_pr_repo", lambda e: "")
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_SESSION_KIND", "human")
    err = io.StringIO()
    with pytest.raises(SystemExit):
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            cmd_pr.cmd_pr_merge()
    assert fake.runs == [] and not saved, fake.runs
    assert "リポジトリ" in err.getvalue(), err.getvalue()


# --- task done 経路: GitHub で MERGED を確かめたときだけ記録する (保守性) ----

def _task_done(monkeypatch, state):
    import cmd_task

    class Head:
        def pr_head(self, ref, repo=""):
            return {"headRefOid": SHA, "state": state, "url": URL}
    saved = []
    data = _project()
    monkeypatch.setattr(gh_port, "_adapter", Head(), raising=False)
    monkeypatch.setattr(cmd_task, "load_project", lambda: data)
    monkeypatch.setattr(cmd_task, "save_project", lambda d: saved.append(d))
    monkeypatch.setattr(cmd_task, "_check_ms_status_for_write", lambda *a, **k: True)
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "取り込み済みの PR を記録する")
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            cmd_task.cmd_task_done()
    except SystemExit as e:
        code = e.code or 0
    return code, err.getvalue(), saved


def test_task_done_on_an_open_pr_records_nothing(monkeypatch):
    """task done で PR を merged にしない (GitHub が OPEN のまま食い違う経路だった)。"""
    code, err, saved = _task_done(monkeypatch, "OPEN")
    assert code != 0 and not saved, err
    assert "beacon pr merge" in err, err


def test_task_done_on_a_merged_pr_records_it(monkeypatch):
    code, _err, saved = _task_done(monkeypatch, "MERGED")
    assert code == 0 and saved
