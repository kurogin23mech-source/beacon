"""cloud upload-initial / migrate-from-local Windows parity (ms-133 e-4649).

These two install/cloud verbs were bash-only, so a Windows/pipx user could not
do the one-shot local→cloud migration (`upload-initial`) or retire an orphan
local project.json after the cloud cut-over (`migrate-from-local`). This pins
that the Python dispatcher now parses them AND routes to the same commands.py
engine + BEACON_* env the bash dispatcher used, and that the two-factor confirm
on migrate-from-local is enforced.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

dispatch = importlib.import_module("beacon_cli.dispatch")


@pytest.fixture
def route(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(dispatch, "_run_commands_py",
                        lambda root, sub, env, **k: calls.append((sub, dict(env))) or 0)
    monkeypatch.setattr(dispatch, "_ensure_project", lambda: None)

    def _run(argv):
        parser = dispatch.build_parser()
        args = parser.parse_args(argv)
        rc = dispatch._handle_cloud(ROOT, args)
        return rc, calls

    return _run


def test_upload_initial_routes_to_cloud_push(route):
    rc, calls = route(["cloud", "upload-initial"])
    assert rc == 0
    sub, env = calls[-1]
    assert sub == "cloud_push"
    assert env["BEACON_FORCE"] == ""


def test_upload_initial_force_flag(route):
    rc, calls = route(["cloud", "upload-initial", "--force"])
    sub, env = calls[-1]
    assert sub == "cloud_push"
    assert env["BEACON_FORCE"] == "1"


def test_migrate_from_local_routes_with_confirm(route):
    rc, calls = route(["cloud", "migrate-from-local", "--confirm", "proj-9"])
    assert rc == 0
    sub, env = calls[-1]
    assert sub == "cloud_migrate_from_local"
    assert env["BEACON_CONFIRM"] == "proj-9"
    assert env["BEACON_FORCE"] == ""


def test_migrate_from_local_force_after_review_alias(route):
    rc, calls = route(["cloud", "migrate-from-local", "--confirm", "p",
                       "--force-after-review"])
    sub, env = calls[-1]
    assert env["BEACON_FORCE"] == "1"


def test_migrate_from_local_requires_confirm(route):
    """Two-factor guard: without --confirm it must refuse and NOT hit the engine
    (mirrors the bash silent-invocation guard)."""
    rc, calls = route(["cloud", "migrate-from-local"])
    assert rc == 1
    assert calls == []


def test_checker_green_after_cloud_backfill():
    """The two cloud verbs left ALLOW_SUBVERB_MISSING_FROM_PYTHON (e-4649), so
    the checker now actively guards their parity and stays green."""
    spec = importlib.util.spec_from_file_location(
        "_chk_e4649", ROOT / "scripts" / "check-cli-help-drift.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "cloud upload-initial" not in mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON
    assert "cloud migrate-from-local" not in mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON
    assert mod.collect_subverb_drift()["ok"]
