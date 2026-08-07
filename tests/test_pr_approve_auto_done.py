"""Unit tests for the e-2369 / ms-95 auto-done-at-pr-approve feature.

When ``beacon pr approve <pr-entry-id>`` is called, the tasks bound to
the PR's commits (via ``meta.resolves`` on the beacon-logged side, or
``e-XXX`` mention in the commit message) should be auto-done when the
judgement comes back HIGH confidence — the same AC-keyword × explicit-id
3-way judgement already used by the ``/beacon-log`` Step 1.9 prompt.

MID-confidence candidates are surfaced as warnings (not auto-done) and
LOW / unmatched ones drop silently. ``BEACON_NO_AUTO_DONE=1`` bypasses
the whole auto-done step.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)
import core  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _make_project(*, with_resolves: bool = True,
                  with_id_mention: bool = True,
                  task_status: str = "todo",
                  task_desc: str = "implement email validator helper",
                  ac: str = "email validator helper rejects malformed addresses",
                  pr_title: str = "feat(ms-1): e-300 add email validator helper",
                  commit_msg: str = "feat(ms-1): e-300 add email validator helper"):
    """A project with one task (e-300), one beacon-logged commit, and one
    PR (e-400) that contains the same commit as a child entry.

    Knobs:
      * ``with_resolves`` — set ``meta.resolves`` on the beacon-logged
        commit so the hash-join finds the bound task.
      * ``with_id_mention`` — keep ``e-300`` in commit / PR title text
        so the regex-extraction path also fires.
      * ``task_status`` — flip to ``"done"`` to test idempotency.
      * ``task_desc`` / ``ac`` — overridden by callers exercising the
        keyword-overlap logic.
      * ``pr_title`` / ``commit_msg`` — overridden by MID / LOW cases
        that strip the task-id from the corpus.
    """
    commit_meta = {"hash": "abc1234"}
    if with_resolves:
        commit_meta["resolves"] = "e-300"

    return {
        "name": "t",
        "summary": "",
        "milestones": [{
            "id": "ms-1",
            "title": "milestone 1",
            "status": "in_progress",
            "entries": [
                {
                    "id": "e-300",
                    "type": "task",
                    "description": task_desc,
                    "acceptance_criteria": ac,
                    "status": task_status,
                },
                # beacon-logged commit (the "log" side of the binding)
                {
                    "id": "e-310",
                    "type": "commit",
                    "description": commit_msg if with_id_mention
                                   else commit_msg.replace("e-300 ", ""),
                    "status": "done",
                    "meta": commit_meta,
                },
                # The PR with the same commit as a child entry. PR-side
                # commits only carry meta.hash (this matches pr_add's
                # actual write shape).
                {
                    "id": "e-400",
                    "type": "pr",
                    "description": pr_title if with_id_mention
                                   else pr_title.replace("e-300 ", ""),
                    "status": "in_review",
                    "meta": {
                        "url": "https://github.com/x/y/pull/42",
                        "pr_number": 42,
                        "pr_status": "in_review",
                        "review_status": "pending",
                        "intent": "ship the validator",
                    },
                    "entries": [{
                        "id": "e-401",
                        "type": "commit",
                        "description": commit_msg if with_id_mention
                                       else commit_msg.replace("e-300 ", ""),
                        "status": "done",
                        "meta": {"hash": "abc1234"},
                    }],
                },
            ],
        }],
    }


def _run_pr_approve(data, *, entry_id="e-400", rationale="LGTM",
                     no_auto_done=False, json_mode=False):
    """Invoke cmd_pr_approve in-process via the BEACON_* env interface,
    with the project file stubbed against a temp path.
    """
    # Patch load_project / save_project to operate against ``data`` in
    # memory. This mirrors the pattern other commands.py tests use to
    # avoid a real .beacon directory.
    captured = {"saved": None}
    orig_load = cmd_pr.load_project
    orig_save = cmd_pr.save_project
    cmd_pr.load_project = lambda: data
    cmd_pr.save_project = lambda d: captured.__setitem__("saved", d)

    env_backup = {
        k: os.environ.get(k)
        for k in ("BEACON_ENTRY_ID", "BEACON_RATIONALE",
                  "BEACON_JSON", "BEACON_NO_AUTO_DONE")
    }
    os.environ["BEACON_ENTRY_ID"] = entry_id
    os.environ["BEACON_RATIONALE"] = rationale
    os.environ["BEACON_JSON"] = "1" if json_mode else ""
    os.environ["BEACON_NO_AUTO_DONE"] = "1" if no_auto_done else ""

    out = io.StringIO()
    err = io.StringIO()
    rc = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                commands.cmd_pr_approve()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        cmd_pr.load_project = orig_load
        cmd_pr.save_project = orig_save
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return {
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "rc": rc,
        "data": data,
    }


def _task(data, tid):
    for ms in data["milestones"]:
        for ent in ms.get("entries", []):
            if ent.get("id") == tid:
                return ent
    return None


# ---------------------------------------------------------------------------
# Helper: _collect_pr_bound_task_ids
# ---------------------------------------------------------------------------

def test_collect_bound_ids_hash_join():
    """meta.resolves on beacon-logged commit is hash-joined to PR child."""
    data = _make_project(with_resolves=True, with_id_mention=False)
    pr_entry = data["milestones"][0]["entries"][2]
    ids = cmd_pr._collect_pr_bound_task_ids(pr_entry, data)
    assert ids == ["e-300"]


def test_collect_bound_ids_regex_fallback():
    """No resolves on logged commit — regex on commit message still binds."""
    data = _make_project(with_resolves=False, with_id_mention=True)
    pr_entry = data["milestones"][0]["entries"][2]
    ids = cmd_pr._collect_pr_bound_task_ids(pr_entry, data)
    assert "e-300" in ids


def test_collect_bound_ids_dedupe():
    """Both signals firing → still one entry per task id."""
    data = _make_project(with_resolves=True, with_id_mention=True)
    pr_entry = data["milestones"][0]["entries"][2]
    ids = cmd_pr._collect_pr_bound_task_ids(pr_entry, data)
    assert ids == ["e-300"]


# ---------------------------------------------------------------------------
# Helper: _judge_pr_approve_auto_done
# ---------------------------------------------------------------------------

def test_judge_high_when_explicit_id_and_keyword_overlap():
    data = _make_project(with_resolves=True, with_id_mention=True)
    pr_entry = data["milestones"][0]["entries"][2]
    j = cmd_pr._judge_pr_approve_auto_done(pr_entry, data)
    assert len(j) == 1
    assert j[0]["task_id"] == "e-300"
    assert j[0]["confidence"] == "HIGH"
    assert j[0]["ac_matched"]  # at least 1 keyword


def test_judge_mid_when_keyword_overlap_only():
    """No explicit id mention but description keywords overlap."""
    data = _make_project(
        with_resolves=True,  # binds the task via hash-join
        with_id_mention=False,  # strips e-300 from corpus text
        pr_title="feat: add email validator helper",
        commit_msg="feat: add email validator helper",
    )
    pr_entry = data["milestones"][0]["entries"][2]
    j = cmd_pr._judge_pr_approve_auto_done(pr_entry, data)
    assert len(j) == 1
    assert j[0]["confidence"] == "MID"


def test_judge_skips_done_tasks():
    data = _make_project(task_status="done")
    pr_entry = data["milestones"][0]["entries"][2]
    j = cmd_pr._judge_pr_approve_auto_done(pr_entry, data)
    assert j == []


def test_judge_skips_unknown_task_id():
    data = _make_project(with_resolves=True, with_id_mention=True)
    # Remove the task so the resolves pointer is dangling.
    data["milestones"][0]["entries"] = [
        e for e in data["milestones"][0]["entries"] if e.get("id") != "e-300"
    ]
    pr_entry = data["milestones"][0]["entries"][1]  # PR shifted to index 1
    j = cmd_pr._judge_pr_approve_auto_done(pr_entry, data)
    assert j == []


# ---------------------------------------------------------------------------
# Command: cmd_pr_approve auto-done integration
# ---------------------------------------------------------------------------

def test_high_confidence_auto_dones_task():
    data = _make_project(with_resolves=True, with_id_mention=True)
    res = _run_pr_approve(data)
    assert res["rc"] in (0, None)
    assert _task(data, "e-300")["status"] == "done"
    done_reason = _task(data, "e-300")["meta"].get("done_reason", "")
    assert "Auto-done from PR approve" in done_reason
    assert "#42" in done_reason  # PR number in the reason
    assert "AC keywords matched" in done_reason
    assert "✓ Auto-done: e-300" in res["stdout"]


def test_mid_confidence_warns_only():
    data = _make_project(
        with_resolves=True,
        with_id_mention=False,
        pr_title="feat: add email validator helper",
        commit_msg="feat: add email validator helper",
    )
    res = _run_pr_approve(data)
    # Task stays todo
    assert _task(data, "e-300")["status"] == "todo"
    # MID warning printed
    assert "⚠ Candidate: e-300" in res["stdout"]
    assert "MID confidence" in res["stdout"]


def test_low_confidence_silent():
    """Task is bound by resolves but shares zero keywords with the PR
    corpus → judgement returns MID-fallback (= explicit binding alone).

    Build a case where neither resolves nor id mention applies → the
    candidate set is empty → silent."""
    data = _make_project(with_resolves=False, with_id_mention=False,
                          pr_title="feat: unrelated change",
                          commit_msg="feat: tweak readme")
    res = _run_pr_approve(data)
    assert _task(data, "e-300")["status"] == "todo"
    assert "Auto-done" not in res["stdout"]
    assert "Candidate" not in res["stdout"]


def test_no_auto_done_flag_bypasses_helper():
    data = _make_project(with_resolves=True, with_id_mention=True)
    res = _run_pr_approve(data, no_auto_done=True)
    assert _task(data, "e-300")["status"] == "todo"
    assert "Auto-done" not in res["stdout"]
    # Approve itself still happened
    pr = data["milestones"][0]["entries"][2]
    assert pr["meta"]["review_status"] == "approved"


def test_pr_with_no_commits_is_noop():
    data = _make_project(with_resolves=False, with_id_mention=False)
    # Strip the PR's child commits entirely
    pr = data["milestones"][0]["entries"][2]
    pr["entries"] = []
    res = _run_pr_approve(data)
    assert _task(data, "e-300")["status"] == "todo"
    assert "Auto-done" not in res["stdout"]
    # Approve completed
    assert pr["meta"]["review_status"] == "approved"


def test_pr_not_found_fails_soft():
    """A bad entry id surfaces the ``find_entry`` ValueError from
    ``core.pr_approve`` as a non-zero exit, NOT a crash inside the
    auto-done helper."""
    data = _make_project(with_resolves=True, with_id_mention=True)
    res = _run_pr_approve(data, entry_id="e-9999")
    assert res["rc"] != 0
    assert "Entry not found" in res["stderr"]


def test_json_mode_includes_auto_done_payload():
    data = _make_project(with_resolves=True, with_id_mention=True)
    res = _run_pr_approve(data, json_mode=True)
    parsed = json.loads(res["stdout"].strip())
    assert parsed["review_status"] == "approved"
    assert any(a["task_id"] == "e-300" for a in parsed["auto_done"])


# ---------------------------------------------------------------------------
# Regression: existing pr_approve invariants
# ---------------------------------------------------------------------------

def test_approve_still_sets_status_and_history():
    """Smoke test that the surrounding approve flow is unchanged."""
    data = _make_project()
    _run_pr_approve(data)
    pr = data["milestones"][0]["entries"][2]
    assert pr["meta"]["pr_status"] == "approved"
    assert pr["meta"]["review_status"] == "approved"
    assert pr["status"] == "approved"
    # review_history appended
    assert pr["meta"].get("review_history")
    assert pr["meta"]["review_history"][-1]["status"] == "approved"
