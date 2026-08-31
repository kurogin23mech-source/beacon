"""Write-through: keep the application-map CORE doc in lockstep with the derived
deliverable-changelog render (ms-161 e-5851 / 受入条件4).

The user chose "生成 doc + write-through" for the re-home: application-map stays a
readable CORE doc (session-start / /beacon-map read it unchanged), but its BODY
becomes a GENERATED view of the root deliverable-changelog — regenerated every time
the log is mutated (``beacon deliverable add / retire / supersede`` and the e-5851
backfill seed). This is what makes 受入条件4's "手メンテなしで append/retire に自動
追随する" literally true: no human retypes the map, and a retired capability drops
out on the next mutation.

Design — REFRESH, never CREATE. ``sync_application_map`` is a no-op unless the
project is dev AND the doc already exists. Non-dev projects have no such map; and
BEFORE the backfill authors the doc once, there is nothing to auto-follow (the
write-through refreshes an existing generated doc, it does not decide to create
one). ``commands_shared.rewrite_document_body`` enforces the same "existing only"
contract, so this is belt-and-suspenders.

Import layering: a bridge above ``deliverable_map`` (render) + ``occupation``
(profession) + ``commands_shared`` (the doc write seam). Nothing in the pure
changelog/map/occupation leaves imports this, so no cycle; the CLI write verbs and
the seed script compose it AFTER their ``save_project``.
"""
from __future__ import annotations

import deliverable_map as _dm
import occupation as _occ
from commands_shared import get_store, rewrite_document_body

# The well-known CORE doc id the derived map re-homes onto (ms-104 authored it,
# ms-161 makes it generated).
APPLICATION_MAP_DOC_ID = "application-map"

# Banner prepended to the generated body so anyone who opens the doc sees it is
# machine-authored and edits belong on the changelog side. Carries NO
# ``type:ident`` wedge (the backticked verbs lack a cli:/api:/skill:/file: prefix),
# so check-map-drift never mistakes the notice for a surface to reconcile.
_GENERATED_BANNER = (
    "> ⚙ **この文書は自動生成です（手編集禁止）。** 成果ログ "
    "(deliverable-changelog) の active エントリを category で要約した導出物で、"
    "`beacon deliverable add` / `beacon deliverable retire` / "
    "`beacon deliverable supersede` の度に再生成されます。手で書き換えても次の記帳で"
    "上書きされます。地図を変えるには成果ログ側 (`beacon deliverable ...`) を操作して"
    "ください。\n"
)


def build_application_map_body(data: dict) -> str:
    """The generated doc body = banner + the dev-rendered derived map. Pure over
    ``data`` (delegates I/O-free rendering to ``deliverable_map``), so a test can
    assert the body without touching a store."""
    return _GENERATED_BANNER + "\n" + _dm.render_map(data, profession="dev")


def sync_application_map(data: dict) -> bool:
    """Regenerate the application-map doc from ``data``'s changelog, IF the project
    is dev and the doc already exists. Returns True if written, False on the no-op
    paths (non-dev / doc absent). I/O only through ``rewrite_document_body``; the
    caller has already persisted the changelog via ``save_project`` (this refreshes
    the derived VIEW, it does not persist the log)."""
    if _occ.resolve_profession(data) != "dev":
        return False
    if not get_store().get_document(APPLICATION_MAP_DOC_ID):
        return False
    return rewrite_document_body(APPLICATION_MAP_DOC_ID,
                                 build_application_map_body(data))
