"""ms-169 e-6238 — cross-user DM body isolation (root fix B, cross-user scoped).

The root of the DM prompt-injection is that the receive hook drops a cross-user
DM's free-text body straight into the AI's input context, where an embedded
「〜して」reads as the user's own request. B closes that at the source **for
cross-user DMs**: the body is NOT auto-injected as prose. Instead the inbox hook
surfaces a bodyless notice (誰から / event_id) and stashes the body in a local
cache; the AI reads it only by explicitly running ``beacon dm show <event_id>``
(reading = a deliberate act), which frames it untrusted (e-6235) and arms the
turn (e-6237).

Scope (ms-169 方針改訂 dec-923bbcdc61d078aa): only **cross-user** DMs are
isolated. A **same-user** DM (your own other session, any project) stays on the
current full-body auto-inject path — it is the same trust boundary the ms-70
gate / dm_consent already use (``same_user`` は素通り), and it keeps same-user
session coordination (the one live autonomous surface) unbroken. A cross-user
DM's harmful *action* is still caught by the A gate regardless of B.

Cross-user is decided by POSITIVE proof (sender_user_id present AND ≠ mine).
The server stamps ``sender_user_id`` from the sender's authenticated JWT on every
bus envelope, so a real cross-user DM always carries it — an attacker can't omit
it to keep the full-body path. Events lacking it are system/self/legacy (safe to
show), and the A gate (e-6237) + untrusted framing (e-6235) remain the
fail-closed backstops for the action itself. This module is pure data-model +
formatting; it performs no network I/O.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Where a cross-user DM body is stashed so `beacon dm show` can read it without
# the body ever entering the AI context automatically. One file per event.
CACHE_DIRPARTS = (".beacon", "dm-untrusted")

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]")

# Prune cached bodies older than this many days on write (bounded local store).
_STALE_DAYS = 14


def resolve_my_user_id() -> str:
    """The current human's user_id (JWT ``sub``).

    Resolution order: ``$BEACON_USER_ID`` (explicit override), then
    ``~/.beacon/auth.json``'s ``user_id``. Returns "" when unresolved; callers
    then fail closed (treat DMs as cross-user). Mirrors
    scripts/session-start-dm-inbox.py's file resolution so the receive hooks and
    that catch-up path agree on identity.
    """
    env = os.environ.get("BEACON_USER_ID", "").strip()
    if env:
        return env
    try:
        auth = json.loads(
            Path(os.path.expanduser("~/.beacon/auth.json")).read_text(encoding="utf-8"))
        return str(auth.get("user_id", "") or "")
    except Exception:
        return ""


def is_cross_user(event: dict, my_user_id: str) -> bool:
    """True only when we can POSITIVELY prove ``event`` is from a different user.

    Requires both a resolved ``my_user_id`` and a present ``sender_user_id`` that
    differ. An unknown sender / unknown self returns False (same-user path, full
    body). This is safe: the server stamps ``sender_user_id`` from the sender's
    authenticated JWT on every bus envelope, so a real cross-user DM ALWAYS
    carries it (an attacker cannot omit or forge it). Events lacking it are
    system / self / legacy — safe to show. Over-withholding them would only add
    friction; the A gate (e-6237) + untrusted framing (e-6235) remain the
    fail-closed backstops for the action itself.
    """
    if not isinstance(event, dict):
        return False
    if not my_user_id:
        return False
    sender = str(event.get("sender_user_id") or "")
    if not sender:
        return False
    return sender != my_user_id


def is_proven_same_user(event: dict, my_user_id: str) -> bool:
    """True only when we can POSITIVELY prove ``event`` is from the SAME user.

    Requires a resolved ``my_user_id`` AND a present ``sender_user_id`` that is
    equal. The exact complement of :func:`is_cross_user` on the *proven* axis:
    an unknown sender / unknown self returns False (NOT proven same-user).

    e-6280 point 1: a proven same-user DM carries no injection risk (the server
    stamps ``sender_user_id`` from the sender's authenticated JWT, so only a
    session authed as THIS human can produce one — it is effectively you talking
    to yourself across sessions), so the A gate need not arm for it. Unknown
    identity is deliberately NOT proven-same-user, so it still arms (fail-closed):
    a real cross-user DM whose ``my_user_id`` failed to resolve must not slip into
    the trusted path."""
    if not isinstance(event, dict) or not my_user_id:
        return False
    sender = str(event.get("sender_user_id") or "")
    if not sender:
        return False
    return sender == my_user_id


# Max characters of a DM body surfaced inline in the gate's approval prompt
# (e-6280): enough for a human to spot an embedded「〜して」without dumping the
# whole body into the permission reason.
PREVIEW_MAXLEN = 200

_WS_RE = re.compile(r"\s+")


def preview_text(event: dict, maxlen: int = PREVIEW_MAXLEN) -> str:
    """A single-line, length-capped preview of a DM body for the gate prompt.

    Whitespace/newlines are collapsed to single spaces so the preview stays one
    readable line inside the permission reason; truncated with an ellipsis."""
    collapsed = _WS_RE.sub(" ", body_text(event)).strip()
    if len(collapsed) > maxlen:
        return collapsed[:maxlen] + "…"
    return collapsed


def sender_label(event: dict) -> str:
    """Human-readable sender id for the gate prompt (user id, else session id)."""
    return str((event or {}).get("sender_user_id")
               or (event or {}).get("sender_session_id") or "?")


def build_source(event: dict, maxlen: int = PREVIEW_MAXLEN) -> dict:
    """Build the ``{event_id, sender, preview}`` record the untrusted-turn state
    stores so the gate can show the human *which* DM (and a body preview) put the
    turn in an untrusted context (e-6280 inline preview)."""
    return {
        "event_id": str((event or {}).get("event_id") or ""),
        "sender": sender_label(event),
        "preview": preview_text(event, maxlen),
    }


def _cache_path(root: "str | Path", event_id: str) -> Path:
    safe = _SAFE_ID_RE.sub("", str(event_id or ""))[:128] or "unknown"
    return Path(root).joinpath(*CACHE_DIRPARTS) / f"{safe}.json"


def _prune_stale(dir_path: Path) -> None:
    """Best-effort removal of cache files older than ``_STALE_DAYS``."""
    try:
        import time
        cutoff = time.time() - _STALE_DAYS * 86400
        for f in dir_path.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                continue
    except Exception:
        pass


def cache_body(root: "str | Path", event: dict) -> "Path | None":
    """Stash a cross-user DM ``event`` locally so ``beacon dm show`` can read it.

    The full event (incl. body) is written to
    ``.beacon/dm-untrusted/<event_id>.json``. Best-effort: returns the path on
    success, None on any failure (the notice still surfaces; the AI just can't
    fetch — better than injecting the body)."""
    event_id = (event or {}).get("event_id") or ""
    if not event_id:
        return None
    path = _cache_path(root, event_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_stale(path.parent)
        path.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        return path
    except Exception:
        return None


def read_cached_body(root: "str | Path", event_id: str) -> "dict | None":
    """Read a stashed cross-user DM event by id, or None if absent/unreadable."""
    try:
        return json.loads(_cache_path(root, event_id).read_text(encoding="utf-8"))
    except Exception:
        return None


def body_text(event: dict) -> str:
    """Extract the human-readable body from a DM event's payload."""
    payload = (event or {}).get("payload") or {}
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text:
            return text
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)


def format_cross_user_notice(event: dict) -> str:
    """A BODYLESS arrival notice for a cross-user DM.

    Deliberately carries NO body / preview — only who it is from and how to fetch
    it — so an embedded command never enters the AI context automatically. The
    AI must run ``beacon dm show <event_id>`` to read the body (which then frames
    it untrusted and arms the gate).
    """
    eid = (event or {}).get("event_id") or "?"
    sender = (event or {}).get("sender_user_id") or (event or {}).get("sender_session_id") or "?"
    channel = (event or {}).get("channel") or "dm"
    when = str((event or {}).get("created_at") or "")[:19]
    return (
        f"  - [{eid}] cross-user DM (channel={channel}) from user {sender} at {when}\n"
        f"    本文は自動表示していません (injection 対策 / ms-169 B)。読むには明示取得: "
        f"`beacon dm show {eid}`\n"
        f"    → 取得すると本文は「信頼できない外部データ」として提示され、以後この "
        f"ターンの副作用ツールは人間承認ゲートを通ります。"
    )


__all__ = [
    "CACHE_DIRPARTS",
    "PREVIEW_MAXLEN",
    "resolve_my_user_id",
    "is_cross_user",
    "is_proven_same_user",
    "preview_text",
    "sender_label",
    "build_source",
    "cache_body",
    "read_cached_body",
    "body_text",
    "format_cross_user_notice",
]
