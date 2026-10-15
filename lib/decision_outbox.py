#!/usr/bin/env python3
"""decision_outbox.py — 純粋なデータ層 seam が decision を機械発行するための outbox
(ms-166 e-6599)。

## なぜ要るか

判断 (judgement) の多くは **純粋なデータ層の関数**で起きる。営業の商談ゲートを閉じる
``sales_entities.settle_gate`` がその代表で、advance / retry / terminal / jump の全 4
遷移がここを通る (= 判断の唯一の漏斗)。だが decision arm への書き込みはネットワーク
I/O なので、その関数の中で直接叩くと

* 純関数性が壊れる (data を受け取り data を返すだけ、という層の契約が崩れる)、
* テストが cloud を要求するようになる、
* 保存されなかった (roll back された) 変更に対しても decision が発行されうる

という 3 つの害が出る。逆に cmd 層 (CLI handler) で叩くと、``cmd_opportunity_phase``
→ ``jump_transition`` のように **handler を経由しない経路が checker から不可視**になり、
ms-174 で実際に起きた jump-bypass (= 1 経路だけ塞いで「構造で閉じた」と誤称する) を
再生産する。

そこで **stage (積む) と flush (書く) を分ける**:

* データ層 seam は :func:`stage` で「この判断を記録したい」という意図だけを積む
  (プロセス内、I/O なし、data は汚さない)。
* 保存 seam (``commands_shared.save_project``) が :func:`flush` で実際に書く。

## 発行されるのは「実際に保存された判断」だけ

:func:`stage` は ``verify`` (= ``(data) -> bool`` の述語) を一緒に受け取る。 :func:`flush`
は **保存された直後の data** に対してその述語を評価し、True のものしか書かない。これで

* 途中で例外が出て保存に至らなかった遷移 → そもそも flush が呼ばれない、
* 保存はされたが当該 seam の変更が data に残っていない → 述語が False で落ちる

の両方が構造的に閉じる (= 「判断したことになっているが商談は動いていない」記録が
物理的に作れない)。本 module は生成物 (= decision payload) の中身を一切知らない汎用の
器なので、営業に限らずどの target class の pure seam からでも同じ型で使える。

stdlib のみの leaf。``commands_shared`` は :func:`flush` の中で遅延 import するので、
data 層 (``sales_entities`` 等) が本 module を import しても依存環が出来ない。
"""
from __future__ import annotations

from typing import Callable, Optional

# ms-166 e-6599: decision kind。判断 (gate judgement) = 「この対象を次のどの状態へ
# 倒すか」を人間が決めた記録。完遂 (completion-verdict) とは別族 — advance / retry は
# 完遂ではないし、terminal の完遂発火は target_completion が別に持つ (独立 judge の
# 語彙混同 finding、memo doc yHbXoyoe2X6qNcu2tJaf)。
GATE_JUDGEMENT_KIND = "gate-judgement"

# (payload, verify) の待ち行列。プロセス内 (= CLI 1 実行分) のみ生存する。
_staged: list = []

# ms-166 e-6997 (独立 AX / 保守性レビューが **独立に同じ high** を挙げた):
# 積んだのに flush されない経路があると、待ち行列が **無言で伸び続ける**。
#
# 実測された形: server の ``POST /entries/{id}/done`` は ``core.task_done`` を呼ぶが、
# 保存は ``operations.apply_operation`` 経由で ``commands_shared.save_project`` を
# 通らないので ``flush`` が一度も走らない。server は自前で decision を書いている
# ので記録自体は在り、**症状はメモリだけが伸びること** だった = 例外もログも出ない。
# 長寿命プロセス (Cloud Run) では遅延障害になる。
#
# そこで待ち行列に **上限** を置き、超えたら「flush されない経路から積まれている」と
# 述べて古い方から捨てる。黙って伸びるより、うるさく捨てる方が良い (記録は監査の
# 副作用で、呼び出し元のフローより優先しない)。この上限は 1 コマンドで現実的に
# 積まれる件数 (PR 承認が複数タスクを閉じる形でも 10 件程度) より十分大きい。
_STAGE_LIMIT = 64
_overflow_disclosed = False


def stage(payload: dict, *, verify: Optional[Callable[[dict], bool]]) -> dict:
    """decision の発行意図を積む (I/O なし)。積んだ payload をそのまま返す。

    ``payload`` は ``client.record_decision`` にそのまま渡せる body
    (kind / decision / rationale / decided_by / evidence / related / context)。
    ``who`` は server が token + session header から刻むので **呼び出し側は載せない**。

    ``verify`` は「保存された data にこの判断の結果が実在するか」を答える述語で、
    **既定値を持たない必須キーワード引数**。この module の存在意義は「保存された判断
    だけを書く」ことなので、その保証を docstring のお願いに委ねない (ms-166 e-6601
    AX-3): 省略できると、次に別の seam を足す者が無意識に検証なしで発行でき、この
    module が「構造的に不可能にした」と称する病理 (= 判断したことになっているが対象は
    動いていない記録) をそのまま再生産できてしまう。検証できない形の seam は
    ``verify=None`` を **明示的に** 渡す — 省略と同じ挙動でも、書いた本人が「検証を
    付けられない」と一度意識する分だけ構造的になる。
    """
    if not isinstance(payload, dict):
        raise TypeError("decision payload must be a dict")
    if not (payload.get("kind") or "").strip():
        raise ValueError("decision payload requires a non-empty 'kind'")
    if not (payload.get("decision") or "").strip():
        raise ValueError("decision payload requires a non-empty 'decision'")
    _staged.append((dict(payload), verify))
    _warn_if_never_flushed()
    return payload


def _warn_if_never_flushed() -> None:
    """待ち行列が上限を超えたら、述べてから古い方を捨てる (ms-166 e-6997)。

    「flush を呼ぶはず」というお願いに依存しないためのトリップワイヤー。超えた時点で
    その process は **積むが書かない経路** を持っていると分かるので、それを名指しする
    (黙って伸ばさない / 無制限に伸ばさない)。
    """
    global _overflow_disclosed
    if len(_staged) <= _STAGE_LIMIT:
        return
    dropped = len(_staged) - _STAGE_LIMIT
    del _staged[:dropped]
    if _overflow_disclosed:
        return
    _overflow_disclosed = True
    import sys
    print(f"[beacon] warning: 判断記録の待ち行列が上限 {_STAGE_LIMIT} 件を超えました "
          f"— この実行経路は decision を積んでいますが flush していません "
          f"(flush は commands_shared.save_project が行います)。古い {dropped} 件を "
          f"捨てました。server のように別の保存経路を持つ呼び出し元は、積まずに "
          f"自分の seam で記録してください。", file=sys.stderr)


def staged() -> tuple:
    """積まれている (payload, verify) の tuple — テスト / 内省用の読み取り。"""
    return tuple(_staged)


def discard() -> int:
    """積まれた意図を書かずに捨て、捨てた件数を返す (テストの後片付け用)。"""
    global _overflow_disclosed
    n = len(_staged)
    _staged.clear()
    _overflow_disclosed = False
    return n


def decided_by_for_actor(actor: str) -> str:
    """seam が記録した ``actor`` から ``decided_by`` を機械導出する (固定文字列にしない)。

    AC「decided_by が settle actor から導出される」の実体。写像:

    * ``human`` / ``human:<...>`` (= ``commands._human_actor`` が刻む人間 master)
      → 端末が人間宣言なら ``human-delegated``、AI セッションが人間の確認を適用した
      形なら ``AI-proposed-human-chose`` (``cmd_target._decided_by_for_gate`` と同じ写像)。
    * それ以外 (機械 actor / 空)
      → ``autonomous-AI`` (= 人間が見ていない判断。最も audit-critical な側に倒す)。

    human 側の写像そのものは ``commands_shared.decided_by_for_gate`` が単一真実源
    (ms-166 e-6601 保守性 M-1)。ここが持つのは **actor から人間かどうかを読む部分だけ**
    で、「人間所有のゲート判断をどの語彙に倒すか」は共有 leaf に委ねる (旧: 同じ写像を
    ここに 3 つ目として独立実装しており、decided_by の意味論を変える者が 3 箇所を手で
    探す必要があった)。
    """
    a = (actor or "").strip().lower()
    if not (a == "human" or a.startswith("human:")):
        return "autonomous-AI"
    try:
        from commands_shared import decided_by_for_gate
    except Exception:
        # 共有 leaf に届かない (= import 環境が整っていない) ときは、人間確認を
        # 前提にした側へ倒す。ゲート判断は人間所有なので AI 単独扱いにはしない。
        return "AI-proposed-human-chose"
    return decided_by_for_gate()


def flush(data: dict) -> int:
    """保存直後に呼ばれ、検証を通った意図だけを decision arm に書く。書いた件数を返す。

    待ち行列は **成否に関わらず必ず空にする** (再試行しない): decision は監査の副作用
    であって、呼び出し元のフローを壊さないのが契約。書き込み失敗は
    ``commands_shared.best_effort_decision_write`` が WARNING で可視化する (silent に
    飲まない — ms-166 e-5978)。local mode / 未ログインでは 0 件で終わる。
    """
    pending = list(_staged)
    _staged.clear()
    if not pending:
        return 0

    survivors = []
    for payload, verify in pending:
        if verify is None:
            survivors.append(payload)
            continue
        try:
            if verify(data):
                survivors.append(payload)
        except Exception:
            # 述語が壊れている = 検証できない。書かない側に倒す (誤記録より欠落)。
            continue
    if not survivors:
        return 0

    try:
        from commands_shared import (best_effort_decision_write, _is_cloud_mode,
                                     _get_api_client)
    except Exception:
        return 0
    if not _is_cloud_mode():
        return 0

    written = 0
    for payload in survivors:
        # ms-166 e-6997: 失敗の警告に **どの対象の判断か** を載せる。初版は
        # ``related.target_id`` だけを読んでいたので、task の判断 (= related.task_id)
        # は `target=?` になり、「どの task の done が記録できなかったか」が分からな
        # かった (= 復旧できない警告)。related が運ぶ実際の鍵から拾う。
        _rel = payload.get("related") or {}
        _ref = next((f"{k}={_rel[k]}" for k in sorted(_rel) if _rel.get(k)), "ref=?")
        label = (f"{payload.get('kind')} for {_ref} "
                 f"decision={payload.get('decision')}")
        with best_effort_decision_write(
                label,
                recovery_hint="the state change itself is committed — do not re-apply"):
            client, config = _get_api_client()
            project_id = config.get("project_id", "")
            if not project_id:
                continue
            client.record_decision(project_id, payload)
            written += 1
    return written
