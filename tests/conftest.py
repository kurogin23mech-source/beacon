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


def pytest_configure(config):
    """Register the suite's own markers so they are not silently mistyped.

    An unregistered marker only warns, and a misspelled one
    (``allow_repo_beacon_writes``) then does nothing while reading as if it
    opted out — the kind of silent no-op this suite's guards exist to catch
    (ms-166 e-6621). With ``--strict-markers`` a typo becomes an error; without
    it, registering at least makes the intended spelling discoverable via
    ``pytest --markers``."""
    config.addinivalue_line(
        "markers",
        "allow_repo_beacon_write: this test's subject is the repository's own "
        ".beacon/ directory, so the leak guard is waived for it")


@pytest.fixture(autouse=True)
def _isolate_bus_sent_log(tmp_path, monkeypatch):
    """ms-141 / e-4965: point the bus recent-send guard's log at a per-test tmp
    file so `beacon bus send` (dm) in any test never writes the real repo
    .beacon/ and never cross-contaminates other tests via a shared fingerprint
    log. Tests that specifically exercise the guard set their own contents."""
    monkeypatch.setenv(
        "BEACON_BUS_SENT_LOG_PATH", str(tmp_path / "bus-sent-log.json"))


# Entries in the repo's .beacon/ that appear on their own while the suite runs,
# so seeing one is not evidence that a test wrote it. The developer's own bridge
# is live in this working copy: its poll loop stamps session state, the trigger
# engine drops files as it fires, and SQLite creates its sidecars on open. Each
# name is listed with why it is not the test's doing, following the doctrine in
# scripts/check-print-before-save.py: a false positive costs one line here, a
# false negative goes unnoticed. Extend it when a real false positive appears —
# do NOT widen it to a prefix match, which would quietly re-admit the leak class
# this guard exists to catch (ms-166 e-6621).
_NOT_THE_TESTS_FAULT = frozenset({
    "session.json",          # bridge poll loop writes its proof-of-life here
    "session-state.json",    # the Claude Code state hook stamps this per event
    "session_notes.jsonl",   # `beacon note` from the session running the suite
    "project.db-shm",        # SQLite shared-memory sidecar, created on open
    "project.db-wal",        # SQLite write-ahead log, created on open
    "triggers",              # the trigger engine fires into this directory
    "bridges",              # per-bridge registration directory
    "fork-notes-backup",     # fork cleanup's snapshot dir (ms-178 e-6702)
})


@pytest.fixture(autouse=True)
def _fail_on_repo_beacon_write(request):
    """Fail the test that leaves a new file in the real repo ``.beacon/``.

    ms-166 / e-6621 AC 4 — the guard has to be observable, not just believed.
    The two fixtures above redirect the files we know about; this one catches the
    NEXT one. Without it a new side-file would leak exactly as the budget did,
    and the cost would again land on whoever runs the suite next rather than on
    the test that caused it.

    Attribution is the whole value: the failure names the test and the file, so
    the person reading it does not have to bisect. Pre-existing files are
    ignored (only files the test CREATES are reported) — the repo legitimately
    holds project.json / cloud.json / session.json, and a developer's own state
    is not the test's fault.

    Under ``-n auto`` attribution is approximate: workers run concurrently, so a
    file one test creates falls inside another's before/after window and both are
    reported. That is the right way to be wrong here — the message names the file,
    which is what locates the真 source, and over-reporting a real leak costs a
    minute while missing one costs the next person's debugging session. Do not
    "fix" it by disabling the guard under xdist.

    Opt out with ``@pytest.mark.allow_repo_beacon_write`` when a test's subject
    genuinely is the real project directory."""
    # ``BEACON_TEST_REPO_BEACON_DIR`` points the guard at a different directory.
    # Only the guard's own probe uses it (tests/test_bus_budget_isolation_e6621.py):
    # verifying that the guard fires used to mean writing the real .beacon/, and
    # under ``-n auto`` that file then appeared inside OTHER workers' before/after
    # windows and got attributed to whichever test happened to straddle it — 4
    # innocent tests blamed per run, measured 2026-10-01. The probe now exercises
    # the guard against a tmp directory, so verifying it costs nothing real.
    beacon_dir = os.environ.get("BEACON_TEST_REPO_BEACON_DIR", "").strip() or \
        os.path.join(os.path.dirname(_TESTS_DIR), ".beacon")
    if request.node.get_closest_marker("allow_repo_beacon_write"):
        yield
        return
    try:
        before = set(os.listdir(beacon_dir))
    except OSError:
        before = None
    yield
    if before is None:
        return
    try:
        after = set(os.listdir(beacon_dir))
    except OSError:
        return
    created = sorted((after - before) - _NOT_THE_TESTS_FAULT)
    if created:
        raise AssertionError(
            "this test created {0} in the repository's own .beacon/ — it must "
            "write under tmp_path instead. A file left there changes how the "
            "NEXT run of the suite behaves, and the failure then looks like "
            "someone else's regression (ms-166 e-6621). Point the resolver at a "
            "tmp path (see _isolate_bus_budget / _isolate_bus_sent_log), or mark "
            "the test @pytest.mark.allow_repo_beacon_write if the real "
            "directory genuinely is the subject.".format(", ".join(created)))


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

