"""Single source of truth for "the live CLI dispatch surface" — the set of verb
keys Beacon exposes (ms-114 e-3740).

Two independent readers need this set: the map-drift lint
(``scripts/check-map-drift.py``, which checks the map doc covers every verb) and
the Q/R/B/C classification ledger (``lib/verb_ledger.py``, which checks every
verb is classified). The enumeration used to be copied into both; this module
holds it ONCE so the two can never drift apart — the surface is defined in a
single place, and both callers import it.

Pure transform: it parses the ``commands = {...}`` dispatch dict out of
``lib/commands.py`` (that dict's textual layout is the de-facto registry of
subcommands). No I/O beyond reading that one source file.
"""

from __future__ import annotations

import os
import re

_LIB_DIR = os.path.dirname(os.path.abspath(__file__))

# dispatch keys that exist but are not user-facing subcommands (no map / ledger
# obligation). Keep in this one place — both readers exclude the same set.
CLI_INTERNAL = {
    "auth_check", "common_setup", "cloud_check_project",
    "retro_default_since", "help_json", "version",
}

# top-level verbs handled directly by bin/beacon (not in the dispatch dict).
CLI_SHELL_TOPLEVEL = {"status", "reset"}


def enumerate_cli_verbs(commands_path: str = "") -> set:
    """Return the live CLI dispatch surface: the keys of the ``commands = {}``
    dict in ``lib/commands.py`` (minus internal-only keys) plus the shell
    top-level verbs. Raises ``RuntimeError`` if the dispatch dict cannot be
    located (a loud failure is correct — the surface is unknown)."""
    path = commands_path or os.path.join(_LIB_DIR, "commands.py")
    src = open(path, encoding="utf-8").read()
    m = re.search(r"\n    commands = \{(.*?)\n    \}", src, re.S)
    if not m:
        raise RuntimeError(
            "dispatch dict `commands = {...}` を lib/commands.py に見つけられません")
    keys = set(re.findall(r'"([a-z0-9_]+)":', m.group(1)))
    return {k for k in keys if k not in CLI_INTERNAL} | CLI_SHELL_TOPLEVEL
