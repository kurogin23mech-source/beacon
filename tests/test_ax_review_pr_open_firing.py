"""ms-119 / e-4003 — AX review fires structurally at the PR-open 節目.

The independent 目的達成 review found the review firing spine had no PR-open leg:
AX 原典 §2 binds AX to interface-change (a PR / diff), but nothing fired AX when
a PR appeared — AX ran only when a human remembered `/beacon-review-run`. Beacon
learns of an interface change when a PR is recorded (`beacon pr add`), so AX
auto-binds there via a persistent `ax-review-due` trigger that `beacon trigger
check` / session-start re-surface until the review runs or the PR closes.

These tests pin the firing / clearing helpers + URL parsing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import commands_shared  # noqa: E402  (ms-127 e-4856: trigger helpers promoted here)
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)


def test_pr_number_from_url_parses_github_pr():
    assert cmd_pr._pr_number_from_url("https://github.com/o/r/pull/477") == "477"
    assert cmd_pr._pr_number_from_url("https://github.com/o/r/pull/1?x=1") == "1"
    assert cmd_pr._pr_number_from_url("") == ""
    assert cmd_pr._pr_number_from_url("https://github.com/o/r/issues/5") == ""


def test_fire_writes_ax_review_due_trigger(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tdir))
    commands._fire_ax_review_due_trigger("477", "fix: something", "https://github.com/o/r/pull/477")
    path = tdir / "ax-review-due-477.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kind"] == "ax-review-due"
    assert data["pr_number"] == "477"
    assert data["review"] == "ax"
    # the message must name the concrete review invocation (structural, not vague)
    assert "--type ax --pr 477" in data["message"]


def test_fire_is_noop_without_pr_number(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tdir))
    commands._fire_ax_review_due_trigger("", "t", "not-a-pr-url")
    # no trigger dir / file created for an unparseable PR
    assert not (tdir / "ax-review-due-.json").exists()


def test_clear_removes_ax_review_due_trigger(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tdir))
    commands._fire_ax_review_due_trigger("500", "t", "https://github.com/o/r/pull/500")
    assert (tdir / "ax-review-due-500.json").exists()
    commands._clear_ax_review_due_trigger("500")
    assert not (tdir / "ax-review-due-500.json").exists()
    # clearing a non-existent one is a harmless no-op
    commands._clear_ax_review_due_trigger("500")
    commands._clear_ax_review_due_trigger("")


def test_trigger_check_surfaces_ax_review_due(tmp_path, monkeypatch, capsys):
    """The trigger is a real file, so `beacon trigger check` (dir scan) surfaces
    it with no special-casing — the structural re-surfacing that keeps AX from
    flowing away unread."""
    tdir = tmp_path / "triggers"
    monkeypatch.setattr(commands_shared, "_get_triggers_dir", lambda: str(tdir))
    # avoid the cloud-touching opportunistic tick during the check
    monkeypatch.setattr(commands, "_maybe_auto_tick", lambda: None)
    commands._fire_ax_review_due_trigger("42", "feat: x", "https://github.com/o/r/pull/42")
    commands.cmd_trigger_check()
    out = capsys.readouterr().out
    payload = json.loads(out)
    kinds = [t.get("kind") for t in payload]
    assert "ax-review-due" in kinds
