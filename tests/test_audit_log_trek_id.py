"""Red tests for AC19 / G3 (= audit log trek_id propagation) — ms-97 / e-2659.

AC19: trek scope 内の mutation (= scope add/remove / member change / halt
/ goal_state 更新) は audit log に必ず `trek_id` を載せる契約。 G3 では
audit query が「指定 trek_id の全 mutation 履歴」 を 1 つの time-line と
して返せることを保証する (= post-mortem / dogfood findings の起点と
なる構造)。

これらは Phase 3 で audit_log の hook 拡張と共に land する。
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(reason="AC19 not yet implemented (= audit log lacks trek_id field on trek mutations)")
def test_scope_mutation_audit_log_carries_trek_id():
    """scope.add / scope.remove の audit entry が trek_id を含むこと。"""
    # Phase 3: server/app.py の trek_scope_add_endpoint /
    # trek_scope_remove_endpoint で audit_log.append() に trek_id を
    # 載せる。 ここでは in-memory audit sink を inject して assert する
    # 構造を想定。
    assert False, "AC19 implementation pending"


@pytest.mark.xfail(reason="G3 not yet implemented (= trek_id timeline query path missing)")
def test_audit_query_by_trek_id_returns_timeline():
    """audit log を trek_id で filter したクエリが時系列で帰ること。"""
    # G3 の保証範囲: scope mutation / member mutation / halt mutation /
    # goal_state 更新の 4 種類を 1 trek_id で混ぜて投げた時、 created_at
    # 昇順で 4 entry を返す統合 query を提供する。
    assert False, "G3 implementation pending"
