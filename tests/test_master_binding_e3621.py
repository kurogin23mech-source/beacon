"""Unit tests for project-level master binding declaration (ms-111 / e-3621 その1).

e-3621 は「master_binding を統一 Target 属性に乗せ Account/Contact を external_ref
化する」。本 test はその第 1 スライス = **project ごとのマスター束縛宣言**を pin する
(Account/Contact の投影 external_ref 化は次スライス)。

ここで pin する不変条件:
  - 既定は Beacon-default master (AC: 既定が Beacon-default)。
  - 束縛軸 org_id は org.project_org_id が単一真実源 (leader 指定): 明示 org_id →
    owner の personal org → "" の順に純粋導出。未宣言 project も所属先が定まる
    (= lazy retrofit、migration 不要)。
  - 明示宣言 > 導出。外部 CRM 接続は system 上書きで表す。
  - resolve は非破壊、ensure は書き込み。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import master_binding as mb  # noqa: E402


# ---------------------------------------------------------------------------
# 既定導出: 未宣言でも所属先が定まる (lazy retrofit)
# ---------------------------------------------------------------------------
def test_default_binding_derives_org_from_owner():
    proj = {"owner": "u1"}          # org_id 未 write の既存 project
    b = mb.resolve_master_binding(proj)
    assert b["system"] == "beacon-default"
    assert b["org_id"] == "org-p-u1"   # owner の personal org を導出
    # 非破壊: resolve は project を書き換えない
    assert mb.MASTER_BINDING_FIELD not in proj


def test_default_binding_prefers_explicit_org_id():
    proj = {"owner": "u1", "org_id": "org-t-team9"}
    assert mb.master_binding_org_id(proj) == "org-t-team9"   # 明示 org_id 優先


def test_owner_less_project_has_empty_org():
    # migration 期の owner-less project は所属未定 ("")
    assert mb.master_binding_org_id({}) == ""
    assert mb.is_beacon_default({})          # system 既定は保たれる


# ---------------------------------------------------------------------------
# 明示宣言 > 導出
# ---------------------------------------------------------------------------
def test_explicit_binding_overrides_derivation():
    proj = {"owner": "u1", "master_binding": {"system": "salesforce", "org_id": "org-t-x"}}
    b = mb.resolve_master_binding(proj)
    assert b == {"system": "salesforce", "org_id": "org-t-x"}
    assert not mb.is_beacon_default(proj)


def test_partial_binding_fills_missing_side_from_default():
    # system だけ宣言 → org_id は導出で埋まる
    proj = {"owner": "u1", "master_binding": {"system": "hubspot"}}
    b = mb.resolve_master_binding(proj)
    assert b["system"] == "hubspot"
    assert b["org_id"] == "org-p-u1"


# ---------------------------------------------------------------------------
# ensure (書き込み) / set (明示設定)
# ---------------------------------------------------------------------------
def test_ensure_writes_default_binding():
    proj = {"owner": "u1"}
    b = mb.ensure_master_binding(proj)
    assert proj["master_binding"] == b == {"system": "beacon-default", "org_id": "org-p-u1"}


def test_ensure_is_idempotent_on_existing():
    proj = {"owner": "u1", "master_binding": {"system": "salesforce", "org_id": "org-t-x"}}
    b1 = mb.ensure_master_binding(proj)
    b2 = mb.ensure_master_binding(proj)
    assert b1 == b2 == {"system": "salesforce", "org_id": "org-t-x"}


def test_set_binding_updates_only_given_side():
    proj = {"owner": "u1"}
    mb.set_master_binding(proj, system="salesforce")     # org_id は導出値を保つ
    assert proj["master_binding"] == {"system": "salesforce", "org_id": "org-p-u1"}
    mb.set_master_binding(proj, org_id="org-t-z")         # system は保つ
    assert proj["master_binding"] == {"system": "salesforce", "org_id": "org-t-z"}


def test_set_binding_rejects_empty_system():
    proj = {}    # 導出 system も既定 beacon-default なので通常空にはならないが、防御
    # system="" は「変えない」= 現在の beacon-default を保つので raise しない
    mb.set_master_binding(proj, system="")
    assert proj["master_binding"]["system"] == "beacon-default"


# ---------------------------------------------------------------------------
# experimental / 意図的未配線 の固定 (e-4360)
#   master_binding は宣言層で、production 経路 (lib/commands.py / server/app.py) から
#   *まだ* 呼ばれていない。「呼び元ゼロ = dead module」と誤読して削除されるのを防ぐため、
#   experimental マーカーの存在と、未配線であること自体をテストで固定する。wire は
#   ms-111 の linking go-live 時に行い、その際このセクションを更新する。
# ---------------------------------------------------------------------------
def test_module_carries_experimental_marker():
    """EXPERIMENTAL = True マーカーを持ち、docstring がその状態を明示している。"""
    assert getattr(mb, "EXPERIMENTAL", None) is True
    doc = mb.__doc__ or ""
    assert "experimental" in doc.lower()
    assert "go-live" in doc.lower()  # wire のタイミングが docstring に明記されている


def _production_sources() -> list[str]:
    lib_dir = REPO / "lib"
    server_dir = REPO / "server"
    paths = [lib_dir / "commands.py", server_dir / "app.py"]
    return [p.read_text(encoding="utf-8") for p in paths if p.exists()]


def test_master_binding_is_not_wired_into_production():
    """production コード (lib/commands.py / server/app.py) が master_binding を import /
    参照していないことを固定する (= 意図的未配線)。

    go-live で配線する時はこのテストを更新する。ここで落ちたら「未配線契約」が破れた
    (= どこかから呼び始めた) 合図であり、docstring / EXPERIMENTAL マーカーの見直しが要る。
    """
    for src in _production_sources():
        assert "master_binding" not in src, (
            "master_binding が production 経路から参照され始めた。"
            "go-live 配線なら本テストと experimental マーカーを更新すること。"
        )
