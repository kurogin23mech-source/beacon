"""lib/disclosure.py — 開示プリミティブ (ms-113 / e-3733・e-3734・e-3735・e-3738).

「誰に何を見せてよいか」を **project 参加 (participation)** を唯一の境界として
判定する 1 つの primitive (= 原始的な部品) に集約する。組織 (org) に居るだけでは
何も見えず、開示は「その情報が紐付く project に参加していること」でのみ与えられる
(e-3733)。この 1 つの規則を、project そのもの・共有 identity (Account, ms-111)・
doc・target・エージェント間問い合わせ (e-3738) すべてに一様に適用する。

なぜ query 層で・現在の membership で評価するか (e-3733):
  - 「組織に居れば全 project が見える」を許すと社内の情報権限が壊れる。
  - 判定を保存時 (write) でなく問い合わせ時 (query) に・その瞬間の membership で
    行うことで、membership 剥奪 (= revocation) が履歴も含めて **即座に** 開示から
    消える (= grandfather しない)。これは収益・情報境界として robust。

設計の核: **fail-closed** (= 迷ったら閉じる)。どの project にも紐付いていない
resource は誰にも開示しない。principal の実効スコープ (lib/principal.py が算出) と
resource の project リンク集合の積が非空のときだけ開示する。

本モジュールは純関数のみ。participation の解決 (= user → 参加 project 集合) と
resource の取得は server 側が担い、その結果を本モジュールに渡す。
"""
from __future__ import annotations

from typing import Iterable

# resource が「どの project に開示されるか」を持つフィールド名。Account / doc /
# target すべてこの 1 つの規約に乗せる (e-3734 の一般性 = 同じ開示プリミティブ)。
PROJECT_LINKS_FIELD = "project_links"

# resource の所有 org を持つフィールド名 (e-3734: Account は org 所有 identity 1 個)。
OWNER_ORG_FIELD = "owner_org_id"


def resource_project_links(resource: dict) -> set[str]:
    """resource が開示される project 集合を返す。無ければ空集合。"""
    if not resource:
        return set()
    return set(resource.get(PROJECT_LINKS_FIELD) or ())


def can_disclose(resource: dict, effective_scope: Iterable[str]) -> bool:
    """resource が principal に開示されるか。

    = resource の project リンク ∩ principal の実効スコープ が非空。
    リンクの無い resource は **誰にも開示しない** (fail-closed)。effective_scope は
    lib/principal.py の client/backend 合成結果を渡す。
    """
    links = resource_project_links(resource)
    if not links:
        return False
    return bool(links & set(effective_scope or ()))


def filter_disclosable(resources: Iterable[dict], effective_scope: Iterable[str]) -> list[dict]:
    """resource 列を、principal に開示可能なものだけに絞る (= query 層フィルタ)。"""
    eff = set(effective_scope or ())
    return [r for r in resources if can_disclose(r, eff)]


def link_resource_to_project(resource: dict, project_id: str) -> bool:
    """resource を project に開示リンクする。追加したら True、既存なら False。

    リンクの張替え / re-home (e-3737) はこの add と :func:`unlink_resource_from_project`
    の組合せで表現する。identity 自体は作り直さない (= org 所有のまま、リンク集合を
    変えるだけ) ので Account の同一性が保たれる。呼び出し側が変更を追跡可能な
    イベントとして記録する (server で decision_event に残す想定)。
    """
    if not project_id:
        raise ValueError("project_id is required to link a resource")
    links = resource.setdefault(PROJECT_LINKS_FIELD, [])
    if project_id in links:
        return False
    links.append(project_id)
    return True


def unlink_resource_from_project(resource: dict, project_id: str) -> bool:
    """resource の project 開示リンクを外す。外したら True、無ければ False。"""
    links = resource.get(PROJECT_LINKS_FIELD) or []
    if project_id not in links:
        return False
    resource[PROJECT_LINKS_FIELD] = [p for p in links if p != project_id]
    return True


def compose_inter_agent_disclosure(
    resources: Iterable[dict],
    requester_effective_scope: Iterable[str],
    *,
    sensitivity_gate=None,
) -> list[dict]:
    """エージェント間問い合わせ応答を、**要求元 (requester) principal** の実効スコープで
    フィルタし、任意で ms-63 の機微フィルタ (sensitivity_gate) と合成する (e-3738).

    強い権限のエージェント (例: 全社を見られる営業エージェント) を踏み台にして、
    本来見えないはずの user (例: 開発のみの user) が Account を引き出す「混乱した
    代理人」越境を塞ぐ。応答側の権限ではなく **要求元** の project 参加で絞るのが肝。

    `sensitivity_gate` は `(resource) -> bool` (= 開示してよければ True) の callable。
    ms-63 disclosure gate (= 受信側応答の機密フィルタ) をここに差し込むと、project
    参加フィルタ **かつ** 機微フィルタの両方を通ったものだけが残る (= 合成)。
    """
    visible = filter_disclosable(resources, requester_effective_scope)
    if sensitivity_gate is None:
        return visible
    return [r for r in visible if sensitivity_gate(r)]
