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


# --- 2. 名乗りと実態を合わせる (独立 AX レビュー high 2 件 + medium 1 件) -------

def _op_project(tmp_path, *ops):
    import json
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": "t", "profession": "dev", "next_entry_id": 1,
        "milestones": [],
        "operations": [dict(id=i, title=i, status=st, entries=[],
                            schedule={"frequency": "daily", "days": ["mon"]})
                       for i, st in ops],
    }), encoding="utf-8")
    return tmp_path


def _run(tmp_path, *args):
    return subprocess.run([os.path.join(ROOT, "bin", "beacon"),
                           "operation", "server-tick", *args],
                          capture_output=True, text=True, cwd=str(tmp_path))


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1.5"])
def test_a_cadence_the_reader_would_override_is_refused(tmp_path, bad):
    """読み取り側が既定に読み替える値を、成功として受けないこと。

    ``tick_scheduler.cadence_minutes`` は非正値を既定 60 分に読み替える。書き込み側が
    ゼロ / 負値を素通りさせていたので、**CLI が「cadence_minutes=0 にした」と成功を
    名乗りながら実際には 60 分で動く** 形だった (独立 AX レビュー high、judge が実測)。
    """
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = _run(proj, "op-1", "--on", "--cadence", bad)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "--cadence" in proc.stderr, proc.stderr


def test_a_positive_cadence_is_accepted(tmp_path):
    """効く側も測る (拒否を広げすぎていないこと)。"""
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = _run(proj, "op-1", "--on", "--cadence", "30")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cadence_minutes=30" in proc.stdout, proc.stdout


def test_the_rule_for_cadence_lives_in_one_place():
    """cadence の規則が 1 箇所であること。

    同じ 1 つのフィールドについて trek 側が独自の検証を持ち、Operation 側はそれを
    知らずに素通りさせていた = 兄弟機能が同じフィールドで別々の規則を持つと、片方
    だけ緩いまま残る。読み取り側と同じ module に寄せ、trek もそこを呼ぶ。
    """
    import ast as _ast
    import inspect
    import tick_scheduler
    import trek
    assert hasattr(tick_scheduler, "validate_cadence_minutes")
    tree = _ast.parse(inspect.getsource(trek._validate_cadence_minutes))
    calls = {(getattr(c.func, "id", None) or getattr(c.func, "attr", ""))
             for c in _ast.walk(tree) if isinstance(c, _ast.Call)}
    assert "validate_cadence_minutes" in calls, (
        "trek が自前の規則を持ったまま (= 同じフィールドで規則が 2 つ): "
        + repr(sorted(calls)))


def test_it_says_whether_firing_will_actually_happen(tmp_path):
    """**実際に発火するか** を報告すること (独立 AX レビュー high)。

    サーバ側の発火条件は `status == "open" AND truthy(server_tick)` の AND なので、
    server_tick=on だけを報告すると「これで自律発火する」と読まれる。status が
    open でない Operation に opt-in しても発火しない。
    """
    proj = _op_project(tmp_path, ("op-1", "open"), ("op-2", "todo"))
    ok = _run(proj, "op-1", "--on")
    assert ok.returncode == 0, ok.stderr
    assert "発火します" in ok.stdout, ok.stdout

    not_yet = _run(proj, "op-2", "--on")
    assert not_yet.returncode == 0, not_yet.stderr
    combined = not_yet.stdout + not_yet.stderr
    assert "まだ発火しません" in combined, combined
    assert "status=todo" in combined, combined
    assert "activate" in combined, "open にする手順を案内していない"


def test_turning_it_off_says_it_will_not_fire(tmp_path):
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = _run(proj, "op-1", "--off")
    assert proc.returncode == 0, proc.stderr
    assert "発火しません" in proc.stdout, proc.stdout


def test_a_stray_second_op_id_is_refused(tmp_path):
    """余分な位置引数を黙って受けないこと (独立 AX レビュー medium)。

    初版は無条件代入で **最後の語が勝つ** 形で、`server-tick op-1 op-2 --on` が
    op-2 に適用され明示した op-1 が黙って捨てられた (judge が実測)。同ファイルの
    兄弟 (create / update) は最初の語が勝つので作法が逆だった。どちらを採っても
    誤った Operation に適用されうるので拒否する。
    """
    proj = _op_project(tmp_path, ("op-1", "open"), ("op-2", "open"))
    proc = _run(proj, "op-1", "op-2", "--on")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "余分な位置引数" in proc.stderr, proc.stderr
    assert "op-1" in proc.stderr and "op-2" in proc.stderr, proc.stderr


def test_an_unknown_operation_sub_verb_exits_non_zero():
    """知らない sub-verb で exit 0 を返さないこと (独立 AX レビュー high)。

    judge が実測: 他の noun の作法 (`channel opt-in`) から類推した
    `beacon operation opt-in <op-id>` が Usage を stdout に出して **exit 0** で
    終わり、終了コードだけを見る script / AI には「成功した」と読めた。
    兄弟 (`beacon review <知らない語>`) は exit 1。
    """
    for guess in ("opt-in", "opt-out", "server-tick-on", "bogus"):
        proc = subprocess.run(
            [os.path.join(ROOT, "bin", "beacon"), "operation", guess, "op-1"],
            capture_output=True, text=True, cwd=ROOT)
        assert proc.returncode != 0, f"{guess}: exit 0 で黙って成功した"
        assert guess in proc.stderr, (
            f"{guess}: 何が知らない語だったのか名指ししていない:\n" + proc.stderr)


def test_asking_for_the_operation_usage_still_succeeds():
    """一覧を見たいだけの呼び出しは成功のままであること。

    知らない語を非 0 にした副作用で、help 要求まで失敗扱いにしない
    (`[ -n "" ] && exit 1` が偽のとき終了コード 1 を残す罠を一度踏んだ)。
    """
    proc = subprocess.run(
        [os.path.join(ROOT, "bin", "beacon"), "operation"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "server-tick" in proc.stdout, "一覧に新しい動詞が出ていない"
