"""Tests for the ms-81 doctor state-machine warnings (e-1921, SPEC AC #9).

Pins the four warning families. Each test feeds a minimal project dict
into the helper directly; we avoid invoking the full cmd_doctor because
that pulls in PATH / cloud / skill checks that are out of scope here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands


def _project(*milestones, worktree_sessions=None):
    data = {"name": "test", "milestones": list(milestones)}
    if worktree_sessions is not None:
        data["worktree_sessions"] = worktree_sessions
    return data


def _ms(ms_id, **fields):
    base = {
        "id": ms_id, "title": ms_id, "status": "in_progress",
        "progress": 0, "target_date": "", "entries": [], "commits": [],
    }
    base.update(fields)
    return base


def _patch_load_project(monkeypatch, data):
    monkeypatch.setattr(commands, "load_project", lambda: data)


class TestDoctorStateMachineWarnings:
    def test_no_warnings_when_clean(self, monkeypatch):
        _patch_load_project(
            monkeypatch,
            _project(_ms("ms-1", assignee="alice")),
        )
        assert commands._doctor_check_ms81_state_machine() == []

    def test_missing_assignee_warns(self, monkeypatch):
        _patch_load_project(
            monkeypatch,
            _project(_ms("ms-1", assignee="")),
        )
        out = commands._doctor_check_ms81_state_machine()
        assert any("ms81-assignee-missing" in w for w in out)
        assert any("ms-1" in w for w in out)

    def test_done_with_leftover_worktree_warns(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        _patch_load_project(
            monkeypatch,
            _project(_ms("ms-1", status="done", assignee="alice")),
        )
        import branch as _branch
        branch_name = _branch.ms_branch_name("ms-1", "ms-1")
        (tmp_path / ".worktrees" / branch_name).mkdir(parents=True)
        out = commands._doctor_check_ms81_state_machine()
        assert any("ms81-leftover-worktree" in w for w in out)

    def test_stale_occupation_warns(self, monkeypatch):
        _patch_load_project(
            monkeypatch,
            _project(_ms(
                "ms-1",
                assignee="alice",
                occupation={"session_id": "", "machine": "mac"},
            )),
        )
        out = commands._doctor_check_ms81_state_machine()
        assert any("ms81-stale-occupation" in w for w in out)

    def test_waiting_with_post_wait_commit_warns(self, monkeypatch):
        _patch_load_project(
            monkeypatch,
            _project(_ms(
                "ms-1",
                status="waiting",
                assignee="alice",
                meta={"waiting_at": "2026-06-18T00:00:00Z"},
                entries=[
                    {
                        "id": "e-1", "type": "commit",
                        "description": "after waiting",
                        "created_at": "2026-06-18T12:00:00Z",
                        "status": "done",
                    }
                ],
            )),
        )
        out = commands._doctor_check_ms81_state_machine()
        assert any("ms81-waiting-write" in w for w in out)

    def test_waiting_with_pre_wait_commit_does_not_warn(self, monkeypatch):
        _patch_load_project(
            monkeypatch,
            _project(_ms(
                "ms-1",
                status="waiting",
                assignee="alice",
                meta={"waiting_at": "2026-06-18T12:00:00Z"},
                entries=[
                    {
                        "id": "e-1", "type": "commit",
                        "description": "before waiting",
                        "created_at": "2026-06-18T00:00:00Z",
                        "status": "done",
                    }
                ],
            )),
        )
        out = commands._doctor_check_ms81_state_machine()
        assert not any("ms81-waiting-write" in w for w in out)
