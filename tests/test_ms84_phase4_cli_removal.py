"""ms-84 Phase 4 tests (e-2038): CLI removal of push / pull / force-pull.

Pins two SPEC acceptance criteria:

- AC3: ``beacon cloud pull`` (and ``push``, ``force-pull``) must exit
  non-zero with an "unknown subcommand" error.
- AC8: ``git grep`` for ``cloud.*pull``/``force-pull``/``cloud.*push``
  in ``bin/`` and ``lib/cli/`` (where applicable) must find no
  user-facing alias declarations. We verify the structural property by
  asserting that bin/beacon no longer routes ``pull`` / ``force-pull``
  to any handler — they hit the wildcard branch.

We deliberately do NOT scan code comments / docstrings — those still
reference the historical names (ms-84 / Phase 4 audit trail) and that
is correct documentation, not a live alias.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fake_beacon_project(tmp_path):
    """A minimal local-mode Beacon root so `ensure_project` is satisfied
    before the cloud subcommand dispatch runs (= the wildcard branch
    only fires after the per-subcommand match)."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(
        '{"name": "phase4-test", "milestones": []}', encoding="utf-8"
    )
    return tmp_path


def _run_beacon(*args, cwd):
    repo_root = Path(__file__).resolve().parent.parent
    bin_beacon = repo_root / "bin" / "beacon"
    if not bin_beacon.exists():
        pytest.skip("bin/beacon not in this checkout")
    return subprocess.run(
        [str(bin_beacon), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, "BEACON_PROJECT_FILE": ".beacon/project.json"},
    )


def test_cloud_pull_is_unknown_subcommand(fake_beacon_project):
    """SPEC AC3: ``beacon cloud pull`` must fail with unknown subcommand."""
    result = _run_beacon("cloud", "pull", cwd=fake_beacon_project)
    assert result.returncode != 0, (
        f"expected non-zero exit; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "unknown subcommand" in combined or "unknown" in combined, combined


def test_cloud_force_pull_is_unknown_subcommand(fake_beacon_project):
    """SPEC AC3: ``beacon cloud force-pull`` must fail with unknown subcommand."""
    result = _run_beacon("cloud", "force-pull", cwd=fake_beacon_project)
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "unknown" in combined, combined


def test_cloud_push_is_unknown_subcommand(fake_beacon_project):
    """SPEC AC3 + Phase 4 task: even ``beacon cloud push`` (legacy alias)
    must fail with unknown subcommand."""
    result = _run_beacon("cloud", "push", cwd=fake_beacon_project)
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "unknown" in combined, combined


def test_cloud_upload_initial_still_dispatches(fake_beacon_project):
    """Sanity: the one remaining cloud-write subcommand still routes to a
    handler (= it does NOT fall through to the unknown-subcommand branch).
    We don't actually let it talk to a network — auth will refuse first.
    The negative we care about is: the wildcard branch did not swallow
    upload-initial too."""
    result = _run_beacon("cloud", "upload-initial", cwd=fake_beacon_project)
    combined = (result.stdout + result.stderr).lower()
    # The "unknown subcommand" message must NOT appear; auth-not-logged-in
    # / cloud.json-missing / similar messages are acceptable.
    assert "unknown subcommand" not in combined


def test_help_no_longer_lists_push_pull_aliases():
    """SPEC AC8: bin/beacon usage text does not advertise the removed
    aliases as live subcommands."""
    repo_root = Path(__file__).resolve().parent.parent
    bin_beacon = repo_root / "bin" / "beacon"
    if not bin_beacon.exists():
        pytest.skip("bin/beacon not in this checkout")
    text = bin_beacon.read_text(encoding="utf-8")
    # Lines that appear in the usage block (the help text after "Cloud:").
    # We check that the live-alias lines were removed. Allow references in
    # comments / ms-84 audit trail.
    for forbidden in (
        "beacon cloud push                  Legacy alias",
        "beacon cloud pull                  Legacy alias",
        "beacon cloud force-pull            Emergency overwrite local from cloud",
    ):
        assert forbidden not in text, (
            f"bin/beacon still advertises removed alias: {forbidden!r}"
        )


def test_help_json_no_longer_lists_push_pull_aliases():
    """The Python-side help_json table (used by docs / Skills) must not
    advertise push / pull / force-pull either."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import commands  # noqa: E402

    # cmd_help_json prints to stdout; reach into the source to inspect the
    # static command list.
    src = Path(__file__).resolve().parent.parent / "lib" / "commands.py"
    text = src.read_text(encoding="utf-8")
    for forbidden in (
        '{"command": "beacon cloud push"',
        '{"command": "beacon cloud pull"',
        '{"command": "beacon cloud force-pull"',
    ):
        assert forbidden not in text, (
            f"lib/commands.py help_json still lists removed alias: {forbidden!r}"
        )
