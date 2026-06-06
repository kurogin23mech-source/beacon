"""Beacon Session Identification (ms-57 / e-1035).

Merged design (Mac + Windows e-1035 part 1):

* Identity resolution priority:
    1. ``CLAUDE_CODE_SESSION_ID`` env var — Claude Code sets this in **hook
       contexts** (post-commit, postcompact, stop, etc) per the official
       docs (https://code.claude.com/docs/en/hooks.md). When present we
       adopt it as-is so beacon's session boundary aligns with Claude
       Code's own. Verified absent from the Bash-tool path on macOS
       2.1.128; verified present from hook subprocess (Windows side).
    2. Existing ``.beacon/session.json`` with fresh ``last_active``
       (default < 60 min) — reuse so sequential CLI calls in one shell
       share an id.
    3. Mint new: ``{agent_slug}-{epoch_ms}-{nonce8}`` so cross-machine
       collisions are statistically impossible.

* ``.beacon/session.json`` is still maintained even when (1) supplies the
  id — runtime state (heartbeat ``last_active``, cloud ``cloud_synced_at``)
  needs a persistent home regardless of where the id came from.

* The ``source`` field on the payload (``claude_code_env`` / ``minted``)
  is forensic-only — never used for control flow. Lets an operator
  inspecting ``.beacon/session.json`` tell which path produced the id.

* Heartbeat / cloud sync details (freshness window, debounce, atomic
  write) are the Mac-side design from earlier slice 1 / slice 2.

side-effect-free at import — all IO is inside ``update_last_active`` /
``get_or_mint_session`` / ``write_session``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import agent as _agent


# ---------------------------------------------------------------------------
# Constants & paths
# ---------------------------------------------------------------------------

# Claude Code sets this env var in hook subprocess environments per the
# official docs. Verified empirically (ms-57 e-1035, Mac + Windows audit):
# present in hook contexts, absent from the Bash-tool subprocess path.
ENV_SESSION_ID = "CLAUDE_CODE_SESSION_ID"

_SESSION_JSON_RELATIVE = Path(".beacon") / "session.json"
_CLOUD_JSON_RELATIVE = Path(".beacon") / "cloud.json"
_CONFIG_JSON_RELATIVE = Path(".beacon") / "config.json"

_DEFAULT_FRESHNESS_SECONDS = 3600
_DEFAULT_CLOUD_DEBOUNCE_SECONDS = 30

_SLUG_NORMALIZE_RE = re.compile(r"-{2,}")

# Cloud-sync-related fields that are excluded from the payload pushed to the
# server (server treats sessions as opaque, but cloud_synced_at is a
# client-only debounce marker).
_LOCAL_ONLY_FIELDS = ("cloud_synced_at",)


def _session_json_path() -> Path:
    """Resolve .beacon/session.json against CWD.

    The bin/beacon wrapper cd's to the project root before dispatch
    (find_beacon_root), so CWD == project root for all sub-commands.
    Mirrors lib/agent._agent_json_path for consistency.
    """
    return Path.cwd() / _SESSION_JSON_RELATIVE


# ---------------------------------------------------------------------------
# Pure helpers (no IO)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC ISO8601 with 'Z' suffix (e.g. '2026-06-05T15:42:18Z')."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _freshness_threshold() -> int:
    raw = os.environ.get("BEACON_SESSION_FRESH_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_FRESHNESS_SECONDS


def _cloud_debounce_seconds() -> int:
    raw = os.environ.get("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_CLOUD_DEBOUNCE_SECONDS


def _slugify(name: str) -> str:
    """Lower-case, alnum + hyphen only; collapse runs of hyphens; strip edges."""
    s = (name or "").lower().strip()
    cleaned = "".join(c if c.isalnum() else "-" for c in s)
    cleaned = _SLUG_NORMALIZE_RE.sub("-", cleaned).strip("-")
    return cleaned or "unknown"


def _detect_harness() -> str:
    """Best-effort label for the calling harness (diagnostic only)."""
    if os.environ.get("CLAUDECODE") == "1":
        version = os.environ.get("AI_AGENT", "").strip()
        return version or "claude-code"
    term = os.environ.get("TERM_PROGRAM", "").strip().lower()
    if term:
        return f"shell-{term}"
    return "shell"


def _mint_session_id(actor: dict) -> str:
    """Pure mint: ``{agent_slug}-{epoch_ms}-{8 hex nonce}``."""
    agent_name = (actor or {}).get("agent") or (actor or {}).get("machine") or "unknown"
    slug = _slugify(agent_name)
    epoch_ms = int(time.time() * 1000)
    nonce = secrets.token_hex(4)
    return f"{slug}-{epoch_ms}-{nonce}"


def _is_cloud_mode() -> bool:
    """Mirror commands._is_cloud_mode without depending on commands.py."""
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    config_path = Path.cwd() / _CONFIG_JSON_RELATIVE
    if not config_path.exists():
        return False
    try:
        with config_path.open("r", encoding="utf-8") as f:
            return json.load(f).get("mode") == "cloud"
    except (json.JSONDecodeError, OSError):
        return False


def _should_cloud_sync(last_sync_iso: str) -> bool:
    """True iff cloud sync is due (never synced, or debounce elapsed)."""
    if not last_sync_iso:
        return True
    try:
        last = datetime.fromisoformat(last_sync_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed >= _cloud_debounce_seconds()


def _is_fresh(last_active_iso: str, now_iso: str, threshold_seconds: int) -> bool:
    """Return True iff ``last_active`` is within ``threshold_seconds`` of ``now``."""
    if not last_active_iso:
        return False
    try:
        last = datetime.fromisoformat(last_active_iso.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = (now - last).total_seconds()
    return 0 <= delta <= threshold_seconds


def _env_session_id() -> str:
    """Return CLAUDE_CODE_SESSION_ID stripped, or '' if absent / blank.

    Centralised so all entry points apply the same trim semantics
    (Windows tests pinned that whitespace-only values fall through to mint).
    """
    raw = os.environ.get(ENV_SESSION_ID, "")
    stripped = raw.strip() if raw else ""
    return stripped


# ---------------------------------------------------------------------------
# Storage IO
# ---------------------------------------------------------------------------

def read_session() -> dict:
    """Read .beacon/session.json or return ``{}`` on absence / parse failure."""
    path = _session_json_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_session(data: dict) -> None:
    """Atomically write ``data`` to .beacon/session.json (tmp + rename)."""
    path = _session_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_mint_session() -> dict:
    """Return the current session payload, minting / adopting as needed.

    Priority (per module docstring):
      1. CLAUDE_CODE_SESSION_ID env var (hook context) — adopt as-is.
      2. Fresh existing .beacon/session.json — reuse.
      3. Mint new.

    Returned dict always contains: session_id, actor, created_at,
    last_active, harness, source. The transient ``minted`` key (True iff
    this call produced a NEW session — including first adoption of an
    env id) is in-memory only and not persisted.
    """
    existing = read_session()
    now = _now_iso()
    actor = _agent.get_actor()

    # 1. Claude Code hook context — env var takes precedence over any
    #    stale marker. If the marker already tracks the same env id, treat
    #    this as a normal reuse (no remint signal).
    env_sid = _env_session_id()
    if env_sid:
        if existing and existing.get("session_id") == env_sid:
            return {**existing, "minted": False}
        payload = {
            "session_id": env_sid,
            "actor": actor,
            "created_at": now,
            "last_active": now,
            "harness": _detect_harness(),
            "source": "claude_code_env",
        }
        write_session(payload)
        return {**payload, "minted": True}

    # 2. Existing fresh marker
    threshold = _freshness_threshold()
    if (
        existing
        and existing.get("session_id")
        and _is_fresh(existing.get("last_active", ""), now, threshold)
    ):
        return {**existing, "minted": False}

    # 3. Mint new
    payload = {
        "session_id": _mint_session_id(actor),
        "actor": actor,
        "created_at": now,
        "last_active": now,
        "harness": _detect_harness(),
        "source": "minted",
    }
    write_session(payload)
    return {**payload, "minted": True}


def _cloud_sync(payload: dict) -> bool:
    """Push the session payload to the cloud registry (best-effort)."""
    try:
        from auth import load_credentials  # noqa: WPS433
        if load_credentials() is None:
            return False
        import commands as _commands  # noqa: WPS433
        client, config = _commands._get_api_client()
        body = {k: v for k, v in payload.items() if k not in _LOCAL_ONLY_FIELDS and v is not None}
        client.upsert_session(config["project_id"], payload["session_id"], body)
        return True
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()
        return False


def update_last_active() -> dict:
    """Heartbeat: get-or-mint, bump ``last_active``, debounced cloud sync.

    Called once per CLI sub-command from the main dispatch. Failures must
    not propagate (the caller wraps in try/except, and _cloud_sync swallows
    its own errors).
    """
    session = get_or_mint_session()
    minted = session.pop("minted", False)

    if not minted:
        session["last_active"] = _now_iso()

    sync_attempted = False
    if _is_cloud_mode() and _should_cloud_sync(session.get("cloud_synced_at", "")):
        sync_attempted = True
        if _cloud_sync(session):
            session["cloud_synced_at"] = _now_iso()

    if not minted or sync_attempted:
        write_session(session)
    return session


def get_session_id() -> str:
    """Convenience: return the current session_id (mints / adopts if needed)."""
    return get_or_mint_session()["session_id"]


__all__ = [
    "ENV_SESSION_ID",
    "get_or_mint_session",
    "get_session_id",
    "read_session",
    "update_last_active",
    "write_session",
]
