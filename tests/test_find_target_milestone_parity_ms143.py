"""Parity pin for ms-143: find_target_milestone's dev-specific RESOLUTION is
unchanged after its milestone-record access was routed through
occupation.target_records (concrete-literal removed, leader 握り).

find_target_milestone は shared helper (task_add / task_list / log_finalize が
resolve に使う)。ms-143 は『milestone レコード集合の取得』だけを
occupation.target_records 経由に付け替え、dev 固有解決 —
  (i) ms_id 指定 → その MS
  (ii) ms_id 空 → 単一 active MS 自動選択 (0 個 / 複数はエラー)
  (iii) duplicate id → --index 要求、範囲外はエラー
— は温存する。この harness がその不変を固定する (乖離 = 抽象化が resolution を
壊した、で報告対象)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import core  # noqa: E402


def _proj(milestones):
    return {"id": "p", "profession": "dev", "milestones": milestones}


def test_resolve_by_id():
    data = _proj([{"id": "ms-1", "status": "todo"},
                  {"id": "ms-2", "status": "in_progress"}])
    assert core.find_target_milestone(data, "ms-2")["id"] == "ms-2"


def test_id_not_found_raises():
    with pytest.raises(ValueError, match="not found"):
        core.find_target_milestone(_proj([{"id": "ms-1", "status": "todo"}]), "ms-9")


def test_empty_id_autoselects_single_active():
    data = _proj([{"id": "ms-1", "status": "done"},
                  {"id": "ms-2", "status": "in_progress"}])
    assert core.find_target_milestone(data, "")["id"] == "ms-2"


def test_empty_id_zero_active_raises():
    data = _proj([{"id": "ms-1", "status": "done"}])
    with pytest.raises(ValueError, match="No active milestone"):
        core.find_target_milestone(data, "")


def test_empty_id_multiple_active_raises():
    data = _proj([{"id": "ms-1", "status": "in_progress"},
                  {"id": "ms-2", "status": "in_progress"}])
    with pytest.raises(ValueError, match="Multiple active"):
        core.find_target_milestone(data, "")


def test_duplicate_id_without_index_raises():
    data = _proj([{"id": "ms-1", "status": "todo", "title": "a"},
                  {"id": "ms-1", "status": "todo", "title": "b"}])
    with pytest.raises(ValueError, match="Ambiguous"):
        core.find_target_milestone(data, "ms-1")


def test_duplicate_id_with_index_selects():
    data = _proj([{"id": "ms-1", "status": "todo", "title": "a"},
                  {"id": "ms-1", "status": "todo", "title": "b"}])
    assert core.find_target_milestone(data, "ms-1", index=2)["title"] == "b"


def test_duplicate_id_index_out_of_range_raises():
    data = _proj([{"id": "ms-1", "status": "todo"},
                  {"id": "ms-1", "status": "todo"}])
    with pytest.raises(ValueError, match="out of range"):
        core.find_target_milestone(data, "ms-1", index=5)
