"""Cross-session pending DM action display helpers (ms-70 / e-1714).

When a cross-user DM (= direct message) bus envelope carries action
implication, ms-70 / e-1713's dispatcher gate writes an
``approval_status="pending"`` sidecar row in ``bus_event_approvals`` and
downgrades effective delivery to ``propose-to-ai``. Doing that closes the
auto-act path while the receiver's terminal is closed — but the receiver
also needs a way to **see** the pending request when they open a new
session. e-1714 closes that gap: ``/beacon-session-start`` calls the
server's ``GET /api/projects/{project_id}/dm/pending`` endpoint, then
formats the response with the helpers in this module.

The Skill is markdown and therefore unrenderable / untestable on its own.
Putting the format logic here in a pure Python helper lets us pin three
critical behaviours with tests (= AC item 5):

(a) Empty pending list collapses to an empty string so the Skill can
    omit the section entirely (no noisy "0 件" header).
(b) Multiple pending rows render a summary plus a one-line hint that
    ``beacon dm show <event_id>`` (= future e-1716 CLI primitive) will
    surface envelope detail.
(c) Already-decided sidecar rows (approved / denied) are filtered out so
    a stale audit row never leaks into the session-start banner. The
    server-side ``list_pending_approvals`` already filters by
    ``approval_status == "pending"``, but a defense-in-depth filter here
    means the helper stays correct even if a caller passes a raw mixed
    list (e.g. from a future "all sidecar rows" endpoint).
"""

from __future__ import annotations

from typing import Iterable


# Header / hint strings are exposed as module constants so tests can pin
# the literal expected by the Skill banner without copy-pasting strings
# across files. The Skill markdown references these by reading the helper
# output verbatim — no string templating on the Skill side.
PENDING_DM_HEADER = "保留中 DM action:"
PENDING_DM_DETAIL_HINT = (
    "  ※ 詳細は `beacon dm show <event_id>` で envelope を展開できます "
    "(= e-1716 で primitive 化予定)。各 envelope は終端で "
    "`beacon dm respond approve|deny <event_id>` で決定してください。"
)


def _is_pending(row: dict) -> bool:
    """Defense-in-depth filter: row must explicitly say approval_status=pending.

    The server's ``list_pending_approvals`` already filters, but a future
    caller might hand this helper a mixed list (e.g. from a debug
    endpoint). Filtering here keeps the banner free of approved / denied
    / auto rows even when upstream is sloppy.
    """
    return (row or {}).get("approval_status") == "pending"


def filter_pending_only(rows: Iterable[dict]) -> list[dict]:
    """Return only the rows whose ``approval_status`` is ``"pending"``.

    Exposed as a public helper so the Skill can also call it independently
    (= belt + suspenders if the endpoint is bypassed).
    """
    return [r for r in rows if _is_pending(r)]


def _format_one_row(row: dict) -> str:
    """Render one pending sidecar row as a single bullet line.

    Schema fields consumed (from ``server/firestore_client.py``
    ``list_pending_approvals`` / ``put_bus_event_approval``):
      - event_id
      - sender_user_id
      - receiver_user_id (carried but not shown — the banner is already
        scoped to "my pending" via the endpoint query)
      - created_at

    The envelope's ``actions_authorized`` is not stored on the sidecar
    (= ms-70 / e-1712 schema by design — the sidecar is decision state,
    the envelope itself stays in ``bus_events``). The Skill's follow-up
    ``beacon dm show <event_id>`` is the canonical path to surface
    action detail; the banner just lists the event ids so the human can
    drill in.
    """
    event_id = row.get("event_id") or "(unknown)"
    sender = row.get("sender_user_id") or "(unknown sender)"
    created_at = row.get("created_at") or "(unknown time)"
    return f"  - {event_id} from {sender} at {created_at}"


def format_pending_dm_summary(rows: Iterable[dict] | None,
                              *, detail_hint: bool = True) -> str:
    """Format pending sidecar rows into a session-start banner section.

    Returns an empty string when the input is empty / None / contains no
    pending rows — so the caller can ``if section: emit(section)`` and
    drop the section entirely without an empty header.

    When at least one pending row exists, returns a multi-line block:

        保留中 DM action: N 件
          - <event_id> from <sender_user_id> at <created_at>
          - ...
          ※ 詳細は `beacon dm show <event_id>` で envelope を展開できます ...

    The ``detail_hint`` flag controls whether the trailing hint line is
    appended. Tests pin both modes so a future tweak (e.g. drop the hint
    for non-verbose output) does not silently change the contract.
    """
    if not rows:
        return ""
    pending = filter_pending_only(rows)
    if not pending:
        return ""
    n = len(pending)
    lines = [f"{PENDING_DM_HEADER} {n} 件"]
    for r in pending:
        lines.append(_format_one_row(r))
    if detail_hint:
        lines.append(PENDING_DM_DETAIL_HINT)
    return "\n".join(lines)
