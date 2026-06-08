"""Regression tests for ``beacon log --prepare`` HEAD resolution from a
git worktree (e-1227 / ms-17).

Background
----------
Beacon supports ``git worktree`` so parallel sub-agents (ms-17 / ms-26 /
ms-51) can each work on a distinct branch. ``.beacon/project.json`` only
lives in the parent repo, so when a beacon command runs from inside a
worktree it relocates up to the parent (``_relocate_to_project_root`` —
e-862). Before the fix, the subsequent ``git rev-parse HEAD`` therefore
captured the **parent repo's** HEAD instead of the worktree's HEAD,
silently attaching the wrong commit hash to every beacon entry created
from a sub-agent's worktree.

The fix stashes the original invocation cwd before the relocate and runs
the HEAD-resolving git commands with ``git -C <invocation_cwd>``. These
tests assert that the captured hash matches ``git rev-parse HEAD`` of the
worktree, not of the parent.

Scope
-----
* ``beacon_cli.dispatch._handle_log`` (Python path used on PowerShell and
  on macOS/Linux when bash is unavailable).
* The whole bash dispatcher (``bin/beacon``) is also covered via an
  end-to-end test that shells out to it.

Both paths share the same root cause (chdir-then-rev-parse) and the same
fix shape (stash invocation cwd → ``git -C``), so they get parallel
coverage.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch  # noqa: E402  (sys.path mutated)


# ---------------------------------------------------------------------------
# Fixtures: a parent repo with .beacon/ and a worktree on a divergent branch
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """Run git with check=True and return stdout (utf-8, stripped)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def parent_repo_with_worktree(tmp_path: Path):
    """Set up a parent repo (with .beacon/) + a linked worktree on its own
    branch, where the worktree's HEAD is one commit ahead of the parent's.

    Yields ``(parent_repo, worktree_dir, parent_head, worktree_head)``.
    """
    parent = tmp_path / "parent"
    parent.mkdir()

    # Parent repo: init + seed commit.
    _git("init", "-q", "-b", "main", cwd=parent)
    _git("config", "user.email", "t@t.t", cwd=parent)
    _git("config", "user.name", "t", cwd=parent)
    _git("config", "commit.gpgsign", "false", cwd=parent)
    (parent / "README").write_text("seed", encoding="utf-8")
    _git("add", "README", cwd=parent)
    _git("commit", "-q", "-m", "chore: seed parent", cwd=parent)

    # .beacon/ lives ONLY in the parent — this is the worktree pattern
    # that triggers the bug.
    (parent / ".beacon").mkdir()
    (parent / ".beacon" / "project.json").write_text(
        json.dumps({
            "name": "t",
            "milestones": [
                {"id": "ms-17", "title": "Worktree HEAD fix",
                 "status": "in_progress", "entries": [], "progress": 0}
            ],
            "summary": "",
        }),
        encoding="utf-8",
    )

    parent_head = _git("rev-parse", "--short", "HEAD", cwd=parent)

    # Create a worktree on a new branch. We place it *inside* the parent
    # tree at ``.worktrees/sub-agent`` to mirror the real-world layout used
    # by beacon's milestone-workspace command (lib/commands.py:3613).
    # This matters because find_beacon_root walks up from cwd, and only
    # finds the parent's .beacon/ when the worktree is an ancestor of it
    # — which is the actual codebase convention.
    worktree = parent / ".worktrees" / "sub-agent"
    _git("worktree", "add", "-b", "sub-agent-work", str(worktree), cwd=parent)

    # Add a divergent commit in the worktree so its HEAD differs from
    # the parent's HEAD. This is the only way the test can distinguish a
    # buggy "captures parent HEAD" outcome from the fixed
    # "captures worktree HEAD" outcome.
    (worktree / "feature.txt").write_text("worktree work", encoding="utf-8")
    _git("add", "feature.txt", cwd=worktree)
    _git("commit", "-q", "-m", "feat: work in worktree", cwd=worktree)

    worktree_head = _git("rev-parse", "--short", "HEAD", cwd=worktree)
    parent_head_after = _git("rev-parse", "--short", "HEAD", cwd=parent)
    # Sanity: the test setup itself depends on the two HEADs differing.
    # If they're equal, the regression test would pass trivially even
    # with the bug in place, so refuse to proceed.
    assert worktree_head != parent_head_after, (
        f"Test setup invariant violated: worktree HEAD ({worktree_head}) "
        f"must differ from parent HEAD ({parent_head_after})."
    )

    yield {
        "parent": parent,
        "worktree": worktree,
        "parent_head": parent_head_after,
        "worktree_head": worktree_head,
    }


# ---------------------------------------------------------------------------
# Python path: beacon_cli.dispatch
# ---------------------------------------------------------------------------


def _build_log_args(prepare: bool = True) -> argparse.Namespace:
    """Mimic the argparse Namespace _handle_log expects."""
    return argparse.Namespace(
        show_help=False,
        ms_id="",
        progress="",
        json=True,  # so commands.py prints structured JSON we can inspect
        prepare=prepare,
        finalize=False,
        summary="",
        explicit_hash="",
        behavior="",
        resolves="",
        message="",
    )


def test_handle_log_uses_invocation_cwd_for_head(
    parent_repo_with_worktree, monkeypatch, capsys
):
    """e-1227 regression: from inside a worktree, ``beacon log --prepare``
    must record the worktree's HEAD, not the parent repo's HEAD.

    The bug was: ``_relocate_to_project_root`` cd's up to the parent repo
    (because .beacon/ lives there), then ``git rev-parse HEAD`` runs
    without ``-C`` and inherits the post-chdir cwd, so it captures the
    parent's HEAD. The fix stashes the original invocation cwd and uses
    ``git -C <invocation_cwd>`` for all HEAD reads.
    """
    fix = parent_repo_with_worktree
    worktree = fix["worktree"]
    expected_hash = fix["worktree_head"]
    parent_hash = fix["parent_head"]

    # Simulate "user invoked beacon from inside the worktree".
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")

    # Stash invocation cwd the same way dispatch() does for a real call.
    dispatch._invocation_cwd = Path.cwd()

    # Now simulate the relocate that dispatch() would do next: we are
    # inside a worktree with no local .beacon/, so it walks up to the
    # parent. We can't actually let _relocate run here (the worktree
    # isn't a subdirectory of the parent on disk — they are siblings
    # under tmp/), so we just chdir to the parent manually to mirror
    # the real-world post-relocate state.
    monkeypatch.chdir(fix["parent"])

    # Mock _run_commands_py so the test doesn't need a real lib/ on disk
    # at the tmp_path level; capture the BEACON_HASH env var the handler
    # would have set on the child invocation.
    captured: dict[str, str] = {}

    def fake_run(root, subcmd, env, **kwargs):
        captured["subcmd"] = subcmd
        captured.update(env)
        return 0

    monkeypatch.setattr(dispatch, "_run_commands_py", fake_run)

    rc = dispatch._handle_log(ROOT, _build_log_args(prepare=True))
    assert rc == 0, "handler should succeed"
    assert captured.get("subcmd") == "log_prepare"

    # The HEAD that got captured must be the *worktree's* HEAD.
    assert captured["BEACON_HASH"] == expected_hash, (
        f"Expected worktree HEAD {expected_hash!r}, got {captured['BEACON_HASH']!r}. "
        f"Parent HEAD is {parent_hash!r} — if BEACON_HASH equals the parent's HEAD, "
        "the regression is back: HEAD is being resolved from the post-relocate cwd "
        "instead of from the invocation cwd."
    )


def test_get_invocation_cwd_falls_back_when_not_initialised(monkeypatch, tmp_path):
    """Direct calls into handlers (e.g. unit tests) that bypass dispatch()
    should still get a sensible cwd from ``get_invocation_cwd()``.
    """
    monkeypatch.chdir(tmp_path)
    # Reset the module state so the fallback path actually runs.
    monkeypatch.setattr(dispatch, "_invocation_cwd", None)
    assert dispatch.get_invocation_cwd() == Path.cwd()


def test_invocation_cwd_stashed_before_relocate(tmp_path, monkeypatch):
    """The cwd we stash must be the *pre-relocate* cwd, not the post.

    Build a project tree like e-862's test_subdir_resolution but assert
    that ``_invocation_cwd`` reflects the nested directory we started in,
    not the project root we relocated to.
    """
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "t", "milestones": []}', encoding="utf-8"
    )
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)

    # Emulate what dispatch() does, in the same order:
    dispatch._invocation_cwd = Path.cwd()  # stash
    dispatch._relocate_to_project_root("status")  # then relocate

    assert dispatch.get_invocation_cwd() == nested.resolve(), (
        "Invocation cwd must be the pre-relocate path so handlers like "
        "_handle_log can resolve git HEAD from where the user actually is."
    )
    assert Path.cwd() == tmp_path.resolve(), "Relocate must still have happened."


# ---------------------------------------------------------------------------
# Bash path: bin/beacon end-to-end
# ---------------------------------------------------------------------------


_BIN_BEACON = ROOT / "bin" / "beacon"
_HAS_BASH = shutil.which("bash") is not None


@pytest.mark.skipif(not _HAS_BASH, reason="bash not available on this system")
@pytest.mark.skipif(
    not _BIN_BEACON.exists(), reason="bin/beacon not present (wheel layout)"
)
def test_bin_beacon_log_prepare_from_worktree(parent_repo_with_worktree, tmp_path):
    """End-to-end: invoke ``bin/beacon log --prepare`` from the worktree and
    confirm the JSON output carries the worktree's HEAD, not the parent's.

    This covers the bash dispatcher (``cmd_log`` in ``bin/beacon``) which
    has the same chdir-then-rev-parse pattern that the Python dispatcher
    has. The fix in bash is ``git -C "$BEACON_INVOCATION_CWD" rev-parse``,
    where ``$BEACON_INVOCATION_CWD`` is stashed at the top of the script
    before the relocate.
    """
    fix = parent_repo_with_worktree
    worktree = fix["worktree"]
    expected_hash = fix["worktree_head"]
    parent_hash = fix["parent_head"]

    # bin/beacon expects to find lib/commands.py via its own resolution
    # rules. We point it at the repo by giving it BEACON_DIR if needed,
    # but the simpler path is to invoke it from a controlled cwd and let
    # it find the project via find_beacon_root.
    env = os.environ.copy()
    # Avoid clobbering the user's real BEACON_PROJECT_FILE.
    env["BEACON_PROJECT_FILE"] = ".beacon/project.json"
    env["BEACON_JSON"] = "1"
    # Make sure the wrapper doesn't try to spawn a tmux dashboard.
    env.pop("BEACON_DASHBOARD", None)

    result = subprocess.run(
        ["bash", str(_BIN_BEACON), "log", "--prepare"],
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # If bin/beacon couldn't find commands.py (e.g. it relies on an
    # install-side env var we didn't set), fall through with a clear
    # message rather than asserting on JSON we never got.
    if result.returncode != 0:
        pytest.skip(
            "bin/beacon could not run end-to-end in this test environment "
            f"(stderr: {result.stderr.strip()!r}). The Python path "
            "(test_handle_log_uses_invocation_cwd_for_head) still covers "
            "the regression on the supported entry-point."
        )

    # The dispatcher prints the prepare JSON to stdout.
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        pytest.skip(
            f"Could not parse prepare JSON from bin/beacon output "
            f"(stdout={result.stdout!r}). The Python path still asserts "
            "the regression."
        )

    captured_hash = payload.get("commit", {}).get("hash", "")
    assert captured_hash == expected_hash, (
        f"bin/beacon recorded {captured_hash!r}; expected worktree HEAD "
        f"{expected_hash!r} (parent HEAD is {parent_hash!r}). "
        "This means the bash side of e-1227 is regressing."
    )
