"""Tests for the review-as-merge-gate loop (ms-119 e-4060): a fired review-due
trigger structurally blocks beacon pr approve/merge until the review is run
(`beacon review done`), closing the "fires into a void, gets skipped" hole."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)


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
    assert commands._pending_review_types_for_pr("482") == []


def test_pending_lists_fired_types(proj):
    _fire(proj, "ax", "482")
    _fire(proj, "maintainability", "482")
    _fire(proj, "ax", "999")  # other PR — must not leak in
    assert sorted(commands._pending_review_types_for_pr("482")) == \
        ["ax", "maintainability"]


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def test_gate_blocks_when_pending(proj, capsys):
    _fire(proj, "ax", "482")
    with pytest.raises(SystemExit) as e:
        cmd_pr._review_gate_check("482", action="approve")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "未実施" in err and "ax" in err


def test_gate_passes_when_none_pending(proj):
    cmd_pr._review_gate_check("482", action="approve")  # no raise


def test_gate_override_proceeds_with_audit(proj, monkeypatch, capsys):
    _fire(proj, "ax", "482")
    monkeypatch.setenv("BEACON_PR_REVIEW_OVERRIDE", "1")
    cmd_pr._review_gate_check("482", action="merge")  # no raise
    assert "override" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# review done — clears trigger, stamps marker, unblocks
# ---------------------------------------------------------------------------

def test_review_done_clears_and_unblocks(proj, monkeypatch, capsys):
    _fire(proj, "ax", "482")
    _fire(proj, "maintainability", "482")
    # run the ax review
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "482")
    commands.cmd_review_done()
    assert commands._pending_review_types_for_pr("482") == ["maintainability"]
    # still blocked (maintainability owed)
    with pytest.raises(SystemExit):
        cmd_pr._review_gate_check("482", action="approve")
    # run the maintainability review → all clear
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "maintainability")
    commands.cmd_review_done()
    assert commands._pending_review_types_for_pr("482") == []
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
