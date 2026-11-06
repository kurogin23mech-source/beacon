"""自律 channel の 3 箇所一致を固定する (ms-185 e-7158)。

自律で動く channel を 1 つ増やす / 引退させるには、**3 つの場所**を揃えて直す必要が
ある。どれか 1 つを忘れても、これまでは全テストが緑のまま通った:

  1. ``lib/bus_delivery.GATED_PROVENANCE_CHANNELS`` — 証拠 (= 人間の承認) を要求する
     channel の集合。``PROVENANCE_REQUIREMENTS`` に要求の中身が在る。
  2. ``channel/bus-autonomous-content.mjs`` の ``CHANNEL_TO_SKILL`` — **指示の
     振り分け先**。受信した event を「この Skill を今すぐ起動せよ」に翻訳する表。
  3. ``channel/bus-qualgate.mjs`` の ``PRE_APPROVED_CHANNELS`` と、その Python 鏡
     ``lib/dm_qualgate.PRE_APPROVED_CHANNELS`` — 送信時に人間の確認を省いてよい一覧。

**忘れたときに何が起きるか**が場所ごとに違い、どれも静かである:

  * 1 に足して 2 を忘れる → 証拠は要求されるが、受信した AI には起動すべき Skill が
    無い。「自律実行せよ」と言われて当て先が無い状態 (= e-7079 で実際に踏んだ形:
    解決できないコマンドを命じる inject は、inject が無いより悪い)。
  * 1 に足して 3 を忘れる → 受信は通るのに、その channel への送信が毎回人間の確認を
    要求する (= 自律の片側だけが動く)。
  * 1 から外して 2 / 3 に残す (= 引退方向) → 証拠を要求されなくなった channel に
    対して、振り分け先と事前承認だけが生き残る。**危険側に壊れる方向**。

``test_gated_channels_match_their_requirements`` は 1 の内部整合 (集合 ↔ 要求) しか
見ておらず、2 と 3 は誰も突き合わせていなかった。

**node が無い環境でも skip しない。** node が在れば実 module を import して (= export
名の改名や消失もここで落ちる)、無ければソースから構造抽出して同じ比較をする。
緑と同じ害を持つ SKIP を置かないため。受信側 node の試験を CI に載せる話は e-7352。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

AUTONOMOUS_MJS = REPO / "channel" / "bus-autonomous-content.mjs"
QUALGATE_MJS = REPO / "channel" / "bus-qualgate.mjs"


def _js_via_node() -> dict | None:
    """実 module を Node で import して 2 つの列挙を取り出す (取れなければ None)。"""
    if shutil.which("node") is None:
        return None
    script = textwrap.dedent(f"""
        const a = await import({json.dumps(AUTONOMOUS_MJS.as_uri())})
        const q = await import({json.dumps(QUALGATE_MJS.as_uri())})
        process.stdout.write(JSON.stringify({{
          channel_to_skill: Object.keys(a.CHANNEL_TO_SKILL).sort(),
          pre_approved: [...q.PRE_APPROVED_CHANNELS].sort(),
        }}))
    """)
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout)


def _js_via_source() -> dict:
    """node 無しでも同じ比較をするための構造抽出 (fallback、skip しないため)。"""
    a = AUTONOMOUS_MJS.read_text(encoding="utf-8")
    q = QUALGATE_MJS.read_text(encoding="utf-8")

    m = re.search(r"export const CHANNEL_TO_SKILL\s*=\s*Object\.freeze\(\{(.*?)\}\)",
                  a, re.DOTALL)
    assert m, "CHANNEL_TO_SKILL の宣言が見つからない (改名/削除された?)"
    skill_keys = sorted(re.findall(r"'([^']+)'\s*:", m.group(1)))

    m = re.search(r"export const PRE_APPROVED_CHANNELS\s*=\s*new Set\(\[(.*?)\]\)",
                  q, re.DOTALL)
    assert m, "PRE_APPROVED_CHANNELS の宣言が見つからない (改名/削除された?)"
    pre_keys = sorted(re.findall(r"'([^']+)'", m.group(1)))

    return {"channel_to_skill": skill_keys, "pre_approved": pre_keys}


def _js_enums() -> dict:
    return _js_via_node() or _js_via_source()


def test_every_gated_channel_has_a_dispatch_target():
    """ゲート集合の channel には、必ず指示の振り分け先が在る。

    足し忘れると「自律実行せよ」と言われて起動先が無い状態になる。
    """
    import bus_delivery as bd
    js = _js_enums()
    missing = sorted(set(bd.GATED_PROVENANCE_CHANNELS) - set(js["channel_to_skill"]))
    assert not missing, (
        f"証拠を要求する channel に振り分け先が無い: {missing}。"
        f"channel/bus-autonomous-content.mjs の CHANNEL_TO_SKILL に"
        f"起動する Skill を足すこと (当て先の無い自律指示は、指示が無いより悪い)"
    )


def test_no_dispatch_target_without_a_gate():
    """振り分け先が在る channel は、必ずゲート集合にも在る (引退方向の穴)。

    集合から外して振り分け先だけ残すと、証拠を要求されない channel に対して
    自律起動の経路が生き残る = 危険側に壊れる。
    """
    import bus_delivery as bd
    js = _js_enums()
    orphan = sorted(set(js["channel_to_skill"])
                    - set(bd.GATED_PROVENANCE_CHANNELS)
                    - set(bd.RETIRED_PROVENANCE_CHANNELS))
    assert not orphan, (
        f"ゲートも引退登録も無い channel に振り分け先が在る: {orphan}。"
        f"証拠を要求しないまま自律起動できる経路になっている"
    )


def test_pre_approved_lists_agree_across_the_two_languages():
    """送信側の事前承認一覧が JS と Python で一致する。"""
    import dm_qualgate as dq
    js = _js_enums()
    assert js["pre_approved"] == sorted(dq.PRE_APPROVED_CHANNELS), (
        f"事前承認一覧がズレている: JS={js['pre_approved']} / "
        f"Python={sorted(dq.PRE_APPROVED_CHANNELS)}。"
        f"channel/bus-qualgate.mjs と lib/dm_qualgate.py は lockstep"
    )


def test_the_three_places_name_the_same_channels():
    """ゲート集合 / 振り分け先 / 事前承認 の 3 つが同じ集合を指す。

    個別の 3 本が通っても、3 つを同時に見ないと「2 箇所だけ足した」形が
    残りうる組み合わせがある (例: 振り分け先と事前承認にだけ足す)。
    """
    import bus_delivery as bd
    import dm_qualgate as dq
    js = _js_enums()
    gated = set(bd.GATED_PROVENANCE_CHANNELS)
    assert set(js["channel_to_skill"]) == gated, (
        f"振り分け先 {sorted(js['channel_to_skill'])} と "
        f"ゲート集合 {sorted(gated)} が一致しない")
    assert set(js["pre_approved"]) == gated, (
        f"事前承認 {sorted(js['pre_approved'])} と "
        f"ゲート集合 {sorted(gated)} が一致しない")
    assert set(dq.PRE_APPROVED_CHANNELS) == gated, (
        f"Python 側の事前承認 {sorted(dq.PRE_APPROVED_CHANNELS)} と "
        f"ゲート集合 {sorted(gated)} が一致しない")


def test_both_extraction_paths_agree_when_node_is_available():
    """node 経由とソース抽出が同じ答えを出す (fallback が嘘をつかない)。

    fallback は node 不在時にしか使われないので、普段は誰も確かめない。
    node が在る環境では両方走らせて突き合わせる。
    """
    via_node = _js_via_node()
    if via_node is None:
        return  # node が無い環境では比較できない (この 1 件だけは成立しない)
    assert via_node == _js_via_source(), (
        "node 経由とソース抽出の結果が違う — fallback の抽出規則が実態と"
        "ズレている (node 不在の CI で嘘の緑を出す)")
