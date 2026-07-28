"""lib/backend_authoring.py — ms-114 ↔ ms-113 接続 seam (e-3745).

サーバサイドの backend authoring (= report を受けて正準ミューテーションを author する
サーバ側の実行) は、宣言した *work-unit scope* (= この 1 作業単位で従事すると宣言した
project 集合) の下でだけ動く。本モジュールは、report seam (``report_primitive.
apply_report``、e-3741/e-3742/e-3743) を principal のスコープ模型 (``principal.
backend_work_unit_effective_scope``、ms-113) に **接続する唯一の場所**。

なぜ独立モジュールか:
  - ``report_primitive`` は依存ゼロ (stdlib のみ) に保つ — authoring が server / network
    を必要としない不変条件 (AC6、test_report_seam_local_fallback が forcing function)。
  - ``principal`` は純粋な集合代数 (誰がどの project に触れてよいか) に保つ。
  - この 2 つを **合成** する層 (両方を import する) がここ。責務が別なので混ぜない。

背骨 + 可搬ヘッド (SPEC 方針2) の具現化: backend principal が「宣言した work-unit を
天井に、呼び出し元 principal・service grant のいずれよりも広くならない」実効スコープを
持ち、その scope の中の project にだけ report を author できる。強い backend を踏み台に
した越境 (= 混乱した代理人) は ``backend_work_unit_effective_scope`` の 3 者 min で
構造的に塞がれ、本モジュールはその実効スコープを ``apply_report`` の gate へ渡すだけ。

本モジュールは薄い glue のみ (I/O なし)。service identity を「どこに登録し・どう保存
するか」や、report の target project をどう解決するかは server 側の責務。
"""

from __future__ import annotations

import principal
import report_primitive


def apply_report_as_backend(project_data: dict, report: dict, *, rules: dict,
                            seen: dict, service_identity: dict,
                            work_unit_scope, originating_scope,
                            target_project_id: str) -> dict:
    """Author a report AS a backend principal, gated by its work-unit effective
    scope — the one call ms-114 server-side authoring makes to run under ms-113.

    Computes the backend work-unit's effective scope via
    ``principal.backend_work_unit_effective_scope`` (= min of the service grant
    ceiling, the declared ``work_unit_scope``, and the originating principal's
    effective scope) and hands it to ``report_primitive.apply_report`` as the
    ``authorizing_scope``. If ``target_project_id`` is outside that scope, the
    report is refused (``status="out_of_scope"``) and the authoring rule never
    runs — a backend can't be tricked into authoring beyond its declared
    work-unit (confused-deputy defense).

    Arguments mirror the two seams they bridge:
      - ``service_identity``  : ``principal.make_service_identity`` result (the
        grant ceiling); malformed values raise ValueError at that layer's entry.
      - ``work_unit_scope``   : project scope declared for this authoring unit.
      - ``originating_scope`` : the calling principal's *effective* scope (client
        → ``client_effective_scope``; an upstream backend → its own
        ``backend_work_unit_effective_scope`` result).
      - ``target_project_id`` : the project this report authors into (the server
        knows which project it loaded).

    Returns whatever ``apply_report`` returns (``authored`` / ``out_of_scope`` /
    ``duplicate`` / ``no_rule`` / ``invalid``). Pure composition — no I/O."""
    effective = principal.backend_work_unit_effective_scope(
        service_identity, work_unit_scope, originating_scope)
    return report_primitive.apply_report(
        project_data, report, rules=rules, seen=seen,
        authorizing_scope=effective, target_project_id=target_project_id)
