"""The dispatcher name itself cannot be declared read-only (ms-180 follow-up).

`Task` and `Agent` were pinned because they start work whose effects the
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
import inspect
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


def test_every_known_dispatcher_is_pinned():
    """Same class, same treatment — for the dispatchers this harness is KNOWN to
    have. This is an enumeration, not a property: see
    ``test_an_unenumerated_dispatcher_can_still_be_declared_read_only`` for the
    limit that follows from that, which is pinned rather than papered over."""
    for dispatcher in ("Task", "Agent", "Skill", "SlashCommand", "KillShell"):
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


def test_pinning_a_dispatcher_does_not_change_what_the_classifier_answers(
        monkeypatch):
    """The invariant, asserted with nothing to fetch and no way to skip.

    A first version compared against ``git show origin/main:lib/tool_effect.py``
    and skipped when that ref did not resolve, or when main already carried the
    pin. An independent maintainability review reproduced this repository's two
    CI triggers and showed the comparison therefore NEVER ran in CI: on
    ``pull_request`` ``actions/checkout@v5`` fetches shallow (test.yml sets no
    fetch-depth) so the ref is absent, and on ``push: main`` the ref resolves but
    by definition already contains the pin. It passed locally — full history, no
    skip — so the hole was invisible where it was written. A skipped assertion
    reads as green and is therefore worse than none.

    The property does not need another commit to state: pinning a dispatcher
    must not change what ``classify_tool`` answers for it. So the set is
    narrowed HERE, in process, and both answers are compared.
    """
    pinned_dispatchers = ["Skill", "SlashCommand", "KillShell"]
    payloads = UNREADABLE + [{"skill": "review"}, {"command": "/beacon-note x"}]
    declareds = ["", te.READ_ONLY, te.SIDE_EFFECT]

    without = frozenset(te.BUILTIN_SIDE_EFFECT) - set(pinned_dispatchers)
    assert len(without) == len(te.BUILTIN_SIDE_EFFECT) - len(pinned_dispatchers), (
        "the dispatchers this test narrows are not all in the set, so the "
        "comparison below would prove nothing")

    before = {}
    for name in pinned_dispatchers + ["Bash", "Read", "mcp__w__frobnicate"]:
        for payload, declared in itertools.product(payloads, declareds):
            key = (name, json.dumps(payload, sort_keys=True), declared)
            before[key] = te.classify_tool(name, payload, declared)

    monkeypatch.setattr(te, "BUILTIN_SIDE_EFFECT", without)
    diffs = [(k, before[k], te.classify_tool(k[0], json.loads(k[1]), k[2]))
             for k in before
             if before[k] != te.classify_tool(k[0], json.loads(k[1]), k[2])]
    assert diffs == [], (
        f"removing the dispatchers from the pinned set changed the "
        f"classifier's answers, so pinning them was not inert: {diffs[:5]}")


def test_the_skill_branch_decides_before_the_builtin_table_is_consulted():
    """Why the above holds, pinned lexically so a reordering is caught.

    ``classify_tool`` answers for a Skill call in the ``name == "Skill"`` branch,
    which runs BEFORE the built-in set is consulted. If those two were swapped,
    pinning ``Skill`` would start deciding Skill calls by the wrapper again —
    reopening the hole this file exists to close. The behavioural test above
    would still pass in that world (both paths answer side-effect for an
    unreadable payload), so the order gets its own assertion.
    """
    source = inspect.getsource(te.classify_tool)
    lines = source.split("\n")
    skill_branch = next(i for i, l in enumerate(lines)
                        if 'name == "Skill"' in l)
    builtin_lookup = next(i for i, l in enumerate(lines)
                          if "name in BUILTIN_SIDE_EFFECT" in l)
    assert skill_branch < builtin_lookup, (
        "the Skill branch no longer runs before the built-in table: pinning "
        "Skill now decides Skill calls by the wrapper name, which is the hole "
        "tests/test_skill_dispatcher_pinned_ms180.py exists to keep closed")


def test_an_unenumerated_dispatcher_can_still_be_declared_read_only():
    """THE LIMIT, pinned so a reader meets it instead of assuming coverage.

    The pinned set is a hand-kept list of names from one harness. Beacon cannot
    enumerate the harness's tools, and refusing every unrecognised bare name
    would eat the ledger's purpose (a Skill and an unknown MCP tool are bare
    names too). So when the harness gains a dispatcher, nothing notices until a
    human adds it — exactly how ``SlashCommand`` and ``KillShell`` were found,
    by an independent AX review that registered one and walked it through the
    gate.

    This test asserts the limit rather than the protection: a dispatcher-shaped
    name NOT in the set is still declarable read-only. If a later change makes
    the coverage genuinely structural, this test should fail — and that failure
    is the signal to delete it, not to work around it.
    """
    unenumerated = "FutureDispatcher"
    assert unenumerated not in te.BUILTIN_SIDE_EFFECT
    assert not te.has_write_verb(unenumerated), (
        "pick a name with no write verb, or this proves nothing about the set")
    row = tl.build_record(unenumerated, purpose="x",
                          effect=tl.EFFECT_READ_ONLY)
    assert row["effect"] == tl.EFFECT_READ_ONLY, (
        "an unenumerated dispatcher-shaped name is no longer declarable "
        "read-only — if that is now structural, delete this test")


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


# ---------------------------------------------------------------------------
# A refusal must answer in the shape the caller asked to be answered in.
# `--json` used to get empty stdout and a Japanese sentence on stderr, while
# the success path answered with a JSON object — so an AI committed to `--json`
# had to parse prose to learn why (independent AX review of PR #808, surfaced
# by the newly pinned `Skill` but shared by every refusal in this command).
# ---------------------------------------------------------------------------

BEACON = REPO / "bin" / "beacon"

#: Every refusal path `beacon tool add` has, not just the one that was reported.
REFUSALS = [
    ("pinned-dispatcher", ["Skill", "--purpose", "x", "--effect", "read-only"]),
    ("pinned-builtin", ["Bash", "--purpose", "x", "--effect", "read-only"]),
    ("write-verb-name", ["send_thing", "--purpose", "x", "--effect", "read-only"]),
    ("no-purpose", ["nm", "--effect", "read-only"]),
    ("no-effect", ["nm", "--purpose", "x"]),
    ("both-record-flags", ["nm", "--purpose", "x", "--effect", "read-only",
                           "--record", "evidence", "--no-record", "r"]),
    ("unknown-recorder", ["nm", "--purpose", "x", "--effect", "outward",
                          "--record", "webhook"]),
    ("unknown-grain", ["nm", "--purpose", "x", "--effect", "outward",
                       "--record", "evidence", "--grain", "hourly"]),
]


@pytest.fixture
def project(tmp_path):
    proc = subprocess.run(
        [str(BEACON), "init", "--name", "t", "--objective", "t",
         "--retro-day", "friday", "--storage", "local"],
        capture_output=True, text=True, cwd=str(tmp_path), timeout=120,
        stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return tmp_path


def _lines(*streams) -> list:
    """Non-empty lines across the given streams.

    The streams are read LINE BY LINE rather than as a whole, because they are
    not ours alone: under parallel test execution a library in the subprocess
    (observed: gRPC's ``ev_poll_posix.cc`` fork warning) writes to the same
    stderr, and a test that parsed the whole stream failed on the noise instead
    of on the property. Assuming exclusive ownership of a shared stream is the
    same mistake as assuming exclusive ownership of a shared checkout.
    """
    out = []
    for stream in streams:
        out.extend(line.strip() for line in (stream or "").split("\n")
                   if line.strip())
    return out


def _json_line(*streams):
    """The first line that parses as a JSON object, or None."""
    for line in _lines(*streams):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


@pytest.mark.parametrize("label, args", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_every_refusal_answers_in_json_when_json_was_asked_for(
        project, label, args):
    proc = subprocess.run([str(BEACON), "tool", "add"] + args + ["--json"],
                          capture_output=True, text=True, cwd=str(project),
                          timeout=120, stdin=subprocess.DEVNULL)
    assert proc.returncode != 0, f"{label} was not refused: {proc.stdout}"
    body = _json_line(proc.stdout, proc.stderr)
    assert body is not None, (
        f"{label}: --json was asked for and the refusal answered in prose, so "
        f"a caller parsing JSON learns nothing about why:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    assert body.get("error"), f"{label}: no error field in {body}"


@pytest.mark.parametrize("label, args", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_the_human_refusal_is_unchanged_without_json(project, label, args):
    """The sentence a human reads must not have moved into a JSON envelope."""
    proc = subprocess.run([str(BEACON), "tool", "add"] + args,
                          capture_output=True, text=True, cwd=str(project),
                          timeout=120, stdin=subprocess.DEVNULL)
    assert proc.returncode != 0
    lines = _lines(proc.stderr)
    assert any(line.startswith("Error: ") for line in lines), (
        f"{label}: no human-readable refusal line:\n{proc.stderr}")
    assert _json_line(proc.stderr) is None, (
        f"{label}: the human channel got JSON; the two shapes were swapped "
        f"rather than separated:\n{proc.stderr}")
