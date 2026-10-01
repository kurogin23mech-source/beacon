"""Process-wide read-only gate for the CLI dispatch chokepoint (ms-160 e-6715).

Some callers invoke a verb for a reason that is *not* "perform this verb":

  * ``--help`` fell through to a command's own parser because the help registry
    had no entry (ms-178 e-6654). A parser that ignores trailing flags then
    EXECUTES — ``beacon note clear --help`` deleted every session note that way.
  * the AX full-surface audit probes each command group with a bogus subcommand
    to see its usage / error surface (``commands._collect_surface_snapshot``).
    ``beacon note`` takes free text as its positional, so the bogus token was
    not rejected as bogus — it was *saved as a real note*. 17 of the 48 notes in
    the live project store were this probe string.

Both are the same shape: an inspection path that must not be able to write. So
the gate is one chokepoint with several *reasons*, not a special case per
caller — a command added later is covered without anyone remembering to.

**Fail-closed on the ledger, not on a list of dangerous verbs.** The allow test
is ``verb_ledger.classify(verb) == "Q"`` (read-only). ``verb_ledger.reconcile()``
is pinned against the live dispatch surface, so a new verb shows up as
unclassified — and unclassified is refused, because "not yet classified" is not
evidence of safety.

Pure logic + a tiny env read: no I/O, no sys.exit, so the policy is testable
without a subprocess. The caller (``commands.main``) does the printing/exiting.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

# Why the gate is active. Each reason owns how a refusal is reported, because
# the two callers need opposite things:
#
#   help  — the operator asked for an explanation. Exit 0 on stdout: "I did not
#           run it" IS the successful answer to that question.
#   probe — a machine is sampling the error surface. A refusal must be loud and
#           non-zero, or the snapshot would record exit-0-with-no-stderr and
#           report the command as a *silent no-op* — inventing the very defect
#           the audit exists to find.
REASON_HELP = "help"
REASON_SURFACE_PROBE = "surface_probe"

# Env var per reason. Set by bin/beacon (help) and by the snapshot collector
# (probe); exported into the whole process so every front end inherits it.
REASON_ENV = {
    REASON_HELP: "BEACON_HELP_ONLY",
    REASON_SURFACE_PROBE: "BEACON_SURFACE_PROBE",
}

# Every env var that can put this process into a read-only reason. A caller that
# spawns a child under ONE reason must strip the others first, or reason priority
# in active_reason() silently decides which refusal shape the child gets. Derived
# from REASON_ENV so a new reason cannot be forgotten here.
ALL_REASON_ENV_VARS = frozenset(REASON_ENV.values())

# Back-compat alias for the pre-e-6715 private name.
_REASON_ENV = REASON_ENV

# Exit code a probe refusal uses. The snapshot matches on this rather than on
# the message text, so re-wording the refusal cannot silently break detection.
# 97 is outside the range any beacon command returns deliberately (0/1/3).
PROBE_REFUSAL_EXIT = 97


def active_reason(env=None) -> Optional[str]:
    """Which read-only reason is in force, or None. ``help`` wins if both are
    set (it is the one with a user waiting for output)."""
    e = os.environ if env is None else env
    for reason in (REASON_HELP, REASON_SURFACE_PROBE):
        if e.get(REASON_ENV[reason]) == "1":
            return reason
    return None


def verb_is_read_only(verb_key: str) -> bool:
    """True only for a verb the ledger classifies Q (read-only).

    Any failure to classify — unknown verb, ledger import error — answers False.
    The gate must not be weakened by the ledger being unavailable.
    """
    try:
        from verb_ledger import classify
        entry = classify(verb_key)
    except Exception:
        return False
    return bool(entry) and entry.get("cls") == "Q"


def refusal(verb_key: str, reason: str, help_query: str = "") -> Tuple[str, bool, int]:
    """Render the refusal for ``verb_key`` under ``reason``.

    Returns ``(message, to_stderr, exit_code)``. The help wording is unchanged
    from ms-178 e-6654 (its tests pin "NOT executed" on stdout, exit 0).
    """
    if reason == REASON_SURFACE_PROBE:
        path = (help_query or verb_key.replace("_", " ")).strip()
        return (
            "beacon {0} — refused: the AX surface probe may only run read-only "
            "commands, and this verb is not one (ledger class != Q).\n"
            "This is the probe working as intended: the command's real behaviour "
            "would have written to the project.".format(path),
            True,
            PROBE_REFUSAL_EXIT,
        )
    path = (help_query or verb_key.replace("_", " ")).strip()
    return (
        "beacon {0} — no help entry is registered for this command, and it is "
        "not read-only, so it was NOT executed.\n"
        "Run 'beacon help' for the command list, or re-run without --help to "
        "actually perform it.".format(path),
        False,
        0,
    )
