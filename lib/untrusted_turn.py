"""ms-169 e-6237 — the untrusted-turn state that bridges the receive hook to the
PreToolUse gate.

The A gate (最後の砦) must pause for human approval when the AI is about to call
a side-effect tool *while untrusted content is live in the turn*. But the two
sides run in SEPARATE processes:

  * the receive hook (``beacon-bus-inbox-hook.py`` on UserPromptSubmit) is where
    untrusted DM content enters the AI's context (ms-169 e-6235 framing), and
  * the gate (``beacon-untrusted-tool-gate.py`` on PreToolUse) fires just before
    each tool call.

A per-turn in-memory flag can't cross that gap, so the receive hook persists a
small state file that the gate reads. This module is the ONE place that state's
shape + lifetime live.

Lifetime (documented tradeoff — ms-169 方針1/2):
  * ARM: the receive hook arms the state the moment it injects untrusted content
    (records the triggering event ids + a timestamp), keyed by the harness
    session id so one session's DM never gates another session in the same cwd.
  * FIRE: while armed, the gate routes any side-effect tool call to human
    approval (read-only calls pass). This holds in every session — including an
    ``armed`` autonomous one, which is exactly when no human is watching.
  * DISARM: the receive hook clears the state on the next *human* UserPromptSubmit
    that carries no new untrusted content — i.e. the human has retaken the turn
    and is back in the loop. This keeps the gate from sticking armed forever
    after a single DM, without ever auto-clearing inside the dangerous window
    (DM arrived → AI acts autonomously, no human in between).

Fail-safe on I/O (missing / corrupt state → "not armed"): the gate must never
brick a session because the state file is unreadable. The security property is
carried by the *classification* being fail-closed (unknown tool → side-effect),
not by this file. A follow-up (Codex PreToolUse parity) will consume the same
state once Codex grows a pre-execution hook; today only the Claude gate reads it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:  # work_base gives the canonical ISO timestamp; degrade gracefully.
    import work_base
except Exception:  # pragma: no cover - work_base always present in-repo
    work_base = None  # type: ignore[assignment]

# State file under the project's .beacon dir. A single file holding a dict keyed
# by session so concurrent sessions in one cwd don't cross-contaminate.
STATE_RELPATH = (".beacon", "untrusted-turn.json")

# Entries older than this (hours) are pruned on write, so dead sessions that
# never disarmed don't accumulate forever.
_STALE_HOURS = 24

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _now_iso() -> str:
    if work_base is not None:
        try:
            return work_base.now_iso()
        except Exception:
            pass
    return ""


def session_key_from_hook_input(hook_input: dict) -> str:
    """Resolve the key both hooks agree on: the harness session id.

    Claude Code passes the same ``session_id`` to every hook event in a session
    (UserPromptSubmit and PreToolUse alike), so keying on it makes the receive
    hook's arm and the gate's read line up. Sanitised to a safe token. Returns
    "" when absent — callers treat an empty key as "cannot track" (fail-safe:
    the gate then does not fire, rather than gate everything blindly).
    """
    if not isinstance(hook_input, dict):
        return ""
    raw = hook_input.get("session_id") or ""
    return _SAFE_KEY_RE.sub("", str(raw))[:128]


def _state_path(root: "str | Path") -> Path:
    return Path(root).joinpath(*STATE_RELPATH)


def _read_all(root: "str | Path") -> dict:
    """Read the whole state map. Fail-safe: any error → empty map."""
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _hours_between(a_iso: str, b_iso: str) -> float:
    """Best-effort hours between two ISO strings; 0.0 if unparseable."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
        return abs((b - a).total_seconds()) / 3600.0
    except Exception:
        return 0.0


def _write_all(root: "str | Path", state: dict) -> None:
    """Write the state map, pruning stale entries. Best-effort (never raises)."""
    now = _now_iso()
    if now:
        pruned = {}
        for k, v in state.items():
            armed_at = (v or {}).get("armed_at", "") if isinstance(v, dict) else ""
            if armed_at and _hours_between(armed_at, now) > _STALE_HOURS:
                continue  # drop stale
            pruned[k] = v
        state = pruned
    path = _state_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def arm(root: "str | Path", session_key: str, *, event_ids=None, at: str = "") -> None:
    """Mark ``session_key`` as being in an untrusted turn.

    ``event_ids`` (the untrusted DMs that triggered it) and ``at`` (timestamp,
    default now) are recorded for the gate's human-readable reason + audit. A
    blank ``session_key`` is a no-op (nothing to track)."""
    if not session_key:
        return
    state = _read_all(root)
    prev = state.get(session_key) or {}
    ids = list(event_ids or [])
    state[session_key] = {
        "armed_at": at or _now_iso(),
        "event_ids": ids,
        "count": (prev.get("count", 0) if isinstance(prev, dict) else 0) + 1,
    }
    _write_all(root, state)


def disarm(root: "str | Path", session_key: str) -> None:
    """Clear any armed state for ``session_key`` (human retook the turn)."""
    if not session_key:
        return
    state = _read_all(root)
    if session_key in state:
        del state[session_key]
        _write_all(root, state)


def is_armed(root: "str | Path", session_key: str) -> "dict | None":
    """Return the armed state dict for ``session_key``, or None if not armed.

    Fail-safe: an unreadable / missing state file returns None (not armed) so a
    corrupt file can never brick tool calls."""
    if not session_key:
        return None
    entry = _read_all(root).get(session_key)
    return entry if isinstance(entry, dict) else None


__all__ = [
    "STATE_RELPATH",
    "session_key_from_hook_input",
    "arm",
    "disarm",
    "is_armed",
]
