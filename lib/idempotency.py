"""「書き込みを受けたが、既に在ったので何もしなかった」の開示を 1 箇所に寄せる層
(ms-166 e-6728)。

何が問題だったか
----------------
同じ意味の開示が **3 通りの名前** で割れていた。どれも「冪等な書き込みが no-op に
なった」ことを呼び出し側に伝えるフラグ:

===========================  ================================  =========================
名前                         どこが出すか                      何が重複の鍵か
===========================  ================================  =========================
``already_set``              受信記録のスタンプ                 (event_id, stage)
                             (server/firestore_client.py)      — 同じ段を二度押した
``idempotent_replay``        bus の送信 (server/app.py)        client_event_id
                             — 同じ送信を二度投げた
``deduplicated``             判断記録 (routers_projects.py)    内容 (what / 対象 / 時刻窓)
                             — 同じ判断を二度宣言した
===========================  ================================  =========================

3 つの違いは **何を鍵に重複と見なすか** だけで、呼び出し側が知りたいことは同じ
「私の書き込みは新しい行を作ったのか、既存の行を返しただけなのか」。にもかかわらず
名前が割れているので:

* 呼び出し側は 3 つの名前を覚えないと「no-op だったか」を判定できない
* 4 つ目のエンドポイントを足す人が、また別の名前を自作する余地がある
* どれを読めばよいかを引ける場所が無かった (この docstring がその 1 箇所)

どう寄せたか
------------
正規の名前は ``idempotent_no_op``。``disclose`` を通せば **正規名と従来名の両方** が
応答に載るので、既存の呼び出し側は壊れない (後方互換)。従来名は ``LEGACY_FLAGS`` に
登録してあり、**登録されていない新しい名前を使うとテストが落ちる**
(tests/test_idempotent_disclosure_single_shape_e6728.py)。

なぜ従来名を消さないか: 読み手は lib 側だけでなく、過去に書かれた外部の消費者
(``--json`` を読むスクリプト) にも居る。正規名を足すのは後方互換だが、従来名を消すのは
破壊的変更。**足すのは安全、消すのは別の判断** という線引きで、この課題は足す側だけを行う。
"""

from __future__ import annotations

# 正規の名前。新しいエンドポイントはこれだけを足せばよい。
CANONICAL = "idempotent_no_op"

# 従来名 → それを出す機構の説明。ここに無い名前を使うとテストが落ちる。
#
# 増やすときは「本当に別の概念か」を先に問うこと。3 つに割れた原因は、どれも
# 「既に在ったので何もしなかった」なのに、**鍵の違い** (段 / client_event_id / 内容) を
# 名前に織り込んでしまったことだった。鍵が違うだけなら名前は増やさない。
LEGACY_FLAGS = {
    "already_set": "受信記録のスタンプ: 同じ (event_id, stage) を二度押した",
    "idempotent_replay": "bus の送信: 同じ client_event_id を二度投げた",
    "deduplicated": "判断記録: 同じ内容 (what / 対象 / 時刻窓) を二度宣言した",
}

#: 「冪等 no-op の開示」として扱う名前の全体 (正規 + 従来)。
ALL_FLAGS = frozenset({CANONICAL} | set(LEGACY_FLAGS))


def disclose(payload: dict, *, no_op: bool, legacy: str = "") -> dict:
    """応答に冪等 no-op の開示を載せる (正規名 + 任意の従来名)。

    ``payload`` を変更して返す (呼び出し側が ``return disclose({...}, ...)`` と書ける)。

    ``no_op`` は **常に載せる** — true のときだけ生やすと、「false」と「このエンドポイント
    はそもそも開示しない」が区別できず、``"idempotent_no_op" in result`` で判定する
    コードが常に false に倒れる (判断記録の独立 AX レビュー AX-3 がこの形を指摘し、
    そこでは常時掲載に直してある。同じ原則をここに一般化する)。

    ``legacy`` を渡すとその名前にも同じ真偽値を載せる。既存の読み手を壊さないため。
    """
    if legacy and legacy not in LEGACY_FLAGS:
        raise ValueError(
            f"未登録の冪等 no-op フラグ名: {legacy!r}。"
            f"同じ意味の名前を増やす前に、LEGACY_FLAGS の表と "
            f"このモジュールの docstring を読んでください "
            f"(3 つに割れた原因は鍵の違いを名前に織り込んだことでした)")
    payload[CANONICAL] = bool(no_op)
    if legacy:
        payload[legacy] = bool(no_op)
    return payload


def was_no_op(result) -> bool:
    """応答が「既に在ったので何もしなかった」を示しているか。

    呼び出し側はこれ 1 つを読めばよい (3 つの名前を覚えなくてよい)。正規名が無い
    古い応答でも従来名から読めるので、サーバを先に更新しなくても動く。
    """
    if not isinstance(result, dict):
        return False
    if CANONICAL in result:
        return bool(result[CANONICAL])
    return any(bool(result.get(name)) for name in LEGACY_FLAGS)
