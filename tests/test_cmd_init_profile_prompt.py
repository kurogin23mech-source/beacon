"""Tests for the ``beacon init`` / first-cloud-push profile prompt (ms-64 e-1633).

When a user has authenticated against multiple Beacon backends, ``beacon init``
and the first ``beacon cloud push`` should each ask once which backend this
project should target. The choice is persisted to ``.beacon/cloud.json.profile``
so all subsequent commands in this cwd flow to that backend automatically.

Skip rules tested here:
  - ``BEACON_PROFILE`` env set       → silent (user already chose)
  - cwd cloud.json already has profile → silent (prior choice persisted)
  - only 1 logged-in profile         → silent (no ambiguity)
  - stdin not a tty                  → silent (don't block hooks/CI)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import commands  # noqa: E402
import profile as profile_mod  # noqa: E402


@pytest.fixture
def beacon_home(tmp_path, monkeypatch):
    home = tmp_path / "beacon-home"
    home.mkdir()
    monkeypatch.setenv("BEACON_HOME", str(home))
    monkeypatch.delenv("BEACON_PROFILE", raising=False)
    monkeypatch.delenv("BEACON_API_URL", raising=False)
    return home


@pytest.fixture
def project_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".beacon").mkdir()
    monkeypatch.chdir(cwd)
    # commands.py uses BEACON_PROJECT_FILE for tests / get_project_file otherwise
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def _login(home: Path, name: str) -> None:
    pdir = home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "credentials.json").write_text('{"id_token": "stub"}')


def _force_tty(monkeypatch, *, value: bool) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: value, raising=False)


# ---------------------------------------------------------------------------
# _maybe_prompt_initial_profile: skip conditions
# ---------------------------------------------------------------------------


class TestMaybePromptInitialProfileSkipConditions:
    def test_skips_when_env_profile_set(self, beacon_home, project_cwd, monkeypatch):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=True)
        monkeypatch.setenv("BEACON_PROFILE", "aws-ga")
        assert commands._maybe_prompt_initial_profile() is None

    def test_skips_when_cloud_json_profile_already_set(self, beacon_home, project_cwd, monkeypatch):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=True)
        (project_cwd / ".beacon" / "cloud.json").write_text(
            json.dumps({"profile": "aws-ga"})
        )
        assert commands._maybe_prompt_initial_profile() is None

    def test_skips_when_single_logged_in_profile(self, beacon_home, project_cwd, monkeypatch):
        _login(beacon_home, "default")
        _force_tty(monkeypatch, value=True)
        assert commands._maybe_prompt_initial_profile() is None

    def test_skips_when_zero_logged_in_profiles(self, beacon_home, project_cwd, monkeypatch):
        _force_tty(monkeypatch, value=True)
        assert commands._maybe_prompt_initial_profile() is None

    def test_skips_when_non_tty(self, beacon_home, project_cwd, monkeypatch):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=False)

        # Even if profile_mod.prompt_choose_profile would be ready to read,
        # _maybe_prompt_initial_profile must return None before calling it.
        def boom(*a, **kw):
            raise AssertionError("prompt_choose_profile must not be called when non-tty")

        monkeypatch.setattr(profile_mod, "prompt_choose_profile", boom)
        assert commands._maybe_prompt_initial_profile() is None


# ---------------------------------------------------------------------------
# _maybe_prompt_initial_profile: actually prompts when conditions are met
# ---------------------------------------------------------------------------


class TestMaybePromptInitialProfileFires:
    def test_prompts_and_returns_choice(self, beacon_home, project_cwd, monkeypatch):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=True)

        captured = {}

        def fake_prompt(candidates, **kwargs):
            captured["candidates"] = candidates
            return "aws-ga"

        monkeypatch.setattr(profile_mod, "prompt_choose_profile", fake_prompt)
        chosen = commands._maybe_prompt_initial_profile()
        assert chosen == "aws-ga"
        assert captured["candidates"] == ["aws-ga", "default"]


# ---------------------------------------------------------------------------
# _persist_initial_profile_choice
# ---------------------------------------------------------------------------


class TestPersistInitialProfileChoice:
    def test_creates_cloud_json_with_profile_field(self, project_cwd):
        commands._persist_initial_profile_choice("aws-ga")
        path = project_cwd / ".beacon" / "cloud.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["profile"] == "aws-ga"

    def test_preserves_existing_fields(self, project_cwd):
        path = project_cwd / ".beacon" / "cloud.json"
        path.write_text(json.dumps({"foo": "bar"}))
        commands._persist_initial_profile_choice("aws-ga")
        data = json.loads(path.read_text())
        assert data["profile"] == "aws-ga"
        assert data["foo"] == "bar"


# ---------------------------------------------------------------------------
# cmd_init integration
# ---------------------------------------------------------------------------


class TestCmdInitIntegration:
    def test_init_without_multi_login_does_not_write_cloud_json(
        self, beacon_home, project_cwd, monkeypatch, capsys
    ):
        _login(beacon_home, "default")  # single profile
        _force_tty(monkeypatch, value=True)
        commands.cmd_init()
        assert (project_cwd / ".beacon" / "project.json").exists()
        assert not (project_cwd / ".beacon" / "cloud.json").exists()

    def test_init_with_multi_login_writes_chosen_profile(
        self, beacon_home, project_cwd, monkeypatch, capsys
    ):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=True)
        monkeypatch.setattr(profile_mod, "prompt_choose_profile", lambda *_a, **_kw: "aws-ga")
        commands.cmd_init()
        cloud_json = project_cwd / ".beacon" / "cloud.json"
        assert cloud_json.exists()
        assert json.loads(cloud_json.read_text()) == {"profile": "aws-ga"}

    def test_init_with_env_profile_writes_nothing_extra(
        self, beacon_home, project_cwd, monkeypatch, capsys
    ):
        _login(beacon_home, "default")
        _login(beacon_home, "aws-ga")
        _force_tty(monkeypatch, value=True)
        monkeypatch.setenv("BEACON_PROFILE", "aws-ga")
        # prompt_choose_profile must not be called when env is set
        monkeypatch.setattr(
            profile_mod,
            "prompt_choose_profile",
            lambda *_a, **_kw: pytest.fail("prompt fired despite BEACON_PROFILE"),
        )
        commands.cmd_init()
        assert not (project_cwd / ".beacon" / "cloud.json").exists()
