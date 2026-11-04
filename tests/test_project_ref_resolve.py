"""Unit tests for ``lib.project_ref.resolve_project_ref`` — ms-97 / e-2694.

Beacon mints project ids as ``<slug>-<6hex>`` (= ``life-plan-simulator-68c5df``)
but CLI users routinely supply the slug alone (= ``life-plan-simulator``).
The session registry, DM fanout, and Trek scope expansion all key off
the suffix'd full id, so a short-name input silently misses every record
and surfaces as a "ghost member" or no-op DM rather than an error.

These tests pin the resolver's contract:

* exact-id input returns unchanged (= fast path, no DB scan needed)
* slug with exactly one matching id returns the full id
* slug with multiple matches raises ``ValueError`` (= ambiguous; caller
  must disambiguate explicitly)
* unknown ref returns unchanged (= caller decides 404 vs accept)
* empty input returns empty string (no resolution attempt)
* the lister can be a callable OR an already-materialised list of rows
* lister errors degrade gracefully to passthrough (= never crash the
  caller on a transient DB hiccup)

The :func:`try_resolve_project_ref` variant additionally swallows the
ambiguous ``ValueError`` so server-side fanout (= ``_resolve_trek_scope
_project_ids``) can keep iterating across treks without one ambiguous
slug aborting the whole loop.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from lib.project_ref import resolve_project_ref, try_resolve_project_ref  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECTS_TWO_DISTINCT = [
    {"project_id": "life-plan-simulator-68c5df", "name": "life-plan-simulator"},
    {"project_id": "beacon-b95643", "name": "beacon"},
]

PROJECTS_AMBIGUOUS = [
    {"project_id": "demo-aaaaaa", "name": "demo a"},
    {"project_id": "demo-bbbbbb", "name": "demo b"},
]


# ---------------------------------------------------------------------------
# resolve_project_ref — core contract
# ---------------------------------------------------------------------------


class TestResolveProjectRef:
    def test_short_name_resolves_to_full_id(self):
        out = resolve_project_ref(
            "life-plan-simulator", db_or_lister=PROJECTS_TWO_DISTINCT,
        )
        assert out == "life-plan-simulator-68c5df"

    def test_exact_full_id_passthrough(self):
        out = resolve_project_ref(
            "life-plan-simulator-68c5df", db_or_lister=PROJECTS_TWO_DISTINCT,
        )
        assert out == "life-plan-simulator-68c5df"

    def test_ambiguous_short_name_raises(self):
        with pytest.raises(ValueError, match="ambiguous"):
            resolve_project_ref("demo", db_or_lister=PROJECTS_AMBIGUOUS)

    def test_unknown_ref_passthrough(self):
        """No match → caller decides 404 vs accept; resolver doesn't raise."""
        out = resolve_project_ref(
            "totally-unknown", db_or_lister=PROJECTS_TWO_DISTINCT,
        )
        assert out == "totally-unknown"

    def test_empty_ref_returns_empty(self):
        assert resolve_project_ref("", db_or_lister=PROJECTS_TWO_DISTINCT) == ""

    def test_lister_can_be_callable(self):
        """Callable lister is invoked once and returns project rows."""
        calls = {"n": 0}

        def _list():
            calls["n"] += 1
            return PROJECTS_TWO_DISTINCT

        out = resolve_project_ref("life-plan-simulator", db_or_lister=_list)
        assert out == "life-plan-simulator-68c5df"
        assert calls["n"] == 1

    def test_lister_raising_degrades_to_passthrough(self):
        """A broken lister returns the input unchanged (no exception)."""

        def _boom():
            raise RuntimeError("db down")

        out = resolve_project_ref("life-plan-simulator", db_or_lister=_boom)
        # Lister broken → no rows → unknown ref passthrough.
        assert out == "life-plan-simulator"

    def test_none_lister_passthrough(self):
        """No lister supplied → resolver is a no-op (= test / offline mode)."""
        out = resolve_project_ref("life-plan-simulator", db_or_lister=None)
        assert out == "life-plan-simulator"


# ---------------------------------------------------------------------------
# try_resolve_project_ref — server fanout variant
# ---------------------------------------------------------------------------


class TestTryResolveProjectRef:
    def test_unique_match_returns_full_id(self):
        out = try_resolve_project_ref(
            "life-plan-simulator", db_or_lister=PROJECTS_TWO_DISTINCT,
        )
        assert out == "life-plan-simulator-68c5df"

    def test_ambiguous_returns_input_unchanged(self):
        """Server fanout must not abort the loop on one ambiguous slug."""
        out = try_resolve_project_ref("demo", db_or_lister=PROJECTS_AMBIGUOUS)
        assert out == "demo"
# ---------------------------------------------------------------------------
# ms-185 e-7070: TestNormalizeScopeEntryResolveKwarg (3 件) を落とした。
#
# 何を測っていたか: ``lib/trek.normalize_scope_entry`` が ``resolve=True`` の
# ときだけ短い名前を展開し、既定 (resolve 無し) では展開しないこと。
# その関数は共同作業空間の scope 項目を正規化するもので、本体ごと消えた。
#
# **解決器そのものの振る舞いはこのファイルの残り 10 件が測り続ける** ——
# 短い名前の一致 / 曖昧なときに ValueError を上げる / 行が無ければ素通り、という
# 契約は Trek とは無関係で、呼び出し元が変わっても要る。落としたのは「Trek の
# scope 項目に適用したとき」という適用側の 3 件だけで、解決器の被覆は減っていない。
# ---------------------------------------------------------------------------
