"""lib/principal.py — Principal (主体) model + 実効スコープ合成 (ms-113 / e-3732).

Principal (= 主体) は「誰の権限で・どの project に従事する誰か」を第一級 (first-class)
にした値。従来 Beacon では「どの user が・どの project に触れるか」が CWD (作業
ディレクトリ) や session_id への暗黙依存で決まっており、開示境界 (= 情報をどこまで
見せてよいかの線) を enforce できなかった。principal を明示的に組み立てて request に
伝播させることで、開示判定をこの 1 つの主体に対して行えるようにする。

principal の 5 要素:
  - user_id            : 委任元の人間 (= 誰の権限か)
  - org_id             : その user が属する組織 (= テナンシー階層の所属先)
  - agent_kind         : "client" (= 人間委任の対話エージェント) か
                         "backend" (= サービス identity で動く自律エージェント) か
  - declared_scope     : この主体が「従事すると宣言した project 集合」。None は
                         「宣言による制約なし (= 参加範囲そのまま)」を意味する
  - focus              : いま焦点を当てている単一 project (= あれば実効をそこに絞る)

実効スコープ (= 実際に触れてよい project 集合) の合成契約:
  - client  : `user の project 参加 ∩ declared_scope ∩ (focus があれば {focus})`
              (= 参加を超えられない。宣言と focus は「絞る」方向にしか効かない)
  - backend : `min(service grant, work-unit 宣言 scope, originating principal 実効)`
              (= サービスの grant を天井に、work-unit 宣言と 呼び出し元 principal の
              いずれよりも広くならない。混乱した代理人 (= 強い backend を踏み台に
              した越境) を構造的に塞ぐ)

本モジュールは純関数のみ (集合演算)。store I/O も request 解釈も server 側が担う
(= lib/server 分離、org.py と同型)。「どの project 集合が見えるか」までを返し、
個々の resource 開示 (= Account / doc / target 単位の可視判定) は lib/disclosure.py
(e-3733/e-3734) が本モジュールの実効スコープを入力に判定する。
"""
from __future__ import annotations

from typing import Iterable, Optional

AGENT_CLIENT = "client"
AGENT_BACKEND = "backend"
VALID_AGENT_KINDS = {AGENT_CLIENT, AGENT_BACKEND}


def make_principal(
    user_id: str,
    org_id: str = "",
    *,
    agent_kind: str = AGENT_CLIENT,
    declared_scope: Optional[Iterable[str]] = None,
    focus: Optional[str] = None,
) -> dict:
    """Principal を組み立てる。

    ``declared_scope=None`` は「宣言による制約なし」= 参加範囲そのまま。空リスト
    ``[]`` とは意味が違う (空リスト = 明示的に「どの project にも従事しない」宣言)。
    """
    if agent_kind not in VALID_AGENT_KINDS:
        raise ValueError(
            f"invalid agent_kind: {agent_kind!r} (valid: {sorted(VALID_AGENT_KINDS)})"
        )
    return {
        "user_id": user_id,
        "org_id": org_id,
        "agent_kind": agent_kind,
        # None を保つ (= 「宣言なし」と「空宣言」を区別する)。
        "declared_scope": list(declared_scope) if declared_scope is not None else None,
        "focus": focus,
    }


def client_effective_scope(
    principal: dict,
    participation: Iterable[str],
) -> set[str]:
    """client principal の実効 project 集合を返す。

    `participation` は user が実際に参加している project 集合 (= 開示境界の真値、
    server が membership から解決して渡す)。実効はこれを **超えられない**:

        eff = participation
        if declared_scope is not None: eff &= declared_scope   # 宣言で絞る
        if focus is not None:          eff &= {focus}          # focus で 1 点に絞る

    declared_scope / focus は「拡張」方向には一切効かない (= 参加していない project を
    宣言や focus で覗くことは不可能)。
    """
    eff = set(participation or ())
    declared = principal.get("declared_scope")
    if declared is not None:
        eff &= set(declared)
    focus = principal.get("focus")
    if focus is not None:
        eff &= {focus}
    return eff


def backend_effective_scope(
    service_grant: Iterable[str],
    work_unit_scope: Iterable[str],
    originating_scope: Iterable[str],
) -> set[str]:
    """backend principal の実効 project 集合を返す = 3 者の min (積集合)。

    - `service_grant`     : サービス identity に与えられた天井 (= これ以上は不可)
    - `work_unit_scope`   : この作業単位で宣言した project scope (実行中は拡張しない)
    - `originating_scope` : 呼び出し元 principal の実効 (= client / 上位 backend)

    どれよりも広くならない = 強い backend を踏み台にした権限昇格・org 横断 roaming を
    構造的に不可能にする (混乱した代理人問題への構造的回答)。
    """
    return set(service_grant or ()) & set(work_unit_scope or ()) & set(originating_scope or ())


def effective_scope(
    principal: dict,
    *,
    participation: Optional[Iterable[str]] = None,
    service_grant: Optional[Iterable[str]] = None,
    work_unit_scope: Optional[Iterable[str]] = None,
    originating_scope: Optional[Iterable[str]] = None,
) -> set[str]:
    """agent_kind に応じて client / backend の実効スコープ合成へ振り分ける facade。

    - client  : `participation` を渡す (必須)。
    - backend : `service_grant` / `work_unit_scope` / `originating_scope` を渡す。
                いずれか欠落は空集合扱い (= min なので最も狭い = 何も触れない、安全側)。
    """
    if principal.get("agent_kind") == AGENT_BACKEND:
        return backend_effective_scope(
            service_grant or (), work_unit_scope or (), originating_scope or ()
        )
    return client_effective_scope(principal, participation or ())


def can_access_project(effective: Iterable[str], project_id: str) -> bool:
    """実効スコープに project_id が含まれるか (= その project に従事してよいか)。"""
    return bool(project_id) and project_id in set(effective or ())


# ---------------------------------------------------------------------------
# Service identity (= backend authz 主体) — ms-113 / e-3736
#
# backend principal は「サービス identity (= 人間ではない自律アクター)」の grant を
# 天井にして動く。ここで言う service identity は authz (認可) 概念であり、profile.py の
# Profile (= どの API endpoint / 認証情報で喋るかのデプロイ identity) とは別物。混同しない:
#   - Profile          : どこに繋ぐか (deployment / endpoint / credentials)
#   - ServiceIdentity  : どこまで触れてよいか (authz / grant ceiling / org 所属)
#
# 本層は純関数のみ (値の組み立てと集合演算)。service identity を「どこに登録し・どう
# 保存するか」(= registry / store) は server 側の責務 (org.py と同型の分離)。ms-114 の
# サーバサイド authoring (e-3742/e-3745) が、この層の `backend_work_unit_effective_scope`
# を seam (= 継ぎ目) として呼び、宣言した work-unit scope の下で backend principal を動かす。
# ---------------------------------------------------------------------------


def make_service_identity(
    service_id: str,
    service_grant: Iterable[str],
    *,
    org_id: str = "",
) -> dict:
    """backend サービス identity を組み立てる。

    - `service_id`    : サービスの識別子 (= 監査で「どのサービスが動いたか」を辿る鍵)
    - `service_grant` : このサービスが **未来永劫触れてよい project 集合の天井**。
                        実行時の実効はこれを超えられない (backend_effective_scope の min)。
    - `org_id`        : このサービスが属する組織 (= テナンシー階層の所属先)。org を跨いだ
                        roaming を authz で塞ぐための所属アンカー。

    service_grant は「天井」なので、空集合 = 何にも触れない (= 最も安全側) を意味する。
    """
    if not service_id:
        raise ValueError("service_id must be non-empty")
    return {
        "service_id": service_id,
        "service_grant": sorted(set(service_grant or ())),
        "org_id": org_id,
    }


def backend_principal(
    service_identity: dict,
    *,
    work_unit_scope: Iterable[str],
    delegating_user_id: str = "",
    focus: Optional[str] = None,
) -> dict:
    """service identity から、1 作業単位 (= work unit) 用の backend principal を組み立てる。

    backend principal は `agent_kind="backend"` で、この作業単位で宣言した
    `work_unit_scope` を `declared_scope` に載せて運ぶ。実行中はこの宣言を **拡張しない**
    (= backend_effective_scope の min により、宣言・天井・呼び出し元のどれよりも広くならない)。

    - `service_identity`    : make_service_identity の戻り値
    - `work_unit_scope`     : この作業単位で「従事する」と宣言する project 集合
    - `delegating_user_id`  : この backend を起動した委任元の人間 (= 監査用、無ければ空)
    - `focus`               : いま焦点を当てる単一 project (= あれば実効をそこに絞る)

    戻り値は make_principal と同じ 5 要素に、監査用の `service_id` を添えた形。
    """
    if not isinstance(service_identity, dict) or not service_identity.get("service_id"):
        raise ValueError("service_identity must be built via make_service_identity")
    p = make_principal(
        delegating_user_id,
        service_identity.get("org_id", ""),
        agent_kind=AGENT_BACKEND,
        declared_scope=work_unit_scope,
        focus=focus,
    )
    # どの service identity が動かしているかを principal に刻む (= 監査で辿れるように)。
    p["service_id"] = service_identity["service_id"]
    return p


def backend_work_unit_effective_scope(
    service_identity: dict,
    work_unit_scope: Iterable[str],
    originating_effective: Iterable[str],
) -> set[str]:
    """backend の 1 作業単位が実際に触れてよい project 集合を返す = ms-114 との接続 seam。

    ms-114 のサーバサイド authoring はこの 1 関数を呼ぶだけでよい:
      - `service_identity`      : 動かすサービスの identity (= 天井 service_grant を持つ)
      - `work_unit_scope`       : この authoring 作業で宣言する scope
      - `originating_effective` : 呼び出し元 principal の **実効** scope
                                  (client なら client_effective_scope、上位 backend なら
                                  その backend_work_unit_effective_scope の結果)

    3 者の min を取るので、強い backend を踏み台にした権限昇格・org 横断 roaming は
    構造的に不可能 (= 混乱した代理人問題への構造的回答)。天井 (service_grant) は
    service_identity から解決するので、呼び出し側が誤って広い grant を渡す余地が無い。
    """
    return backend_effective_scope(
        service_identity.get("service_grant", ()),
        work_unit_scope,
        originating_effective,
    )
