"""ms-169 e-6236 — effect-based tool classification.

The ms-169 A gate (e-6237) pauses for human approval when untrusted content is
live AND the AI is about to call a *side-effect* tool. This module locks the
classifier that draws that line by EFFECT, not tool family (方針3):

  * read-only (Read / Grep / list・get・search 系 / MCP reads) → pass
  * write / outward (Write / Bash / MCP send・create・update・dml / Skill
    dm-send・discord-post・deploy) → gate target
  * unknown tool / unrecognisable verb → default-deny (gate target)

The default-deny tail is the security property: a tool we cannot positively
recognise as read-only must never slip through as read-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import tool_effect as te  # noqa: E402


# --- built-in read-only pass -------------------------------------------------

def test_builtin_read_only_tools_pass():
    for name in ("Read", "Grep", "Glob", "LS", "NotebookRead", "WebSearch"):
        assert te.classify_tool(name) == te.READ_ONLY, name
        assert te.is_side_effect(name) is False, name


# --- built-in side-effect gate ----------------------------------------------

def test_builtin_side_effect_tools_gate():
    for name in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
                 "Task", "Agent"):
        assert te.classify_tool(name) == te.SIDE_EFFECT, name
        assert te.is_side_effect(name) is True, name


def test_webfetch_is_side_effect_as_outward_channel():
    # WebFetch reaches an arbitrary URL → doubles as an exfiltration sink even
    # though it "reads". Fail-closed → gate.
    assert te.is_side_effect("WebFetch") is True


# --- MCP writes gate ---------------------------------------------------------

def test_mcp_write_tools_gate():
    for name in (
        "mcp__gmail__send_email",
        "mcp__gmail__draft_email",
        "mcp__slack-ga__conversations_add_message",
        "mcp__salesforce__salesforce_dml_records",
        "mcp__google-drive__createFolder",
        "mcp__google-drive__updateGoogleDoc",
        "mcp__outlook__delete-event",
        "mcp__chatwork__post_room_message",
        "mcp__beacon-bus__reply",
    ):
        assert te.is_side_effect(name) is True, name


# --- MCP reads pass ----------------------------------------------------------

def test_mcp_read_tools_pass():
    for name in (
        "mcp__gmail__read_email",
        "mcp__gmail__search_emails",
        "mcp__slack-ga__channels_list",
        "mcp__salesforce__salesforce_query_records",
        "mcp__google-drive__search",
        "mcp__google-calendar__list-events",
        "mcp__google-calendar__get-event",
        "mcp__snowflake__describe_object",
    ):
        assert te.classify_tool(name) == te.READ_ONLY, name


def test_mixed_verb_name_classifies_as_write():
    # get_or_create_label reads OR creates → the strongest effect (create) wins.
    assert te.is_side_effect("mcp__gmail__get_or_create_label") is True


def test_unknown_mcp_verb_defaults_to_gate():
    # A verb we don't recognise on an MCP tool must default-deny, not pass.
    assert te.is_side_effect("mcp__weird__frobnicate_thing") is True


def test_degenerate_mcp_name_defaults_to_gate():
    assert te.is_side_effect("mcp__serveronly") is True


# --- Skills ------------------------------------------------------------------

def test_skill_send_publish_deploy_gate():
    for skill in ("beacon-dm-send", "beacon-dm-reply", "discord-post",
                  "beacon-deploy", "beacon-push", "beacon-sales-email"):
        assert te.is_side_effect("Skill", {"skill": skill}) is True, skill


def test_skill_name_from_command_string():
    assert te.is_side_effect("Skill", {"command": "/beacon-dm-send foo"}) is True


def test_skill_without_resolvable_name_defaults_to_gate():
    assert te.is_side_effect("Skill", {}) is True
    assert te.is_side_effect("Skill", None) is True


# --- camelCase + generic + fail-closed --------------------------------------

def test_camel_case_verbs():
    assert te.classify_tool("listEvents") == te.READ_ONLY
    assert te.classify_tool("createEvent") == te.SIDE_EFFECT


def test_unknown_tool_defaults_to_gate():
    assert te.is_side_effect("SomeFutureTool") is True
    assert te.is_side_effect("frobnicate") is True


def test_empty_or_non_string_name_is_side_effect():
    assert te.is_side_effect("") is True
    assert te.is_side_effect("   ") is True
    assert te.is_side_effect(None) is True  # type: ignore[arg-type]
    assert te.is_side_effect(123) is True  # type: ignore[arg-type]
