"""PreToolUse forcing function for cross-user DM sends (ms-110 / e-3444).

Client-side companion to the server backstop (e-3443). The server is the
authoritative choke point — it rejects a cross-user DM with no
``recipient_confirmed`` claim with a 403. But a raw 403 is a poor moment to
learn "you should have used /beacon-dm-send": the AI has already composed and
fired the primitive. This module lets a **PreToolUse hook** catch the
primitive *before* it runs and loudly redirect the AI to the Skill, so the
accident is stopped a step earlier with a clear message.

What it guards
--------------
Two send primitives an AI might call directly instead of going through
``/beacon-dm-send``:

  * the MCP ``reply`` tool (``mcp__beacon-bus__reply``)
  * ``beacon bus send`` on the shell (Bash tool)

Client-side cross-user proxy
----------------------------
The hook has no cheap way to resolve a recipient session in *another* user's
project to a user_id (that lookup lives server-side). So it uses **cross-
project** as the client-side proxy for "another human": each user's data
lives in their own project, so a DM whose ``recipient_project_id`` differs
from this session's project is the risky surface. This mirrors the existing
qualitative gate's "external" notion (``dm_qualgate.CAT_EXTERNAL``). It is
deliberately best-effort — the server backstop, which resolves real user_ids,
remains the correctness guarantee. The hook only decides *deny vs allow the
attempt*, never *is this truly cross-user* (that is e-3443's job).

Carve-outs (never block these — SPEC §1/§2, and AC "同一 user / Trek /
Operation 経路は hook で誤 block しない"):

  * a reply (``in_reply_to`` present) — recipient derived from the parent
  * a Trek-scoped / operation channel — pre-approved scope
  * a non-dm channel — not a person-directed DM (broadcast / coordination)
  * a same-project recipient — same user's own sessions (best-effort)
  * a send carrying a valid one-time Skill token — the Skill confirmed it

The one-time token
------------------
``/beacon-dm-send`` (e-3445) mints a short-lived single-use token after a
human confirms the recipient, then fires the primitive. The hook reads the
token, and if it matches the send target, allows the send and consumes the
token. This is what lets a Skill-driven cross-user send through while a bare
primitive call is denied.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import time
from datetime import datetime, timezone
from typing import Optional

try:  # pragma: no cover - import shim (see dm_consent for the same pattern)
    import dm_qualgate as _qualgate
except ImportError:  # pragma: no cover
    from lib import dm_qualgate as _qualgate


# Tool names / command markers that count as a bus DM send primitive.
MCP_REPLY_TOOL = "mcp__beacon-bus__reply"
_BUS_SEND_MARKERS = ("beacon bus send", "bus send")

# Decision actions.
ACTION_ALLOW = "allow"
ACTION_DENY = "deny"

# Reasons (stable strings for tests + observability).
REASON_NOT_GUARDED = "not_a_dm_send_primitive"
REASON_REPLY = "reply_derived_recipient"
REASON_TREK_SCOPE = "trek_scoped_channel"
REASON_NON_DM = "non_dm_channel"
REASON_SAME_PROJECT = "same_project_recipient"
REASON_TOKEN_OK = "skill_token_present"
REASON_CROSS_PROJECT_NO_TOKEN = "cross_project_no_confirmation"

TOKEN_TTL_SECONDS = 180

_DENY_MESSAGE = (
    "Blocked: this looks like a cross-project (potentially cross-user) DM sent "
    "by calling the bus primitive directly. Sending to the wrong human is a "
    "trust-breaking accident, so cross-user DMs must go through the "
    "/beacon-dm-send Skill, which confirms the recipient with you and issues "
    "the one-time confirmation the server requires. Re-send via /beacon-dm-send "
    "(it handles new sends and replies). If this is a reply, use the reply flow "
    "with in_reply_to. (The server enforces this too; the primitive would be "
    "rejected with 403.)"
)


# ---------------------------------------------------------------------------
# Parse the tool call into a normalized send target (or None if not guarded)
# ---------------------------------------------------------------------------

def parse_send_target(tool_name: str, tool_input: object) -> Optional[dict]:
    """Return a normalized send target dict, or None if this is not a bus DM
    send primitive the hook should consider.

    Keys: channel, recipient_project_id, recipient_session_id,
    recipient_user_id, in_reply_to. Missing fields are empty strings.
    """
    ti = tool_input if isinstance(tool_input, dict) else {}

    if tool_name == MCP_REPLY_TOOL:
        return {
            "channel": str(ti.get("channel") or "dm"),
            "recipient_project_id": str(ti.get("recipient_project_id") or ""),
            "recipient_session_id": str(ti.get("recipient_session_id") or ""),
            "recipient_user_id": str(ti.get("recipient_user_id") or ""),
            "in_reply_to": str(ti.get("in_reply_to") or ""),
        }

    if tool_name == "Bash":
        command = str(ti.get("command") or "")
        if not any(m in command for m in _BUS_SEND_MARKERS):
            return None
        return _parse_bus_send_command(command)

    return None


def _parse_bus_send_command(command: str) -> Optional[dict]:
    """Extract send target flags from a ``beacon bus send ...`` shell command.

    Best-effort: uses shlex and tolerates ``--flag value`` and ``--flag=value``.
    A command we cannot parse still returns a target (with blanks) so the
    caller applies the safe default rather than silently skipping the guard.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    # Only guard the actual `bus send` subcommand, not e.g. `bus receive`.
    if not _has_bus_send_subcommand(tokens):
        return None

    out = {
        "channel": "", "recipient_project_id": "", "recipient_session_id": "",
        "recipient_user_id": "", "in_reply_to": "",
    }
    flag_map = {
        "--channel": "channel",
        "--to": "recipient_session_id",
        "--to-user": "recipient_user_id",
        "--project": "recipient_project_id",
        "--recipient-project": "recipient_project_id",
        "--in-reply-to": "in_reply_to",
    }
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        key = None
        val = None
        if tok.startswith("--") and "=" in tok:
            flag, _, val = tok.partition("=")
            key = flag_map.get(flag)
        elif tok in flag_map:
            key = flag_map[tok]
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 1
        if key is not None and val is not None:
            out[key] = val
        i += 1
    if not out["channel"]:
        out["channel"] = "dm"
    return out


def _has_bus_send_subcommand(tokens: list[str]) -> bool:
    for i, t in enumerate(tokens):
        if t == "bus" and i + 1 < len(tokens) and tokens[i + 1] == "send":
            return True
    return False


# ---------------------------------------------------------------------------
# The decision (pure)
# ---------------------------------------------------------------------------

def decide(
    target: Optional[dict],
    *,
    local_project_id: str,
    token: Optional[dict] = None,
) -> dict:
    """Return ``{action, reason, consume_token, message}`` for a send attempt.

    ``target`` is ``parse_send_target``'s output (None → not guarded → allow).
    ``local_project_id`` is this session's project id (from .beacon/cloud.json).
    ``token`` is a **already-validated** (fresh, unexpired) one-time token dict
    or None; the caller reads + freshness-checks it (``read_valid_token``).

    ``consume_token`` is True only when a token was the reason the send is
    allowed — the caller then deletes it so it cannot be reused.
    """
    if target is None:
        return _d(ACTION_ALLOW, REASON_NOT_GUARDED)

    channel = str(target.get("channel") or "dm")

    # Carve-out: reply — recipient derived from the parent envelope (§2).
    if target.get("in_reply_to"):
        return _d(ACTION_ALLOW, REASON_REPLY)

    # Carve-out: Trek-scoped / operation channel — pre-approved scope (§1).
    if _qualgate.is_trek_scoped_channel(channel):
        return _d(ACTION_ALLOW, REASON_TREK_SCOPE)

    # Carve-out: non-dm channel — not a person-directed DM.
    if channel != "dm":
        return _d(ACTION_ALLOW, REASON_NON_DM)

    # Carve-out: same-project recipient — the same user's own sessions
    # (best-effort). No recipient project (a --to session in this project) or
    # an explicit recipient project equal to ours is treated as same-project.
    recipient_project = str(target.get("recipient_project_id") or "")
    if not recipient_project or (
        local_project_id and recipient_project == local_project_id
    ):
        return _d(ACTION_ALLOW, REASON_SAME_PROJECT)

    # Cross-project (client-side proxy for cross-user). A valid Skill token is
    # the only pass; consume it so it is single-use.
    if token and _token_matches_target(token, target):
        return _d(ACTION_ALLOW, REASON_TOKEN_OK, consume_token=True)

    return _d(ACTION_DENY, REASON_CROSS_PROJECT_NO_TOKEN, message=_DENY_MESSAGE)


def _token_matches_target(token: dict, target: dict) -> bool:
    """The token must confirm the recipient this send is actually aimed at.

    Match on whichever identifier the token pins (project / session / user).
    A blank on both sides does not count as a match.
    """
    for key in ("recipient_project_id", "recipient_session_id", "recipient_user_id"):
        tv = str(token.get(key) or "")
        sv = str(target.get(key) or "")
        if tv and sv and tv == sv:
            return True
    return False


def _d(action: str, reason: str, *, consume_token: bool = False,
       message: str = "") -> dict:
    return {
        "action": action,
        "reason": reason,
        "consume_token": consume_token,
        "message": message,
    }


# ---------------------------------------------------------------------------
# One-time token file (issued by /beacon-dm-send e-3445, consumed by the hook)
# ---------------------------------------------------------------------------

def _token_path(beacon_dir: Optional[str] = None) -> str:
    base = beacon_dir or os.environ.get("BEACON_DIR", "") or ".beacon"
    return os.path.join(base, "dm_send_consent_token.json")


def mint_token_id() -> str:
    return f"st-{int(time.time())}-{secrets.token_hex(3)}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_token(
    *,
    recipient_project_id: str = "",
    recipient_session_id: str = "",
    recipient_user_id: str = "",
    confirmation_id: str = "",
    beacon_dir: Optional[str] = None,
    issued_at_ts: Optional[int] = None,
) -> dict:
    """Write a one-time consent token (called by /beacon-dm-send, e-3445).

    The token pins the confirmed recipient and an issue timestamp; the hook
    only honours it while fresh (``TOKEN_TTL_SECONDS``) and deletes it on use.
    """
    if not (recipient_project_id or recipient_session_id or recipient_user_id):
        raise ValueError("token must pin at least one recipient identifier")
    ts = int(issued_at_ts if issued_at_ts is not None else time.time())
    token = {
        "token_id": mint_token_id(),
        "confirmation_id": confirmation_id or "",
        "recipient_project_id": recipient_project_id or "",
        "recipient_session_id": recipient_session_id or "",
        "recipient_user_id": recipient_user_id or "",
        "issued_at": _utcnow_iso(),
        "issued_at_ts": ts,
    }
    base = beacon_dir or os.environ.get("BEACON_DIR", "") or ".beacon"
    os.makedirs(base, exist_ok=True)
    path = _token_path(beacon_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(token, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return token


def read_valid_token(
    beacon_dir: Optional[str] = None, *, now_ts: Optional[int] = None,
) -> Optional[dict]:
    """Read the pending token if it exists and is unexpired, else None.

    Does not delete — the caller consumes via ``consume_token`` only when the
    token actually authorised a send (so a mismatched token is not burned).
    """
    path = _token_path(beacon_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(token, dict):
        return None
    issued = token.get("issued_at_ts")
    if not isinstance(issued, (int, float)):
        return None
    now = int(now_ts if now_ts is not None else time.time())
    if now - int(issued) > TOKEN_TTL_SECONDS:
        return None
    return token


def consume_token(beacon_dir: Optional[str] = None) -> bool:
    """Delete the pending token (single-use). Returns True if one was removed."""
    path = _token_path(beacon_dir)
    if os.path.exists(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return False


__all__ = [
    "MCP_REPLY_TOOL",
    "ACTION_ALLOW",
    "ACTION_DENY",
    "REASON_NOT_GUARDED",
    "REASON_REPLY",
    "REASON_TREK_SCOPE",
    "REASON_NON_DM",
    "REASON_SAME_PROJECT",
    "REASON_TOKEN_OK",
    "REASON_CROSS_PROJECT_NO_TOKEN",
    "TOKEN_TTL_SECONDS",
    "parse_send_target",
    "decide",
    "issue_token",
    "read_valid_token",
    "consume_token",
    "mint_token_id",
]
