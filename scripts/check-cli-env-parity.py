#!/usr/bin/env python3
"""Every env var a verb READS must be passed by the frontends that reach it
(ms-160 e-6674).

Beacon has two CLI frontends — ``bin/beacon`` (bash) and
``beacon_cli/dispatch.py`` (the Windows / pipx shim). Both translate argv into
environment variables and then run ``lib/commands.py <verb>``. The thing that
actually carries a flag's value is therefore the **flag → env mapping**, and
that mapping is written out by hand, twice.

``scripts/check-cli-help-drift.py`` compares flag NAMES. It cannot see this
class, and the reason is structural: *a flag missing from one frontend is not
"unequal", it is absent* — two frontends that both lack a flag look aligned,
and a frontend that lacks a flag the other has drops out of the comparison
rather than failing it. Its ``REQUIRED_FLAG_PARITY`` closes the gap for a
hand-curated handful of verbs; everything else is unchecked.

What that costs, measured: ``beacon doc add --force``, ``search --source``,
``deploy record --version``, ``cloud list --json`` and ~40 more are accepted by
bash and REJECTED by the Python shim. A Windows operator following a Skill
hits ``unrecognized arguments``. ms-160 e-6674 found the class after
``meeting reschedule`` silently dropped a calendar assignment; PR #778 found
``bus send --recipient-confirmed`` missing, which made cross-user DM
impossible from Windows — not inconvenient, impossible.

So this checker compares the MAPPING, for every verb, not a curated list:

  reads(verb)   — the BEACON_* names ``cmd_<verb>()`` pulls out of os.environ
  bash(verb)    — the BEACON_*= assignments on the path that dispatches <verb>
  python(verb)  — the BEACON_* keys the ``_handle_*`` that dispatches <verb>
                  mentions

A name a verb reads but a frontend reaching it never sets is reported.

KNOWN_GAPS carries the backlog that existed when the checker was written, so it
goes green today and every NEW drift fails. The backlog is data, not a verdict:
each line is classified in the companion audit (see the task's notes), and
burning it down is follow-up work, not a precondition for stopping the bleeding.

Deliberately conservative — both directions of imprecision are stated:
  * the Python side is read per ENCLOSING FUNCTION, so a handler that mentions
    a name anywhere counts as passing it. That under-reports (a name mentioned
    but not actually threaded through slips by) and never over-reports.
  * the bash side reads the 60 lines above the dispatch line, which is where
    the ``VAR=... python3 "$COMMANDS_PY" <verb>`` idiom puts its assignments.

Plain script, no pytest import: scripts/ci-strict-drift-guards.sh calls it from
jobs that have no pytest (ms-160 e-6349).
"""

import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Env names that are never a frontend's job to pass: operator / test knobs read
# straight from the environment by design, and values a frontend resolves for
# itself. Matching is exact on the name.
AMBIENT_ENV = {
    # operator + test knobs (documented as env-only; no flag is intended)
    "BEACON_DEBUG", "BEACON_QUALGATE_OFF", "BEACON_SUPPRESS_DEPRECATION",
    "BEACON_DOCTOR_MIN_VERSION", "BEACON_DOCTOR_SKIP_CLOUD_STATE",
    "BEACON_DOCTOR_SKIP_MS81", "BEACON_DOCTOR_SKIP_PRINCIPLE_MARKER",
    "BEACON_DOCTOR_SKIP_SKILL_DRIFT",
    # resolved by the session layer, not supplied on the command line
    "BEACON_SESSION_ID", "BEACON_SESSION_KIND",
    # set by the autonomous-execution envelope, not by a human's argv
    "BEACON_OPERATION_AUTO_EXECUTE", "BEACON_OPERATION_ENVELOPE_ID",
}


def _verb_reads(lib_dir: pathlib.Path) -> dict:
    """verb -> BEACON_* names its cmd_<verb>() reads from os.environ."""
    reads = collections.defaultdict(set)
    for path in sorted(lib_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith("cmd_"):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", None) in ("get", "getenv")
                        and node.args):
                    a = node.args[0]
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and a.value.startswith("BEACON_")):
                        reads[fn.name[4:]].add(a.value)
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)
                        and node.slice.value.startswith("BEACON_")):
                    reads[fn.name[4:]].add(node.slice.value)
    return reads


def _python_sets(dispatch_path: pathlib.Path):
    """verb -> BEACON_* names the _handle_* dispatching it mentions.

    Per enclosing function rather than per call site: handlers build the env in
    a local (``env = {...}; _run_commands_py(root, verb, env)``), and chasing
    the variable would add precision the report does not need — this direction
    of error under-reports, never over-reports.
    """
    tree = ast.parse(dispatch_path.read_text(encoding="utf-8"))
    sets = collections.defaultdict(set)
    verbs = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        keys, local = set(), set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.startswith("BEACON_")):
                keys.add(node.value)
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "_run_commands_py"
                    and len(node.args) >= 2):
                v = node.args[1]
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    local.add(v.value)
        for v in local:
            verbs.add(v)
            sets[v] |= keys
    return sets, verbs


_DISPATCH_RE = re.compile(r'\$COMMANDS_PY"?\s+([a-z0-9_]+)')
_ASSIGN_RE = re.compile(r'\b(BEACON_[A-Z0-9_]+)=')
# How far above the dispatch line the `VAR=... python3 "$COMMANDS_PY" <verb>`
# idiom spreads its assignments. Generous on purpose: over-reaching only makes
# the checker quieter, never noisier.
_BASH_LOOKBACK = 60


def _bash_sets(bin_path: pathlib.Path, lib_dir: pathlib.Path):
    text = bin_path.read_text(encoding="utf-8")
    for sh in sorted(lib_dir.glob("*.sh")):
        text += "\n" + sh.read_text(encoding="utf-8")
    lines = text.splitlines()
    sets = collections.defaultdict(set)
    verbs = set()
    for i, line in enumerate(lines):
        m = _DISPATCH_RE.search(line)
        if not m:
            continue
        verb = m.group(1)
        verbs.add(verb)
        window = "\n".join(lines[max(0, i - _BASH_LOOKBACK):i + 1])
        sets[verb] |= set(_ASSIGN_RE.findall(window))
    return sets, verbs


def collect(root: pathlib.Path = ROOT) -> list:
    """[(verb, env, frontend)] for every env a reaching frontend never sets."""
    reads = _verb_reads(root / "lib")
    py_sets, py_verbs = _python_sets(root / "beacon_cli" / "dispatch.py")
    sh_sets, sh_verbs = _bash_sets(root / "bin" / "beacon", root / "bin" / "lib")

    out = []
    for verb in sorted(reads):
        for env in sorted(reads[verb] - AMBIENT_ENV):
            if verb in sh_verbs and env not in sh_sets.get(verb, set()):
                out.append((verb, env, "bash"))
            if verb in py_verbs and env not in py_sets.get(verb, set()):
                out.append((verb, env, "python"))
    return [row for row in out if row not in KNOWN_GAPS]


def main() -> int:
    rows = collect()
    # Scan ONCE. Written inline in the comprehension this re-ran the whole
    # scan per allowlist entry (68 scans, 52s instead of under 2).
    raw = set(_collect_raw())
    stale = sorted(g for g in KNOWN_GAPS if g not in raw)
    if not rows and not stale:
        print("[cli-env-parity] OK: every env a verb reads is passed by the "
              "frontends that reach it ({0} known gaps still allowlisted)."
              .format(len(KNOWN_GAPS)))
        return 0
    if rows:
        print("[cli-env-parity] a verb reads an env its frontend never passes:",
              file=sys.stderr)
        for verb, env, front in rows:
            print(f"  {verb}: {env}  (not set by the {front} frontend)",
                  file=sys.stderr)
        print("", file=sys.stderr)
        print("  The flag parses but its value never reaches the implementation,", file=sys.stderr)
        print("  or the flag does not exist on that frontend at all. Name parity", file=sys.stderr)
        print("  (check-cli-help-drift.py) cannot see this — an absent flag is not", file=sys.stderr)
        print("  'unequal'. See ms-160 e-6674.", file=sys.stderr)
        print("  -> wire the flag → env mapping on that frontend, or add the row to", file=sys.stderr)
        print("     AMBIENT_ENV if the value is genuinely not a frontend's to pass.", file=sys.stderr)
    if stale:
        print("[cli-env-parity] KNOWN_GAPS rows that are no longer gaps:", file=sys.stderr)
        for verb, env, front in stale:
            print(f"  {verb}: {env} ({front})", file=sys.stderr)
        print("  -> delete them; a fixed gap left in the list exempts the next "
              "regression at the same spot.", file=sys.stderr)
    return 1


def _collect_raw() -> list:
    """collect() without the KNOWN_GAPS subtraction (for stale detection)."""
    saved = set(KNOWN_GAPS)
    KNOWN_GAPS.clear()
    try:
        return collect()
    finally:
        KNOWN_GAPS.update(saved)


# The backlog as it stood when this checker was written (ms-160 e-6674). Data,
# not a verdict: each row is classified in the task's audit. Burning it down is
# follow-up work; the checker exists so the list can only shrink.
KNOWN_GAPS: set = {
    ("account_add", "BEACON_ACCOUNT_ASSIGNEE", "python"),
    ("account_contact", "BEACON_CONTACT_PHONE", "python"),
    ("account_delete", "BEACON_CANCEL_REASON", "python"),
    ("account_list", "BEACON_AS_PROJECT", "python"),
    ("account_list", "BEACON_LINKED", "python"),
    ("acquisition_delete", "BEACON_ACKNOWLEDGE", "bash"),
    ("bus_directory", "BEACON_DIR_CWD_ONLY", "python"),
    ("bus_send", "BEACON_BUS_ALLOW_DUPLICATE", "python"),
    ("bus_send", "BEACON_BUS_CLIENT_EVENT_ID", "python"),
    ("bus_send", "BEACON_BUS_CONTEXT", "python"),
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "bash"),
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "python"),
    ("bus_send", "BEACON_BUS_IS_RETRY", "python"),
    ("bus_send", "BEACON_BUS_RATIONALE", "python"),
    ("bus_send", "BEACON_BUS_RECIPIENT_CONFIRMED", "python"),
    ("bus_send", "BEACON_BUS_RECIPIENT_USER", "python"),
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "bash"),
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "python"),
    ("cloud_list", "BEACON_JSON", "python"),
    ("deploy_list", "BEACON_BACKEND", "python"),
    ("deploy_record", "BEACON_BACKEND", "python"),
    ("deploy_record", "BEACON_JSON", "bash"),
    ("deploy_record", "BEACON_VERSION", "python"),
    ("deploy_rollback", "BEACON_REGION", "bash"),
    ("doc_add", "BEACON_ACCOUNT", "python"),
    ("doc_add", "BEACON_FORCE", "python"),
    ("doc_add", "BEACON_OPPORTUNITY", "python"),
    ("doc_delete", "BEACON_REASON", "python"),
    ("doc_list", "BEACON_ACCOUNT", "python"),
    ("doc_list", "BEACON_INCLUDE_TRASHED", "python"),
    ("doc_list", "BEACON_OPPORTUNITY", "python"),
    ("doc_update", "BEACON_ACCOUNT", "python"),
    ("doc_update", "BEACON_MS_SET", "python"),
    ("doc_update", "BEACON_OPPORTUNITY", "python"),
    ("doc_update", "BEACON_OP_SET", "python"),
    ("log", "BEACON_RESOLVES_SET", "python"),
    ("log_finalize", "BEACON_RESOLVES_SET", "python"),
    ("meeting_cancel", "BEACON_MTG_CANCEL_REASON", "python"),
    ("opportunity_add", "BEACON_OPP_ASSIGNEE", "python"),
    ("opportunity_delete", "BEACON_CANCEL_REASON", "python"),
    ("pr_create", "BEACON_GH_ARGS", "bash"),
    ("pr_create", "BEACON_GH_ARGS", "python"),
    ("push_record", "BEACON_VERSION", "python"),
    ("rollback", "BEACON_ROLLBACK_CWD", "bash"),
    ("rollback", "BEACON_ROLLBACK_CWD", "python"),
    ("rollback", "BEACON_ROLLBACK_NO_RECORD", "bash"),
    ("run_record", "BEACON_JSON", "bash"),
    ("sales_gmail_permalink", "BEACON_MSGID", "bash"),
    ("sales_gmail_permalink", "BEACON_MSGID", "python"),
    ("search", "BEACON_ACTOR", "python"),
    ("search", "BEACON_CLAIMANT", "python"),
    ("search", "BEACON_INCLUDE_BUS_DM", "python"),
    ("search", "BEACON_INCLUDE_SESSION_LOGS", "python"),
    ("search", "BEACON_INCLUDE_TREK", "python"),
    ("search", "BEACON_SOURCE", "python"),
    ("skill_install", "BEACON_SETTINGS_PATH", "bash"),
    ("sync", "BEACON_MS_ID", "bash"),
    ("sync", "BEACON_MS_ID", "python"),
    ("task_cancel", "BEACON_JSON", "bash"),
    ("trek_pulse_ack", "BEACON_TREK_BLOCKERS", "python"),
    ("trek_pulse_ack", "BEACON_TREK_NEEDS_LEADER", "python"),
    ("trek_pulse_ack", "BEACON_TREK_STATE_SUMMARY", "python"),
    ("trek_pulse_ack", "BEACON_TREK_TIME_ON_TASK", "python"),
    ("trek_task_state", "BEACON_TREK_ATTAINMENT_VERDICT", "python"),
    ("trek_task_state", "BEACON_TREK_VERDICT", "python"),
    ("trigger_fire", "BEACON_TRIGGER_TREK_ID", "python"),
    ("view", "BEACON_VIEW_SKIP_HANDSHAKE", "bash"),
    ("view", "BEACON_VIEW_SKIP_HANDSHAKE", "python"),
}

if __name__ == "__main__":
    sys.exit(main())
