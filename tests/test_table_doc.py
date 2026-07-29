"""Tests for lib/table_doc.py — the table-doc core data model (ms-131 e-4494).

Covers: format detection & backward compat (plain markdown untouched), column
normalization, row operations with append-only history (add-row / set-cell /
soft-delete rm-row), the cell-value validator seam (where ms-131 e-4495 hooks
type-checking), and serialize <-> parse round-trip.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import table_doc as td  # noqa: E402


COLUMNS = [
    {"key": "name", "label": "名前", "type": "text"},
    {"key": "phase", "label": "フェーズ", "type": "enum"},
    {"key": "amount", "label": "金額", "type": "number"},
]


@pytest.fixture(autouse=True)
def _reset_validator():
    """Each test starts with the structural-only default validator."""
    td.set_cell_validator(None)
    yield
    td.set_cell_validator(None)


# ---------------------------------------------------------------------------
# Format detection & backward compat (SPEC 受入条件 1 / 8).
# ---------------------------------------------------------------------------

def test_plain_markdown_is_not_a_table():
    plain = "---\nscope: memo\ntarget: ms-1\n---\n# タイトル\n\n本文です。\n"
    assert td.is_table_content(plain) is False
    with pytest.raises(td.TableDocError):
        td.parse_table(plain)


def test_no_frontmatter_is_not_a_table():
    assert td.is_table_content("# just markdown\n\nno frontmatter") is False
    assert td.is_table_content("") is False


def test_format_table_is_detected():
    content = "---\nscope: memo\nformat: table\n---\n# T\n\n```beacon-table\n{}\n```\n"
    assert td.is_table_content(content) is True


def test_explicit_format_markdown_is_not_a_table():
    content = "---\nformat: markdown\nscope: memo\n---\n# T\n\nbody\n"
    assert td.is_table_content(content) is False


# ---------------------------------------------------------------------------
# Column normalization.
# ---------------------------------------------------------------------------

def test_new_table_normalizes_columns():
    t = td.new_table([{"key": "name"}])
    assert t["columns"] == [{"key": "name", "label": "name", "type": "text"}]
    assert t["rows"] == []


def test_columns_require_at_least_one():
    with pytest.raises(td.TableDocError):
        td.new_table([])


def test_column_key_required():
    with pytest.raises(td.TableDocError):
        td.new_table([{"label": "no key"}])


def test_duplicate_column_key_rejected():
    with pytest.raises(td.TableDocError):
        td.new_table([{"key": "a"}, {"key": "a"}])


def test_column_helpers():
    t = td.new_table(COLUMNS)
    assert td.column_keys(t) == ["name", "phase", "amount"]
    assert td.column_map(t)["phase"]["label"] == "フェーズ"


# ---------------------------------------------------------------------------
# add_row + history seed.
# ---------------------------------------------------------------------------

def test_add_row_returns_id_and_seeds_history():
    t = td.new_table(COLUMNS)
    rid = td.add_row(t, {"name": "Acme", "phase": "lead"}, actor="claude", at="T0")
    assert rid == "r1"
    row = td.get_row(t, "r1")
    assert row["cells"] == {"name": "Acme", "phase": "lead"}
    assert len(row["history"]) == 1
    h = row["history"][0]
    assert h["op"] == td.OP_ADD_ROW
    assert h["actor"] == "claude"
    assert h["at"] == "T0"
    assert h["cells"] == {"name": "Acme", "phase": "lead"}


def test_add_row_rejects_unknown_column():
    t = td.new_table(COLUMNS)
    with pytest.raises(td.TableDocError):
        td.add_row(t, {"nope": "x"}, actor="a", at="T0")


def test_add_row_ids_increment_deterministically():
    t = td.new_table(COLUMNS)
    assert td.add_row(t, {"name": "a"}, actor="a", at="T0") == "r1"
    assert td.add_row(t, {"name": "b"}, actor="a", at="T1") == "r2"


def test_explicit_row_id_and_collision():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "a"}, actor="a", at="T0", row_id="custom")
    with pytest.raises(td.TableDocError):
        td.add_row(t, {"name": "b"}, actor="a", at="T1", row_id="custom")


# ---------------------------------------------------------------------------
# set_cell preserves the old value in history (append-only).
# ---------------------------------------------------------------------------

def test_set_cell_appends_history_and_preserves_old():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "Acme", "phase": "lead"}, actor="a", at="T0")
    td.set_cell(t, "r1", "phase", "meeting", actor="b", at="T1")
    row = td.get_row(t, "r1")
    assert row["cells"]["phase"] == "meeting"
    assert len(row["history"]) == 2
    h = row["history"][-1]
    assert h == {"at": "T1", "actor": "b", "op": td.OP_SET_CELL,
                 "key": "phase", "old": "lead", "new": "meeting"}


def test_set_cell_old_value_none_when_unset():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "Acme"}, actor="a", at="T0")
    td.set_cell(t, "r1", "amount", 100, actor="a", at="T1")
    assert td.get_row(t, "r1")["history"][-1]["old"] is None


def test_set_cell_unknown_column_rejected():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "Acme"}, actor="a", at="T0")
    with pytest.raises(td.TableDocError):
        td.set_cell(t, "r1", "ghost", "x", actor="a", at="T1")


def test_set_cell_missing_row_raises():
    t = td.new_table(COLUMNS)
    with pytest.raises(td.TableDocError):
        td.set_cell(t, "nope", "name", "x", actor="a", at="T0")


# ---------------------------------------------------------------------------
# rm_row is a soft delete (tombstone) — audit trail survives.
# ---------------------------------------------------------------------------

def test_rm_row_soft_deletes_and_keeps_history():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "a"}, actor="x", at="T0")
    td.add_row(t, {"name": "b"}, actor="x", at="T1")
    td.rm_row(t, "r1", actor="x", at="T2")
    # Row still present in storage (tombstoned) but excluded from active view.
    assert len(t["rows"]) == 2
    assert [r["id"] for r in td.active_rows(t)] == ["r2"]
    removed = td.get_row(t, "r1")
    assert removed["removed_at"] == "T2"
    assert removed["removed_by"] == "x"
    assert removed["history"][-1]["op"] == td.OP_RM_ROW


def test_removed_row_id_not_reused():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "a"}, actor="x", at="T0")
    td.rm_row(t, "r1", actor="x", at="T1")
    assert td.add_row(t, {"name": "b"}, actor="x", at="T2") == "r2"


def test_set_cell_on_removed_row_refused():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "a"}, actor="x", at="T0")
    td.rm_row(t, "r1", actor="x", at="T1")
    with pytest.raises(td.TableDocError):
        td.set_cell(t, "r1", "name", "b", actor="x", at="T2")


def test_double_rm_row_refused():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "a"}, actor="x", at="T0")
    td.rm_row(t, "r1", actor="x", at="T1")
    with pytest.raises(td.TableDocError):
        td.rm_row(t, "r1", actor="x", at="T2")


# ---------------------------------------------------------------------------
# Cell-value validator seam (ms-131 e-4495 hooks here).
# ---------------------------------------------------------------------------

def test_validator_seam_can_reject_values():
    def reject_non_numeric(column, value):
        if column["type"] == "number" and not isinstance(value, (int, float)):
            raise td.TableDocError("number 型に非数値")
        return value

    td.set_cell_validator(reject_non_numeric)
    t = td.new_table(COLUMNS)
    with pytest.raises(td.TableDocError):
        td.add_row(t, {"amount": "not-a-number"}, actor="a", at="T0")


def test_validator_seam_can_coerce_values():
    td.set_cell_validator(lambda column, value: str(value).upper())
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "acme"}, actor="a", at="T0")
    assert td.get_row(t, "r1")["cells"]["name"] == "ACME"


# ---------------------------------------------------------------------------
# Serialize <-> parse round-trip.
# ---------------------------------------------------------------------------

def _wrap(body: str) -> str:
    """Assemble a full table-doc content the way the write path will."""
    return "---\nscope: memo\nformat: table\n---\n" + body


def test_serialize_parse_round_trip():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "Acme", "phase": "lead", "amount": 100},
               actor="claude", at="T0")
    td.set_cell(t, "r1", "phase", "meeting", actor="claude", at="T1")
    td.rm_row(t, "r1", actor="claude", at="T2")
    td.add_row(t, {"name": "Beta"}, actor="claude", at="T3")

    content = _wrap(td.serialize_table_body("顧客リスト", t))
    parsed = td.parse_table(content)

    assert parsed["columns"] == t["columns"]
    assert parsed["rows"] == t["rows"]


def test_serialize_body_keeps_title_heading_first():
    t = td.new_table(COLUMNS)
    body = td.serialize_table_body("My Table", t)
    # Local title extraction reads the first ``# `` line, so it must lead.
    assert body.splitlines()[0] == "# My Table"
    assert "```beacon-table" in body


def test_parse_rejects_malformed_json():
    content = _wrap("# T\n\n```beacon-table\n{not json}\n```\n")
    with pytest.raises(td.TableDocError):
        td.parse_table(content)


def test_parse_rejects_missing_block():
    content = _wrap("# T\n\njust prose, no table block\n")
    with pytest.raises(td.TableDocError):
        td.parse_table(content)


def test_parse_rejects_unterminated_block():
    content = _wrap("# T\n\n```beacon-table\n{}\n")
    with pytest.raises(td.TableDocError):
        td.parse_table(content)


def test_parse_renormalizes_columns_from_hand_edit():
    # A hand-edited file smuggling a duplicate column key must be rejected.
    content = _wrap('# T\n\n```beacon-table\n'
                    '{"columns": [{"key": "a"}, {"key": "a"}], "rows": []}\n```\n')
    with pytest.raises(td.TableDocError):
        td.parse_table(content)


# ---------------------------------------------------------------------------
# Rendering (used by ``show`` — ms-131 e-4496).
# ---------------------------------------------------------------------------

def test_render_table_shows_active_rows_only():
    t = td.new_table(COLUMNS)
    td.add_row(t, {"name": "Acme", "phase": "lead", "amount": 100}, actor="a", at="T0")
    td.add_row(t, {"name": "Beta", "phase": "won", "amount": 200}, actor="a", at="T1")
    td.rm_row(t, "r2", actor="a", at="T2")
    rendered = td.render_table(t)
    assert "名前" in rendered and "フェーズ" in rendered
    assert "Acme" in rendered
    assert "Beta" not in rendered  # tombstoned row excluded
