"""Beacon Session Identification (ms-57 / e-1035).

ms-57 SPEC § 1〜3: 情報を3層に分離する設計の前提となるセッション識別基盤。

なぜ独立モジュールか:
* harness session_id (Claude Code 等) は素の CLI に届かないことを e-1035 で
  検証済 — 環境変数には CLAUDECODE / AI_AGENT のような presence flag しか無く、
  会話インスタンス単位で一意な ID は露出していない。
* したがって beacon 自身が session_id を *mint* する必要があり、その mint
  ロジックを 1 箇所に集約する。
* 後続タスク (e-1036 notes scoping / e-1037 session log / e-1039 孤児救済)
  すべてがこの session_id に依存するので、early に固める価値がある。

設計 (SPEC § 7 と整合):

* mint 鍵 = ``{agent_slug}-{epoch_ms}-{nonce8}``
  * agent_slug: ``lib.agent.get_actor().agent`` を URL-safe にスラグ化
  * epoch_ms: ミリ秒精度の単調増加成分 (ソート可能)
  * nonce8: ``secrets.token_hex(4)`` で 8 hex char (同 ms 内の衝突予防)
* 再利用判定 (= 同一セッション扱い): ``.beacon/session.json`` の ``last_active``
  が freshness threshold (既定 60 分) 以内なら既存 session を返す。
  stale (= dead) なら新規 mint。
* freshness threshold = ``BEACON_SESSION_FRESH_SECONDS`` 環境変数で上書き可能
  (テスト・dispatch 用)。既定 3600 秒。
* 永続化: ``.beacon/session.json`` (atomic write via tmp+rename) のみ。
  cloud session registry は本 MS の次タスクで層を分けて追加する。

side-effect-free at import — 全副作用は ``update_last_active()`` 呼び出し時のみ。
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

_SESSION_JSON_RELATIVE = Path(".beacon") / "session.json"

_DEFAULT_FRESHNESS_SECONDS = 3600

_SLUG_NORMALIZE_RE = re.compile(r"-{2,}")


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


def _slugify(name: str) -> str:
    """Lower-case, alnum + hyphen only; collapse runs of hyphens; strip edges.

    Empty / all-non-alnum input degrades to ``'unknown'`` so that mint never
    produces an id that starts or ends with a hyphen.
    """
    s = (name or "").lower().strip()
    cleaned = "".join(c if c.isalnum() else "-" for c in s)
    cleaned = _SLUG_NORMALIZE_RE.sub("-", cleaned).strip("-")
    return cleaned or "unknown"


def _detect_harness() -> str:
    """Best-effort label for the calling harness — recorded for forensics only.

    Not used in any logic; purely diagnostic so that a future operator
    inspecting .beacon/session.json can tell whether the session was started
    from Claude Code, a bare shell, etc.
    """
    if os.environ.get("CLAUDECODE") == "1":
        version = os.environ.get("AI_AGENT", "").strip()
        return version or "claude-code"
    term = os.environ.get("TERM_PROGRAM", "").strip().lower()
    if term:
        return f"shell-{term}"
    return "shell"


def _mint_session_id(actor: dict) -> str:
    """Pure mint: ``{agent_slug}-{epoch_ms}-{8 hex nonce}``.

    Pure so unit tests can lock the format without monkey-patching the
    clock — we accept a dict and use the current time / secrets module
    directly (those are the only impure bits) but the *shape* is asserted
    purely from inputs.
    """
    agent_name = (actor or {}).get("agent") or (actor or {}).get("machine") or "unknown"
    slug = _slugify(agent_name)
    epoch_ms = int(time.time() * 1000)
    nonce = secrets.token_hex(4)
    return f"{slug}-{epoch_ms}-{nonce}"


def _is_fresh(last_active_iso: str, now_iso: str, threshold_seconds: int) -> bool:
    """Return True iff ``last_active`` is within ``threshold_seconds`` of ``now``.

    Defensively tolerant of: missing input (False), trailing 'Z', and naive
    timestamps (treated as UTC — old payloads predate the timezone discipline).
    Any parse failure returns False so a corrupt timestamp cannot accidentally
    keep a dead session alive.
    """
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


# ---------------------------------------------------------------------------
# Storage IO
# ---------------------------------------------------------------------------

def read_session() -> dict:
    """Read .beacon/session.json or return ``{}`` on absence / parse failure.

    Swallow IO and JSON errors instead of raising — heartbeat must never
    break a user-facing command. A corrupt file is treated the same as
    "no session yet" and will be overwritten by the next mint.
    """
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
    """Atomically write ``data`` to .beacon/session.json (tmp + rename).

    Raises on IO failure — the caller (update_last_active) wraps in try/except
    so the CLI doesn't crash, but we don't want to silently lose writes when
    debugging: surface the error to the immediate caller.
    """
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
    """Return the current session payload, minting a new one if needed.

    Returned dict always contains: session_id, actor, created_at,
    last_active, harness. The transient ``minted`` key (True iff this call
    produced a new session) is *not* persisted — it's an in-memory signal
    for ``update_last_active`` to decide whether to bump ``last_active``.
    """
    existing = read_session()
    now = _now_iso()
    threshold = _freshness_threshold()

    if (
        existing
        and existing.get("session_id")
        and _is_fresh(existing.get("last_active", ""), now, threshold)
    ):
        return {**existing, "minted": False}

    actor = _agent.get_actor()
    payload = {
        "session_id": _mint_session_id(actor),
        "actor": actor,
        "created_at": now,
        "last_active": now,
        "harness": _detect_harness(),
    }
    write_session(payload)
    return {**payload, "minted": True}


def update_last_active() -> dict:
    """Heartbeat: get-or-mint then bump ``last_active`` to now if reusing.

    Called once per CLI sub-command from the main dispatch. Failures here
    must never propagate — the caller wraps in try/except.
    """
    session = get_or_mint_session()
    if session.pop("minted", False):
        return session
    session["last_active"] = _now_iso()
    write_session(session)
    return session


def get_session_id() -> str:
    """Convenience: return the current (mint-if-needed) session_id.

    Does NOT bump heartbeat — that is ``update_last_active``'s job. Use this
    when a downstream caller only needs the id (e.g. to tag a note).
    """
    return get_or_mint_session()["session_id"]


__all__ = [
    "get_or_mint_session",
    "get_session_id",
    "read_session",
    "update_last_active",
    "write_session",
]
