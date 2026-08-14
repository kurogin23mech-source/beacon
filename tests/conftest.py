"""Shared pytest configuration for the Beacon test suite.

ms-119 / e-4008: the attainment gate (`beacon milestone done` / `operation
close` / `target approve`) refuses direct completion / self-approval from an
*AI session*, treating an unset ``BEACON_SESSION_KIND`` as AI for safety (the
same convention as the PR merge ban). The pytest suite, however, is a
deterministic developer-run driver — its fixtures create and complete targets
as setup, not as an autonomous agent asserting an owned verdict. So the harness
declares itself human here, which is accurate and keeps setup flows unguarded.

Tests that specifically exercise the ban (test_attainment_gate_ban.py) override
this per-test via explicit env swaps, so the global default never masks them.
"""

import json
import os
import sys

import pytest

# ms-142 e-5144: centralize the ``sys.path`` boilerplate every test module used to
# repeat (``sys.path.insert(0, .../lib)`` + scripts/ + tests/). pytest AUTO-IMPORTS
# this conftest before any test module under tests/, so putting the project's own
# source roots on the path here lets a test do ``import capability_ledger`` (lib),
# import a hyphen-free scripts module, or import a sibling test helper
# (``capability_profession_matrix``) without its own insert. THIS is the one place
# test sys.path setup lives — a test file under tests/ must NOT add its own insert
# (a file under a future sub-package outside this conftest's scope would need its own).
# Paths are absolutized so the ``not in sys.path`` guard is truly idempotent even
# against an absolute-path entry a not-yet-migrated module inserted (additive; a
# module that still carries its own insert is unaffected).
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _sub in ("lib", "scripts"):
    _p = os.path.abspath(os.path.join(_TESTS_DIR, "..", _sub))
    if _p not in sys.path:
        sys.path.insert(0, _p)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

# Declare the test harness human-driven. os.environ mutation here also
# propagates to subprocess children (tests that invoke the `beacon` CLI), so
# both in-process command calls and subprocess runs inherit it.
os.environ.setdefault("BEACON_SESSION_KIND", "human")

# ms-123 / e-4029: mark the whole suite as a test context so the cloud write
# guard (lib/cloud_write_guard.py) refuses to create projects on the
# production cloud. This is what closes the leak that left 48 phase4-test
# residue projects behind. setdefault (not a hard set) so a test that
# deliberately clears it can. Propagates to `beacon` CLI subprocess children.
os.environ.setdefault("BEACON_TEST_MODE", "1")


@pytest.fixture(autouse=True)
def _isolate_bus_sent_log(tmp_path, monkeypatch):
    """ms-141 / e-4965: point the bus recent-send guard's log at a per-test tmp
    file so `beacon bus send` (dm) in any test never writes the real repo
    .beacon/ and never cross-contaminates other tests via a shared fingerprint
    log. Tests that specifically exercise the guard set their own contents."""
    monkeypatch.setenv(
        "BEACON_BUS_SENT_LOG_PATH", str(tmp_path / "bus-sent-log.json"))


@pytest.fixture
def fake_cloud_config(tmp_path, monkeypatch):
    """Canonical, namespace-free way to fake cloud mode in a unit test (ms-108 e-5217).

    Writes a ``cloud.json`` next to a tmp project file and points
    ``BEACON_PROJECT_FILE`` at that project file. Because ``_get_cloud_config_path``
    derives the cloud path from ``get_project_file()`` (which reads
    ``BEACON_PROJECT_FILE``), EVERY module that calls ``_get_cloud_config_path``
    resolves to THIS ``cloud.json`` with **no monkeypatch** — so a test no longer
    needs to know which module's globals hold the from-imported symbol and patch
    each one (the op-1 leak's真因: patching ``commands._get_cloud_config_path`` alone
    silently missed ``cmd_trigger``'s copy).

    Returns the ``.beacon`` dir (a ``pathlib.Path``); overwrite ``cloud.json`` there
    to customise ``api_url`` / ``project_id`` before the code under test reads it."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True, exist_ok=True)
    (beacon_dir / "cloud.json").write_text(
        json.dumps({"api_url": "https://api.test", "project_id": "proj-1"}))
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    return beacon_dir

