"""Receive-bridge liveness classifier (ms-93 — recipient stability follow-up).

A Beacon session sends via a CLI push (``beacon bus send`` needs no bridge) but
*receives* only through a live MCP ``beacon-bus`` bridge polling the bus. These
two paths are asymmetric, and the asymmetry fails silently: if you launch
bclaude in repo A (``.mcp.json`` present → bridge starts) and then ``cd`` into a
git worktree B that has its own ``.beacon/`` but no ``.mcp.json``, the CLI now
uses B's session_id — which has NO bridge. Sending keeps working, so it looks
connected; but incoming DMs never live-wake and only surface on the next prompt
catch-up. Observed 2026-07-07 on profile-extractor: two DMs silently stalled.

The other half of the trap is *diagnosis*. A directory row for the orphaned
session shows ``bridge=false`` and ``last_poll_at=None``. ``None`` means "no
bridge ever polled" (never started), NOT "the bridge died" — a bridge that died
leaves its last poll timestamp behind. Conflating the two sends a debugger down
the wrong path ("why did my bridge die?" when it never started).

This module is a **pure classifier over one directory row** (the shape from
``beacon bus directory --json`` / ``GET /sessions``). It powers ``beacon channel
status`` and session-start so the "silently send-only" state becomes loud.
"""

from __future__ import annotations

from typing import Any

# Verdicts (closed vocabulary — tests pin this set).
RECEIVING = "receiving"          # live bridge is polling; DMs live-wake.
NEVER_STARTED = "never_started"  # no bridge ever polled (no .mcp.json here).
STALE = "stale"                  # a bridge polled before but isn't live now.

_VERDICTS = frozenset({RECEIVING, NEVER_STARTED, STALE})


def classify_receive(row: dict[str, Any]) -> dict[str, Any]:
    """Classify a session's receive capability from its directory row.

    Returns ``{verdict, can_receive_live, headline, advice}``:

      * ``never_started`` — ``last_poll_at`` is empty: no bridge ever ran for
        this session (the silent send-only orphan). ``can_receive_live=False``.
      * ``receiving`` — a poll timestamp exists AND the row is bridge-live and
        poll-healthy. ``can_receive_live=True``.
      * ``stale`` — a poll timestamp exists but the bridge is not live now
        (died / aging out). ``can_receive_live=False``.

    The empty-vs-present ``last_poll_at`` split is the load-bearing distinction:
    it separates "never started" from "died", which look identical if you only
    read ``bridge=false``.
    """
    last_poll = str(row.get("last_poll_at") or "").strip()
    bridge = bool(row.get("bridge"))
    health = row.get("poll_health")
    healthy = bool(health.get("healthy")) if isinstance(health, dict) else False

    if not last_poll:
        return {
            "verdict": NEVER_STARTED,
            "can_receive_live": False,
            "headline": "送信専用 (受信 bridge 未起動)",
            "advice": (
                "この cwd では受信 bridge が一度も起動していません "
                "(= .mcp.json 無し / channel 未 install の可能性)。"
                "`beacon bus send` は効きますが、他セッションからの DM は "
                "live-wake せず、次回 prompt の catch-up でのみ届きます。"
                "`beacon channel install` でこの cwd の受信を有効化できます。"
            ),
        }
    if bridge and healthy:
        return {
            "verdict": RECEIVING,
            "can_receive_live": True,
            "headline": "受信中",
            "advice": "",
        }
    return {
        "verdict": STALE,
        "can_receive_live": False,
        "headline": "受信 bridge 停止の可能性",
        "advice": (
            f"受信 bridge が過去に起動しましたが、現在は live に poll して "
            f"いません (最終 poll: {last_poll})。セッションが落ちた / aging "
            f"out した可能性があります。"
        ),
    }
