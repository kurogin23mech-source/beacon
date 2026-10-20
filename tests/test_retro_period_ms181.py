"""`beacon retro` answers the period it was asked for, or refuses (ms-181 e-7101).

What was wrong
==============
Measured on 2026-10-20 against this repo's own code:

  1. ``beacon retro --since yesterday --until today`` exited **0**, echoed
     ``"period": {"since": "yesterday", ...}`` and returned an empty week. The
     spec was neither honoured nor refused — the output looked like a confident
     "nothing happened", and a reader had no way to tell that apart from "your
     words were ignored". The same words work in ``beacon recap``, so one
     vocabulary meant two things across two verbs, both reporting success.
  2. A *valid* ISO window was wrong too: for ``2026-10-19..2026-10-20`` a
     commit stamped ``2026-10-20T08:00:00Z`` was dropped, because
     ``"2026-10-20T08:00:00Z" > "2026-10-20"`` as text. ``--until`` defaults to
     today, so the work the retro was being written about was the part it lost.
  3. The same payload contradicted itself: ``source_breakdown`` goes through
     ``search``/``date_window`` and counted that commit (2 human events) while
     ``milestones`` used its own raw comparison and reported none. Three
     spellings of one rule lived in one function (raw compare /
     truncate-then-compare / date_window).
  4. Entries carrying no stamp at all were dropped silently, so "could not be
     judged" was indistinguishable from "did not happen".

Each test here is inverted by ``scripts/invert-ms184-guards.py`` — the one
harness for this period surface — so it has been watched failing.
"""

import datetime
import json
import os
import sys

import pytest

import cmd_retro
import core

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
# Stamps are relative to the real clock so the natural specs under test
# ("yesterday", "this-week", "2d") land on the fixture without a frozen clock.
# Every spec below resolves to a window ending today and starting no later than
# yesterday, so none of them depends on the weekday.

TODAY = datetime.date.today()
YESTERDAY = TODAY - datetime.timedelta(days=1)
WINDOW = (YESTERDAY.isoformat(), TODAY.isoformat())

#: A full timestamp DURING the window's last day — the shape that used to fall
#: out of the week, because a date-only upper bound sorts before it as text.
ENTRY_END_OF_WINDOW = {
    "id": "e-9", "type": "commit", "description": "shipped", "status": "",
    "created_at": TODAY.isoformat() + "T08:00:00Z",
}
ENTRY_UNDATED = {"id": "e-8", "type": "task", "description": "no stamp",
                 "status": "todo", "created_at": ""}
ENTRY_LONG_BEFORE = {"id": "e-7", "type": "task", "description": "long before",
                     "status": "todo",
                     "created_at": (TODAY - datetime.timedelta(days=180))
                     .isoformat() + "T10:00:00Z"}


def _project():
    return {
        "name": "T",
        "profession": "dev",
        "milestones": [{
            "id": "ms-1", "title": "m", "status": "in_progress", "progress": 1,
            "entries": [dict(ENTRY_END_OF_WINDOW), dict(ENTRY_UNDATED),
                        dict(ENTRY_LONG_BEFORE)],
        }],
        "deployments": [{
            "id": "e-50", "type": "minor",
            "date": TODAY.isoformat() + "T09:00:00Z",
            "milestones": ["ms-1"], "target_ids": ["ms-1"],
            "newly_completed_ms": [], "description": "ship",
        }],
    }


def _prepare(monkeypatch, capsys, since, until):
    """Drive ``cmd_retro_prepare`` the way both CLI fronts do (via env)."""
    monkeypatch.setattr(cmd_retro, "load_project", _project)
    monkeypatch.setattr(cmd_retro, "_load_local_documents", lambda: [])
    monkeypatch.setenv("BEACON_SINCE", since)
    monkeypatch.setenv("BEACON_UNTIL", until)
    monkeypatch.delenv("BEACON_RETRO_CATCH_UP", raising=False)
    cmd_retro.cmd_retro_prepare()
    return json.loads(capsys.readouterr().out)


def _entry_ids(payload):
    return [e["id"] for m in payload["milestones"] for e in m["entries"]]


# ---------------------------------------------------------------------------
# 受入条件 1: an unreadable period fails visibly instead of answering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["yesterdayy", "last fortnight", "0d", "--until"])
def test_an_unreadable_period_exits_nonzero_instead_of_an_empty_week(
        bad, monkeypatch, capsys):
    """A period nobody can read must not come back as a confident empty answer.

    This is the headline defect. An error gets fixed; a plausible wrong answer
    gets believed and written into a weekly retro document.
    """
    with pytest.raises(SystemExit) as exc:
        _prepare(monkeypatch, capsys, bad, WINDOW[1])
    assert exc.value.code == 1

    out = capsys.readouterr()
    assert out.out.strip() == "", \
        "a refused period must print no payload at all — a partial JSON body " \
        "beside a nonzero exit invites a caller to parse it anyway"
    assert "--since" in out.err, "the message must name the flag at fault"
    assert bad in out.err, "the message must quote back what was rejected"


def test_the_refusal_teaches_the_vocabulary_it_wants(monkeypatch, capsys):
    """The accepted forms are listed by the parser, so help cannot drift."""
    with pytest.raises(SystemExit):
        _prepare(monkeypatch, capsys, "lastweek", WINDOW[1])
    err = capsys.readouterr().err
    for form in ("yesterday", "last-week", "YYYY-MM-DD"):
        assert form in err, f"{form!r} is accepted but the refusal never says so"


def test_an_inverted_window_is_refused_in_this_verb_s_own_flag_names(
        monkeypatch, capsys):
    """`--since` after `--until` is nonsense, and the advice must not point at
    `--from` / `--to`, which `beacon retro` does not have."""
    with pytest.raises(SystemExit) as exc:
        _prepare(monkeypatch, capsys, WINDOW[1], WINDOW[0])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--since" in err and "--until" in err
    assert "--from" not in err


# ---------------------------------------------------------------------------
# 受入条件 2: the natural vocabulary is the SAME one recap accepts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("since", ["yesterday", "this-week", "2d", "this-month",
                                   WINDOW[0]])
def test_retro_reads_the_same_period_words_as_recap(since, monkeypatch, capsys):
    """One vocabulary, one meaning, across both verbs that take a period.

    ``beacon recap --since yesterday`` already resolved these; retro ignoring
    them left the same words meaning two different things depending on which
    verb was typed — and the AI driving the Skill had no signal either way.
    """
    out = _prepare(monkeypatch, capsys, since, "today")
    # Resolved, not echoed: the bounds are ISO dates whatever was typed.
    assert out["period"]["since"] <= WINDOW[0]
    assert out["period"]["until"] == WINDOW[1]
    assert out["period"]["received"] == {"since": since, "until": "today"}
    assert ENTRY_END_OF_WINDOW["id"] in _entry_ids(out), \
        f"--since {since} resolved to {out['period']['since']}.." \
        f"{out['period']['until']} but lost the entry stamped inside it"


def test_the_resolved_window_is_not_the_echo_of_what_was_typed(monkeypatch,
                                                               capsys):
    """`period.since` used to be the raw string, which is how an ignored spec
    passed for an honoured one. The resolved bound and the spec this handler
    was handed are now two separate fields, so neither can stand in for the
    other. The second is ``received``, not ``requested``: both fronts default
    an absent flag before calling, so it is not a record of what a person
    typed and must not be named as if it were (AX review of PR #811)."""
    out = _prepare(monkeypatch, capsys, "yesterday", "today")
    assert out["period"]["since"] == WINDOW[0]
    assert out["period"]["until"] == WINDOW[1]
    assert out["period"]["received"]["since"] == "yesterday"


# ---------------------------------------------------------------------------
# 受入条件 4: one period rule — the facets of one payload must agree
# ---------------------------------------------------------------------------

def test_every_facet_of_one_payload_sees_the_same_record(monkeypatch, capsys):
    """The narrative, the deploys and the breakdown must not disagree.

    Before the fix, for yesterday..today: ``milestones`` 0 entries, ``deploys``
    1, ``source_breakdown`` ``{"human": 2}`` — one payload giving three
    different answers about the same two records, because each facet carried
    its own spelling of the period test. This is the behavioural form of "one
    rule, one place": a facet that grows a private comparison again drops out
    of step here.
    """
    out = _prepare(monkeypatch, capsys, *WINDOW)
    narrative = _entry_ids(out)
    assert narrative == [ENTRY_END_OF_WINDOW["id"]], \
        "the weekly narrative lost the commit stamped during the final day"
    assert [d["id"] for d in out["deploys"]] == ["e-50"]
    assert out["source_breakdown"].get("human") == \
        len(narrative) + len(out["deploys"])


def test_a_full_timestamp_on_the_final_day_is_inside_the_window():
    """`--until <today>` means "through today", not "before today began".

    Pinned on the walker directly as well as through the payload: this is the
    representation detail (date-only bound vs full timestamp) that silently
    changed the answer, and today is retro's default ``--until``.
    """
    got = core.collect_retro_entries([dict(ENTRY_END_OF_WINDOW)], *WINDOW)
    assert [e["id"] for e in got] == [ENTRY_END_OF_WINDOW["id"]]


def test_a_period_nothing_falls_into_really_returns_nothing():
    """The other direction: the window still excludes what lies outside it.

    Without this, a walker that returned everything would satisfy the test
    above — the shape of the original ms-184 bug (absence read as agreement).
    """
    entries = [dict(ENTRY_END_OF_WINDOW), dict(ENTRY_LONG_BEFORE),
               dict(ENTRY_UNDATED)]
    assert core.collect_retro_entries(entries, "2020-01-01", "2020-01-02") == []


# ---------------------------------------------------------------------------
# Records the period question cannot be asked about are counted, not vanished
# ---------------------------------------------------------------------------

def test_undated_entries_are_excluded_and_reported(monkeypatch, capsys):
    """"We could not judge these" must not read as "these did not happen"."""
    out = _prepare(monkeypatch, capsys, *WINDOW)
    assert ENTRY_UNDATED["id"] not in _entry_ids(out), \
        "an undated entry cannot be inside a window"
    assert out["undated"]["excluded"] == 1
    assert out["undated"]["by_type"] == {"task": 1}


def test_a_deploy_with_no_date_is_counted_too(monkeypatch, capsys):
    """The deploy facet reports what it could not judge, like the walker does.

    This facet used to truncate the stamp to ten characters and compare that,
    which happened to get the end-of-day case right and so hid the shared hole
    behind an accident. The accident did not extend to an undated deploy: it
    fell out of the window and was never counted. Pinned separately because
    a reversion of just this facet is otherwise invisible.
    """
    project = _project()
    project["deployments"].append({
        "id": "e-51", "type": "minor", "date": "",
        "milestones": [], "target_ids": [], "newly_completed_ms": [],
        "description": "no stamp",
    })
    monkeypatch.setattr(cmd_retro, "load_project", lambda: project)
    monkeypatch.setattr(cmd_retro, "_load_local_documents", lambda: [])
    monkeypatch.setenv("BEACON_SINCE", WINDOW[0])
    monkeypatch.setenv("BEACON_UNTIL", WINDOW[1])
    monkeypatch.delenv("BEACON_RETRO_CATCH_UP", raising=False)
    cmd_retro.cmd_retro_prepare()
    out = json.loads(capsys.readouterr().out)

    assert [d["id"] for d in out["deploys"]] == ["e-50"], \
        "a deploy with no stamp cannot be inside a window"
    assert out["undated"]["by_type"] == {"task": 1, "deploy": 1}
    assert out["undated"]["excluded"] == 2


def test_an_undated_record_is_in_window_when_no_period_was_asked(monkeypatch,
                                                                 capsys):
    """With no bounds there is no period question, so nothing is unjudgeable.

    Guards the boundary the three-valued classification turns on: an empty
    window must not start excluding records as "undated". Both fronts always
    pass bounds, but a programmatic caller need not.
    """
    out = _prepare(monkeypatch, capsys, "", "")
    assert ENTRY_UNDATED["id"] in _entry_ids(out)
    assert out["undated"] == {"excluded": 0, "by_type": {}}


# ---------------------------------------------------------------------------
# 受入条件 4: neither CLI front keeps a period rule of its own
# ---------------------------------------------------------------------------

def test_neither_front_interprets_the_period_itself():
    """bash and the Python front must hand the spec over verbatim.

    A front that resolved "yesterday" on its own would reintroduce the split:
    two parsers, two week/month-boundary behaviours, one flag name. Both MAY
    compute a default when the flag is absent (they do, in ISO) — what neither
    may do is translate a spec the caller actually gave.
    """
    bash = _read("bin/lib/cmd_retro.sh")
    assert 'BEACON_SINCE="$since" BEACON_UNTIL="$until"' in bash, \
        "bash must pass --since / --until through as given"
    assert '--since) since="${2:-}"' in bash, \
        "bash must store --since verbatim, not reshape it"

    py = _read("beacon_cli/dispatch.py")
    arm = py[py.index("def _handle_retro("):py.index("def _compute_default_since(")]
    assert "if not since:\n        since = _compute_default_since()" in arm, \
        "the Python front may only default an ABSENT --since"

    # Structural, not a substring sweep: the first draft of this test matched
    # the *name* and went red on a comment that merely cited the parser, which
    # is how a guard gets loosened until it stops guarding. Walk the function's
    # own call graph instead, so prose about the rule can never trip it and a
    # real call can never hide behind formatting.
    import ast
    tree = ast.parse(py)
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_handle_retro")
    called = {
        (node.func.attr if isinstance(node.func, ast.Attribute)
         else getattr(node.func, "id", ""))
        for node in ast.walk(handler) if isinstance(node, ast.Call)
    }
    assert "parse_since" not in called, \
        "the Python front must not parse a period itself — commands.py does"
    imported = {alias.name for node in ast.walk(handler)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names}
    assert "date_window" not in imported, \
        "the front reaching for the period module is how a second resolution " \
        "point starts; it passes the spec on instead"


@pytest.mark.parametrize("spec", ["3d", "2w", "6h", "2m"])
def test_until_refuses_a_length_of_time_instead_of_meaning_today(
        spec, monkeypatch, capsys):
    """`--until 3d` used to be accepted and silently mean `--until today`.

    A span back from now has no upper end of its own, so ``parse_since`` put
    today there — which is also what ``--until today`` and omitting the flag
    give. Measured by an independent AX review of PR #811: ``--since 7d
    --until 3d`` and ``--since 7d --until today`` produced byte-identical
    windows at exit 0. A caller who had just learned ``--since 3d`` means
    "3 days ago" would read ``--until 3d`` as "up to 3 days ago" and be handed
    "up to now". That is the same silent-substitution shape this whole task
    exists to remove, so it is refused rather than reinterpreted.
    """
    with pytest.raises(SystemExit) as exc:
        _prepare(monkeypatch, capsys, "7d", spec)
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "--until" in out.err and spec in out.err
    assert "--since" in out.err, \
        "the refusal must say where a span DOES belong, not just say no"


@pytest.mark.parametrize("spec", ["today", "yesterday", "last-week"])
def test_until_still_takes_a_point_in_time(spec, monkeypatch, capsys):
    """The refusal above must not swallow the forms that do carry an upper end.

    ``last-week`` matters most: its upper bound is last Sunday, genuinely not
    today, so a blanket "only ISO dates on --until" would have broken a working
    and useful spelling.
    """
    out = _prepare(monkeypatch, capsys, "2026-01-01", spec)
    assert out["period"]["until"]
    assert out["period"]["received"]["until"] == spec


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026-00-00"])
def test_a_date_shaped_string_that_is_not_a_date_is_refused(bad, monkeypatch,
                                                            capsys):
    """`2026-13-01` is digit-shaped, sorts like a date, and is not one.

    It used to pass straight through as a bound. Worse than silent: being
    lexicographically large it tripped the ordering check, so the caller was
    told its range was INVERTED and would go off swapping flags, never
    learning month 13 does not exist (AX review of PR #811). A misdirecting
    error is a wrong answer with a louder voice, so the message must name the
    real problem.
    """
    with pytest.raises(SystemExit) as exc:
        _prepare(monkeypatch, capsys, bad, "today")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "calendar date" in err, \
        f"the message must say {bad!r} is not a real date, not blame ordering"
    assert "is after" not in err, \
        "an impossible date must not be reported as an inverted range"


def test_the_undated_block_is_rendered_by_the_shared_renderer():
    """One renderer, so every verb reports this concept with the same keys."""
    import date_window as dw
    assert dw.undated_block({"task": 2, "commit": 1}) == {
        "excluded": 3, "by_type": {"commit": 1, "task": 2}}
    assert dw.undated_block({}) == {"excluded": 0, "by_type": {}}


def test_the_retro_stamp_set_is_named_not_an_inline_chain():
    """The set retro reads is declared, and differs from the shared one.

    ``RETRO_ENTRY_STAMPS`` is ``CREATION_STAMPS`` minus ``pushed_at`` (a push
    belongs to the push facet, not the narrative). The difference has to be
    visible to whoever changes the shared set next.
    """
    import date_window as dw
    assert dw.RETRO_ENTRY_STAMPS == ("created_at", "date")
    assert "pushed_at" in dw.CREATION_STAMPS
    assert "pushed_at" not in dw.RETRO_ENTRY_STAMPS

    walked = core.collect_retro_entries(
        [{"id": "e-p", "type": "push", "status": "",
          "pushed_at": TODAY.isoformat() + "T08:00:00Z"}], *WINDOW)
    assert walked == [], \
        "a push stamp must not enter the narrative — that is the push facet"


def test_the_retro_walker_reads_the_declared_stamp_set():
    """The walker must READ the declaration, not re-spell it inline.

    This guard is structural on purpose, and that is not a shortcut: naming the
    set was a pure refactor — the behaviour is byte-identical by design — so NO
    behavioural probe can separate "reads RETRO_ENTRY_STAMPS" from "has its own
    ``created_at`` or ``date`` chain". The first version of this test tried
    anyway (a push-only entry is excluded either way) and the inversion harness
    caught it passing with the fix reverted. A guard that cannot fail is worse
    than none, so the instrument matches what is actually being guaranteed:
    that a future change to the declaration reaches this caller.
    """
    import ast
    src = _read("lib/core.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "collect_retro_entries")

    names = {node.attr for node in ast.walk(fn)
             if isinstance(node, ast.Attribute)}
    assert "RETRO_ENTRY_STAMPS" in names, \
        "collect_retro_entries must read date_window.RETRO_ENTRY_STAMPS"

    # ...and must not carry its own copy of the field names beside it.
    inline = [node for node in ast.walk(fn)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get"
              and node.args
              and isinstance(node.args[0], ast.Constant)
              and node.args[0].value in ("created_at", "date", "pushed_at")]
    assert not inline, \
        ("the stamp field names are spelled inline again "
         f"({[n.args[0].value for n in inline]}) — the declaration in "
         "date_window is then not the only place they live")


def test_an_explicit_range_bound_that_is_not_a_date_is_refused():
    """`validate_range` is where recap's --from/--to meets this rule.

    retro never reaches it for a calendar-invalid bound (``parse_since``
    refuses first), so the retro-level test could not guard this check — the
    inversion harness caught that too. recap's explicit range DOES bypass
    ``parse_since`` and lands here, which is why the check belongs in the
    shared module rather than in retro's handler.
    """
    import date_window as dw
    msg = dw.validate_range("2026-13-01", "2026-10-20")
    assert "calendar date" in msg, msg
    assert "is after" not in msg, \
        "an impossible date must not be reported as an inverted range"
    # the upper bound is checked too, not just the lower one
    assert "calendar date" in dw.validate_range("2026-01-01", "2026-02-30")
    # and a genuinely inverted range of REAL dates still reports ordering
    assert "is after" in dw.validate_range("2026-10-20", "2026-10-19")
    # a usable range stays usable
    assert dw.validate_range("2026-10-19", "2026-10-20") == ""


def test_both_help_surfaces_list_the_same_period_vocabulary():
    """The two `--help` paths must not advertise different vocabularies.

    The bash front renders from the registry in lib/commands.py and is the
    DEFAULT path on macOS; the Python front prints its own usage block. The
    first version of this change hand-typed a partial subset into the registry
    (ending in "…", missing last-week / this-month / <N>w / <N>h / <N>m /
    A..B) while the Python side derived the full list — so the surface most
    readers reach was the incomplete one (AX review of PR #811). Both are now
    generated from the parser's own declaration.
    """
    import commands
    import date_window as dw
    entry = next(r for r in commands._help_registry()
                 if r["command"] == "beacon retro")
    for form in dw.SINCE_FORMS:
        assert form in entry["description"], \
            f"the registry help omits the accepted form {form!r}"

    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import beacon_cli.dispatch as dispatch  # package-relative imports inside
    assert tuple(dispatch._since_forms()) == tuple(dw.SINCE_FORMS), \
        "the Python front's help must come from the same declaration"


def test_the_help_registry_documents_the_flags_and_their_period():
    """`beacon retro --help` renders from the registry, which listed no flags
    at all — so the two flags the command reads, and the vocabulary they take,
    were absent from every help surface (`--help`, `beacon help --json`)."""
    import commands
    entry = next(r for r in commands._help_registry()
                 if r["command"] == "beacon retro")
    assert {"--since <period>", "--until <period>"} <= set(entry["flags"])
    assert "--until" in entry["description"], \
        "the description must say what each end means, not just name the flags"
