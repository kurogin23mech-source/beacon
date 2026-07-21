"""汎用 Target 開示層 (ms-113 generalization).

開示 (project_links への add/remove) は どの Target でも同一の純粋操作なのに、
当初 Account 専用 CLI (`beacon account link`) に縛られていた。target_disclosure は
それを id で任意 Target を引く汎用層に持ち上げる。ここでは「Account 以外の Target
(opportunity / milestone) にも同じ開示が効く」= entity 名に縛られていないことを pin。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import target_disclosure as td  # noqa: E402
import disclosure  # noqa: E402


def _data():
    # 複数 Target collection を持つ project (accounts / opportunities / milestones)。
    return {
        "owner": "u1",
        "accounts": [{"id": "acc-1", "name": "顧客A"}],
        "opportunities": [{"id": "opp-1", "name": "商談X"}],
        "milestones": [{"id": "ms-1", "title": "MS1"}],
    }


def test_find_target_scans_all_collections():
    d = _data()
    assert td.find_target(d, "acc-1")["name"] == "顧客A"
    assert td.find_target(d, "opp-1")["name"] == "商談X"
    assert td.find_target(d, "ms-1")["title"] == "MS1"
    assert td.find_target(d, "nope-9") is None


def test_disclose_works_on_account():
    d = _data()
    assert td.disclose_target(d, "acc-1", "proj-dev") is True
    assert d["accounts"][0][disclosure.PROJECT_LINKS_FIELD] == ["proj-dev"]


def test_disclose_works_on_opportunity_not_just_account():
    # entity 名に縛られていないことの証明: 商談(Opportunity)も同じ動詞で開示できる。
    d = _data()
    assert td.disclose_target(d, "opp-1", "proj-dev") is True
    assert disclosure.can_disclose(d["opportunities"][0], {"proj-dev"}) is True
    assert disclosure.can_disclose(d["opportunities"][0], {"proj-other"}) is False


def test_disclose_works_on_milestone():
    d = _data()
    assert td.disclose_target(d, "ms-1", "proj-dev") is True
    assert d["milestones"][0][disclosure.PROJECT_LINKS_FIELD] == ["proj-dev"]


def test_undisclose_and_idempotency():
    d = _data()
    td.disclose_target(d, "opp-1", "proj-dev")
    assert td.disclose_target(d, "opp-1", "proj-dev") is False   # 既存
    assert td.undisclose_target(d, "opp-1", "proj-dev") is True
    assert td.undisclose_target(d, "opp-1", "proj-dev") is False  # 既に無い


def test_unknown_target_raises():
    d = _data()
    for fn in (td.disclose_target, td.undisclose_target):
        try:
            fn(d, "ghost-9", "proj-x")
            assert False, "expected ValueError"
        except ValueError:
            pass
