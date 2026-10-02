"""`--help` must never destroy data (ms-178 e-6654).

`beacon note clear --help` used to DELETE every session note. The cause was not
a missing guard: bin/beacon already reserves -h/--help ahead of the parsers
(ms-120 e-3897). That guard has a fall-through — when the help registry has no
entry for the command, `help_render` exits 3 and the bash dispatcher continues
into the command's own arm so bash-only commands (bus / trek) keep their bespoke
help. The fall-through is safe only for a parser that actually reads -h/--help;
`note clear` ignored trailing tokens and executed instead of explaining.

`note` was absent from the 170-entry registry, so it took that path every time.
The real damage was not the local file (moved to .bak) but the CLOUD notes:
that store is shared by every session on the project, so one session asking for
help wiped the other sessions' handoff notes (observed 2026-09-28).

Two layers are pinned here:
  1. `note` is registered, so --help renders real usage and never falls through.
  2. The fall-through itself is fail-closed at the python chokepoint: under
     BEACON_HELP_ONLY only a Q (read-only) verb may run. That closes the CLASS —
     a future destructive verb missing from the registry degrades help, not data.
Plus the confirmation contract: `note clear` requires an explicit -y/--yes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
COMMANDS_PY = ROOT / "lib" / "commands.py"
BASH = shutil.which("bash")

sys.path.insert(0, str(ROOT / "lib"))

# ms-160 e-6714 scoped `note clear`'s DEFAULT to the calling session's own
# notes. This file is about the --help / confirmation gate on the SHARED store,
# which is now spelled `--all`; the gate's contract is unchanged, only the verb
# that reaches it. The note is left untagged on purpose — it stands for
# "somebody else's / legacy", which the scoped default must never assume.
NOTE_LINE = json.dumps({"ts": "2026-09-29T10:00:00+0900", "text": "handoff"}) + "\n"
CLEAR_ALL = ["clear", "--all"]


@pytest.fixture
def project(tmp_path):
    """A local-mode project (no cloud.json) holding one session note."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8"
    )
    notes = beacon / "session_notes.jsonl"
    notes.write_text(NOTE_LINE, encoding="utf-8")
    return tmp_path, notes


def _run(cwd, *args, env=None):
    e = dict(os.environ)
    e.pop("BEACON_HELP_ONLY", None)
    e.pop("BEACON_NOTE_CLEAR_YES", None)
    if env:
        e.update(env)
    return subprocess.run(
        [BASH, str(BIN), *args], cwd=str(cwd), capture_output=True, text=True, env=e
    )


# --- layer 1: asking for help never destroys ------------------------------

@pytest.mark.skipif(BASH is None, reason="bash required")
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_note_clear_help_does_not_delete_notes(project, flag):
    """The named breakage: `note clear --help` must explain, not wipe."""
    cwd, notes = project
    r = _run(cwd, "note", *CLEAR_ALL, flag)
    assert notes.exists(), (
        f"`beacon note clear {flag}` DELETED the session notes — asking for help "
        "must never mutate data (e-6654)"
    )
    assert notes.read_text(encoding="utf-8") == NOTE_LINE
    assert "clear" in r.stdout.lower(), r.stdout + r.stderr
    assert "cleared" not in r.stdout.lower(), (
        "clear actually ran under --help: " + r.stdout
    )


@pytest.mark.skipif(BASH is None, reason="bash required")
def test_note_list_help_renders_usage_not_the_notes(project):
    """`note list --help` dumped the notes as JSON, because the old dispatcher
    read ANY third token as "json mode" (`${3:+1}`). Help is not a data query."""
    cwd, _ = project
    r = _run(cwd, "note", "list", "--help")
    assert "handoff" not in r.stdout, (
        "`note list --help` printed note CONTENT instead of usage: " + r.stdout
    )
    assert "--json" in r.stdout, r.stdout + r.stderr


@pytest.mark.skipif(BASH is None, reason="bash required")
def test_note_help_lists_subcommands(project):
    """`beacon note --help` used to point at itself ("run 'beacon note --help'"),
    a circular non-answer. It now renders the noun's subcommands."""
    cwd, _ = project
    r = _run(cwd, "note", "--help")
    out = r.stdout
    assert "beacon note list" in out and "beacon note clear" in out, out + r.stderr


# --- layer 2: the confirmation contract ------------------------------------

@pytest.mark.skipif(BASH is None, reason="bash required")
def test_bare_note_clear_refuses_without_yes(project):
    """clear removes the project's SHARED cloud notes too, so a bare verb must
    not be enough. Refusal is non-zero and names the count + recovery path."""
    cwd, notes = project
    r = _run(cwd, "note", *CLEAR_ALL)
    assert r.returncode != 0, "bare `note clear` succeeded silently: " + r.stdout
    assert notes.exists(), "bare `note clear` deleted the notes"
    assert "--yes" in r.stderr, r.stderr
    assert "1 session note" in r.stderr, r.stderr


@pytest.mark.skipif(BASH is None, reason="bash required")
@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_note_clear_with_yes_still_clears(project, flag):
    """The guard must not break the intended operation (it is a confirmation,
    not a removal): with --yes the notes are cleared and .bak is left behind."""
    cwd, notes = project
    r = _run(cwd, "note", *CLEAR_ALL, flag)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not notes.exists(), "clear --yes did not clear: " + r.stdout
    assert notes.with_suffix(".jsonl.bak").exists(), "local backup was not kept"


# --- layer 2: the chokepoint is fail-closed, and it really fires -----------

def test_help_only_refuses_destructive_verb_even_with_confirmation(project):
    """A guard that cannot fail is useless. Drive the chokepoint directly: with
    BEACON_HELP_ONLY set, note_clear (ledger class B) must refuse even though
    the confirmation env is ALSO set — i.e. the refusal comes from the help gate,
    not from the missing --yes."""
    cwd, notes = project
    r = subprocess.run(
        [sys.executable, str(COMMANDS_PY), "note_clear"],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, "BEACON_HELP_ONLY": "1", "BEACON_NOTE_CLEAR_YES": "1"},
    )
    assert notes.exists(), "HELP_ONLY did not stop a destructive verb: " + r.stdout
    assert "NOT executed" in r.stdout, r.stdout + r.stderr


def test_help_only_still_allows_read_only_verb(project):
    """Fail-closed must not mean fail-always: a Q verb renders under HELP_ONLY,
    which is what keeps bespoke help on read-only bash commands working."""
    cwd, _ = project
    r = subprocess.run(
        [sys.executable, str(COMMANDS_PY), "note_list"],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, "BEACON_HELP_ONLY": "1"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "handoff" in r.stdout, r.stdout + r.stderr


def test_every_destructive_note_verb_is_classified_non_q():
    """The gate reads the verb ledger, so it is only as good as the ledger's
    coverage. Pin the note family's classes: add/clear must NOT be Q (or the
    chokepoint would wave them through), list must be Q (or help would break)."""
    from verb_ledger import classify

    assert (classify("note_clear") or {}).get("cls") != "Q"
    assert (classify("note_add") or {}).get("cls") != "Q"
    assert (classify("note_list") or {}).get("cls") == "Q"


def test_unclassified_verb_is_treated_as_unsafe():
    """Fail-closed on the ledger itself: a verb the ledger does not know is
    treated as unsafe, so forgetting to classify a new verb cannot open the
    hole back up. `classify` returning None is the condition the gate relies on."""
    from verb_ledger import classify

    assert classify("definitely_not_a_real_verb_e6654") is None


# --- both CLI frontends agree (ms-178 e-6654) ------------------------------

def _dispatch_note(cwd, *argv):
    """Drive the OTHER frontend (beacon_cli/dispatch.py, the Windows/Codex
    path) in-process. `root` only locates commands.py; the notes file still
    comes from cwd, so the temp project stays isolated."""
    code = (
        "import sys, pathlib\n"
        "from beacon_cli.dispatch import dispatch\n"
        f"sys.exit(dispatch(pathlib.Path({str(ROOT)!r}), sys.argv[1:]))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "lib")])
    env.pop("BEACON_NOTE_CLEAR_YES", None)
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        cwd=str(cwd), capture_output=True, text=True, env=env,
    )


def test_dispatch_frontend_also_refuses_bare_clear(project):
    """Beacon has two CLI frontends (bin/beacon bash + beacon_cli/dispatch.py).
    A gate on one only is a gate the other walks around, so pin the second."""
    cwd, notes = project
    r = _dispatch_note(cwd, "note", *CLEAR_ALL)
    assert r.returncode != 0, r.stdout + r.stderr
    assert notes.exists(), "dispatch.py cleared notes without --yes"
    assert "--yes" in r.stderr, r.stderr


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_dispatch_frontend_clears_with_yes(project, flag):
    """...and that the second frontend can still actually clear (it must pass
    the confirmation through, not merely fail to)."""
    cwd, notes = project
    r = _dispatch_note(cwd, "note", *CLEAR_ALL, flag)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not notes.exists(), "dispatch.py --yes did not clear: " + r.stdout


def test_dispatch_frontend_help_does_not_clear(project):
    cwd, notes = project
    r = _dispatch_note(cwd, "note", "clear", "--help")
    assert notes.exists(), "dispatch.py `note clear --help` deleted the notes"
    assert "--yes" in r.stdout, r.stdout + r.stderr
