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


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "_cli_help_flag_drift_e6611", CHECKER
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _strict_exit(cwd: Path = ROOT) -> int:
    """Run the checker the way CI does (scripts/ci-strict-drift-guards.sh).

    Read-only: used to assert the real tree is clean. Injection cases do NOT go
    through here — see the test-the-test section for why they must not.
    """
    return subprocess.run(
        [sys.executable, str(CHECKER), "--strict"],
        cwd=str(cwd), capture_output=True, text=True,
    ).returncode


def _with_registry(mod, mutate):
    """Run ``collect_help_flag_drift`` against a mutated copy of the registry.

    ``mutate`` receives the real registry rows (a list of dicts) and returns the
    rows to judge instead. Nothing on disk is touched: the seam is
    ``_registry_entries``, so the injection lives entirely in memory.
    """
    rows = [dict(e) for e in mod._registry_entries()]
    injected = mutate(rows)
    original = mod._registry_entries
    mod._registry_entries = lambda *a, **k: injected
    try:
        return mod.collect_help_flag_drift()
    finally:
        mod._registry_entries = original


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
    # Keep the allowlist itself honest: keys are verb paths (NOT display
    # strings — see ALLOW_ADVERTISED_FLAG's note), values are long-flag sets.
    for key, flags in mod.ALLOW_ADVERTISED_FLAG.items():
        assert isinstance(key, tuple) and key and all(isinstance(k, str) for k in key), key
        assert not key[0].startswith("beacon"), (
            f"{key!r} looks like a display string; key by verb path, e.g. ('doc', 'add')"
        )
        assert flags and all(f.startswith("--") for f in flags), (key, flags)


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
    assert "--title" in mod.ALLOW_ADVERTISED_FLAG.get(("doc", "add"), set()), (
        "either `beacon doc add --title` is fixed in the registry — then delete "
        "its ALLOW_ADVERTISED_FLAG row and this test — or the suppression is "
        "missing and the guard is not covering the reported case"
    )
    # With the suppression lifted, the guard must actually name it.
    original = dict(mod.ALLOW_ADVERTISED_FLAG)
    try:
        mod.ALLOW_ADVERTISED_FLAG.pop(("doc", "add"))
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
# A-1 regression: prose must never register as an implemented flag.
# ---------------------------------------------------------------------------
#
# The flag-label pattern matches a label position (line start, after ``in``,
# after ``;;``) rather than only line start, because bash writes short arms
# inline. That un-anchoring opened a FALSE-PASS door: a comment or a usage
# ``echo`` containing the characters ``in --notaflag)`` made the guard believe
# the CLI implements ``--notaflag``, so a genuinely nonexistent advertised flag
# would pass silently. Found with a working reproduction by the independent AX
# review of PR #771. A detector that cannot fail is worse than none, so each
# vector is pinned.

def test_comment_text_is_not_read_as_a_flag():
    mod = _load_checker()
    assert mod._case_flags("# usage note: pass values in --notaflag) form\n") == set()


def test_echo_string_is_not_read_as_a_flag():
    mod = _load_checker()
    assert mod._case_flags('echo "  ;; --anotherfake) was removed"\n') == set()
    assert mod._case_flags('echo "see: case $1 in --ghostflag) ..."\n') == set()


def test_real_label_survives_a_trailing_comment():
    """Blanking prose must not cost us the real flag on the same line."""
    mod = _load_checker()
    flags = mod._case_flags("    --real) x=1; shift ;;  # or in --fake) form\n")
    assert flags == {"--real"}


def test_prose_blanking_preserves_offsets():
    """Arm boundaries are offsets into the stripped text; a stripper that
    changed line lengths would misalign every slice."""
    mod = _load_checker()
    src = 'a="xx"  # comment in --f)\n  --real) y=1 ;;\n'
    for keep in (False, True):
        out = mod._strip_comments_and_strings(src, keep_strings=keep)
        assert len(out) == len(src)
        assert out.count("\n") == src.count("\n")


# ---------------------------------------------------------------------------
# Test-the-test: each failure direction must actually turn the gate red.
# ---------------------------------------------------------------------------
#
# These inject drift IN MEMORY (the ``_registry_entries`` / allowlist seams),
# never by writing to lib/commands.py or to this checker. An earlier draft did
# mutate those tracked files and restore them in ``finally``, which the
# independent maintainability review of PR #771 flagged: a timeout, Ctrl-C, OOM
# or CI job kill between write and restore leaves a corrupted source on disk —
# and for the checker itself that breaks every OTHER drift check sharing the
# file, with nothing anywhere saying why. In-memory injection removes the write
# step rather than trying to make it survivable.


def test_tree_is_green_through_the_real_ci_path():
    """The gate CI actually runs is green on this tree (read-only)."""
    assert _strict_exit() == 0


def test_ghost_flag_injection_is_detected():
    """A newly advertised nonexistent flag is reported."""
    mod = _load_checker()

    def mutate(rows):
        for row in rows:
            if row.get("command") == "beacon doc list":
                row["flags"] = list(row.get("flags", [])) + ["--sort-by <field>"]
        return rows

    report = _with_registry(mod, mutate)
    assert "beacon doc list --sort-by" in report["ghost_flags"]
    assert report["ok"] is False


def test_resurrecting_a_fixed_drift_is_detected():
    """`stop scoped --kind` (fixed in e-6611) cannot come back silently."""
    mod = _load_checker()

    def mutate(rows):
        for row in rows:
            if row.get("command", "").startswith("beacon stop scoped"):
                row["flags"] = ["--kind ms|task|session", "--reason <text>", "--json"]
        return rows

    report = _with_registry(mod, mutate)
    assert "beacon stop scoped --kind" in report["ghost_flags"]
    assert report["ok"] is False


def test_stale_allowlist_row_is_detected():
    """An allowlist row covering an accepted flag fails, so a fixed drift
    cannot leave its suppression behind."""
    mod = _load_checker()
    original = dict(mod.ALLOW_ADVERTISED_FLAG)
    try:
        mod.ALLOW_ADVERTISED_FLAG[("pr", "add")] = {"--author"}
        report = mod.collect_help_flag_drift()
        assert "beacon pr add --author" in report["stale_allowlist"]
        assert report["ok"] is False
    finally:
        mod.ALLOW_ADVERTISED_FLAG.clear()
        mod.ALLOW_ADVERTISED_FLAG.update(original)


def test_not_ok_report_makes_strict_exit_nonzero():
    """The last link: a not-ok report must become a non-zero exit, or every
    detection above would still let CI pass."""
    mod = _load_checker()
    original = mod.collect_drift
    mod.collect_drift = lambda *a, **k: {
        "ok": False, "missing_from_bin_help": [], "missing_from_help_json": [],
        "missing_from_readme": [], "ghost_flags": ["beacon x y --z"],
        "stale_advertised_flag_allowlist": [],
    }
    try:
        assert mod.main(["--strict"]) == 1
        assert mod.main([]) == 0, "without --strict the checker stays advisory"
    finally:
        mod.collect_drift = original


def test_allowlist_key_survives_a_display_rename():
    """The allowlist is keyed by verb path, so rewording a row's placeholders
    must not make the same flag show up as a new ghost AND a stale row."""
    mod = _load_checker()

    def mutate(rows):
        for row in rows:
            if row.get("command") == "beacon doc add":
                row["command"] = "beacon doc add <title>"
        return rows

    report = _with_registry(mod, mutate)
    assert report["ghost_flags"] == []
    assert report["stale_allowlist"] == []


def test_tree_still_green_after_injections():
    """No injection leaked: every seam above restored, nothing written to disk."""
    assert _strict_exit() == 0
    assert _load_checker().collect_help_flag_drift()["ok"] is True
