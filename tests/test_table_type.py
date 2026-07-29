"""Tests for lib/table_type.py — table-doc column type-checking (ms-131 e-4495).

Covers each column type (number / date / bool / ref / enum / text): valid values
pass (with coercion where defined), invalid values are rejected; column-def
validation; and that the checker actually engages on both write paths
(add_row and set_cell) once installed on the table_doc seam.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import table_doc as td  # noqa: E402
import table_type as tt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_seam():
    td.set_cell_validator(None)
    yield
    td.set_cell_validator(None)


def col(ctype, **extra):
    c = {"key": "c", "label": "C", "type": ctype}
    c.update(extra)
    return c


# ---------------------------------------------------------------------------
# number
# ---------------------------------------------------------------------------

def test_number_accepts_int_float_and_numeric_string():
    assert tt.validate_value(col("number"), 10) == 10
    assert tt.validate_value(col("number"), 3.5) == 3.5
    assert tt.validate_value(col("number"), "42") == 42
    assert tt.validate_value(col("number"), "3.14") == 3.14


def test_number_rejects_non_numeric_string_and_bool():
    with pytest.raises(td.TableDocError):
        tt.validate_value(col("number"), "abc")
    with pytest.raises(td.TableDocError):
        tt.validate_value(col("number"), True)  # bool is not a number


# ---------------------------------------------------------------------------
# date
# ---------------------------------------------------------------------------

def test_date_accepts_iso_date():
    assert tt.validate_value(col("date"), "2026-07-29") == "2026-07-29"


def test_date_rejects_bad_format_and_impossible_date():
    for bad in ["2026/07/29", "29-07-2026", "2026-13-01", "2026-02-30", "not-a-date"]:
        with pytest.raises(td.TableDocError):
            tt.validate_value(col("date"), bad)


# ---------------------------------------------------------------------------
# bool
# ---------------------------------------------------------------------------

def test_bool_accepts_and_coerces():
    assert tt.validate_value(col("bool"), True) is True
    assert tt.validate_value(col("bool"), "true") is True
    assert tt.validate_value(col("bool"), "No") is False
    assert tt.validate_value(col("bool"), 1) is True
    assert tt.validate_value(col("bool"), 0) is False


def test_bool_rejects_garbage():
    with pytest.raises(td.TableDocError):
        tt.validate_value(col("bool"), "maybe")
    with pytest.raises(td.TableDocError):
        tt.validate_value(col("bool"), 5)


# ---------------------------------------------------------------------------
# enum
# ---------------------------------------------------------------------------

def test_enum_accepts_allowed_and_rejects_outside():
    c = col("enum", values=["lead", "meeting", "won"])
    assert tt.validate_value(c, "meeting") == "meeting"
    with pytest.raises(td.TableDocError):
        tt.validate_value(c, "lost")


def test_enum_without_values_is_a_config_error():
    with pytest.raises(td.TableDocError):
        tt.validate_value(col("enum"), "x")


# ---------------------------------------------------------------------------
# ref
# ---------------------------------------------------------------------------

def test_ref_accepts_prefixed_id():
    assert tt.validate_value(col("ref"), "opp-3") == "opp-3"
    assert tt.validate_value(col("ref"), "acc-12") == "acc-12"


def test_ref_rejects_bad_shape():
    for bad in ["opp", "3", "-5", "opp 3", ""]:
        # "" is empty → passes (unset); test only the malformed non-empty ones.
        if bad == "":
            continue
        with pytest.raises(td.TableDocError):
            tt.validate_value(col("ref"), bad)


def test_ref_enforces_declared_prefix():
    c = col("ref", ref="opp")
    assert tt.validate_value(c, "opp-9") == "opp-9"
    with pytest.raises(td.TableDocError):
        tt.validate_value(c, "acc-9")  # prefix mismatch


# ---------------------------------------------------------------------------
# text / empty / unknown
# ---------------------------------------------------------------------------

def test_text_accepts_anything_and_stringifies():
    assert tt.validate_value(col("text"), "hello") == "hello"
    assert tt.validate_value(col("text"), 123) == "123"


def test_empty_always_passes_regardless_of_type():
    assert tt.validate_value(col("number"), None) is None
    assert tt.validate_value(col("date"), "") == ""
    assert tt.validate_value(col("ref"), None) is None


def test_unknown_type_is_lenient():
    assert tt.validate_value(col("mystery"), "x") == "x"


# ---------------------------------------------------------------------------
# column-definition validation
# ---------------------------------------------------------------------------

def test_validate_column_types_accepts_valid():
    tt.validate_column_types([
        {"key": "a", "type": "number"},
        {"key": "b", "type": "enum", "values": ["x", "y"]},
        {"key": "c", "type": "ref", "ref": "opp"},
        {"key": "d", "type": "text"},
    ])


def test_validate_column_types_rejects_unknown_type():
    with pytest.raises(td.TableDocError):
        tt.validate_column_types([{"key": "a", "type": "bogus"}])


def test_validate_column_types_rejects_enum_without_values():
    with pytest.raises(td.TableDocError):
        tt.validate_column_types([{"key": "a", "type": "enum"}])


# ---------------------------------------------------------------------------
# Integration: the seam engages on both write paths.
# ---------------------------------------------------------------------------

TYPED_COLUMNS = [
    {"key": "name", "label": "名前", "type": "text"},
    {"key": "phase", "label": "フェーズ", "type": "enum", "values": ["lead", "won"]},
    {"key": "amount", "label": "金額", "type": "number"},
    {"key": "due", "label": "期日", "type": "date"},
    {"key": "opp", "label": "商談", "type": "ref", "ref": "opp"},
]


def test_install_makes_add_row_type_check():
    tt.install()
    t = td.new_table(TYPED_COLUMNS)
    # Valid row passes and coerces the numeric string.
    rid = td.add_row(t, {"name": "Acme", "phase": "lead", "amount": "100",
                         "due": "2026-08-01", "opp": "opp-3"}, actor="a", at="T0")
    assert td.get_row(t, rid)["cells"]["amount"] == 100
    # Invalid enum is rejected at add_row.
    with pytest.raises(td.TableDocError):
        td.add_row(t, {"phase": "lost"}, actor="a", at="T1")


def test_install_makes_set_cell_type_check():
    tt.install()
    t = td.new_table(TYPED_COLUMNS)
    td.add_row(t, {"name": "Acme"}, actor="a", at="T0")
    with pytest.raises(td.TableDocError):
        td.set_cell(t, "r1", "due", "2026-99-99", actor="a", at="T1")
    # A valid update coerces and lands, with the old value preserved in history.
    td.set_cell(t, "r1", "amount", "250", actor="a", at="T2")
    row = td.get_row(t, "r1")
    assert row["cells"]["amount"] == 250
    assert row["history"][-1]["new"] == 250


def test_column_config_round_trips_through_serialize():
    t = td.new_table(TYPED_COLUMNS)
    content = "---\nformat: table\n---\n" + td.serialize_table_body("T", t)
    parsed = td.parse_table(content)
    enum_col = td.column_map(parsed)["phase"]
    assert enum_col["values"] == ["lead", "won"]
    ref_col = td.column_map(parsed)["opp"]
    assert ref_col["ref"] == "opp"
