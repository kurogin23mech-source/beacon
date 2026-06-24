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

It deliberately ignores positional arg shape and flag spelling — those
are too noisy to diff and are checked elsewhere (the dispatcher itself
enforces ``--flag`` parsing). The drift this catches is the most common
one in practice: a brand-new subcommand that nobody added to the README
or to ``cmd_help_json``.

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
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN_BEACON = ROOT / "bin" / "beacon"
COMMANDS_PY = ROOT / "lib" / "commands.py"
README = ROOT / "README.md"
PYTHON_DISPATCH = ROOT / "beacon_cli" / "dispatch.py"


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
    """Extract the verb set from the bash dispatcher's usage() text.

    We look for lines indented with two spaces inside the heredoc that
    begin with ``beacon ``. Skip the closing ``EOF`` and section headers.
    """
    if not path.exists():
        return set()
    verbs: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        # usage() block lines look like:  "  beacon milestone add ..."
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

    bin_missing: set[str] = set()
    json_missing: set[str] = set()
    readme_missing: set[str] = set()

    for v in union:
        if v == "":
            # Bare `beacon` (dashboard launch) — appears as different rows in
            # all three sources but isn't a "subcommand". Skip entirely.
            continue
        in_bin = v in bin_verbs
        in_json = v in json_verbs
        in_readme = v in readme_verbs
        if not in_bin and v not in ALLOW_MISSING_FROM_BIN_HELP:
            bin_missing.add(v)
        if not in_json and v not in ALLOW_MISSING_FROM_HELP_JSON:
            json_missing.add(v)
        if not in_readme and v not in ALLOW_MISSING_FROM_README:
            readme_missing.add(v)

    # ms-44 e-1171: bash main case vs Python _HANDLERS parity.
    dispatch_drift = collect_dispatch_drift(bin_path, python_dispatch_path)

    report = {
        "ok": not (
            bin_missing
            or json_missing
            or readme_missing
            or not dispatch_drift["ok"]
        ),
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
    }
    return report


def _format_text(report: dict) -> str:
    if report["ok"]:
        return (
            "[cli-drift] OK: bin/beacon, cmd_help_json, README CLI tables, "
            "and bash↔Python dispatch are aligned.\n"
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
