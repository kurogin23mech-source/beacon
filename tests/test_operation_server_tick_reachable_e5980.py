"""サーバ側の定期ティックに Operation を乗せる経路が **到達可能** であることの試験
(ms-166 e-5980)。

何が起きていたか
----------------
本番の定期ティック駆動 (``beacon-tick.timer`` → ``POST
/api/system/trek-scheduler/tick``) は ms-66 e-1391 で復旧しており、2026-10-14 時点で
生きている (``GET /api/system/tick-health`` が ``status=ok`` / uptime 約 5 日 /
``scheduled_fires`` 186 を返す)。つまり「駆動が止まっている疑い」は否定された。

残っていたのは **opt-in する経路が無い** ことだった。ティックは
``meta.server_tick`` を立てた Operation だけを発火対象にするが、それを立てる動詞
``operation_server_tick`` は

* bin/beacon に route されておらず (= macOS / Linux の人が到達できない)、
* どの Skill / hook / script からも呼ばれていなかった

ため、**「定期実行の Operation を open にしたのに何も起きない」** が起きる。動詞の
docstring は「内部専用: 有効化 setup 時に 1 回叩く」と書いていたが、その「叩く」人が
どこにも居なかった —— 配線はあるが動かない (= この MS の主題) の典型。

この file が留めるもの
----------------------
1. bash フロントから `beacon operation server-tick <op-id>` に到達できること
2. 到達できることが help の単一ソースにも載っていること (= 探せること)
3. 活性化の Skill (= 叩く主体) が実際にこの動詞を踏むこと、3 つの複製すべてで
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))


def _bash_source() -> str:
    with open(os.path.join(ROOT, "bin", "beacon"), encoding="utf-8") as f:
        return f.read()


def test_the_bash_front_routes_the_sub_verb():
    """`beacon operation server-tick` が bin/beacon の operation case に在ること。

    無いと macOS / Linux の利用者 (= 主要動線) がこの機能に到達できない。
    """
    src = _bash_source()
    start = src.index("\n    operation)")
    # 次の **トップレベルの case 枝** までを operation の case とみなす
    # (枝名を決め打ちすると、その枝が動いたとき試験だけが壊れる)。
    # operation) 行そのものを含めないよう、その行の末尾から探す。
    body_at = src.index("\n", start + 1) + 1
    nxt = re.search(r"^    [a-z][a-z0-9|_-]*\)", src[body_at:], re.M)
    operation_case = (src[body_at: body_at + nxt.start()] if nxt
                      else src[body_at:])
    assert re.search(r"^\s+server-tick\)", operation_case, re.M), (
        "operation の case に server-tick) が無い:\n" + operation_case[:400])


def test_the_sub_verb_passes_the_env_contract_the_python_side_reads():
    """bash が渡す env と python が読む env が一致すること。

    片方だけ直すと旗を受け取って黙って無視する形 (= silent no-op) になる。
    """
    src = _bash_source()
    case = src[src.index("            server-tick)"):]
    case = case[:case.index("operation_server_tick") + len("operation_server_tick")]
    import cmd_operation
    import inspect
    py = inspect.getsource(cmd_operation.cmd_operation_server_tick)
    for env in ("BEACON_OP_ID", "BEACON_SERVER_TICK", "BEACON_CADENCE"):
        assert env in case, f"bash が {env} を渡していない"
        assert env in py, f"python が {env} を読んでいない"


def test_running_it_without_an_op_id_explains_itself():
    """op-id 無しで叩いたら Usage を出して非 0 で終わること (黙って成功しない)。"""
    proc = subprocess.run(
        [os.path.join(ROOT, "bin", "beacon"), "operation", "server-tick"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "server-tick <op-id>" in combined, combined


def test_it_is_findable_in_the_single_help_source():
    """help の単一ソースに載っていること (= 探して見つかること)。"""
    import commands
    # **前方一致では測らない。** `startswith` 版は動詞名を `server-tick-x` に
    # 書き換えても通った (反転 U46 で実測) —— 前方一致は別の動詞名を同じものと
    # 見なす。語として取り出して集合で比べる。
    rows = [r for r in commands._help_registry()
            if r["command"].split()[:3] == ["beacon", "operation", "server-tick"]]
    assert len(rows) == 1, [r["command"] for r in commands._help_registry()
                            if "server" in r["command"]]
    flags = " ".join(rows[0]["flags"])
    assert "--on" in flags and "--off" in flags and "--cadence" in flags, flags


SKILL_COPIES = (
    "skills/beacon-operation-setup.md",
    "shared/skills/beacon-operation-setup/SKILL.md",
    "plugins/beacon/skills/beacon-operation-setup/SKILL.md",
)


@pytest.mark.parametrize("rel", SKILL_COPIES)
def test_the_activation_skill_actually_calls_it(rel):
    """**叩く主体** が実際に踏むこと。3 つの複製すべてで。

    動詞を route しただけでは、Operation を open にする人がそれを踏まない限り
    何も変わらない (= 配線はあるが動かない、この MS の主題)。複製を 1 つ忘れると
    install 元によって挙動が変わる (memory reference_skill_canonical_copy_filename)。
    """
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        body = f.read()
    # **実行するコマンドとして踏んでいるか**を見る。本文全体への部分一致で測った版は、
    # 散文側の言及 (「後から切るのは `... --off`」など) が残るだけで通った
    # (反転 U47 / U48 で実測)。囲み (```bash) の中だけを母集団にする。
    blocks = re.findall(r"```bash\n(.*?)```", body, re.S)
    assert blocks, f"{rel} に bash の囲みが無い"
    joined = "\n".join(blocks)
    assert "beacon operation server-tick" in joined, (
        f"{rel} が opt-in の動詞を **実行するコマンドとして** 踏んでいない "
        f"(散文で触れているだけでは Skill は叩かない)")
    # 発火しない状態を黙って作らないよう、駆動の生死を確認する経路も案内すること
    assert "tick-health" in joined, (
        f"{rel} が駆動の生死 (tick-health) の確認を実行するコマンドとして"
        f"持っていない")
