"""ms-136 e-4697 — inward inject: the test-injection seam on the receiving line.

The auto-debug基盤 (ms-136) verifies real use-case journeys by running the real
CLI in a throwaway environment. A sales journey has a step that Beacon can never
produce on its own during a test: **the customer replies**. In production that
inbound arrival is discovered by the reply-watch (E) / standup-intake (D) Skills
polling Gmail / Slack, then recorded as an inbound Communication. In a test no
real reply comes, so the journey stalls with the ball永遠に相手 (counterpart) and
nothing to ingest.

This module is the SPEC's 方針5 foundation: instead of building an *outward*
suppression flag (Beacon never auto-sends anyway — send = 人間承認 + MCP, so a
journey naturally stops at "送信intent を記帳した" and fires nothing外向き), we
make the *inward* (受信) side test-injectable. `inject_inbound_communication`
feeds a擬似着信 (a fake arriving reply) through the **same** receiving line the
real Skills use — `sales_entities.communication_add(direction=inbound)` — so the
journey can be driven past the wait: 取り込み (ingest) → ball が自分に戻る →
phase 前進 becomes possible.

Boundary (方針4 = 検証境界は Beacon の責任 L2 まで):
  - This is the ingest (取り込み) side only. It appends an inbound Communication
    and reads the derived ball. It performs **zero** outbound / network / bus /
    MCP I/O — structurally, its only dependencies are `sales_entities` (pure
    project-data mutation) and `work_base` (timestamps). So "本物の外部送信は
    一切発火しない" (task e-4697 AC #3) holds by construction, not by a flag.
  - Phase 前進 itself stays an explicit judgement call (advance/terminal_
    transition). Injecting an arrival only flips the ball back to us — the
    *precondition* for advancing — which is what lets a journey verify the full
    取り込み→ball→phase chain (AC #2). The scenario runner (e-4698) drives the
    advance step; this seam does not advance on its own.

Injected arrivals are stamped `source["injected"] = True` so a擬似着信 is always
distinguishable from a real customer reply in the audit trail — the same seam
could run against a shared / cloud project, and a test arrival must never be
mistaken for a real one.
"""

from __future__ import annotations

from typing import Optional

import sales_entities as se
import work_base

# Marker key written into a Communication's ``source`` dict to flag it as a
# 擬似着信 (test-injected arrival) rather than a real customer reply.
INJECT_SOURCE_MARKER = "injected"


def inject_inbound_communication(
    data: dict,
    target_id: str,
    summary: str,
    *,
    channel: str = "email",
    body: str = "",
    source_ref: str = "",
    source_url: str = "",
    occurred_at: str = "",
    at: str = "",
) -> dict:
    """Inject a擬似着信 (fake inbound reply) onto the receiving line and report
    the ingest result.

    Simulates "the counterpart just replied on ``channel``" by appending an
    **inbound** Communication to ``target_id`` (an opp-/acc- target or an
    act-/nrt- work item) via the real ``sales_entities.communication_add`` path
    — the exact seam the reply-watch (E) / standup-intake (D) Skills feed in
    production. The arrival is stamped ``source["injected"] = True`` for audit.

    ``direction`` is always inbound: an injected arrival *is* a受信 by
    definition (方針5 — the outward side needs no injection because Beacon never
    auto-sends). ``at`` (default = now) is used as ``created_at`` and, when
    ``occurred_at`` is blank, as the occurrence time too, so a bare inject lands
    at the tail of the chronological log and drives the derived ball.

    Returns a machine-checkable result the scenario runner can assert on::

        {
          "comm_id":     "comm-3",
          "target_id":   "opp-1",       # the resolved container (opp/acc)
          "channel":     "email",
          "direction":   "inbound",
          "ball_before": "counterpart", # None if the deal had no comms yet
          "ball_after":  "self",
          "ingested":    True,          # ball_after == BALL_SELF (取り込み成立)
        }

    ``ingested`` is the 取り込み成功 signal (AC #2): a fresh inbound arrival must
    return the ball to us. It is True iff the derived ball is now BALL_SELF.

    Raises ValueError (surfacing ``communication_add``'s own guards) when the
    target cannot be resolved or ``summary`` is empty.
    """
    container, _linked = se.resolve_communication_target(data, target_id)
    if container is None:
        raise ValueError(
            "inject target not found (opp-…/acc-… target or act-…/nrt-… work "
            f"item): {target_id}")

    stamp = at or work_base.now_iso()

    # Audit marker + trace pointers. Kept as a plain dict so the injected flag
    # travels with the record and shows up in `communication list`.
    source: dict = {INJECT_SOURCE_MARKER: True}
    if source_ref:
        source["ref"] = source_ref
    if source_url:
        source["url"] = source_url

    ball_before = se.derive_ball(container)

    comm_id = se.communication_add(
        data,
        target_id,
        summary,
        direction=se.COMM_INBOUND,
        channel=channel,
        body=body,
        source=source,
        occurred_at=occurred_at or stamp,
        created_at=stamp,
    )

    ball_after = se.derive_ball(container)

    return {
        "comm_id": comm_id,
        "target_id": container.get("id", target_id),
        "channel": (channel or "").strip().lower() or "other",
        "direction": se.COMM_INBOUND,
        "ball_before": ball_before,
        "ball_after": ball_after,
        "ingested": ball_after == se.BALL_SELF,
    }


def is_injected(comm: dict) -> bool:
    """True if ``comm`` is a擬似着信 (test-injected arrival) — i.e. its ``source``
    carries the inject marker. Lets audit / cleanup tell fake arrivals from real
    customer replies (e.g. to strip them before treating a project as real)."""
    return bool((comm or {}).get("source", {}).get(INJECT_SOURCE_MARKER))
