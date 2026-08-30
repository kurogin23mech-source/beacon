#!/usr/bin/env python3
"""CLI help drift detector for Beacon (ms-10 e-722, ms-44 e-1171).

Checks alignment between four independent "lists of subcommands":

  1. ``bin/beacon``                — the bash dispatcher's usage() text
                                     (what `beacon --help` prints).
  2. ``lib/commands.py``           — the ``cmd_help_json`` entries
                                     (what `beacon help --json` prints).
  3. ``README.md``                  — the ``### <Section>`` tables under
                                     ``## CLI Commands``.
  4. ``beacon_cli/dispatch.py``    — top-level verbs in the ``_HANDLERS``
                                     dict (what Windows pipx users get).
                                     Drift here = `argparse invalid choice`
                                     on Windows (ms-44 e-1171). Compared
                                     against the bash main case switch.

When a maintainer adds a subcommand, all four surfaces must be kept in
sync. This script extracts the "noun" pair (subcommand + subsubcommand,
e.g. ``milestone add``) from each source and reports any source that is
missing one.

The fourth check (dispatch parity) catches the specific drift that broke
Windows cross-machine DM in 2026-06-07: PR #74 added ``session id`` and
``channel install`` to bin/beacon (bash) only, so Windows pipx users hit
``argparse invalid choice: 'session'``. Adding to this lint forces both
sides to stay in step.

  5. ``REQUIRED_FLAG_PARITY``       — a *curated* bash ↔ Python **flag**
                                     parity check (ms-126 e-4223). The four
                                     checks above compare subcommands and
                                     ignore flags; this one names specific
                                     (verb, flag) pairs — seeded with the
                                     mandatory-priority contract (``--priority``
                                     / ``--untriaged``) — that must exist on
                                     BOTH the bash ``cmd_<verb>()`` arg-loop and
                                     the Python subparser, catching a flag added
                                     to one surface and silently forgotten on
                                     the other.

Apart from that curated set, the checker deliberately ignores positional
arg shape and general flag spelling — a blanket flag diff is too noisy, and
the dispatcher itself enforces ``--flag`` parsing. The drift these catch is
the common one in practice: a brand-new subcommand that nobody added to the
README or to ``cmd_help_json``, or a load-bearing flag that lands on only one
of the two dispatch surfaces.

Allowlists
----------
Some entries intentionally live in only one place:

* ``DOC_ONLY``       — long-form examples in README that aren't actual
                       runnable subcommands (e.g. ``beacon milestone add ...
                       [--owner U]`` is just an option-set hint, not a
                       different verb).
* ``BIN_ONLY``       — verbs that exist in the bash dispatcher but
                       intentionally omitted from --help / README
                       (internal / deprecated / alias).
* ``HELPJSON_ONLY``  — entries we want in machine-readable help but not
                       in user-facing prose.

Adding/removing an item here is itself a reviewable change.

Run modes
---------
``--warn``  (default): print mismatches, exit 0 (for pre-commit).
``--strict``         : print mismatches, exit 1 (for CI gate).
``--json``           : machine-readable output.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_BEACON = ROOT / "bin" / "beacon"
# ms-127 e-4867: bin/beacon's noun-family cmd_<verb>() bodies are being split
# out into bin/lib/cmd_*.sh and `source`d back at runtime (bash god-module
# split B phase). The dispatch `case` stays in bin/beacon, but the *function
# bodies* move here — so any scan that slices a cmd_<verb>() definition (flag
# parity) must read bin/beacon AND these sourced files as one logical surface.
BIN_LIB_DIR = ROOT / "bin" / "lib"
COMMANDS_PY = ROOT / "lib" / "commands.py"
README = ROOT / "README.md"
PYTHON_DISPATCH = ROOT / "beacon_cli" / "dispatch.py"


def _bash_function_source(bin_path: Path = BIN_BEACON) -> str:
    """Combined bash source where ``cmd_<verb>()`` bodies may live.

    Returns ``bin/beacon`` concatenated with every ``bin/lib/cmd_*.sh`` family
    file (sorted for determinism). A family function's slice end is still the
    next ``^cmd_…()`` header, which concatenation preserves across file joins,
    so flag scanning is unaffected by *where* a function physically lives. The
    dispatch ``case`` block (scanned elsewhere) stays in bin/beacon and is not
    duplicated by this join.
    """
    parts = [bin_path.read_text(encoding="utf-8")]
    lib_dir = bin_path.parent / "lib"
    if lib_dir.is_dir():
        for family in sorted(lib_dir.glob("cmd_*.sh")):
            parts.append(family.read_text(encoding="utf-8"))
    # Join with a newline so the last line of one file can't fuse with the
    # first line of the next (line-anchored regexes depend on real boundaries).
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Allowlists — see module docstring.
# ---------------------------------------------------------------------------
# These three sets define which verbs may legitimately be missing from each
# surface. They snapshot the state at e-722's first deploy so the script
# reports only NEW drift (the goal: a new feature commit triggers a warning,
# the pre-existing asymmetries don't).
#
# Shrinking these sets is itself a doc-improvement task (and shrinks the
# baseline). Adding to them must be justified inline.

# Verbs that the bash --help text intentionally doesn't list.
# Sources of truth: bin/beacon's usage() heredoc.
ALLOW_MISSING_FROM_BIN_HELP: set[str] = {
    # Subverbs covered by the parent verb's row in usage() — e.g.
    # `beacon task` line documents add/done/list and we have separate
    # entries for show/detail/delete only in cmd_help_json + README.
    "entry move",
    "issue import",
    "issue list",
    "issue sync",
    "milestone done",
    "milestone rename",
    "milestone show",
    "milestone workspace",
    "milestone workspace-cleanup",
    "retro done",
    "task delete",
    "task detail",
    "task show",
}

# Verbs that cmd_help_json doesn't currently expose (machine-readable subset).
# This is the largest list — cmd_help_json predates many subcommands.
# Shrinking this is the highest-value follow-up.
ALLOW_MISSING_FROM_HELP_JSON: set[str] = {
    "cloud",
    "cloud off",
    "cloud open",
    "deploy list",
    "deploy record",
    "deploy void",
    "doc delete",
    "doctor",
    "entry move",
    "incident close",
    "incident escalate",
    "incident open",
    "issue import",
    "issue list",
    "issue sync",
    "member add",
    "member list",
    "member remove",
    "member role",
    "milestone delete",
    "milestone done",
    "milestone show",
    "milestone update",
    "note",
    "note clear",
    "note list",
    "operation close",
    "operation list",
    "operation open",
    "operation show",
    "pr close",
    "pr create",
    "pr request-changes",
    "pr show",
    "push list",
    "push record",
    "retro done",
    "run list",
    "run record",
    "search",
    "task cancel",
    "task delete",
    "task detail",
    "task show",
    "trigger clear",
    "trigger fire",
    "cloud status",
    "milestone workspace",
    "milestone workspace-cleanup",
    "reset",
    "update",
    # ms-55 coordination signal CLIs — bash-only, help_json + README docs
    # are a follow-up (= dedicated entries for each verb). Until then, users
    # discover them via `beacon <verb> --help` (= bash help text).
    "claim handoff", "claim list", "claim post", "claim release",
    "claim request", "claim respond",
    "morning",
    "resume global", "resume scoped",
    "rollback",
    "stop global", "stop scoped", "stop status",
    "stuck check",
}

# Top-level verbs intentionally available in bin/beacon (bash) but NOT in
# beacon_cli/dispatch.py (Python). These rely on tmux / interactive curses
# / bash-only features that don't translate to Windows pipx (the Python
# entry-point). Documented in dispatch._print_top_help "Not yet available".
#
# Adding to this list = explicit decision to keep a verb bash-only.
# Removing = the verb is now expected to work via Python dispatch too.
# Source of truth for the asymmetry baseline as of ms-44 e-1171.
ALLOW_BASH_ONLY_DISPATCH: set[str] = {
    "setup",       # interactive shell setup (tmux/zsh detection)
    # ms-61 / e-2348: `retro` is now in dispatch.py (prepare/save/done)
    "update",      # self-update via brew/curl, bash-specific
    "reset",       # destructive admin op, bash-only
    "run",         # operation run record (member-aware, not in Python yet)
    "incident",    # incident open/close (operation-coupled, not in Python yet)
    "machine-key",  # ms-151 e-5474: machine 認証の鍵 発行/一覧/失効 (owner 限定,
                    # cloud endpoint)。bash-only for now; dispatch.py Windows parity
                    # = follow-up、run/incident と同じ precedent (rare な dev/ops 管理
                    # verb で、hot な Windows path ではない)。
    # ms-133 e-4642: `sales` and `org` were removed from this allowlist — both
    # now have top-level Python parity (`"sales": _handle_sales` /
    # `"org": _handle_org` in _HANDLERS, `sales)` / `org)` in bin/beacon's main
    # case). Their entries here had gone stale; keeping them would have masked a
    # real future regression if either lost parity. (SPEC AC5.)
    "migrate",     # ms-109 e-3695: `migrate target-labels` one-shot backfill,
                   # bash-only for now (dispatch.py parity = follow-up, same as
                   # sales — a rare migration verb, not on the hot Windows path)
    "target",      # ms-119 e-3912: `target review-request/approve/reject/list`
    "tgt",         # (目的達成レビュー). bash-only for now; dispatch.py Windows
                   # parity = follow-up, same precedent as sales/migrate — a
                   # dev/ops review verb, not on the hot Windows path.
    "target-class",  # ms-124 e-4091: `target-class add/list` declares a
    "tclass",        # data-defined target-class (no-code onboarding). bash-only
                     # for now; dispatch.py Windows parity = follow-up, same
                     # precedent as target — a back-office authoring verb, not on
                     # the hot Windows path.
    "review",      # ms-119 e-3947: `review context` emits the review-kernel
                   # bundle for an independent judge. bash-only for now;
                   # dispatch.py Windows parity = follow-up, same precedent as
                   # target — a dev/ops review verb, not on the hot Windows path.
    # (`org` removed here — see the ms-133 e-4642 note above; it gained Python
    # top-level parity via `"org": _handle_org`.)
    # ms-73 e-1762/e-1763/e-1764 cleared the ms-55 coordination-signal
    # exempts (stop / resume / rollback / claim / stuck / morning) once
    # commit 3b5b64a (e-1735) landed their Python parity. Their entries
    # have been removed here as part of the ms-73 drift-gate sweep.
    # `help` is handled in dispatch.py BEFORE _HANDLERS is consulted
    # (early return in `dispatch()`), so it intentionally doesn't appear
    # as a key in the dict — but it IS handled. The `-h`/`--help` flag
    # forms are filtered at parse time (they don't pass the verb regex).
    "help",
    # NOTE: Once any of these grow Python parity, REMOVE the entry here.
}

# Top-level verbs in beacon_cli/dispatch.py but NOT in bin/beacon (bash).
# Empty by design: every Python verb must also exist in bash, since bash
# is the primary surface on macOS/Linux. Add only if a verb is genuinely
# Win-only (e.g. a future ``beacon win-only-thing``).
ALLOW_PYTHON_ONLY_DISPATCH: set[str] = set()

# Verbs that the README CLI Commands tables don't currently list. Usually
# because they're documented in a different section (Cloud Mode, INSTALL.md
# etc.) or are internal aliases.
ALLOW_MISSING_FROM_README: set[str] = {
    "cloud",            # documented in Cloud Mode section, not CLI table
    "cloud off",
    "cloud open",
    "deploy void",      # destructive admin op, intentionally not promoted
    "doc delete",       # rare — destructive
    "member add",       # documented narratively in team-collab section
    "member list",
    "member remove",
    "member role",
    "pr show",          # discoverable via `beacon pr add` workflow
    "task cancel",      # subsumed by `task update --status cancelled`
    "auth login",       # documented in Cloud Mode / INSTALL.md
    "auth logout",
    "auth status",
    "cloud join",       # documented in Cloud Mode section
    "cloud list",
    "cloud pull",
    "cloud push",
    "cloud status",
    # e-1862: renamed aliases live in the Cloud Mode section
    # (alongside their legacy `push` / `pull` partners), not in the
    # main `## CLI Commands` table.
    "cloud upload-initial",
    "cloud force-pull",
    # ms-95 / e-2339: orphan-retire helper, documented alongside its
    # sibling `cloud upload-initial` in the Cloud Mode section.
    "cloud migrate-from-local",
    "help",             # `beacon help` mirrors --help, not a "command"
    "update",           # documented in self-update section, not CLI table
    # ms-55 coordination signal CLIs — README rows are a follow-up; until
    # then they're discovered via `beacon <verb> --help`.
    "claim handoff", "claim list", "claim post", "claim release",
    "claim request", "claim respond",
    "morning",
    "resume global", "resume scoped",
    "rollback",
    "stop global", "stop scoped", "stop status",
    "stuck check",
}


# ---------------------------------------------------------------------------
# ms-133 e-4642: bash ↔ Python SUB-verb parity (noun + subcommand)
# ---------------------------------------------------------------------------
# The dispatch parity check (collect_dispatch_drift) compares only TOP-LEVEL
# verbs. That left a whole class of drift invisible (2026-07-30 audit, report
# doc JToylm5EStT4c6gK3DZR): a noun exists on both surfaces but a *sub-verb*
# (e.g. `phase add`, `opportunity describe`) is a registered argparse subparser
# choice only in bash, so Windows/pipx users hit `argparse invalid choice` on
# the sub-verb even though `beacon phase --help` lists it on macOS.
#
# Scope by construction: only nouns whose Python handler uses argparse
# ``add_subparsers`` can drift this way — argparse rejects an unknown choice.
# A noun that takes a permissive positional (``note <text_or_sub>``,
# ``sessions <list_arg>``) dispatches the sub-verb manually and accepts
# anything, so it can't ``invalid choice``; python_sub_verbs() emits no choice
# set for it and the comparison skips it. This keeps the check honest (no false
# positives from manually-dispatched nouns).
#
# The two sets below SNAPSHOT the sub-verb drift as of e-4642's first deploy so
# the checker reports only NEW drift. Shrinking them is the follow-up work:
#   * profession-critical rows tagged (e-4643) are backfilled by that task;
#   * cloud upload-initial / migrate-from-local -> e-4649 (install/cloud parity);
#   * operation/trek/bus/channel/deploy/member/project/milestone/trigger rows are
#     dev/ops verbs deliberately bash-only for now (方針3 — not on the hot
#     Windows path), each removable when/if it grows Python parity.
# Removing an entry after adding the matching Python subparser is the whole
# point: the check then guards that verb's parity forever.

# Sub-verbs present in bin/beacon's main-case routing but NOT registered as a
# Python subparser choice (=> `argparse invalid choice` on Windows/pipx).
ALLOW_SUBVERB_MISSING_FROM_PYTHON: set[str] = {
    # -- profession-critical rows backfilled by e-4643 have been REMOVED from
    #    this snapshot (acquisition start/done, opportunity describe/desc,
    #    phase add/rename/move/remove/delete/rm). They now have Python subparser
    #    parity, so the checker actively GUARDS them: if a future change drops
    #    the dispatch subparser, the drift re-appears here as a failure.
    # (cloud upload-initial / migrate-from-local were REMOVED here — e-4649
    #  backfilled them into the Python dispatcher, so the checker now guards
    #  their parity.)
    # -- dev/ops verbs, bash-only for now (方針3, not on the hot Windows path) --
    "bus auto-execute",
    "channel opt-in", "channel opt-out", "channel opt_in", "channel opt_out",
    "channel status", "channel uninstall",
    "claim view",
    "deploy delete", "deploy rollback", "deploy void",
    "member invitation", "member invite", "member join", "member whoami",
    "milestone cancel", "milestone delete", "milestone depends",
    "milestone occupations", "milestone release", "milestone rename",
    "milestone wait",
    "operation activate", "operation approve", "operation close",
    "operation create", "operation list", "operation open",
    "operation pause", "operation resume",  # ms-160 e-5814
    "operation revoke",
    "operation show", "operation start", "operation status", "operation task",
    "operation update",
    "project cleanup", "project export", "project import", "project orphans",
    "project rename",
    "trek blanket-approve", "trek blanket-revoke", "trek block", "trek blockers",
    "trek review-verdicts", "trek summary-sent", "trek task", "trek unblock",
    "trigger tick",
}

# Sub-verbs registered as a Python subparser choice but absent from bin/beacon's
# main-case routing. macOS/Linux users won't reach these via bash.
ALLOW_SUBVERB_MISSING_FROM_BASH: set[str] = {
    # `claim ls` is a Python-side alias of `claim list`; bash exposes
    # `claim list`. Benign alias asymmetry, not a real gap.
    "claim ls",
}


# ---------------------------------------------------------------------------
# ms-126 e-4223 (AX#4 + Maint#5): bash ↔ Python *flag* parity
# ---------------------------------------------------------------------------
# The four checks above compare which *subcommands* exist on each surface but
# deliberately ignore flags. That left a silent gap: a flag can be added to one
# surface (e.g. `--priority` on the Python dispatcher's argparse) and forgotten
# on the other (the bash `cmd_*` arg-loop is a hand-maintained separate copy),
# so `beacon <verb> --priority` works on macOS/Linux but `argparse invalid
# choice`-style breaks — or silently no-ops — on the other path. That is exactly
# the ms-126 failure mode: the mandatory-priority contract must be reachable
# identically from both surfaces.
#
# This is a *curated* parity check, not a blanket flag diff (blanket diffing is
# too noisy — see the module docstring). Each entry names a verb and the flags
# that MUST exist on BOTH the bash function and the Python subparser. Seeded
# with ms-126's priority contract; extend as other cross-surface flags become
# load-bearing. Because only these named pairs are enforced, an unrelated
# bash-only or Python-only flag never trips it — no allowlist needed, the map
# itself is the scope. Verb keys are canonical "noun sub" (bash function
# ``cmd_<noun>_<sub>`` / Python nested subparser ``<noun> <sub>``).
REQUIRED_FLAG_PARITY: dict[str, set[str]] = {
    "milestone add": {"--priority", "--untriaged"},
    "milestone update": {"--priority"},
    "task add": {"--priority", "--untriaged"},
    "task update": {"--priority"},
}


# Subcommand tokens are kept strictly lowercase + hyphen in the codebase.
# Anything starting with an uppercase letter is description prose, not a verb.
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _extract_verb(line: str) -> str | None:
    """Pull the (subcommand subsubcommand) pair from a help / table line.

    Returns the verb in canonical form, e.g. ``"milestone add"`` or
    ``"status"`` or ``""`` for the bare ``beacon`` launch line.

    Stops at the first token that is not a valid lowercase verb token,
    or that starts with ``<``, ``[``, ``-`` (positional / flag / option).
    """
    line = line.strip()
    if not line.startswith("beacon"):
        return None
    parts_in = line.split()
    if not parts_in or parts_in[0] != "beacon":
        return None
    out: list[str] = []
    for tok in parts_in[1:3]:  # at most two verb tokens (subcmd + subsubcmd)
        if tok.startswith(("<", "[", "-")):
            break
        if not _TOKEN_RE.match(tok):
            break
        out.append(tok)
    return " ".join(out)


def parse_bin_beacon(path: Path = BIN_BEACON) -> set[str]:
    """Extract the verb set actually printed by ``beacon --help``.

    ms-120 e-3897: bin/beacon's ``usage()`` no longer holds a hand-maintained
    heredoc — it renders the top-level help from ``lib/commands.py``'s
    ``_help_registry`` (the same source ``beacon help --json`` uses). So instead
    of scraping bash source, we render the real help text and parse the verbs
    the user/AI actually sees. Because both this and ``parse_help_json`` derive
    from that one registry, they align by construction (a single source can't
    drift from itself); the checker's live value is now the README and
    dispatch-parity comparisons below. If the renderer breaks, this surface goes
    empty and the drift shows up here — so the render path stays under test.
    """
    commands_py = path.parent.parent / "lib" / "commands.py"
    if not commands_py.exists():
        return set()
    env = dict(os.environ)
    env.pop("BEACON_HELP_QUERY", None)  # empty query -> full top-level help
    try:
        out = subprocess.run(
            [sys.executable, str(commands_py), "help_render"],
            capture_output=True, text=True, env=env, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    verbs: set[str] = set()
    for line in out.splitlines():
        if not line.startswith("  beacon"):
            continue
        verb = _extract_verb(line.strip())
        if verb is None:
            continue
        verbs.add(verb)
    return verbs


def parse_help_json(commands_py: Path = COMMANDS_PY) -> set[str]:
    """Run ``cmd_help_json()`` in-process and collect its command verbs."""
    if not commands_py.exists():
        return set()
    spec = importlib.util.spec_from_file_location("_beacon_commands", commands_py)
    if spec is None or spec.loader is None:
        return set()
    # commands.py imports neighbours (auth, core, ...) by their bare module
    # name. Add lib/ to sys.path so those resolve.
    lib_path = str(commands_py.parent)
    added = False
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)
        added = True
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        # We don't want a transient import error to break the drift check.
        # Surface it as an empty set and let the caller notice via the
        # "every README entry is unhandled" signal.
        print(f"[cli-drift] WARN: could not import commands.py ({e})", file=sys.stderr)
        return set()
    finally:
        if added:
            try:
                sys.path.remove(lib_path)
            except ValueError:
                pass

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            mod.cmd_help_json()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[cli-drift] WARN: cmd_help_json raised ({e})", file=sys.stderr)
        return set()

    raw = buf.getvalue().strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[cli-drift] WARN: cmd_help_json emitted non-JSON ({e})", file=sys.stderr)
        return set()

    verbs: set[str] = set()
    for entry in data.get("commands", []):
        cmd = entry.get("command", "")
        verb = _extract_verb(cmd)
        if verb is not None:
            verbs.add(verb)
    return verbs


_README_CMD_RE = re.compile(r"^\|\s*`([^`]+)`")


def parse_readme(path: Path = README) -> set[str]:
    """Extract verbs from the ``## CLI Commands`` table-of-tables.

    We scope to the section between ``## CLI Commands`` and the next
    ``## ``-level heading so that example backticks elsewhere in the README
    don't pollute the set.
    """
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^##\s+CLI Commands\s*$", text, re.MULTILINE)
    if not m:
        return set()
    # Find next ## heading after this one.
    rest = text[m.end():]
    end = re.search(r"^##\s+\S", rest, re.MULTILINE)
    section = rest if end is None else rest[: end.start()]

    verbs: set[str] = set()
    for line in section.splitlines():
        m2 = _README_CMD_RE.match(line)
        if not m2:
            continue
        cmd_text = m2.group(1).strip()
        verb = _extract_verb(cmd_text)
        if verb is None:
            continue
        verbs.add(verb)
    return verbs


# ---------------------------------------------------------------------------
# ms-44 e-1171: bash main case / Python _HANDLERS parity
# ---------------------------------------------------------------------------

# Top-level case branches in the bash dispatcher's MAIN switch (the one at
# the bottom of bin/beacon, not the helper switches inside ensure_project
# etc.). Match a line that is exactly 4 spaces + lowercase verb + ``)``.
# Also handles union pattern like ``    milestone|ms)`` — splits on ``|``
# and yields every verb in the union.
_BIN_CASE_RE = re.compile(r"^    ([a-z][a-z0-9_|-]*)\)\s*$")

# Top-level handler keys in beacon_cli/dispatch.py's _HANDLERS dict.
# Source of truth for "what Windows pipx users can invoke". Match lines
# like ``    "milestone": _handle_milestone,`` (4-space indent inside dict).
_PY_HANDLER_RE = re.compile(r'^    "([a-z][a-z0-9_-]*)"\s*:\s*_handle_')


def parse_bin_main_cases(path: Path = BIN_BEACON) -> set[str]:
    """Extract top-level verbs from the bash dispatcher's MAIN case switch.

    bin/beacon has multiple ``case`` blocks; only the bottom one (at column
    0 with branches at 4-space indent) is the user-facing dispatcher. The
    helper switches inside functions are indented deeper.
    """
    if not path.exists():
        return set()
    verbs: set[str] = set()
    in_main_case = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == 'case "${1:-}" in':
            # Column-0 top-level switch (the main dispatcher).
            in_main_case = True
            continue
        if in_main_case and line == "esac":
            in_main_case = False
            continue
        if not in_main_case:
            continue
        m = _BIN_CASE_RE.match(line)
        if m:
            # Split union patterns: ``milestone|ms`` -> {milestone, ms}.
            # Also drop flag-shaped aliases like ``-h`` / ``--help`` that
            # appear inside unions but aren't real command verbs.
            for verb in m.group(1).split("|"):
                if verb and not verb.startswith("-"):
                    verbs.add(verb)
    return verbs


def parse_python_handlers(path: Path = PYTHON_DISPATCH) -> set[str]:
    """Extract top-level verbs from beacon_cli/dispatch.py's _HANDLERS dict.

    We scope to the literal dict definition and pull every ``"verb":
    _handle_xxx,`` row. Aliases are intentionally included (e.g. ``ms``
    aliases ``milestone``) — bash should expose them too.
    """
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    # Find _HANDLERS dict literal start.
    m = re.search(r"^_HANDLERS:.*\{\s*$", text, re.MULTILINE)
    if m is None:
        return set()
    rest = text[m.end():]
    # Find matching closing brace at column 0.
    end = re.search(r"^\}\s*$", rest, re.MULTILINE)
    body = rest if end is None else rest[: end.start()]

    verbs: set[str] = set()
    for line in body.splitlines():
        m2 = _PY_HANDLER_RE.match(line)
        if m2:
            verbs.add(m2.group(1))
    return verbs


def collect_dispatch_drift(
    bin_path: Path = BIN_BEACON,
    python_dispatch_path: Path = PYTHON_DISPATCH,
) -> dict:
    """Compare bash main case branches vs Python _HANDLERS keys.

    Returns dict with:
      - bash_verbs / python_verbs (sorted lists)
      - missing_from_python (in bash but not Python, excluding allowlist)
      - missing_from_bash (in Python but not bash, excluding allowlist)
      - ok (both missing sets empty)
    """
    bash_verbs = parse_bin_main_cases(bin_path)
    python_verbs = parse_python_handlers(python_dispatch_path)

    # Strip noise: catch-all and empty-string clauses, aliases that the
    # bash side surfaces through the same case (we treat them as parity).
    def _norm(s: set[str]) -> set[str]:
        return {v for v in s if v and v != "*"}

    bash_verbs = _norm(bash_verbs)
    python_verbs = _norm(python_verbs)

    missing_from_python = (bash_verbs - python_verbs) - ALLOW_BASH_ONLY_DISPATCH
    missing_from_bash = (python_verbs - bash_verbs) - ALLOW_PYTHON_ONLY_DISPATCH

    return {
        "ok": not (missing_from_python or missing_from_bash),
        "bash_verbs": sorted(bash_verbs),
        "python_verbs": sorted(python_verbs),
        "missing_from_python_dispatch": sorted(missing_from_python),
        "missing_from_bash_dispatch": sorted(missing_from_bash),
    }


# ---------------------------------------------------------------------------
# ms-126 e-4223: bash ↔ Python flag parity extraction
# ---------------------------------------------------------------------------

def _subparsers_action(parser) -> "argparse._SubParsersAction | None":
    """Return the argparse sub-parsers action of ``parser`` (or None)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _load_dispatch_parser(python_dispatch_path: Path = PYTHON_DISPATCH):
    """Import ``beacon_cli.dispatch`` and return its ``build_parser()`` result.

    ``beacon_cli.dispatch`` uses package-relative imports, so it must be loaded
    as part of its package (the repo root that contains ``beacon_cli/`` on
    ``sys.path``), not as a bare file — hence ``import_module`` over the package
    name rather than a spec-from-file-location load. Because ``import_module``
    caches in ``sys.modules``, this always introspects the *installed* package;
    callers that need to introspect a different parser (e.g. a synthetic one in
    a test) inject it via ``python_verb_flags(parser=...)`` instead of pointing
    this at another file — the path argument here only anchors which repo root
    goes on ``sys.path``, it cannot swap the cached module.
    """
    pkg_root = python_dispatch_path.resolve().parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))
    module = importlib.import_module("beacon_cli.dispatch")
    return module.build_parser()


def python_verb_flags(
    python_dispatch_path: Path = PYTHON_DISPATCH,
    parser=None,
) -> dict[str, set[str]]:
    """Map canonical ``"noun sub"`` → set of ``--long`` flags the Python
    dispatcher registers on that nested subparser.

    Introspects a real argparse parser (not a regex over the source), so it sees
    exactly the flags argparse would accept — including aliases like
    ``--priority -p`` (only the ``--`` spellings are collected; the parity
    contract is about long flags). Aliased nouns (``ms`` → ``milestone``) also
    appear as keys; we key the required map by canonical names so both resolve.

    ``parser`` is the real injection seam: pass a ``build_parser()``-shaped
    ``ArgumentParser`` to introspect it directly. When ``None`` (production),
    the installed ``beacon_cli.dispatch`` parser is loaded — see
    ``_load_dispatch_parser`` for why the module can't be swapped by path.
    """
    if parser is None:
        parser = _load_dispatch_parser(python_dispatch_path)

    result: dict[str, set[str]] = {}
    top = _subparsers_action(parser)
    if top is None:
        return result
    for noun, noun_parser in top.choices.items():
        nested = _subparsers_action(noun_parser)
        if nested is None:
            continue
        for sub, sub_parser in nested.choices.items():
            flags = {
                opt
                for action in sub_parser._actions
                for opt in action.option_strings
                if opt.startswith("--")
            }
            result[f"{noun} {sub}"] = flags
    return result


_BASH_FUNC_RE = re.compile(r"^cmd_[a-z0-9_]+\(\)", re.MULTILINE)
# A case label like ``--priority)`` or ``--acceptance-criteria|--ac)`` — capture
# the whole ``--a|--b`` alias group that precedes the closing paren.
_BASH_CASE_FLAG_RE = re.compile(r"^\s*(--[a-zA-Z0-9|=?*.\-]+)\)", re.MULTILINE)


def bash_verb_flags(verb: str, bin_path: Path = BIN_BEACON) -> "set[str] | None":
    """Collect the ``--long`` flags parsed inside the bash ``cmd_<verb>()``
    function body (``verb`` = canonical ``"noun sub"``).

    The bash dispatcher hand-parses flags in a ``case "$1" in … --flag) …``
    loop inside a ``cmd_<noun>_<sub>()`` function. We slice that function (its
    ``cmd_…()`` header to the next ``cmd_…()`` header) and read the case labels,
    splitting ``--a|--b`` alias groups.

    Returns the set of long flags, or ``None`` when the function definition
    isn't found. The two are distinct failures with distinct fixes ("the loop
    is missing a flag" vs "the function was renamed/removed / the verb key is
    wrong"), so the caller must not collapse a missing function into an empty
    flag set. The function *header* is matched line-anchored (``^cmd_…()``) —
    the same anchor used to find the slice *end* — so a bare mention of the
    name in a comment or usage string can't be mistaken for the definition
    (an unanchored ``str.find`` could latch onto an earlier occurrence and
    slice the wrong region, yielding a false pass).
    """
    # ms-127 e-4867: cmd_<verb>() bodies may live in bin/beacon OR a sourced
    # bin/lib/cmd_*.sh family file. Scan the combined surface so a function
    # that was split out is still found (else the split reads as "handler
    # absent" and fails CI on a pure move).
    text = _bash_function_source(bin_path)
    name = "cmd_" + verb.replace(" ", "_")
    header_re = re.compile(r"^" + re.escape(name) + r"\(\)", re.MULTILINE)
    m = header_re.search(text)
    if m is None:
        return None
    nxt = _BASH_FUNC_RE.search(text, m.end())
    body = text[m.start():nxt.start()] if nxt else text[m.start():]
    flags: set[str] = set()
    for group in _BASH_CASE_FLAG_RE.findall(body):
        for token in group.split("|"):
            token = token.split("=")[0]  # normalise ``--x=…`` shapes
            if token.startswith("--") and token != "--":
                flags.add(token)
    return flags


def collect_flag_parity(
    bin_path: Path = BIN_BEACON,
    python_dispatch_path: Path = PYTHON_DISPATCH,
) -> dict:
    """Verify every ``REQUIRED_FLAG_PARITY`` (verb, flag) exists on BOTH the
    bash function and the Python subparser.

    Returns dict with ``ok`` plus three lists: ``missing_from_python_flags`` /
    ``missing_from_bash_flags`` (``"<verb> <flag>"`` strings), and
    ``missing_bash_functions`` (``"<verb>"`` — the whole bash ``cmd_<verb>()``
    is absent, a different fix than a missing flag: add/rename the function or
    correct the verb key, not "add a flag to a loop that doesn't exist").
    """
    py_flags = python_verb_flags(python_dispatch_path)
    missing_python: list[str] = []
    missing_bash: list[str] = []
    missing_bash_functions: list[str] = []
    for verb, required in REQUIRED_FLAG_PARITY.items():
        have_py = py_flags.get(verb, set())
        have_bash = bash_verb_flags(verb, bin_path)
        if have_bash is None:
            # The cmd_<verb>() function itself is gone — report once, and don't
            # also emit per-flag "missing from loop" noise for a loop that
            # doesn't exist (that would misdirect the fix).
            missing_bash_functions.append(verb)
            have_bash = set()
            bash_function_present = False
        else:
            bash_function_present = True
        for flag in sorted(required):
            if flag not in have_py:
                missing_python.append(f"{verb} {flag}")
            if bash_function_present and flag not in have_bash:
                missing_bash.append(f"{verb} {flag}")
    return {
        "ok": not (missing_python or missing_bash or missing_bash_functions),
        "missing_from_python_flags": sorted(missing_python),
        "missing_from_bash_flags": sorted(missing_bash),
        "missing_bash_functions": sorted(missing_bash_functions),
    }


# ---------------------------------------------------------------------------
# ms-133 e-4642: bash ↔ Python sub-verb parity extraction
# ---------------------------------------------------------------------------

def _noun_alias_map(parser) -> "dict[str, str]":
    """Map every top-level noun spelling → its canonical noun.

    ``build_parser`` registers alias nouns (``ms`` for ``milestone``, ``opp``
    for ``opportunity``) as extra keys in the top ``_SubParsersAction.choices``
    that point to the *same* parser object. We group by object identity and pick
    the longest spelling as canonical (milestone>ms, opportunity>opp,
    account>acc, …), so the bash and Python sides collapse to one key before we
    diff sub-verbs — otherwise every aliased noun would double-count."""
    top = _subparsers_action(parser)
    if top is None:
        return {}
    groups: "dict[int, list[str]]" = {}
    for name, p in top.choices.items():
        groups.setdefault(id(p), []).append(name)
    amap: "dict[str, str]" = {}
    for names in groups.values():
        canon = max(names, key=len)
        for n in names:
            amap[n] = canon
    return amap


def python_sub_verbs(
    python_dispatch_path: Path = PYTHON_DISPATCH,
    parser=None,
) -> "dict[str, set[str]]":
    """Map canonical noun → set of sub-verb argparse *choices* the Python
    dispatcher enforces on that noun's nested subparser.

    Only nouns whose handler uses ``add_subparsers`` appear. A noun that takes a
    permissive positional (``note <text_or_sub>``, ``sessions <list_arg>``)
    dispatches its sub-verb by hand and accepts any token, so it can't
    ``argparse invalid choice`` — it contributes nothing here and the parity
    comparison skips it. ``parser`` is the injection seam for tests; when None
    the installed ``beacon_cli.dispatch`` parser is loaded."""
    if parser is None:
        parser = _load_dispatch_parser(python_dispatch_path)
    amap = _noun_alias_map(parser)
    top = _subparsers_action(parser)
    out: "dict[str, set[str]]" = {}
    if top is None:
        return out
    for noun, np in top.choices.items():
        nested = _subparsers_action(np)
        if nested is None:
            continue
        out.setdefault(amap.get(noun, noun), set()).update(nested.choices)
    return out


_BIN_NOUN_CASE_RE = re.compile(r"^    ([a-z][a-z0-9_|-]*)\)\s*$")
_BIN_INNER_CASE_OPEN_RE = re.compile(r"case .*? in\s*$")
_BIN_INNER_LABEL_RE = re.compile(r"^\s+([a-z][a-z0-9_|-]*)\)(?:\s|$)")
_BIN_BRANCH_END_RE = re.compile(r"^        ;;\s*$")
_BIN_ESAC_RE = re.compile(r"^\s+esac\s*$")


def parse_bin_sub_verbs(
    bin_path: Path = BIN_BEACON,
    alias_map: "dict[str, str] | None" = None,
) -> "dict[str, set[str]]":
    """Map canonical noun → set of sub-verb case labels bin/beacon's MAIN switch
    routes for that noun.

    Each top-level noun branch may open an inner ``case "${2:-}" in`` that lists
    the sub-verbs. We walk the branch tracking ``case``/``esac`` depth so a
    deeper nested case (e.g. acquisition's ``attack-list`` flows) doesn't leak
    its labels, and collect only the FIRST inner level. Union labels
    (``remove|delete|rm``) split into separate sub-verbs; the terminal ``*``
    wildcard is skipped. ``alias_map`` (from ``_noun_alias_map``) canonicalises
    aliased noun branches (``milestone|ms``) so both sides key alike."""
    if alias_map is None:
        alias_map = {}
    lines = bin_path.read_text(encoding="utf-8").splitlines()
    out: "dict[str, set[str]]" = {}
    in_main = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line == 'case "${1:-}" in':
            in_main = True
            i += 1
            continue
        if in_main and line == "esac":
            in_main = False
            i += 1
            continue
        if in_main:
            m = _BIN_NOUN_CASE_RE.match(line)
            if m:
                nouns = [x for x in m.group(1).split("|") if not x.startswith("-")]
                j = i + 1
                depth = 0
                while j < n:
                    lj = lines[j]
                    if depth == 0 and _BIN_BRANCH_END_RE.match(lj):
                        break
                    if _BIN_INNER_CASE_OPEN_RE.search(lj):
                        depth += 1
                        j += 1
                        continue
                    if _BIN_ESAC_RE.match(lj):
                        depth -= 1
                        j += 1
                        continue
                    if depth == 1:
                        lm = _BIN_INNER_LABEL_RE.match(lj)
                        if lm:
                            for sub in lm.group(1).split("|"):
                                if sub == "*":
                                    continue
                                for nn in nouns:
                                    canon = alias_map.get(nn, nn)
                                    out.setdefault(canon, set()).add(sub)
                    j += 1
                i = j
                continue
        i += 1
    return out


def collect_subverb_drift(
    bin_path: Path = BIN_BEACON,
    python_dispatch_path: Path = PYTHON_DISPATCH,
) -> dict:
    """Compare bash inner-case sub-verbs vs Python nested-subparser choices.

    Only nouns that are subparser-backed on the Python side (i.e. can
    ``invalid choice``) AND present in the bash main switch are compared.
    Returns the missing sets minus the ALLOW_SUBVERB_* snapshots."""
    parser = _load_dispatch_parser(python_dispatch_path)
    amap = _noun_alias_map(parser)
    py = python_sub_verbs(python_dispatch_path, parser=parser)
    bash = parse_bin_sub_verbs(bin_path, alias_map=amap)

    missing_from_python: set[str] = set()
    missing_from_bash: set[str] = set()
    for noun in set(py) & set(bash):
        for sub in bash[noun] - py[noun]:
            missing_from_python.add(f"{noun} {sub}")
        for sub in py[noun] - bash[noun]:
            missing_from_bash.add(f"{noun} {sub}")

    missing_from_python -= ALLOW_SUBVERB_MISSING_FROM_PYTHON
    missing_from_bash -= ALLOW_SUBVERB_MISSING_FROM_BASH
    return {
        "ok": not (missing_from_python or missing_from_bash),
        "missing_from_python_subverbs": sorted(missing_from_python),
        "missing_from_bash_subverbs": sorted(missing_from_bash),
    }


# Use [ \t]* (not \s*) after the colon and (.*) (not (.+)): a family with NO
# function deps writes an empty `# requires-fn:` line. With \s* + (.+) the
# regex would let \s* swallow the newline and (.+) grab the NEXT line's text
# (e.g. the requires-var line), producing bogus "missing symbol" reports. [ \t]*
# keeps the match on one line; (.*) allows an empty (dep-free) declaration.
_REQUIRES_FN_RE = re.compile(r"^#[ \t]*requires-fn:[ \t]*(.*)$", re.MULTILINE)
_REQUIRES_VAR_RE = re.compile(r"^#[ \t]*requires-var:[ \t]*(.*)$", re.MULTILINE)
# requires-cmd: cross-file cmd_* deps (a family fn that calls a cmd_* defined in
# another family file), ms-127 e-4867.
_REQUIRES_CMD_RE = re.compile(r"^#[ \t]*requires-cmd:[ \t]*(.*)$", re.MULTILINE)


def collect_requires_drift(bin_path: Path = BIN_BEACON) -> dict:
    """ms-127 e-4867: verify each family file's `# requires-fn:` / `# requires-var:`
    declaration against reality.

    The god-module split moves cmd_<verb>() bodies into sourced bin/lib/cmd_*.sh
    files that implicitly depend on helpers defined in bin/beacon (the dispatcher).
    Each family file declares those cross-file deps in a machine-readable seam:

        # requires-fn: ensure_project _guard_flag
        # requires-var: COMMANDS_PY BEACON_INVOCATION_CWD

    Without a guard that seam is just a comment that can silently rot when a
    helper is renamed. This check makes it a *verified contract*: every declared
    `requires-fn` must be a real `name()` function in bin/beacon, and every
    `requires-var` must be assigned/exported there. A context-zero reader (and
    the next family split) can then trust the declaration.

    Returns {ok, missing_fn: ["<file>: <sym>"...], missing_var: [...]}.
    """
    bin_text = bin_path.read_text(encoding="utf-8")
    defined_fns = {
        m.group(1)
        for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)", bin_text, re.MULTILINE)
    }
    assigned_vars = {
        m.group(1)
        for m in re.finditer(
            r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", bin_text, re.MULTILINE
        )
    }
    # exported inline (e.g. `export FOO="..." cmd_bar`) — catch `export NAME=` and
    # bare `export NAME` forms too.
    assigned_vars |= {
        m.group(1)
        for m in re.finditer(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)", bin_text)
    }

    lib_dir = bin_path.parent / "lib"
    missing_fn: list[str] = []
    missing_var: list[str] = []
    # ms-127 e-4867: also validate lib→lib `cmd_*` calls. A family function may
    # call a cmd_* defined in ANOTHER family file (e.g. cmd_launch calls
    # cmd_status). That cross-file dep is declared in `# requires-cmd:` and
    # checked two ways: (1) each declared symbol is defined somewhere; (2)
    # COMPLETENESS — every cross-file cmd_* actually invoked is declared (the
    # reverse direction both review lenses asked for, so an undeclared cross-lib
    # call can't silently pass).
    undeclared_cmd: list[str] = []
    missing_cmd: list[str] = []
    fam_files = sorted(lib_dir.glob("cmd_*.sh")) if lib_dir.is_dir() else []
    # map every cmd_* definition to the file that defines it (lib + dispatcher)
    cmd_def_file: dict[str, str] = {}
    for fam in fam_files:
        for m in re.finditer(r"^(cmd_[a-z0-9_]+)\(\)", fam.read_text(encoding="utf-8"), re.MULTILINE):
            cmd_def_file[m.group(1)] = fam.name
    for m in re.finditer(r"^(cmd_[a-z0-9_]+)\(\)", bin_text, re.MULTILINE):
        cmd_def_file.setdefault(m.group(1), bin_path.name)
    # command-position cmd_* token: at statement start or after ; && || then do else
    call_re = re.compile(r"(?:^|;|&&|\|\||\bthen\b|\bdo\b|\belse\b)\s*(cmd_[a-z0-9_]+)\b")
    if lib_dir.is_dir():
        for family in fam_files:
            ftext = family.read_text(encoding="utf-8")
            local_defs = {m.group(1) for m in re.finditer(r"^(cmd_[a-z0-9_]+)\(\)", ftext, re.MULTILINE)}
            declared_cmd: set[str] = set()
            for m in _REQUIRES_FN_RE.finditer(ftext):
                for sym in m.group(1).split():
                    if sym not in defined_fns:
                        missing_fn.append(f"{family.name}: {sym}")
            for m in _REQUIRES_VAR_RE.finditer(ftext):
                for sym in m.group(1).split():
                    if sym not in assigned_vars:
                        missing_var.append(f"{family.name}: {sym}")
            for m in _REQUIRES_CMD_RE.finditer(ftext):
                for sym in m.group(1).split():
                    declared_cmd.add(sym)
                    if sym not in cmd_def_file:
                        missing_cmd.append(f"{family.name}: {sym} (declared, defined nowhere)")
            # completeness: scan code lines (full-line comments stripped) for
            # command-position cmd_* invocations resolving to ANOTHER file.
            for raw in ftext.splitlines():
                if raw.lstrip().startswith("#"):
                    continue
                for m in call_re.finditer(raw):
                    tok = m.group(1)
                    if tok in local_defs:
                        continue  # local call, fine
                    if tok in cmd_def_file and cmd_def_file[tok] != family.name:
                        if tok not in declared_cmd:
                            undeclared_cmd.append(f"{family.name}: {tok} (calls it, not in requires-cmd)")
    undeclared_cmd = sorted(set(undeclared_cmd))
    return {
        "ok": not (missing_fn or missing_var or missing_cmd or undeclared_cmd),
        "missing_requires_fn": sorted(missing_fn),
        "missing_requires_var": sorted(missing_var),
        "missing_requires_cmd": sorted(missing_cmd),
        "undeclared_cross_lib_cmd": undeclared_cmd,
    }


def collect_drift(
    bin_path: Path = BIN_BEACON,
    commands_path: Path = COMMANDS_PY,
    readme_path: Path = README,
    python_dispatch_path: Path = PYTHON_DISPATCH,
) -> dict:
    """Return a structured drift report (see module docstring)."""
    bin_verbs = parse_bin_beacon(bin_path)
    json_verbs = parse_help_json(commands_path)
    readme_verbs = parse_readme(readme_path)

    union = bin_verbs | json_verbs | readme_verbs

    json_missing: set[str] = set()
    readme_missing: set[str] = set()

    for v in union:
        if v == "":
            # Bare `beacon` (dashboard launch) — appears as different rows in
            # all three sources but isn't a "subcommand". Skip entirely.
            continue
        in_json = v in json_verbs
        in_readme = v in readme_verbs
        if not in_json and v not in ALLOW_MISSING_FROM_HELP_JSON:
            json_missing.add(v)
        if not in_readme and v not in ALLOW_MISSING_FROM_README:
            readme_missing.add(v)

    # ms-120 e-3897: `beacon --help` now RENDERS the cmd_help_json registry, so
    # bin_verbs and json_verbs share one source and cannot drift by hand. The
    # only failure mode left is a broken renderer that drops registry commands
    # from the printed help — that is what bin_missing now guards (registry
    # commands absent from rendered `--help`). No allowlist: every registry
    # command must appear. (Pre-existing README/registry gaps are reported by
    # json_missing / readme_missing above, not here.)
    bin_missing = {v for v in (json_verbs - bin_verbs) if v != ""}

    # ms-44 e-1171: bash main case vs Python _HANDLERS parity.
    dispatch_drift = collect_dispatch_drift(bin_path, python_dispatch_path)

    # ms-126 e-4223: bash ↔ Python flag parity (curated priority contract).
    flag_parity = collect_flag_parity(bin_path, python_dispatch_path)

    # ms-133 e-4642: bash ↔ Python sub-verb parity (noun + subcommand).
    subverb_drift = collect_subverb_drift(bin_path, python_dispatch_path)

    # ms-127 e-4867: family file `# requires-fn/var/cmd:` seam vs reality.
    requires_drift = collect_requires_drift(bin_path)

    report = {
        "ok": not (
            bin_missing
            or json_missing
            or readme_missing
            or not dispatch_drift["ok"]
            or not flag_parity["ok"]
            or not subverb_drift["ok"]
            or not requires_drift["ok"]
        ),
        "missing_requires_fn": requires_drift["missing_requires_fn"],
        "missing_requires_var": requires_drift["missing_requires_var"],
        "missing_requires_cmd": requires_drift["missing_requires_cmd"],
        "undeclared_cross_lib_cmd": requires_drift["undeclared_cross_lib_cmd"],
        "bin_verbs": sorted(bin_verbs),
        "json_verbs": sorted(json_verbs),
        "readme_verbs": sorted(readme_verbs),
        "missing_from_bin_help": sorted(bin_missing),
        "missing_from_help_json": sorted(json_missing),
        "missing_from_readme": sorted(readme_missing),
        # ms-44 e-1171 surface (bash main case vs Python _HANDLERS):
        "bash_dispatch_verbs": dispatch_drift["bash_verbs"],
        "python_dispatch_verbs": dispatch_drift["python_verbs"],
        "missing_from_python_dispatch": dispatch_drift["missing_from_python_dispatch"],
        "missing_from_bash_dispatch": dispatch_drift["missing_from_bash_dispatch"],
        # ms-126 e-4223 surface (bash cmd_* flags vs Python subparser flags):
        "missing_from_python_flags": flag_parity["missing_from_python_flags"],
        "missing_from_bash_flags": flag_parity["missing_from_bash_flags"],
        "missing_bash_functions": flag_parity["missing_bash_functions"],
        # ms-133 e-4642 surface (bash inner-case sub-verbs vs Python subparser
        # choices — the noun+subcommand parity that top-level checks miss):
        "missing_from_python_subverbs": subverb_drift["missing_from_python_subverbs"],
        "missing_from_bash_subverbs": subverb_drift["missing_from_bash_subverbs"],
    }
    return report


def _format_text(report: dict) -> str:
    if report["ok"]:
        return (
            "[cli-drift] OK: bin/beacon, cmd_help_json, README CLI tables, "
            "bash↔Python dispatch, and required flag parity are aligned.\n"
        )
    lines = ["[cli-drift] Drift detected between CLI source-of-truth surfaces:", ""]
    if report["missing_from_bin_help"]:
        lines.append("  - missing from bin/beacon usage() (not shown by `beacon --help`):")
        for v in report["missing_from_bin_help"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> add a line under the matching section in bin/beacon's usage() heredoc.")
    if report["missing_from_help_json"]:
        lines.append("  - missing from cmd_help_json (not shown by `beacon help --json`):")
        for v in report["missing_from_help_json"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> add an entry to the `commands` list in lib/commands.py:cmd_help_json.")
    if report["missing_from_readme"]:
        lines.append("  - missing from README ## CLI Commands tables:")
        for v in report["missing_from_readme"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> add a row to the matching ### subsection in README.md.")
    if report.get("missing_from_python_dispatch"):
        lines.append("  - in bash dispatch but missing from beacon_cli/dispatch.py _HANDLERS:")
        for v in report["missing_from_python_dispatch"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> Windows pipx users hit `argparse invalid choice` for these.")
        lines.append("    -> add `_handle_<verb>` + `sub.add_parser('<verb>', ...)` in")
        lines.append("       beacon_cli/dispatch.py and register in _HANDLERS, OR add")
        lines.append("       the verb to ALLOW_BASH_ONLY_DISPATCH if intentionally bash-only.")
    if report.get("missing_from_bash_dispatch"):
        lines.append("  - in beacon_cli/dispatch.py _HANDLERS but missing from bin/beacon main case:")
        for v in report["missing_from_bash_dispatch"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> macOS/Linux users won't see these (bash is the default path).")
        lines.append("    -> add the verb to bin/beacon's main case, OR add it to")
        lines.append("       ALLOW_PYTHON_ONLY_DISPATCH if intentionally Python-only.")
    if report.get("missing_from_python_flags"):
        lines.append("  - required flag missing from Python dispatch subparser (beacon_cli/dispatch.py):")
        for v in report["missing_from_python_flags"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> add the flag to the matching sub.add_parser(...).add_argument(...) in")
        lines.append("       beacon_cli/dispatch.py so Windows/pipx users get the same contract.")
    if report.get("missing_from_bash_flags"):
        lines.append("  - required flag missing from bash cmd_* arg-loop (bin/beacon):")
        for v in report["missing_from_bash_flags"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> add a `--flag)` case to the matching cmd_<verb>() loop in bin/beacon,")
        lines.append("       OR revise REQUIRED_FLAG_PARITY if the contract intentionally changed.")
    if report.get("missing_bash_functions"):
        lines.append("  - bash cmd_<verb>() function itself not found in bin/beacon:")
        for v in report["missing_bash_functions"]:
            lines.append(f"      beacon {v}  (expected function: cmd_{v.replace(' ', '_')}())")
        lines.append("    -> the whole handler is absent (renamed/removed), not just a flag.")
        lines.append("       Add/rename the cmd_<verb>() function in bin/beacon, OR fix the verb")
        lines.append("       key in REQUIRED_FLAG_PARITY if it no longer matches a real function.")
    if report.get("missing_from_python_subverbs"):
        lines.append("  - sub-verb in bin/beacon routing but NOT a Python subparser choice:")
        for v in report["missing_from_python_subverbs"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> Windows/pipx users hit `argparse invalid choice` on the sub-verb.")
        lines.append("    -> register it via `<noun>_sub.add_parser('<sub>', ...)` in")
        lines.append("       beacon_cli/dispatch.py, OR add it to ALLOW_SUBVERB_MISSING_FROM_PYTHON")
        lines.append("       if the sub-verb is intentionally bash-only for now.")
    if report.get("missing_from_bash_subverbs"):
        lines.append("  - sub-verb registered as a Python subparser choice but missing from bin/beacon:")
        for v in report["missing_from_bash_subverbs"]:
            lines.append(f"      beacon {v}")
        lines.append("    -> macOS/Linux (bash) users can't reach it.")
        lines.append("    -> add the inner-case label in bin/beacon's main switch, OR add it to")
        lines.append("       ALLOW_SUBVERB_MISSING_FROM_BASH if it's an intentional Python-only alias.")
    if report.get("missing_requires_fn"):
        lines.append("  - bin/lib/cmd_*.sh `# requires-fn:` names a function absent from bin/beacon:")
        for v in report["missing_requires_fn"]:
            lines.append(f"      {v}")
        lines.append("    -> the family file declares a helper dep that no longer exists (renamed/removed).")
        lines.append("       Fix the `# requires-fn:` line, or restore the function in bin/beacon.")
    if report.get("missing_requires_var"):
        lines.append("  - bin/lib/cmd_*.sh `# requires-var:` names a variable absent from bin/beacon:")
        for v in report["missing_requires_var"]:
            lines.append(f"      {v}")
        lines.append("    -> the family file declares a variable dep that isn't assigned/exported in bin/beacon.")
        lines.append("       Fix the `# requires-var:` line, or set the variable in bin/beacon.")
    if report.get("missing_requires_cmd"):
        lines.append("  - bin/lib/cmd_*.sh `# requires-cmd:` names a cmd_* defined nowhere:")
        for v in report["missing_requires_cmd"]:
            lines.append(f"      {v}")
        lines.append("    -> fix the `# requires-cmd:` line, or restore the cmd_* function.")
    if report.get("undeclared_cross_lib_cmd"):
        lines.append("  - bin/lib/cmd_*.sh calls a cmd_* from another family file but doesn't declare it:")
        for v in report["undeclared_cross_lib_cmd"]:
            lines.append(f"      {v}")
        lines.append("    -> add the called function to that file's `# requires-cmd:` line so the")
        lines.append("       lib→lib dependency is a machine-verified contract (a zero-context reader")
        lines.append("       must see the dep without grepping all of bin/lib/).")
    lines.append("")
    lines.append("Allowlists for intentional asymmetries live in scripts/check-cli-help-drift.py.")
    lines.append("This guard is part of ms-10 e-722 (doc & skill auto-sync) + ms-44 e-1171 (dispatch parity).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bin", default=str(BIN_BEACON))
    parser.add_argument("--commands", default=str(COMMANDS_PY))
    parser.add_argument("--readme", default=str(README))
    parser.add_argument("--python-dispatch", default=str(PYTHON_DISPATCH))
    args = parser.parse_args(argv)

    report = collect_drift(
        bin_path=Path(args.bin),
        commands_path=Path(args.commands),
        readme_path=Path(args.readme),
        python_dispatch_path=Path(args.python_dispatch),
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(_format_text(report))

    if report["ok"]:
        return 0
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
