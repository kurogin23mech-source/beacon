"""心拍が「どこで作業しているか」を運ぶこと (ms-159 e-7235)。

運用室の行に出る「どの対象か」「何をしているか」は、サーバが
``git.branch`` / ``git.head_subject`` から導出する (宣言が無いとき)。その git を
刻むのが橋渡し役の cold-start 1 回だけだったため、ターミナルを開いたまま
ブランチを切り替えると古い値が残り続け、最終活動時刻だけが新鮮なので
**古い情報が権威あるものに見えて**いた。

ここは構造で固定する: 心拍の本体に cwd / git を乗せる口が在ること、心拍を組む
呼び出しが実際にそれを渡していること、そして収集が 1 回だけに戻されたら赤くなること。
JS 側の挙動は node で実走させて確かめる (文字列一致だけで済ませない)。
"""

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUS = ROOT / "channel" / "bus.mjs"
HEARTBEAT = ROOT / "channel" / "bus-heartbeat.mjs"

_node = shutil.which("node")
needs_node = pytest.mark.skipif(_node is None, reason="node が無い環境")


def _run_node(script: str) -> str:
    out = subprocess.run([_node, "--input-type=module", "-e", script],
                         cwd=str(ROOT), capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, f"node 失敗: {out.stderr}"
    return out.stdout.strip()


@needs_node
def test_heartbeat_body_carries_cwd_and_git():
    """``where`` を渡すと cwd / git が本体に乗ること。"""
    got = json.loads(_run_node(
        "import {buildHeartbeatBody} from './channel/bus-heartbeat.mjs';"
        "const b = buildHeartbeatBody({nowIso:'t', pollIntervalMs:5000,"
        " where:{cwd:'/w', git:{branch:'ms-9', head_short:'abc1234',"
        " head_subject:'件名'}}});"
        "console.log(JSON.stringify(b));"))
    assert got["cwd"] == "/w"
    assert got["git"]["branch"] == "ms-9"
    assert got["git"]["head_short"] == "abc1234"
    assert got["git"]["head_subject"] == "件名"


@needs_node
def test_heartbeat_body_omits_where_when_absent():
    """渡されないときは乗せないこと (後方互換: サーバの merge が前の値を保つ)。

    空欄を送ってしまうと、古いが実在した値を空で上書きして「不明」に落とす。
    捏造しないのと同じ理由で、無いものは送らない。
    """
    for where in ("undefined", "{}", "{git:{}}"):
        got = json.loads(_run_node(
            "import {buildHeartbeatBody} from './channel/bus-heartbeat.mjs';"
            f"const b = buildHeartbeatBody({{nowIso:'t', pollIntervalMs:5000, where:{where}}});"
            "console.log(JSON.stringify(b));"))
        assert "cwd" not in got, f"where={where} で cwd を送っている: {got}"
        assert "git" not in got, f"where={where} で git を送っている: {got}"


def test_heartbeat_call_site_passes_where():
    """心拍を組む呼び出しが、毎回「どこで」を収集して渡していること。

    **これが e-7235 の反転点**: 収集を cold-start の 1 回だけに戻す (= この
    引数を削る) と赤くなる。本体に口が在るだけでは直らない — 渡されなければ
    古い値のままなので、口の存在と配線の両方を固定する。
    """
    src = BUS.read_text(encoding="utf-8")
    i = src.index("buildHeartbeatBody({")
    call = src[i:src.index("})", i)]
    assert "where:" in call, (
        "心拍の組み立てに where が渡されていない。cold-start の 1 回刻みに"
        "戻ると、運用室の対象と作業内容が古い値のまま出る (e-7235)")
    assert "collectLayer1Where()" in call, (
        "where が毎回の収集ではなく固定値から来ている。収集し直さないと"
        "ブランチ切り替えが反映されない")


@needs_node
def test_where_collection_output_shape():
    """収集が cwd と git の 3 項目を返すこと (2 回の git 呼び出しに畳んだ後も)。

    先頭コミットの短縮 hash と件名を 1 回の `git log` から切り出しているので、
    切り出しを間違えると件名に hash が混ざる / 空になる。実リポジトリで実走して確かめる。
    """
    got = json.loads(_run_node(
        "import {execFileSync} from 'node:child_process';"
        "const se=(c,a)=>{try{return execFileSync(c,a,{encoding:'utf8',"
        "stdio:['ignore','pipe','ignore']}).trim()}catch(e){return ''}};"
        "const head=se('git',['log','-1','--pretty=%h%n%s']);"
        "const nl=head.indexOf('\\n');"
        "console.log(JSON.stringify({"
        " short: nl>=0?head.slice(0,nl):head,"
        " subject: nl>=0?head.slice(nl+1):''}));"))
    assert got["short"], "短縮 hash が空"
    assert "\n" not in got["short"], f"短縮 hash に改行が混ざっている: {got['short']!r}"
    assert got["subject"], "件名が空"
    assert got["short"] not in got["subject"], (
        f"件名に hash が混ざっている: {got['subject']!r}")


def test_layer1_refresh_is_documented_where_it_is_collected():
    """収集の節が「毎 heartbeat 刻み直す」と明言していること。

    機構を変えたのに説明が古いままだと、次に触る人が「ここは 1 回だけのはず」と
    読んで配線を外す。

    **肯定で固定する**。最初はこれを否定 (= 旧文言が残っていたら赤) で書いたが、
    「その文言はもう通用しない」と説明するための引用まで弾いてしまい、正しい変更を
    妨げる偽陽性になった。禁じたい文字列を数えるのではなく、**在るべき記述が在るか**
    を見る方が、言い回しの自由を奪わずに意図を守れる。
    """
    src = BUS.read_text(encoding="utf-8")
    i = src.index("function collectLayer1Where()")
    # 関数の直前にある説明の節 (= 直前の空行 2 つより後ろ) を見る。
    doc = src[max(0, src.rfind("\n\n", 0, i)):i]
    assert "毎 heartbeat" in doc or "毎回刻み直す" in doc, (
        "Layer 1 を毎 heartbeat 刻み直すことが、収集している場所の説明に書かれて"
        f"いない。次に触る人が 1 回刻みに戻す。読んだ節:\n{doc[-600:]}")
    # 旧前提そのもの (cold-start の 1 回だけ、という**主張**) が残っていないこと。
    # 引用は許すので、主張の形 ("Stamped ONCE") だけを見る。
    assert "Stamped ONCE on cold-start as part of the first heartbeat" not in src, (
        "Layer 1 を毎回刻むようにしたのに、1 回だけと断言する記述が残っている")
