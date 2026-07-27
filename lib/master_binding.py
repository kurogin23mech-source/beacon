"""lib/master_binding.py — project ごとの「マスター束縛」宣言 (ms-111 / e-3621 その1).

各 project (= インスタンス) が「自分の顧客 identity のマスター (= 真値源) は誰か」を
宣言する層 (SPEC ms-111 §5 master_binding)。既定は Beacon-default master、外部 CRM
(顧客管理システム) に接続する時はその system を指す。束縛軸は org_id (SPEC §8 案 B):
同じ org に属す project が同じマスターを共有する。

所属 org の単一真実源は `org.project_org_id(project)` (ms-118 / e-4233、leader 指定):
明示 org_id → owner の personal org (org-p-<uid>) → "" の順に純粋導出する。master_binding
は既定でこの導出値を org_id に採るので、org_id 未 write の既存 project も所属先が一意に
決まり、全 project を一括変換する migration が要らない (= lazy retrofit、org 層と同方針)。

本モジュールは純関数のみ (dict を受けて dict を返す)。store I/O は server / CLI が担う。
`master_binding` を「持っていない」 project でも `resolve_master_binding` が既定を live
導出するので、宣言の書き込みは任意 (= 明示宣言 or 導出のどちらでも所属先が定まる)。

開示 (= 誰がそのマスターを読めるか) はこの束縛では決まらない: 束縛は「どのマスターに
属すか」だけを表し、access は participation-only (= project 参加でのみ与えられる、e-3733
の開示プリミティブ) が別途決める。org に居るだけでは access は付かない (leader 指定)。
"""
from __future__ import annotations

import master_identity as mi
import org as org_mod

# project.json 上の宣言フィールド名 + その下の key。
MASTER_BINDING_FIELD = "master_binding"
_SYSTEM_KEY = "system"
_ORG_ID_KEY = "org_id"

# 既定のマスター system = Beacon 自身 (schema 層と単一真実源を共有)。
BEACON_DEFAULT_SYSTEM = mi.BEACON_DEFAULT_SYSTEM


def default_master_binding(project: dict) -> dict:
    """project の既定マスター束縛を純粋導出する (未宣言でも所属先が定まる)。

    system = Beacon-default、org_id = org.project_org_id(project) (= 束縛軸の単一
    真実源)。owner も org_id も無い migration 期の project では org_id="" になる
    (= 所属未定、宣言も導出もできない状態を素直に表す)。
    """
    return {
        _SYSTEM_KEY: BEACON_DEFAULT_SYSTEM,
        _ORG_ID_KEY: org_mod.project_org_id(project),
    }


def normalize_master_binding(value: object, project: dict) -> dict:
    """宣言値を寛容に読む (tolerant reader、house style)。

    dict なら system/org_id を正規化し、欠けた側は既定導出で埋める。dict でない
    (= 未宣言 / 壊れた値) なら丸ごと既定導出にフォールバックする。これにより明示
    宣言と live 導出のどちらでも同じ shape が返る。
    """
    base = default_master_binding(project)
    if not isinstance(value, dict):
        return base
    system = str(value.get(_SYSTEM_KEY) or base[_SYSTEM_KEY]).strip() or base[_SYSTEM_KEY]
    org_id = str(value.get(_ORG_ID_KEY) or base[_ORG_ID_KEY]).strip()
    return {_SYSTEM_KEY: system, _ORG_ID_KEY: org_id}


def resolve_master_binding(project: dict) -> dict:
    """project の実効マスター束縛を返す (明示宣言 > 既定導出)。I/O 無し・非破壊。"""
    return normalize_master_binding((project or {}).get(MASTER_BINDING_FIELD), project or {})


def ensure_master_binding(project: dict) -> dict:
    """未宣言なら既定束縛を project に書き込み、実効束縛を返す (lazy retrofit)。

    org.stamp_project_org と同型: 既に宣言があれば正規化して no-op、無ければ導出値を
    書き込む。呼び出し側が「書き込みたい時」にだけ使う (= 読むだけなら resolve で十分)。
    """
    resolved = resolve_master_binding(project)
    project[MASTER_BINDING_FIELD] = resolved
    return resolved


def set_master_binding(project: dict, *, system: str = "", org_id: str = "") -> dict:
    """マスター束縛を明示設定する (外部 CRM 接続や org 束縛の上書き用)。

    - `system`  : 空なら既存 / 既定を保つ。外部 CRM 接続時にその system 名を入れる。
    - `org_id`  : 空なら既存 / 導出値を保つ。明示指定で束縛軸を固定する。
    空指定は「その面は変えない」を意味し、両方空なら現在の実効束縛を書き込むだけ。
    """
    current = resolve_master_binding(project)
    new = {
        _SYSTEM_KEY: (system or "").strip() or current[_SYSTEM_KEY],
        _ORG_ID_KEY: (org_id or "").strip() or current[_ORG_ID_KEY],
    }
    if not new[_SYSTEM_KEY]:
        raise ValueError("master_binding.system cannot be empty")
    project[MASTER_BINDING_FIELD] = new
    return new


def is_beacon_default(project: dict) -> bool:
    """project が既定 (Beacon-default) マスターに束縛されているか (= 外部 CRM 未接続)。"""
    return resolve_master_binding(project)[_SYSTEM_KEY] == BEACON_DEFAULT_SYSTEM


def master_binding_system(project: dict) -> str:
    """実効マスター束縛の system 名。"""
    return resolve_master_binding(project)[_SYSTEM_KEY]


def master_binding_org_id(project: dict) -> str:
    """実効マスター束縛の org_id (= 束縛軸、SPEC §8)。所属未定なら ""。"""
    return resolve_master_binding(project)[_ORG_ID_KEY]
