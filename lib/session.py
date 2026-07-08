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
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
# _CONFIG_JSON_RELATIVE removed in e-1861 (ms-61): config.json `mode` field
# was the silent-drift attack surface — cloud.json existence is now sole truth.
_BRIDGES_DIR_RELATIVE = Path(".beacon") / "bridges"

ENV_FORCE_MINT = "BEACON_FORCE_MINT"

_DEFAULT_FRESHNESS_SECONDS = 3600
_DEFAULT_CLOUD_DEBOUNCE_SECONDS = 30
# ms-98 / e-2769: TTL for the cloud-first session cache. When
# BEACON_USE_CLOUD_FIRST_SESSION=1, every CLI invocation used to call
# POST /api/me/heartbeat. This cache lets us reuse the previous
# server-minted sid whenever the identity tuple is stable and the last
# heartbeat is within this window, cutting the 2026-07-02 429-storm's
# largest amplifier at the source. Override via
# BEACON_SESSION_CLOUD_MINT_TTL_SECONDS ; set to 0 to disable the cache.
_DEFAULT_CLOUD_MINT_TTL_SECONDS = 300

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


def _cloud_mint_cache_ttl_seconds() -> int:
    """TTL for the cloud-first session cache (ms-98 / e-2769).

    Returns 0 when the cache is disabled — either the operator opted out
    via env var or a negative value slipped through the parse. Callers
    treat 0 as "no cache, always heartbeat".
    """
    raw = os.environ.get("BEACON_SESSION_CLOUD_MINT_TTL_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_CLOUD_MINT_TTL_SECONDS


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
    """Mirror commands._is_cloud_mode without depending on commands.py.

    e-1861 (ms-61): cloud.json existence is the single source of truth.
    The legacy ``config.json["mode"] == "cloud"`` dual-source check was
    removed to close the silent-drift window where a sub-agent rewriting
    ``config.json`` to ``{"mode": "local"}`` could silently flip the CLI
    off cloud and produce apparent user data loss.
    """
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    return (Path.cwd() / _CLOUD_JSON_RELATIVE).exists()


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

ENV_USE_CLOUD_FIRST = "BEACON_USE_CLOUD_FIRST_SESSION"
ENV_PARENT_PID = "BEACON_PARENT_PID"

# ms-95 / e-2870 — Guard against api_client → session recursion during
# cloud-first mint. ``get_or_mint_session_via_server`` calls
# ``client.me_heartbeat``, which routes through ``api_client._request``,
# which (since ms-97 / e-2694) stamps an ``X-Beacon-Session`` header by
# calling ``resolve_active_session_id``. Without this guard the resolver
# would call back into ``get_or_mint_session`` and start a new mint,
# looping until the 60s wall-clock watchdog kills the CLI.
#
# ContextVar (not a plain module-level bool) so async / threaded callers
# don't share the flag across contexts. Default False = normal resolver
# behaviour; set True only for the duration of the server mint call.
_in_cloud_first_mint: ContextVar[bool] = ContextVar(
    "_in_cloud_first_mint", default=False,
)

# ~/.beacon/machine.json — per-user cache of the server-issued machine_id
# (ms-62 / e-1510). Lives outside the project cwd because machine identity
# is the same across every Beacon project on this device.
_MACHINE_JSON_REL = ".beacon/machine.json"


def _machine_json_path() -> Path:
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    return Path(home) / _MACHINE_JSON_REL


def _read_machine_cache() -> dict:
    p = _machine_json_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_machine_cache(machine_id: str, fingerprint: str) -> None:
    p = _machine_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "machine_id": machine_id,
                "fingerprint": fingerprint,
                "cached_at": _now_iso(),
            },
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        f.write("\n")
    os.replace(tmp, p)


def _resolve_parent_pid() -> int:
    """Return the terminal pid that anchors this session (ms-62 / e-1510).

    Preferred source: ``BEACON_PARENT_PID`` env set by ``bin/bclaude`` (=
    the wrapper that started Claude Code). Mac and Windows bootstrap
    paths both export it so every subprocess inherits the same value.

    Fallback: ``os.getppid()`` (= immediate parent). Less stable for
    server-side tuple lookup but at least non-zero.
    """
    raw = os.environ.get(ENV_PARENT_PID, "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return os.getppid()


def _cloud_mint_cache_hit(
    project_id: str, machine_id: str, parent_pid: int,
) -> Optional[dict]:
    """Return a cached session payload iff every gate passes.

    ms-98 / e-2769: called from ``get_or_mint_session_via_server`` before
    the heartbeat API call. Gates in order:

      1. TTL > 0 (cache not disabled by operator).
      2. ``.beacon/session.json`` exists and is a valid dict with a
         ``session_id``.
      3. Cached identity tuple matches the current call — same
         ``machine_id`` and same ``parent_pid``. Any drift means the
         server would mint a new sid, so the cache must not short-
         circuit that.
      4. Cached ``last_active`` is within the TTL window.
      5. Cached ``source`` proves the sid came from an earlier server
         mint (``server_minted``). We refuse to return a locally-minted
         sid via the cloud path — that would confuse the server on the
         next heartbeat because the tuple lookup would find no matching
         document.

    Returns ``None`` when any gate fails; the caller falls through to
    the network heartbeat and updates the cache with the fresh payload.
    """
    ttl = _cloud_mint_cache_ttl_seconds()
    if ttl <= 0:
        return None
    existing = read_session()
    if not existing:
        return None
    sid = (existing.get("session_id") or "").strip()
    if not sid:
        return None
    if (existing.get("machine_id") or "") != machine_id:
        return None
    if int(existing.get("parent_pid") or 0) != parent_pid:
        return None
    if existing.get("source") != "server_minted":
        return None
    now = _now_iso()
    if not _is_fresh(existing.get("last_active", ""), now, ttl):
        return None
    # Cache hit — refresh last_active in-memory so downstream readers see
    # a current timestamp, but don't rewrite the file (that would defeat
    # the "the on-disk stamp survives across CLI calls" contract that
    # gates freshness). ``minted=False`` mirrors the server contract for
    # "same tuple → same sid, no new record".
    return {**existing, "last_active": now, "minted": False}


def get_or_mint_session_via_server() -> dict:
    """Server-first session mint (ms-62 / e-1510).

    Flow:
      1. Read project_id from ``.beacon/cloud.json``.
      2. Resolve machine_id from ``~/.beacon/machine.json`` cache; on a
         miss, call POST /api/me/machine to mint one + write the cache.
      3. **Cache short-circuit (ms-98 / e-2769)** — if
         ``.beacon/session.json`` holds a server-minted sid for the
         current identity tuple and its ``last_active`` is within the
         cache TTL (default 300 s, tunable via
         ``BEACON_SESSION_CLOUD_MINT_TTL_SECONDS``, ``0`` to disable),
         return the cached payload without touching the network. This is
         the primary throttle for the ``/api/me/heartbeat`` hot path —
         before the cache, every ``beacon <cmd>`` under
         ``BEACON_USE_CLOUD_FIRST_SESSION=1`` triggered one heartbeat.
      4. Call POST /api/me/heartbeat with the identity tuple
         (project_id, machine_id, parent_pid). Server returns the sid
         (= same tuple → same sid for continuity).
      5. Materialise .beacon/session.json with the returned sid so the
         legacy single-claim reader and other non-server-aware paths see
         the same id.

    Raises on any failure (auth missing, server 404 on old build,
    network error, malformed response). The caller in
    get_or_mint_session() catches and falls back to the legacy mint
    chain so missing server support never breaks the CLI.

    ms-95 / e-2870 — sets ``_in_cloud_first_mint=True`` for the duration
    of this call so ``resolve_active_session_id`` (called reentrantly via
    ``api_client._request`` while stamping ``X-Beacon-Session``) returns
    empty instead of triggering another mint. The token is reset in a
    ``finally`` so exceptions leaving this frame still clear the guard.
    """
    _mint_guard_token = _in_cloud_first_mint.set(True)
    try:
        return _get_or_mint_session_via_server_impl()
    finally:
        _in_cloud_first_mint.reset(_mint_guard_token)


def _get_or_mint_session_via_server_impl() -> dict:
    """Body of ``get_or_mint_session_via_server`` (ms-95 / e-2870 split).

    Extracted so the public entry point can wrap the whole call with the
    ``_in_cloud_first_mint`` guard without indenting the entire body by
    another level. Identical semantics to the pre-e-2870 body.
    """
    cloud_path = Path.cwd() / _CLOUD_JSON_RELATIVE
    if not cloud_path.exists():
        raise RuntimeError("cloud.json not found (local-mode project?)")
    with cloud_path.open("r", encoding="utf-8") as f:
        cloud_cfg = json.load(f)
    project_id = (cloud_cfg.get("project_id") or "").strip()
    if not project_id:
        raise RuntimeError("project_id missing from cloud.json")

    actor = _agent.get_actor()
    fingerprint = actor.get("machine") or actor.get("agent") or "unknown"

    # Cache short-circuit needs the identity tuple, so resolve machine_id
    # and parent_pid first. If both are cached (typical steady state), no
    # API call is made at all. If machine_id needs minting, we take one
    # network hop for that but still consult the sid cache afterwards.
    cached_machine = _read_machine_cache()
    machine_id = (cached_machine.get("machine_id") or "").strip()
    parent_pid = _resolve_parent_pid()
    if parent_pid <= 0:
        raise RuntimeError("could not resolve parent_pid")

    if machine_id and cached_machine.get("fingerprint") == fingerprint:
        cached_sid = _cloud_mint_cache_hit(project_id, machine_id, parent_pid)
        if cached_sid is not None:
            return cached_sid

    # Cache miss (or machine_id needed minting) — go to the network.
    # Lazy import: commands._get_api_client requires auth + cloud config.
    import commands as _commands  # noqa: WPS433
    client, _config = _commands._get_api_client()

    if not machine_id or cached_machine.get("fingerprint") != fingerprint:
        resp = client.me_upsert_machine(
            fingerprint,
            hostname=actor.get("machine") or "",
            agent=actor.get("agent") or "",
        )
        machine_id = (resp.get("machine_id") or "").strip()
        if not machine_id:
            raise RuntimeError("server returned no machine_id")
        _write_machine_cache(machine_id, fingerprint)
        # machine_id just changed — retry the sid cache with the new value.
        cached_sid = _cloud_mint_cache_hit(project_id, machine_id, parent_pid)
        if cached_sid is not None:
            return cached_sid

    heartbeat = client.me_heartbeat(
        project_id, machine_id, parent_pid,
        cwd=str(Path.cwd()),
        agent={"kind": "claude-code", "machine_id": machine_id},
    )
    sid = (heartbeat.get("session_id") or "").strip()
    if not sid:
        raise RuntimeError("server returned no session_id")

    now = _now_iso()
    payload = {
        "session_id": sid,
        "actor": actor,
        "created_at": heartbeat.get("created_at") or now,
        "last_active": now,
        "harness": _detect_harness(),
        "source": "server_minted",
        "machine_id": machine_id,
        "parent_pid": parent_pid,
    }
    write_session(payload)
    return {**payload, "minted": bool(heartbeat.get("minted"))}


def get_or_mint_session() -> dict:
    """Return the current session payload, minting / adopting as needed.

    Priority (ms-62 e-1510 cloud-first added on top of e-1460 pid-tree
    resolution and e-1035 base):
      -1. BEACON_USE_CLOUD_FIRST_SESSION=1 env — opt in to the
          server-side identity tuple flow (ms-62). On success, the
          server-returned sid is the truth; on any failure we silently
          fall through to the legacy chain so a missing endpoint never
          breaks the CLI. Default OFF during the v0.32.x compat window
          (see ms-62 task e-1513 for the migration plan).
      0. BEACON_FORCE_MINT=1 env — bus.mjs cold-start sets this when it
         detects another alive bridge in this cwd (= a 2nd+ bclaude is
         starting). Skips reuse entirely and mints fresh.
      1. CLAUDE_CODE_SESSION_ID env var (hook context) — adopt as-is.
      2. Pid-tree match against .beacon/bridges/<sid>.json — when the
         calling process is a descendant of a known bclaude that owns
         an alive bridge, reuse that bridge's session_id. This is what
         keeps sequential CLI calls inside a 2nd bclaude pointing at
         the correct session_id even though session.json was
         overwritten by the 1st bclaude's bridge.
      3. Fresh existing .beacon/session.json — reuse (legacy single-
         bclaude path).
      4. Mint new.

    Returned dict always contains: session_id, actor, created_at,
    last_active, harness, source. The transient ``minted`` key (True iff
    this call produced a NEW session — including first adoption of an
    env id) is in-memory only and not persisted.
    """
    existing = read_session()
    now = _now_iso()
    actor = _agent.get_actor()

    # -1. Cloud-first (ms-62 / e-1510). Opt-in until v0.33.0 hard cut.
    if os.environ.get(ENV_USE_CLOUD_FIRST) == "1":
        try:
            return get_or_mint_session_via_server()
        except BaseException:
            # Any failure silently falls through to the legacy chain so a
            # missing endpoint / auth glitch / network failure does not
            # break the CLI. BEACON_DEBUG=1 surfaces the trace.
            if os.environ.get("BEACON_DEBUG") == "1":
                import traceback as _tb
                _tb.print_exc()

    # 0. Force-mint (e-1460): bus.mjs cold-start passes this when it
    #    detected another alive bridge in this cwd. Skips all reuse paths.
    if os.environ.get(ENV_FORCE_MINT) == "1":
        return mint_fresh_session()

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

    # 2. Pid-tree match against bridges/<sid>.json (e-1460). When multiple
    #    bclaude run in the same cwd, session.json holds whichever sid was
    #    written last; the only reliable per-bclaude signal is the bridge
    #    claim that records parent_pid (= the bclaude that spawned it).
    claim = find_my_bridge_claim()
    if claim.get("session_id"):
        return {
            "session_id": claim["session_id"],
            "actor": actor,
            "created_at": claim.get("started_at", now),
            "last_active": now,
            "harness": _detect_harness(),
            "source": "bridges_pid_tree",
            "minted": False,
        }

    # 3. Existing fresh marker
    threshold = _freshness_threshold()
    if (
        existing
        and existing.get("session_id")
        and _is_fresh(existing.get("last_active", ""), now, threshold)
    ):
        return {**existing, "minted": False}

    # 4. Mint new
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


def mint_fresh_session() -> dict:
    """Mint a brand-new session id, skipping all reuse paths (e-1460).

    Used by bus.mjs cold-start (via BEACON_FORCE_MINT=1) when it detects
    another alive bridge in this cwd, meaning a 2nd+ bclaude is starting
    and must NOT inherit the 1st bclaude's session_id from session.json.

    Writes session.json so the legacy single-claim fallback in
    ``read_bridge_session`` and any non-pid-tree-aware reader sees a
    consistent file. The pid-tree-aware reader in
    ``find_my_bridge_claim`` will still resolve the *correct* per-bclaude
    sid via .beacon/bridges/<sid>.json regardless of which write won the
    session.json race.

    DEPRECATED (ms-62 / e-1511): the client-side mint chain is being
    replaced by the server-side identity tuple lookup
    (``get_or_mint_session_via_server``). Scheduled for removal at the
    v0.33.0 hard cut per ms-62 task e-1513 (migration plan doc_id
    ``2zQ2J3SUsuVqOLoZVSvA``). In v0.32.x this function is still
    callable; from v0.33.0 it disappears and the server-side path is
    the only mint route.
    """
    if os.environ.get("BEACON_WARN_LEGACY_MINT") == "1":
        import warnings
        warnings.warn(
            "session.mint_fresh_session is scheduled for removal at v0.33.0"
            " hard cut (ms-62 e-1511). Enable BEACON_USE_CLOUD_FIRST_SESSION=1"
            " to migrate to the server-side identity tuple path.",
            DeprecationWarning,
            stacklevel=2,
        )
    now = _now_iso()
    actor = _agent.get_actor()
    payload = {
        "session_id": _mint_session_id(actor),
        "actor": actor,
        "created_at": now,
        "last_active": now,
        "harness": _detect_harness(),
        "source": "minted_fresh",
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


def _bridges_dir() -> Path:
    """Resolve .beacon/bridges/ — per-sid bridge claim directory (e-1460)."""
    return Path.cwd() / _BRIDGES_DIR_RELATIVE


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


def _get_ancestor_pids(start_pid: int | None = None, max_depth: int = 20) -> set:
    """Walk up the parent process chain, return the set of all ancestor pids.

    Includes ``start_pid`` itself so callers can do a single ``in`` check
    against bridges' recorded ``parent_pid``. Uses ``ps -o ppid=`` on
    macOS/Linux. Best-effort: stops at pid 1 / pid 0 / lookup failure.

    e-1460: this is the load-bearing primitive for "which bclaude do I
    belong to?". A Bash-tool CLI subprocess inherits the chain
    `bash → claude (bclaude) → terminal shell → ...`; bus.mjs is a
    direct child of bclaude. Either way, the bclaude pid is among the
    ancestors, so matching against ``bridges/<sid>.json.parent_pid``
    identifies the owning session uniquely.
    """
    import subprocess
    if start_pid is None:
        start_pid = os.getpid()
    pids = {start_pid}
    pid = start_pid
    for _ in range(max_depth):
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            )
            ppid_str = result.stdout.strip()
            ppid = int(ppid_str) if ppid_str else 0
        except (subprocess.SubprocessError, ValueError, OSError):
            break
        if ppid <= 1:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def _read_claims_from_dir(bridges_dir: Path) -> list:
    """Read all ``bridges/<sid>.json`` claim dicts from one directory.

    Best-effort: missing dir / unreadable files yield an empty list rather
    than raising, so callers can point this at any (possibly absent) worktree.
    """
    claims = []
    if bridges_dir.exists() and bridges_dir.is_dir():
        for f in sorted(bridges_dir.glob("*.json")):
            try:
                with f.open("r", encoding="utf-8") as fp:
                    data = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("session_id"):
                claims.append(data)
    return claims


def _in_linked_worktree() -> bool:
    """True when the current cwd is a *linked* git worktree.

    A linked worktree's ``.git`` is a FILE (a ``gitdir:`` pointer), whereas the
    main worktree's ``.git`` is a directory. Gating the cross-worktree bridge
    scan on this keeps the ``git worktree list`` subprocess off the hot path for
    bridge-less sessions in a normal repo / non-git dir — it only runs in the
    exact scenario the receive-orphan fix targets (``cd`` into a worktree).
    """
    try:
        return (Path.cwd() / ".git").is_file()
    except OSError:
        return False


def _sibling_worktree_bridges_dirs() -> list:
    """Return ``.beacon/bridges/`` dirs for every OTHER git worktree of this repo.

    The receive-orphan fix (ms-93): a bclaude launched in worktree A writes its
    bridge claim to ``A/.beacon/bridges/``. If the CLI later runs in a different
    worktree B (hand ``cd`` or ``beacon milestone start``), ``B/.beacon/bridges/``
    is empty, so the pid-tree resolver misses A's claim and the session silently
    falls through to B's own sid — orphaning the receive bridge (sends keep
    working, incoming DMs never live-wake). Enumerating sibling worktrees lets
    the pid-tree match find the running bridge regardless of which worktree the
    CLI sits in.

    Best-effort: returns ``[]`` outside a git repo, on any git failure, or when
    ``git worktree list`` is unavailable. Excludes the current cwd (already
    covered by the fast path).
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    cwd = str(Path.cwd())
    dirs = []
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            wt = line[len("worktree "):].strip()
            if wt and wt != cwd:
                dirs.append(Path(wt) / _BRIDGES_DIR_RELATIVE)
    return dirs


def _caller_agent_kind() -> str:
    """Best-effort structural agent kind of the CURRENT process (e-3091).

    Returns ``"codex"`` / ``"claude-code"`` / ``""`` (unknown). Used solely to
    make :func:`find_my_bridge_claim` agent_kind-aware so a Claude Code CLI
    call never adopts a co-located Codex daemon's bridge claim (and vice
    versa) when both share an ancestor pid in the same cwd.

    Signals, in order:
      1. ``BEACON_BUS_SENDER`` starting with ``"codex-"`` — the Codex
         receive-loop daemon exports this sid for every child CLI call
         (``scripts/codex-receive-loop.py``), so it is the strongest Codex
         marker available in a plain subprocess env.
      2. ``CLAUDECODE=1`` — Claude Code sets this in every Bash-tool / bridge
         subprocess (the same signal :func:`_detect_harness` keys off).

    Returns ``""`` when neither fires; the caller then falls back to
    pid-tree-only adoption (= pre-e-3091 behaviour) so a truly-unknown caller
    is never blocked from its own bridge.
    """
    sender = (os.environ.get("BEACON_BUS_SENDER") or "").strip()
    if sender.startswith("codex-"):
        return "codex"
    if os.environ.get("CLAUDECODE") == "1":
        return "claude-code"
    return ""


def _claim_kind_matches_caller(claim_kind: str, caller_kind: str) -> bool:
    """Agent-kind adoption gate for one bridge claim (e-3091).

    Rules:
      * Claim WITHOUT ``agent_kind`` (legacy / pre-e-3091 → ``""`` here):
        adoptable by ANY caller — pid-tree alone decides. Keeps existing
        single-bridge cwds working through the migration window. (AC #4)
      * Claim WITH ``agent_kind``: adoptable only by a caller of the SAME
        kind. A Claude Code CLI never adopts a codex bridge and vice versa.
        (AC #2)
      * ``caller_kind`` unknown (``""``): treated as "matches anything" so a
        caller we cannot classify is never blocked from its own bridge
        (degrades to pid-tree-only, the pre-e-3091 behaviour).
    """
    if not claim_kind:
        return True
    if not caller_kind:
        return True
    return claim_kind == caller_kind


def _match_claim_by_pidtree(claims: list, ancestors: set, caller_kind: str = "") -> dict:
    """Return the first alive claim whose ``parent_pid`` is in ``ancestors``.

    A claim is only adopted when its bridge process (``pid``) is still alive —
    a dead bus.mjs cannot own the current session. Legacy claims without
    ``parent_pid`` are skipped (they cannot be attributed to a specific bclaude).

    e-3091: ``caller_kind`` gates cross-kind adoption. A claim that records an
    ``agent_kind`` (bus.mjs stamps ``"claude-code"``) is only adopted by a
    caller of the same kind, so a Claude Code session and a co-located Codex
    daemon sharing an ancestor pid in one cwd never silently swap sids.
    Claims without ``agent_kind`` stay adoptable by anyone (legacy fallback).
    """
    for claim in claims:
        pid = claim.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            continue
        parent_pid = claim.get("parent_pid")
        if not (isinstance(parent_pid, int) and parent_pid in ancestors):
            continue
        claim_kind = str(claim.get("agent_kind") or "").strip()
        if not _claim_kind_matches_caller(claim_kind, caller_kind):
            continue
        return claim
    return {}


def read_all_bridge_claims() -> list:
    """Return all bridge claims from .beacon/bridges/*.json + legacy fallback.

    Each claim dict carries at least ``session_id`` and ``pid`` (= the
    bus.mjs process pid); newer per-sid claims (e-1460) additionally
    carry ``parent_pid`` (= the bclaude that spawned this bridge), which
    is what makes pid-tree resolution work.

    Liveness is NOT filtered here — callers decide whether stale claims
    should be ignored. This keeps the function usable for diagnostics
    (e.g. ``beacon doctor``) that want to see dead claims too.
    """
    claims = _read_claims_from_dir(_bridges_dir())
    # Legacy fallback: single .beacon/bridge.json (pre-e-1460 schema, no
    # parent_pid). Include it only if no per-sid claim with the same sid
    # exists, so a half-migrated cwd doesn't double-count.
    legacy = _bridge_claim_path()
    if legacy.exists():
        try:
            with legacy.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("session_id"):
            existing_sids = {c.get("session_id") for c in claims}
            if data["session_id"] not in existing_sids:
                claims.append(data)
    return claims


def find_my_bridge_claim() -> dict:
    """Return the alive bridge claim whose parent_pid matches my ancestor chain.

    Returns ``{}`` when no per-sid claim with a recorded ``parent_pid``
    matches any of my ancestor pids (or when no bridges are alive at all).
    Legacy claims without ``parent_pid`` are skipped — they cannot
    reliably be attributed to a specific bclaude.

    e-1460: this is the bclaude-keyed session resolver. Multiple bclaude
    sharing a cwd each have their own ``bridges/<sid>.json`` written by
    their respective bus.mjs at cold-start; pid-tree match picks the
    right one for the calling CLI.

    e-3091 (short-term mitigation): the pid-tree match is now agent_kind-
    aware. Before this fix a Claude Code CLI call and a co-located Codex
    receive-loop daemon sharing an ancestor pid in the same cwd could adopt
    each other's bridge claim (the claim files carried no ``agent_kind``),
    making ``agent.kind`` on the shared sid non-deterministic (last-writer-
    wins) and breaking ``resolve_stable_identity(agent_kind="codex")``. Now a
    claim that records ``agent_kind`` is only adopted by a caller of the same
    kind (see :func:`_caller_agent_kind` / :func:`_claim_kind_matches_caller`).
    This is a band-aid on top of the already-planned e-1511 removal below —
    the real cure is the server-side identity tuple lookup, which is inherently
    agent_kind-keyed; keep this only until that supersedes the pid-tree path.

    DEPRECATED (ms-62 / e-1511): the bridges/<sid>.json + pid-tree
    resolver is being replaced by the server-side identity tuple lookup
    (``(project_id, machine_id, parent_pid)`` → ``sid``). Scheduled for
    removal at the v0.33.0 hard cut per ms-62 task e-1513 (migration plan
    doc_id ``2zQ2J3SUsuVqOLoZVSvA``).
    """
    if os.environ.get("BEACON_WARN_LEGACY_MINT") == "1":
        import warnings
        warnings.warn(
            "session.find_my_bridge_claim (pid-tree resolver) is scheduled for"
            " removal at v0.33.0 hard cut (ms-62 e-1511). The server-side"
            " identity tuple lookup supersedes it.",
            DeprecationWarning,
            stacklevel=2,
        )
    my_ancestors = _get_ancestor_pids()
    caller_kind = _caller_agent_kind()
    # Fast path: claims in the current cwd's .beacon/bridges/ (+ legacy). This
    # is the common case and pays no extra process cost.
    match = _match_claim_by_pidtree(
        read_all_bridge_claims(), my_ancestors, caller_kind)
    if match:
        return match
    # Fallback (ms-93 receive-orphan fix): the CLI is in a git worktree that
    # has no local bridge claim, but my bclaude's bridge is alive in a sibling
    # worktree. Search those by the same pid-tree key so the running bridge
    # stays authoritative across a `cd` / `milestone start` into a worktree —
    # otherwise the session silently orphans its receive path (send-only).
    # Gated to linked worktrees so bridge-less sessions in a normal repo never
    # pay the `git worktree list` subprocess.
    if _in_linked_worktree():
        for bdir in _sibling_worktree_bridges_dirs():
            match = _match_claim_by_pidtree(
                _read_claims_from_dir(bdir), my_ancestors, caller_kind)
            if match:
                return match
    return {}


def read_bridge_session() -> dict:
    """Return the active bus.mjs bridge's claim, or {} if absent/stale.

    Resolution order (e-1460):
      1. Pid-tree match in .beacon/bridges/<sid>.json — when multiple
         bclaude share a cwd, each owns its own per-sid claim and is
         identified by parent_pid. The match is bclaude-keyed.
      2. Legacy .beacon/bridge.json — single-claim back-compat for
         pre-e-1460 cwds. Only used when no per-sid claim matches.

    A claim is considered stale (and ignored) when its recorded ``pid``
    (= bus.mjs pid) is no longer alive on this host. The bridge clears
    its claim file on a graceful shutdown; crashes / SIGKILL leave a
    stale claim that this pid-liveness check then drops.

    Shape: ``{session_id, pid, cwd, started_at}`` (legacy) or with
    ``parent_pid`` added (per-sid).
    """
    # 1. Pid-tree match in bridges/<sid>.json — e-1460
    claim = find_my_bridge_claim()
    if claim:
        return claim
    # 2. Legacy single-claim fallback
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


def _codex_session_pointer_path() -> Path:
    """Cwd-level pointer the Codex receive-loop daemon publishes (e-2531).

    Mirrors ``scripts/codex-receive-loop.py:_session_pointer_file`` — the daemon
    writes ``{session_id, project_id, ...}`` here on (re)start so any process in
    the cwd (hooks, the agent's own `beacon bus send`) can discover the stable
    codex- sid without pid math.
    """
    return Path.cwd() / ".beacon" / "codex" / "receive-loop.session.json"


def read_codex_session_pointer() -> str:
    """Return the Codex daemon's stable session_id for this cwd, or "".

    Best-effort: any read/parse failure yields "" so the caller falls through
    to the normal mint chain. Only the ``session_id`` field is consumed here;
    the rest of the pointer (project_id / thread / app_server_url) is for the
    Codex hook path.
    """
    try:
        path = _codex_session_pointer_path()
        if not path.is_file():
            return ""
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return str(data.get("session_id") or "").strip()
    except Exception:
        return ""
    return ""


def resolve_active_session_id() -> str:
    """Return the session_id the CLI should treat as the current session.

    Resolution order (e-1331 + e-1460 + e-2531):
      1. Bridge claim — pid-tree match in .beacon/bridges/<sid>.json
         (per-bclaude isolation), or legacy .beacon/bridge.json fallback.
      2. Codex session pointer (e-2531) — the stable codex- sid the Codex
         receive-loop daemon published for this cwd, so every Codex send path
         shares one sender identity instead of minting a fresh sv-.
      3. Fall through to :func:`get_session_id` which applies the
         existing env / mint / session.json precedence (which is itself
         pid-tree-aware as of e-1460).

    ms-95 / e-2870 — when this is called reentrantly during a cloud-first
    mint (= ``api_client._request`` stamping ``X-Beacon-Session`` while
    ``get_or_mint_session_via_server`` is in flight), return the empty
    string instead of triggering another mint. The mint itself will
    materialise the session_id and store it; there is no in-flight sid
    to stamp on the ``me_heartbeat`` request that produced the reentry.
    """
    if _in_cloud_first_mint.get():
        return ""
    claim = read_bridge_session()
    if claim.get("session_id"):
        return claim["session_id"]
    # e-2531: adopt the Codex receive-loop session pointer if present. Without
    # this, a Codex-originated `beacon bus send` that does NOT set
    # BEACON_BUS_SENDER (= any non-daemon path: the agent sending directly)
    # falls through to get_session_id() and mints a FRESH sv- every call, so
    # the same Codex session's outbound DMs churn their sender identity and
    # "who replied" attribution in the directory breaks. The pointer holds the
    # stable codex- sid the daemon minted, shared by every process in this cwd.
    # Ordering is safe: a Claude send matches the pid-tree bridge claim above
    # and never reaches here; a Codex send is a descendant of the daemon (not
    # a bclaude), so the claim is empty and the cwd-level pointer is correct.
    codex_sid = read_codex_session_pointer()
    if codex_sid:
        return codex_sid
    return get_session_id()


__all__ = [
    "ENV_FORCE_MINT",
    "ENV_PARENT_PID",
    "ENV_SESSION_ID",
    "ENV_USE_CLOUD_FIRST",
    "find_my_bridge_claim",
    "get_or_mint_session",
    "fork_workspace",
    "list_forks",
    "get_or_mint_session_via_server",
    "get_session_id",
    "mint_fresh_session",
    "read_all_bridge_claims",
    "read_bridge_session",
    "read_codex_session_pointer",
    "read_session",
    "resolve_active_session_id",
    "update_last_active",
    "write_session",
]


# ---------------------------------------------------------------------------
# Workspace fork (ms-67 / e-1549)
# ---------------------------------------------------------------------------

def _default_worktree_runner(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr).

    Centralised here so tests can inject a fake runner via the
    ``runner=`` kwarg on :func:`fork_workspace` without needing to
    monkey-patch ``subprocess.run`` at module scope.
    """
    import subprocess as _sp
    proc = _sp.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def fork_workspace(
    ms_id: str,
    ms_title: str,
    *,
    parent_session_id: str,
    parent_branch: str,
    parent_repo_path: Path | str,
    short_uuid: str | None = None,
    runner=None,
) -> dict:
    """Create a sibling worktree for parallel work on ``ms_id``.

    Carries out the 4-step setup that ms-67 (= /beacon-session-fork Skill)
    folds into one command:

    1. ``git worktree add <wt-path> -b <child-branch>``
    2. copy ``.beacon/cloud.json`` from the parent repo so the child
       worktree binds to the same Beacon project
    3. run ``beacon channel install`` in the child worktree to generate
       ``.mcp.json`` (= MCP server declaration)
    4. write ``.beacon/fork.json`` recording the parent ↔ child link so
       the child's ``/beacon-session-start`` can surface "you were forked
       from sv-xxxx"

    All side effects flow through ``runner`` so tests can replace it with
    a fake. ``short_uuid`` is also injectable for deterministic test
    output; default is 6 hex characters.

    Returns a dict ``{worktree_path, branch, fork_record}`` describing
    what was created. Raises ``RuntimeError`` on subprocess failure with
    enough context for the caller to surface a useful message.
    """
    if not ms_id or not ms_id.startswith("ms-"):
        raise ValueError(f"ms_id must look like 'ms-<n>', got {ms_id!r}")

    parent_root = Path(parent_repo_path).resolve()
    if short_uuid is None:
        short_uuid = secrets.token_hex(3)  # 6 hex chars
    branch = f"{ms_id}-fork-{short_uuid}"
    wt_path = parent_root / ".worktrees" / f"{ms_id}-fork-{short_uuid}"

    if runner is None:
        runner = _default_worktree_runner

    # Step 1: git worktree add — also creates the branch
    rc, stdout, stderr = runner(
        ["git", "worktree", "add", str(wt_path), "-b", branch],
        str(parent_root),
    )
    if rc != 0:
        raise RuntimeError(
            f"git worktree add failed (rc={rc}): {stderr.strip() or stdout.strip()}"
        )

    # Step 2: copy .beacon/cloud.json so the child binds to the same project,
    # and symlink .beacon/project.json so the SessionStart hook's
    # `test -f .beacon/project.json` check passes in the child cwd. Without
    # this the hook silently skips and the child never auto-runs
    # /beacon-session-start (= bug found during ms-67 e-1554 dogfood, the hook
    # is global ~/.claude/settings.json and doesn't walk up).
    parent_cloud = parent_root / ".beacon" / "cloud.json"
    parent_project = parent_root / ".beacon" / "project.json"
    child_beacon_dir = wt_path / ".beacon"
    child_beacon_dir.mkdir(parents=True, exist_ok=True)
    if parent_cloud.exists():
        child_beacon_dir.joinpath("cloud.json").write_bytes(parent_cloud.read_bytes())
    if parent_project.exists():
        symlink_path = child_beacon_dir / "project.json"
        if not symlink_path.exists() and not symlink_path.is_symlink():
            symlink_path.symlink_to(parent_project)

    # Step 3: run beacon channel install in the child worktree
    rc, stdout, stderr = runner(
        ["beacon", "channel", "install"],
        str(wt_path),
    )
    if rc != 0:
        # Channel install failure is not fatal for the fork itself — the
        # child can re-run it later. Record the failure in fork.json so
        # the child's session-start can surface a hint, but keep going.
        channel_install_status = {"ok": False, "stderr": stderr.strip()[:500]}
    else:
        channel_install_status = {"ok": True}

    # Step 3.5: force-refresh the project cache (ms-67 hotfix / 親 fork stale-cache 観測)
    # beacon status writes back the cloud-fresh project.json so the child's
    # session-start sees the actual milestone set, not the parent's pre-fork
    # stale snapshot. Non-fatal — if it fails the child can still operate,
    # just from a stale view until the next beacon command refreshes.
    # See docs/memo (ms-36 領域) for root-cause context — this is a band-aid
    # over a deeper cache/cwd interaction that ms-36 retro should revisit.
    rc, stdout, stderr = runner(
        ["beacon", "status", "--json"],
        str(wt_path),
    )
    status_refresh_status = {"ok": rc == 0}
    if rc != 0:
        status_refresh_status["stderr"] = stderr.strip()[:500]

    # Step 4: write .beacon/fork.json
    fork_record = {
        "parent_session_id": parent_session_id,
        "parent_branch": parent_branch,
        "parent_repo_path": str(parent_root),
        "target_ms_id": ms_id,
        "target_ms_title": ms_title,
        "child_branch": branch,
        "created_at": _now_iso(),
        "channel_install": channel_install_status,
        "status_refresh": status_refresh_status,
    }
    child_beacon_dir.joinpath("fork.json").write_text(
        json.dumps(fork_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "worktree_path": str(wt_path),
        "branch": branch,
        "fork_record": fork_record,
    }


def list_forks(repo_root: Path | str, runner=None) -> list[dict]:
    """List active fork worktrees under ``repo_root``.

    A fork is identified by a worktree (anything under
    ``<repo_root>/.worktrees/`` or any path reported by ``git worktree
    list``) that has a ``.beacon/fork.json`` file. Returns a list of
    dicts with the visible fields callers care about:

      worktree_path / target_ms_id / target_ms_title / child_branch /
      parent_session_id / parent_branch / created_at

    Skips worktrees that don't have ``.beacon/fork.json`` so this is
    safe to call in any repo, including ones that have non-fork
    worktrees (e.g., ms-65 cwd-aware milestone start branches).

    Worktrees that ``git`` no longer tracks (= already removed but
    leftover dir) are filtered out via ``git worktree list``.
    """
    repo_root = Path(repo_root).resolve()
    if runner is None:
        runner = _default_worktree_runner

    rc, stdout, stderr = runner(
        ["git", "worktree", "list", "--porcelain"],
        str(repo_root),
    )
    if rc != 0:
        return []

    # Parse porcelain: blocks separated by blank lines, each block has
    # a "worktree <path>" line first.
    wt_paths: list[Path] = []
    for block in stdout.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("worktree "):
                wt_paths.append(Path(line[len("worktree "):]))
                break

    forks: list[dict] = []
    for wt in wt_paths:
        fj = wt / ".beacon" / "fork.json"
        if not fj.exists():
            continue
        try:
            record = json.loads(fj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        forks.append({
            "worktree_path": str(wt),
            "target_ms_id": record.get("target_ms_id", ""),
            "target_ms_title": record.get("target_ms_title", ""),
            "child_branch": record.get("child_branch", ""),
            "parent_session_id": record.get("parent_session_id", ""),
            "parent_branch": record.get("parent_branch", ""),
            "created_at": record.get("created_at", ""),
        })
    return forks
