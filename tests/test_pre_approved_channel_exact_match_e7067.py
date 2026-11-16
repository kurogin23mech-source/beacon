"""事前承認済み channel の判定が名前の前方一致ではなく明示の一覧であることを測る
(ms-185 e-7067)。

**なぜこのファイルが要るか**: 「この channel への送信は人間の確認を省いてよい」の
判定が、以前は ``channel.startswith("trek-")`` の**前方一致**だった。前方一致は
「``trek-`` で始まる名前を付けた channel なら、誰の承認も無いのに確認を省ける」
経路で、**裏付け (= 人間の承認) と判定が切り離されている**。

Trek を畳む (ms-185) ときに一覧から ``trek-*`` を外すだけでは、この前方一致が
残る限り穴は閉じない。しかも同じ一覧に ``operation-trigger`` が同居しているので、
一覧ごと消すと Operation の事前承認が落ちる (= 逆側に壊れる)。つまり **両方を
同時に見ないと正しさが決まらない**ので、1 ファイルで両側を固定する。

固定するのは 4 点:
  1. 任意の名前が前方一致で事前承認済みにならない (両実装)。
  2. ``operation-trigger`` の確認省略は**維持**されている (両実装・両経路)。
  3. 一覧の中身が Python と JS で一致している (= 片方だけ足される drift の防止)。
  4. 引退した判定理由の文字列が、過去の監査記録のために読めるまま残っている。

ここは**振る舞い**を測る。定数名や文字列の存在検査ではない (配線を逆にしても
名前は全部残るので、文字列検査は偽の緑になる — ms-185 で実際に踏んだ型)。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import dm_consent  # noqa: E402
import dm_qualgate  # noqa: E402

QUALGATE_MJS = REPO / "channel" / "bus-qualgate.mjs"

# 前方一致が復活したら当たる名前。「それらしい接頭辞を付けただけ」で
# 事前承認済みになってはいけない、という形を名前で表している。
PREFIXED_IMPOSTORS = [
    "trek-trigger",            # 実在した channel 名
    "trek-anything",           # 任意の名前 (前方一致の本体)
    "trek-",                   # 接頭辞だけ
    "operation-trigger-extra",  # 許可名の前方一致 (逆向きの緩さ)
]

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not available — JS gate side is Node-only",
)


def _js_is_pre_approved(channel: str) -> bool:
    script = textwrap.dedent(f"""
        import {{ isPreApprovedChannel }} from '{QUALGATE_MJS.as_posix()}'
        process.stdout.write(JSON.stringify(isPreApprovedChannel(process.argv[1])))
    """)
    proc = subprocess.run(["node", "--input-type=module", "-e", script, "--", channel],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


def _js_pre_approved_list() -> list:
    script = textwrap.dedent(f"""
        import {{ PRE_APPROVED_CHANNELS }} from '{QUALGATE_MJS.as_posix()}'
        process.stdout.write(JSON.stringify([...PRE_APPROVED_CHANNELS].sort()))
    """)
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr!r}")
    return json.loads(proc.stdout)


# --- 1. 前方一致で事前承認済みにならない --------------------------------

@pytest.mark.parametrize("channel", PREFIXED_IMPOSTORS)
def test_prefixed_name_is_not_pre_approved_python(channel):
    assert dm_qualgate.is_pre_approved_channel(channel) is False, (
        f"{channel!r} が事前承認済みと判定された — 名前の前方一致が復活している")


@needs_node
@pytest.mark.parametrize("channel", PREFIXED_IMPOSTORS)
def test_prefixed_name_is_not_pre_approved_js(channel):
    assert _js_is_pre_approved(channel) is False, (
        f"{channel!r} が JS 側で事前承認済みと判定された — 前方一致が復活している")


@pytest.mark.parametrize("channel", PREFIXED_IMPOSTORS)
def test_prefixed_name_is_held_by_the_armed_qual_gate(channel):
    """armed なセッションの自律送信は、別プロジェクト宛なら保留に倒れる。

    経路 (b): classify_outbound_reply → evaluate_outbound_qual_gate(armed=True)。
    """
    cat = dm_qualgate.classify_outbound_reply(
        channel=channel, sender_project_id="p1", recipient_project_id="p2")
    gate = dm_qualgate.evaluate_outbound_qual_gate(cat, armed=True)
    assert cat == dm_qualgate.CAT_EXTERNAL
    assert gate["hold"] is True


@pytest.mark.parametrize("channel", PREFIXED_IMPOSTORS)
def test_prefixed_name_is_not_exempted_as_pre_approved_on_the_send_path(channel):
    """経路 (a): 送信側の確認免除が「事前承認済み」を根拠にしない。

    **注意して読むこと。** この経路では「確認が必要になる」とは書けない —
    ``dm`` 以外の channel は *もともと別の根拠* (特定の相手に向けた送信では
    ない = ``non_dm_channel``) で免除されるからで、その規則は Trek とは無関係に
    前から在る。ここで固定するのは **免除の根拠が事前承認済みでないこと**。
    根拠が変わることに意味がある: 監査記録に「Trek の事前承認で免除した」と
    残らなくなり、一覧に名前を足す以外に免除を得る方法が無くなる。
    """
    _required, reason = dm_consent.classify_send_consent(
        sender_user_id="alice@example.com",
        recipient_user_id="bob@example.com",
        channel=channel,
    )
    assert reason != dm_consent.CONSENT_SKIP_PRE_APPROVED


# --- 2. operation-trigger の免除は維持する (逆側に壊していない) ----------

def test_operation_trigger_still_skips_consent_on_the_send_path():
    """一覧ごと消すと Operation の事前承認が落ちる — その逆側をここで固定する。"""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="alice@example.com",
        recipient_user_id="bob@example.com",
        channel="operation-trigger",
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_PRE_APPROVED


def test_operation_trigger_still_passes_the_armed_qual_gate():
    cat = dm_qualgate.classify_outbound_reply(
        channel="operation-trigger", sender_project_id="p1",
        recipient_project_id="p2")
    assert cat == dm_qualgate.CAT_NORMAL
    assert dm_qualgate.evaluate_outbound_qual_gate(cat, armed=True)["hold"] is False


@needs_node
def test_operation_trigger_still_pre_approved_js():
    assert _js_is_pre_approved("operation-trigger") is True


# --- 3. 一覧の中身が両実装で一致する ------------------------------------

@needs_node
def test_the_two_implementations_hold_the_same_list():
    """片方だけに channel を足す drift を捕まえる。

    振る舞いの一致は test_dm_qualgate_parity.py が入力を揃えて測るが、それは
    **試験が列挙した入力**しか見ない。一覧そのものを比べると、どちらかに足され
    た未知の名前も捕まる。
    """
    assert sorted(dm_qualgate.PRE_APPROVED_CHANNELS) == _js_pre_approved_list()


def test_the_list_is_exactly_operation_trigger():
    """事前承認済みは operation-trigger だけ (e-7067 の受入条件)。

    増やすときは「誰のどの承認で事前承認済みと言えるか」を 1 行書けること。
    書けない channel をここに足すと、確認を省く根拠の無い経路が増える。
    """
    assert set(dm_qualgate.PRE_APPROVED_CHANNELS) == {"operation-trigger"}


# --- 4. 引退した判定理由は読めるまま残す --------------------------------

def test_retired_reason_string_is_still_explainable():
    """過去の監査記録に残る文字列が意味を失わない。

    判定理由は ``audit_record["sender_consent"]["reason"]`` として**永続化される**。
    規則を消したら、その規則で免除された過去の記録が読めなくなる — 記録は在る
    のに製品のどの面からも意味が引けない状態で、監査可能性そのものが落ちる。
    """
    out = dm_consent.explain_consent_reason("trek_scoped_channel")
    assert "引退" in out
    assert "未登録" not in out, "引退した理由が『説明が未登録』に落ちている"


def test_retired_vocabulary_has_no_live_constant():
    """引退側に定数を残さない (= 誰もセットしない判定を作らない)。"""
    live = {v for k, v in vars(dm_consent).items()
            if k.startswith("CONSENT_SKIP_") or k.startswith("CONSENT_REQUIRED_")}
    overlap = set(dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS) & live
    assert not overlap, f"引退させた理由が定数として生きている: {sorted(overlap)}"
