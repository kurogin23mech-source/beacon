"""Morning briefing aggregator (ms-55 e-1650).

The output surface of the ms-55 両輪 design. After a night of
autonomous execution, the user opens `beacon morning` and sees a
4-category summary:

  ✓ 完了 (Completed):   work that ran end-to-end without halting
  ⚠ 停止 (Halted):      stop signals raised + rollback status
  ✗ skip (Skipped):     work auto-deferred because a dependency halted
  ⏱ 介入要望 (Needs hand): everything else needing the human's eyes
                          (= STUCK timeouts, expired credentials, etc.)

This module is pure aggregation. It takes a list of events (= bus
events on the relevant channels — `stop-signal`, `claim-signal` — plus
any synthetic records the CLI cares to add) and a window
(`since` / `until` datetimes) and produces a structured digest.

The CLI in lib/commands.py wraps this with the I/O — reading events
from the bus, projecting them into the structured input format, and
formatting the output for terminal vs JSON consumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import stop_signal as _stop
import claims as _claims


# ---------------------------------------------------------------------------
# Bucket constants
# ---------------------------------------------------------------------------

BUCKET_COMPLETED = "completed"      # ✓
BUCKET_HALTED = "halted"            # ⚠
BUCKET_SKIPPED = "skipped"          # ✗
BUCKET_NEEDS_ATTENTION = "needs_attention"  # ⏱

BUCKETS = (
    BUCKET_COMPLETED,
    BUCKET_HALTED,
    BUCKET_SKIPPED,
    BUCKET_NEEDS_ATTENTION,
)


# ---------------------------------------------------------------------------
# Briefing record types
# ---------------------------------------------------------------------------

@dataclass
class BriefingEntry:
    """One row in the morning briefing.

    Fields:
      bucket: one of BUCKETS.
      title: short summary shown in the digest.
      detail: optional extended text (= reason, target ref, ...).
      at: ISO8601 timestamp of when this happened.
      source: short tag for traceability (e.g. "stop-signal",
        "claim-release", "stuck-detector").
      ref: optional reference id (event_id / claim_id / commit_hash).
    """
    bucket: str
    title: str
    detail: str = ""
    at: str = ""
    source: str = ""
    ref: str = ""


@dataclass
class Briefing:
    """Structured output of build_briefing.

    Counters per bucket let consumers render headlines without
    re-scanning the entries list.
    """
    since: str = ""
    until: str = ""
    entries: list[BriefingEntry] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def by_bucket(self, bucket: str) -> list[BriefingEntry]:
        return [e for e in self.entries if e.bucket == bucket]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _in_window(ts: str, *, since: Optional[datetime],
               until: Optional[datetime]) -> bool:
    dt = _parse_iso(ts)
    if dt is None:
        return False
    if since and dt < since:
        return False
    if until and dt > until:
        return False
    return True


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def build_briefing(
    events: list[dict],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    extra_entries: Optional[list[BriefingEntry]] = None,
) -> Briefing:
    """Reduce a bus event stream + extra synthetic records into a
    bucketed briefing.

    Args:
      events: bus events. Stop / resume on `stop-signal` and
        claim/response/release on `claim-signal` are recognized;
        other channels are ignored. Each event must have `channel`,
        `payload`, and `created_at`.
      since / until: optional window. Events outside are dropped.
      extra_entries: synthetic records the caller wants in the digest
        without inventing fake bus events (e.g. commit summaries
        pulled from `git log`).

    Returns a Briefing with the entries sorted ascending by `at`.

    Bucketing rules:
      * stop signal with reason_kind="stuck" → 介入要望
      * other stop signals → 停止
      * claim_release with outcome="completed" → 完了
      * claim_release with outcome="abandoned" → skip
      * extra_entries are inserted verbatim with their declared bucket
    """
    entries: list[BriefingEntry] = []

    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("created_at") or ""
        if not _in_window(ts, since=since, until=until):
            continue

        channel = ev.get("channel") or ""

        if channel == _stop.STOP_CHANNEL:
            stop = _stop.parse_stop_event(ev)
            if stop:
                bucket = (
                    BUCKET_NEEDS_ATTENTION
                    if stop.get("reason_kind") == "stuck"
                    else BUCKET_HALTED
                )
                target = stop.get("target") or {}
                if stop.get("scope") == _stop.SCOPE_GLOBAL:
                    tag = "GLOBAL"
                else:
                    tag = f"{target.get('kind', '?')}:{target.get('id', '?')}"
                title = (
                    f"STOP signal on {tag} "
                    f"({stop.get('reason_kind', '?')})"
                )
                entries.append(BriefingEntry(
                    bucket=bucket,
                    title=title,
                    detail=stop.get("reason") or "",
                    at=stop.get("issued_at") or ts,
                    source="stop-signal",
                    ref=stop.get("event_id") or "",
                ))
                continue
            # resume on stop-signal channel is informational only;
            # don't surface in the briefing.
            continue

        if channel == _claims.CLAIM_CHANNEL:
            release = _claims.parse_release_event(ev)
            if release:
                outcome = release.get("outcome")
                if outcome == _claims.OUTCOME_COMPLETED:
                    bucket = BUCKET_COMPLETED
                    verb = "claim released as completed"
                elif outcome == _claims.OUTCOME_ABANDONED:
                    bucket = BUCKET_SKIPPED
                    verb = "claim released as abandoned"
                else:
                    continue
                entries.append(BriefingEntry(
                    bucket=bucket,
                    title=f"{verb} ({release.get('claim_id', '?')})",
                    detail=release.get("reason") or "",
                    at=release.get("issued_at") or ts,
                    source="claim-release",
                    ref=release.get("event_id") or "",
                ))
                continue
            # claim / claim_response are intermediate state, not part
            # of the morning surface.
            continue

    if extra_entries:
        for e in extra_entries:
            if not isinstance(e, BriefingEntry):
                continue
            if e.bucket not in BUCKETS:
                continue
            if not _in_window(e.at, since=since, until=until):
                continue
            entries.append(e)

    entries.sort(key=lambda e: e.at or "")

    counts = {b: 0 for b in BUCKETS}
    for e in entries:
        counts[e.bucket] = counts.get(e.bucket, 0) + 1

    return Briefing(
        since=since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else "",
        until=until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else "",
        entries=entries,
        counts=counts,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BUCKET_LABELS = {
    BUCKET_COMPLETED: ("✓ 完了 (Completed)", "✓"),
    BUCKET_HALTED: ("⚠ 停止 (Halted)", "⚠"),
    BUCKET_SKIPPED: ("✗ Skip", "✗"),
    BUCKET_NEEDS_ATTENTION: ("⏱ 介入要望 (Needs attention)", "⏱"),
}


def render_briefing(briefing: Briefing) -> str:
    """Format a Briefing for terminal display.

    Order is fixed: 完了 → 停止 → skip → 介入要望. Empty buckets are
    still shown with a "(none)" placeholder so the operator knows the
    aggregator looked at it.
    """
    lines: list[str] = []
    lines.append("Beacon morning briefing")
    if briefing.since or briefing.until:
        lines.append(f"  window: {briefing.since or '(start)'} → {briefing.until or '(now)'}")
    counts = briefing.counts or {}
    summary = "  summary: " + "  ".join(
        f"{_BUCKET_LABELS[b][1]} {counts.get(b, 0)}" for b in BUCKETS
    )
    lines.append(summary)
    lines.append("")

    for bucket in BUCKETS:
        label, _ = _BUCKET_LABELS[bucket]
        bucket_entries = briefing.by_bucket(bucket)
        lines.append(f"{label} — {len(bucket_entries)} item(s)")
        if not bucket_entries:
            lines.append("  (none)")
        for e in bucket_entries:
            line = f"  - [{e.at or '?'}] {e.title}"
            if e.source:
                line += f"  (source: {e.source})"
            lines.append(line)
            if e.detail:
                lines.append(f"      {e.detail}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
