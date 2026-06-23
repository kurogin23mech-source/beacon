"""CLI-side creator stamping tests for ms-43 / e-2281.

Pins the 6 acceptance criteria from the e-2281 task description:

  AC 1. ``cmd_milestone_add`` resolves the caller identity (env / creds /
        members[]) and threads ``author={...}`` through to
        ``core.milestone_add`` so ``meta.author`` is populated on new MS.
  AC 2. ``cmd_task_add`` does the same against ``core.task_add``.
  AC 3. ``cmd_operation_open`` does the same against ``core.operation_open``.
  AC 4. With identity present, the Web UI sees the human author dict
        (``{user_id, email, display_name}``) instead of falling back to
        the legacy ``created_by`` literal.
  AC 5. ``core._get_actor()`` still returns the ``"claude"`` literal under
        ``BEACON_CLAUDE_CODE=1`` — that path is untouched for backward
        compatibility (the UI fallback chain reads ``meta.author`` first
        and only hits ``created_by`` when absent).
  AC 6. Existing entries are not backfilled — the helper only runs on
        the create path, never rewrites past data.

The core-layer contract (= the ``author=`` keyword on milestone_add /
task_add / operation_open) is already pinned by
``test_ms43_e2246_creator_meta.py``. This module pins the CLI wiring on
top of that — i.e. that ``cmd_*_add`` actually passes the resolved
author through instead of silently leaving it ``None``.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands  # noqa: E402
import core  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_project(path, members=None):
    """Write a minimal valid project.json under ``path/.beacon/``."""
    (path / ".beacon").mkdir(parents=True, exist_ok=True)
    data = {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1",
                "title": "Sample",
                "status": "in_progress",
                "progress": 0,
                "target_date": "",
                "entries": [],
                "commits": [],
            }
        ],
        "operations": [],
    }
    if members is not None:
        data["members"] = members
    (path / ".beacon" / "project.json").write_text(json.dumps(data))


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Default: no env identity — tests that need it set their own.
    for var in (
        "BEACON_USER_ID",
        "BEACON_USER_EMAIL",
        "BEACON_DISPLAY_NAME",
        "BEACON_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point BEACON_HOME at a clean tmp so the helper's legacy-credentials
    # fallback does not accidentally read the developer's real creds.
    monkeypatch.setenv("BEACON_HOME", str(tmp_path / ".beacon_home"))
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_current_author — env / creds / members[] resolution
# ---------------------------------------------------------------------------


class TestResolveCurrentAuthor:
    def test_env_vars_take_precedence(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_USER_ID", "u-alice")
        monkeypatch.setenv("BEACON_USER_EMAIL", "alice@example.com")
        monkeypatch.setenv("BEACON_DISPLAY_NAME", "Alice")
        out = commands._resolve_current_author({"members": []})
        assert out == {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }

    def test_display_name_resolves_via_members_when_env_unset(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_USER_ID", "u-bob")
        monkeypatch.setenv("BEACON_USER_EMAIL", "bob@example.com")
        data = {
            "members": [
                {
                    "id": "u-bob",
                    "user_id": "u-bob",
                    "email": "bob@example.com",
                    "display_name": "Bob Builder",
                }
            ]
        }
        out = commands._resolve_current_author(data)
        assert out["display_name"] == "Bob Builder"
        assert out["user_id"] == "u-bob"

    def test_display_name_resolves_via_email_match(
        self, project_dir, monkeypatch
    ):
        # No user_id env, only email — member lookup should still hit.
        monkeypatch.setenv("BEACON_USER_EMAIL", "carol@example.com")
        data = {
            "members": [
                {"id": "u-carol", "email": "carol@example.com",
                 "display_name": "Carol"}
            ]
        }
        out = commands._resolve_current_author(data)
        assert out["display_name"] == "Carol"
        assert out["email"] == "carol@example.com"

    def test_no_identity_returns_empty_dict(self, project_dir):
        # No env vars set, no credentials file → empty dict (= the
        # downstream call passes author=None and skips meta.author).
        out = commands._resolve_current_author({"members": []})
        assert out == {}

    def test_data_none_does_not_crash(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_USER_ID", "u-x")
        monkeypatch.setenv("BEACON_USER_EMAIL", "x@x")
        out = commands._resolve_current_author(None)
        assert out == {"user_id": "u-x", "email": "x@x"}


# ---------------------------------------------------------------------------
# cmd_milestone_add — AC 1
# ---------------------------------------------------------------------------


class TestCmdMilestoneAddStampsAuthor:
    def test_creates_with_meta_author_when_env_set(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_USER_ID", "u-alice")
        monkeypatch.setenv("BEACON_USER_EMAIL", "alice@example.com")
        monkeypatch.setenv("BEACON_DISPLAY_NAME", "Alice")
        monkeypatch.setenv("BEACON_TITLE", "Plan v2")
        commands.cmd_milestone_add()
        data = commands.load_project()
        new_ms = next(m for m in data["milestones"] if m["title"] == "Plan v2")
        assert new_ms["meta"]["author"] == {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }

    def test_creates_without_meta_author_when_unauthenticated(
        self, project_dir, monkeypatch
    ):
        # AC 4 / AC 6 fallback safety: no identity → no meta.author, so the
        # UI falls back to the legacy created_by literal. We must not
        # invent a fake author dict.
        monkeypatch.setenv("BEACON_TITLE", "Local MS")
        commands.cmd_milestone_add()
        data = commands.load_project()
        new_ms = next(m for m in data["milestones"] if m["title"] == "Local MS")
        assert "author" not in new_ms.get("meta", {})

    def test_existing_ms_not_backfilled(self, project_dir, monkeypatch):
        # AC 6: creating a new MS must not touch existing MS's meta.author.
        monkeypatch.setenv("BEACON_USER_ID", "u-z")
        monkeypatch.setenv("BEACON_USER_EMAIL", "z@x")
        monkeypatch.setenv("BEACON_TITLE", "Brand new")
        commands.cmd_milestone_add()
        data = commands.load_project()
        old_ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        assert "author" not in old_ms.get("meta", {})


# ---------------------------------------------------------------------------
# cmd_task_add — AC 2
# ---------------------------------------------------------------------------


class TestCmdTaskAddStampsAuthor:
    def test_creates_with_meta_author_when_env_set(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_USER_ID", "u-alice")
        monkeypatch.setenv("BEACON_USER_EMAIL", "alice@example.com")
        monkeypatch.setenv("BEACON_DISPLAY_NAME", "Alice")
        monkeypatch.setenv("BEACON_DESCRIPTION", "First task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        commands.cmd_task_add()
        data = commands.load_project()
        ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        entry = ms["entries"][-1]
        assert entry["meta"]["author"] == {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }

    def test_creates_without_meta_author_when_unauthenticated(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_DESCRIPTION", "Anon task")
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        commands.cmd_task_add()
        data = commands.load_project()
        ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        entry = ms["entries"][-1]
        assert "author" not in entry.get("meta", {})


# ---------------------------------------------------------------------------
# cmd_operation_open — AC 3
# ---------------------------------------------------------------------------


class TestCmdOperationOpenStampsAuthor:
    def test_creates_with_meta_author_when_env_set(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_USER_ID", "u-alice")
        monkeypatch.setenv("BEACON_USER_EMAIL", "alice@example.com")
        monkeypatch.setenv("BEACON_DISPLAY_NAME", "Alice")
        monkeypatch.setenv("BEACON_OPERATION_TITLE", "Daily check")
        monkeypatch.setenv("BEACON_OPERATION_SCHEDULE", "weekdays")
        commands.cmd_operation_open()
        data = commands.load_project()
        op = data["operations"][-1]
        assert op["meta"]["author"] == {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }

    def test_creates_without_meta_author_when_unauthenticated(
        self, project_dir, monkeypatch
    ):
        monkeypatch.setenv("BEACON_OPERATION_TITLE", "Anon op")
        monkeypatch.setenv("BEACON_OPERATION_SCHEDULE", "weekly")
        commands.cmd_operation_open()
        data = commands.load_project()
        op = data["operations"][-1]
        assert "author" not in op.get("meta", {})


# ---------------------------------------------------------------------------
# AC 5 — _get_actor "claude" literal preserved for backward compat
# ---------------------------------------------------------------------------


class TestGetActorClaudeLiteralPreserved:
    def test_get_actor_still_returns_claude_under_env(self, monkeypatch):
        # The UI fallback chain reads meta.author first; created_by /
        # _get_actor stays untouched so legacy MS / entries don't regress.
        monkeypatch.setenv("BEACON_CLAUDE_CODE", "1")
        assert core._get_actor() == "claude"
