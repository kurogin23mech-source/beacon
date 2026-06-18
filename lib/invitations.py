"""Pure data operations for project member invitations (ms-78 e-1803/e-1804).

Beacon の「招待 URL を発行 → 招かれた人がランディング `/join/<token>` で
受諾 → project member に追加」フローのコアになるデータ操作層。

設計判断 (= ms-78 SPEC pk1CKCuiHVQLODbjaCpi 参照):
- 招待は **project document の `invitations[]` 配列** として持つ (= members[] と同パターン)。
  Firestore subcollection / DynamoDB の新 table を作らずに済むので、両 backend で
  対称実装が `apply_operation` の closure 内で完結する。
- token は server で発行 (= cryptographically random、`secrets.token_urlsafe(32)`)、
  **plaintext は発行レスポンスでのみ返却**、永続化するのは SHA256 hash のみ
  (= DB dump が漏れても token plaintext は復元不可)。
- 有効期限 7 日 default、accept 時に invitations[] から削除 (= 1 回 consume / replay 防止)。
- role は invite 発行時に server が確定し、accept 経路では role を上書きできない
  (= role 詐称防止)。

この module は I/O 一切なし — `apply_operation` の closure 内で呼ぶ。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_INVITE_ROLES = {"viewer", "editor"}
DEFAULT_EXPIRY_DAYS = 7


# ---------------------------------------------------------------------------
# Token primitives
# ---------------------------------------------------------------------------

def generate_token(project_id: str = "") -> str:
    """Generate a fresh URL-safe random invite token (plaintext).

    Format: ``<project_id>.<random>`` so the public landing endpoint can
    parse the project_id from the token without an O(N) project scan.
    project_id is non-secret (it appears in URLs / API paths everywhere),
    and the random suffix provides full unguessability.

    32 bytes of entropy → ~43 chars base64url. Caller stores the SHA256
    hash, NOT this plaintext, and returns the plaintext exactly once
    (in the invite-create response) so the inviter can share it.
    """
    suffix = secrets.token_urlsafe(32)
    if project_id:
        return f"{project_id}.{suffix}"
    return suffix


def parse_token_project_id(token: str) -> str:
    """Extract the project_id prefix from a token, or '' if it has no prefix.

    Used by the public landing endpoints to scope a token lookup to a single
    project document instead of scanning every project."""
    if not token or "." not in token:
        return ""
    pid, _, _ = token.partition(".")
    return pid


def hash_token(token: str) -> str:
    """Return the SHA256 hex hash of `token`. Used as the storage key
    so leaked DB dumps cannot be replayed against `/join/<token>`."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry_iso(days: int = DEFAULT_EXPIRY_DAYS) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _is_expired(expires_at: str) -> bool:
    """Return True iff `expires_at` (ISO8601 UTC) is in the past."""
    try:
        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except Exception:
        # malformed timestamps treated as expired (= fail closed)
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------
#
# Each invitation stored in project["invitations"][i]:
#   {
#     "id": "<random short id, for cancel reference>",
#     "token_hash": "<sha256 of plaintext>",
#     "email": "<invitee email>",
#     "role": "viewer" | "editor",
#     "invited_by": "<user_id of inviter>",
#     "invited_by_email": "<email of inviter, for display>",
#     "created_at": "<iso>",
#     "expires_at": "<iso, +7d default>",
#   }
#
# plaintext token is NEVER stored.


def invitations_list(data: dict) -> list:
    """Return the current invitations list (heals corrupt field).

    Sweeps out expired entries on read so the UI never sees stale rows.
    Sweeping is non-mutating — the caller decides whether to persist.
    """
    inv = data.get("invitations")
    if not isinstance(inv, list):
        return []
    return [i for i in inv if isinstance(i, dict)]


def invitation_create(
    data: dict,
    *,
    email: str,
    role: str,
    invited_by_user_id: str,
    invited_by_email: str = "",
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
    project_id: str = "",
) -> tuple[dict, str]:
    """Add a new invitation. Returns (invitation_dict, plaintext_token).

    The plaintext token is returned ONCE and never stored — caller must
    surface it to the inviter in the API response.

    Raises ValueError on bad inputs / duplicate live invite for the same email.
    """
    if not email or "@" not in email:
        raise ValueError("email is required and must look like an address")
    email = email.strip().lower()
    if role not in VALID_INVITE_ROLES:
        raise ValueError(
            f"Invalid invite role: {role}. Valid: {', '.join(sorted(VALID_INVITE_ROLES))}"
        )
    invitations = data.setdefault("invitations", [])
    if not isinstance(invitations, list):
        invitations = []
        data["invitations"] = invitations

    # Reject duplicate live (= unexpired) invitation for the same email.
    # If the inviter wants to re-send, they cancel the old one first.
    for inv in invitations:
        if (isinstance(inv, dict)
                and inv.get("email") == email
                and not _is_expired(inv.get("expires_at", ""))):
            raise ValueError(
                f"A live invitation for '{email}' already exists. "
                "Cancel it first to issue a new token."
            )

    # Also reject if the email is already a project member or the owner;
    # the caller (= server endpoint) should already check this against
    # the user collection, but we double-guard here so unit tests + future
    # callers cannot accidentally create dead invites.
    members = data.get("members") or []
    if isinstance(members, list):
        for m in members:
            if isinstance(m, dict) and (m.get("email") or "").lower() == email:
                raise ValueError(f"'{email}' is already a project member")

    token = generate_token(project_id)
    invitation = {
        "id": secrets.token_urlsafe(8),
        "token_hash": hash_token(token),
        "email": email,
        "role": role,
        "invited_by": invited_by_user_id,
        "invited_by_email": invited_by_email,
        "created_at": _now_iso(),
        "expires_at": _expiry_iso(expiry_days),
    }
    invitations.append(invitation)
    return invitation, token


def invitation_cancel(data: dict, invitation_id: str) -> dict:
    """Remove an invitation by id. Returns the removed invitation dict.

    Raises ValueError if not found.
    """
    invitations = data.get("invitations") or []
    if not isinstance(invitations, list):
        raise ValueError(f"Invitation not found: {invitation_id}")
    for i, inv in enumerate(invitations):
        if isinstance(inv, dict) and inv.get("id") == invitation_id:
            return invitations.pop(i)
    raise ValueError(f"Invitation not found: {invitation_id}")


def invitation_find_by_token(data: dict, token: str) -> Optional[dict]:
    """Look up an invitation by **plaintext token**. Returns None if not found
    OR expired. Does not mutate. Caller decides whether to consume."""
    th = hash_token(token)
    for inv in invitations_list(data):
        if inv.get("token_hash") == th:
            if _is_expired(inv.get("expires_at", "")):
                return None
            return inv
    return None


def invitation_consume(data: dict, token: str) -> dict:
    """Atomically consume an invitation by plaintext token.

    Removes the invitation row and returns it. Raises ValueError if the
    token does not match a live invitation (= unknown / already consumed / expired).

    The caller is responsible for adding the new member to data["members"]
    using the returned `role` field — this function only handles the invitation
    side, so the closure used by `apply_operation` is a single atomic write.
    """
    th = hash_token(token)
    invitations = data.get("invitations") or []
    if not isinstance(invitations, list):
        raise ValueError("invitation not found")
    for i, inv in enumerate(invitations):
        if not isinstance(inv, dict):
            continue
        if inv.get("token_hash") != th:
            continue
        if _is_expired(inv.get("expires_at", "")):
            # Pop expired hit too so the list stays clean.
            invitations.pop(i)
            raise ValueError("invitation expired")
        return invitations.pop(i)
    raise ValueError("invitation not found")


def invitation_sweep_expired(data: dict) -> int:
    """Drop all expired invitations in-place. Returns count removed."""
    invitations = data.get("invitations") or []
    if not isinstance(invitations, list):
        return 0
    kept = [inv for inv in invitations
            if isinstance(inv, dict) and not _is_expired(inv.get("expires_at", ""))]
    removed = len(invitations) - len(kept)
    data["invitations"] = kept
    return removed


def invitation_public_view(inv: dict, *, include_token_hash: bool = False) -> dict:
    """Return a dict safe to serialize to API clients (no token_hash by default)."""
    out = {
        "id": inv.get("id", ""),
        "email": inv.get("email", ""),
        "role": inv.get("role", ""),
        "invited_by": inv.get("invited_by", ""),
        "invited_by_email": inv.get("invited_by_email", ""),
        "created_at": inv.get("created_at", ""),
        "expires_at": inv.get("expires_at", ""),
    }
    if include_token_hash:
        out["token_hash"] = inv.get("token_hash", "")
    return out
