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
# name is listed with why it is not the test's doing, following the doctrine the
# sibling guard scripts/check-pid-liveness.py states on its own ALLOWLIST: a false
# positive costs one line here, a false negative goes unnoticed. (An earlier draft
# cited scripts/check-print-before-save.py, which lives in an unmerged PR —
# citing a file the reader cannot open is worse than citing none.)
# Extend it when a real false positive appears —
# do NOT widen it to a prefix match, which would quietly re-admit the leak class
# this guard exists to catch (ms-166 e-6621).
# Deliberately NOT excluded, though an earlier draft listed them:
# session.json / session-state.json / session_notes.jsonl. The reasoning for
# excusing them was "the developer's live bridge writes these" — true here, but
# FALSE in CI, where no bridge runs. And it was never needed: this guard reports
# files a test CREATES, so a bridge updating a file that already exists is
# invisible to it either way. Excluding them bought nothing locally and voided the
# guard exactly where it matters: a full run in a clean checkout left a
# test-created session.json sitting there unreported, which in turn made the
# directory exist and let a later test materialise the store.
_NOT_THE_TESTS_FAULT = frozenset({
    # SQLite's sidecars accompany project.db, which is NOT excluded: a test that
    # materialises the store is a real leak and gets reported by name. Listing the
    # two sidecars keeps that report to one line instead of three. (They are not
    # present in this repo today — a read only materialises the store when the
    # project is discovered from the cwd, not when BEACON_PROJECT_FILE points at
    # it, which is itself the two-conventions split tracked as e-6820.)
    "project.db-shm",
    "project.db-wal",
    ".DS_Store",             # Finder writes this whenever it looks at the folder
    "triggers",              # the trigger engine fires into this directory
    "bridges",              # per-bridge registration directory
    "fork-notes-backup",     # fork cleanup's snapshot dir (ms-178 e-6702)
    # Transient half of the cloud cache's atomic write (lib/store_api.py writes
    # "<cache>.tmp" then renames). A test that calls load_project() without its
    # own project file fetches from the cloud and refreshes this cache, which is
    # a legitimate read path, not state the test invented. Serially the rename
    # completes inside the same test and the name is gone again by teardown; it
    # only becomes visible under -n auto, where one worker's mid-write lands in
    # another's before/after window — 29 innocent tests blamed in one run,
    # measured 2026-10-01 (see e-6816).
    "project.json.tmp",
})

# NOTE on what this guard does NOT see: it reports files a test CREATES, so a
# test that rewrites an existing file's CONTENT is invisible to it. That is a
# deliberate limit, not an oversight — the repo's .beacon/ holds live state the
# developer's own bridge rewrites constantly, so content comparison would be all
# noise. Catching "a test rewrote real project state" needs a different
# instrument than this one.


# Leaks that already existed when this guard went in. Recorded debt, not a
# verdict: each name is a file some test creates in the repository's own
# .beacon/, and burning the list down is follow-up work (e-6833) rather than a
# precondition for stopping the bleeding — the same split the sibling guards use
# (scripts/check-cli-env-parity.py's KNOWN_GAPS, scripts/check-pid-liveness.py's
# ALLOWLIST).
#
# Keyed on the FILE, not on (test, file), and that is deliberate. The guard
# reports only the first test to create a given file, so which test gets named
# changes with collection order: five runs of the same suite blamed five
# different tests (test_review_context_kernel → test_surface_snapshot →
# test_pr_create_infer → test_api_client → test_attainment_backlog_disposition).
# An allowlist keyed on the test would therefore be flaky by construction.
#
# What this still gates: any file NAME not listed here fails immediately, so new
# code cannot introduce a new KIND of leak. What it does not gate: another test
# leaking one of these two names. That is the debt, and it is why the list must
# shrink to empty rather than grow.
KNOWN_LEAKS = frozenset({
    "session.json",   # in-process calls that stamp session state (lib/session.py,
                      # which resolves via Path.cwd() and so ignores
                      # BEACON_PROJECT_FILE — see e-6820)
    "project.db",     # the local SQLite store, materialised by any project read
                      # discovered from the cwd (+ its -shm/-wal sidecars, which
                      # are excluded above so the report stays one line)
})

# Which KNOWN_LEAKS names were actually observed this run, so a fixed one does
# not sit in the list exempting the next regression at the same name.
_OBSERVED_LEAKS: "set[str]" = set()


def _snapshot_beacon_dir(beacon_dir: str) -> "set[str]":
    """Relative paths of files under ``beacon_dir``, excluded entries dropped.

    Two properties this must have, both from the independent review of the first
    version of this guard (PR #780):

    * **An absent directory is an empty snapshot, not "skip the check."** The
      first version returned None when ``.beacon/`` did not exist and then
      skipped silently — which disabled the guard in exactly the environment the
      leak hid in. CI checks out a tree with no ``.beacon/`` at all (it is
      gitignored), so a test that CREATED the directory and wrote into it passed
      with no complaint. Reproduced in a bare worktree before fixing.
    * **It recurses.** ``os.listdir`` saw only the top level, and the repo's
      ``.beacon/`` already holds bridges/ codex/ documents/ fork-notes-backup/
      retro/ session_logs/ triggers/ — those names sit in both snapshots, so a
      new file INSIDE any of them was invisible. The guard claimed to catch "the
      NEXT one" while being blind to seven existing subtrees.

    Directories named in ``_NOT_THE_TESTS_FAULT`` are pruned rather than walked:
    their contents are excluded anyway and they are the ones that grow. A full
    walk measured 0.55 ms over 431 files — about 1% of the suite at two snapshots
    per test, which is worth paying; pruning keeps it from tracking the trigger
    and bridge directories as they fill up.
    """
    out: "set[str]" = set()
    for root, dirs, files in os.walk(beacon_dir):
        dirs[:] = [d for d in dirs if d not in _NOT_THE_TESTS_FAULT]
        for name in files:
            if name in _NOT_THE_TESTS_FAULT:
                continue
            out.add(os.path.relpath(os.path.join(root, name), beacon_dir))
    return out


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
    before = _snapshot_beacon_dir(beacon_dir)
    yield
    created_all = _snapshot_beacon_dir(beacon_dir) - before
    _OBSERVED_LEAKS.update(created_all & KNOWN_LEAKS)
    created = sorted(created_all - KNOWN_LEAKS)
    if created:
        raise AssertionError(
            "this test created {0} in the repository's own .beacon/ — it must "
            "write under tmp_path instead. A file left there changes how the "
            "NEXT run of the suite behaves, and the failure then looks like "
            "someone else's regression (ms-166 e-6621).\n"
            "Fix it in the module that writes, one of two ways:\n"
            "  * point BEACON_PROJECT_FILE at tmp in an autouse fixture — one "
            "redirect moves every side-file the bus family resolves (worked "
            "example: tests/test_trek_cli_cloud.py::_isolate_project_root);\n"
            "  * or set the specific override, e.g. BEACON_BUS_BUDGET_PATH or "
            "BEACON_BUS_SENT_LOG_PATH.\n"
            "If the real directory genuinely is this test's subject, mark it "
            "@pytest.mark.allow_repo_beacon_write.".format(", ".join(created)))


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """A tmp project that BOTH resolution conventions will find. Returns its dir.

    Isolating a test from the developer's repo takes two moves, not one, because
    this codebase answers "where is .beacon/" in two ways (ms-166 e-6820):

      * ``get_project_file()`` reads ``BEACON_PROJECT_FILE`` — the store, the bus
        budget, the send log, documents;
      * ``lib/session.py`` builds paths from ``Path.cwd()`` and never looks at that
        env var — session.json, cloud.json, the bridge claim, bridges/.

    Setting only the env var leaves the second family writing the real repo. That
    is how ``test_pr_create_infer`` came to leave a session.json behind in a clean
    checkout, which then made ``.beacon/`` exist and let a later test materialise
    the SQLite store (found by the guard below on 2026-10-01).

    Use this rather than re-deriving the recipe per module: when e-6820 collapses
    the two conventions into one, this fixture is the single place that changes.
    """
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True, exist_ok=True)
    (beacon_dir / "project.json").write_text(
        json.dumps({"name": "isolated", "milestones": []}), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    return beacon_dir


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


def pytest_sessionfinish(session, exitstatus):
    """Report KNOWN_LEAKS entries that never happened — i.e. already fixed.

    A fixed entry left in the list exempts the next regression at the same name,
    which is the failure mode #777 and #781 both guard against on their own
    allowlists. Reported only when the whole suite ran: under ``-k`` / ``-m`` /
    ``--deselect`` an unobserved name means "not exercised", not "fixed", and
    crying stale on a subset run would teach people to ignore the message.
    """
    opt = session.config.option
    filtered = bool(getattr(opt, "keyword", "") or getattr(opt, "markexpr", "")
                    or getattr(opt, "deselect", None) or getattr(opt, "file_or_dir", None) != ["tests/"])
    if filtered or exitstatus != 0:
        return
    fixed = sorted(KNOWN_LEAKS - _OBSERVED_LEAKS)
    if fixed:
        print("\n[repo-beacon-leaks] KNOWN_LEAKS entries that no longer happen: "
              + ", ".join(fixed)
              + "\n  -> delete them from tests/conftest.py; a fixed entry left in "
                "the list exempts the next regression at the same name (e-6833).")
