"""bash ↔ Python SUB-verb parity guard (ms-133 e-4642).

The pre-existing dispatch check compares only TOP-LEVEL verbs, so a sub-verb
that is an argparse subparser choice on bin/beacon but not on
beacon_cli/dispatch.py (=> `argparse invalid choice` for Windows/pipx users)
slipped through — that was the 55+ drift the 2026-07-30 audit found. These pin
the extension that closes it:

  * the bash inner-case parser and the Python subparser-choice reader agree on
    real sub-verbs;
  * nouns dispatched via a permissive positional (`note`, `sessions`) are
    exempt by construction (they can't `invalid choice`) — the false-positive
    guard;
  * the current tree is green against the snapshot, and REMOVING a snapshot
    entry turns it red (so e-4643's backfill is genuinely guarded, not a no-op);
  * the stale `sales`/`org` top-level allowlist entries are gone (AC5).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "_cli_help_drift_e4642", ROOT / "scripts" / "check-cli-help-drift.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_bash_parser_extracts_inner_case_sub_verbs():
    """The nested-case walker pulls the real sub-verb labels for a noun,
    splitting union labels and preserving hyphen spelling."""
    mod = _load_checker()
    parser = mod._load_dispatch_parser()
    amap = mod._noun_alias_map(parser)
    bash = mod.parse_bin_sub_verbs(alias_map=amap)
    # phase list/add/rename/move/remove (+ delete/rm aliases from the union).
    assert {"list", "add", "rename", "move", "remove"} <= bash.get("phase", set())
    # task's inner case is routed the same way.
    assert {"add", "done", "list"} <= bash.get("task", set())
    # cloud's multi-line case bodies (label on its own line) are captured too —
    # the parser must not require `label) cmd ;;` on one physical line.
    assert {"join", "status"} <= bash.get("cloud", set())


def test_python_reader_sees_subparser_choices_only():
    """python_sub_verbs reports argparse subparser choices, and OMITS nouns that
    take a permissive positional (they dispatch manually, can't invalid-choice).
    This omission is the false-positive guard — without it `note clear` /
    `sessions list` would be reported as bogus Windows breakage."""
    mod = _load_checker()
    py = mod.python_sub_verbs()
    # phase is subparser-backed and exposes `list` (+ the e-4643 backfilled
    # add/rename/move/remove — we assert `list` presence, forward-compatible).
    assert "list" in py.get("phase", set())
    # milestone is subparser-backed and exposes many choices.
    assert {"add", "start", "done"} <= py.get("milestone", set())
    # `note` and `sessions` use a positional (text_or_sub / list_arg) => absent.
    assert "note" not in py
    assert "sessions" not in py


def test_noun_aliases_collapse_to_canonical():
    """ms→milestone, opp→opportunity, acc→account etc. collapse so an aliased
    noun doesn't double-count on either surface."""
    mod = _load_checker()
    amap = mod._noun_alias_map(mod._load_dispatch_parser())
    assert amap.get("ms") == "milestone"
    assert amap.get("opp") == "opportunity"
    assert amap.get("acc") == "account"
    # canonical maps to itself.
    assert amap.get("milestone") == "milestone"


def test_current_tree_is_green_against_snapshot():
    """With the e-4642 snapshot in place the sub-verb surface is clean."""
    mod = _load_checker()
    drift = mod.collect_subverb_drift()
    assert drift["ok"], (
        "unexpected sub-verb drift: "
        f"py={drift['missing_from_python_subverbs']} "
        f"bash={drift['missing_from_bash_subverbs']}"
    )


def test_removing_a_snapshot_entry_turns_it_red():
    """The snapshot is load-bearing: drop a profession-critical deferral and the
    checker must flag it. This is what makes e-4643's backfill a real,
    regression-guarded shrink rather than a silent no-op."""
    mod = _load_checker()
    # Use a still-deferred entry as the victim ("phase add" left the snapshot
    # once e-4643 backfilled it — its guard is now proven by parity, not the
    # snapshot). Any remaining bash-only sub-verb demonstrates the mechanism.
    victim = "trek block"
    assert victim in mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON
    original = set(mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON)
    try:
        mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON.discard(victim)
        drift = mod.collect_subverb_drift()
        assert not drift["ok"]
        assert victim in drift["missing_from_python_subverbs"]
    finally:
        mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON.clear()
        mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON.update(original)


def test_new_bash_only_sub_verb_is_detected():
    """A brand-new sub-verb added to bin/beacon but not to dispatch.py (and not
    snapshotted) is caught. We simulate by injecting into the bash-side map."""
    mod = _load_checker()
    parser = mod._load_dispatch_parser()
    amap = mod._noun_alias_map(parser)
    py = mod.python_sub_verbs(parser=parser)
    bash = mod.parse_bin_sub_verbs(alias_map=amap)
    # 'phase' is subparser-backed on Python; add a fake bash sub-verb.
    bash.setdefault("phase", set()).add("frobnicate")
    missing = set()
    for noun in set(py) & set(bash):
        for sub in bash[noun] - py[noun]:
            missing.add(f"{noun} {sub}")
    missing -= mod.ALLOW_SUBVERB_MISSING_FROM_PYTHON
    assert "phase frobnicate" in missing


def test_stale_sales_and_org_top_level_allowlist_removed():
    """AC5: `sales` and `org` gained Python top-level parity, so their stale
    ALLOW_BASH_ONLY_DISPATCH entries must be gone (else a future loss of parity
    would be masked)."""
    mod = _load_checker()
    assert "sales" not in mod.ALLOW_BASH_ONLY_DISPATCH
    assert "org" not in mod.ALLOW_BASH_ONLY_DISPATCH


def test_overall_checker_stays_green_strict():
    """The whole gate (all surfaces incl. the new sub-verb one) is green."""
    mod = _load_checker()
    report = mod.collect_drift()
    assert report["ok"], report
