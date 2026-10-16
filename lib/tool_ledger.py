"""tool_ledger — the declared registry of the tools this project uses (ms-180 e-7000).

WHY this exists
---------------
Beacon rests on three arms: ナレッジ (what is known), ログ (what was done) and
道具 (the means of doing). Two of them have their truth source inside Beacon —
docs / targets / decisions, and commits / pushes / PRs. **The third did not.**
Tools lived only as files: a git repo's ``skills/``, each machine's
``~/.claude/skills``, and whatever MCP servers a host happened to configure.
Beacon's data model held zero records about them.

Three symptoms followed from that, and all three were observed before this
module existed (SPEC ms-180 § 背景):

  1. Copies of the same tool drift (``beacon doctor`` reports 17 skills out of
     sync), because the truth is a file and files get copied.
  2. The log cannot say WITH WHAT something was done — tools have no identifier
     to reference, so the audit loop is cut in one place.
  3. Knowledge can mandate a tool that does not exist. ``CLAUDE.md`` declares a
     hard dependency on ``/review``; a doc referencing a *milestone* is checked
     by CI, a doc referencing a *tool* was checked by nothing.

What this module is — and deliberately is not
---------------------------------------------
It is **a ledger of declarations, not a runtime**. Each row says what a tool is
for, what it does, and where its use gets recorded. Beacon holds no tool
implementations (SPEC 方針5): distribution stays with the repo / the terminal /
TrailNode, and this module holds only "what exists, what it can do, is it
callable here". So there is no ``tool run`` verb here and never should be —
wrapping the execution of hundreds of MCP tools was the rejected alternative
(SPEC 方針2), and it would drag the tool implementations back inside Beacon.

The ledger answers two questions asked by machinery that ALREADY runs on every
tool call, replacing a guess in each case (SPEC 方針1):

  * ``PostToolUse`` → *does using this tool get recorded, and how?*
    Previously a hand-written ``case`` statement in ``bin/beacon-save-hook.sh``
    that named two Google-Drive servers and silently dropped everything else.
  * ``PreToolUse``  → *what does this tool do?*
    Previously a verb-token regex over the tool's NAME (``tool_effect``).

Both callers are interrupts, not AI procedure. Nothing here asks the AI to route
its work through Beacon — an AI that forgets to ask would be exactly the "wiring
exists but nothing runs" failure ms-166 spent a milestone removing.

Why the truth is a local file
-----------------------------
The ledger is read **on every tool call** — the hottest path Beacon has. Putting
its truth behind a network round trip would add latency to calling any tool at
all, so the local file is authoritative in both local and cloud mode, and the
cloud copy is a one-way mirror for audit and cross-session reading (SPEC 方針8).
``lookup`` therefore performs no I/O beyond reading one small JSON file, and the
callers that live in a hook cache it per process.

Three values for the record sink, not two
-----------------------------------------
``record.sink`` is ``specified`` / ``declined`` / ``undecided`` (SPEC 方針4).
A single empty value would let a row nobody has judged yet read as "decided not
to record" — the exact shape (collapsing *absent* and *unexamined* into one
value) that ms-166 kept having to pull back apart. ``undecided`` is a debt
marker: the tool is registered, and the question of whether its use is evidence
is openly unanswered.

Completeness is not a goal
--------------------------
The ledger does not try to enumerate every tool on the machine (SPEC 方針6).
Scanning terminals and reconciling would reproduce the very disease
``skills-drift`` already is: detecting divergence forever instead of removing the
possibility of it. Unregistered is not silent either — ``PreToolUse`` sees every
call, so a tool missing from the ledger surfaces the moment it is used. "We do
not scan" and "the gap does not quietly stay open" hold at the same time.
"""

from __future__ import annotations

import json
import os
from typing import Optional

# One-way dependency: this module imports the pure classifier, and the classifier
# takes a ledger as a PARAMETER (it performs no I/O and imports nothing from here).
# That keeps the pair acyclic while letting the pinned-tool set be derived from the
# classifier's own table instead of copied.
import tool_effect as _tool_effect

# ---------------------------------------------------------------------------
# Vocabulary. Each set is closed and validated — an unknown value is refused at
# write time rather than stored and reinterpreted later by whoever reads it.
# ---------------------------------------------------------------------------

#: Where the ledger's truth lives, relative to a project root.
LEDGER_RELPATH = (".beacon", "tools.json")

#: Schema version of the on-disk file. Bump only for a breaking shape change.
LEDGER_VERSION = 1

# 作用 (what a call DOES). Three values, matching the question the SPEC asks:
# does it only read, does it change state, does it send outward?
EFFECT_READ_ONLY = "read-only"
EFFECT_STATE_CHANGE = "state-change"
EFFECT_OUTWARD = "outward"
EFFECTS = (EFFECT_READ_ONLY, EFFECT_STATE_CHANGE, EFFECT_OUTWARD)

# 記録先 (the sink), three-valued on purpose — see module docstring.
SINK_SPECIFIED = "specified"
SINK_DECLINED = "declined"
SINK_UNDECIDED = "undecided"
SINKS = (SINK_SPECIFIED, SINK_DECLINED, SINK_UNDECIDED)

# 粒度 (grain). ``each`` writes one record per call; ``turn`` folds a turn's
# calls of the same tool into one. Chosen per tool rather than globally: reading
# one mailbox can fan out into twenty calls while sending one mail is one call,
# and there is no reason those two share a grain (SPEC 方針3).
GRAIN_EACH = "each"
GRAIN_TURN = "turn"
GRAINS = (GRAIN_EACH, GRAIN_TURN)

#: Recorders a ``specified`` sink may name. Each must be a verb that already
#: exists; the ledger declares WHERE a use is recorded, it does not invent sinks.
RECORDER_SAVE = "save"          # `beacon save` — an external-artifact entry
RECORDER_EVIDENCE = "evidence"  # `beacon target evidence` — 証跡 (one use of a tool)
RECORDERS = (RECORDER_SAVE, RECORDER_EVIDENCE)


class ToolLedgerError(ValueError):
    """Raised for an invalid registration. A ValueError subclass so callers that
    already catch ValueError (the CLI command layer) keep working."""


# ---------------------------------------------------------------------------
# Tools whose effect a declaration may NOT soften.
# ---------------------------------------------------------------------------
# ``tool_effect`` classifies by name, and for most tools that is a guess — which
# is the whole reason to replace it with a declaration. But a handful of built-in
# tools are not guesses: ``Bash`` executes arbitrary commands, ``Write``/``Edit``
# mutate files, ``WebFetch`` reaches an arbitrary URL (so it doubles as an
# exfiltration channel), ``Task``/``Agent`` spawn an agent whose downstream
# effects are unbounded. The ms-169 approval gate exists because untrusted DM
# text in the context must not reach precisely those.
#
# If the ledger could declare ``Bash`` read-only, registering a row would silently
# disarm that gate. So those names are pinned: a registration that softens one is
# refused when written AND ignored when read. Both, because the file is a file —
# the CLI is one writer, a text editor is another, and a guard that only covers
# the writer it knows about is not a guard (the lesson from ms-174: enumerate
# every writer to the state you are protecting).
#
# Hardening in the other direction is always allowed: a declaration may mark a
# tool MORE dangerous than the heuristic guessed.
# The NAMES are DERIVED from ``tool_effect.BUILTIN_SIDE_EFFECT`` rather than
# re-listed here. Re-listing them would make the pin a hand-maintained copy of
# another module's table: add one tool there and the ledger would quietly become
# able to demote it, with every test still green. Deriving makes the pin extend
# itself, and ``tests/test_tool_effect_ledger_ms180`` asserts the two sets are
# equal so the derivation cannot be replaced by a copy without going red.
#
# Only the ``outward`` flavour needs naming: ``WebFetch`` reaches an ARBITRARY url,
# so it is an exfiltration channel and not merely a local mutation. Everything else
# in that table changes local state.
_PINNED_OUTWARD = frozenset({"WebFetch"})

NON_SOFTENABLE_EFFECT = {
    name: (EFFECT_OUTWARD if name in _PINNED_OUTWARD else EFFECT_STATE_CHANGE)
    for name in _tool_effect.BUILTIN_SIDE_EFFECT
}


def is_softening(name: str, effect: str) -> bool:
    """True when declaring ``effect`` for ``name`` would weaken what is already
    POSITIVELY known about it.

    Only ``read-only`` can soften: it is the one value that makes the ms-169 gate
    let a call through. Declaring ``outward`` for ``Bash`` — stricter than its
    pinned ``state-change`` — is fine and stays allowed.

    Two kinds of positive knowledge are protected, and the distinction is what
    keeps this guard from eating the ledger's whole purpose:

      1. the PINNED built-ins (``Bash`` / ``Write`` / ``WebFetch`` / …), whose
         effect is known from the harness, not guessed; and
      2. a name carrying a WRITE VERB (``mcp__gmail__send_email`` → ``send``).
         A write verb is a positive signal, not an absence of one — so a row
         saying 「読み取りだけ」 about it is a detectable lie, and the ledger does
         not pass it.

    What stays declarable is exactly the case the ledger exists for: a name with
    NO recognisable verb (``mcp__weird__frobnicate``), which the heuristic can
    only default-deny. There, the declaration replaces a guess rather than
    contradicting evidence — so over-gating gets fixed while under-gating cannot
    be introduced.

    DECLARED TENSION — this narrows e-7003 受入条件1 (ms-180 / 2026-10-16)
    --------------------------------------------------------------------
    Clause 2 is a deliberate, disclosed exception to the task's own AC1
    (「台帳に載っている道具については、作用の判定が台帳の宣言を読む形になる
    (名前の形から推測しない)」). For a registered tool whose NAME carries a write
    verb, the name — not the row — decides, which is the opposite of what AC1
    asks. Writing that down rather than leaving it implicit:

      * AC3's supporting clause is 「…嘘を台帳が通さない形**も考える**」 — "also
        consider", a suggestion, NOT a requirement. An earlier version of this
        docstring cited it as e-7004 (wrong task) and the adjudication record
        called it 明示要求 (overstated). A neutral audit of that adjudication
        caught both; the decline stands, the justification was one-sided.
      * The line is kept because the asymmetry is real: a write verb is positive
        evidence of effect, while "no recognisable verb" is the absence of
        evidence. Overriding evidence is a different act from resolving an
        unknown, and SPEC 受入条件6 (「ms-169 の承認ゲートが緩まない」) is the
        stronger constraint when the two pull apart.
      * The residual cost is real and NOT hidden: a genuinely read-only tool
        whose name happens to contain ``run`` / ``execute`` / ``mark`` /
        ``download`` / … cannot be declared read-only and stays gated in an
        untrusted turn. That is the fail-safe direction, but it is a cost, and
        the owner of the ledger should know it exists rather than discover it.
    """
    if effect != EFFECT_READ_ONLY:
        return False
    if name in NON_SOFTENABLE_EFFECT:
        return True
    return _tool_effect.has_write_verb(name)


def ledger_path(root: "str | os.PathLike" = ".") -> str:
    """Absolute path of the ledger file for a project root."""
    return os.path.join(str(root), *LEDGER_RELPATH)


# ---------------------------------------------------------------------------
# Read / write. Tolerant reads (a missing file is an empty ledger, not an error)
# so every consumer can be added without first requiring the file to exist.
# ---------------------------------------------------------------------------

def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "tools": []}


def load_ledger(path: str) -> dict:
    """Read the ledger. A missing or unreadable file yields an EMPTY ledger.

    Tolerant on purpose: an unreadable ledger must mean "nothing is declared",
    which routes every consumer to its own fallback — ``PreToolUse`` back to the
    fail-closed heuristic, ``PostToolUse`` to recording nothing. The failure mode
    of a corrupt file is therefore "no declarations", never "wrong declarations".
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return empty_ledger()
    if not isinstance(data, dict):
        return empty_ledger()
    tools = data.get("tools")
    if not isinstance(tools, list):
        return empty_ledger()
    return {
        "version": data.get("version", LEDGER_VERSION),
        "tools": [t for t in tools if isinstance(t, dict) and t.get("name")],
    }


def save_ledger(path: str, ledger: dict) -> None:
    """Write the ledger atomically (temp file + replace) so a reader on the hot
    path never observes a half-written file."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "version": LEDGER_VERSION,
        "tools": sorted(ledger.get("tools") or [],
                        key=lambda t: str(t.get("name") or "")),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Registration (the write side).
# ---------------------------------------------------------------------------

def build_record(name: str, *, purpose: str, effect: str,
                 recorder: str = "", grain: str = "",
                 decline_reason: str = "",
                 sink: str = "", at: str = "", by: str = "") -> dict:
    """Validate one registration and return the row to store.

    ``sink`` is derived rather than asked for when it can be: naming a recorder
    means ``specified``, giving a decline reason means ``declined``, and giving
    neither means ``undecided``. A caller may pass ``sink`` explicitly to say
    "undecided" out loud. Validate-before-mutate: every check runs here, so a
    caller cannot half-write a row (same contract as ``target_engine``'s
    child-field writers).

    ``purpose`` is REQUIRED (ms-180 e-7006 受入条件2). The criterion asked to be
    able to tell an intentionally-blank 用途 from a forgotten one, and the honest
    way to answer that is to make the ambiguous state unreachable rather than to
    add a third value for it: every tool is used FOR something, so a row without
    a purpose is always an omission. The read side still reports a blank one
    (an older or hand-edited file can hold it), because a rule enforced on only
    one of two writers is not enforced.

    ``grain`` defaults to ``each`` when omitted, and the row RECORDS that the
    value was defaulted rather than chosen (``grain_source``) — 受入条件4: an
    unspecified grain must not silently become one of the two.
    """
    tool = str(name or "").strip()
    if not tool:
        raise ToolLedgerError("道具の名前が必要です (例: mcp__gmail__send_email)")
    purpose_txt = str(purpose or "").strip()
    if not purpose_txt:
        raise ToolLedgerError(
            "用途が必要です (--purpose \"<何のために使うか>\")。"
            "用途は後から「何のために登録したのか」を読む唯一の列なので、"
            "空のまま登録できる形にしていません "
            "(空欄だと『用途なし』と『書き忘れ』が区別できなくなります)。")
    if effect not in EFFECTS:
        raise ToolLedgerError(
            f"作用は {list(EFFECTS)} のいずれかです (指定: {effect!r})")
    if is_softening(tool, effect):
        # Say WHICH kind of positive knowledge is being contradicted: a pinned
        # built-in, or a write verb in the name. A single generic message would
        # leave the author guessing which rule they hit (and the two have
        # different right answers — one is "you cannot", the other is often
        # "you meant state-change / outward").
        if tool in NON_SOFTENABLE_EFFECT:
            why = (f"この道具はハーネス側で作用が既知で "
                   f"({NON_SOFTENABLE_EFFECT[tool]})、推測ではありません")
        else:
            why = ("この名前は書き込み / 送信を表す動詞を含んでいます "
                   "(= 作用の積極的な証拠)。宣言が「読み取りだけ」と言っても"
                   "それは検知できる嘘になります")
        raise ToolLedgerError(
            f"『{tool}』を {EFFECT_READ_ONLY} として登録できません。{why}。"
            "ms-169 の承認ゲートがこの判定に依っているため、台帳の宣言で"
            "ゲートを緩めることはできません。厳しくする方向 "
            f"({EFFECT_STATE_CHANGE} / {EFFECT_OUTWARD}) の宣言は可能です。")
    grain_given = str(grain or "").strip()
    grain_source = "declared" if grain_given else "default"
    resolved_grain = grain_given or GRAIN_EACH
    if resolved_grain not in GRAINS:
        raise ToolLedgerError(
            f"粒度は {list(GRAINS)} のいずれかです (指定: {grain!r})")

    rec_name = (recorder or "").strip()
    reason = (decline_reason or "").strip()
    if rec_name and reason:
        raise ToolLedgerError(
            "記録先の指定 (--record) と記録しない宣言 (--no-record) は同時に"
            "指定できません。どちらか一方を選んでください。")
    if rec_name:
        if rec_name not in RECORDERS:
            raise ToolLedgerError(
                f"記録先は {list(RECORDERS)} のいずれかです (指定: {rec_name!r})")
        resolved_sink = SINK_SPECIFIED
    elif reason:
        resolved_sink = SINK_DECLINED
    else:
        resolved_sink = SINK_UNDECIDED
    if sink:
        if sink not in SINKS:
            raise ToolLedgerError(
                f"記録先の状態は {list(SINKS)} のいずれかです (指定: {sink!r})")
        if sink != resolved_sink:
            raise ToolLedgerError(
                f"記録先の状態 {sink!r} が指定内容と矛盾します "
                f"(指定内容からは {resolved_sink!r})")
    # ``declined`` REQUIRES a reason — that is what distinguishes it from
    # ``undecided``. Without one the two are indistinguishable in the file, and
    # the 3-value split that this ledger exists to keep would collapse back to 2.
    if resolved_sink == SINK_DECLINED and not reason:
        raise ToolLedgerError(
            "記録しないと決めた場合は理由が必要です "
            "(--no-record は理由とセットで指定してください)")

    # ``grain_source`` makes the defaulting VISIBLE in the stored row (受入条件4).
    # Without it, "nobody chose a grain" and "someone chose 毎回" are the same
    # bytes on disk — the same collapse of *unexamined* into a value that the
    # 3-valued record sink exists to prevent, just one field over.
    record_block = {"sink": resolved_sink, "grain": resolved_grain,
                    "grain_source": grain_source}
    if resolved_sink == SINK_SPECIFIED:
        record_block["recorder"] = rec_name
    if resolved_sink == SINK_DECLINED:
        record_block["reason"] = reason

    row = {
        "name": tool,
        "purpose": purpose_txt,
        "effect": effect,
        "record": record_block,
    }
    if at:
        row["registered_at"] = at
    if by:
        row["registered_by"] = by
    return row


def upsert(ledger: dict, row: dict) -> dict:
    """Insert or replace a row by tool name, in place, and return the ledger.
    Re-registering a tool is an update, not a duplicate — the ledger is keyed by
    the tool's name, so two rows for one name would make every reader's answer
    depend on list order."""
    name = str(row.get("name") or "")
    tools = [t for t in _rows(ledger)
             if isinstance(t, dict) and str(t.get("name") or "") != name]
    tools.append(row)
    ledger["tools"] = tools
    ledger.setdefault("version", LEDGER_VERSION)
    return ledger


def remove(ledger: dict, name: str) -> bool:
    """Drop a tool's row. True when a row was actually removed."""
    before = list(_rows(ledger))
    after = [t for t in before if str(t.get("name") or "") != str(name)]
    ledger["tools"] = after
    return len(after) != len(before)


# ---------------------------------------------------------------------------
# Lookup (the read side) — what the two interrupts call.
# ---------------------------------------------------------------------------

def _rows(ledger) -> list:
    """The ledger's rows, tolerant of ANY shape.

    Every read path funnels through here so a malformed ledger reads as "no
    declarations" rather than raising. This tolerance used to live in
    ``tool_effect._declared_effect``'s try/except; moving the translation to this
    module (to break the import cycle) dropped it, and ``gate_classification``
    began raising ``AttributeError`` on a non-dict. The hooks happen to always
    pass ``load_ledger``'s output (always a dict), so nothing would have failed in
    production — which is exactly why it needed a test rather than a reading.
    The property belongs HERE, at the one place every reader goes through, not in
    each caller's except clause.
    """
    if not isinstance(ledger, dict):
        return []
    rows = ledger.get("tools")
    return rows if isinstance(rows, list) else []


def lookup(ledger: dict, name: str) -> Optional[dict]:
    """The row declared for ``name``, or ``None`` when the tool is unregistered.

    Exact-name match only. No prefix or substring matching: a loose match is how
    a guard ends up green for the wrong reason, and the readers of this function
    are a security gate and a recorder (the repeated lesson written down as CORE
    doc ``jnFsoNaNvuZegI73noCn`` — a guard must close on the property itself, and
    a partial match closes on a coincidence)."""
    target = str(name or "").strip()
    if not target:
        return None
    for row in _rows(ledger):
        if isinstance(row, dict) and str(row.get("name") or "").strip() == target:
            return row
    return None


def effect_of(ledger: dict, name: str) -> Optional[str]:
    """The DECLARED effect of ``name``, or ``None`` when undeclared / invalid.

    ``None`` means "the ledger has no answer" and sends the caller to its own
    fallback — it never means read-only. A row whose stored effect is outside the
    vocabulary, or one that contradicts positive knowledge about the tool
    (a pinned built-in, or a write verb in the name), also yields ``None``: this
    is the read-side half of the guard, so hand-editing the file cannot do what
    the CLI refuses to do."""
    row = lookup(ledger, name)
    if row is None:
        return None
    effect = row.get("effect")
    if effect not in EFFECTS:
        return None
    if is_softening(str(name or "").strip(), effect):
        return None
    return effect


def gate_classification(ledger: dict, name: str) -> str:
    """The ledger's answer in the EFFECT GATE's own two-value vocabulary, or ""
    when the ledger has no usable answer (ms-180 e-7003).

    Returns ``tool_effect.READ_ONLY`` / ``tool_effect.SIDE_EFFECT`` / ``""``.
    ``""`` means "no declaration" and sends the caller back to its fail-closed
    heuristic — it never means read-only.

    The translation lives HERE, not in ``tool_effect``, because the dependency
    between the two modules runs one way: this module imports the classifier (to
    derive the pinned set from its table), so the classifier must not import this
    one. It previously did, via an import hidden inside a function body — a real
    cycle whose crash was merely deferred to call time, found by an independent
    maintainability review of PR #802. Keeping the translation on this side makes
    the one-way direction true rather than asserted.

    Safety is unchanged: it goes through ``effect_of``, which refuses to answer
    for a row contradicting positive knowledge about the tool.
    """
    declared = effect_of(ledger, name)
    if declared == EFFECT_READ_ONLY:
        return _tool_effect.READ_ONLY
    if declared in (EFFECT_STATE_CHANGE, EFFECT_OUTWARD):
        return _tool_effect.SIDE_EFFECT
    return ""


def record_plan(ledger: dict, name: str) -> dict:
    """What should happen, record-wise, for one call of ``name``.

    Returns a dict that is always safe to read::

        {"action": "record",    "recorder": ..., "grain": ...}  # sink specified
        {"action": "skip",      "reason": ...}                  # declined, with why
        {"action": "undecided"}                                 # registered, unjudged
        {"action": "unregistered"}                              # not in the ledger

    The four actions are distinct because the callers do four different things
    with them, and because folding ``undecided`` into ``skip`` would re-create the
    silent gap this ledger exists to remove: a row nobody has judged would read
    as a decision not to record.
    """
    row = lookup(ledger, name)
    if row is None:
        return {"action": "unregistered"}
    block = row.get("record")
    if not isinstance(block, dict):
        return {"action": "undecided"}
    sink = block.get("sink")
    if sink == SINK_SPECIFIED:
        recorder = block.get("recorder")
        if recorder not in RECORDERS:
            # Declared "specified" but naming no usable recorder: that is a
            # broken row, not a decision to record. Report it as undecided so it
            # shows up as debt rather than silently recording nowhere.
            return {"action": "undecided"}
        grain = block.get("grain")
        return {
            "action": "record",
            "recorder": recorder,
            "grain": grain if grain in GRAINS else GRAIN_EACH,
        }
    if sink == SINK_DECLINED:
        return {"action": "skip", "reason": str(block.get("reason") or "")}
    return {"action": "undecided"}


def purposeless_tools(ledger: dict) -> list:
    """Rows whose 用途 is blank. ``build_record`` refuses to create one, so this
    only catches an older file or a hand edit — and that is exactly why it
    exists: a rule enforced on one writer and not the other is not enforced
    (the ms-174 lesson)."""
    return [row for row in _rows(ledger)
            if isinstance(row, dict) and not str(row.get("purpose") or "").strip()]


def grain_defaulted_tools(ledger: dict) -> list:
    """Rows whose grain was DEFAULTED rather than chosen (受入条件4), so "nobody
    decided" stays readable after the fact. A row from before ``grain_source``
    existed is reported as defaulted — it predates the choice being recorded, so
    claiming it was declared would be an invention."""
    out = []
    for row in _rows(ledger):
        if not isinstance(row, dict):
            continue
        block = row.get("record") if isinstance(row.get("record"), dict) else {}
        if block.get("grain_source") != "declared":
            out.append(row)
    return out


#: ``record_plan`` actions that mean "the use left NO record, and that is a DEBT
#: somebody has to clear" — as opposed to ``skip`` (declined = a decision, not a
#: debt) and ``record`` (nothing owed).
#:
#: Defined ONCE here because both tool-call interrupts ask the same question and
#: each used to hardcode the literal tuple. An independent maintainability review
#: of PR #802 pointed out that this is the very duplication the same PR guards
#: against for ``NON_SOFTENABLE_EFFECT`` (derived, never re-listed) — so if a
#: fifth outcome is ever added, one hook could be updated and the other silently
#: left behind, with nothing to say so.
DEBT_ACTIONS = frozenset({"unregistered", "undecided"})

#: Every action ``record_plan`` can return, named once where it is produced.
#: Callers that explain these actions to a reader (``beacon tool used``) hold a
#: table keyed by them; without the vocabulary written down, a fifth action
#: would be added here and that table would fall through to printing the raw
#: action word, which is the shape a sibling guard in this module already
#: forbids for the recorder and effect vocabularies (ms-180 follow-up,
#: independent maintainability review of PR #805).
RECORD_PLAN_ACTIONS = frozenset({"record", "skip", "undecided", "unregistered"})


def is_debt_action(action: str) -> bool:
    """True when a ``record_plan`` action is a debt worth noting (see DEBT_ACTIONS)."""
    return str(action or "") in DEBT_ACTIONS


def undecided_tools(ledger: dict) -> list:
    """Rows whose record sink nobody has judged yet — the ledger's own debt list."""
    return [row for row in _rows(ledger)
            if isinstance(row, dict)
            and record_plan(ledger, row.get("name")).get("action") == "undecided"]


def find_by_purpose(ledger: dict, query: str) -> list:
    """Tools whose 用途 matches ``query``, best match first (ms-180 e-7006).

    The 用途 column is the one column no interrupt consumes — it is read by people
    and by the AI, and it exists so that two ways of doing the same job do not
    quietly coexist. Matching is therefore deliberately generous (this is a
    lookup aid, not a guard): a row matches when it shares any token with the
    query, ranked by how many tokens it shares. The loose-match prohibition
    applies to guards, where a coincidental match grants safety; here a
    coincidental match only shows one extra row to a reader who can see why.
    """
    tokens = [t for t in _normalize(query).split() if t]
    if not tokens:
        return []
    scored = []
    for row in _rows(ledger):
        if not isinstance(row, dict):
            continue
        haystack = _normalize(
            f"{row.get('purpose') or ''} {row.get('name') or ''}")
        hits = sum(1 for t in tokens if t in haystack)
        if hits:
            scored.append((hits, str(row.get("name") or ""), row))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [row for _, _, row in scored]


def _normalize(text: str) -> str:
    """Lowercase and split on separators so ``createGoogleDoc`` and
    ``create_google_doc`` normalize alike. Japanese text has no token separator,
    so it is kept whole and matched as a substring by ``find_by_purpose``."""
    import re
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    return re.sub(r"[^0-9A-Za-z぀-ヿ㐀-鿿]+", " ", spaced).lower()


__all__ = [
    "LEDGER_RELPATH", "LEDGER_VERSION",
    "EFFECT_READ_ONLY", "EFFECT_STATE_CHANGE", "EFFECT_OUTWARD", "EFFECTS",
    "SINK_SPECIFIED", "SINK_DECLINED", "SINK_UNDECIDED", "SINKS",
    "GRAIN_EACH", "GRAIN_TURN", "GRAINS",
    "RECORDER_SAVE", "RECORDER_EVIDENCE", "RECORDERS",
    "NON_SOFTENABLE_EFFECT", "is_softening",
    "ToolLedgerError",
    "ledger_path", "empty_ledger", "load_ledger", "save_ledger",
    "build_record", "upsert", "remove",
    "lookup", "effect_of", "gate_classification", "record_plan",
    "DEBT_ACTIONS", "RECORD_PLAN_ACTIONS", "is_debt_action",
    "undecided_tools",
    "purposeless_tools", "grain_defaulted_tools", "find_by_purpose",
]
