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

ms-70 / e-1715: inline banner for live bus listen / receive streams.
----------------------------------------------------------------------

session-start (= e-1714) covers "terminal was closed when the DM landed".
The other half is "terminal is open *right now* and a new pending DM
arrives mid-session". That path goes through ``beacon bus listen`` /
``beacon bus receive`` — the CLIs that AI sessions stream events from.

To make the gate structurally unmissable on that path, ``cmd_bus_listen``
and ``cmd_bus_receive`` look up each outgoing event's sidecar row before
printing the JSON line. If the row says ``approval_status="pending"``,
they emit a single-line **banner** (with a recognizable prefix) on the
preceding stdout line. The JSON event payload still follows verbatim, so
the receiving Monitor / AI session sees both:

    [DM-PENDING-APPROVAL] event_id=... from=... at=... — ask user before acting
    {"event_id": "...", "channel": "dm", ...}

AI session contract (= 規約, codified here so every reader of this module
sees the same rule):

  **If an event line in your bus stream is preceded by a stdout line
  starting with the prefix ``BANNER_PREFIX``
  (``[DM-PENDING-APPROVAL]``), you MUST stop and ask the user for an
  explicit y/n decision before taking any action implied by the
  envelope. Do not auto-act, do not reply, do not run tools triggered
  by the message — surface the banner to the user verbatim and wait.**

This contract is structural (= the banner shows up in stdout the AI is
already reading) rather than advisory (= no separate "remember to
check" prompt). The PREFIX is intentionally distinct from
``PENDING_DM_HEADER`` (= the session-start summary header) so a
single-line scan can distinguish "live arrival banner" from
"start-of-session digest header".
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

# e-1715: inline banner prefix for live bus listen / receive streams.
# Tests pin this literal — any change requires re-checking docstrings
# (Skill markdown / AI gate detection logic) that reference the same
# string. Keep the prefix in single brackets so a simple ``startswith``
# scan on the stream works without regex.
BANNER_PREFIX = "[DM-PENDING-APPROVAL]"
BANNER_SUFFIX = "ask user before acting"


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


# ---------------------------------------------------------------------------
# e-1715: inline banner for live bus listen / receive streams.
# ---------------------------------------------------------------------------

def format_inline_dm_banner(event_id: str,
                            sender_user_id: str = "",
                            created_at: str = "") -> str:
    """Render a single-line banner for one pending-DM envelope.

    The CLI (= ``cmd_bus_listen`` / ``cmd_bus_receive``) emits this
    string on stdout immediately before the corresponding event's JSON
    line. AI sessions reading the stream see the banner-then-event
    pair and (per the module docstring's contract) must stop and ask
    the user before acting.

    The line is single-quoted (= no embedded newlines) so a downstream
    consumer doing per-line splitting never confuses the banner with
    the JSON event payload.

    Empty / missing fields collapse to the literal
    ``(unknown)`` so the banner stays readable even if the sidecar
    schema picks up nullable fields in the future. The structural
    information (= ``BANNER_PREFIX`` at the start, ``event_id=`` token,
    closing instruction) stays intact regardless.
    """
    eid = event_id or "(unknown)"
    sender = sender_user_id or "(unknown)"
    ts = created_at or "(unknown)"
    return (
        f"{BANNER_PREFIX} event_id={eid} from={sender} at={ts} "
        f"-- {BANNER_SUFFIX}"
    )


def build_pending_lookup(rows: Iterable[dict] | None) -> dict:
    """Index pending sidecar rows by ``event_id`` for O(1) lookup.

    Used by ``cmd_bus_listen`` / ``cmd_bus_receive`` to decide whether
    to emit a banner before printing each event. Already-decided rows
    are excluded (= same defense-in-depth filter as
    ``format_pending_dm_summary``) so a stale ``approved`` row never
    triggers a banner.

    Returns an empty dict for ``None`` / empty input. The CLI treats
    "empty dict" as "no pending — print events normally", so a transient
    server hiccup in the lookup step degrades silently to the legacy
    behaviour (= e-1715 AC requirement: do not break existing output
    when sidecar lookup fails).
    """
    if not rows:
        return {}
    out: dict = {}
    for r in rows:
        if not _is_pending(r):
            continue
        eid = (r or {}).get("event_id")
        if not eid:
            continue
        out[eid] = r
    return out
