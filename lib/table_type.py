"""Beacon table-doc column type-checking (ms-131 e-4495).

The core model (``table_doc``) stores cell values opaquely and routes every write
through one seam (``table_doc.set_cell_validator``). This module is the checker
that plugs into that seam: it validates — and where sensible, coerces — a cell
value against its column's declared type, so an add-row / set-cell can never
smuggle an ill-typed value (a non-date string into a ``date`` column, a value
outside an ``enum``'s allowed set, …) into the table.

Type vocabulary (SPEC ms-131 設計方針 2): the numeric / date / bool types reuse
the semantics ms-122 established for its data-defined descriptors
(``target_descriptor.ALLOWED_FIELD_TYPES``); ``ref`` / ``enum`` / ``text`` are the
table-specific additions. Kept deliberately generic — a table-doc is a *generic*
primitive that any Target can attach (設計方針 6), so ``ref`` checks the id
*shape* (and an optional declared prefix) rather than hard-coding Beacon's Target
taxonomy. Existence of the referenced entity is a separate, project-data concern
left to the caller (CLI / e-4497 linkage), not this pure value checker.

Empty is not a type error: ``None`` and ``""`` mean "unset" and always pass — a
row need not fill every column, and "this column is required" is a separate
policy, not a type check.

Install with ``table_type.install()`` (sets the seam) or call ``validate_value``
directly. All failures raise ``table_doc.TableDocError`` so callers catch one
exception type across the model and the type layer.
"""

from __future__ import annotations

import datetime
import re

from table_doc import TableDocError

# Column types this layer understands. ``text`` is the default / catch-all.
TEXT = "text"
NUMBER = "number"
DATE = "date"
BOOL = "bool"
REF = "ref"
ENUM = "enum"
VALID_COLUMN_TYPES = (TEXT, NUMBER, DATE, BOOL, REF, ENUM)

# A reference id is ``<prefix>-<rest>`` (e.g. ``opp-3``, ``acc-12``, ``ctr-7``).
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9_]+$")

_TRUE_STRINGS = {"true", "1", "yes", "y"}
_FALSE_STRINGS = {"false", "0", "no", "n"}


def _is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


# ---------------------------------------------------------------------------
# Per-type value checks. Each returns the (possibly coerced) value or raises.
# ---------------------------------------------------------------------------

def _check_number(column, value):
    # bool is a subclass of int in Python; a checkbox value is not a number.
    if isinstance(value, bool):
        raise TableDocError(f"列 '{column['key']}' は number 型です: 真偽値は不可")
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
    raise TableDocError(f"列 '{column['key']}' は number 型です: 数値でない値 {value!r}")


def _check_date(column, value):
    if not isinstance(value, str):
        raise TableDocError(f"列 '{column['key']}' は date 型です: YYYY-MM-DD 文字列が必要 ({value!r})")
    text = value.strip()
    try:
        # Rejects both bad format and impossible dates (e.g. 2026-13-40).
        datetime.date.fromisoformat(text)
    except ValueError:
        raise TableDocError(
            f"列 '{column['key']}' は date 型です: YYYY-MM-DD 形式の実在日付が必要 ({value!r})")
    return text


def _check_bool(column, value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRINGS:
            return True
        if low in _FALSE_STRINGS:
            return False
    raise TableDocError(f"列 '{column['key']}' は bool 型です: 真偽に解釈できない値 {value!r}")


def _check_enum(column, value):
    allowed = column.get("values")
    if not isinstance(allowed, list) or not allowed:
        raise TableDocError(
            f"enum 列 '{column['key']}' に許可値集合 (values) が定義されていません")
    allowed_str = [str(a) for a in allowed]
    if str(value) not in allowed_str:
        raise TableDocError(
            f"列 '{column['key']}' は enum 型です: 許可値 {allowed_str} 以外の {value!r}")
    return value


def _check_ref(column, value):
    if not isinstance(value, str) or not _REF_RE.match(value.strip()):
        raise TableDocError(
            f"列 '{column['key']}' は ref 型です: '<prefix>-<id>' 形式の参照 id が必要 ({value!r})")
    ref = value.strip()
    # An optional declared prefix pins which id-space the column references
    # (``{type: ref, ref: opp}`` → only ``opp-*`` values). Absent = any shape OK.
    expected = column.get("ref")
    if expected:
        prefix = ref.split("-", 1)[0]
        if prefix != str(expected):
            raise TableDocError(
                f"列 '{column['key']}' は {expected}-* を参照します: prefix 不一致の {value!r}")
    return ref


_CHECKERS = {
    NUMBER: _check_number,
    DATE: _check_date,
    BOOL: _check_bool,
    ENUM: _check_enum,
    REF: _check_ref,
}


def validate_value(column: dict, value):
    """Validate/coerce ``value`` against ``column``'s declared type.

    Returns the (possibly coerced) value, or raises ``TableDocError``. ``text``
    and unknown types accept anything (coerced to ``str`` when not already a
    scalar-friendly type). Empty (None / "") always passes — see module docstring.
    """
    if _is_empty(value):
        return value
    ctype = (column.get("type") or TEXT).strip() or TEXT
    checker = _CHECKERS.get(ctype)
    if checker is None:
        # text / unknown: accept as-is, normalizing non-str scalars to text.
        return value if isinstance(value, str) else str(value)
    return checker(column, value)


# ---------------------------------------------------------------------------
# Column-definition validation (run at table creation — e-4496 wires this).
# ---------------------------------------------------------------------------

def validate_column_types(columns) -> None:
    """Reject structurally-invalid typed columns before a table is created.

    An unknown type name or an enum column missing its ``values`` set is a
    definition error the user should hear at creation time, not on first
    add-row. Raises ``TableDocError`` on the first problem.
    """
    for col in columns:
        ctype = (col.get("type") or TEXT).strip() or TEXT
        if ctype not in VALID_COLUMN_TYPES:
            raise TableDocError(
                f"列 '{col.get('key')}' の type '{ctype}' は未知です "
                f"(有効: {' / '.join(VALID_COLUMN_TYPES)})")
        if ctype == ENUM:
            allowed = col.get("values")
            if not isinstance(allowed, list) or not allowed:
                raise TableDocError(
                    f"enum 列 '{col.get('key')}' には values (許可値集合) が必要です")


# ---------------------------------------------------------------------------
# Seam wiring.
# ---------------------------------------------------------------------------

def install() -> None:
    """Route table_doc's add-row / set-cell writes through this type checker."""
    import table_doc
    table_doc.set_cell_validator(validate_value)


def uninstall() -> None:
    """Restore the core structural-only default (mainly for tests)."""
    import table_doc
    table_doc.set_cell_validator(None)
