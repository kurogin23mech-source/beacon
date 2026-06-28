"""Red tests for AC7 (= normalize_scope_entry strict mode) — ms-97 / e-2659.

AC7 では `normalize_scope_entry` が不正 scope (= project_id 欠落 / 不正
ref pattern / type 不一致) を silent fallback ではなく `ValueError` で
拒否することを契約とする。 現状の `normalize_scope_entry` は best-effort
で 不完全 entry を埋めて返すので、 strict 化に伴って既存呼び出しの
fail-loud 化が必要 (= 後続 phase で land する)。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


@pytest.mark.xfail(reason="AC7 not yet implemented (= normalize_scope_entry still permissive)")
def test_normalize_scope_rejects_missing_project_id():
    """project_id が空の scope entry は ValueError で拒否されること。"""
    with pytest.raises(ValueError, match="project_id"):
        trek.normalize_scope_entry({
            "type": "milestone",
            "ref": "ms-99",
            "project_id": "",
        })


@pytest.mark.xfail(reason="AC7 not yet implemented (= unknown scope.type silently dropped)")
def test_normalize_scope_rejects_unknown_type():
    """type が registry に無い値 (= 'bogus') は ValueError で拒否される。"""
    with pytest.raises(ValueError, match="type"):
        trek.normalize_scope_entry({
            "type": "bogus",
            "ref": "x-1",
            "project_id": "p-1",
        })
