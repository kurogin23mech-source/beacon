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
    """DEPRECATED (ms-54 e-1319): CLI-side heartbeat is no longer the truth source.

    Post Option C (PR #111 / commit 78048b6) the bridge's poll loop owns both
    mint+heartbeat (``last_active``) and the receive-capability signal
    (``last_poll_at``). A duplicate write from the CLI created ambiguity:
    a session could heartbeat ``last_active`` (proves Python ran) while
    the bridge poll loop was dead, leaving the directory advertising a
    receiver that could not actually receive.

    Now a thin shim around :func:`get_or_mint_session`: it still materialises
    ``.beacon/session.json`` on first call so the bridge has an id to read,
    but does NOT bump ``last_active`` or push to cloud sessions/. Use
    :func:`get_session_id` (pure getter) for new code.

    Kept callable so any external script that still imports the symbol does
    not break — the side-effect removal is the structural fix.
    """
    import warnings
    warnings.warn(
        "session.update_last_active is deprecated post Option C (ms-54 e-1319);"
        " the bridge poll loop is the truth source for heartbeat. Use"
        " session.get_session_id() for a pure read.",
        DeprecationWarning,
        stacklevel=2,
    )
    session = get_or_mint_session()
    session.pop("minted", None)
    return session


def get_session_id() -> str:
    """Pure getter: return the current session_id, minting once if needed.

    Post Option C (ms-54 e-1319) this is the *only* CLI path for resolving
    the session_id. No ``last_active`` bump, no cloud sync — the bridge owns
    both signals via its poll loop (PR #111 / commit 78048b6).

    First-call mint still writes ``.beacon/session.json`` because the bridge
    reads that file at startup; subsequent calls are pure reads.
    """
    return get_or_mint_session()["session_id"]


# ms-54 / e-1331 quick fix: when a bus.mjs bridge is running in this cwd, it
# writes its session_id to .beacon/bridge.json. The CLI uses that as the
# *authoritative* session_id for any "what is the active session" question
# (focus / attention / future intent stamping) so a CLI call from the user's
# terminal doesn't write to a different session_id than the bridge is using
# for DM delivery.
#
# Without this, get_or_mint_session() in a terminal that has
# CLAUDE_CODE_SESSION_ID set would adopt the env value, overwriting whatever
# the bridge wrote — and the bridge has no way to see CLAUDE_CODE_SESSION_ID
# (Claude Code does not forward it to MCP subprocesses, verified 2026-06-07).

_BRIDGE_CLAIM_RELATIVE = Path(".beacon") / "bridge.json"


def _bridge_claim_path() -> Path:
    """Resolve .beacon/bridge.json against CWD (same convention as session.json)."""
    return Path.cwd() / _BRIDGE_CLAIM_RELATIVE


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live process on this host.

    Uses ``os.kill(pid, 0)``: ``ProcessLookupError`` means dead,
    ``PermissionError`` (the signalling process can't reach the target)
    counts as alive — the bridge may belong to another user but still
    legitimately own the claim. Anything else is degraded to "unknown,
    treat as alive" so the CLI doesn't silently fall through on
    transient OS quirks.
    """
    import os
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def read_bridge_session() -> dict:
    """Return the active bus.mjs bridge's claim, or {} if absent/stale.

    A claim is considered stale (and ignored) when its recorded ``pid`` is
    no longer alive on this host. The bridge clears the claim file on a
    graceful shutdown; crashes / SIGKILL leave a stale claim that this
    pid-liveness check then drops.

    Shape: ``{session_id, pid, cwd, started_at}``.
    """
    import json
    path = _bridge_claim_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    pid = data.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return {}
    return data


def resolve_active_session_id() -> str:
    """Return the session_id the CLI should treat as the current session.

    Resolution order (e-1331 quick fix):
      1. Bridge claim (.beacon/bridge.json) — authoritative when a bus.mjs
         is actively running in this cwd. Skips the env-vs-marker tussle
         because the bridge wrote this file with its real session_id.
      2. Fall through to :func:`get_session_id` which applies the existing
         env / mint / session.json precedence. Used when no bridge is
         running.
    """
    claim = read_bridge_session()
    if claim.get("session_id"):
        return claim["session_id"]
    return get_session_id()


__all__ = [
    "ENV_SESSION_ID",
    "get_or_mint_session",
    "get_session_id",
    "read_bridge_session",
    "read_session",
    "resolve_active_session_id",
    "update_last_active",
    "write_session",
]
