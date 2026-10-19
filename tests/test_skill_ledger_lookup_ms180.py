"""A Skill reaches the ledger under its own name (ms-180 follow-up).

The harness names a Skill call ``Skill`` and carries the skill in
``tool_input``. Both tool-call interrupts looked the ledger up by the harness's
name, so:

  * a Skill registered WITH a record sink left **no record** and was debited as
    "`Skill` is not in the ledger" — "listed, and then silently not recorded",
    which this milestone exists to remove, in the very class of 道具 its
    background names first (``~/.claude/skills``, ``/review``);
  * registering the literal name ``Skill`` recorded "Skill を使用", losing which
    skill it was;
  * a Skill's declared effect never reached the PreToolUse gate.

An independent attainment judge found this and it was the one counterexample
holding the milestone at not-attained. These tests fail if the resolution is
removed from either interrupt.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))

import tool_effect as te      # noqa: E402
import tool_ledger as tl      # noqa: E402
import tool_usage_debt as debt  # noqa: E402

GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"
BASH_LEG = REPO / "bin" / "beacon-save-hook.sh"


# ---------------------------------------------------------------------------
# The resolver — one definition, reusing the parser the heuristic already had.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name, tool_input, expected", [
    ("Skill", {"skill": "review"}, "review"),
    ("Skill", {"name": "review"}, "review"),
    ("Skill", {"command": "/beacon-log abc"}, "beacon-log"),
    ("Skill", {"command": "beacon-log"}, "beacon-log"),
    # Unreadable payload: the bare harness name, so the use is debited under a
    # name a human can see rather than dropped for having no name.
    ("Skill", {}, "Skill"),
    ("Skill", None, "Skill"),
    ("Skill", {"skill": "   "}, "Skill"),
    # Every other tool is unaffected.
    ("mcp__gmail__send_email", {"to": "x"}, "mcp__gmail__send_email"),
    ("Bash", {"command": "ls"}, "Bash"),
    ("", {}, ""),
    (None, {}, ""),
])
def test_ledger_name_resolves_the_name_the_ledger_is_keyed_by(
        tool_name, tool_input, expected):
    assert te.ledger_name(tool_name, tool_input) == expected


def test_the_resolution_has_one_parser():
    """A second parser could disagree with this one about the same payload.

    The check is `scripts/check-single-skill-parser.py`, which reads the syntax
    tree. An earlier version of this test compared source text against
    ``'get("skill")'``; an independent maintainability review planted
    ``tool_input.get('skill')`` — the same read with single quotes — and it
    stayed green. The quote style must not decide whether a guard fires, so the
    comparison moved to the parsed form; `test_single_skill_parser_guard.py`
    plants each spelling and requires the guard to catch it.
    """
    source = (LIB / "tool_effect.py").read_text(encoding="utf-8")
    assert source.count("def _skill_name(") == 1
    assert "_skill_name(tool_input)" in source
    proc = subprocess.run([sys.executable,
                           str(REPO / "scripts" / "check-single-skill-parser.py")],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (
        f"a second place reads which Skill a call names:\n"
        f"{proc.stdout}\n{proc.stderr}")


# ---------------------------------------------------------------------------
# The recording interrupt.
# ---------------------------------------------------------------------------

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


def _payload(root: Path, tool_name: str, tool_input=None, session="s") -> str:
    return json.dumps({"hook_event_name": "PostToolUse", "session_id": session,
                       "cwd": str(root), "tool_name": tool_name,
                       "tool_input": tool_input or {}})


def _fire(root: Path, body: str, leg: str) -> int:
    if leg == "bash":
        return subprocess.run([str(BASH_LEG)], input=body, capture_output=True,
                              text=True, timeout=60, cwd=str(root)).returncode
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", "beacon_cli.hooks.save_hook"],
                          input=body, capture_output=True, text=True,
                          timeout=60, cwd=str(root), env=env).returncode


def _entries(root: Path) -> list:
    return json.loads((root / ".beacon" / "project.json").read_text(
        encoding="utf-8"))["milestones"][0]["entries"]


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_a_declared_skill_leaves_a_record_under_its_own_name(project, leg):
    """The defect itself: this recorded nothing at all before."""
    _declare(project, tl.build_record("review", purpose="PR をレビューする",
                                      effect=tl.EFFECT_STATE_CHANGE,
                                      recorder=tl.RECORDER_EVIDENCE))
    assert _fire(project, _payload(project, "Skill", {"skill": "review"}),
                 leg) == 0
    entries = _entries(project)
    assert len(entries) == 1, (
        f"{leg} leg recorded nothing for a Skill that declares a record sink — "
        f"'listed, and then silently not recorded'")
    assert entries[0]["meta"]["tool"] == "review", (
        f"the record names {entries[0]['meta']['tool']!r}, so which skill ran "
        f"is lost")


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_a_skill_that_declined_recording_records_nothing(project, leg):
    """The declared answer must still be honoured through the resolution."""
    _declare(project, tl.build_record("beacon-note", purpose="メモ",
                                      effect=tl.EFFECT_STATE_CHANGE,
                                      decline_reason="揮発メモは証跡ではない"))
    assert _fire(project, _payload(project, "Skill", {"skill": "beacon-note"}),
                 leg) == 0
    assert _entries(project) == []
    assert debt.load_debt(debt.debt_path(str(project / ".beacon"))) == [], (
        "a declined sink is an answer, not a debt")


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_an_unregistered_skill_is_debited_under_the_skill_name(project, leg):
    """The debt must name the skill, not the wrapper — otherwise every
    unregistered skill collapses into one row called `Skill`."""
    _declare(project)
    assert _fire(project, _payload(project, "Skill", {"skill": "some-skill"}),
                 leg) == 0
    names = [r.get("name") for r in
             debt.load_debt(debt.debt_path(str(project / ".beacon")))]
    assert names == ["some-skill"], names


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_an_unreadable_skill_payload_is_still_visible_as_debt(project, leg):
    """Fail toward debt: a call we cannot name is debited under `Skill`, not
    dropped."""
    _declare(project)
    assert _fire(project, _payload(project, "Skill", {}), leg) == 0
    names = [r.get("name") for r in
             debt.load_debt(debt.debt_path(str(project / ".beacon")))]
    assert names == ["Skill"], names


@pytest.mark.parametrize("leg", ["bash", "python"])
def test_per_tool_grain_applies_to_the_resolved_skill(project, leg):
    """`turn` grain is a property of the 道具, so it must fold the SKILL's calls
    — keying on `Skill` would fold every skill together."""
    _declare(project,
             tl.build_record("review", purpose="p",
                             effect=tl.EFFECT_STATE_CHANGE,
                             recorder=tl.RECORDER_EVIDENCE, grain=tl.GRAIN_TURN),
             tl.build_record("beacon-note", purpose="p",
                             effect=tl.EFFECT_STATE_CHANGE,
                             recorder=tl.RECORDER_EVIDENCE, grain=tl.GRAIN_TURN))
    begin = json.dumps({"hook_event_name": "UserPromptSubmit",
                        "session_id": "sT", "cwd": str(project)})
    assert _fire(project, begin, leg) == 0
    for _ in range(3):
        _fire(project, _payload(project, "Skill", {"skill": "review"},
                                session="sT"), leg)
    _fire(project, _payload(project, "Skill", {"skill": "beacon-note"},
                            session="sT"), leg)
    tools = [e["meta"]["tool"] for e in _entries(project)]
    assert sorted(tools) == ["beacon-note", "review"], (
        f"expected one fold per skill within the turn, got {tools}")


# ---------------------------------------------------------------------------
# The gate. A declaration may TIGHTEN but never SOFTEN — now on the skill's own
# name, which is the name that carries the verb evidence.
# ---------------------------------------------------------------------------

def _arm(root: Path, session: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "_ut_for_skill_test", LIB / "untrusted_turn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.arm(str(root), session, sources=["dm:x"])


def _gate(root: Path, tool_name: str, tool_input, session="sG") -> str:
    body = json.dumps({"hook_event_name": "PreToolUse", "session_id": session,
                       "cwd": str(root), "tool_name": tool_name,
                       "tool_input": tool_input})
    proc = subprocess.run([sys.executable, str(GATE)], input=body,
                          capture_output=True, text=True, timeout=60,
                          cwd=str(root))
    out = (proc.stdout or "").strip()
    if not out:
        return "not-gated"
    return (json.loads(out).get("hookSpecificOutput", {})
            .get("permissionDecision") or "unknown")


@pytest.fixture
def armed(project):
    _arm(project, "sG")
    return project


def _seed_raw(root: Path, *rows: dict) -> None:
    """Write rows straight to the ledger file, bypassing the CLI's refusals —
    the gate must hold even against a hand-edited ledger."""
    path = tl.ledger_path(root)
    led = tl.load_ledger(path)
    led["tools"] = list(rows)
    tl.save_ledger(path, led)


def test_a_skill_declared_read_only_cannot_soften_a_write_verb_name(armed):
    """`beacon-dm-send` carries `send`: positive evidence of effect, which a
    declaration must not override. This protection was unreachable for Skills
    before, because the lookup used the name `Skill`.

    NOTE this test PINS behaviour rather than detecting the regression: without
    the resolution the heuristic on the skill name also answers `ask`, so it
    stays green either way. Its job is to fail if a later change lets a
    declaration soften a write-verb name — which is the thing SPEC 受入条件6
    (ms-169's gate does not loosen) forbids. The inversion detectors for the
    lookup itself are the recording tests above and the verb-less case below.
    """
    _seed_raw(armed, {"name": "beacon-dm-send", "purpose": "p",
                      "effect": "read-only", "record": {"sink": "undecided"}})
    assert _gate(armed, "Skill", {"skill": "beacon-dm-send"}) == "ask"


def test_a_skill_declared_state_change_is_gated(armed):
    """Tightening is honoured through the resolution. Also a pin rather than an
    inversion detector: the heuristic answers `ask` for this name too."""
    _seed_raw(armed, {"name": "frobnicate-other", "purpose": "p",
                      "effect": "state-change", "record": {"sink": "undecided"}})
    assert _gate(armed, "Skill", {"skill": "frobnicate-other"}) == "ask"


def test_an_unregistered_skill_stays_fail_closed(armed):
    _seed_raw(armed)
    assert _gate(armed, "Skill", {"skill": "unregistered-frob"}) == "ask"


def test_a_skill_with_no_recognisable_verb_can_be_declared_read_only(armed):
    """DISCLOSED CHANGE IN BEHAVIOUR. A name with no recognisable verb is the
    case the ledger exists for: the heuristic can only default-deny it, so the
    declaration resolves an unknown rather than contradicting evidence. Every
    other declared tool already got this; a Skill did not, only because the
    lookup used the wrong name. Closing the lookup therefore also lets a
    declared, verb-less Skill through where it used to be gated — the designed
    rule, applied to a class that was accidentally excluded from it.
    """
    _seed_raw(armed, {"name": "frobnicate-thing", "purpose": "p",
                      "effect": "read-only", "record": {"sink": "undecided"}})
    assert _gate(armed, "Skill", {"skill": "frobnicate-thing"}) == "not-gated"


@pytest.mark.parametrize("tool_name, tool_input, expected", [
    ("Bash", {"command": "ls"}, "ask"),
    ("Write", {"file_path": "x"}, "ask"),
    ("Read", {"file_path": "x"}, "not-gated"),
    ("mcp__gmail__send_email", {"to": "x"}, "ask"),
])
def test_non_skill_gating_is_unchanged(armed, tool_name, tool_input, expected):
    """Only the LOOKUP moved; `is_side_effect` still receives the harness's own
    name, so nothing outside the Skill class may shift."""
    _seed_raw(armed)
    assert _gate(armed, tool_name, tool_input) == expected


# ---------------------------------------------------------------------------
# A spelling variant of a pinned built-in is not new knowledge (AX review of
# PR #807, high). Resolving Skills to their own names made the free-form skill
# namespace reach the pinned table for the first time, and the table matched
# EXACTLY: `Bash` was refused, `bash` was accepted, and `Skill(bash)` declared
# read-only then passed the ms-169 gate with no prompt at all.
# ---------------------------------------------------------------------------

def _variants(name: str) -> list:
    """Spellings that must all collapse onto the same pinned name."""
    out = {name, name.lower(), name.upper()}
    # NotebookEdit -> notebook-edit / notebook_edit
    spaced = "".join(f" {c}" if c.isupper() else c for c in name).split()
    if len(spaced) > 1:
        out.add("-".join(p.lower() for p in spaced))
        out.add("_".join(p.lower() for p in spaced))
    return sorted(out)


@pytest.mark.parametrize("pinned", sorted(tl.NON_SOFTENABLE_EFFECT))
def test_no_spelling_of_a_pinned_builtin_can_be_declared_read_only(pinned):
    """Every pinned name, every variant — not four examples."""
    for spelling in _variants(pinned):
        assert tl.is_softening(spelling, tl.EFFECT_READ_ONLY) is True, (
            f"{spelling!r} can be declared read-only although it is the same "
            f"name as the pinned {pinned!r}")
        assert tl.pinned_canonical_name(spelling) == pinned, (
            f"{spelling!r} did not resolve to {pinned!r}")


@pytest.mark.parametrize("pinned", sorted(tl.NON_SOFTENABLE_EFFECT))
def test_the_refusal_names_the_pinned_spelling_it_collided_with(pinned):
    """A refusal the author cannot act on is a refusal they will work around."""
    lowered = pinned.lower()
    if lowered == pinned:
        pytest.skip(f"{pinned} has no distinct lower-case spelling")
    with pytest.raises(tl.ToolLedgerError) as err:
        tl.build_record(lowered, purpose="x", effect=tl.EFFECT_READ_ONLY)
    assert pinned in str(err.value), (
        f"the refusal for {lowered!r} does not name {pinned!r}: {err.value}")


@pytest.mark.parametrize("spelling", ["bash", "BASH", "webfetch", "notebook-edit"])
def test_a_skill_named_like_a_pinned_builtin_stays_gated(armed, spelling):
    """End to end through the gate, with the ledger hand-edited so the CLI's
    refusal cannot be what is being measured."""
    _seed_raw(armed, {"name": spelling, "purpose": "p", "effect": "read-only",
                      "record": {"sink": "undecided"}})
    assert _gate(armed, "Skill", {"skill": spelling}) == "ask", (
        f"a skill named {spelling!r} declared read-only passed the gate in an "
        f"armed untrusted turn")


def test_a_stricter_declaration_is_still_allowed_for_a_pinned_name():
    """The guard must only block SOFTENING. Declaring `outward` for Bash is
    stricter than its pinned `state-change` and has to stay possible, or the
    guard has eaten the ledger's purpose."""
    row = tl.build_record("bash", purpose="x", effect=tl.EFFECT_OUTWARD)
    assert row["effect"] == tl.EFFECT_OUTWARD
    assert tl.is_softening("bash", tl.EFFECT_OUTWARD) is False
