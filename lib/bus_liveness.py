"""ms-165 / e-5965 — the *progress* dimension of session liveness.

Session liveness has three orthogonal dimensions (CORE doc
``liveness-three-dimensions``). Each answers a different question and is
computed from a different signal, so collapsing them into one field hides the
failure mode the others can't see:

  * **transport** — can a receive path reach this session at all?
    (``ws_live`` / ``poll_health.healthy`` → ``live``). The picker reads this.
  * **progress**  — is a live session actually CONSUMING (draining) its inbox,
    or receiving-but-not-consuming (wedged)?  (``draining`` → ``reachable``).
    Only the send-path strict check reads this. THIS MODULE.
  * **attention** — is a human/AI actively driving the session right now?
    (``heartbeat_fresh``). Informational only.

The wedge this dimension catches: a session whose bridge polls (so ``live`` is
true) but never advances its cursor — DMs addressed to it pile up unread. The
sender is fooled into "sent✓ delivered✗" because the transport check only
proves polling. ``draining`` is derived from the EXISTING unread + cursor state
(no new schema): if the oldest event still unread by the recipient is older
than the attentiveness window, the session is receiving but not consuming.

Pure functions only — the impure store scan that produces
``oldest_unread_created_at`` lives on the server (``app._oldest_unread_addressed_created_at``).
Keeping the derivation pure lets "stale unread ⇒ not draining" be pinned
without a bus fixture.
"""
from __future__ import annotations

import datetime
from typing import Optional

# Send-path graded verdicts (ms-165 SPEC 方針 b). Named constants so callers
# compare against a symbol, not a bare string that could drift.
SEND_NORMAL = "normal"
SEND_WEDGED = "wedged"
SEND_NOT_LIVE = "not_live"


def derive_draining(oldest_unread_created_at, now, window_seconds) -> Optional[bool]:
    """Return whether a session is *draining* its inbox.

    - ``True``  — no unread backlog, OR the oldest unread event is younger than
      ``window_seconds`` (still in the normal in-flight grace period).
    - ``False`` — the oldest event still unread by the recipient is OLDER than
      ``window_seconds``: it is receiving (polling) but not consuming = wedged.
    - ``None``  — unknown (no timestamp to judge, or unparseable). Never a hard
      signal; callers treat ``None`` as "not definitively wedged" so an idle
      session with no backlog is never wrongly dropped.

    ``oldest_unread_created_at`` is the ``created_at`` of the OLDEST event still
    unread by the recipient (past its cursor), or a falsy value when the inbox
    has no backlog. The threshold reuses the attentiveness window (a healthy
    bridge drains in seconds; a backlog older than the human-attention window is
    a wedge, not in-flight latency) — no new constant, per SPEC 方針 a.
    """
    if not oldest_unread_created_at:
        # No backlog past the cursor → the session is keeping up.
        return True
    try:
        oldest = datetime.datetime.fromisoformat(
            str(oldest_unread_created_at).replace("Z", "+00:00"))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        # Unparseable stamp — unknown, not dead. Fail toward reachable.
        return None
    age = (now - oldest).total_seconds()
    return age <= window_seconds


def is_reachable(live, draining) -> bool:
    """``reachable = live AND (draining is not False)``.

    Only a *definitive* wedge (``draining is False``) makes a live session
    unreachable. Unknown draining (``None``) keeps a live session reachable, so
    an idle fork with no backlog is never dropped from the picker (the SPEC 方針
    c no-regression guarantee: the ``live`` union is unchanged; ``reachable`` is
    an ADDITIONAL field only the send path reads strictly).
    """
    return bool(live) and (draining is not False)


def classify_send_delivery(live, draining) -> str:
    """3-tier graded send verdict for a DM recipient (SPEC 方針 b).

    - ``SEND_NORMAL``   — live and reachable: deliver as usual; the send
      response is byte-unchanged (the healthy common path must not regress).
    - ``SEND_WEDGED``   — live but NOT draining: still enqueued, but the send
      response carries loud ``recipient_wedged`` / ``delivery_uncertain`` flags
      so every client path (CLI --json, MCP reply, headless) surfaces it — not
      a soft field a caller can skip past.
    - ``SEND_NOT_LIVE`` — no transport liveness at all: existing behaviour
      unchanged (the picker / soft-warn path already covers a dead session).
    """
    if not live:
        return SEND_NOT_LIVE
    if draining is False:
        return SEND_WEDGED
    return SEND_NORMAL
