"""PreToolUse cross-user DM send guard (ms-110 / e-3444).

Pins the client-side forcing function: deny a cross-project (proxy for
cross-user) DM sent via the bus primitive directly, while never blocking the
carved-out paths (reply / Trek·operation channel / non-dm / same-project /
Skill-token) — AC "同一 user / Trek / Operation 経路は hook で誤 block しない".
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import dm_send_guard as guard  # noqa: E402

LOCAL = "proj-mine"
OTHER = "proj-other"


# ---------------------------------------------------------------------------
# parse_send_target
# ---------------------------------------------------------------------------

def test_parse_mcp_reply_tool():
    t = guard.parse_send_target(guard.MCP_REPLY_TOOL, {
        "channel": "dm", "recipient_project_id": OTHER,
        "recipient_session_id": "sv-bob", "in_reply_to": "",
    })
    assert t["recipient_project_id"] == OTHER
    assert t["channel"] == "dm"


def test_parse_bus_send_bash_flags():
    cmd = f"beacon bus send --channel dm --to sv-bob --project {OTHER} --payload '{{}}'"
    t = guard.parse_send_target("Bash", {"command": cmd})
    assert t["channel"] == "dm"
    assert t["recipient_session_id"] == "sv-bob"
    assert t["recipient_project_id"] == OTHER


def test_parse_bus_send_equals_form():
    cmd = f"beacon bus send --channel=dm --recipient-project={OTHER}"
    t = guard.parse_send_target("Bash", {"command": cmd})
    assert t["channel"] == "dm"
    assert t["recipient_project_id"] == OTHER


def test_parse_ignores_non_send_bash():
    assert guard.parse_send_target("Bash", {"command": "beacon bus receive"}) is None
    assert guard.parse_send_target("Bash", {"command": "ls -la"}) is None


def test_parse_ignores_unrelated_tool():
    assert guard.parse_send_target("Read", {"file_path": "/x"}) is None


# ---------------------------------------------------------------------------
# decide — deny path
# ---------------------------------------------------------------------------

def test_cross_project_no_token_denied():
    t = {"channel": "dm", "recipient_project_id": OTHER, "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_DENY
    assert d["reason"] == guard.REASON_CROSS_PROJECT_NO_TOKEN
    assert "/beacon-dm-send" in d["message"]


def test_deny_message_present_for_bash_primitive():
    cmd = f"beacon bus send --channel dm --to sv-bob --project {OTHER}"
    t = guard.parse_send_target("Bash", {"command": cmd})
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_DENY


# ---------------------------------------------------------------------------
# decide — carve-outs (never block)
# ---------------------------------------------------------------------------

def test_reply_allowed():
    t = {"channel": "dm", "recipient_project_id": OTHER, "in_reply_to": "ev-1"}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_REPLY


@pytest.mark.parametrize("channel", ["operation-trigger", "trek-progress-check",
                                     "trek-trigger", "trek-task-review"])
def test_trek_scoped_channel_allowed(channel):
    t = {"channel": channel, "recipient_project_id": OTHER, "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_TREK_SCOPE


def test_non_dm_channel_allowed():
    t = {"channel": "claim-signal", "recipient_project_id": OTHER, "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_NON_DM


def test_same_project_recipient_allowed():
    t = {"channel": "dm", "recipient_project_id": LOCAL, "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_SAME_PROJECT


def test_no_recipient_project_treated_as_same_project():
    """A --to session with no explicit project is a local recipient."""
    t = {"channel": "dm", "recipient_project_id": "",
         "recipient_session_id": "sv-mine2", "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_SAME_PROJECT


def test_not_guarded_tool_allowed():
    d = guard.decide(None, local_project_id=LOCAL, token=None)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_NOT_GUARDED


# ---------------------------------------------------------------------------
# decide — token pass (single-use)
# ---------------------------------------------------------------------------

def test_valid_matching_token_allows_and_consumes():
    t = {"channel": "dm", "recipient_project_id": OTHER,
         "recipient_session_id": "sv-bob", "in_reply_to": ""}
    token = {"recipient_project_id": OTHER, "recipient_session_id": "sv-bob"}
    d = guard.decide(t, local_project_id=LOCAL, token=token)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["reason"] == guard.REASON_TOKEN_OK
    assert d["consume_token"] is True


def test_token_for_other_recipient_does_not_pass():
    """A token confirming Bob must not authorise a send to Carol."""
    t = {"channel": "dm", "recipient_project_id": OTHER,
         "recipient_session_id": "sv-carol", "in_reply_to": ""}
    token = {"recipient_project_id": "proj-bob", "recipient_session_id": "sv-bob"}
    d = guard.decide(t, local_project_id=LOCAL, token=token)
    assert d["action"] == guard.ACTION_DENY


# ---------------------------------------------------------------------------
# token file lifecycle
# ---------------------------------------------------------------------------

def test_issue_read_consume_roundtrip(tmp_path):
    bd = str(tmp_path / ".beacon")
    guard.issue_token(recipient_project_id=OTHER, recipient_session_id="sv-bob",
                      beacon_dir=bd, issued_at_ts=1000)
    tok = guard.read_valid_token(bd, now_ts=1000)
    assert tok is not None
    assert tok["recipient_project_id"] == OTHER
    assert guard.consume_token(bd) is True
    # After consume, gone.
    assert guard.read_valid_token(bd, now_ts=1000) is None


def test_expired_token_not_returned(tmp_path):
    bd = str(tmp_path / ".beacon")
    guard.issue_token(recipient_project_id=OTHER, beacon_dir=bd, issued_at_ts=1000)
    stale = 1000 + guard.TOKEN_TTL_SECONDS + 1
    assert guard.read_valid_token(bd, now_ts=stale) is None


def test_issue_requires_a_recipient(tmp_path):
    with pytest.raises(ValueError):
        guard.issue_token(beacon_dir=str(tmp_path / ".beacon"))


def test_missing_token_file_reads_none(tmp_path):
    assert guard.read_valid_token(str(tmp_path / ".beacon")) is None


# ---------------------------------------------------------------------------
# End-to-end through the token file (issue → decide with fresh token)
# ---------------------------------------------------------------------------

def test_skill_issued_token_allows_cross_project_send(tmp_path):
    bd = str(tmp_path / ".beacon")
    guard.issue_token(recipient_project_id=OTHER, recipient_session_id="sv-bob",
                      beacon_dir=bd, issued_at_ts=2000)
    token = guard.read_valid_token(bd, now_ts=2000)
    t = {"channel": "dm", "recipient_project_id": OTHER,
         "recipient_session_id": "sv-bob", "in_reply_to": ""}
    d = guard.decide(t, local_project_id=LOCAL, token=token)
    assert d["action"] == guard.ACTION_ALLOW
    assert d["consume_token"] is True


# ---------------------------------------------------------------------------
# Entrypoint (bin/beacon-pretooluse-dm-guard.py) end-to-end via subprocess
# ---------------------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402

_HOOK = os.path.join(os.path.dirname(__file__), "..", "bin",
                     "beacon-pretooluse-dm-guard.py")


def _run_hook(event: dict):
    p = subprocess.run(
        [sys.executable, _HOOK], input=json.dumps(event),
        capture_output=True, text=True, timeout=20,
    )
    return p


def _cwd_with_project(tmp_path, project_id):
    bd = tmp_path / ".beacon"
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "cloud.json").write_text(json.dumps({"project_id": project_id}))
    return str(tmp_path)


def test_entrypoint_denies_cross_project_reply_tool(tmp_path):
    cwd = _cwd_with_project(tmp_path, LOCAL)
    p = _run_hook({
        "hook_event_name": "PreToolUse",
        "tool_name": guard.MCP_REPLY_TOOL,
        "tool_input": {"channel": "dm", "recipient_project_id": OTHER,
                       "recipient_session_id": "sv-bob"},
        "cwd": cwd,
    })
    assert p.returncode == 0
    out = json.loads(p.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "/beacon-dm-send" in hso["permissionDecisionReason"]


def test_entrypoint_allows_reply(tmp_path):
    cwd = _cwd_with_project(tmp_path, LOCAL)
    p = _run_hook({
        "tool_name": guard.MCP_REPLY_TOOL,
        "tool_input": {"channel": "dm", "recipient_project_id": OTHER,
                       "recipient_session_id": "sv-bob", "in_reply_to": "ev-9"},
        "cwd": cwd,
    })
    assert p.returncode == 0
    assert p.stdout.strip() == ""  # allow → no output


def test_entrypoint_allows_with_token_and_consumes(tmp_path):
    cwd = _cwd_with_project(tmp_path, LOCAL)
    guard.issue_token(recipient_project_id=OTHER, recipient_session_id="sv-bob",
                      beacon_dir=os.path.join(cwd, ".beacon"))
    p = _run_hook({
        "tool_name": guard.MCP_REPLY_TOOL,
        "tool_input": {"channel": "dm", "recipient_project_id": OTHER,
                       "recipient_session_id": "sv-bob"},
        "cwd": cwd,
    })
    assert p.returncode == 0
    assert p.stdout.strip() == ""  # allowed by token
    # Token consumed (single-use).
    assert guard.read_valid_token(os.path.join(cwd, ".beacon")) is None


def test_entrypoint_fail_open_on_garbage(tmp_path):
    p = _run_hook_raw("not json at all")
    assert p.returncode == 0
    assert p.stdout.strip() == ""


def _run_hook_raw(text: str):
    return subprocess.run(
        [sys.executable, _HOOK], input=text,
        capture_output=True, text=True, timeout=20,
    )
