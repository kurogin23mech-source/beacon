"""Red tests for AC16 / AC17 (= cross-project fanout independence) — ms-97 / e-2659.

AC16: 1 trek の scope が 2 projects にまたがる時、 各 project の session 群
それぞれに progress-check DM が独立に届く (= 1 project の down が
他 project の fanout を巻き込まない構造)。
AC17: fanout の event_id は per-project unique (= 1 DM event = 1 project bus
への 1 append、 cross-project event id collision 不可)。

これらは server-level integration test として Phase 2 で実装する。 現状は
scaffolding のみ (= contract pin で xfail マーク)。
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="AC16 not yet implemented (= cross-project fanout integration test scaffolding)")
def test_fanout_to_two_projects_yields_one_event_per_project():
    """2 project にまたがる trek scope → 2 bus event (= 1 per project)。"""
    # Phase 2 で in-memory db.list_sessions stub + tick endpoint を使い
    # 「project A に 1 session, project B に 1 session」 の trek を tick
    # して、 fired[] の recipients が 2 project 跨ぎで揃うことを assert
    # する予定。
    assert False, "AC16 implementation pending"


@pytest.mark.xfail(reason="AC17 not yet implemented (= per-project event_id uniqueness contract)")
def test_fanout_event_ids_are_unique_per_project():
    """同 tick の fanout で生成される event_id が project 間で衝突しない。"""
    # Phase 2: 2 project に同時 append した bus event の id が prefix or
    # uuid 由来で必ず unique であることを `db.append_bus_event` の戻り値
    # で確認する。
    assert False, "AC17 implementation pending"
