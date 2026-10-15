"""ms-180 e-7003 — the effect judgment becomes a ledger LOOKUP, not a name guess.

``tool_effect`` classified by regex over a tool's NAME. That is a guess, and the
ms-169 approval gate consumes it: a wrong "read-only" means a side-effect tool
runs in an untrusted-DM turn with no human in the loop. The ledger replaces the
guess with a declaration.

Replacing a guess with a declaration raises one question, and these tests exist
to answer it in the only direction that matters: **can a ledger row make the gate
weaker?** The answer has to be no, enforced structurally rather than promised,
because the file is a plain JSON file that anyone can edit.

What is pinned here:

  * a declaration REPLACES the heuristic for ordinary tools (that is the feature),
  * a declaration can never soften a pinned built-in — checked for every member of
    ``BUILTIN_SIDE_EFFECT``, through the real gate hook as well as the classifier,
  * the pinned set is DERIVED from ``BUILTIN_SIDE_EFFECT`` rather than copied, so
    a tool added there is protected automatically. The equality is asserted, and
    an inversion harness proves these assertions actually fail when the guard is
    removed — a green guard is trusted, so a guard that cannot go red is worse
    than no guard at all.
  * an unregistered tool keeps the ORIGINAL behaviour exactly (受入条件6).
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

import tool_effect as te      # noqa: E402
import tool_ledger as tl      # noqa: E402
import untrusted_turn as ut   # noqa: E402

GATE = REPO / "bin" / "beacon-untrusted-tool-gate.py"


def _ledger(*rows: dict) -> dict:
    led = tl.empty_ledger()
    for row in rows:
        tl.upsert(led, row)
    return led


def _raw_ledger(**name_to_effect) -> dict:
    """A ledger as if hand-edited: rows the CLI would have refused to write."""
    return {"version": 1, "tools": [
        {"name": n, "effect": e,
         "record": {"sink": tl.SINK_UNDECIDED, "grain": tl.GRAIN_EACH}}
        for n, e in name_to_effect.items()]}


# ---------------------------------------------------------------------------
# The feature: a declaration replaces the guess.
# ---------------------------------------------------------------------------

def test_declaration_overrides_a_wrong_name_guess():
    """``frobnicate`` carries no recognisable verb, so the heuristic default-denies
    it. Declaring it read-only is exactly the correction the ledger exists for."""
    assert te.classify_tool("mcp__weird__frobnicate") == te.SIDE_EFFECT
    led = _ledger(tl.build_record("mcp__weird__frobnicate", purpose="p",
                                  effect=tl.EFFECT_READ_ONLY))
    assert te.classify_tool("mcp__weird__frobnicate", None, led) == te.READ_ONLY


def test_declaration_can_tighten_a_tool_the_heuristic_called_read_only():
    """The ledger must work in the strict direction too, or it could only ever
    reduce gating."""
    assert te.classify_tool("mcp__x__list_things") == te.READ_ONLY
    led = _ledger(tl.build_record("mcp__x__list_things", purpose="p",
                                  effect=tl.EFFECT_OUTWARD))
    assert te.classify_tool("mcp__x__list_things", None, led) == te.SIDE_EFFECT


@pytest.mark.parametrize("effect,expected", [
    (tl.EFFECT_READ_ONLY, te.READ_ONLY),
    (tl.EFFECT_STATE_CHANGE, te.SIDE_EFFECT),
    (tl.EFFECT_OUTWARD, te.SIDE_EFFECT),
])
def test_each_declared_effect_maps_to_one_gate_answer(effect, expected):
    led = _ledger(tl.build_record("mcp__srv__thing", purpose="p", effect=effect))
    assert te.classify_tool("mcp__srv__thing", None, led) == expected


# ---------------------------------------------------------------------------
# 受入条件6 — an unregistered tool behaves EXACTLY as before.
# ---------------------------------------------------------------------------

_SAMPLE_NAMES = [
    "Read", "Grep", "Write", "Bash", "WebFetch", "Task", "Skill",
    "mcp__gmail__send_email", "mcp__gmail__search_emails",
    "mcp__drive__createGoogleDoc", "mcp__x__frobnicate", "", "   ",
]


@pytest.mark.parametrize("name", _SAMPLE_NAMES)
def test_unregistered_tools_classify_identically_with_and_without_a_ledger(name):
    led = _ledger(tl.build_record("something__else", purpose="p",
                                  effect=tl.EFFECT_READ_ONLY))
    assert te.classify_tool(name, None, led) == te.classify_tool(name)
    assert te.classify_tool(name, None, None) == te.classify_tool(name)


def test_an_unreadable_ledger_leaves_the_heuristic_in_charge(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text("{not json", encoding="utf-8")
    led = tl.load_ledger(str(path))
    for name in _SAMPLE_NAMES:
        assert te.classify_tool(name, None, led) == te.classify_tool(name)


def test_a_garbage_ledger_object_does_not_raise_into_the_classifier():
    """The classifier is called from a hook. A malformed ledger must degrade to
    the heuristic, never throw."""
    for junk in ("not a ledger", 42, [1, 2, 3], {"tools": "nope"}):
        assert te.classify_tool("Bash", None, junk) == te.SIDE_EFFECT
        assert te.classify_tool("Read", None, junk) == te.READ_ONLY


# ---------------------------------------------------------------------------
# The one-way guard: no row can soften a pinned built-in.
# ---------------------------------------------------------------------------

def test_pinned_set_is_derived_from_the_classifier_table_not_copied():
    """If this ever has to be maintained by hand, adding a tool to
    ``BUILTIN_SIDE_EFFECT`` would silently make it demotable with every test
    still green."""
    assert set(tl.NON_SOFTENABLE_EFFECT) == set(te.BUILTIN_SIDE_EFFECT)


@pytest.mark.parametrize("pinned", sorted(te.BUILTIN_SIDE_EFFECT))
def test_hand_edited_read_only_row_cannot_soften_a_pinned_builtin(pinned):
    led = _raw_ledger(**{pinned: tl.EFFECT_READ_ONLY})
    assert tl.effect_of(led, pinned) is None
    assert te.classify_tool(pinned, None, led) == te.SIDE_EFFECT
    assert te.is_side_effect(pinned, None, led) is True


def test_softening_every_pinned_builtin_at_once_still_gates_all_of_them():
    led = _raw_ledger(**{n: tl.EFFECT_READ_ONLY for n in te.BUILTIN_SIDE_EFFECT})
    assert all(te.is_side_effect(n, None, led) for n in te.BUILTIN_SIDE_EFFECT)


# ---------------------------------------------------------------------------
# Inversion harness — prove the assertions above actually fail when the guard
# is removed. Without this, the tests above could be passing for the wrong
# reason and nobody would know (the ms-173/#747 lesson: a guard that cannot go
# red is a false "safe").
# ---------------------------------------------------------------------------

def test_inversion_removing_the_softening_guard_makes_the_gate_weaker(monkeypatch):
    """Replace ``is_softening`` with a no-op — i.e. delete the guard — and confirm
    the pinned built-ins DO become read-only. That is what the guard prevents, so
    seeing it happen here is what proves the guard is load-bearing."""
    monkeypatch.setattr(tl, "is_softening", lambda name, effect: False)
    led = _raw_ledger(**{n: tl.EFFECT_READ_ONLY for n in te.BUILTIN_SIDE_EFFECT})
    leaked = [n for n in te.BUILTIN_SIDE_EFFECT
              if not te.is_side_effect(n, None, led)]
    assert sorted(leaked) == sorted(te.BUILTIN_SIDE_EFFECT), (
        "with the guard disabled every pinned built-in should leak; if some did "
        "not, this inversion no longer exercises the guard and the tests above "
        "may be passing for an unrelated reason")


def test_inversion_a_copied_pin_set_would_miss_a_newly_added_builtin(monkeypatch):
    """Simulate the maintenance hazard the derivation removes: a COPIED pin set
    that predates a new entry in ``BUILTIN_SIDE_EFFECT``."""
    stale_copy = {n: tl.EFFECT_STATE_CHANGE
                  for n in te.BUILTIN_SIDE_EFFECT if n != "Bash"}
    monkeypatch.setattr(tl, "NON_SOFTENABLE_EFFECT", stale_copy)
    led = _raw_ledger(Bash=tl.EFFECT_READ_ONLY)
    assert te.is_side_effect("Bash", None, led) is False, (
        "a stale copy of the pin set should let Bash through — this is the "
        "failure mode deriving the set from BUILTIN_SIDE_EFFECT prevents")


# ---------------------------------------------------------------------------
# Through the REAL gate hook (subprocess) — the classifier is not the gate.
# ---------------------------------------------------------------------------

def _beacon_root(tmp_path: Path) -> Path:
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({"name": "t"}))
    return tmp_path


def _run_gate(cwd: Path, hook_input: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps({**hook_input, "cwd": str(cwd)}),
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _decision(out: dict) -> str:
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "")


def _write_ledger(root: Path, led: dict) -> None:
    tl.save_ledger(tl.ledger_path(root), led)


def _write_raw_ledger(root: Path, led: dict) -> None:
    Path(tl.ledger_path(root)).write_text(
        json.dumps(led, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("pinned", sorted(te.BUILTIN_SIDE_EFFECT))
def test_gate_still_asks_for_a_pinned_builtin_declared_read_only(tmp_path, pinned):
    """The end-to-end form of the guard: an armed untrusted turn plus a ledger
    that declares ``Bash`` (etc.) read-only must STILL stop for a human."""
    root = _beacon_root(tmp_path)
    _write_raw_ledger(root, _raw_ledger(**{pinned: tl.EFFECT_READ_ONLY}))
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                           "tool_name": pinned, "tool_input": {}})
    assert _decision(out) == "ask", f"{pinned} escaped the gate via the ledger"


def test_gate_honours_a_declaration_that_tightens(tmp_path):
    """``Grep`` passes the heuristic. Declared outward, the gate must ask — the
    declaration has to be able to ADD protection, not only confirm it."""
    root = _beacon_root(tmp_path)
    _write_ledger(root, _ledger(tl.build_record(
        "Grep", purpose="ログを外に送る wrapper", effect=tl.EFFECT_OUTWARD)))
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                           "tool_name": "Grep", "tool_input": {"pattern": "x"}})
    assert _decision(out) == "ask"


def test_gate_lets_a_declared_read_only_tool_through(tmp_path):
    """The over-gating side: an unknown-verb MCP tool that is genuinely read-only
    should stop being gated once it is declared, or declaring it buys nothing."""
    root = _beacon_root(tmp_path)
    name = "mcp__weird__frobnicate"
    ut.arm(root, "sv-1", event_ids=["e-1"])
    out = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                           "tool_name": name, "tool_input": {}})
    assert _decision(out) == "ask"  # before declaring: default-deny
    _write_ledger(root, _ledger(tl.build_record(
        name, purpose="状態を見るだけ", effect=tl.EFFECT_READ_ONLY)))
    out2 = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                            "tool_name": name, "tool_input": {}})
    assert _decision(out2) == ""  # after declaring: passes


def test_gate_with_no_ledger_file_behaves_as_before(tmp_path):
    root = _beacon_root(tmp_path)
    assert not Path(tl.ledger_path(root)).exists()
    ut.arm(root, "sv-1", event_ids=["e-1"])
    assert _decision(_run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Write", "tool_input": {}})) == "ask"
    assert _decision(_run_gate(root, {
        "hook_event_name": "PreToolUse", "session_id": "sv-1",
        "tool_name": "Read", "tool_input": {}})) == ""


def test_gate_does_not_fire_when_not_armed_even_with_a_ledger(tmp_path):
    """The ledger changes WHAT a tool is, never WHEN the gate applies. A
    side-effect tool outside an untrusted turn still passes."""
    root = _beacon_root(tmp_path)
    _write_ledger(root, _ledger(tl.build_record(
        "mcp__x__thing", purpose="p", effect=tl.EFFECT_OUTWARD)))
    out = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                           "tool_name": "mcp__x__thing", "tool_input": {}})
    assert _decision(out) == ""


def test_gate_records_unregistered_use_as_debt(tmp_path):
    """e-7002 via the gate: observing an undeclared tool must note the debt and
    must NOT block the call."""
    import tool_usage_debt as debt
    root = _beacon_root(tmp_path)
    _write_ledger(root, _ledger(tl.build_record(
        "Read", purpose="読む", effect=tl.EFFECT_READ_ONLY)))
    out = _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                           "tool_name": "mcp__brand__new_tool",
                           "tool_input": {}})
    assert _decision(out) == ""  # not armed → the call proceeds untouched
    rows = debt.load_debt(debt.debt_path(str(root / ".beacon")))
    assert [r["name"] for r in rows] == ["mcp__brand__new_tool"]


def test_gate_records_no_debt_for_a_tool_whose_sink_is_settled(tmp_path):
    """A tool with a DECIDED sink leaves no debt — whether the decision was to
    record or not to. Only an unjudged sink does (受入条件2)."""
    import tool_usage_debt as debt
    root = _beacon_root(tmp_path)
    _write_ledger(root, _ledger(
        tl.build_record("Read", purpose="読む", effect=tl.EFFECT_READ_ONLY,
                        decline_reason="読み取りは証跡ではない"),
        tl.build_record("mcp__gmail__send_email", purpose="送る",
                        effect=tl.EFFECT_OUTWARD,
                        recorder=tl.RECORDER_EVIDENCE)))
    for name in ("Read", "mcp__gmail__send_email"):
        _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                         "tool_name": name, "tool_input": {}})
    assert debt.load_debt(debt.debt_path(str(root / ".beacon"))) == []


def test_gate_records_an_undecided_tools_use_as_its_own_reason(tmp_path):
    """The gate sees every call, so it is also where an unjudged row being USED
    becomes visible — distinguishable from a tool nobody declared at all."""
    import tool_usage_debt as debt
    root = _beacon_root(tmp_path)
    _write_ledger(root, _ledger(tl.build_record(
        "Read", purpose="読む", effect=tl.EFFECT_READ_ONLY)))
    _run_gate(root, {"hook_event_name": "PreToolUse", "session_id": "sv-1",
                     "tool_name": "Read", "tool_input": {}})
    rows = debt.load_debt(debt.debt_path(str(root / ".beacon")))
    assert [(r["name"], r["reason"]) for r in rows] == \
        [("Read", debt.REASON_UNDECIDED)]
