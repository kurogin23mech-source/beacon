"""Regression tests for the observing-follow-up safety fixes shipped in PR #691.

Two latent bugs surfaced by an inventory of tasks parked on observing
milestones, plus two residual defects caught by the independent AX +
maintainability review of the fix PR:

  * e-4775 (ms-127): ``commands_shared._resolve_active_api_url`` fell into an
    ``except`` branch that called itself, infinite-recursing (and crashing the
    whole CLI) whenever ``import profile`` failed in a partial install / sandbox.
  * PR #691 AX review: the fallback's api_url precedence must match
    ``profile._resolve_api_url`` (env BEACON_API_URL > cwd cloud.json > default).
    An earlier form let cloud.json override env — an inverted chain.
  * e-5730 (ms-120): ``beacon doc add`` persisted the doc BEFORE resolving the
    recording target, so an ambiguous target (multiple active milestones, no
    ``--ms``) errored AFTER the write, leaving an orphan doc with no target.
  * PR #691 AX/maintainability review: the sibling of e-5730 — an explicit but
    NONEXISTENT ``--ms`` id — also orphan-wrote, because the existence check is
    lenient for milestone ids. Both are now fail-closed (validate before write).

The api_url fixes are unit-checked directly; the doc-add fail-closed behavior is
driven through bin/beacon via subprocess (mirrors the e-3754 test style).
"""

from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BEACON_BIN = REPO / "bin" / "beacon"

# conftest already puts REPO/lib on sys.path.
import commands_shared  # noqa: E402


# ---------------------------------------------------------------------------
# e-4775 + PR #691 precedence: _resolve_active_api_url fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_unimportable(monkeypatch):
    """Force ``import profile`` to raise, driving _resolve_active_api_url into
    its legacy fallback branch (the partial-install condition)."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "profile":
            raise ImportError("simulated partial install")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_fallback_does_not_recurse(profile_unimportable, monkeypatch, tmp_path):
    """e-4775: the fallback must NOT self-recurse; it returns the default url."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_API_URL", raising=False)
    # No RecursionError, and the documented default is returned.
    assert commands_shared._resolve_active_api_url() == "https://beacon-ai.dev"


def test_fallback_env_wins_over_cloud_json(profile_unimportable, monkeypatch, tmp_path):
    """PR #691 AX: precedence must match profile._resolve_api_url — env first."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"api_url": "https://cloud.example", "project_id": "x"})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_API_URL", "https://env.example")
    # env must win; cloud.json must NOT override it (the inverted-chain bug).
    assert commands_shared._resolve_active_api_url() == "https://env.example"


def test_fallback_cloud_json_used_when_env_unset(profile_unimportable, monkeypatch, tmp_path):
    """With no env override, the cwd cloud.json api_url is honored."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"api_url": "https://cloud.example", "project_id": "x"})
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_API_URL", raising=False)
    assert commands_shared._resolve_active_api_url() == "https://cloud.example"


# ---------------------------------------------------------------------------
# e-5730 + sibling: doc add is fail-closed (validate before persist)
# ---------------------------------------------------------------------------

def _run(cwd, *args, check=False):
    env = dict(os.environ)
    env["BEACON_SESSION_KIND"] = "machine"  # allow --priority seeding non-interactively
    return subprocess.run(
        [str(BEACON_BIN), *args], cwd=str(cwd), env=env,
        text=True, capture_output=True, check=check,
    )


def _docs_on_disk(cwd):
    docs_dir = Path(cwd) / ".beacon" / "documents"
    if not docs_dir.exists():
        return []
    return sorted(p.stem for p in docs_dir.glob("*.md"))


@pytest.fixture
def dev_project_two_active(tmp_path):
    """A local dev project with TWO active milestones (ambiguous auto-pick)."""
    subprocess.run(
        [str(BEACON_BIN), "init"], input="e5730\nverify e5730\n5\n1\n",
        cwd=str(tmp_path), text=True, check=True, capture_output=True,
        env={**os.environ, "BEACON_SESSION_KIND": "machine"},
    )
    _run(tmp_path, "milestone", "add", "MS one", "--priority", "medium", check=True)
    _run(tmp_path, "milestone", "add", "MS two", "--priority", "medium", check=True)
    _run(tmp_path, "milestone", "start", "ms-1", check=True)
    _run(tmp_path, "milestone", "start", "ms-2", check=True)
    return tmp_path


def test_doc_add_ambiguous_target_fails_before_write(dev_project_two_active):
    """e-5730: 2 active milestones + no --ms → error BEFORE any doc is written."""
    before = _docs_on_disk(dev_project_two_active)
    r = _run(dev_project_two_active, "doc", "add", "Orphan risk",
             "--scope", "spec", "--content", "body text")
    assert r.returncode != 0
    assert "Multiple active milestones" in (r.stdout + r.stderr)
    # No orphan doc was persisted.
    assert _docs_on_disk(dev_project_two_active) == before


def test_doc_add_explicit_missing_milestone_fails_before_write(dev_project_two_active):
    """Sibling of e-5730: explicit but nonexistent --ms → fail-closed, no orphan."""
    before = _docs_on_disk(dev_project_two_active)
    r = _run(dev_project_two_active, "doc", "add", "Bad target",
             "--scope", "spec", "--ms", "ms-999", "--content", "body")
    assert r.returncode != 0
    assert "Milestone not found: ms-999" in (r.stdout + r.stderr)
    assert _docs_on_disk(dev_project_two_active) == before


def test_doc_add_explicit_valid_milestone_still_writes(dev_project_two_active):
    """Valid explicit --ms must still write (fix is fail-closed, not fail-shut)."""
    r = _run(dev_project_two_active, "doc", "add", "Good target",
             "--scope", "spec", "--ms", "ms-1", "--content", "ok")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "good-target" in _docs_on_disk(dev_project_two_active)
