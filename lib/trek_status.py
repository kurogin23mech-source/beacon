"""Session-start Trek status display helpers (ms-75 e-1813/e-1854/e-2047,
consolidated ms-85 e-3180).

session-start shows two Trek things at start of session:

  * Step 1o   — the list of joined Treks, plus a reminder that Trek scope
    grants blanket auto-approval for in-scope DMs, so the user always knows
    which blanket exception they are currently under.
  * Step 1o-2 — for active Treks, an armed (autonomous-execution) self-check
    warning when the session is not armed.

e-3180 pulls the display / decision logic that used to live as prose in the
Skill markdown into pure, tested functions so ``scripts/session-start-trek-status.py``
can render both sections from one call site. The rendered text is unchanged.

The module holds:
  * ``format_joined_treks`` — the per-trek block list (Step 1o body).
  * ``has_active_trek`` — whether any joined trek is ``status == "active"``
    (gates both the blanket reminder and the armed self-check).
  * ``is_armed`` — the AND condition from Step 1o-2 (a trek-系 channel is in
    ``bus_auto_execute_channels`` AND budget is armed).
  * ``BLANKET_APPROVAL_REMINDER`` / ``NOT_ARMED_WARNING`` — the exact literal
    lines the Skill emitted, exposed as constants so tests pin them.
"""
from __future__ import annotations

from typing import Iterable


BLANKET_APPROVAL_REMINDER = (
    "Trek 参加中: 同 Trek scope 内の DM (= 計画 / 議論 / 実装計画) は自動承認 "
    "(= blanket 例外、 ms-70/e-1854) で配信されています。 撤回したい場合は "
    "`beacon trek leave <trek-id>` を実行してください。 デプロイ / リリースのみ "
    "user 確認境界です。"
)

NOT_ARMED_WARNING = (
    "⚠ Trek 参加中だが自律実行モードが not-armed です。 `/beacon-bus-armed` で"
    "起動するか、 `beacon trek join <trek-id>` を再実行して auto-arm し直して"
    "ください (= 進行 DM が wake せず silent-ack 病理を再生します)。"
)

# Trek-系 channels that count toward "armed" (Step 1o-2).
TREK_CHANNELS = {"trek-progress-check", "trek-trigger", "trek-task-review"}


def has_active_trek(treks: Iterable[dict] | None) -> bool:
    """True when at least one joined trek is ``status == "active"``.

    Only active treks carry the blanket auto-approval exception and are the
    subject of the armed self-check; planning / archived treks are shown but
    do not trigger the warnings.
    """
    for t in treks or []:
        if (t or {}).get("status") == "active":
            return True
    return False


def format_joined_treks(treks: Iterable[dict] | None) -> str:
    """Render the joined-trek list section (Step 1o body).

    One block per trek, mirroring the fields the Skill prose enumerated:
      - ``[trek_id] title`` (1 line)
      - ``status: <status>``
      - ``⚠ HALTED: <reason>`` when ``halt`` is non-null (emphasized)
      - ``目標: <goal_state>`` when goal_state is non-empty
      - ``他 N 名`` for the other members (members minus self; --joined already
        filters to treks the caller is in, so self is one of the members)

    Returns "" for empty input so the caller can omit the section.
    """
    treks = list(treks or [])
    if not treks:
        return ""
    blocks: list[str] = []
    for t in treks:
        t = t or {}
        trek_id = str(t.get("trek_id") or "")
        title = str(t.get("title") or "")
        lines = [f"[{trek_id}] {title}"]
        lines.append(f"  status: {t.get('status') or ''}")
        halt = t.get("halt")
        if halt:
            reason = ""
            if isinstance(halt, dict):
                reason = str(halt.get("reason") or "")
            lines.append(f"  ⚠ HALTED: {reason}")
        goal = str(t.get("goal_state") or "").strip()
        if goal:
            lines.append(f"  目標: {goal}")
        others = max(0, len(t.get("members") or []) - 1)
        lines.append(f"  他 {others} 名")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def is_armed(channels_payload, budget_payload) -> bool:
    """Return the Step 1o-2 armed AND-condition.

    armed := a trek-系 channel is present in ``bus_auto_execute_channels``
             AND budget is armed (total > 0 AND used < total).

    Accepts the raw JSON payloads from ``beacon bus auto-execute list --json``
    and ``beacon bus budget show --json``. Missing / malformed payloads
    resolve to "not armed" so a fetch failure surfaces the warning rather
    than silently claiming armed (fail-loud on the safety-relevant side —
    same posture as the original prose).
    """
    channels = _extract_channels(channels_payload)
    if not (TREK_CHANNELS & set(channels)):
        return False
    total, used = _extract_budget(budget_payload)
    return total > 0 and used < total


def _extract_channels(payload) -> list[str]:
    """Pull the auto-execute channel list from a few plausible shapes."""
    if isinstance(payload, list):
        return [str(c) for c in payload]
    if isinstance(payload, dict):
        for key in ("bus_auto_execute_channels", "channels", "auto_execute"):
            v = payload.get(key)
            if isinstance(v, list):
                return [str(c) for c in v]
    return []


def _extract_budget(payload) -> tuple[int, int]:
    """Return (total, used) from a budget payload, defaulting to (0, 0)."""
    if not isinstance(payload, dict):
        return 0, 0
    try:
        total = int(payload.get("total") or 0)
    except Exception:
        total = 0
    try:
        used = int(payload.get("used") or 0)
    except Exception:
        used = 0
    return total, used
