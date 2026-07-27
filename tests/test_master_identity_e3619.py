"""Unit tests for thin master identity schema (ms-111 / e-3619).

ms-111 は「顧客 identity (誰が誰か) を薄いマスター (真値源) に一元化し各インスタンス
へ投影・同期する」L1 semantic layer。e-3619 はその最初の一手 = **薄いマスター schema
の確定**。store 本体 (MySQL 別 schema + /master API) は e-3620、投影側の external_ref
化は e-3621 が担うので、ここでは純関数の schema 契約だけを pin する。

ここで pin する不変条件 (= 薄いマスターの schema 契約そのもの):
  - identity only (SPEC §3): builder は allowlist の field しか作らず、work data
    (phase / health / assignee / contacts …) の混入を assert_identity_only が弾く。
    これが「マスターは薄く保つ」を prompt でなく code で閉じる forcing function。
  - 束縛軸 = org_id (SPEC §8 案 B): org_id 空の orphan identity を作れない。
  - external_ref (system of record 宣言): 既定は Beacon-default 自己参照、外部 CRM
    接続時は system+ref_id で adapter が突き合わせられる。tolerant reader が str
    旧式も読む (schema 進化の互換契約)。
  - id は project 横断共有ゆえ minted hex (macc-/mcon-)、prefix で種別判定できる。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import master_identity as mi  # noqa: E402

NOW = "2026-07-27T00:00:00+00:00"
ORG = "org-p-u1"


# ---------------------------------------------------------------------------
# id mint / 判定: project 横断共有ゆえ minted hex、prefix で種別を区別
# ---------------------------------------------------------------------------
def test_mint_ids_have_expected_prefix_and_are_unique():
    a1 = mi.mint_master_account_id()
    a2 = mi.mint_master_account_id()
    c1 = mi.mint_master_contact_id()
    assert mi.is_master_account_id(a1) and a1.startswith("macc-")
    assert mi.is_master_contact_id(c1) and c1.startswith("mcon-")
    assert a1 != a2                       # 発行のたび一意
    assert not mi.is_master_account_id(c1)  # 種別を取り違えない
    assert not mi.is_master_contact_id(a1)


# ---------------------------------------------------------------------------
# Account builder: identity のみ + org_id 束縛 + external_ref
# ---------------------------------------------------------------------------
def test_new_master_account_minimal_is_identity_only():
    acc = mi.new_master_account("Acme Inc", org_id=ORG, now=NOW)
    # allowlist の field だけ (= work data ゼロ) を構造で保証
    assert set(acc.keys()) == mi.MASTER_ACCOUNT_ALLOWED_FIELDS
    assert acc["name"] == "Acme Inc"
    assert acc["org_id"] == ORG
    assert acc["created_at"] == NOW == acc["updated_at"]
    assert mi.is_master_account_id(acc["master_account_id"])
    # 既定 external_ref は Beacon-default 自己参照
    assert acc["external_ref"] == {"system": "beacon-default", "ref_id": ""}


def test_new_master_account_explicit_id_and_external_ref():
    acc = mi.new_master_account(
        "  Acme  ", org_id=ORG, now=NOW,
        master_account_id="macc-fixed",
        external_ref={"system": "salesforce", "ref_id": "0015000000abc"},
    )
    assert acc["master_account_id"] == "macc-fixed"
    assert acc["name"] == "Acme"  # trim される
    assert acc["external_ref"] == {"system": "salesforce", "ref_id": "0015000000abc"}


def test_new_master_account_requires_name_and_org():
    with pytest.raises(ValueError):
        mi.new_master_account("", org_id=ORG, now=NOW)
    with pytest.raises(ValueError):
        mi.new_master_account("Acme", org_id="", now=NOW)  # orphan identity 禁止 (§8)


# ---------------------------------------------------------------------------
# Contact builder: 親会社必須 + 連絡先は identity 属性
# ---------------------------------------------------------------------------
def test_new_master_contact_is_identity_only():
    con = mi.new_master_contact(
        "Taro Yamada", org_id=ORG, master_account_id="macc-fixed", now=NOW,
        email="taro@acme.example", phone="03-0000-0000", role="CTO",
    )
    assert set(con.keys()) == mi.MASTER_CONTACT_ALLOWED_FIELDS
    assert con["master_account_id"] == "macc-fixed"  # 親会社への relation
    assert con["email"] == "taro@acme.example"
    assert con["role"] == "CTO"
    assert mi.is_master_contact_id(con["master_contact_id"])


def test_new_master_contact_requires_parent_account():
    with pytest.raises(ValueError):
        mi.new_master_contact("Taro", org_id=ORG, master_account_id="", now=NOW)


# ---------------------------------------------------------------------------
# 薄さの構造的強制: work data の混入を弾く (= e-3619 受入条件の中核)
# ---------------------------------------------------------------------------
def test_assert_identity_only_rejects_work_fields():
    leaky = mi.new_master_account("Acme", org_id=ORG, now=NOW)
    # 投影側の work field を紛れ込ませる
    leaky["phase"] = "リード"
    leaky["health"] = "good"
    leaky["contacts"] = []
    with pytest.raises(ValueError) as ei:
        mi.assert_identity_only(leaky, kind="account")
    msg = str(ei.value)
    assert "phase" in msg and "health" in msg  # 何が漏れたか名指しする
    assert not mi.is_identity_only(leaky, kind="account")


def test_assert_identity_only_accepts_clean_records():
    acc = mi.new_master_account("Acme", org_id=ORG, now=NOW)
    con = mi.new_master_contact("Taro", org_id=ORG, master_account_id="macc-x", now=NOW)
    assert mi.is_identity_only(acc, kind="account")
    assert mi.is_identity_only(con, kind="contact")


def test_assert_identity_only_unknown_kind():
    with pytest.raises(ValueError):
        mi.assert_identity_only({}, kind="deal")


# ---------------------------------------------------------------------------
# external_ref: system of record 宣言 + tolerant reader (schema 進化互換)
# ---------------------------------------------------------------------------
def test_new_external_ref_requires_system():
    assert mi.new_external_ref("beacon-default") == {"system": "beacon-default", "ref_id": ""}
    with pytest.raises(ValueError):
        mi.new_external_ref("")


def test_normalize_external_ref_tolerant():
    # dict はそのまま正規化
    assert mi.normalize_external_ref({"system": "hubspot", "ref_id": "42"}) == \
        {"system": "hubspot", "ref_id": "42"}
    # 旧式 str は Beacon-default 下の ref_id とみなす
    assert mi.normalize_external_ref("legacy-id") == \
        {"system": "beacon-default", "ref_id": "legacy-id"}
    # None/空は Beacon-default 自己参照へフォールバック
    assert mi.normalize_external_ref(None) == {"system": "beacon-default", "ref_id": ""}
    assert mi.normalize_external_ref({}) == {"system": "beacon-default", "ref_id": ""}


# ---------------------------------------------------------------------------
# accessor: 寛容な読み
# ---------------------------------------------------------------------------
def test_accessors_are_tolerant():
    acc = mi.new_master_account("Acme", org_id=ORG, now=NOW)
    assert mi.master_account_org_id(acc) == ORG
    assert mi.master_account_name(acc) == "Acme"
    assert mi.master_external_ref(acc) == {"system": "beacon-default", "ref_id": ""}
    con = mi.new_master_contact("Taro", org_id=ORG, master_account_id="macc-x", now=NOW)
    assert mi.master_contact_parent_id(con) == "macc-x"
    # 壊れた入力でも落ちない
    assert mi.master_account_org_id({}) == ""
    assert mi.master_account_org_id(None) == ""
