"""Account の org 所有 + project リンク開示 (ms-113 / e-3734).

顧客DB (Account) を「1つの org 所有 identity」にし、開示は project 参加でのみ
与える。ここで pin する要求 (user 明示):
  - home project からの read は従来通り全件見える (既存 project 認可がゲート)
  - 別 project から参照するとき、その project が Account の project_links に
    入っていなければ **どうやっても見えない** (fail-closed)
  - link されていれば見える / unlink した瞬間 見えなくなる (剥奪即時)
  - identity は複製しない (1 Account を複数 project に link するだけ)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import sales_entities as se  # noqa: E402
# ms-113 generalization: link/unlink は Account 専用 helper から、任意 Target を
# 開示する汎用層 (target_disclosure) に移した。テストもそちら経由に更新。
import target_disclosure as td  # noqa: E402
import disclosure  # noqa: E402


def _project(owner="u1"):
    # phase を明示して account_phases 設定への依存を避ける。
    return {"owner": owner, "accounts": []}


def test_account_add_sets_owner_org_and_empty_links():
    data = _project(owner="u1")
    acc_id = se.account_add(data, "取引先A", phase="リード")
    acc = se.find_account(data, acc_id)
    # 所有 org は project の owner から決定的に導出 (personal org)。
    assert acc[disclosure.OWNER_ORG_FIELD] == "org-p-u1"
    # 生成時 project_links は空 (= home 内でのみ見える、cross は opt-in)。
    assert acc[disclosure.PROJECT_LINKS_FIELD] == []


def test_account_add_explicit_owner_org_wins():
    data = _project(owner="u1")
    acc_id = se.account_add(data, "取引先B", phase="リード", owner_org_id="org-team-9")
    acc = se.find_account(data, acc_id)
    assert acc[disclosure.OWNER_ORG_FIELD] == "org-team-9"


def test_home_read_is_unchanged_even_without_links():
    # home project からの read は絞らない = 既存挙動 (全件見える)。
    data = _project()
    se.account_add(data, "A", phase="リード")
    se.account_add(data, "B", phase="リード")
    visible = se.accounts_disclosable_from(data["accounts"], "proj-home", is_home=True)
    assert len(visible) == 2


def test_cross_project_unlinked_is_invisible_failclosed():
    data = _project()
    se.account_add(data, "A", phase="リード")  # link していない
    # 別 project (proj-dev) から参照 → link されていないので 0 件 (fail-closed)。
    visible = se.accounts_disclosable_from(data["accounts"], "proj-dev", is_home=False)
    assert visible == []


def test_cross_project_linked_is_visible():
    data = _project()
    acc_id = se.account_add(data, "A", phase="リード")
    assert td.disclose_target(data, acc_id, "proj-dev") is True
    visible = se.accounts_disclosable_from(data["accounts"], "proj-dev", is_home=False)
    assert len(visible) == 1
    assert visible[0]["id"] == acc_id
    # 別の非 link project からは見えない。
    assert se.accounts_disclosable_from(data["accounts"], "proj-other", is_home=False) == []


def test_unlink_revokes_immediately():
    data = _project()
    acc_id = se.account_add(data, "A", phase="リード")
    td.disclose_target(data, acc_id, "proj-dev")
    assert len(se.accounts_disclosable_from(data["accounts"], "proj-dev")) == 1
    # unlink した瞬間 (次の query 評価) から見えない = 剥奪即時。
    assert td.undisclose_target(data, acc_id, "proj-dev") is True
    assert se.accounts_disclosable_from(data["accounts"], "proj-dev") == []


def test_link_is_idempotent_and_identity_not_duplicated():
    data = _project()
    acc_id = se.account_add(data, "A", phase="リード")
    assert td.disclose_target(data, acc_id, "proj-dev") is True
    assert td.disclose_target(data, acc_id, "proj-dev") is False  # 既存 link
    # Account は 1 個のまま (複製されない、複数 project に link されるだけ)。
    assert len(data["accounts"]) == 1
    assert set(se.find_account(data, acc_id)[disclosure.PROJECT_LINKS_FIELD]) == {"proj-dev"}


def test_link_unknown_account_raises():
    data = _project()
    try:
        td.disclose_target(data, "acc-999", "proj-x")
        assert False, "expected ValueError"
    except ValueError:
        pass
