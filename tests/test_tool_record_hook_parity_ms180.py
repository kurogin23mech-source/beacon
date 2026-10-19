"""ms-180 — findings from the independent 思想 (philosophy) review of PR #802.

The philosophy judge asked a question the other two reviewers do not: does the
implementation keep the promises the SPEC made? It found five, and the first is
the most serious defect in the whole milestone:

  P1 (high)   The PostToolUse save hook has TWO entry points — the bash leg and
              the ``beacon-hook-save`` console script that
              ``commands._resolve_hook_command`` prefers FIRST. ms-180 rewrote
              only the bash leg to consult the ledger and left the Python one
              carrying its own hand-written Google-Drive table. On the install
              path users actually get, a declared record sink produced NO record
              and no debt either (the PreToolUse gate sees ``action == record``
              and stays quiet). That is the "wiring exists but nothing runs"
              failure ms-180 was written to remove, reproduced inside its own fix.
  P2 (medium) ``HOOK_MANIFEST`` never registered ``UserPromptSubmit``, so the
              turn epoch never advanced and 受入条件4's per-tool grain never held
              in a shipped install — while ``tool_turn``'s docstring claimed the
              registration existed.
  P3 (medium) The matcher was ``mcp__`` only, so a Skill / built-in / CLI verb
              registered WITH a record sink silently recorded nothing.
  P4 (medium) The hook discarded the writer's output and exit code, so "decided
              not to write" and "could not write" were the same silence.
  P6 (low)    Both interrupts wrote the debt file for the same call, so the count
              was double the real number of uses.

The through-line: the INSTALL is part of "the interrupt". A feature that needs a
new event, a wider matcher, or a second entry point is not wired until the
manifest and both legs say so — however confidently the comment beside the code
says otherwise. These tests pin that structurally, because a comment asserting a
property is what was there before and it was false.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import commands            # noqa: E402
import tool_ledger as tl   # noqa: E402
import tool_turn as tt     # noqa: E402
import tool_usage_debt as tdebt  # noqa: E402

BASH_LEG = REPO / "bin" / "beacon-save-hook.sh"
BASH_HOOK = REPO / "bin" / "beacon-tool-record-hook.py"
PY_LEG = REPO / "beacon_cli" / "hooks" / "save_hook.py"
GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"
BEACON = REPO / "bin" / "beacon"


def _executable_code(path: Path) -> str:
    """The file's code with comments AND docstrings removed.

    Both legs legitimately DESCRIBE the Google-Drive table they replaced, so a
    raw grep would fail on the documentation while saying nothing about the logic.
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return "\n".join(l for l in source.splitlines()
                         if not l.lstrip().startswith("#"))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "entries": []}], "documents": []},
        ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _declare(root: Path, *rows: dict) -> None:
    led = tl.empty_ledger()
    for row in rows:
        tl.upsert(led, row)
    tl.save_ledger(tl.ledger_path(root), led)


def _payload(root: Path, tool_name: str, tool_input=None,
             event="PostToolUse", session="s") -> str:
    return json.dumps({"hook_event_name": event, "session_id": session,
                       "cwd": str(root), "tool_name": tool_name,
                       "tool_input": tool_input or {}})


def _fire_bash(root: Path, body: str) -> int:
    return subprocess.run([str(BASH_LEG)], input=body, capture_output=True,
                          text=True, timeout=60, cwd=str(root)).returncode


def _fire_python(root: Path, body: str, cwd: Path = None) -> int:
    """Drive the console-script leg exactly as the installer would."""
    import os
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", "beacon_cli.hooks.save_hook"],
                          input=body, capture_output=True, text=True,
                          timeout=60, cwd=str(cwd or root), env=env).returncode


def _entries(root: Path) -> list:
    return json.loads((root / ".beacon" / "project.json").read_text(
        encoding="utf-8"))["milestones"][0]["entries"]


# ---------------------------------------------------------------------------
# P1 — BOTH legs consult the ledger. The Python one is the preferred install.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("leg", ["bash", "python"])
def test_both_hook_legs_record_through_the_ledger(project, leg):
    _declare(project, tl.build_record(
        "mcp__gmail__send_email", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    body = _payload(project, "mcp__gmail__send_email", {"subject": "ご提案"})
    fire = _fire_bash if leg == "bash" else _fire_python
    assert fire(project, body) == 0
    entries = _entries(project)
    assert len(entries) == 1, f"{leg} leg recorded nothing"
    assert entries[0]["meta"]["tool"] == "mcp__gmail__send_email"


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_both_legs_respect_a_declined_sink(project, leg):
    _declare(project, tl.build_record("Grep", purpose="下調べ",
                                      effect=tl.EFFECT_READ_ONLY,
                                      decline_reason="証跡ではない"))
    fire = _fire_bash if leg == "bash" else _fire_python
    fire(project, _payload(project, "Grep", {"pattern": "x"}))
    assert _entries(project) == []


@pytest.mark.parametrize("leg", [PY_LEG, BASH_HOOK, BASH_LEG])
def test_no_leg_names_a_tool_or_a_server_in_its_code(leg):
    """The policy must live in the ledger, not in either leg. P1 was exactly a
    leg that still carried its own table."""
    code = _executable_code(leg)
    assert "mcp__" not in code, f"{leg.name} names an MCP tool"
    assert "google" not in code.lower(), f"{leg.name} names a server"
    assert "_MONITORED_SOURCES" not in code


def test_both_legs_delegate_to_one_shared_decision():
    """One definition, two wrappers — not two implementations kept in step."""
    for leg in (PY_LEG, BASH_HOOK):
        code = _executable_code(leg)
        assert "tool_record_hook" in code, (
            f"{leg.name} does not route through lib/tool_record_hook")


def test_the_python_leg_walks_up_to_find_the_project(project):
    """The old version checked only ``./.beacon/project.json``, so it silently
    did nothing whenever the harness cwd was a subdirectory."""
    deep = project / "a" / "b" / "c"
    deep.mkdir(parents=True)
    _declare(project, tl.build_record(
        "mcp__gmail__send_email", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    assert _fire_python(project,
                        _payload(project, "mcp__gmail__send_email",
                                 {"subject": "deep"}), cwd=deep) == 0
    assert len(_entries(project)) == 1


def test_the_python_leg_resolves_lib_from_the_wheel_layout_first():
    """``lib/`` is remapped to ``beacon_cli/_bundled_lib`` in the wheel. Resolving
    only the source layout is how this leg ended up effectively unreachable on
    the install path the resolver prefers."""
    code = _executable_code(PY_LEG)
    assert "_bundled_lib" in code
    first = code.index("_bundled_lib")
    src_layout = code.index('parent.parent / \'lib\'') if \
        "parent.parent / 'lib'" in code else len(code)
    assert first < src_layout, "_bundled_lib must be tried before the source layout"


# ---------------------------------------------------------------------------
# P2 / P3 — the INSTALL is part of the interrupt.
# ---------------------------------------------------------------------------

def _save_entries():
    return [h for h in commands.HOOK_MANIFEST
            if "beacon-save-hook.sh" == h.get("resolver")]


def test_the_record_hook_is_installed_for_the_turn_boundary():
    """受入条件4 needs ``UserPromptSubmit``; without it the epoch never advances
    and the per-tool grain never holds in a shipped install."""
    events = {e for h in _save_entries() for e in h["events"]}
    assert "UserPromptSubmit" in events
    assert "PostToolUse" in events


def test_the_turn_boundary_entry_takes_no_matcher():
    """``UserPromptSubmit`` carries no tool, so a tool matcher there would never
    fire."""
    for h in _save_entries():
        if h["events"] == ["UserPromptSubmit"]:
            assert h.get("matcher") is None


def test_the_record_hook_matcher_is_not_restricted_to_mcp():
    """The ledger accepts any 道具 and ``tool add`` does not restrict the name's
    shape, so an ``mcp__``-only matcher meant a registered Skill recorded nothing
    AND raised no debt — "listed but silently not recorded", worse than
    unregistered. Which calls matter is the ledger's decision (方針3)."""
    for h in _save_entries():
        if "PostToolUse" in h["events"]:
            assert h.get("matcher") == "*", h


def test_tool_turns_docstring_no_longer_claims_an_unwired_registration():
    """The docstring asserted the registration existed while the manifest carried
    PostToolUse only. A comment claiming a property is not evidence of it."""
    doc = (LIB / "tool_turn.py").read_text(encoding="utf-8")
    assert "HOOK_MANIFEST" in doc, (
        "the docstring should point at the manifest entry that makes its claim true")


# ---------------------------------------------------------------------------
# P4 — "could not write" is its own visible outcome.
# ---------------------------------------------------------------------------

def test_a_failed_write_is_recorded_as_its_own_debt_reason(tmp_path):
    """The ledger said record and nothing was written. Collapsing that into
    silence hides the one case where the ledger's own promise broke."""
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [], "documents": []}), encoding="utf-8")  # no worked target
    _declare(tmp_path, tl.build_record(
        "mcp__x__send_thing", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    assert _fire_bash(tmp_path,
                      _payload(tmp_path, "mcp__x__send_thing", {})) == 0
    rows = tdebt.load_debt(tdebt.debt_path(str(tmp_path / ".beacon")))
    assert [r["reason"] for r in rows] == [tdebt.REASON_WRITE_FAILED]
    assert rows[0].get("detail")


def test_tool_used_exits_nonzero_when_the_writer_could_not_record(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [], "documents": []}), encoding="utf-8")
    _declare(tmp_path, tl.build_record(
        "mcp__x__send_thing", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    r = subprocess.run([str(BEACON), "tool", "used", "mcp__x__send_thing",
                        "--summary", "s"], cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0


def test_the_json_body_and_exit_code_agree_on_a_failed_write(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [], "documents": []}), encoding="utf-8")
    _declare(tmp_path, tl.build_record(
        "mcp__x__send_thing", purpose="送る", effect=tl.EFFECT_OUTWARD,
        recorder=tl.RECORDER_EVIDENCE))
    r = subprocess.run([str(BEACON), "tool", "used", "mcp__x__send_thing",
                        "--summary", "s", "--json"], cwd=str(tmp_path),
                       capture_output=True, text=True, timeout=60)
    assert json.loads(r.stdout)["recorded"] is False
    assert r.returncode != 0


def test_the_three_debt_reasons_stay_separate(tmp_path):
    """Each needs a different action: register it / close the question / find out
    what broke. One "no record" list would hide which."""
    path = tdebt.debt_path(str(tmp_path))
    for name, reason in (("a", tdebt.REASON_UNREGISTERED),
                         ("b", tdebt.REASON_UNDECIDED),
                         ("c", tdebt.REASON_WRITE_FAILED)):
        tdebt.record_no_record_use(path, name, reason=reason, at="T")
    rows = tdebt.load_debt(path)
    for reason in tdebt.REASONS:
        assert len(tdebt.debt_by_reason(rows, reason)) == 1, reason


# ---------------------------------------------------------------------------
# P6 — the debt file has exactly ONE writer.
# ---------------------------------------------------------------------------

def test_one_use_counts_once_even_though_both_interrupts_see_it(project):
    _declare(project)
    body_pre = _payload(project, "mcp__never__declared", {}, event="PreToolUse")
    subprocess.run([sys.executable, str(GATE)], input=body_pre,
                   capture_output=True, text=True, timeout=60)
    _fire_bash(project, _payload(project, "mcp__never__declared", {}))
    rows = tdebt.load_debt(tdebt.debt_path(str(project / ".beacon")))
    assert len(rows) == 1
    assert rows[0]["count"] == 1, (
        "both interrupts wrote the debt, so the count is not an observed fact")


def test_the_gate_does_not_write_the_debt_file():
    """Structural: the recording interrupt owns the record-shaped fact."""
    code = _executable_code(GATE)
    assert "record_no_record_use" not in code
    assert "record_unregistered" not in code


def test_the_gate_still_consults_the_ledger_for_the_effect_question():
    """Over-reach check: removing the debt write must not remove the gate's own
    reason to read the ledger."""
    code = _executable_code(GATE)
    assert "gate_classification" in code


# ---------------------------------------------------------------------------
# Findings from the independent MAINTAINABILITY review of PR #805.
#
#   M1 (high)   beacon_cli/hooks/save_hook.py hand-rolled its OWN walk-up
#               root-finder — a fourth copy of a rule bin/hook_bootstrap was
#               created to de-duplicate (its docstring records the drift that
#               prompted it). Editing the marker rule in one place and missing
#               the other reproduces, one layer down, the very "one leg updated,
#               the other stale" defect this module was written to fix.
#   M2 (high)   The debt display hand-named three reasons instead of iterating
#               the vocabulary, so a 4th reason produced a row in NO bucket —
#               text or JSON — while the header still counted it.
#   M4 (medium) handle()'s fail-safe plus each leg's fail-safe swallowed a real
#               bug twice: renaming one function gave 33 bare "assert 0 == 1"
#               failures with no traceback anywhere.
#
# M3 (the census guard that could not fail for lib/cmd_tool.py) is pinned in
# tests/test_tool_recorder_dispatch_ms180.py next to the guard itself.
# ---------------------------------------------------------------------------

def test_the_shared_module_owns_root_finding():
    import tool_record_hook
    assert hasattr(tool_record_hook, "find_root")


@pytest.mark.parametrize("leg", [
    REPO / "bin" / "beacon-tool-record-hook.py",
    REPO / "beacon_cli" / "hooks" / "save_hook.py",
])
def test_neither_leg_hand_rolls_its_own_root_finder(leg):
    code = _executable_code(leg)
    assert "find_root" in code, f"{leg.name} must use the shared find_root"
    assert ".beacon" not in code or "project.json" not in code, (
        f"{leg.name} appears to re-implement the marker rule")


def test_both_root_finders_agree(tmp_path):
    """Two copies of the rule remain by construction — hook_bootstrap cannot
    depend on lib/ (it is the thing that FINDS lib). So they are pinned against
    each other instead, which is what the previous fourth copy lacked."""
    import importlib.util
    import tool_record_hook
    spec = importlib.util.spec_from_file_location(
        "_hb", REPO / "bin" / "hook_bootstrap.py")
    hb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hb)

    root = tmp_path / "proj"
    (root / ".beacon").mkdir(parents=True)
    (root / ".beacon" / "project.json").write_text("{}", encoding="utf-8")
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    for start in (root, deep, outside, tmp_path):
        mine = tool_record_hook.find_root(start)
        theirs = hb.find_beacon_root(start)
        assert (mine is None) == (theirs is None), start
        if mine is not None:
            assert Path(mine).resolve() == Path(theirs).resolve(), start


def test_the_debt_display_is_exhaustive_over_the_vocabulary():
    """M2: every reason must have a label/action, so adding one to REASONS
    without wiring its presentation fails at the definition site rather than
    producing a silently empty bucket."""
    assert set(tdebt.REASON_DISPLAY) == set(tdebt.REASONS)
    for meta in tdebt.REASON_DISPLAY.values():
        # No `key` any more: that separate machine spelling was itself a thing
        # that could drift, and it did (see
        # tests/test_review_findings_pr805_round2.py). Machine output is keyed
        # by the REASON constants, so only the display text lives here.
        assert meta["label"] and meta["action"]


def test_a_fourth_debt_reason_is_displayed_rather_than_vanishing(project,
                                                                  monkeypatch):
    """The judge's reproduction: a hypothetical 4th reason was accepted by the
    writer, counted in the header, and shown in no bucket at all."""
    monkeypatch.setattr(tdebt, "REASONS",
                        tuple(tdebt.REASONS) + ("quota-exceeded",))
    monkeypatch.setitem(tdebt.REASON_DISPLAY, "quota-exceeded",
                        {"key": "quota_exceeded", "label": "上限超過",
                         "action": "上限を上げる"})
    path = tdebt.debt_path(str(project / ".beacon"))
    tdebt.record_no_record_use(path, "mcp__q__tool", reason="quota-exceeded",
                               at="T1")
    rows = tdebt.load_debt(path)
    assert [r["reason"] for r in rows] == ["quota-exceeded"]
    buckets = {r: tdebt.debt_by_reason(rows, r) for r in tdebt.REASONS}
    shown = sum(len(v) for v in buckets.values())
    assert shown == len(rows), (
        "a row counted in the header must appear in exactly one bucket")


def test_the_hook_debug_hatch_records_a_swallowed_bug(project, monkeypatch):
    """M4: the production path stays fail-safe, but a bug inside it must be
    findable instead of indistinguishable from a correct no-op."""
    import tool_record_hook
    monkeypatch.setenv("BEACON_HOOK_DEBUG", "1")
    monkeypatch.setattr(tool_record_hook._tl, "load_ledger",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    rc = tool_record_hook.handle(
        {"hook_event_name": "PostToolUse", "session_id": "s",
         "tool_name": "mcp__x__y", "tool_input": {}}, project)
    assert rc == 0, "the fail-safe contract must hold"
    log = project / ".beacon" / "hook-debug.log"
    assert log.exists(), "the swallowed exception left no trace"
    assert "boom" in log.read_text(encoding="utf-8")


def test_the_debug_hatch_is_silent_when_not_enabled(project, monkeypatch):
    """Off by default: the log must not appear in normal operation."""
    import tool_record_hook
    monkeypatch.delenv("BEACON_HOOK_DEBUG", raising=False)
    monkeypatch.setattr(tool_record_hook._tl, "load_ledger",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("boom")))
    assert tool_record_hook.handle(
        {"hook_event_name": "PostToolUse", "session_id": "s",
         "tool_name": "mcp__x__y", "tool_input": {}}, project) == 0
    assert not (project / ".beacon" / "hook-debug.log").exists()


def test_the_known_old_shape_hook_is_named():
    """M5: two idioms now coexist for two-leg hooks. The old one is named so the
    next person choosing between them knows the intended direction."""
    doc = (LIB / "tool_record_hook.py").read_text(encoding="utf-8")
    assert "post_commit" in doc
