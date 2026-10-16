"""ms-180 — findings from the independent MAINTAINABILITY review of PR #802.

All five reproduced mechanically before being accepted. The three pinned here:

  M2 (high)   ``cmd_tool_used`` dispatched on the recorder with a binary
              ``if/else`` over the currently-two values of ``tl.RECORDERS``. The
              judge added a third recorder, which passed the ledger's own
              validation and was then written through the EVIDENCE path while
              reporting ``recorded: true`` and exiting 0. Widening a declared,
              validated vocabulary is an INVITED change here (the ledger's
              docstring says so), and every other such vocabulary in the module
              — EFFECTS / SINKS / GRAINS — is safe to widen because it is
              validated, not dispatched on. Only this one broke, and it broke by
              writing a WRONG record rather than failing.
  M3 (medium) Both hook scripts hardcoded the literal tuple
              ``("unregistered", "undecided")`` to decide "is this action a
              debt" — the exact duplication the same PR avoids for
              ``NON_SOFTENABLE_EFFECT`` (derived, never re-listed).
  M5 (low)    ``return True if _write(...) else True`` — a ternary whose branches
              are identical, two screens from a sibling where the same shape IS
              load-bearing. A reader "simplifying" it to ``return _write(...)``
              would silently invert the documented fail-safe.
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

import cmd_tool          # noqa: E402
import tool_ledger as tl  # noqa: E402
import tool_turn as tt    # noqa: E402

BEACON = REPO / "bin" / "beacon"


# ---------------------------------------------------------------------------
# M2 — the recorder dispatch is exhaustive, not a binary branch.
# ---------------------------------------------------------------------------

def test_every_declared_recorder_has_a_wired_writer():
    """The invariant that makes widening ``tl.RECORDERS`` safe: the vocabulary and
    the dispatch table are the same set, asserted rather than assumed."""
    assert set(cmd_tool._RECORDER_WRITERS) == set(tl.RECORDERS)


def test_adding_a_recorder_without_a_writer_fails_loudly(tmp_path, monkeypatch):
    """The judge's reproduction: a third recorder passed validation and was
    silently written through the evidence path. It must now stop at the point
    that decides correctness."""
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "entries": []}], "documents": []}),
        encoding="utf-8")
    monkeypatch.setattr(tl, "RECORDERS",
                        tuple(tl.RECORDERS) + ("webhook",))
    row = tl.build_record("mcp__webhook__ping", purpose="p",
                          effect=tl.EFFECT_STATE_CHANGE, recorder="webhook")
    tl.save_ledger(str(tmp_path / ".beacon" / "tools.json"),
                   tl.upsert(tl.empty_ledger(), row))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    monkeypatch.setenv("BEACON_TOOL_NAME", "mcp__webhook__ping")
    monkeypatch.setenv("BEACON_TOOL_SUMMARY", "pinged")
    with pytest.raises(SystemExit) as exc:
        cmd_tool.cmd_tool_used()
    assert exc.value.code != 0
    entries = json.loads(
        (tmp_path / ".beacon" / "project.json").read_text(encoding="utf-8")
    )["milestones"][0]["entries"]
    assert entries == [], "an unwired recorder must not write through some other path"


def test_the_error_names_the_table_to_update():
    """A loud failure that does not say what to do is only half a fix."""
    source = (LIB / "cmd_tool.py").read_text(encoding="utf-8")
    assert "_RECORDER_WRITERS" in source
    idx = source.index("writer が配線されていません")
    window = source[idx - 200:idx + 400]
    assert "_RECORDER_WRITERS" in window
    assert "RECORDERS" in window


def test_no_binary_recorder_branch_remains():
    """Structural, not behavioural: the defect was the SHAPE (an ``else`` that
    swallows every unknown value), so pin that the shape is gone."""
    tree = ast.parse((LIB / "cmd_tool.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_tool_used")
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            src = ast.unparse(node)
            assert "RECORDER_SAVE" not in src, (
                "cmd_tool_used compares against a specific recorder again; use "
                "the _RECORDER_WRITERS registry so an unwired value cannot fall "
                "into an else branch")


@pytest.mark.parametrize("recorder", sorted(tl.RECORDERS))
def test_each_wired_writer_shares_one_signature(recorder):
    """The registry calls every writer identically; a writer needing different
    arguments would reintroduce the per-recorder branch the registry removed."""
    import inspect
    params = list(inspect.signature(
        cmd_tool._RECORDER_WRITERS[recorder]).parameters)
    assert params == ["name", "text", "effect", "target", "url"], params


# ---------------------------------------------------------------------------
# M3 — one definition of "which actions are debts", consumed by both hooks.
# ---------------------------------------------------------------------------

def test_the_debt_action_set_is_defined_once():
    assert tl.DEBT_ACTIONS == frozenset({"unregistered", "undecided"})
    assert tl.is_debt_action("unregistered") is True
    assert tl.is_debt_action("undecided") is True
    assert tl.is_debt_action("skip") is False      # declined = an answer
    assert tl.is_debt_action("record") is False
    assert tl.is_debt_action("") is False
    assert tl.is_debt_action(None) is False


#: Every file that decides "is this action a debt". After the philosophy review
#: of PR #802 that is ONE place — the shared hook module — because the debt file
#: has one writer and the hook decision has one definition with two thin entry
#: points. If a second file ever asks this question, add it here so the
#: no-hardcoded-literal rule covers it too.
_DEBT_DECIDING_FILES = [LIB / "tool_record_hook.py"]


@pytest.mark.parametrize("path", _DEBT_DECIDING_FILES, ids=lambda p: p.name)
def test_the_debt_decision_hardcodes_no_literals(path):
    """With the tuple copied into a file, a fifth outcome could be added to the
    vocabulary and silently forgotten here — the duplication this PR already
    refuses for the pinned-effect set."""
    source = path.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert '"unregistered", "undecided"' not in code
    assert '"undecided", "unregistered"' not in code
    assert "is_debt_action" in code


def test_no_other_file_decides_debt_membership_by_hand():
    """Guard the list above: if some file starts testing the action against the
    literals itself, this fails rather than letting the census go stale."""
    import re
    pattern = re.compile(r'["\']unregistered["\']\s*,\s*["\']undecided["\']'
                         r'|["\']undecided["\']\s*,\s*["\']unregistered["\']')
    offenders = []
    # The census used to be ``LIB.glob("tool_*.py")``, which does NOT match
    # lib/cmd_tool.py — the CLI display surface, i.e. the file most likely to
    # need this very logic. An independent maintainability review of PR #805
    # planted the forbidden literal there and this test PASSED. A guard that
    # cannot fail for the likeliest regression site is worse than no guard,
    # because green CI then reads as proof. Cover every module both interrupts
    # and the CLI can reach.
    for path in (list((REPO / "bin").glob("*.py")) + list(LIB.glob("*.py"))
                 + list((REPO / "beacon_cli").rglob("*.py"))):
        if path.name == "tool_ledger.py":
            continue  # the definition itself
        code = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                          if not l.lstrip().startswith("#"))
        if pattern.search(code):
            offenders.append(path.name)
    assert offenders == [], offenders


@pytest.mark.parametrize("victim", ["lib/cmd_tool.py",
                                    "beacon_cli/hooks/save_hook.py"])
def test_the_census_actually_catches_a_planted_literal(victim):
    """Test-the-test. The previous census silently excluded lib/cmd_tool.py, so
    it reported OK with the forbidden literal sitting in it. Plant the literal in
    each at-risk file and require the census to find it — a guard is only worth
    its green when it can go red."""
    import re
    path = REPO / victim
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(
            original + '\n\ndef _planted(action):\n'
                       '    return action in ("unregistered", "undecided")\n',
            encoding="utf-8")
        pattern = re.compile(r'["\']unregistered["\']\s*,\s*["\']undecided["\']'
                             r'|["\']undecided["\']\s*,\s*["\']unregistered["\']')
        found = []
        for p in (list((REPO / "bin").glob("*.py")) + list(LIB.glob("*.py"))
                  + list((REPO / "beacon_cli").rglob("*.py"))):
            if p.name == "tool_ledger.py":
                continue
            code = "\n".join(
                l for l in p.read_text(encoding="utf-8").splitlines()
                if not l.lstrip().startswith("#"))
            if pattern.search(code):
                found.append(p.name)
        assert path.name in found, (
            f"the census does not reach {victim}; it would pass with the "
            f"forbidden literal planted there")
    finally:
        path.write_text(original, encoding="utf-8")


def test_the_hook_entry_points_delegate_rather_than_decide():
    """Both legs are wrappers. The debt decision (and every other) lives once in
    lib/tool_record_hook — which is the fix for the defect where one leg was
    rewritten and the other was left on the old behaviour."""
    for leg in (REPO / "bin" / "beacon-tool-record-hook.py",
                REPO / "beacon_cli" / "hooks" / "save_hook.py"):
        source = leg.read_text(encoding="utf-8")
        assert "tool_record_hook" in source, leg.name
        assert "is_debt_action" not in source, (
            f"{leg.name} decides debt membership itself; it should delegate")


def test_every_record_plan_action_is_either_a_debt_an_answer_or_a_record():
    """Completeness: if ``record_plan`` grows an action, it must be classified —
    this is what would go red when the fifth outcome appears."""
    known = {"record", "skip"} | set(tl.DEBT_ACTIONS)
    led = tl.empty_ledger()
    tl.upsert(led, tl.build_record("a", purpose="p",
                                   effect=tl.EFFECT_READ_ONLY,
                                   recorder=tl.RECORDER_SAVE))
    tl.upsert(led, tl.build_record("b", purpose="p",
                                   effect=tl.EFFECT_READ_ONLY,
                                   decline_reason="理由"))
    tl.upsert(led, tl.build_record("c", purpose="p",
                                   effect=tl.EFFECT_READ_ONLY))
    for name in ("a", "b", "c", "never-registered"):
        assert tl.record_plan(led, name)["action"] in known


# ---------------------------------------------------------------------------
# M5 — claim_fold's return is unconditional, and the shape says so.
# ---------------------------------------------------------------------------

def test_claim_fold_returns_true_even_when_the_write_fails(tmp_path, monkeypatch):
    """The documented fail-safe: a failed write must not read as "already folded",
    which would make the caller skip recording — the opposite of this module's
    stated direction (duplicates over silence)."""
    path = str(tmp_path / "turn.json")
    tt.begin_turn(path, "s1", at="T1")
    monkeypatch.setattr(tt, "_write", lambda *a, **k: False)
    assert tt.claim_fold(path, "s1", "toolA") is True


def test_claim_fold_has_no_no_op_ternary():
    """Structural: the defect was a ternary whose branches are identical, which
    reads as conditional next to ``begin_turn``'s genuinely conditional one."""
    tree = ast.parse((LIB / "tool_turn.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "claim_fold")
    for node in ast.walk(fn):
        if isinstance(node, ast.IfExp):
            body, orelse = ast.unparse(node.body), ast.unparse(node.orelse)
            assert body != orelse, (
                f"no-op ternary in claim_fold: both branches are {body}")


def test_begin_turns_ternary_is_still_load_bearing():
    """Guard the guard: the sibling's ternary SHOULD differ between branches. If
    this ever matches too, the test above has stopped meaning anything."""
    tree = ast.parse((LIB / "tool_turn.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "begin_turn")
    ternaries = [n for n in ast.walk(fn) if isinstance(n, ast.IfExp)]
    assert ternaries, "begin_turn's conditional return disappeared"
    assert any(ast.unparse(n.body) != ast.unparse(n.orelse) for n in ternaries)


def test_folding_still_works_end_to_end(tmp_path):
    """Behaviour, not just shape: the fold itself must be unchanged."""
    path = str(tmp_path / "turn.json")
    tt.begin_turn(path, "s1", at="T1")
    assert tt.claim_fold(path, "s1", "toolA") is True    # first call this turn
    assert tt.claim_fold(path, "s1", "toolA") is False   # folded
    tt.begin_turn(path, "s1", at="T2")
    assert tt.claim_fold(path, "s1", "toolA") is True    # new turn
