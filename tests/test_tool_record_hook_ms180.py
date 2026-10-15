"""ms-180 e-7001 / e-7002 / e-7014 — "using a tool leaves a record", driven by the ledger.

``bin/beacon-save-hook.sh`` fired on every MCP call, and its body was a ``case``
statement naming two Google-Drive servers. So the promise held for two tools and
silently failed for every other tool in the project. These tests drive the real
hook as a subprocess against a real project and assert what actually lands.

The four ledger outcomes must stay four. Each produces a different observable
result, and collapsing any pair would hide the one that is a debt behind one that
is an answer:

  specified   → a record appears (at the declared grain)
  declined    → no record, and that is the final answer
  undecided   → no record, and it is listed as a debt to close
  unregistered→ no record, and the USE is noted so the gap cannot stay silent

Also pinned: the shim really is a shim (no tool name survives in the shell file),
because the whole point of e-7001 is that the policy moved out of code and into
data. A test that only checked behaviour would pass just as well with the old
``case`` statement extended by hand.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import tool_ledger as tl       # noqa: E402
import tool_turn as tt         # noqa: E402
import tool_usage_debt as tdebt  # noqa: E402

HOOK = REPO / "bin" / "beacon-tool-record-hook.py"
SHIM = REPO / "bin" / "beacon-save-hook.sh"
BEACON = REPO / "bin" / "beacon"


@pytest.fixture
def project(tmp_path):
    """A real dev project with one active milestone, plus a ledger."""
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "total_tasks": 0, "done_tasks": 0,
                        "entries": []}],
        "documents": [],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _declare(root: Path, *rows: dict) -> None:
    led = tl.empty_ledger()
    for row in rows:
        tl.upsert(led, row)
    tl.save_ledger(tl.ledger_path(root), led)


def _fire(root: Path, tool_name: str, tool_input=None, *,
          event="PostToolUse", session="sess-A") -> None:
    payload = {"hook_event_name": event, "session_id": session,
               "cwd": str(root), "tool_name": tool_name,
               "tool_input": tool_input or {}}
    proc = subprocess.run([sys.executable, str(HOOK)],
                          input=json.dumps(payload), capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def _entries(root: Path) -> list:
    data = json.loads((root / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return data["milestones"][0]["entries"]


def _descriptions(root: Path) -> list:
    return [e.get("description", "") for e in _entries(root)]


# ---------------------------------------------------------------------------
# The four outcomes.
# ---------------------------------------------------------------------------

def test_specified_sink_produces_a_record(project):
    _declare(project, tl.build_record(
        "mcp__gmail__send_email", purpose="メールを送る",
        effect=tl.EFFECT_OUTWARD, recorder=tl.RECORDER_EVIDENCE))
    _fire(project, "mcp__gmail__send_email", {"subject": "ご提案"})
    assert len(_entries(project)) == 1
    assert "ご提案" in _descriptions(project)[0]


def test_declined_sink_produces_no_record(project):
    _declare(project, tl.build_record(
        "Grep", purpose="下調べ", effect=tl.EFFECT_READ_ONLY,
        decline_reason="下調べは証跡ではない"))
    _fire(project, "Grep", {"pattern": "foo"})
    assert _entries(project) == []


def test_undecided_sink_produces_no_record_but_is_listed_as_debt(project):
    _declare(project, tl.build_record(
        "mcp__slack__channels_list", purpose="一覧を見る",
        effect=tl.EFFECT_READ_ONLY))
    _fire(project, "mcp__slack__channels_list", {})
    assert _entries(project) == []
    led = tl.load_ledger(tl.ledger_path(project))
    assert [r["name"] for r in tl.undecided_tools(led)] == \
        ["mcp__slack__channels_list"]


def test_unregistered_tool_produces_no_record_but_notes_the_use(project):
    _declare(project, tl.build_record("Read", purpose="読む",
                                      effect=tl.EFFECT_READ_ONLY))
    _fire(project, "mcp__nobody__declared_me", {})
    assert _entries(project) == []
    rows = tdebt.load_debt(tdebt.debt_path(str(project / ".beacon")))
    assert [r["name"] for r in rows] == ["mcp__nobody__declared_me"]
    assert rows[0]["count"] == 1


def test_repeated_unregistered_use_aggregates_rather_than_appending(project):
    """One row per tool with a count: a raw log would grow on the hot path and
    bury the actionable unit (which tools need a decision)."""
    _declare(project)
    for _ in range(3):
        _fire(project, "mcp__nobody__declared_me", {})
    rows = tdebt.load_debt(tdebt.debt_path(str(project / ".beacon")))
    assert len(rows) == 1 and rows[0]["count"] == 3


def test_an_undecided_tool_is_not_counted_as_unregistered(project):
    """Registered-but-unjudged and never-registered are different debts and must
    not be merged into one list."""
    _declare(project, tl.build_record("mcp__a__thing", purpose="p",
                                      effect=tl.EFFECT_READ_ONLY))
    _fire(project, "mcp__a__thing", {})
    assert tdebt.load_debt(tdebt.debt_path(str(project / ".beacon"))) == []


# ---------------------------------------------------------------------------
# e-7014 — the grain is per tool.
# ---------------------------------------------------------------------------

def test_each_grain_records_every_call(project):
    _declare(project, tl.build_record(
        "mcp__gmail__send_email", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE, grain=tl.GRAIN_EACH))
    _fire(project, "", event="UserPromptSubmit")
    for subject in ("A", "B", "C"):
        _fire(project, "mcp__gmail__send_email", {"subject": subject})
    assert len(_entries(project)) == 3


def test_turn_grain_folds_a_turn_into_one_record(project):
    _declare(project, tl.build_record(
        "mcp__gmail__search_emails", purpose="確認する",
        effect=tl.EFFECT_READ_ONLY, recorder=tl.RECORDER_EVIDENCE,
        grain=tl.GRAIN_TURN))
    _fire(project, "", event="UserPromptSubmit")
    for query in ("A社", "B社", "C社"):
        _fire(project, "mcp__gmail__search_emails", {"query": query})
    assert len(_entries(project)) == 1
    _fire(project, "", event="UserPromptSubmit")
    for query in ("D社", "E社"):
        _fire(project, "mcp__gmail__search_emails", {"query": query})
    assert len(_entries(project)) == 2


def test_both_grains_hold_simultaneously_for_different_tools(project):
    """受入条件4: 「毎回残す」と「1 ターン分を畳む」が別の道具で別々に成立する."""
    _declare(
        project,
        tl.build_record("mcp__gmail__send_email", purpose="送る",
                        effect=tl.EFFECT_OUTWARD,
                        recorder=tl.RECORDER_EVIDENCE, grain=tl.GRAIN_EACH),
        tl.build_record("mcp__gmail__search_emails", purpose="確認する",
                        effect=tl.EFFECT_READ_ONLY,
                        recorder=tl.RECORDER_EVIDENCE, grain=tl.GRAIN_TURN))
    _fire(project, "", event="UserPromptSubmit")
    for _ in range(3):
        _fire(project, "mcp__gmail__search_emails", {"query": "x"})
        _fire(project, "mcp__gmail__send_email", {"subject": "y"})
    descriptions = _descriptions(project)
    folded = [d for d in descriptions if "search_emails" in d]
    each = [d for d in descriptions if "send_email" in d]
    assert len(folded) == 1, descriptions
    assert len(each) == 3, descriptions


def test_turn_grain_folds_per_session_not_globally(project):
    """Two sessions in one cwd are two turns. Folding across them would drop one
    session's record entirely."""
    _declare(project, tl.build_record(
        "mcp__gmail__search_emails", purpose="確認する",
        effect=tl.EFFECT_READ_ONLY, recorder=tl.RECORDER_EVIDENCE,
        grain=tl.GRAIN_TURN))
    for session in ("sess-A", "sess-B"):
        _fire(project, "", event="UserPromptSubmit", session=session)
        _fire(project, "mcp__gmail__search_emails", {"query": "x"},
              session=session)
        _fire(project, "mcp__gmail__search_emails", {"query": "y"},
              session=session)
    assert len(_entries(project)) == 2


def test_turn_grain_degrades_to_each_when_no_turn_boundary_was_ever_seen(project):
    """An install wired for PostToolUse only never advances the epoch. Folding
    anyway would record the first call and silently drop every later one forever,
    so the grain degrades toward MORE recording instead."""
    _declare(project, tl.build_record(
        "mcp__gmail__search_emails", purpose="確認する",
        effect=tl.EFFECT_READ_ONLY, recorder=tl.RECORDER_EVIDENCE,
        grain=tl.GRAIN_TURN))
    # NOTE: no UserPromptSubmit fire at all.
    assert tt.current_epoch(tt.turn_path(str(project / ".beacon")),
                            "sess-A") is None
    for query in ("A", "B", "C"):
        _fire(project, "mcp__gmail__search_emails", {"query": query})
    assert len(_entries(project)) == 3


# ---------------------------------------------------------------------------
# The policy lives in data, not in the hook.
# ---------------------------------------------------------------------------

def _executable_code(path: Path) -> str:
    """The file's CODE with comments and docstrings removed.

    Prose has to be excluded, not just ``#`` comments: both files explain the
    ``case``-statement history they replaced, and that explanation legitimately
    names the two Google-Drive servers. Grepping raw text would therefore fail on
    the documentation while saying nothing about the logic. For Python the AST
    round-trip drops comments and docstrings exactly; for the shell shim there is
    no AST, so comment lines are stripped and the remainder is one ``exec`` line.
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        import ast
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        return ast.unparse(ast.fix_missing_locations(tree))
    return "\n".join(line for line in source.splitlines()
                      if not line.lstrip().startswith("#"))


def test_the_shell_hook_is_a_shim_with_no_tool_name_and_no_case_statement():
    """Behaviour tests alone would also pass if someone extended the old ``case``
    statement by hand. The point of e-7001 is that the policy LEFT the code."""
    code = _executable_code(SHIM)
    assert "case" not in code
    assert "mcp__" not in code
    assert "beacon-tool-record-hook.py" in code


def test_the_python_hook_names_no_tool_and_no_server():
    """Same property for the hook that replaced it: adding a tool to the record
    set must be a ``beacon tool add``, not an edit of this file."""
    code = _executable_code(HOOK)
    assert "mcp__" not in code
    assert "google" not in code.lower()
    assert "drive" not in code.lower()


def test_the_code_extractor_actually_strips_prose_and_keeps_logic(tmp_path):
    """Test-the-test: an extractor that silently returned the whole file would
    make the two assertions above fail on documentation, and one that returned
    nothing would make them pass vacuously."""
    sample = tmp_path / "s.py"
    sample.write_text('"""doc mentions mcp__gmail__send_email."""\n'
                      'x = 1  # comment mentions mcp__drive__thing\n',
                      encoding="utf-8")
    code = _executable_code(sample)
    assert "mcp__" not in code      # prose + comments gone
    assert "x = 1" in code          # logic kept
    sample.write_text('"""doc."""\ny = "mcp__real__call"\n', encoding="utf-8")
    assert "mcp__real__call" in _executable_code(sample)  # a real literal stays


def test_adding_a_tool_to_the_record_set_needs_no_code_change(project):
    """The end-to-end form of the same claim: a brand-new tool starts recording
    purely because a ledger row appeared."""
    _declare(project)
    _fire(project, "mcp__brand__new_sender", {"title": "x"})
    assert _entries(project) == []
    _declare(project, tl.build_record(
        "mcp__brand__new_sender", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    _fire(project, "mcp__brand__new_sender", {"title": "x"})
    assert len(_entries(project)) == 1


# ---------------------------------------------------------------------------
# Fail-safe: a recording hook must never disturb the session it observes.
# ---------------------------------------------------------------------------

# Every JSON payload below carries an explicit ``cwd``. An ABSENT cwd makes the
# hook fall back to the process cwd, which during a test run is this repository —
# and the hook would then note its debt into the repo's own .beacon (observed
# while writing these tests: a `tool-unregistered.json` appeared at the repo root).
# The malformed-input contract is about not crashing, so it is exercised inside a
# tmp dir; the cwd-fallback behaviour itself is correct hook semantics and is
# covered separately by ``test_hook_outside_a_beacon_project_is_a_clean_noop``.
@pytest.mark.parametrize("body", [
    "", "   ", "not json", "[]", "null",
    '{"hook_event_name": "PostToolUse", "cwd": "%(cwd)s"}',      # no tool name
    '{"hook_event_name": "PostToolUse", "tool_name": "", "cwd": "%(cwd)s"}',
    '{"hook_event_name": "SomethingElse", "tool_name": "Bash", "cwd": "%(cwd)s"}',
    '{"hook_event_name": "PostToolUse", "tool_name": "x", '
    '"tool_input": "a string, not an object", "cwd": "%(cwd)s"}',
    '{"hook_event_name": "PostToolUse", "tool_name": "x", "cwd": "/nonexistent"}',
])
def test_hook_exits_cleanly_on_any_malformed_input(tmp_path, body):
    payload = body % {"cwd": str(tmp_path)} if "%(cwd)s" in body else body
    proc = subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # a recording hook injects nothing into context


def test_corrupt_ledger_records_nothing_and_does_not_fail(project):
    Path(tl.ledger_path(project)).write_text("{not json", encoding="utf-8")
    _fire(project, "mcp__gmail__send_email", {"subject": "x"})
    assert _entries(project) == []


def test_hook_outside_a_beacon_project_is_a_clean_noop(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"hook_event_name": "PostToolUse",
                          "session_id": "s", "cwd": str(tmp_path),
                          "tool_name": "mcp__x__y", "tool_input": {}}),
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ""
