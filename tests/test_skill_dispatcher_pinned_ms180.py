"""The dispatcher name itself cannot be declared read-only (ms-180 follow-up).

`Task` and `Agent` are pinned because they start work whose effects the
classifier cannot see. `Skill` is the same class and was missed, so
``beacon tool add Skill --effect read-only`` was accepted — and with that one
row, every Skill call whose payload the resolver cannot read (``{}``,
``{"skill": ""}``, a renamed key) passed an armed untrusted turn with no prompt,
because the declaration answered for the WRAPPER instead of for a tool.

An independent attainment review of ms-180 found this. Before the ledger keyed
Skills by their own name, such a row disarmed EVERY Skill call, so this is the
remainder of an older hole rather than a new one — but SPEC 受入条件6
(「ms-169 の承認ゲートが緩まない」) promises it is closed, so it is closed here.

The pin must not change what the classifier ANSWERS for a Skill call: the
``name == "Skill"`` branch resolves the skill and decides before the pinned set
is consulted. That equivalence is asserted below against the pre-change module,
rather than argued from reading the order of two `if`s.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
sys.path.insert(0, str(LIB))

import tool_effect as te      # noqa: E402
import tool_ledger as tl      # noqa: E402

GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"

#: Payload shapes the resolver cannot read a skill name out of. Each one used to
#: pass the gate when a `Skill` row declared read-only.
UNREADABLE = [
    {},
    {"skill": ""},
    {"skill": "   "},
    {"skill": None},
    {"skill_name": "beacon-dm-send"},
    {"skill": None, "command": ""},
]


def test_the_dispatcher_is_pinned_like_the_other_dispatchers():
    """Same class, same treatment — stated as a property, not as three names."""
    for dispatcher in ("Task", "Agent", "Skill"):
        assert tl.pinned_effect(dispatcher), (
            f"{dispatcher} is not pinned, so a ledger row may declare it "
            f"read-only and answer for whatever it dispatches")
    assert tl.is_softening("Skill", tl.EFFECT_READ_ONLY) is True


@pytest.mark.parametrize("spelling", ["Skill", "skill", "SKILL"])
def test_no_spelling_of_the_dispatcher_can_be_declared_read_only(spelling):
    with pytest.raises(tl.ToolLedgerError):
        tl.build_record(spelling, purpose="x", effect=tl.EFFECT_READ_ONLY)


def test_a_stricter_declaration_for_the_dispatcher_is_still_allowed():
    """The guard blocks softening only; `state-change` for Skill is honest and
    must stay possible."""
    row = tl.build_record("Skill", purpose="x", effect=tl.EFFECT_STATE_CHANGE)
    assert row["effect"] == tl.EFFECT_STATE_CHANGE


def test_a_real_skill_can_still_declare_read_only():
    """Pinning the wrapper must not take the ledger's purpose with it: a skill
    with no recognisable verb is exactly the case a declaration exists to
    resolve."""
    row = tl.build_record("frobnicate-thing", purpose="x",
                          effect=tl.EFFECT_READ_ONLY)
    assert row["effect"] == tl.EFFECT_READ_ONLY


def test_the_pin_does_not_change_what_the_classifier_answers():
    """Equivalence against the PRE-CHANGE module, over the whole cross product.

    The claim "listing Skill only affects the pinned set" rests on the
    ``name == "Skill"`` branch running before the built-in set is consulted.
    Asserted by comparing the two modules' answers, so a later reordering of
    those branches makes this test — not a human's reading — the thing that
    notices.
    """
    pre_source = subprocess.run(
        ["git", "show", "origin/main:lib/tool_effect.py"],
        capture_output=True, text=True, cwd=str(REPO), timeout=60)
    if pre_source.returncode != 0:
        pytest.skip("origin/main not available in this checkout")
    if '"Skill",' in pre_source.stdout.split("BUILTIN_SIDE_EFFECT")[1][:400]:
        pytest.skip("origin/main already pins Skill; this comparison is spent")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "tool_effect_pre.py"
        path.write_text(pre_source.stdout, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("_te_pre", path)
        pre = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pre)

        names = ["Skill", "Task", "Agent", "Bash", "Read", "Write", "skill",
                 "mcp__g__send_email", "mcp__w__frobnicate", "review", ""]
        payloads = UNREADABLE + [{"skill": "review"},
                                 {"command": "/beacon-note x"}]
        declareds = ["", te.READ_ONLY, te.SIDE_EFFECT]
        diffs = [
            (n, p, d, pre.classify_tool(n, p, d), te.classify_tool(n, p, d))
            for n, p, d in itertools.product(names, payloads, declareds)
            if pre.classify_tool(n, p, d) != te.classify_tool(n, p, d)
        ]
        assert diffs == [], f"the pin changed the classifier's answers: {diffs[:5]}"
        assert te.BUILTIN_SIDE_EFFECT - pre.BUILTIN_SIDE_EFFECT == {"Skill"}


# ---------------------------------------------------------------------------
# End to end through the real gate.
# ---------------------------------------------------------------------------

@pytest.fixture
def armed(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "entries": []}], "documents": []},
        ensure_ascii=False), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(
        "_ut_for_pin_test", LIB / "untrusted_turn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.arm(str(tmp_path), "sQ", sources=["dm:x"])
    return tmp_path


def _seed_raw(root: pathlib.Path, *rows: dict) -> None:
    """Straight to the ledger file: the gate must hold even when the CLI's
    refusal was bypassed."""
    path = tl.ledger_path(root)
    led = tl.load_ledger(path)
    led["tools"] = list(rows)
    tl.save_ledger(path, led)


def _gate(root: pathlib.Path, tool_input) -> str:
    body = json.dumps({"hook_event_name": "PreToolUse", "session_id": "sQ",
                       "cwd": str(root), "tool_name": "Skill",
                       "tool_input": tool_input})
    proc = subprocess.run([sys.executable, str(GATE)], input=body,
                          capture_output=True, text=True, timeout=60,
                          cwd=str(root))
    out = (proc.stdout or "").strip()
    if not out:
        return "not-gated"
    return (json.loads(out).get("hookSpecificOutput", {})
            .get("permissionDecision") or "unknown")


@pytest.mark.parametrize("payload", UNREADABLE,
                         ids=[json.dumps(p, sort_keys=True) for p in UNREADABLE])
def test_an_unreadable_skill_payload_stays_gated_despite_a_dispatcher_row(
        armed, payload):
    _seed_raw(armed, {"name": "Skill", "purpose": "x", "effect": "read-only",
                      "record": {"sink": "undecided"}})
    assert _gate(armed, payload) == "ask", (
        f"a hand-written `Skill` read-only row let {payload!r} through an "
        f"armed untrusted turn with no prompt")


def test_a_readable_declared_skill_still_passes(armed):
    """The pin must not gate a skill whose own row resolves an unknown name —
    otherwise it has eaten the ledger's purpose rather than closed a hole."""
    _seed_raw(armed,
              {"name": "Skill", "purpose": "x", "effect": "read-only",
               "record": {"sink": "undecided"}},
              {"name": "frobnicate-thing", "purpose": "x",
               "effect": "read-only", "record": {"sink": "undecided"}})
    assert _gate(armed, {"skill": "frobnicate-thing"}) == "not-gated"
