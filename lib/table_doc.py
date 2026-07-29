"""Beacon table-doc core data model (ms-131 e-4494).

A *table-doc* is a Beacon document whose payload is a typed table — columns,
rows, and a per-row append-only history — instead of free-form markdown. This
module is the single source of truth for that structure: it parses a document's
content into a table model, mutates the model (add-row / set-cell / rm-row) while
preserving history, and serializes the model back to document content.

Design (SPEC ms-131, doc WJPqjy7D6PrZQqdMZqT8):

1. ``table`` is a *format* attribute, not a scope. It lives in the document
   frontmatter as ``format: table`` (markdown-scoped docs simply omit it, so the
   default stays markdown and pre-existing docs are byte-for-byte unchanged). The
   scope axis (core / spec / memo / …) is orthogonal — a doc can be a spec *and* a
   table.
2. Each table-doc carries its own column definitions (``columns: [{key, label,
   type}]``). The table is self-describing; it does not depend on an external
   schema. Value type-checking (number/date/bool/ref/enum/text) is a separate
   concern layered on top (ms-131 e-4495) via the ``validate_cell`` seam below —
   this module keeps a structural-only default so the core model stays usable on
   its own.
3. Rows are structured and every row keeps an append-only history. ``set_cell``
   never overwrites a past value; it records ``{old, new}`` in that row's history
   (mirrors the商談 phase_history / commit log — CORE doc
   ``data-immutability-principle``). ``rm_row`` is a soft delete (tombstone) for
   the same reason: nothing is silently destroyed, the audit trail survives, and
   readers filter tombstoned rows via ``active_rows``.

The on-disk shape is a fenced ``beacon-table`` code block placed after the
document's ``# title`` heading, so the local title-extraction path
(first ``# `` line in the body) keeps working and a human viewing the raw file
still sees a delimited, machine-readable payload::

    ---
    scope: memo
    format: table
    ---
    # 顧客リスト

    ```beacon-table
    {"columns": [...], "rows": [...]}
    ```

Timestamps and actors are passed in by callers (never read from the clock here)
so every operation is pure and deterministic for tests, matching ``core.save_entry``.
"""

from __future__ import annotations

import json

# Frontmatter discriminator: a doc is a table-doc iff ``format`` == this value.
TABLE_FORMAT = "table"
FORMAT_MARKDOWN = "markdown"

# Info string of the fenced code block that holds the table payload in the body.
_FENCE_TAG = "beacon-table"
_FENCE_OPEN = "```" + _FENCE_TAG
_FENCE_CLOSE = "```"

# History operation kinds (append-only; never mutated in place).
OP_ADD_ROW = "add_row"
OP_SET_CELL = "set_cell"
OP_RM_ROW = "rm_row"


class TableDocError(ValueError):
    """Raised on malformed table structure or an invalid table operation."""


# ---------------------------------------------------------------------------
# Frontmatter format detection (minimal, dependency-free).
# ---------------------------------------------------------------------------

def _frontmatter_format(content: str) -> str:
    """Return the ``format`` frontmatter value, or ``markdown`` when absent.

    Inlined single-line parse (same contract as ``store_local._read_frontmatter``)
    to avoid importing ``commands`` and creating a circular dependency. Only the
    ``format`` key is needed here, so a full YAML parse is unnecessary.
    """
    if not content.startswith("---"):
        return FORMAT_MARKDOWN
    end = content.find("\n---", 3)
    if end == -1:
        return FORMAT_MARKDOWN
    header = content[4:end]
    for line in header.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        if key.strip() == "format":
            return val.strip() or FORMAT_MARKDOWN
    return FORMAT_MARKDOWN


def is_table_content(content: str) -> bool:
    """True iff the document's frontmatter declares ``format: table``.

    Plain markdown docs (the default) return False, so every table-doc code path
    is a no-op on them — the backward-compat contract (SPEC 受入条件 1 / 8).
    """
    return _frontmatter_format(content or "") == TABLE_FORMAT


# ---------------------------------------------------------------------------
# Column definitions.
# ---------------------------------------------------------------------------

def normalize_columns(columns) -> list:
    """Validate and normalize a column-definition list.

    Each column is ``{key, label, type}``; ``key`` is required and unique,
    ``label`` defaults to ``key``, ``type`` defaults to ``text``. Structural only
    — whether a *cell value* satisfies its column ``type`` is enforced by the
    type-checking layer (ms-131 e-4495) through ``validate_cell``.
    """
    if not isinstance(columns, list) or not columns:
        raise TableDocError("table には最低 1 列の定義が必要です (columns)")
    seen = set()
    normalized = []
    for i, col in enumerate(columns):
        if not isinstance(col, dict):
            raise TableDocError(f"列定義 [{i}] は object である必要があります")
        key = str(col.get("key", "")).strip()
        if not key:
            raise TableDocError(f"列定義 [{i}] に 'key' が必要です")
        if key in seen:
            raise TableDocError(f"列 key が重複しています: {key!r}")
        seen.add(key)
        ctype = str(col.get("type", "text")).strip() or "text"
        out = {
            "key": key,
            "label": str(col.get("label", "") or key),
            "type": ctype,
        }
        # Preserve any extra column config beyond the core triple so typed
        # columns can carry their own settings — e.g. ``values`` (the allowed
        # set for an enum) or ``ref`` (the referenced entity kind). The core
        # model stores them opaquely; the type-checking layer (ms-131 e-4495)
        # interprets them. Forward-compatible: unknown keys round-trip untouched.
        for k, v in col.items():
            if k not in out:
                out[k] = v
        normalized.append(out)
    return normalized


def column_keys(table: dict) -> list:
    """The ordered list of column keys."""
    return [c["key"] for c in table.get("columns", [])]


def column_map(table: dict) -> dict:
    """Map of column key -> column definition."""
    return {c["key"]: c for c in table.get("columns", [])}


# ---------------------------------------------------------------------------
# Cell-value validation seam.
#
# The core model does not type-check cell *values* — it only rejects unknown
# column keys. ms-131 e-4495 replaces ``validate_cell`` (or sets
# ``_CELL_VALIDATOR``) to enforce number/date/bool/ref/enum/text. Keeping the
# hook here means add_row / set_cell route every write through one checkpoint, so
# the type layer wires in without re-plumbing the operations.
# ---------------------------------------------------------------------------

_CELL_VALIDATOR = None  # set by the type-checking layer; signature (column, value)


def set_cell_validator(fn) -> None:
    """Install the cell-value validator used by add_row / set_cell.

    ``fn(column: dict, value) -> value`` returns the (possibly coerced) value or
    raises ``TableDocError`` for an invalid value. Passing ``None`` restores the
    structural-only default. Provided so ms-131 e-4495 can layer type-checking
    onto the single write checkpoint without touching the operations.
    """
    global _CELL_VALIDATOR
    _CELL_VALIDATOR = fn


def validate_cell(column: dict, value):
    """Validate/coerce one cell value against its column. Structural default."""
    if _CELL_VALIDATOR is not None:
        return _CELL_VALIDATOR(column, value)
    return value


def _check_cells(table: dict, cells: dict) -> dict:
    """Reject unknown column keys and run each value through ``validate_cell``.

    Returns the (possibly coerced) cell dict. Absent columns are allowed (a row
    need not fill every column); unknown keys are a hard error so typos surface.
    """
    if not isinstance(cells, dict):
        raise TableDocError("cells は object である必要があります")
    cols = column_map(table)
    out = {}
    for key, value in cells.items():
        if key not in cols:
            raise TableDocError(f"未知の列です: {key!r} (定義済: {', '.join(cols) or '(なし)'})")
        out[key] = validate_cell(cols[key], value)
    return out


# ---------------------------------------------------------------------------
# Model construction and row lookup.
# ---------------------------------------------------------------------------

def new_table(columns) -> dict:
    """Build a fresh, empty table model from a column-definition list."""
    return {"columns": normalize_columns(columns), "rows": []}


def active_rows(table: dict) -> list:
    """Rows that are not tombstoned (the rows ``show`` renders)."""
    return [r for r in table.get("rows", []) if not r.get("removed_at")]


def get_row(table: dict, row_id: str) -> dict:
    """Return the row with ``row_id`` (including a tombstoned one), or raise."""
    for row in table.get("rows", []):
        if row.get("id") == row_id:
            return row
    raise TableDocError(f"行が見つかりません: {row_id}")


def _next_row_id(table: dict) -> str:
    """Deterministic ``r{N}`` id: max existing numeric suffix + 1 (no randomness).

    Counts tombstoned rows too, so a removed row's id is never reused (the audit
    trail stays unambiguous).
    """
    max_n = 0
    for row in table.get("rows", []):
        rid = str(row.get("id", ""))
        if rid.startswith("r") and rid[1:].isdigit():
            max_n = max(max_n, int(rid[1:]))
    return f"r{max_n + 1}"


# ---------------------------------------------------------------------------
# Row operations — each records append-only history.
# ---------------------------------------------------------------------------

def add_row(table: dict, cells: dict, *, actor: str, at: str,
            row_id: str | None = None) -> str:
    """Append a row and seed its history with an ``add_row`` entry.

    ``actor`` / ``at`` are recorded verbatim (callers pass the clock — the model
    stays pure). Returns the new row id.
    """
    checked = _check_cells(table, cells)
    rid = row_id or _next_row_id(table)
    if any(r.get("id") == rid for r in table.get("rows", [])):
        raise TableDocError(f"行 id が重複しています: {rid}")
    table.setdefault("rows", []).append({
        "id": rid,
        "cells": checked,
        "history": [{
            "at": at,
            "actor": actor,
            "op": OP_ADD_ROW,
            "cells": dict(checked),
        }],
    })
    return rid


def set_cell(table: dict, row_id: str, key: str, value, *,
             actor: str, at: str) -> None:
    """Update one cell, appending ``{old, new}`` to that row's history.

    The previous value is preserved in history (never overwritten in place) —
    SPEC 設計方針 3 / CORE doc ``data-immutability-principle``. Updating a
    tombstoned row is refused.
    """
    row = get_row(table, row_id)
    if row.get("removed_at"):
        raise TableDocError(f"削除済みの行は更新できません: {row_id}")
    cols = column_map(table)
    if key not in cols:
        raise TableDocError(f"未知の列です: {key!r} (定義済: {', '.join(cols) or '(なし)'})")
    new_value = validate_cell(cols[key], value)
    old_value = row.get("cells", {}).get(key)
    row.setdefault("cells", {})[key] = new_value
    row.setdefault("history", []).append({
        "at": at,
        "actor": actor,
        "op": OP_SET_CELL,
        "key": key,
        "old": old_value,
        "new": new_value,
    })


def rm_row(table: dict, row_id: str, *, actor: str, at: str) -> None:
    """Soft-delete a row (tombstone), appending an ``rm_row`` history entry.

    A hard delete would violate ``data-immutability-principle`` (state changes
    must stay traceable), so the row is marked ``removed_at`` / ``removed_by`` and
    excluded from ``active_rows`` rather than dropped. Removing an
    already-removed row is a no-op error.
    """
    row = get_row(table, row_id)
    if row.get("removed_at"):
        raise TableDocError(f"既に削除済みの行です: {row_id}")
    row["removed_at"] = at
    row["removed_by"] = actor
    row.setdefault("history", []).append({
        "at": at,
        "actor": actor,
        "op": OP_RM_ROW,
    })


# ---------------------------------------------------------------------------
# Serialization: model <-> document body.
# ---------------------------------------------------------------------------

def build_table_block(table: dict) -> str:
    """Serialize a table model into a fenced ``beacon-table`` JSON block."""
    payload = {
        "columns": table.get("columns", []),
        "rows": table.get("rows", []),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"{_FENCE_OPEN}\n{body}\n{_FENCE_CLOSE}"


def serialize_table_body(title: str, table: dict) -> str:
    """Build the document body: ``# title`` heading + the table block.

    Frontmatter (including ``format: table``) is added by the caller's document
    write path (``commands._add_frontmatter``, wired in ms-131 e-4496); this
    returns only the body so the two layers stay separated.
    """
    heading = f"# {title.strip()}" if title and title.strip() else "# (untitled)"
    return f"{heading}\n\n{build_table_block(table)}\n"


def _extract_block(content: str) -> str:
    """Return the raw JSON text inside the fenced ``beacon-table`` block."""
    lines = content.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == _FENCE_OPEN:
            start = i + 1
            break
    if start is None:
        raise TableDocError("beacon-table ブロックが本文に見つかりません")
    collected = []
    for line in lines[start:]:
        if line.strip() == _FENCE_CLOSE:
            return "\n".join(collected)
        collected.append(line)
    raise TableDocError("beacon-table ブロックが閉じられていません (``` が不足)")


def parse_table(content: str) -> dict:
    """Parse a table-doc's full content into a normalized table model.

    Raises ``TableDocError`` when the content is not a table-doc (``format`` is
    not ``table``) or the payload is malformed. Column definitions are
    re-normalized on read so a hand-edited file can't smuggle in a duplicate /
    keyless column.
    """
    if not is_table_content(content):
        raise TableDocError("このドキュメントは format: table ではありません")
    raw = _extract_block(content)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise TableDocError(f"beacon-table ペイロードが不正な JSON です: {exc}")
    if not isinstance(payload, dict):
        raise TableDocError("beacon-table ペイロードは object である必要があります")
    columns = normalize_columns(payload.get("columns"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        raise TableDocError("rows は list である必要があります")
    return {"columns": columns, "rows": rows}


# ---------------------------------------------------------------------------
# Human-readable rendering (used by ``show`` — ms-131 e-4496).
# ---------------------------------------------------------------------------

def render_table(table: dict) -> str:
    """Render active rows as a GitHub-flavored markdown table (for ``show``)."""
    cols = table.get("columns", [])
    if not cols:
        return "(空のテーブル)"
    header = "| " + " | ".join(c.get("label") or c["key"] for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    lines = [header, sep]
    for row in active_rows(table):
        cells = row.get("cells", {})
        rendered = []
        for c in cols:
            val = cells.get(c["key"], "")
            rendered.append("" if val is None else str(val))
        lines.append("| " + " | ".join(rendered) + " |")
    return "\n".join(lines)
