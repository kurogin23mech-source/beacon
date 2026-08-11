"""Unit tests for occupation.kind_display_label (ms-143 e-5047).

The deadline surfaces (``beacon deadline due`` + scripts/session-start-deadlines.py)
used to each hardcode ``{"milestone": "MS", "task": "タスク", "activity": "活動"}``.
This resolver is the single declaration both read; a descriptor-defined occupation
declares its own label so a NEW occupation's kinds surface a display label with zero
wiring at the call sites. Built-in kinds stay byte-identical to the old map, and an
unlisted kind falls back to the kind string (= old ``label_jp.get(kind, kind)``).
"""
from __future__ import annotations

import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation  # noqa: E402


def test_builtin_labels_match_the_old_hardcoded_map():
    assert occupation.kind_display_label(None, "milestone") == "MS"
    assert occupation.kind_display_label(None, "task") == "タスク"
    assert occupation.kind_display_label(None, "activity") == "活動"


def test_unlisted_kind_falls_back_to_the_kind_string():
    # parity with label_jp.get(kind, kind): opportunity/account were never mapped.
    assert occupation.kind_display_label(None, "opportunity") == "opportunity"
    assert occupation.kind_display_label(None, "whatever") == "whatever"


def test_descriptor_kind_declares_its_own_label():
    data = {"profession": "legal", "target_classes": [
        {"kind": "contract", "display_label": "契約", "id_prefix": "ctr-",
         "collection": "contracts", "profession": "legal", "label": "Contracts"},
    ]}
    assert occupation.kind_display_label(data, "contract") == "契約"


def test_label_jp_alias_on_descriptor_is_accepted():
    data = {"profession": "legal", "target_classes": [
        {"kind": "matter", "label_jp": "案件", "id_prefix": "mat-",
         "collection": "matters", "profession": "legal", "label": "Matters"},
    ]}
    assert occupation.kind_display_label(data, "matter") == "案件"


def test_builtin_wins_over_data_and_no_data_is_safe():
    # a built-in kind resolves without consulting descriptors at all.
    data = {"target_classes": [{"kind": "milestone", "display_label": "上書き"}]}
    assert occupation.kind_display_label(data, "milestone") == "MS"
    # descriptor kind with no declared label falls back to the kind string.
    data2 = {"target_classes": [{"kind": "widget", "id_prefix": "w-"}]}
    assert occupation.kind_display_label(data2, "widget") == "widget"
