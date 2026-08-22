"""Pure v3 (item-level) decompose/assemble for the local SQLite store.

ms-148 e-5411. The server's MySQL backend already splits a project document into
item-level rows — meta, one row per milestone (without its entries), one row per
entry (with its own children inlined) — under the generic ``(pk, sk, data)``
shape. That v3 layout is the *sole reference implementation* of the semantics
(SPEC 方針2), and the local SQLite store must match it so a future local→cloud
move is a same-shape transfer, not another conversion.

Rather than import ``server/mysql_client`` (which pulls in pymysql and lives in
the server layer), this module reimplements the two pure functions the local
store needs — ``decompose`` and ``assemble`` — with no MySQL dependency. Their
equivalence to the server's ``_v3_decompose`` / ``_v3_assemble`` is pinned by
``tests/test_v3_schema_equivalence.py`` so the two copies can never drift.

Row layout (mirrors the server's pk/sk convention):
  * meta      — the project document minus ``milestones``, stamped
                ``schema_version=3``.
  * milestone — one row per milestone, keyed by ``ms_id``, WITHOUT its entries.
  * entry     — one row per entry, keyed ``"{ms_id}#{entry_id}"``, WITH any
                deeper children (e.g. commits under a task) left inline in the
                entry's own ``entries`` list — so an entry write stays a single
                row update (e-5412 item-level updates build on this).
"""

from __future__ import annotations

import re

SCHEMA_V3_ENTRY = 3


def _entry_sort_key(entry: dict) -> tuple:
    """Order entries within a milestone by (created_at, id).

    v3 stores each entry as its own row, so array order is no longer implicit —
    it is recomputed on assemble. Entries with a ``created_at`` come first in
    ascending order; entries without one sort to the end; ties break on entry id.
    Matches the server's ``_v3_entry_sort_key``.
    """
    ts = entry.get("created_at", "") or ""
    eid = entry.get("id", "") or ""
    return (ts or "￿", eid)


def _ms_sort_key(ms: dict):
    """Numeric ms-N ordering (ms-1, ms-2, ...), matching the server assemble."""
    mid = str(ms.get("id", ""))
    m = re.match(r"ms-(\d+)$", mid)
    return (0, int(m.group(1))) if m else (1, mid)


def decompose(data: dict) -> tuple[dict, dict, dict]:
    """Split a unified project dict into ``(meta, ms_map, entry_map)``.

    meta:      project meta minus ``milestones`` (schema_version stamped to 3).
    ms_map:    ``{milestone_id: milestone_dict_without_entries}``
    entry_map: ``{"{ms_id}#{entry_id}": entry_dict_with_children_inline}``

    Mirrors ``server/mysql_client._v3_decompose``.
    """
    meta = {k: v for k, v in (data or {}).items() if k != "milestones"}
    meta["schema_version"] = SCHEMA_V3_ENTRY

    ms_map: dict = {}
    entry_map: dict = {}
    for ms in (data or {}).get("milestones", []) or []:
        ms_id = ms.get("id", "")
        if not ms_id:
            continue
        ms_map[ms_id] = {k: v for k, v in ms.items() if k != "entries"}
        for entry in ms.get("entries", []) or []:
            entry_id = entry.get("id", "")
            if not entry_id:
                continue
            entry_map[f"{ms_id}#{entry_id}"] = dict(entry)
    return meta, ms_map, entry_map


def assemble(meta: dict, ms_rows: list[tuple[str, dict]],
             entry_rows: list[tuple[str, dict]]) -> dict:
    """Rebuild a unified project dict from meta + milestone rows + entry rows.

    ms_rows:    ``[(milestone_id, ms_data_without_entries), ...]``
    entry_rows: ``[(sk="{ms_id}#{entry_id}", entry_data_with_children), ...]``

    Inverse of :func:`decompose`; mirrors ``server/mysql_client._v3_assemble``.
    Any stray ``milestones`` key on ``meta`` or ``entries`` key on a milestone
    row is dropped defensively so residue can't shadow the reassembled arms.
    """
    result = {k: v for k, v in (meta or {}).items() if k != "milestones"}

    entries_by_ms: dict[str, list[dict]] = {}
    for sk, entry_data in entry_rows:
        ms_id, _, _entry_id = sk.partition("#")
        if not ms_id:
            continue
        entries_by_ms.setdefault(ms_id, []).append(entry_data)

    milestones = []
    for ms_id, ms_data in ms_rows:
        ms_dict = dict(ms_data or {})
        ms_dict.pop("entries", None)
        children = entries_by_ms.get(ms_id, [])
        children.sort(key=_entry_sort_key)
        ms_dict["entries"] = children
        milestones.append(ms_dict)

    milestones.sort(key=_ms_sort_key)
    result["milestones"] = milestones
    return result
