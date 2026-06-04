"""Unit tests for the release-marker trigger auto-derivation (ms-52 e-952).

Reflects v* tag state into a local release-<version>.json trigger so the
CI-side `beacon trigger fire` (running on an ephemeral runner) doesn't
need to reach the maintainer's local cloud session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


@pytest.fixture
def git_repo_with_beacon(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        beacon_dir = repo / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "triggers").mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "t", "milestones": [], "summary": ""}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

        _git("init", "-q", "-b", "main", cwd=repo)
        _git("config", "user.email", "t@t.t", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        _git("config", "commit.gpgsign", "false", cwd=repo)
        (repo / "README").write_text("seed", encoding="utf-8")
        _git("add", ".", cwd=repo)
        _git("commit", "-q", "-m", "chore: seed", cwd=repo)
        yield repo


def _add_commit(repo: Path, msg: str):
    fname = msg.replace(":", "_").replace(" ", "_").replace("(", "_").replace(")", "_")[:40]
    (repo / fname).write_text("x", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", msg, cwd=repo)


def _read_trigger(repo: Path, name: str) -> dict | None:
    p = repo / ".beacon" / "triggers" / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _run(monkeypatch, repo: Path):
    monkeypatch.chdir(repo)
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    commands._auto_fire_release_marker_trigger()


def test_no_tag_no_marker(git_repo_with_beacon, monkeypatch):
    _run(monkeypatch, git_repo_with_beacon)
    # Trigger dir is empty.
    assert list((git_repo_with_beacon / ".beacon" / "triggers").iterdir()) == []


def test_latest_tag_fires_marker(git_repo_with_beacon, monkeypatch):
    repo = git_repo_with_beacon
    _add_commit(repo, "feat: a")
    _add_commit(repo, "fix: b")
    _git("tag", "v0.2.0", cwd=repo)

    _run(monkeypatch, repo)
    t = _read_trigger(repo, "release-0.2.0")
    assert t is not None
    assert t["kind"] == "release-marker"
    assert t["tag"] == "v0.2.0"
    assert t["source"] == "auto-from-tag"
    # commit_count counts commits up to v0.2.0 (no prev tag) — seed + 2 here.
    assert t["commit_count"] == 3
    assert "beacon v0.2.0 released" in t["message"]
    assert "/discord-post" in t["message"]


def test_idempotent_when_existing(git_repo_with_beacon, monkeypatch):
    repo = git_repo_with_beacon
    _git("tag", "v0.1.0", cwd=repo)
    _run(monkeypatch, repo)
    first = _read_trigger(repo, "release-0.1.0")
    assert first is not None
    first_created = first["created_at"]

    # Run again — should NOT overwrite the existing trigger.
    _run(monkeypatch, repo)
    second = _read_trigger(repo, "release-0.1.0")
    assert second["created_at"] == first_created


def test_only_latest_tag_fires(git_repo_with_beacon, monkeypatch):
    """If multiple v* tags exist but none has a marker, only the LATEST
    fires (older missed tags require explicit catch-up)."""
    repo = git_repo_with_beacon
    _add_commit(repo, "feat: a")
    _git("tag", "v0.1.0", cwd=repo)
    _add_commit(repo, "feat: b")
    _git("tag", "v0.2.0", cwd=repo)

    _run(monkeypatch, repo)
    assert _read_trigger(repo, "release-0.1.0") is None
    t = _read_trigger(repo, "release-0.2.0")
    assert t is not None
    assert t["previous_tag"] == "v0.1.0"
    # commits in v0.1.0..v0.2.0 = 1
    assert t["commit_count"] == 1


def test_marker_persists_after_new_release(git_repo_with_beacon, monkeypatch):
    """Old release-* triggers persist; new tag fires a fresh marker."""
    repo = git_repo_with_beacon
    _git("tag", "v0.1.0", cwd=repo)
    _run(monkeypatch, repo)
    assert _read_trigger(repo, "release-0.1.0") is not None

    _add_commit(repo, "feat: new")
    _git("tag", "v0.2.0", cwd=repo)
    _run(monkeypatch, repo)

    # Both markers now present.
    assert _read_trigger(repo, "release-0.1.0") is not None
    new = _read_trigger(repo, "release-0.2.0")
    assert new is not None
    assert new["previous_tag"] == "v0.1.0"
