"""Independent review of PR #805, round 2 — the four findings it produced.

Each test fails if its fix is reverted, so the finding cannot come back
unnoticed. Three of the four are the same error type this milestone exists to
remove — *the signal was added, but not to every consumer* — one layer further
down than the round-1 findings that already fixed it once.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import commands as cmds            # noqa: E402
import tool_ledger as tl           # noqa: E402
import tool_usage_debt as debt     # noqa: E402

BEACON = REPO / "bin" / "beacon"


# ---------------------------------------------------------------------------
# M1 (high) — a changed declaration must reach an install that already has the
# hook. Only `command` used to be compared, so a widened matcher reached new
# installs only and every existing one kept the old one, silently.
# ---------------------------------------------------------------------------

def _save_spec():
    return next(s for s in cmds.HOOK_MANIFEST if s["key"] == "save")


def _installed_as(spec, *, matcher, timeout=None, status=None):
    """A settings.json 'hooks' dict holding this hook with the given fields."""
    command = cmds._resolve_manifest_hook_command(spec)
    assert command, "the save hook's command must resolve in this checkout"
    entry = {"hooks": [{
        "type": "command", "command": command,
        "timeout": spec["timeout"] if timeout is None else timeout,
        "statusMessage": spec["statusMessage"] if status is None else status,
    }]}
    if matcher is not None:
        entry["matcher"] = matcher
    return {event: [json.loads(json.dumps(entry))] for event in spec["events"]}


def test_installer_rewrites_a_hook_whose_matcher_predates_the_manifest():
    """The pre-ms-180 install: same command, the old `mcp__` matcher."""
    spec = _save_spec()
    assert spec.get("matcher") == "*", (
        "this test is about the widened matcher; the manifest no longer "
        "declares `*` for the save hook, so it is stale")
    hooks = _installed_as(spec, matcher="mcp__")

    assert cmds._install_manifest_hook(hooks, spec) is True, (
        "the installer reported no change, so an existing install keeps the "
        "old matcher — the declaration changes and nothing happens")
    for event in spec["events"]:
        matchers = [e.get("matcher") for e in hooks[event]]
        assert "*" in matchers, (
            f"{event}: matcher is still {matchers}, so the hook goes on "
            f"filtering out the tool calls this version means to see")
    assert cmds._manifest_hook_drift(hooks, spec) == []


@pytest.mark.parametrize("field, installed", [
    ("matcher", {"matcher": "mcp__"}),
    ("timeout", {"timeout": 3}),
    ("statusMessage", {"status": "something older"}),
])
def test_every_declared_field_is_compared_not_just_the_command(field, installed):
    """All four declared fields, or the comparison has a hole per field."""
    spec = _save_spec()
    kwargs = {"matcher": spec.get("matcher")}
    kwargs.update(installed)
    hooks = _installed_as(spec, **kwargs)

    drift = cmds._manifest_hook_drift(hooks, spec)
    assert any(f == field for _event, f in drift), (
        f"a stale {field} was not reported as drift: {drift}")
    assert cmds._install_manifest_hook(hooks, spec) is True
    assert cmds._manifest_hook_drift(hooks, spec) == [], (
        f"the installer did not bring {field} up to the declaration")


def test_an_install_matching_the_manifest_is_left_alone():
    """Idempotence: the fix must not make every run rewrite settings.json."""
    spec = _save_spec()
    hooks = _installed_as(spec, matcher=spec.get("matcher"))
    assert cmds._manifest_hook_drift(hooks, spec) == []
    assert cmds._install_manifest_hook(hooks, spec) is False


def test_doctor_names_a_hook_that_is_wired_differently_than_declared():
    """`missing` and `wired differently` are different facts: doctor must not
    report one under the other's message, and must report this one at all."""
    source = (REPO / "lib" / "commands.py").read_text(encoding="utf-8")
    assert "_manifest_hook_drift(_all_hooks, spec)" in source, (
        "doctor does not consult the drift check, so a stale matcher stays "
        "invisible until someone reads settings.json by hand")
    assert "are wired \nhistorically-impossible" not in source  # guard typo check
    assert "wired " in source and "differently than declared" in source


# ---------------------------------------------------------------------------
# A1 (medium) — the write-failure sentence reached the human channel only.
# ---------------------------------------------------------------------------

def _run(args, cwd, env_extra=None):
    env = dict(os.environ)
    env.pop("BEACON_SURFACE_PROBE", None)
    env.pop("BEACON_HELP_ONLY", None)
    env.update(env_extra or {})
    return subprocess.run([str(BEACON)] + args, capture_output=True, text=True,
                          cwd=str(cwd), env=env, timeout=120,
                          stdin=subprocess.DEVNULL)


def _json(proc, what):
    """Parse a command's JSON body, saying what went wrong instead of crashing.

    A test that dies in `json.loads` reports a parse error, which tells a reader
    nothing about the property under test — and a command that crashed produces
    exactly that. Check the invocation first.
    """
    assert proc.stdout.strip(), (
        f"{what} printed nothing on stdout (exit {proc.returncode})\n"
        f"--- stderr ---\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{what} did not print JSON ({exc})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        ) from None


def _seed_debt(project, *rows):
    """Write debt rows through the module's own path/format, not a guessed one."""
    path = Path(debt.debt_path(str(project / ".beacon")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unregistered": list(rows)}, ensure_ascii=False),
                    encoding="utf-8")
    assert debt.load_debt(str(path)), "the seeded debt row is not readable back"


def _seed_degraded(project, name):
    """Mark `name` as having had its `turn` grain silently become `each`.

    Seeded through tool_turn's own file so the listing sees it the same way a
    real degraded fold would be seen.
    """
    import tool_turn
    path = Path(tool_turn.turn_path(str(project / ".beacon")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"degraded": {
        name: {"count": 1, "last_seen": "2026-01-01T00:00:00Z"}}}),
        encoding="utf-8")
    assert [r["name"] for r in tool_turn.degraded_tools(str(path))] == [name]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    # Flags, not env: the dispatcher assigns BEACON_INIT_* from the flags and
    # would overwrite an inherited value with the empty string.
    proc = _run(["init", "--name", "t", "--objective", "t",
                 "--retro-day", "friday", "--storage", "local"], tmp_path)
    assert proc.returncode == 0, f"init failed:\n{proc.stdout}\n{proc.stderr}"
    return tmp_path


def test_a_failed_write_explains_itself_on_the_json_channel_too(project):
    """`--json` is the channel automation parses; a bare slug there is the
    defect, not a detail."""
    add = _run(["tool", "add", "mcp__x__send_thing", "--purpose", "p",
                "--effect", "outward", "--record", "evidence"], project)
    assert add.returncode == 0, f"{add.stdout}\n{add.stderr}"

    # No worked Target, so the writer cannot write: the failure path.
    used = _run(["tool", "used", "mcp__x__send_thing", "--summary", "s",
                 "--json"], project)
    body = _json(used, "`tool used --json`")
    assert body.get("recorded") is False, body
    assert body.get("message"), (
        f"the JSON body carries only the code {body.get('reason')!r} with no "
        f"sentence: {body}")

    text = _run(["tool", "used", "mcp__x__send_thing", "--summary", "s"],
                project)
    reason = body["reason"]
    sys.path.insert(0, str(REPO / "lib"))
    import cmd_tool
    assert body["message"] == cmd_tool.explain_write_failure(reason), (
        "the two channels explain the same failure differently")
    assert reason not in body["message"] or len(body["message"]) > len(reason)
    assert text.returncode == 1 and used.returncode == 1


def test_the_debt_list_explains_the_write_failure_on_both_channels(project):
    """Same split, in the debt listing: the text loop explained each row's
    detail and the JSON branch emitted the bare code."""
    row = {"name": "mcp__x__send_thing", "count": 1,
           "last_seen": "2026-01-01T00:00:00Z",
           "reason": debt.REASON_WRITE_FAILED, "detail": "no-target"}
    _seed_debt(project, row)

    payload = _json(_run(["tool", "unregistered", "--json"], project),
                    "`tool unregistered --json`")
    assert debt.REASON_WRITE_FAILED in payload, (
        f"no bucket for {debt.REASON_WRITE_FAILED!r}; keys are "
        f"{sorted(payload)} (see the sibling test on the reason's spelling)")
    bucket = payload[debt.REASON_WRITE_FAILED]
    assert bucket, payload
    assert bucket[0].get("detail_message"), (
        f"the JSON row carries only detail={bucket[0].get('detail')!r}: "
        f"{bucket[0]}")


# ---------------------------------------------------------------------------
# A2 (medium) — one concept, two spellings, in one payload.
# ---------------------------------------------------------------------------

def test_the_debt_payload_is_keyed_by_the_reason_constant_itself(project):
    """A bucket name that differs from the value its own rows carry is a
    spelling a reader can learn and then fail to match on."""
    row = {"name": "t", "count": 1, "last_seen": "2026-01-01T00:00:00Z",
           "reason": debt.REASON_WRITE_FAILED, "detail": "no-target"}
    _seed_debt(project, row)

    payload = _json(_run(["tool", "unregistered", "--json"], project),
                    "`tool unregistered --json`")
    for reason in debt.REASONS:
        assert reason in payload, (
            f"{reason!r} has no bucket; the payload keys are "
            f"{sorted(payload)} — a second spelling of the reason")
        for entry in payload[reason]:
            assert entry.get("reason") == reason, (
                f"bucket {reason!r} holds a row whose own reason is "
                f"{entry.get('reason')!r}")


def test_no_second_machine_spelling_of_a_reason_exists():
    """Remove the thing that can drift, rather than keeping it in step."""
    for reason, meta in debt.REASON_DISPLAY.items():
        assert "key" not in meta, (
            f"{reason!r} carries a separate machine name {meta.get('key')!r}; "
            f"two spellings of one concept is what drifted")


# ---------------------------------------------------------------------------
# A3 (low) — a filtered view reported whole-ledger signals unlabelled.
# ---------------------------------------------------------------------------

def test_a_filtered_listing_only_reports_on_what_it_returned(project):
    """`--undecided` named a fully-decided tool in `degraded`, which appears
    nowhere else in the same response."""
    assert _run(["tool", "add", "mcp__a__undecided", "--purpose", "p",
                 "--effect", "read-only"], project).returncode == 0
    assert _run(["tool", "add", "mcp__b__decided", "--purpose", "p",
                 "--effect", "outward", "--record", "evidence",
                 "--grain", "turn"], project).returncode == 0
    _seed_degraded(project, "mcp__b__decided")

    full = _json(_run(["tool", "list", "--json"], project),
                 "`tool list --json`")
    assert any(r["name"] == "mcp__b__decided" for r in full.get("degraded", [])), (
        "the unfiltered view should still show the degraded-grain tool; "
        "otherwise this test proves nothing about scoping")

    narrowed = _json(_run(["tool", "list", "--undecided", "--json"], project),
                     "`tool list --undecided --json`")
    shown = {r["name"] for r in narrowed["tools"]}
    assert shown == {"mcp__a__undecided"}, shown
    for name in [r["name"] for r in narrowed.get("degraded", [])]:
        assert name in shown, (
            f"`degraded` names {name!r}, which this response never returned")


# ---------------------------------------------------------------------------
# M2 (low, partially accepted) — the enumerable vocabulary is now written down
# and the table that explains it is checked against it. The write-failure codes
# are NOT enumerable (generated per class), which is why that helper answers an
# unknown code by naming where it came from instead; see its docstring.
# ---------------------------------------------------------------------------

def test_every_record_plan_action_has_an_explanation():
    source = (REPO / "lib" / "cmd_tool.py").read_text(encoding="utf-8")
    start = source.index("            reasons = {")
    end = source.index("}", source.index("unregistered", start))
    block = source[start:end]
    explained = {a for a in tl.RECORD_PLAN_ACTIONS if f'"{a}":' in block}
    # `record` is handled before this table (it is the success path).
    assert explained | {"record"} == set(tl.RECORD_PLAN_ACTIONS), (
        f"actions with no sentence: "
        f"{sorted(set(tl.RECORD_PLAN_ACTIONS) - explained - {'record'})} — "
        f"they would print the raw action word")


def test_the_action_vocabulary_matches_what_record_plan_can_return():
    """The written-down vocabulary must not drift from the producer."""
    source = (REPO / "lib" / "tool_ledger.py").read_text(encoding="utf-8")
    produced = set(__import__("re").findall(r'\{"action":\s*"([a-z]+)"', source))
    assert produced, "could not read the actions out of record_plan"
    assert produced <= set(tl.RECORD_PLAN_ACTIONS), (
        f"record_plan returns {sorted(produced - set(tl.RECORD_PLAN_ACTIONS))} "
        f"which RECORD_PLAN_ACTIONS does not list")
    assert tl.DEBT_ACTIONS <= tl.RECORD_PLAN_ACTIONS
