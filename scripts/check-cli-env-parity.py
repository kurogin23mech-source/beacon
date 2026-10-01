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
goes green today and every NEW drift fails. Each row carries its verdict, in
the shape ``scripts/check-pid-liveness.py``'s ALLOWLIST uses (the reason lives
with the entry, not in a ticket) — see that file for the established form. The backlog is data, not a verdict:
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
# Values a frontend is not supposed to pass. Scoped to ``(verb, env)``; the
# verb ``"*"`` means genuinely process-wide. Scoping matters because env names
# are ad-hoc strings: a global exemption added to clear ONE verb also silences
# every other verb that happens to read the same name, now and in future, with
# nothing in the output naming the collateral (AX review, PR #781 A-4). The
# "*" rows are the ones where that breadth is the intent, stated as a choice.
AMBIENT_ENV = {
    # genuinely process-wide: set by the operator's environment or by a layer
    # above argv, never by a flag on any verb.
    ("*", "BEACON_DEBUG"),
    ("*", "BEACON_SESSION_ID"),          # resolved by the session layer
    ("*", "BEACON_SESSION_KIND"),
    # set by the autonomous-execution envelope, not by a human's argv
    ("*", "BEACON_OPERATION_AUTO_EXECUTE"),
    ("*", "BEACON_OPERATION_ENVELOPE_ID"),
    # per-verb operator / test knobs, documented as env-only (no flag intended)
    ("doctor", "BEACON_DOCTOR_MIN_VERSION"),
    ("doctor", "BEACON_DOCTOR_SKIP_CLOUD_STATE"),
    ("doctor", "BEACON_DOCTOR_SKIP_MS81"),
    ("doctor", "BEACON_DOCTOR_SKIP_PRINCIPLE_MARKER"),
    ("doctor", "BEACON_DOCTOR_SKIP_SKILL_DRIFT"),
    ("bus_send", "BEACON_QUALGATE_OFF"),
    ("summary", "BEACON_SUPPRESS_DEPRECATION"),
}


def _is_ambient(verb: str, env: str) -> bool:
    return ("*", env) in AMBIENT_ENV or (verb, env) in AMBIENT_ENV


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


def _env_keys_of(node) -> set:
    """BEACON_* names this expression contributes as dict KEYS."""
    out = set()
    if isinstance(node, ast.Dict):
        for k in node.keys:
            if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                    and k.value.startswith("BEACON_")):
                out.add(k.value)
    return out


def _copy_source(node):
    """If this expression copies another local dict (``dict(x)`` / ``{**x}``),
    the name it copies from."""
    if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict"
            and len(node.args) == 1 and isinstance(node.args[0], ast.Name)):
        return node.args[0].id
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None and isinstance(v, ast.Name):   # {**x}
                return v.id
    return None


def _python_sets(dispatch_path: pathlib.Path):
    """verb -> BEACON_* names the handler actually PUTS IN the env it passes.

    Structural, not "the name appears somewhere in this function". The first
    version matched any string constant in the enclosing function, so a TODO
    note (``_todo = ["BEACON_X"]``), a help string or a log message naming the
    variable counted as having wired it — a false negative that an agent
    documenting intent before implementing would walk straight into
    (AX review, PR #781 A-1, reproduced with a decoy).

    Tracked here: dict literals passed as the env argument, dict literals
    assigned to a local that is then passed, ``d["BEACON_X"] = ...`` subscript
    writes to such a local, and ``d.update({...})``. Anything subtler is not
    counted — which keeps the error on the reporting side, never the silencing
    side.
    """
    tree = ast.parse(dispatch_path.read_text(encoding="utf-8"))
    sets = collections.defaultdict(set)
    verbs = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # local name -> BEACON_* keys written into it. Two passes, because a
        # copy (``env = dict(base_env)``) can precede or follow the literal it
        # copies depending on how the handler is written.
        locals_: dict = collections.defaultdict(set)
        copies: list = []          # (dest, source) for dict(x) / {**x}

        def _record(tgt, value):
            keys = _env_keys_of(value)
            if isinstance(tgt, ast.Name):
                if keys:
                    locals_[tgt.id] |= keys
                src = _copy_source(value)
                if src:
                    copies.append((tgt.id, src))
            # env["BEACON_X"] = ...
            if (isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Name)
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                    and tgt.slice.value.startswith("BEACON_")):
                locals_[tgt.value.id].add(tgt.slice.value)

        for node in ast.walk(fn):
            # plain and ANNOTATED assignment (``base_env: Dict[str, str] = {...}``
            # is an AnnAssign, and missing it made every key of the biggest env
            # dict in the file invisible)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    _record(tgt, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                _record(node.target, node.value)
            # env.update({...})
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "update"
                    and isinstance(getattr(node.func, "value", None), ast.Name)
                    and node.args):
                locals_[node.func.value.id] |= _env_keys_of(node.args[0])

        # propagate copies to a fixed point (dict(dict(x)) chains are rare but
        # cost nothing to follow)
        for _ in range(len(copies) + 1):
            changed = False
            for dest, src in copies:
                if locals_.get(src) and not locals_[src] <= locals_[dest]:
                    locals_[dest] |= locals_[src]
                    changed = True
            if not changed:
                break

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "_run_commands_py"
                    and len(node.args) >= 2):
                continue
            v = node.args[1]
            if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                continue
            verb = v.value
            verbs.add(verb)
            passed = set()
            env_arg = node.args[2] if len(node.args) >= 3 else None
            for kw in node.keywords:
                if kw.arg == "env":
                    env_arg = kw.value
            if isinstance(env_arg, ast.Dict):
                passed |= _env_keys_of(env_arg)
            elif isinstance(env_arg, ast.Name):
                passed |= locals_.get(env_arg.id, set())
            elif env_arg is not None:
                # an expression we do not model (a call, a merge, …): fall back
                # to every key this function builds, so an unmodelled shape
                # reports LESS rather than inventing a gap.
                passed |= set().union(*locals_.values()) if locals_ else set()
            sets[verb] |= passed
    return sets, verbs


_DISPATCH_RE = re.compile(r'\$COMMANDS_PY"?\s+([a-z0-9_]+)')
_ASSIGN_RE = re.compile(r'\b(BEACON_[A-Z0-9_]+)=')
# How far above the dispatch line the `VAR=... python3 "$COMMANDS_PY" <verb>`
# idiom spreads its assignments. Generous on purpose: over-reaching only makes
# the checker quieter, never noisier.
_BASH_LOOKBACK = 60


def _quote_spans(line: str):
    """[(start, end)] of quoted regions, and the offset where a comment starts."""
    spans, quote, start, i = [], None, 0, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                spans.append((start, i))
                quote = None
        elif ch in "'\"":
            quote, start = ch, i
        elif ch == "#":
            return spans, i
        i += 1
    if quote:
        spans.append((start, len(line)))
    return spans, len(line)


def _bash_assignments(line: str) -> set:
    """BEACON_* names ASSIGNED on this line.

    A name inside a comment or inside a quoted string is not wiring: an
    `# example: BEACON_X=1` that outlived the code it described, or an echoed
    usage line, must not read as coverage (AX review, PR #781 A-2).

    Positions are filtered rather than the text blanked. Blanking also erased
    `"$COMMANDS_PY"` — the marker that identifies the dispatch line — which
    silently reduced the bash side to ZERO verbs. Caught by measuring; it
    would have shipped a guard that saw nothing at all on that frontend.
    """
    spans, comment_at = _quote_spans(line)
    out = set()
    for m in _ASSIGN_RE.finditer(line):
        pos = m.start(1)
        if pos >= comment_at:
            continue
        if any(a < pos < b for a, b in spans):
            continue
        out.add(m.group(1))
    return out


def _bash_sets(bin_path: pathlib.Path, lib_dir: pathlib.Path):
    """verb -> BEACON_* names assigned on the path that dispatches it.

    Each file is scanned INDEPENDENTLY. Concatenating bin/beacon with every
    bin/lib/*.sh let a dispatch line near the top of a short file reach its
    lookback window back into the tail of an unrelated preceding file and
    count that file's assignments as coverage — and six of the per-command
    files are shorter than the window (AX review, PR #781 A-2).
    """
    sets = collections.defaultdict(set)
    verbs = set()
    for src in [bin_path] + sorted(lib_dir.glob("*.sh")):
        try:
            lines = src.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        assigns = [_bash_assignments(l) for l in lines]
        for i, line in enumerate(lines):
            _, comment_at = _quote_spans(line)
            m = _DISPATCH_RE.search(line[:comment_at])   # not a commented-out call
            if not m:
                continue
            verb = m.group(1)
            verbs.add(verb)
            lo = max(0, i - _BASH_LOOKBACK)              # never crosses into
            for j in range(lo, i + 1):                   # another file
                sets[verb] |= assigns[j]
    return sets, verbs


def collect(root: pathlib.Path = ROOT, exclude=None) -> list:
    """[(verb, env, frontend)] for every env a reaching frontend never sets.

    ``exclude`` defaults to the recorded backlog; pass ``set()`` for the raw
    set. Taking it as an ARGUMENT rather than reading the module global lets
    the stale check ask for the unfiltered answer without briefly emptying a
    constant other code may be reading (maintainability review, PR #781 M-4 —
    and the hazard was not theoretical: the clear/restore broke the moment the
    backlog became a dict).
    """
    if exclude is None:
        exclude = KNOWN_GAPS
    reads = _verb_reads(root / "lib")
    py_sets, py_verbs = _python_sets(root / "beacon_cli" / "dispatch.py")
    sh_sets, sh_verbs = _bash_sets(root / "bin" / "beacon", root / "bin" / "lib")

    out = []
    for verb in sorted(reads):
        for env in sorted(e for e in reads[verb] if not _is_ambient(verb, e)):
            if verb in sh_verbs and env not in sh_sets.get(verb, set()):
                out.append((verb, env, "bash"))
            if verb in py_verbs and env not in py_sets.get(verb, set()):
                out.append((verb, env, "python"))
    return [row for row in out if row not in exclude]


def main() -> int:
    rows = collect()
    # Scan ONCE. Written inline in the comprehension this re-ran the whole
    # scan per allowlist entry (68 scans, 52s instead of under 2).
    raw = set(_collect_raw())
    stale = sorted(g for g in KNOWN_GAPS if g not in raw)
    if not rows and not stale:
        print("[cli-env-parity] OK: no NEW (verb, env, frontend) gap outside "
              "the recorded backlog ({0} rows). Method: per-handler def-use on "
              "the Python side, per-file comment/quote-aware scan on the bash "
              "side — both err toward reporting less, so this is 'nothing new "
              "was detected', not 'every mapping is proven'."
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
    """collect() without the backlog subtraction (for stale detection)."""
    return collect(exclude=set())


# The backlog as it stood when this checker was written (ms-160 e-6674). Data,
# not a verdict: every row carries its classification inline. Burning it down is
# follow-up work; the checker exists so the list can only shrink.
#
# WHY A BARE COORDINATE IS ENOUGH HERE — AND WHEN IT STOPS BEING ENOUGH.
# An independent review (PR #781 AX-3, severity high) asked for a content
# fingerprint per row, so that a DIFFERENT defect landing on an already-listed
# (verb, env, frontend) could not hide behind the old entry. That was declined,
# because this checker reports exactly one state — "this frontend does not put
# this key in the env it passes" — and exactly one thing produces it: no
# mapping. There is no second cause for a fingerprint to tell apart.
#
# That argument has an expiry, and it is worth stating because the next person
# to touch this will not re-derive it. It holds only while the checker tests
# KEY PRESENCE. Two things it deliberately does not look at today:
#   (a) the key is passed but its VALUE comes from the wrong flag;
#   (b) the key is passed on only ONE branch of a conditional.
# Both currently count as "passed". The moment this checker is tightened to
# judge either of them, a single coordinate gains two distinct meanings
# ("absent" vs "present but wrong"), the one-cause argument fails, and
# KNOWN_GAPS needs the fingerprint AX-3 asked for. Reconsider it then rather
# than rediscovering the question.
KNOWN_GAPS: dict = {
    ("account_add", "BEACON_ACCOUNT_ASSIGNEE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_contact", "BEACON_CONTACT_PHONE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_delete", "BEACON_CANCEL_REASON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_list", "BEACON_AS_PROJECT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("account_list", "BEACON_LINKED", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("acquisition_delete", "BEACON_ACKNOWLEDGE", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("bus_directory", "BEACON_DIR_CWD_ONLY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_ALLOW_DUPLICATE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_CLIENT_EVENT_ID", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_CONTEXT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("bus_send", "BEACON_BUS_DEDUP_WINDOW_SEC", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("bus_send", "BEACON_BUS_IS_RETRY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_RATIONALE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_RECIPIENT_CONFIRMED", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("bus_send", "BEACON_BUS_RECIPIENT_USER", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("cloud_check_project", "BEACON_CLOUD_PROJECT_ID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("cloud_list", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_list", "BEACON_BACKEND", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_record", "BEACON_BACKEND", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_record", "BEACON_JSON", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("deploy_record", "BEACON_VERSION", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("deploy_rollback", "BEACON_REGION", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("doc_add", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_add", "BEACON_FORCE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_add", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_delete", "BEACON_REASON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_INCLUDE_TRASHED", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_list", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_ACCOUNT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_MS_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_OPPORTUNITY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("doc_update", "BEACON_OP_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("issue_sync", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("log", "BEACON_RESOLVES_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("log_finalize", "BEACON_RESOLVES_SET", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("meeting_cancel", "BEACON_MTG_CANCEL_REASON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("member_role", "BEACON_JSON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("opportunity_add", "BEACON_OPP_ASSIGNEE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("opportunity_delete", "BEACON_CANCEL_REASON", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("opportunity_list", "BEACON_ALL", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("pr_create", "BEACON_GH_ARGS", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("pr_create", "BEACON_GH_ARGS", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("push_record", "BEACON_VERSION", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("rollback", "BEACON_ROLLBACK_CWD", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("rollback", "BEACON_ROLLBACK_CWD", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("rollback", "BEACON_ROLLBACK_NO_RECORD", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("run_record", "BEACON_JSON", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sales_gmail_permalink", "BEACON_MSGID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sales_gmail_permalink", "BEACON_MSGID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("search", "BEACON_ACTOR", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_CLAIMANT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_BUS_DM", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_SESSION_LOGS", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_INCLUDE_TREK", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("search", "BEACON_SOURCE", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("skill_install", "BEACON_SETTINGS_PATH", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("sync", "BEACON_MS_ID", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("sync", "BEACON_MS_ID", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("task_cancel", "BEACON_JSON", "bash"):
        "bash 未配線 / python は渡す — bash 側の取りこぼし",
    ("trek_pulse_ack", "BEACON_TREK_BLOCKERS", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trek_pulse_ack", "BEACON_TREK_NEEDS_LEADER", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trek_pulse_ack", "BEACON_TREK_STATE_SUMMARY", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trek_pulse_ack", "BEACON_TREK_TIME_ON_TASK", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trek_task_state", "BEACON_TREK_ATTAINMENT_VERDICT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trek_task_state", "BEACON_TREK_VERDICT", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("trigger_fire", "BEACON_TRIGGER_TREK_ID", "python"):
        "python 未配線 / bash は渡す — 二重フロント drift。Windows・pipx から使えない",
    ("view", "BEACON_VIEW_SKIP_HANDSHAKE", "bash"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
    ("view", "BEACON_VIEW_SKIP_HANDSHAKE", "python"):
        "両フロント未配線 — 旗が存在しない。内部用か未実装かは個別判定が要る",
}

if __name__ == "__main__":
    sys.exit(main())
