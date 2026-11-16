"""同じ完了主張の 2 つの入口が、同じレビュー義務を立てることの固定
(ms-119 / e-7440)。

**何が起きていた** (2026-11-11 実踏): 完了主張を人間承認に回す入口は 2 つある
—— `beacon milestone done <id> --review` (cmd_milestone の
`_route_completion_to_review`) と `beacon target review-request <id> --new-state ...`
(cmd_target の `cmd_target_review_request`)。前者は承認依頼を作った直後に
review-due (= 節目のレビュー義務) を発火していたが、**後者は発火していなかった**。

その非等価が外に出た形: review-request から入ったあと実際に思想レビューを実施しても
`beacon review done --type philosophy --target <id>` が「レビュー未実施トリガーが
ありません (完了の節目が発火していない)」で拒否する。**実施したレビューを記録する
口が無い**ので、2026-11-11 の思想レビュー 7 件の findings は台帳ではなく report doc
に退避するしかなかった。トリガーを手で捏造すれば通るが、それは記録の意味を壊す。
ms-119 が閉じようとした「実施されたか分からない」の裏返しの穴。

**この module が測るもの**: 文面ではなく **発火の等価性**。同じ target・同じ遷移を
2 つの入口から通し、立つ review-due の binding 集合が一致することを突き合わせる。
片方だけ直ったら赤くなる。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import cmd_target  # noqa: E402
import cmd_milestone  # noqa: E402
import commands_shared as cs  # noqa: E402

MS = "ms-7440"


def _project(tmp_path, status="in_progress"):
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t",
        "milestones": [{"id": MS, "title": "テスト target", "status": status,
                        "progress": 90, "entries": []}],
        "operations": [],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path / ".beacon" / "project.json"


class _Store:
    """SPEC doc を 1 件持つ store (= 思想 binding が立つ前提)。"""

    def __init__(self, proj_file):
        self._proj = proj_file

    def load_project(self):
        return json.loads(self._proj.read_text(encoding="utf-8"))

    def save_project(self, data):
        self._proj.write_text(json.dumps(data, ensure_ascii=False),
                              encoding="utf-8")

    def list_documents(self):
        return [{"doc_id": "spec-7440", "scope": "spec", "milestone": MS,
                 "updated_at": "2026-11-11T00:00:00"}]

    def get_document(self, doc_id):
        return {"content": "# SPEC\n"} if doc_id == "spec-7440" else None


@pytest.fixture
def env(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj))
    store = _Store(proj)
    for mod in (cs, commands, cmd_target, cmd_milestone):
        monkeypatch.setattr(mod, "get_store", lambda: store, raising=False)
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_milestone, "_actor_str", lambda: "m/a",
                        raising=False)
    for k in ("BEACON_TARGET_ID", "BEACON_NEW_STATE", "BEACON_OLD_STATE",
              "BEACON_INTENT", "BEACON_EVIDENCE", "BEACON_MS_ID",
              "BEACON_REASON", "BEACON_REVIEW"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path, monkeypatch


def _review_due(tmp_path):
    p = tmp_path / ".beacon" / "triggers" / f"review-due-{MS}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def test_review_request_fires_the_review_due(env, capsys):
    """**本体**: review-request から入っても節目のレビュー義務が立つ。

    修正前はトリガーが 1 つも書かれず、その後に実施したレビューを
    `beacon review done --target` が受け付けられなかった。
    """
    tmp_path, monkeypatch = env
    monkeypatch.setenv("BEACON_TARGET_ID", MS)
    monkeypatch.setenv("BEACON_NEW_STATE", "observing")
    cmd_target.cmd_target_review_request()
    capsys.readouterr()
    fired = _review_due(tmp_path)
    assert fired is not None, "review-due が発火していない"
    assert "philosophy" in (fired.get("bindings") or []), fired


def test_both_entries_raise_the_same_obligation(tmp_path, monkeypatch, capsys):
    """**等価性**: 2 つの入口が立てる binding 集合が一致する。

    `round 8 の『入口 2 つが非等価』` と同根なので、片方だけ発火する形に戻ったら
    ここで止める。
    """
    def _run(which):
        d = tmp_path / which
        d.mkdir()
        proj = _project(d)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(proj))
        store = _Store(proj)
        for mod in (cs, commands, cmd_target, cmd_milestone):
            monkeypatch.setattr(mod, "get_store", lambda: store, raising=False)
        monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
        monkeypatch.setattr(cmd_milestone, "_actor_str", lambda: "m/a",
                            raising=False)
        if which == "via-target":
            monkeypatch.setenv("BEACON_TARGET_ID", MS)
            monkeypatch.setenv("BEACON_NEW_STATE", "observing")
            cmd_target.cmd_target_review_request()
        else:
            cmd_milestone._route_completion_to_review(
                store.load_project(), MS, reason="", new_state="observing")
        capsys.readouterr()
        fired = _review_due(d)
        return sorted((fired or {}).get("bindings") or [])

    via_target = _run("via-target")
    via_milestone = _run("via-milestone")
    assert via_target == via_milestone, (
        f"入口で立つ義務が違います: review-request={via_target} / "
        f"milestone done --review={via_milestone}")
    assert via_target, "どちらの入口でも義務が立っていない (前提が崩れている)"


def test_recorded_review_is_accepted_after_review_request(env, capsys):
    """**実害の消滅**: review-request の後に実施したレビューを記録できる。

    修正前は `review done --target` が exit 2 (「レビュー未実施トリガーが
    ありません」) で、実施したレビューを残す口が無かった。
    """
    tmp_path, monkeypatch = env
    monkeypatch.setenv("BEACON_TARGET_ID", MS)
    monkeypatch.setenv("BEACON_NEW_STATE", "observing")
    cmd_target.cmd_target_review_request()
    capsys.readouterr()
    # 記録側が触る外部 (判断記録 / 作業フォルダ横断台帳) は本題でないので止める。
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "record_review_done_across_worktrees",
                        lambda *a, **k: commands.RECORDED_CWD_ONLY)
    commands._emit_review_done_for_target(
        "philosophy", MS, adj_summary="", adjudications=[], no_findings=True)
    out = capsys.readouterr().out
    assert "レビュー実施を記録" in out, out
    # 義務は消えている (冪等な打ち直しが「解消済み」と言える状態)。
    fired = _review_due(tmp_path)
    assert "philosophy" not in ((fired or {}).get("bindings") or []), fired
