"""ms-184 — direct tests for lib/cmd_recap.py (the verbs' own behaviour).

Why this file exists
====================
An independent maintainability review of PR#803 observed that no test imported
this module: the only test touching it inspected its SOURCE TEXT (AST + banned
substrings). The two libraries it wraps were well covered, which made the
feature look tested end-to-end while the module's own branching — period
resolution, local-vs-cloud, the server-sweep-then-fallback, JSON assembly,
``--limit`` truncation, the error exits — had no asserted outcome anywhere.

That gap is not hypothetical: the fallback-reason bug it hid (a module-level
list, appended to and never cleared, read at index ``[0]`` so a second call in
one process reported the FIRST call's reason) was found by reading, not by a
test. These tests cover the behaviour that bug lived in, so the next one has
somewhere to fail.

The handlers read their input from ``BEACON_*`` env vars (the shape both CLI
front-ends set) and print to stdout, so each test sets env, captures stdout,
and asserts on the parsed JSON — driving the real handler, not a re-implementation.
"""

from __future__ import annotations

import json
import os

import pytest

import cmd_recap
import date_window as dw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENV_KEYS = ("BEACON_SINCE", "BEACON_FROM", "BEACON_TO", "BEACON_LIMIT",
             "BEACON_EVIDENCE", "BEACON_JSON", "BEACON_RECAP_PROJECT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from no period / no flags set."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(scope="module", autouse=True)
def _no_env_leak():
    """Fail if this MODULE leaves any BEACON_* env var behind.

    The leak this catches broke five tests in an unrelated family and blamed
    them (twice now: first a store/session write, now an env write). Both
    times the damage landed elsewhere in the suite while a single-file run was
    green, so the signal belongs at the boundary this file owns.

    MODULE scope on purpose. The first version was per-test, which measured at
    the wrong moment: a per-test fixture that does not depend on ``monkeypatch``
    can finalize BEFORE monkeypatch undoes its own changes, so it reported 18
    leaks that were really just not-yet-undone patches. Checking once, after
    every test in the file has finished, is the claim actually being made —
    and it cannot be fooled by fixture ordering.

    Compared as a delta, so vars the harness legitimately sets
    (BEACON_SESSION_KIND / BEACON_TEST_MODE) are not mistaken for ours.
    """
    before = {k: v for k, v in os.environ.items() if k.startswith("BEACON_")}
    yield
    after = {k: v for k, v in os.environ.items() if k.startswith("BEACON_")}
    added = {k: after[k] for k in set(after) - set(before)}
    changed = {k: (before[k], after[k]) for k in set(after) & set(before)
               if before[k] != after[k]}
    assert not added and not changed, (
        f"this module leaked env state (added={added}, changed={changed}). "
        "Use monkeypatch.setenv, never a raw os.environ write — a raw write "
        "survives teardown and reaches every later test in the process.")


@pytest.fixture(autouse=True)
def _no_real_store(monkeypatch):
    """Make reaching the REAL store a loud failure in this file.

    ``_read_current_project`` reads documents through ``store.get_store()``.
    The first version of these tests stubbed ``load_project`` but left that
    call live, so every handler test quietly talked to the real (cloud) store
    — which resolves a session id and, on a machine with no
    ``.beacon/session.json`` yet, CREATES one via a ``session.json.tmp``
    atomic write. That temp file is not on the suite's known-leak allowlist,
    so under CI's parallel workers it appeared inside ANOTHER test's
    before/after window and failed an innocent bystander
    (``test_advisory_silent_when_no_project``), which is exactly the
    misattribution tests/conftest.py documents. Locally it never reproduced,
    because this machine already has a fresh session.json and so no write
    happens.

    A unit test for this module has no business reaching the live store, so
    the reach is replaced AND forbidden: an unstubbed one raises here instead
    of silently doing I/O and blaming someone else three files away.
    """
    import store as _store

    def _refuse(*_a, **_k):
        raise AssertionError(
            "a test in this file reached the REAL store (store.get_store). "
            "Stub the dependency — a cmd_recap unit test must not touch the "
            "live project store, which resolves/creates a session file and "
            "then gets attributed to an unrelated test.")

    monkeypatch.setattr(_store, "get_store", _refuse)


#: The day a relative spec of "yesterday" actually resolves to, read from the
#: SAME parser the handler uses. A fixture that hardcodes an absolute date and
#: then asks for "yesterday" is a date bomb: it passes on the day it was
#: written and silently stops exercising anything the next day (these tests
#: broke exactly that way one day after being written). This repo already
#: guards the same mistake elsewhere (scripts/check-arm-fixed-timestamp.py).
_YESTERDAY_FROM, _YESTERDAY_TO = dw.parse_since("yesterday")


def _project(pid="p-test", *, commit_at=None):
    commit_at = _YESTERDAY_FROM if commit_at is None else commit_at
    return {
        "project_id": pid, "name": pid.upper(),
        "milestones": [{
            "id": "ms-1", "title": "the work", "status": "in_progress",
            "created_at": "2026-01-01T00:00:00Z",
            "entries": [{"id": "e-1", "type": "commit", "description": "did a thing",
                         "created_at": commit_at}],
        }],
    }


def _stub_store(monkeypatch, documents=()):
    """Replace ``store.get_store`` with one returning ``documents``."""
    import store as _store

    class _S:
        def list_documents(self):
            return list(documents)

    monkeypatch.setattr(_store, "get_store", lambda *a, **k: _S())


@pytest.fixture
def local(monkeypatch):
    """Pin the module into local mode with one readable project, no real I/O."""
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", lambda: _project())
    _stub_store(monkeypatch)
    return _project()


def _run(handler, capsys, monkeypatch, **env):
    """Drive a handler in JSON mode, with every env write SCOPED to the test.

    The first version wrote straight to ``os.environ`` and never undid it, so
    ``BEACON_JSON=1`` (and whichever period/count flags a case set) leaked into
    every test that ran afterwards IN THE SAME PROCESS. Measured: after this
    file, ``BEACON_JSON`` / ``BEACON_SINCE`` / ``BEACON_EVIDENCE`` /
    ``BEACON_LIMIT`` were all still set. Downstream that flipped other
    families' CLI output to JSON and broke five ``test_trek_cli.py``
    assertions — tests that have nothing to do with this feature and that
    pass fine on their own. The autouse ``_clean_env`` fixture did not save us:
    it clears at SETUP, so it only ever cleaned up after the previous test,
    never after the last one, and a raw ``os.environ`` write is invisible to
    monkeypatch's own teardown.

    ``monkeypatch.setenv`` undoes itself at teardown, which is the whole point.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BEACON_JSON", "1")
    handler()
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Period resolution — the error paths had no coverage at all
# ---------------------------------------------------------------------------

def test_since_and_from_to_together_is_refused(monkeypatch, capsys):
    """Honouring one and ignoring the other would answer an unasked period."""
    monkeypatch.setenv("BEACON_SINCE", "yesterday")
    monkeypatch.setenv("BEACON_FROM", "2026-10-14")
    monkeypatch.setenv("BEACON_TO", "2026-10-14")
    with pytest.raises(SystemExit) as exc:
        cmd_recap._resolve_window()
    assert exc.value.code == 1
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.parametrize("given,missing", [
    ({"BEACON_TO": "2026-10-14"}, "--from"),
    ({"BEACON_FROM": "2026-10-14"}, "--to"),
])
def test_one_sided_explicit_range_is_refused(monkeypatch, capsys, given, missing):
    """One end alone used to mean "unbounded on the other side".

    Measured before the fix: ``--to 2026-10-14`` with no ``--from`` returned
    the entire project history (179 targets / 7625 evidence vs 27 / 307 for the
    default window) with exit code 0 — a caller who bounded a query and got
    everything back could not tell from the output.
    """
    for k, v in given.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        cmd_recap._resolve_window()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert missing in err
    assert "--since" in err, "the error must name the flag that DOES take an open period"


def test_inverted_explicit_range_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_FROM", "2026-10-20")
    monkeypatch.setenv("BEACON_TO", "2026-10-14")
    with pytest.raises(SystemExit) as exc:
        cmd_recap._resolve_window()
    assert exc.value.code == 1
    assert "is after" in capsys.readouterr().err


def test_default_period_is_applied_and_labelled_as_default(monkeypatch):
    lo, hi, label = cmd_recap._resolve_window()
    assert (lo, hi) == dw.parse_since(cmd_recap.DEFAULT_SINCE)
    assert "default" in label, \
        "an unstated period must say it was defaulted, not look like a choice"


def test_bad_period_refuses_instead_of_defaulting(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_SINCE", "lastweek")
    with pytest.raises(SystemExit) as exc:
        cmd_recap._resolve_window()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "unparseable period" in err
    assert "last-week" in err, "the error must list the accepted spellings"


# ---------------------------------------------------------------------------
# Count flags — a negative value used to mean "all"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["-1", "-5"])
def test_negative_count_is_refused_not_read_as_all(monkeypatch, capsys, raw):
    """``--evidence -1`` reached the same ``<= 0`` branch as 0 and returned
    every row — the opposite end of the scale from what the flag name says."""
    monkeypatch.setenv("BEACON_EVIDENCE", raw)
    with pytest.raises(SystemExit) as exc:
        cmd_recap._int_env("BEACON_EVIDENCE", 5)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--evidence" in err and "0 or more" in err


def test_zero_count_still_means_no_cap(monkeypatch):
    monkeypatch.setenv("BEACON_EVIDENCE", "0")
    assert cmd_recap._int_env("BEACON_EVIDENCE", 5) == 0


def test_non_integer_count_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("BEACON_LIMIT", "abc")
    with pytest.raises(SystemExit):
        cmd_recap._int_env("BEACON_LIMIT", 0)
    assert "--limit" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# recap targets — the assembled answer
# ---------------------------------------------------------------------------

def test_targets_reports_the_window_the_project_and_the_movement(local, capsys, monkeypatch):
    out = _run(cmd_recap.cmd_recap_targets, capsys, monkeypatch, BEACON_SINCE="yesterday")
    assert out["window"]["from"] == out["window"]["to"]
    assert out["window"]["spec"] == "yesterday"
    assert out["project_id"] == "p-test"
    assert [t["target_id"] for t in out["targets"]] == ["ms-1"]


def test_targets_evidence_cap_does_not_lower_the_count(monkeypatch, capsys):
    project = {"project_id": "p", "name": "P", "milestones": [{
        "id": "ms-1", "title": "m", "status": "todo",
        "created_at": "2026-01-01T00:00:00Z",
        "entries": [{"id": f"e-{i}", "type": "commit", "description": f"c{i}",
                     "created_at": _YESTERDAY_FROM} for i in range(8)]}]}
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", lambda: project)
    _stub_store(monkeypatch)
    out = _run(cmd_recap.cmd_recap_targets, capsys, monkeypatch,
               BEACON_SINCE="yesterday", BEACON_EVIDENCE="3")
    t = out["targets"][0]
    assert len(t["evidence"]) == 3
    assert t["evidence_count"] == 8


def test_named_project_in_local_mode_refuses_rather_than_reading_this_one(
        monkeypatch, capsys):
    """Answering about a DIFFERENT project than the one asked for would be
    indistinguishable from the right answer in the output."""
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setenv("BEACON_RECAP_PROJECT", "some-other-project")
    monkeypatch.setenv("BEACON_SINCE", "yesterday")
    with pytest.raises(SystemExit) as exc:
        cmd_recap.cmd_recap_targets()
    assert exc.value.code == 1
    assert "cloud mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# recap projects — the sweep, its source, and its fallback reason
# ---------------------------------------------------------------------------

def test_projects_in_local_mode_says_it_saw_only_one(local, capsys, monkeypatch):
    out = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch, BEACON_SINCE="yesterday")
    assert out["source"] == "local"
    assert out["projects_scanned"] == 1, \
        "a one-project answer must not be rendered as a 12-project sweep"


def test_projects_prefers_the_server_sweep(monkeypatch, capsys):
    served = {"projects": [{"project_id": "p1", "name": "P1", "target_count": 1,
                            "evidence_count": 2, "last_evidence_at": "2026-10-14",
                            "targets": []}],
              "project_count": 1, "unavailable": [], "projects_scanned": 3,
              "source": "server"}

    class _C:
        def me_recap(self, **kw):
            return dict(served)

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_recap, "_get_api_client", lambda: (_C(), {}))
    out = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch, BEACON_SINCE="yesterday")
    assert out["source"] == "server"
    assert "fallback_reason" not in out
    assert out["projects_scanned"] == 3


def test_projects_falls_back_and_carries_the_reason_it_observed(
        monkeypatch, capsys):
    class _C:
        def me_recap(self, **kw):
            raise RuntimeError("API error 404: Not Found")

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_recap, "_get_api_client", lambda: (_C(), {}))
    monkeypatch.setattr(cmd_recap, "_scan_owned_projects",
                        lambda lo, hi: ([], 7, "cloud"))
    out = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch, BEACON_SINCE="yesterday")
    assert out["source"] == "cloud"
    assert "404" in out["fallback_reason"], \
        "the reason must be the one observed, not a guessed cause"
    assert out["projects_scanned"] == 7


def test_fallback_reason_does_not_leak_between_calls(monkeypatch, capsys):
    """The regression this file was written for.

    The reason used to live in a module-level list that was appended to and
    never cleared, and the renderer read index ``[0]`` — so a SECOND call in
    the same process reported the FIRST call's reason. Two calls here, two
    different server failures, and the second answer must carry the second
    reason.
    """
    reasons = iter([RuntimeError("API error 404: Not Found"),
                    TimeoutError("read timed out")])

    class _C:
        def me_recap(self, **kw):
            raise next(reasons)

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_recap, "_get_api_client", lambda: (_C(), {}))
    monkeypatch.setattr(cmd_recap, "_scan_owned_projects",
                        lambda lo, hi: ([], 1, "cloud"))

    first = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch, BEACON_SINCE="yesterday")
    second = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch, BEACON_SINCE="yesterday")
    assert "404" in first["fallback_reason"]
    assert "timed out" in second["fallback_reason"], (
        f"second call reported the first call's reason: "
        f"{second.get('fallback_reason')!r}")


def test_projects_limit_truncates_the_list_but_not_the_count(monkeypatch, capsys):
    def _rows(lo, hi):
        rows = []
        for i in range(4):
            rows.append({"project_id": f"p{i}", "name": f"P{i}", "result": {
                "moved": True, "target_count": 1, "evidence_count": 1,
                "last_evidence_at": f"{_YESTERDAY_FROM}T0{i}:00:00Z", "targets": [],
                "undated": {"count": 0, "by_kind": {}},
                "sources_unavailable": []}})
        return rows, 4, "cloud"

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_recap, "_server_recap", lambda lo, hi: (None, "no endpoint"))
    monkeypatch.setattr(cmd_recap, "_scan_owned_projects", _rows)
    out = _run(cmd_recap.cmd_recap_projects, capsys, monkeypatch,
               BEACON_SINCE="yesterday", BEACON_LIMIT="2")
    assert len(out["projects"]) == 2
    assert out["project_count"] == 4, "a display cap must not lower the count"


def test_document_read_failure_is_not_reported_as_zero(monkeypatch):
    """A document source that could not be READ must say so (受入条件 6/8).

    The first version of this file got that right for decisions (via
    ``_decisions_for``, returning ``None`` with a docstring explaining why) and
    then wrote the opposite for documents, inline, at every call site: a failed
    store read became ``[]`` and ``derive`` reported ``document`` as
    CONSULTED. So "the store was down" and "this project has no documents in
    the window" were the same answer — the exact confusion this milestone
    exists to remove, in its own code. An independent maintainability review
    reproduced it.
    """
    import store as _store

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", lambda: _project())

    def _boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(_store, "get_store", _boom)
    _pid, _name, _data, documents, _dec = cmd_recap._read_current_project()
    assert documents is None, \
        "a failed read must be None (not consulted), never [] (zero records)"

    # ... and a SUCCESSFUL read of zero documents must still count as consulted,
    # or the fix would have traded one conflation for its mirror image.
    class _Empty:
        def list_documents(self):
            return []

    monkeypatch.setattr(_store, "get_store", lambda *a, **k: _Empty())
    _pid, _name, _data, documents, _dec = cmd_recap._read_current_project()
    assert documents == [], "an empty-but-successful read is zero, not unknown"


# ---------------------------------------------------------------------------
# The caveats must survive into the rendered text, not only the JSON
# ---------------------------------------------------------------------------

def _shipping_only_project():
    """A project whose only in-window activity is an unattributable deploy."""
    return {
        "project_id": "p", "name": "P",
        "milestones": [{"id": "ms-1", "title": "m", "status": "todo",
                        "created_at": "2026-01-01T00:00:00Z", "entries": []}],
        "deployments": [{"id": "deploy-9", "description": "本番に出した",
                         "milestones": [],
                         "date": _YESTERDAY_FROM + "T12:00:00Z"}],
    }


def test_text_output_does_not_contradict_itself_on_a_shipping_only_day(
        monkeypatch, capsys):
    """The printed text must not say "nothing progressed" and then correct it.

    The branch keyed off ``targets`` while ``moved`` had been widened to
    "targets OR unattributed shipping", so a day whose whole story was a
    deploy printed 「この期間に進んだ対象はありません。」 on line 2 and
    「プロジェクトは動いています」 three lines later — one output, two answers
    (AX review PR#810). A reader who trusts the leading assertion and stops
    gets exactly the wrong answer this milestone exists to remove.
    """
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", _shipping_only_project)
    _stub_store(monkeypatch)
    monkeypatch.setenv("BEACON_SINCE", "yesterday")
    cmd_recap.cmd_recap_targets()
    out = capsys.readouterr().out

    assert "進んだ対象はありません" not in out, \
        "a day that shipped must not be announced as nothing having progressed"
    assert "出荷の証跡" in out, "it must say what DID happen"
    assert "対象が特定できていない出荷" in out, \
        "the unattributed rows themselves must be listed"
    assert "deploy-9" in out


def test_text_output_still_says_nothing_when_nothing_happened(
        monkeypatch, capsys):
    """The mirror case: widening ``moved`` must not make every day look busy."""
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", _shipping_only_project)
    _stub_store(monkeypatch)
    monkeypatch.setenv("BEACON_FROM", "2020-01-01")
    monkeypatch.setenv("BEACON_TO", "2020-01-02")
    cmd_recap.cmd_recap_targets()
    out = capsys.readouterr().out
    assert "進んだ対象はありません" in out
    assert "出荷の証跡" not in out


def test_breadth_view_says_how_much_movement_names_no_target(
        monkeypatch, capsys):
    """`recap projects` must carry the same caveat at its own altitude."""
    def _rows(lo, hi):
        import progress_window as pw
        res = pw.derive(_shipping_only_project(), from_date=lo, to_date=hi,
                        documents=[], decisions=[], evidence_limit=0)
        return [{"project_id": "p", "name": "P", "result": res}], 1, "cloud"

    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_recap, "_server_recap",
                        lambda lo, hi: (None, "no endpoint"))
    monkeypatch.setattr(cmd_recap, "_scan_owned_projects", _rows)
    monkeypatch.setenv("BEACON_SINCE", "yesterday")
    cmd_recap.cmd_recap_projects()
    out = capsys.readouterr().out
    assert "動いたプロジェクト 1 件" in out, \
        "a project that moved only by shipping must be listed"
    assert "対象未特定の出荷 1 件" in out
    assert "どの対象の記録か分からない出荷" in out


def test_text_output_states_what_could_not_be_judged(monkeypatch, capsys):
    """Printed for a ZERO result too — that is when a reader is likeliest to
    read "could not tell" as "nothing happened"."""
    project = {"project_id": "p", "name": "P", "milestones": [{
        "id": "ms-1", "title": "m", "status": "done", "created_at": "",
        "entries": [{"id": "e-1", "type": "commit", "description": "undated",
                     "created_at": ""}]}]}
    monkeypatch.setattr(cmd_recap, "_is_cloud_mode", lambda: False)
    monkeypatch.setattr(cmd_recap, "load_project", lambda: project)
    _stub_store(monkeypatch)
    monkeypatch.setenv("BEACON_SINCE", "yesterday")
    cmd_recap.cmd_recap_targets()
    out = capsys.readouterr().out
    assert "進んだ対象はありません" in out
    assert "判定できなかった" in out, \
        "a zero answer that hides an unjudgeable record is the bug this MS is about"
