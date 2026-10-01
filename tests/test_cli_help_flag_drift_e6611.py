"""help registry ↔ parser flag drift guard (ms-133 e-6611).

e-3897 made every help surface render from one registry, so help can no longer
drift against *other help*. What stayed open is help drifting against the
*parsers*: the registry is hand-written, so it can advertise a flag no entry
point accepts. The reported case was ``beacon doc add --title X`` — help said
``--title <title>``, both fronts take ``title`` as a positional, and the call
dies with "'--title' is not a valid flag". AI agents trust help and get
rejected (AX 原則 1/3; 原則 6 = close it structurally, not by proofreading).

These pin the guard AND the guard's own teeth. A drift detector that cannot be
shown to go red on drift is worse than none: it reads as a safety net while
silently passing everything. So alongside "the tree is green" there are
injection tests for each failure direction — a ghost flag, a resurrected
already-fixed drift, and an allowlist row that outlived its drift.

Also pinned are the three bash parser shapes whose mis-reading produced FALSE
findings during development, because each one, left unhandled, reports a flag
the CLI really accepts as nonexistent:

  * a case label leading with a short alias (``-r|--reason)``);
  * an inline single-line case (``case "$1" in --json) f=1 ;; *) ;; esac``);
  * a verb that ``exec``s into another path instead of parsing (``dm send`` →
    ``bus send``), and a flag read by a ``[[ "$2" == "--json" ]]`` test.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check-cli-help-drift.py"
COMMANDS_PY = ROOT / "lib" / "commands.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "_cli_help_flag_drift_e6611", CHECKER
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _strict_exit(cwd: Path = ROOT) -> int:
    """Run the checker the way CI does (scripts/ci-strict-drift-guards.sh)."""
    return subprocess.run(
        [sys.executable, str(CHECKER), "--strict"],
        cwd=str(cwd), capture_output=True, text=True,
    ).returncode


# ---------------------------------------------------------------------------
# The contract: the tree is clean.
# ---------------------------------------------------------------------------

def test_no_registry_flag_is_unaccepted_by_every_front():
    """Every advertised long flag is accepted by bash or by Python argparse."""
    mod = _load_checker()
    report = mod.collect_help_flag_drift()
    assert report["ghost_flags"] == [], (
        "help advertises flags no front accepts: " + ", ".join(report["ghost_flags"])
    )


def test_allowlist_has_no_stale_entries():
    """A fixed drift must drop its allowlist row, else the next regression on
    that same flag passes silently."""
    mod = _load_checker()
    assert _load_checker().collect_help_flag_drift()["stale_allowlist"] == []
    # Keep the allowlist itself honest: entries are per-command flag sets.
    for command, flags in mod.ALLOW_ADVERTISED_FLAG.items():
        assert command.startswith("beacon "), command
        assert flags and all(f.startswith("--") for f in flags), (command, flags)


def test_every_registry_command_path_resolves_to_some_front():
    """No advertised command is unreachable in BOTH fronts.

    ``unresolved`` is not a hard failure in the checker (prose-shaped rows exist
    and sub-verb parity owns "command missing"), but it must stay empty here: a
    path that resolves nowhere also means its flags were never really checked,
    which would quietly shrink this guard's coverage.
    """
    mod = _load_checker()
    assert mod.collect_help_flag_drift()["unresolved"] == []


def test_e6611_reported_case_is_detected_not_merely_absent():
    """The reported ``doc add --title`` is a flag the guard really catches.

    The registry FIX for this row belongs to open PR #680, so this branch leaves
    the line alone and suppresses it via ALLOW_ADVERTISED_FLAG (editing the same
    line here would collide with that PR). What must hold is that the drift is
    *known*, not that it is gone: remove the suppression and the guard names it.

    Nothing here needs updating when #680 merges —
    ``test_allowlist_has_no_stale_entries`` turns red the moment the registry row
    is fixed, which is what forces the allowlist row to be deleted. That red is
    the intended handoff signal, not a surprise.
    """
    mod = _load_checker()
    assert "--title" in mod.ALLOW_ADVERTISED_FLAG.get("beacon doc add", set()), (
        "either `beacon doc add --title` is fixed in the registry — then delete "
        "its ALLOW_ADVERTISED_FLAG row and this test — or the suppression is "
        "missing and the guard is not covering the reported case"
    )
    # With the suppression lifted, the guard must actually name it.
    original = dict(mod.ALLOW_ADVERTISED_FLAG)
    try:
        mod.ALLOW_ADVERTISED_FLAG.pop("beacon doc add")
        report = mod.collect_help_flag_drift()
        assert "beacon doc add --title" in report["ghost_flags"]
    finally:
        mod.ALLOW_ADVERTISED_FLAG.clear()
        mod.ALLOW_ADVERTISED_FLAG.update(original)


# ---------------------------------------------------------------------------
# Parser-shape regressions that produce FALSE findings.
# ---------------------------------------------------------------------------

def test_short_alias_first_case_label_is_read():
    """``-r|--reason)`` — anchoring the flag pattern on ``--`` dropped the whole
    alias group, reporting a real flag as nonexistent."""
    mod = _load_checker()
    flags = mod.bash_flags_for_path(["milestone", "wait"])
    assert flags is not None and "--reason" in flags


def test_inline_single_line_case_is_read():
    """``case "$1" in --json) ... ;; *) ... ;; esac`` on one line — how
    ``sales target list`` takes ``--json``."""
    mod = _load_checker()
    flags = mod.bash_flags_for_path(["sales", "target", "list"])
    assert flags is not None and "--json" in flags


def test_exec_delegation_is_followed():
    """``dm send`` execs ``bus send``; its flags live on the delegate."""
    mod = _load_checker()
    flags = mod.bash_flags_for_path(["dm", "send"])
    assert flags is not None
    assert {"--to-user", "--recipient-confirmed"} <= flags


def test_flag_read_by_a_test_compare_is_read():
    """``beacon help --json`` is dispatched by ``[[ "$2" == "--json" ]]``."""
    mod = _load_checker()
    flags = mod.bash_flags_for_path(["help"])
    assert flags is not None and "--json" in flags


def test_assignment_is_not_mistaken_for_a_parsed_flag():
    """The test-compare reader must not accept ``x="--json"``: over-accepting
    turns a real finding into a silent pass."""
    mod = _load_checker()
    assert mod._test_compare_flags('default="--not-a-flag"\n') == set()
    assert mod._test_compare_flags('if [[ "$1" == "--real" ]]; then\n') == {"--real"}


def test_sibling_arm_flags_do_not_leak_into_a_path():
    """``cmd_doc()`` holds ``--title`` for ``doc update``. Scanning the whole
    function would call the advertised ``doc add --title`` real — the exact
    false pass that would have hidden e-6611."""
    mod = _load_checker()
    add = mod.bash_flags_for_path(["doc", "add"])
    update = mod.bash_flags_for_path(["doc", "update"])
    assert add is not None and update is not None
    assert "--title" in update
    assert "--title" not in add


def test_main_dispatch_switch_is_the_last_column0_case():
    """bin/beacon has an earlier column-0 ``case`` (project-root relocation).
    Picking the first makes every inline noun resolve as absent."""
    mod = _load_checker()
    for noun in ("claim", "stop", "resume", "trek"):
        flags = mod.bash_flags_for_path([noun])
        assert flags is not None, f"inline noun {noun!r} did not resolve in bash"


# ---------------------------------------------------------------------------
# Test-the-test: each failure direction must actually turn the gate red.
# ---------------------------------------------------------------------------

def _mutate(path: Path, old: str, new: str):
    original = path.read_text(encoding="utf-8")
    assert original.count(old) == 1, f"anchor not unique in {path.name}: {old[:60]!r}"
    path.write_text(original.replace(old, new, 1), encoding="utf-8")
    return original


def test_green_before_injection():
    assert _strict_exit() == 0, "tree must be clean before the injection tests"


def test_ghost_flag_injection_turns_the_gate_red():
    """A newly advertised nonexistent flag fails CI."""
    old = '{"command": "beacon doc list", "flags": ["--json", "--scope <scope>", "--ms <id>"]'
    new = ('{"command": "beacon doc list", "flags": ["--json", "--scope <scope>", '
           '"--ms <id>", "--sort-by <field>"]')
    original = _mutate(COMMANDS_PY, old, new)
    try:
        mod = _load_checker()
        report = mod.collect_help_flag_drift()
        assert "beacon doc list --sort-by" in report["ghost_flags"]
        assert _strict_exit() == 1
    finally:
        COMMANDS_PY.write_text(original, encoding="utf-8")


def test_resurrecting_a_fixed_drift_turns_the_gate_red():
    """`stop scoped --kind` (fixed in e-6611) cannot come back silently."""
    old = ('"flags": ["--target <ms|task|session>:<id>", "--reason-kind <k>", '
           '"--reason <text>", "--machine-reason <json>", "--json"]')
    new = '"flags": ["--kind ms|task|session", "--reason-kind <k>", "--reason <text>", "--json"]'
    original = _mutate(COMMANDS_PY, old, new)
    try:
        mod = _load_checker()
        assert "beacon stop scoped --kind" in mod.collect_help_flag_drift()["ghost_flags"]
        assert _strict_exit() == 1
    finally:
        COMMANDS_PY.write_text(original, encoding="utf-8")


def test_stale_allowlist_row_turns_the_gate_red():
    """An allowlist row covering a flag that is actually accepted fails, so a
    fixed drift cannot leave its suppression behind."""
    old = '    "beacon milestone list": {"--json"},'
    new = old + '\n    "beacon pr add <github-url>": {"--author"},'
    original = _mutate(CHECKER, old, new)
    try:
        mod = _load_checker()
        stale = mod.collect_help_flag_drift()["stale_allowlist"]
        assert "beacon pr add <github-url> --author" in stale
        assert _strict_exit() == 1
    finally:
        CHECKER.write_text(original, encoding="utf-8")


def test_green_after_injections():
    """Every injection restored its file; a leaked mutation would show here."""
    assert _strict_exit() == 0
