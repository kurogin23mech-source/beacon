"""ms-180 — the five findings from the independent AX review of PR #802.

All five reproduced against the real CLI before being accepted, and all five are
pinned here so none can come back silently. What each one was:

  F1 (high)   `beacon tool used` exited 0 while its own JSON said
              ``"recorded": false`` — a caller checking only ``$?`` (the cheapest
              automation signal, and the one the AX principles require collecting)
              read a non-recorded use as success.
  F2 (high)   One concept spelled two ways inside one verb family
              (``--record`` on ``add`` / ``--recorder`` on ``used``), AND the
              Python frontend's single shared argparse parser silently ACCEPTED a
              sibling subcommand's flag and ignored it, while bash rejected it.
  F3 (high)   No excess-positional guard on the value-taking subcommands, so an
              unquoted multi-word ``--purpose`` lost every word after the first
              while still printing success — silently corrupting the one column
              the feature exists to make trustworthy.
  F4 (medium) Usage on a missing / unknown subcommand exited 0 in bash and 1 in
              Python for byte-identical input.
  F5 (medium) ``_record_via_save`` never passed ``tool=`` to the recorder, so
              every ``save``-recorded use was invisible to a ``meta.tool`` query
              while its ``evidence`` sibling was fine.

F5's root cause is worth stating, because it is the generalisable one: the
original work verified that every WRITER *accepts* the structured field, and did
not verify that every CALLER *passes* it. Both recorder paths are therefore
asserted together below — the asymmetry, not just the field.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BEACON = REPO / "bin" / "beacon"
HOOK = REPO / "bin" / "beacon-save-hook.sh"


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".beacon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "progress": 0, "entries": []}],
        "documents": [],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def bash(project: Path, *argv: str):
    """Run the bash frontend from inside ``project``."""
    return subprocess.run([str(BEACON), *argv], cwd=str(project),
                          capture_output=True, text=True, timeout=60)


def py(project: Path, *argv: str):
    """Run the Python/argparse frontend on the same input.

    ``root`` is the beacon INSTALL root (where lib/commands.py lives); the
    project comes from the cwd, same as bash.
    """
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from beacon_cli.dispatch import dispatch\n"
        f"sys.exit(dispatch(Path({str(REPO)!r}), sys.argv[1:]))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-c", code, *argv],
                          cwd=str(project), capture_output=True, text=True,
                          timeout=60, env=env)


def declare(project: Path, name: str, *extra: str) -> None:
    r = bash(project, "tool", "add", name, "--purpose", "用途", "--effect",
             "read-only", *extra)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# F1 — the exit code must agree with the body.
# ---------------------------------------------------------------------------

def test_used_exits_nonzero_when_the_tool_is_unregistered(project):
    r = bash(project, "tool", "used", "mcp__never__registered",
             "--summary", "did a thing")
    assert r.returncode != 0, "a non-recorded use must not report success"


def test_used_exits_nonzero_when_the_record_sink_is_undecided(project):
    declare(project, "mcp__x__peek")
    r = bash(project, "tool", "used", "mcp__x__peek", "--summary", "peeked")
    assert r.returncode != 0


def test_used_exits_zero_when_recording_was_declined(project):
    """``declined`` is an ANSWER, not a debt — nothing went wrong, so 0 is
    correct. This is the distinction that makes the non-zero cases meaningful."""
    declare(project, "Grep", "--no-record", "下調べは証跡ではない")
    r = bash(project, "tool", "used", "Grep", "--summary", "grepped")
    assert r.returncode == 0, r.stderr


def test_used_exits_zero_when_it_actually_recorded(project):
    r = bash(project, "tool", "add", "mcp__gmail__send_email", "--purpose",
             "送る", "--effect", "outward", "--record", "evidence")
    assert r.returncode == 0, r.stderr
    r = bash(project, "tool", "used", "mcp__gmail__send_email",
             "--summary", "sent")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("reason", ["unregistered", "undecided"])
def test_the_json_body_and_the_exit_code_never_disagree(project, reason):
    if reason == "undecided":
        declare(project, "mcp__x__peek")
        name = "mcp__x__peek"
    else:
        name = "mcp__never__registered"
    r = bash(project, "tool", "used", name, "--summary", "s", "--json")
    body = json.loads(r.stdout)
    assert body["recorded"] is False and body["reason"] == reason
    assert r.returncode != 0, (
        "the process said success while the body said recorded=false")


# ---------------------------------------------------------------------------
# F2 — one spelling, and a sibling's flag is an error in BOTH frontends.
# ---------------------------------------------------------------------------

def test_the_sink_flag_is_spelled_record_on_both_subcommands(project):
    """One concept, one spelling. Two spellings inside one verb family is the
    flag-transfer mistake AX principle 1 names."""
    assert bash(project, "tool", "add", "mcp__a__thing", "--purpose", "p",
                "--effect", "read-only", "--record", "evidence").returncode == 0
    assert bash(project, "tool", "used", "mcp__a__thing", "--record",
                "evidence", "--summary", "s").returncode == 0


def test_the_old_recorder_spelling_is_gone_from_both_frontends(project):
    declare(project, "mcp__a__thing", "--record", "evidence")
    assert bash(project, "tool", "used", "mcp__a__thing", "--recorder",
                "evidence", "--summary", "s").returncode != 0
    assert py(project, "tool", "used", "mcp__a__thing", "--recorder",
              "evidence", "--summary", "s").returncode != 0


@pytest.mark.parametrize("argv,label", [
    (("tool", "add", "mcp__b__thing", "--purpose", "p", "--effect",
      "read-only", "--summary", "oops"), "add + used's --summary"),
    (("tool", "add", "mcp__b__thing", "--purpose", "p", "--effect",
      "read-only", "--clear"), "add + unregistered's --clear"),
    (("tool", "list", "--purpose", "p"), "list + add's --purpose"),
    (("tool", "unregistered", "--grain", "turn"), "unregistered + add's --grain"),
])
def test_a_sibling_subcommands_flag_is_rejected_by_both_frontends(project, argv,
                                                                  label):
    """Every `tool` flag lives on ONE shared argparse parser, so without an
    explicit per-subcommand check the Python frontend accepted a sibling's flag
    and silently ignored it — while bash rejected it. The two frontends must
    agree on what counts as an error."""
    assert bash(project, *argv).returncode != 0, f"bash accepted {label}"
    assert py(project, *argv).returncode != 0, f"python accepted {label}"


@pytest.mark.parametrize("argv", [
    ("tool", "list", "--undecided", "--json"),
    ("tool", "unregistered", "--json"),
])
def test_a_subcommands_own_flags_still_work_in_both_frontends(project, argv):
    """Over-reach check: the per-subcommand table must not reject legitimate
    flags, or it would break the surface it is protecting."""
    assert bash(project, *argv).returncode == 0
    assert py(project, *argv).returncode == 0


# ---------------------------------------------------------------------------
# F3 — an excess positional is a hard error naming the real cause.
# ---------------------------------------------------------------------------

def test_an_unquoted_multiword_purpose_fails_instead_of_truncating(project):
    """The worst of the five: the purpose silently became its first word while
    the command printed success — corrupting the one column ``build_record``
    refuses to leave EMPTY but cannot detect as truncated."""
    r = bash(project, "tool", "add", "mcp__cal__confirm", "--purpose",
             "confirm", "a", "meeting", "--effect", "state-change")
    assert r.returncode != 0
    assert "mcp__cal__confirm" not in bash(
        project, "tool", "list").stdout, "the bad row must not be stored"


def test_the_excess_positional_error_names_the_token_and_the_fix(project):
    """A misleading message is itself the defect: the reused guard said
    "'a' looks like a flag, not a value", which is wrong for a plain word and
    sends the reader after the wrong cause."""
    r = bash(project, "tool", "add", "mcp__cal__confirm", "--purpose",
             "confirm", "a", "meeting", "--effect", "state-change")
    assert "'a'" in r.stderr
    assert "引用符" in r.stderr          # names the actual recovery
    assert "flag" not in r.stderr.lower()  # and not the wrong cause


def test_a_quoted_multiword_purpose_is_stored_whole(project):
    assert bash(project, "tool", "add", "mcp__cal__ok", "--purpose",
                "confirm a meeting", "--effect", "state-change").returncode == 0
    body = json.loads(bash(project, "tool", "show", "mcp__cal__ok",
                           "--json").stdout)
    assert body["tool"]["purpose"] == "confirm a meeting"


def test_an_unquoted_lookup_query_fails_instead_of_narrowing(project):
    """Silently dropping words turned a malformed invocation into a plausible
    "not found" — a false negative the caller cannot distinguish from a real one."""
    r = bash(project, "tool", "lookup", "send", "a", "mail")
    assert r.returncode != 0


@pytest.mark.parametrize("sub", ["add", "show", "remove", "used", "lookup",
                                  "list", "unregistered"])
def test_every_tool_subcommand_rejects_an_excess_positional(project, sub):
    argv = ["tool", sub]
    if sub in ("add", "show", "remove", "used", "lookup"):
        argv.append("mcp__some__tool")
    argv.append("STRAY")
    assert bash(project, *argv).returncode != 0, f"{sub} swallowed a stray token"


@pytest.mark.parametrize("sub", ["add", "show", "remove", "used", "lookup"])
def test_help_still_works_on_the_guarded_subcommands(project, sub):
    """The guard must not eat ``-h``: that would trade a silent drop for a
    broken help path."""
    r = bash(project, "tool", sub, "-h")
    assert r.returncode == 0


# ---------------------------------------------------------------------------
# F4 — usage on a missing / unknown subcommand is a failure in both frontends.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [("tool",), ("tool", "bogus")])
def test_missing_or_unknown_subcommand_fails_in_both_frontends(project, argv):
    assert bash(project, *argv).returncode != 0
    assert py(project, *argv).returncode != 0


def test_explicit_help_is_still_a_success(project):
    assert bash(project, "tool", "--help").returncode == 0
    assert py(project, "tool", "--help").returncode == 0


# ---------------------------------------------------------------------------
# F5 — BOTH recorder paths stamp the structured tool field.
# ---------------------------------------------------------------------------

def _entries(project: Path) -> list:
    data = json.loads((project / ".beacon" / "project.json").read_text(
        encoding="utf-8"))
    return data["milestones"][0]["entries"]


@pytest.mark.parametrize("recorder", ["save", "evidence"])
def test_both_recorder_paths_stamp_meta_tool(project, recorder):
    """Asserted for BOTH recorders in one parametrize precisely because the bug
    was an asymmetry: verifying the writers ACCEPT the field is not the same as
    verifying every caller PASSES it."""
    name = f"mcp__drive__upload_{recorder}"
    assert bash(project, "tool", "add", name, "--purpose", "保管する",
                "--effect", "state-change", "--record",
                recorder).returncode == 0
    assert bash(project, "tool", "used", name, "--record", recorder,
                "--summary", "did it").returncode == 0
    entries = _entries(project)
    assert len(entries) == 1
    assert (entries[0].get("meta") or {}).get("tool") == name


def test_the_hook_path_also_stamps_meta_tool_for_the_save_recorder(project):
    assert bash(project, "tool", "add", "mcp__drive__upload", "--purpose",
                "保管する", "--effect", "state-change", "--record",
                "save").returncode == 0
    payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s",
                          "cwd": str(project),
                          "tool_name": "mcp__drive__upload",
                          "tool_input": {"title": "report.pdf"}})
    r = subprocess.run([str(HOOK)], input=payload, capture_output=True,
                       text=True, timeout=60, cwd=str(project))
    assert r.returncode == 0, r.stderr
    entries = _entries(project)
    assert len(entries) == 1
    assert (entries[0].get("meta") or {}).get("tool") == "mcp__drive__upload"


def test_the_tool_is_not_carried_only_as_prose_in_the_description(project):
    """The structured field is the point; a ``[tool: …]`` substring in free text
    is exactly the loose match the design rejected. Pin that the save path no
    longer relies on it."""
    assert bash(project, "tool", "add", "mcp__drive__upload", "--purpose",
                "保管する", "--effect", "state-change", "--record",
                "save").returncode == 0
    assert bash(project, "tool", "used", "mcp__drive__upload", "--record",
                "save", "--summary", "uploaded y").returncode == 0
    entry = _entries(project)[0]
    assert entry["meta"]["tool"] == "mcp__drive__upload"
    assert "[tool:" not in entry["description"]


# ---------------------------------------------------------------------------
# Findings from the independent AX review of PR #805 (the follow-up PR).
#
#   A1 (high)   The degraded-grain disclosure reached only the human-text output
#               of `tool list`. `--json` reported `grain: turn` for a tool that
#               was in fact recording every call, because the json body returned
#               BEFORE the degraded check ran. The one channel AI tooling
#               actually parses was the one channel that lacked the signal — the
#               same shape as the defect this milestone exists to remove: the
#               disclosure was added, but not to every consumer.
#   A2 (medium) A write failure printed the raw internal slug (`no-target`,
#               `empty-summary`, `…-no-evidence-arm`) while its three sibling
#               non-recording states each got a full sentence naming the fix.
#               Within one command's own failure vocabulary, two tiers explained
#               themselves and the third sent the reader into lib/occupation.py.
# ---------------------------------------------------------------------------

def _declare_turn_tool(project: Path, name: str = "mcp__turn__tool") -> None:
    r = bash(project, "tool", "add", name, "--purpose", "確認する",
             "--effect", "read-only", "--record", "save", "--grain", "turn")
    assert r.returncode == 0, r.stderr


def _fire(project: Path, tool_name: str, *, event="PostToolUse",
          session="no-boundary") -> None:
    payload = json.dumps({"hook_event_name": event, "session_id": session,
                          "cwd": str(project), "tool_name": tool_name,
                          "tool_input": {}})
    r = subprocess.run([str(REPO / "bin" / "beacon-save-hook.sh")],
                       input=payload, capture_output=True, text=True,
                       timeout=60, cwd=str(project))
    assert r.returncode == 0, r.stderr


def test_json_output_discloses_a_degraded_turn_grain(project):
    """A1: the signal must reach the consumer that parses, not only the reader."""
    _declare_turn_tool(project)
    for _ in range(2):
        _fire(project, "mcp__turn__tool")          # no UserPromptSubmit ever
    body = json.loads(bash(project, "tool", "list", "--json").stdout)
    assert body["degraded"], "--json hides the degradation the text output shows"
    assert body["degraded"][0]["name"] == "mcp__turn__tool"
    record = body["tools"][0]["record"]
    assert record["grain"] == "turn"              # the DECLARATION is preserved
    assert record["grain_effective"] == "each"    # …and so is the reality
    assert record["grain_degraded"] is True


def test_json_and_text_agree_on_which_tools_are_degraded(project):
    """Both modes read one helper, so they cannot disagree."""
    _declare_turn_tool(project)
    _fire(project, "mcp__turn__tool")
    body = json.loads(bash(project, "tool", "list", "--json").stdout)
    text = bash(project, "tool", "list").stdout
    assert ("mcp__turn__tool" in text) and ("縮退" in text)
    assert [r["name"] for r in body["degraded"]] == ["mcp__turn__tool"]


def test_a_working_fold_reports_no_degradation_in_either_mode(project):
    """Over-reach check: the signal must be ABSENT when the fold works, or it is
    noise and gets ignored — and the fold itself must still happen."""
    _declare_turn_tool(project, "mcp__turn__ok")
    _fire(project, "", event="UserPromptSubmit", session="s")
    for _ in range(2):
        _fire(project, "mcp__turn__ok", session="s")
    body = json.loads(bash(project, "tool", "list", "--json").stdout)
    assert body["degraded"] == []
    assert "grain_degraded" not in body["tools"][0]["record"]
    assert len(_entries(project)) == 1, "the fold stopped working"


def test_json_also_carries_the_other_two_advisory_counts(project):
    """The same asymmetry would apply to the undecided / purposeless signals."""
    assert bash(project, "tool", "add", "mcp__a__thing", "--purpose", "p",
                "--effect", "read-only").returncode == 0
    body = json.loads(bash(project, "tool", "list", "--json").stdout)
    assert body["undecided_count"] == 1
    assert "purposeless_count" in body


@pytest.mark.parametrize("code,must_mention", [
    ("no-target", "milestone start"),
    ("empty-summary", "--summary"),
    ("no-milestone", "--target"),
    ("beacon-cli-not-found", "PATH"),
    ("milestone-no-evidence-arm", "milestone"),
    ("opportunity-arm-communications-no-writer", "writer"),
])
def test_every_known_write_failure_names_a_concrete_next_step(code,
                                                               must_mention):
    """A2: the write-failure tier must explain itself like its siblings do."""
    import sys as _sys
    _sys.path.insert(0, str(REPO / "lib"))
    import cmd_tool
    message = cmd_tool.explain_write_failure(code)
    assert code not in message or must_mention in message, message
    assert must_mention in message, f"{code} -> {message}"
    assert len(message) > 20, message


def test_an_unknown_write_failure_code_still_says_where_to_look():
    """An exhaustive table is impossible (codes are generated per class), so the
    fallback must be usable rather than a bare slug echo."""
    import sys as _sys
    _sys.path.insert(0, str(REPO / "lib"))
    import cmd_tool
    message = cmd_tool.explain_write_failure("something-brand-new")
    assert "something-brand-new" in message
    assert "occupation" in message, "the fallback should name where codes come from"


def test_the_write_failure_message_is_not_the_bare_reason_slug(tmp_path):
    """End-to-end: the user-facing line must not be just the internal code."""
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [], "documents": []}), encoding="utf-8")
    assert bash(tmp_path, "tool", "add", "mcp__x__send_thing", "--purpose",
                "送る", "--effect", "outward", "--record",
                "evidence").returncode == 0
    r = bash(tmp_path, "tool", "used", "mcp__x__send_thing", "--summary", "s")
    assert r.returncode != 0
    assert "記録しませんでした: no-target" not in r.stderr, (
        "the raw slug is still the whole message")
    assert "milestone start" in r.stderr


def test_the_debt_list_explains_the_write_failure_too(tmp_path):
    """The same code also surfaces in `tool unregistered`; it must be explained
    in both places, not just at the moment of failure."""
    (tmp_path / ".beacon").mkdir(parents=True)
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "summary": "",
        "milestones": [], "documents": []}), encoding="utf-8")
    assert bash(tmp_path, "tool", "add", "mcp__x__send_thing", "--purpose",
                "送る", "--effect", "outward", "--record",
                "evidence").returncode == 0
    payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s",
                          "cwd": str(tmp_path),
                          "tool_name": "mcp__x__send_thing", "tool_input": {}})
    subprocess.run([str(REPO / "bin" / "beacon-save-hook.sh")], input=payload,
                   capture_output=True, text=True, timeout=60,
                   cwd=str(tmp_path))
    out = bash(tmp_path, "tool", "unregistered").stdout
    assert "milestone start" in out, out
