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
    # ms-160 e-6349: `bus` は help レジストリを持たず bash 側の専用 help を使う設計
    # (cmd_help_render が exit 3 して bash にフォールバックする、ms-120 e-3897)。
    # ここに bus の 1 subcommand だけ登録すると `beacon bus --help` がその 1 件しか
    # 描かなくなり、send / listen / receive / status を隠してしまう = 改善ではなく退行。
    # README には載せる (user 向けの動詞なので) が、レジストリ側は空のままにする。
    "bus consent-check",
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
    # ms-161 e-5902/e-5903: deliverable-changelog curation + derived-map render.
    # bash-only for now (dev-facing 記帳/整地 verbs, not on the hot Windows path);
    # dispatch.py parity = follow-up, same precedent as the ops verbs below. The
    # checker canonicalises this noun to its plural alias ("deliverables"), so the
    # allowlist keys match that form.
    "deliverables add", "deliverables retire", "deliverables supersede",
    "deliverables map",
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
    # ms-159 #739 (maintainability): `attention` (case arm, not a cmd_<verb>()
    # function) and `session working` (nested verb) don't fit this guard's
    # function-based model. Their bash↔Python argv→env parity is pinned instead by
    # test_attention_roster_e6293.py::TestDispatchParity and
    # test_session_working_e6291.py::TestDispatchSessionWorking.
    #
    # ms-178 #770 (maintainability): `session fork cleanup --force` has the same
    # shape (nested verb, case arm) and so is pinned the same way, by
    # test_fork_cleanup_preserves_notes_e6702.py::test_force_flag_parity_across_frontends
    # — which drives --force through BOTH frontends' argv rather than injecting
    # the env var, so a broken parse on either side fails CI.
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
# A case label like ``--priority)``, ``--acceptance-criteria|--ac)`` or
# ``-r|--reason)`` — capture the whole alias group before the closing paren.
# The group may LEAD with a short alias (``-r|--reason``, ``-m|--ms``), so this
# anchors on a single ``-``; callers keep only the ``--`` spellings. Anchoring on
# ``--`` instead (as this did until ms-133 e-6611) silently dropped every flag
# whose case label happened to list its short form first — ``milestone wait``'s
# ``--reason`` and ``milestone occupations``' ``--ms`` among them — which reads as
# "the flag does not exist" and is a false finding, not a missed one.
# Label POSITION is what makes this safe to un-anchor from line start: a label
# follows the ``in`` of a ``case``, a previous arm's ``;;``, or starts the line.
# Bash writes short arms inline (``case "$1" in --json) f=1; shift ;; *) shift
# ;; esac`` — how ``sales target list`` takes ``--json``), so a line-anchored
# pattern misses them and reports the flag as nonexistent. Matching a bare
# ``--flag)`` anywhere instead would be worse: usage prose like
# ``[--naming <p>] | --clear))`` would register ``--clear`` as implemented,
# turning a missed finding into a silent false pass.
_BASH_CASE_FLAG_RE = re.compile(
    r"(?:^[ \t]*|\bin[ \t]+|;;[ \t]*)(-[a-zA-Z0-9|=?*.\-]+)\)", re.MULTILINE
)


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
    # Shared with the ghost-flag check via _case_label_flags (ms-133 e-6611):
    # one scanner, so a fix to flag reading cannot land on only one of two
    # copies and make the two reports contradict each other. This call stays a
    # whole-body scan (no comment/string blanking, no arm scoping) to keep the
    # curated parity contract's behaviour exactly as it was.
    return _case_label_flags(body)


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
# ms-133 e-6611: help registry ↔ parser flag drift
#   "advertised but accepted by no front"
# ---------------------------------------------------------------------------
#
# e-3897 made every help surface render from ONE registry (_help_registry), so
# help can no longer drift against *other help*. What stayed open is help
# drifting against the *parsers*: the registry is a hand-written list, so it can
# advertise a flag that neither entry point accepts. `beacon doc add --title X`
# was the reported case — the registry said ``--title <title>``, both fronts take
# ``title`` as a positional, and the call dies with "'--title' is not a valid
# flag". AI agents trust help and get rejected (AX 原則 1/3; 原則 6 says close
# the gap structurally rather than by proofreading).
#
# What counts as "real" here
# --------------------------
# A flag is real when **at least one** front accepts it: the bash hand-parser
# (macOS/Linux) OR the Python argparse dispatcher (Windows pipx). The registry
# describes one conceptual CLI, so a flag that only one front implements is NOT
# this check's finding — that asymmetry is ``REQUIRED_FLAG_PARITY``'s contract,
# a deliberately curated list. This check answers the narrower question only:
# *does the advertised flag exist anywhere at all?* Keeping the two contracts
# separate is what makes this one safe to run blanket over all 191 entries.
#
# Why flags are unioned along the whole sub-verb path
# ---------------------------------------------------
# The bash front puts a sub-command's flags in two different places:
#   * inside the sub-arm      — ``cmd_doc()`` → ``add)`` → its own arg loop;
#   * in the PARENT arm       — ``stop)`` parses ``--target/--reason/...`` in one
#                               shared loop *before* dispatching on ``scoped``.
# So the flags of ``stop scoped`` live on ``stop``, while the flags of
# ``doc add`` live on ``add``. Unioning every arm along the path handles both
# without special-casing, at the cost of over-approximating across siblings (a
# flag only meaningful for ``stop global`` also counts as existing for
# ``stop scoped``). That trade is deliberate: this check must never cry wolf on
# a flag the CLI really does accept, and "exists but on the sibling" is still
# "exists", which is all it claims.


# Registry commands whose advertised flags are knowingly not resolvable here.
# Every entry needs a reason; an entry that stops being needed must be deleted,
# not left to rot (a stale allowlist silently re-opens the hole it covered).
#
# KEYED BY VERB PATH, not by the registry's display string. A row's display text
# carries placeholders that get reworded for documentation reasons alone
# ("beacon pr add" → "beacon pr add <github-url>" in this very change), and a
# raw-string key would stop matching on such a rename even though nothing about
# the implementation moved. The guard would then report the same flag as a NEW
# ghost and the allowlist row as stale, simultaneously — and both messages would
# point at the wrong repair. The verb path is the part that only changes when the
# command itself does. (ms-133 e-6611; raised independently by both the AX and
# the maintainability review of PR #771, which is why it is fixed rather than
# noted.)
ALLOW_ADVERTISED_FLAG: dict[tuple[str, ...], set[str]] = {
    # --- Fixed by open PR #680 (fix/help-registry-flag-drift), not yet merged.
    # Listed so this guard can land first without editing the same registry
    # lines that PR rewrites. DELETE these three once #680 is on main — if the
    # entries survive the merge, the guard goes red and names them again.
    ("doc", "add"): {"--title"},
    ("stuck", "check"): {"--idle-min"},
    ("milestone", "list"): {"--json"},
}


_HEREDOC_RE = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?")


def _blank_span(line: str, start: int, end: int) -> str:
    """Replace ``line[start:end]`` with spaces, preserving length.

    Every stripper here is length-preserving on purpose: arm boundaries are
    computed as offsets into the stripped text and then used to slice it, so a
    stripper that shortened lines would silently misalign every slice.
    """
    return line[:start] + " " * (end - start) + line[end:]


def _strip_comments_and_strings(body: str, keep_strings: bool = False) -> str:
    """Blank out bash comments and (optionally) quoted string contents.

    Case labels are always bare code — ``--flag)`` never appears inside quotes —
    so prose can only ever produce FALSE matches. Without this, a mere comment
    or usage ``echo`` containing the characters ``in --notaflag)`` registers
    ``--notaflag`` as a flag the CLI implements, and a genuinely nonexistent flag
    advertised in help then passes the guard silently. That is the one failure
    direction this whole check must never have: a detector that cannot fail reads
    as a safety net while guaranteeing nothing (ms-133 e-6611, found by the
    independent AX review of PR #771 with a working reproduction).

    ``keep_strings=True`` leaves quoted contents intact for the test-compare
    reader, which legitimately needs them (``[[ "$2" == "--json" ]]`` puts the
    flag inside quotes). Comments are blanked in both modes.
    """
    out: list[str] = []
    for line in body.splitlines(keepends=True):
        nl = len(line) - len(line.rstrip("\n"))
        text, tail = (line[: len(line) - nl], line[len(line) - nl:]) if nl else (line, "")
        i = 0
        quote: str | None = None
        q_start = 0
        while i < len(text):
            ch = text[i]
            if quote is None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == "#":
                    # Unquoted '#' starts a comment: blank to end of line.
                    text = _blank_span(text, i, len(text))
                    break
                if ch in "\"'":
                    quote = ch
                    q_start = i
            else:
                if ch == "\\" and quote == '"':
                    i += 2
                    continue
                if ch == quote:
                    if not keep_strings:
                        # Blank the contents, keep the delimiters so the shape
                        # of the line (and its length) is unchanged.
                        text = _blank_span(text, q_start + 1, i)
                    quote = None
            i += 1
        out.append(text + tail)
    return "".join(out)
# A case label like ``add)`` / ``list|ls)`` — a verb-shaped arm, never a flag
# arm (those start with ``-`` and are matched by _BASH_CASE_FLAG_RE instead).
_BASH_CASE_VERB_RE = re.compile(
    r"^([ \t]+)([a-z0-9][a-z0-9|_*?.\-]*)\)", re.MULTILINE
)


def _strip_heredocs(body: str) -> str:
    """Blank out heredoc bodies so usage prose can't be read as bash syntax.

    Usage text routinely contains ``…| other)`` and similar, which the case-label
    regex would happily match as an arm — slicing the wrong region and letting a
    ghost flag pass. Replacing heredoc lines with blanks (rather than deleting
    them) keeps every offset stable, so slices computed on the stripped text
    still line up with the original.
    """
    out: list[str] = []
    pending: list[str] = []
    terminator: str | None = None
    for line in body.splitlines(keepends=True):
        if terminator is None:
            out.append(line)
            for m in _HEREDOC_RE.finditer(line):
                pending.append(m.group(1))
            if pending:
                terminator = pending.pop(0)
            continue
        if line.strip() == terminator:
            out.append(line)
            terminator = pending.pop(0) if pending else None
        else:
            # Same length, no bash tokens: offsets stay valid.
            out.append(" " * (len(line) - 1) + "\n" if line.endswith("\n") else " " * len(line))
    return "".join(out)


def _outer_case_arms(body: str) -> "list[tuple[int, int, int, list[str]]]":
    """Split ``body`` into its shallowest-indent case arms.

    Returns ``(label_start, content_start, end, alternatives)`` per arm.
    ``content_start`` sits just past the label so a nested lookup never re-reads
    the arm's own label as if it were an inner arm — doing so would collapse the
    indent floor to the label's own level and make every inner arm invisible.
    "Shallowest indent" is what separates a function's sub-command arms from the
    flag arms nested inside each one.
    """
    labels = [
        (m.start(), m.end(), m.group(1), m.group(2))
        for m in _BASH_CASE_VERB_RE.finditer(body)
    ]
    if not labels:
        return []
    indent = min(len(ind) for _, _, ind, _ in labels)
    outer = [(s, e, lbl) for s, e, ind, lbl in labels if len(ind) == indent]
    arms: list[tuple[int, int, int, list[str]]] = []
    for i, (start, label_end, lbl) in enumerate(outer):
        end = outer[i + 1][0] if i + 1 < len(outer) else len(body)
        arms.append((start, label_end, end, lbl.split("|")))
    return arms


def _preamble_flags(body: str) -> set[str]:
    """Flags parsed in ``body`` *outside* any of its sub-command arms.

    This is the shared arg loop a parent runs before dispatching on the
    sub-command (``stop)`` reads ``--target/--reason/...`` for all of
    ``scoped|global|status``). Taking only the preamble — instead of the whole
    body — is what keeps a sibling's flag from masquerading as this path's:
    ``cmd_doc()`` holds ``--title`` for ``doc update``, and scanning the whole
    function would have declared the advertised ``doc add --title`` real, which
    is the exact bug this guard exists to catch.
    """
    arms = _outer_case_arms(body)
    return _case_flags(body if not arms else body[: arms[0][0]])


# ``[[ "${2:-}" == "--json" ]]`` — a flag read by an explicit test instead of a
# case arm. Restricted to lines that actually open a test (``[``/``[[``) so a
# plain assignment such as ``default="--json"`` is not mistaken for a parser
# accepting the flag.
_BASH_TEST_FLAG_RE = re.compile(r"(?:==|!=|=)[ \t]*\"?(--[a-zA-Z0-9\-]+)\"?")


def _test_compare_flags(body: str) -> set[str]:
    """Long flags a body matches by string comparison inside a ``[``/``[[`` test.

    ``beacon help --json`` is dispatched this way (``if [[ "${2:-}" == "--json"
    ]]``). Reading only case labels would call that flag nonexistent even though
    it is the documented way to get machine-readable help.
    """
    flags: set[str] = set()
    for line in body.splitlines():
        if "[[" not in line and "[ " not in line:
            continue
        for token in _BASH_TEST_FLAG_RE.findall(line):
            if token != "--":
                flags.add(token)
    return flags


def _case_label_flags(body: str) -> set[str]:
    """Long flags named by ``--flag)`` case labels in ``body``.

    The single implementation of "read the flags out of a bash arg loop",
    shared by ``bash_verb_flags`` (curated bash↔Python parity) and
    ``_case_flags`` (the registry ghost-flag check). Keeping one copy matters
    because the two callers' reports are printed side by side: if a fix to this
    scan landed in only one of two duplicated loops, parity could call a flag
    missing while the ghost check calls the same flag real, and nothing would
    say which one to believe.

    Comment / string blanking is the CALLER's job, and the two callers differ:
    ``_case_flags`` blanks both (labels are bare code), while
    ``bash_verb_flags`` is a plain whole-function scan. See ``_case_flags``.
    """
    flags: set[str] = set()
    for group in _BASH_CASE_FLAG_RE.findall(body):
        for token in group.split("|"):
            token = token.split("=")[0]  # normalise ``--x=…`` shapes
            if token.startswith("--") and token != "--":
                flags.add(token)
    return flags


def _case_flags(body: str) -> set[str]:
    """Long flags ``body`` parses — ``--flag)`` case labels plus test compares.

    The two readers need different views of the same text, which is why the
    stripping happens here rather than once upstream: a case label is bare code,
    so quoted prose must be blanked before scanning for it; a test compare keeps
    its flag INSIDE quotes, so that reader needs them left alone. Comments are
    noise to both.

    Note the deliberate difference from ``bash_verb_flags``: that one scans a
    whole ``cmd_<verb>()`` body as one scope and so also sees sibling sub-arms'
    flags, which is fine for its curated parity contract but would make
    ``doc update``'s ``--title`` look like ``doc add``'s. Path-scoped callers go
    through ``bash_flags_for_path``, which separates preamble from arm.
    """
    labels = _case_label_flags(_strip_comments_and_strings(body))
    return labels | _test_compare_flags(
        _strip_comments_and_strings(body, keep_strings=True)
    )


def _bash_main_switch(bin_path: Path = BIN_BEACON) -> str:
    """The user-facing dispatcher switch in bin/beacon (column-0 ``case``).

    Nouns with no ``cmd_<noun>()`` function (``trek``, ``stop``, ``resume``,
    ``claim``, ``dm``) are handled inline here, so a path lookup has to be able
    to fall back to it.

    bin/beacon has MORE THAN ONE column-0 ``case "${1:-}" in``: an early one
    decides whether to relocate to the project root. Taking the first match
    yields a 12-line switch that contains almost no verbs, and every inline noun
    then resolves as "absent" — which this check would report as a ghost flag for
    a flag the CLI really accepts. The dispatcher is the LAST column-0 switch
    (the file's own comment calls it "--- Main dispatch ---"), so scan from the
    end.
    """
    if not bin_path.exists():
        return ""
    text = bin_path.read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r'^case "\$\{1:-\}" in$', text, re.MULTILINE)]
    if not starts:
        return ""
    rest = text[starts[-1]:]
    m = re.search(r"^esac\s*$", rest, re.MULTILINE)
    return rest[: m.end()] if m else rest


def _bash_function_body(name: str, source: str) -> "str | None":
    """Slice ``cmd_<name>()``'s body out of the combined bash source."""
    header = re.compile(r"^" + re.escape(name) + r"\(\)", re.MULTILINE)
    m = header.search(source)
    if m is None:
        return None
    nxt = _BASH_FUNC_RE.search(source, m.end())
    return source[m.start(): nxt.start() if nxt else len(source)]


def _bash_fn_name(tokens: "list[str]") -> str:
    """``["account", "transcript-source"]`` → ``cmd_account_transcript_source``.

    Hyphens in a sub-verb become underscores in the bash function name
    (``transcript-source`` → ``..._transcript_source``); forgetting that reads as
    "handler absent" and would make a real finding invisible.
    """
    return "cmd_" + "_".join(tokens).replace("-", "_")


# ``exec "$BEACON_DIR/bin/beacon" bus send --channel dm "$@"`` — a verb that
# re-enters the CLI at another path instead of parsing flags itself.
_BASH_DELEGATE_RE = re.compile(
    r"""exec\s+(?:"[^"]*/bin/beacon"|\$?\{?BEACON[A-Z_]*\}?/bin/beacon|beacon)\s+"""
    r"""((?:[a-z][a-z0-9\-]*\s+){1,3})""",
    re.VERBOSE,
)


def _delegated_flags(
    body: str, bin_path: Path = BIN_BEACON, _depth: int = 0
) -> set[str]:
    """Flags of the path an arm ``exec``s into, when it delegates parsing.

    ``beacon dm send`` is the live case: the arm validates one thing then
    ``exec``s ``bus send --channel dm "$@"``, so ``--to-user`` /
    ``--recipient-confirmed`` are parsed by ``bus send`` and appear nowhere in the
    ``dm`` arm. Without following the hop, every flag the delegate owns reads as
    nonexistent — a wall of false findings on a verb that works. ``_depth`` caps
    the hops so a delegation cycle can't spin.
    """
    if _depth > 2:
        return set()
    flags: set[str] = set()
    for m in _BASH_DELEGATE_RE.finditer(body):
        target = [tok for tok in m.group(1).split() if not tok.startswith("-")]
        if not target:
            continue
        got = bash_flags_for_path(target, bin_path)
        if got:
            flags |= got
    return flags


def bash_flags_for_path(
    tokens: "list[str]", bin_path: Path = BIN_BEACON
) -> "set[str] | None":
    """Union of long flags the bash front accepts along the sub-verb path.

    ``tokens`` is the full path, noun first (``["doc", "add"]``,
    ``["account", "transcript-source", "set"]``). Resolution tries, in order:
    the most specific dedicated ``cmd_<noun>_<sub>…()`` function, then shorter
    prefixes, then the inline main-switch arm. Whatever anchors the path, the
    remaining tokens are followed down nested case arms and every arm's flags
    along the way are unioned (see the module note on why the parent counts).

    Returns ``None`` when the path can't be anchored at all — a *different*
    failure from "anchored but the flag is absent", and the caller must keep
    them apart: one means "the registry names a command bash doesn't implement",
    the other "the registry names a flag that command doesn't take".
    """
    if not tokens:
        return None
    source = _strip_heredocs(_bash_function_source(bin_path))

    body: str | None = None
    rest: list[str] = []
    for cut in range(len(tokens), 0, -1):
        body = _bash_function_body(_bash_fn_name(tokens[:cut]), source)
        if body is not None:
            rest = list(tokens[cut:])
            break
    if body is None:
        main = _strip_heredocs(_bash_main_switch(bin_path))
        for _s, content, end, alts in _outer_case_arms(main):
            if tokens[0] in alts:
                body = main[content:end]
                rest = list(tokens[1:])
                break
    if body is None:
        return None

    flags = _preamble_flags(body)
    for token in rest:
        for _s, content, end, alts in _outer_case_arms(body):
            if token in alts:
                body = body[content:end]
                flags |= _preamble_flags(body)
                break
        else:
            # The path stops resolving part-way: keep what we gathered rather
            # than returning None. The prefix really is implemented, so this is
            # not the "command absent" case, and the parent arm's shared flag
            # loop is often exactly where the flags live.
            return flags
    # Leaf reached: this arm's own flags are this path's, siblings excluded.
    flags |= _case_flags(body)
    return flags | _delegated_flags(body, bin_path)


def python_flags_for_path(tokens: "list[str]", parser=None) -> "set[str] | None":
    """Long flags argparse accepts at ``tokens``, descending nested subparsers.

    Returns ``None`` when the path doesn't exist in the Python dispatcher (it
    may still exist in bash — the caller unions the two).
    """
    if not tokens:
        return None
    if parser is None:
        try:
            parser = _load_dispatch_parser()
        except Exception as e:  # pragma: no cover - import-environment guard
            print(f"[cli-drift] WARN: could not load dispatch parser ({e})",
                  file=sys.stderr)
            return None
    node = parser
    for token in tokens:
        action = _subparsers_action(node)
        if action is None or token not in action.choices:
            return None
        node = action.choices[token]
    return {
        opt
        for action in node._actions
        for opt in action.option_strings
        if opt.startswith("--")
    }


_REGISTRY_FLAG_RE = re.compile(r"^(--[a-zA-Z0-9][a-zA-Z0-9\-]*)")


def _registry_entries(commands_py: Path = COMMANDS_PY) -> "list[dict]":
    """The raw ``_help_registry()`` rows (command + flags), not just verbs.

    ``parse_help_json`` deliberately reduces to a verb set; this check needs the
    advertised flag strings, so it reads the registry function directly.
    """
    if not commands_py.exists():
        return []
    spec = importlib.util.spec_from_file_location("_beacon_commands_reg", commands_py)
    if spec is None or spec.loader is None:
        return []
    lib_path = str(commands_py.parent)
    added = False
    if lib_path not in sys.path:
        sys.path.insert(0, lib_path)
        added = True
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return list(mod._help_registry())
    except Exception as e:
        print(f"[cli-drift] WARN: could not read _help_registry ({e})", file=sys.stderr)
        return []
    finally:
        if added:
            try:
                sys.path.remove(lib_path)
            except ValueError:
                pass


def _registry_path(command: str) -> "list[str] | None":
    """``"beacon stop scoped <target>"`` → ``["stop", "scoped"]``.

    Stops at the first placeholder (``<id>``) or flag, which is where the verb
    path ends and arguments begin.
    """
    parts = command.split()
    if len(parts) < 2 or parts[0] != "beacon":
        return None
    path: list[str] = []
    for token in parts[1:]:
        if token.startswith("<") or token.startswith("-") or token.startswith("["):
            break
        path.append(token)
    return path or None


def collect_help_flag_drift(
    bin_path: Path = BIN_BEACON,
    commands_py: Path = COMMANDS_PY,
    parser=None,
) -> dict:
    """Every registry-advertised long flag must be accepted by some front.

    Returns ``ok`` plus:
      * ``ghost_flags``      — ``"<command> <flag>"``: advertised, accepted by
                               neither front. The registry is lying; fix the
                               registry (or the parser, if the flag was meant
                               to exist).
      * ``unresolved``       — commands whose verb path neither front exposes.
                               Reported separately and NOT a failure: several
                               registry rows are prose-shaped
                               (``beacon log [message]``) and the sub-verb
                               checks above already own "command missing".
      * ``stale_allowlist``  — ALLOW_ADVERTISED_FLAG entries that no longer
                               correspond to a ghost. A fixed drift must drop
                               its allowlist line, else the next regression on
                               that flag passes silently.
    """
    ghosts: list[str] = []
    unresolved: list[str] = []
    matched_allow: dict[tuple[str, ...], set[str]] = {}

    for entry in _registry_entries(commands_py):
        command = entry.get("command", "")
        advertised: set[str] = set()
        for raw in entry.get("flags", []) or []:
            m = _REGISTRY_FLAG_RE.match(str(raw).strip())
            if m:
                advertised.add(m.group(1))
        if not advertised:
            continue
        path = _registry_path(command)
        if path is None:
            continue

        real: set[str] = set()
        anchored = False
        for getter in (
            lambda: python_flags_for_path(path, parser=parser),
            lambda: bash_flags_for_path(path, bin_path),
        ):
            got = getter()
            if got is not None:
                anchored = True
                real |= got
        if not anchored:
            unresolved.append(command)
            continue

        key = tuple(path)
        allowed = ALLOW_ADVERTISED_FLAG.get(key, set())
        for flag in sorted(advertised - real):
            if flag in allowed:
                matched_allow.setdefault(key, set()).add(flag)
            else:
                ghosts.append(f"{command} {flag}")

    stale: list[str] = []
    for key, flags in ALLOW_ADVERTISED_FLAG.items():
        for flag in sorted(flags - matched_allow.get(key, set())):
            # Rendered as a command line so the report reads like the help it
            # describes, even though the key itself is the verb path.
            stale.append("beacon " + " ".join(key) + " " + flag)

    return {
        "ok": not (ghosts or stale),
        "ghost_flags": sorted(ghosts),
        "unresolved": sorted(unresolved),
        "stale_allowlist": sorted(stale),
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

    # ms-133 e-6611: registry-advertised flags vs what any front accepts.
    help_flag_drift = collect_help_flag_drift(bin_path, commands_path)

    report = {
        "ok": not (
            bin_missing
            or json_missing
            or readme_missing
            or not dispatch_drift["ok"]
            or not flag_parity["ok"]
            or not subverb_drift["ok"]
            or not requires_drift["ok"]
            or not help_flag_drift["ok"]
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
        # ms-133 e-6611 surface (help registry vs the parsers): a flag the help
        # advertises that NO front accepts, and allowlist rows that outlived
        # their drift.
        "ghost_flags": help_flag_drift["ghost_flags"],
        "ghost_flag_unresolved": help_flag_drift["unresolved"],
        "stale_advertised_flag_allowlist": help_flag_drift["stale_allowlist"],
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
    if report.get("ghost_flags"):
        lines.append("  - help advertises a flag NO front accepts (`beacon help --json` lies):")
        for v in report["ghost_flags"]:
            lines.append(f"      {v}")
        lines.append("    -> an AI that trusts help calls this and gets rejected (or, worse, the flag")
        lines.append("       is silently dropped). Fix the `flags` list of that row in")
        lines.append("       lib/commands.py:_help_registry to match the parsers — or implement the")
        lines.append("       flag if help was describing the intent. Only if it genuinely cannot be")
        lines.append("       resolved here, add it to ALLOW_ADVERTISED_FLAG with a reason.")
    if report.get("stale_advertised_flag_allowlist"):
        lines.append("  - ALLOW_ADVERTISED_FLAG entry no longer matches any drift:")
        for v in report["stale_advertised_flag_allowlist"]:
            lines.append(f"      {v}")
        lines.append("    -> the drift it covered is fixed; delete the line. Leaving it in place")
        lines.append("       would silently swallow the next regression on that same flag.")
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
