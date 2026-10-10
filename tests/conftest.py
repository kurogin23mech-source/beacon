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
import time

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
# is live in this working copy: its poll loop stamps session state and the
# trigger engine drops files as it fires. Each name is listed with why it is not
# the test's doing, following the doctrine the
# sibling guard scripts/check-pid-liveness.py states on its own ALLOWLIST: a false
# positive costs one line here, a false negative goes unnoticed. (An earlier draft
# cited scripts/check-print-before-save.py, which lives in an unmerged PR —
# citing a file the reader cannot open is worse than citing none.)
# Extend it when a real false positive appears —
# do NOT widen it to a prefix match, which would quietly re-admit the leak class
# this guard exists to catch (ms-166 e-6621).
#
# SQLite's companion files are NOT in this set and must not be added to it: they
# are derived from the db's own name by _is_sqlite_sidecar below. That is not a
# prefix match on this set's entries — the db itself stays reportable, which is
# the property the rule above is protecting.
# Deliberately NOT excluded, though an earlier draft listed them:
# session.json / session-state.json / session_notes.jsonl. The reasoning for
# excusing them was "the developer's live bridge writes these" — true here, but
# FALSE in CI, where no bridge runs. And it was never needed: this guard reports
# files a test CREATES, so a bridge updating a file that already exists is
# invisible to it either way. Excluding them bought nothing locally and voided the
# guard exactly where it matters: a full run in a clean checkout left a
# test-created session.json sitting there unreported, which in turn made the
# directory exist and let a later test materialise the store.
# SQLite's companion files accompany a ``*.db``, which is NOT excused: a test
# that materialises the store is a real leak and gets reported by the db's own
# name. Excusing the companions keeps that report to one line instead of three.
#
# **Derived, not enumerated — and that is the whole point of this function.**
# The previous version listed ``project.db-shm`` and ``project.db-wal`` by name
# in ``_NOT_THE_TESTS_FAULT``. Those are the two companions of WAL mode, and the
# list silently assumed WAL is always in effect. It is not:
# ``lib/store_sqlite.py::_ensure_schema`` issues ``PRAGMA journal_mode=WAL`` once
# and **deliberately tolerates it failing** with SQLITE_BUSY under a concurrent
# migration (issuing the transition on every connect is what crashed with
# "database is locked"). While the store stays in rollback-journal mode, a write
# creates ``project.db-journal`` instead — a third name the list did not hold, so
# the guard reported a mid-write transient as a leak and blamed whichever test's
# window straddled it. That is what turned main red on 2026-10-09 (measured: the
# same file name, a different test named on each run).
#
# This is the SECOND time this file lost on the same axis: ``project.json.tmp``
# was once excused by NAME here, and the next atomic writer — different name,
# same mechanism — went straight through it, which is why that rule moved to the
# persistence property in ``_report_persisting``. Keying on the property covers
# every journal mode SQLite has, present and future (``-journal`` / ``-wal`` /
# ``-shm`` / the ``-mj<hex>`` master journal), without enumerating any of them.
#
# The base is still gated: ``project.db`` itself, and any other ``*.db``, is
# reported. A companion cannot exist without its db, so excusing companions
# never hides a database that was leaked.
#
# **The db must actually be there — the name alone is not enough.** The first
# version of this predicate took only the name and excused anything shaped like
# ``<x>.db-<suffix>``. Measured on the independent review of this change: it
# excused ``weird.db-export.json``, ``notes.db-backup`` and ``release.db-old``
# with no ``.db`` present at all, while the docstring claimed "companion files of
# a *.db in the same directory". That is the same disease as the bug this change
# fixes, mirrored: having moved off enumerating names, the property was drawn so
# loosely that it swallowed unrelated files — and a guard that is trusted while
# silently excusing real leftovers is worse than no guard. Requiring the base to
# be present in the SAME directory listing makes the docstring's promise the
# actual rule, and keeps a stray ``<x>.db-<suffix>`` reportable.
def _is_sqlite_sidecar(name: str, present: "set[str]") -> bool:
    """True for SQLite's companion of a ``*.db`` that is itself in ``present``.

    ``present`` is the set of file names in the same directory as ``name``, so a
    companion is only excused alongside the database it belongs to.
    """
    base, sep, suffix = name.rpartition("-")
    return (bool(sep) and bool(suffix)
            and base.endswith(".db") and base in present)


_NOT_THE_TESTS_FAULT = frozenset({
    ".DS_Store",             # Finder writes this whenever it looks at the folder
    "triggers",              # the trigger engine fires into this directory
    "bridges",              # per-bridge registration directory
    "fork-notes-backup",     # fork cleanup's snapshot dir (ms-178 e-6702)
    # NOTE: the transient half of an atomic write ("<name>.tmp", written then
    # renamed) used to be excused HERE, one name at a time — "project.json.tmp"
    # sat in this set. That axis was wrong: it exempted the one transient we had
    # met instead of the property that makes a transient harmless, so the next
    # atomic writer leaked straight through it. It did, measured below in
    # _report_persisting. The rule now lives at report time and is keyed on
    # persistence, which is what the harm model is about.
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
# ms-166 e-6820 で **真因の片方は畳んだ**: lib/session.py が Path.cwd() から組んで
# BEACON_PROJECT_FILE を見ていなかった二重流儀を session._beacon_dir() に寄せた。
# これで以前名指しされていた経路 (review_context / pr_create / api_client /
# surface_snapshot / attainment) は漏らさなくなり、一覧を空にして 2738 件を走らせた
# 時点では漏れ 0 件だった。
#
# **それでも一覧は空にできない。** フル実行ではまだ漏れる。理由は下のキー設計の注記に
# 書いてあるとおり、**このガードは「あるファイル名を最初に作ったテスト」しか報告しない**
# ので、2738 件の部分実行で 0 件でも母集団が空だとは言えないし、1 件直しても次に別の
# テストが名指しされるだけで収束しない。実際に一度空にしたら CI が落ち、収集順で変わる
# テストが名指しされた (2026-10-05)。
#
# 掃討の順序 (e-6833 の残り):
#   1. まずガードを **全ての作成者を報告する** 形に変える (現状は最初の 1 件だけ)。
#   2. フル実行を 1 回して、名前ごとに全作成者の一覧を得る。
#   3. その一覧を潰してから、この一覧を空にする。
# この順でないと「1 件直す → 別の 1 件が名指しされる」を繰り返すだけになる。
KNOWN_LEAKS = frozenset({
    "session.json",   # 残り: lib/session.py の解決は e-6820 で畳んだが、
                      # BEACON_PROJECT_FILE を設定しないテストは既定の
                      # cwd/.beacon に落ちるので、そこを通る経路がまだ在る
    "project.db",     # the local SQLite store, materialised by any project read
                      # discovered from the cwd. Its companion files are excused
                      # by _is_sqlite_sidecar above (derived from the db's name,
                      # so every journal mode is covered) and the report stays
                      # one line.
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
        here = set(files)   # a companion is only excused next to its own db
        for name in files:
            if name in _NOT_THE_TESTS_FAULT or _is_sqlite_sidecar(name, here):
                continue
            out.add(os.path.relpath(os.path.join(root, name), beacon_dir))
    return out


def _report_persisting(beacon_dir: str, created: "set[str]") -> "set[str]":
    """Keep only the names that are STILL on disk — the ones actually left behind.

    The guard's harm model is persistence: "a file left there changes how the
    NEXT run of the suite behaves." A name that was present at the after-snapshot
    but is gone by the time we report was never left behind, so reporting it
    cannot be right — whatever it was, the next run will not see it.

    Two writers produce exactly that shape, and both blamed innocent tests:

    * **Atomic writes inside the suite.** ``<name>.tmp`` then ``os.replace``.
      Serially the rename lands inside the same test and the name is gone by
      teardown; under ``-n auto`` one worker's mid-write falls in another's
      before/after window — 29 innocent tests blamed in one run, measured
      2026-10-01 (lib/store_api.py's cloud cache).
    * **Writers that are not the suite at all.** A developer's own Claude Code
      session has ``bin/beacon-state-hook.py`` wired into five hooks; every turn
      it walks up from the cwd to this repo's ``.beacon/`` and atomically
      rewrites ``session-state.json``. Measured 2026-10-05: the suite under
      ``-n 4`` reported ``session-state.json.tmp`` against
      test_plugin_skill_matches_canonical_source, a test that does not touch
      ``.beacon/`` at all. CI has no interactive session, so this false alarm
      fires ONLY on a developer's machine — which is the worst place for it, the
      one where "run it again" gets learned.

    The previous fix for the first writer named ``project.json.tmp`` in
    ``_NOT_THE_TESTS_FAULT``. That axis was the file name, so the second writer's
    ``.tmp`` — a different name, same mechanism — went straight through it. Keying
    on persistence covers every atomic writer, present and future, without
    enumerating any of them, and it is strictly STRONGER than the name exclusion
    it replaces: a ``project.json.tmp`` that genuinely persists is now reported,
    whereas the name exclusion was permanently blind to it.

    What this deliberately does NOT do: identify the writer. A persisting file
    created by an outside process is still reported against whichever test
    straddled it. Persistence is checkable here; authorship is not.
    """
    return {name for name in created
            if os.path.exists(os.path.join(beacon_dir, name))}


# 掃討のための計器 (ms-166 e-6833 ステップ1)。**ゲートではなく測定。**
#
# ガード本体は「置き去りにされたか」を軸にしているので、ある名前を **最初に作った**
# テストしか報告しない (2 人目以降は before スナップショットに既にその名前が在る)。
# 一覧を空にするには「その名前を作る全員」が要るが、ガードからは 1 人しか見えない。
# だから 1 件直すたびに別の 1 件が名指しされ、収束しない。
#
# そこで **別の軸の計器** を opt-in で足す: 既知の名前について、ファイルの更新時刻が
# そのテストの窓に入っていれば「このテストが書いた」と記録する。更新時刻はガードが
# 意図的に見ていない軸 (開発者自身のセッションが常時書き換えるのでノイズになる) なので、
# 既定では走らせない。フル実行を 1 回するときだけ BEACON_LEAK_CENSUS=1 で立てる。
#
# 使い方:
#   BEACON_LEAK_CENSUS=1 pytest tests/ -p no:randomly
#   → 終了時に tests/_leak_census.json へ {名前: [テスト, ...]} を書き出す
#   → その一覧を潰してから KNOWN_LEAKS を空にする (順序を飛ばすと収束しない)
_CENSUS_ON = os.environ.get("BEACON_LEAK_CENSUS", "") == "1"
_CENSUS: "dict" = {}


def _census_record(beacon_dir: str, names, window_start: float, test_id: str) -> None:
    """既知の名前のうち、この窓の中で書かれたものを記録する。"""
    for name in names:
        path = os.path.join(beacon_dir, name)
        try:
            if os.path.getmtime(path) >= window_start:
                _CENSUS.setdefault(name, []).append(test_id)
        except OSError:
            continue


def _census_dump() -> None:
    """国勢調査の結果を書き出す (opt-in のときだけ)。

    **独立した pytest_sessionfinish として定義しない。** 最初そう書いたが、この
    conftest には既に pytest_sessionfinish が在り (後に定義されている方が勝つ)、
    私の hook は一度も呼ばれなかった。pytest は落ちも警告もしないので、
    「書き出されない」ことに自分で気づくまで分からなかった — 同じファイルに同名の
    hook を足すのは、黙って無効になる形。既存の hook から呼ぶ。
    """
    if not _CENSUS_ON:
        return
    out = os.path.join(_TESTS_DIR, "_leak_census.json")
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({k: sorted(set(v)) for k, v in sorted(_CENSUS.items())},
                      f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"\n[leak-census] {len(_CENSUS)} 名前 / "
              f"{sum(len(set(v)) for v in _CENSUS.values())} 作成者 → {out}")
    except OSError as e:
        print(f"\n[leak-census] 書き出せませんでした: {e}")


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
    _window_start = time.time()
    yield
    if _CENSUS_ON:
        # ガードが見る「新規作成」とは別の軸 (更新時刻) で、既知の名前の全作成者を
        # 数える。ゲートの判定には使わない — あくまで掃討のための測定。
        _census_record(beacon_dir, KNOWN_LEAKS, _window_start,
                       request.node.nodeid)
    created_all = _report_persisting(beacon_dir,
                                     _snapshot_beacon_dir(beacon_dir) - before)
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
    # 国勢調査は部分実行でも終了コードに依らず書き出す (測定なので、落ちた実行の
    # 情報も要る)。下の「もう起きない名前」の報告はフル実行かつ成功時だけ。
    _census_dump()
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
