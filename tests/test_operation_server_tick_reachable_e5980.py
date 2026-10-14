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


def test_running_it_without_an_op_id_explains_itself(tmp_path):
    """op-id 無しで叩いたら Usage を出して非 0 で終わること (黙って成功しない)。

    **使い捨てプロジェクトの中で走らせる。** cwd をこの repo にした版は、ローカルには
    `.beacon/` が在るのに CI のチェックアウトには無いため、`ensure_project` が先に
    落ちて「No Beacon project found here」になった (= ローカル緑 / CI 赤の
    非 hermetic な試験)。測りたいのは sub-verb の引数処理なので、project の有無に
    依存しない形にする。
    """
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = subprocess.run(
        [os.path.join(ROOT, "bin", "beacon"), "operation", "server-tick"],
        capture_output=True, text=True, cwd=str(proj))
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


def test_the_firing_preconditions_live_in_one_place():
    """発火の前提の一覧が 1 箇所であること (独立保守性レビュー high)。

    初版はここ (CLI) に status + server_tick だけの独自コピーを書き、**envelope
    (事前承認) の関門を見ていなかった** ので envelope 無しの Operation に
    「→ 発火します」と言っていた。envelope の関門は server/app.py の
    `_fire_operation` に手で書かれていた = 前提の知識が 2 箇所に割れていた。
    **報告する側と実際に発火する側の両方が同じ関数を通る** ことを構造で固定する。
    """
    import ast as _ast
    import inspect
    import cmd_operation
    import tick_scheduler
    assert hasattr(tick_scheduler, "firing_blockers")
    calls = {(getattr(c.func, "id", None) or getattr(c.func, "attr", ""))
             for c in _ast.walk(_ast.parse(inspect.getsource(
                 cmd_operation.cmd_operation_server_tick)))
             if isinstance(c, _ast.Call)}
    assert "firing_blockers" in calls, (
        "報告する側が共通の前提判定を通っていない: " + repr(sorted(calls)))
    # 実際に発火する側も同じ関数を通ること (片方だけ直して食い違う形を塞ぐ)。
    #
    # **構文木で実際の呼び出しを見る。** ソースへの部分一致で書いた版は、この欠陥を
    # 説明する **コメント** が `tick_scheduler.firing_blockers` という文字列を含む
    # ために、呼び出しを消しても緑のままだった (反転 U59 で実測)。緩い一致。
    with open(os.path.join(ROOT, "server", "app.py"), encoding="utf-8") as f:
        app_tree = _ast.parse(f.read())
    fire_fn = next((n for n in _ast.walk(app_tree)
                    if isinstance(n, _ast.FunctionDef)
                    and n.name == "_fire_operation"), None)
    assert fire_fn is not None, "_fire_operation が見つからない"
    fire_calls = {(getattr(c.func, "id", None) or getattr(c.func, "attr", ""))
                  for c in _ast.walk(fire_fn) if isinstance(c, _ast.Call)}
    assert "firing_blockers" in fire_calls, (
        "実際に発火する側が共通の前提判定を通っていない (= CLI の報告と食い違う): "
        + repr(sorted(fire_calls)))


@pytest.mark.parametrize("state,expect_blocked", [
    ("present", False), ("absent", True), ("unknown", False),
])
def test_the_envelope_gate_is_part_of_the_preconditions(state, expect_blocked):
    """envelope の関門が前提の一覧に入っていること。

    「無い」と「確認できない」を分ける: 確認できないものを blocker と断定しない
    (local モード / 認証 / 未到達 を全部 None に畳む取得関数に引きずられない)。
    """
    import tick_scheduler
    blockers, undetermined = tick_scheduler.firing_blockers(
        status="open", server_tick=True, envelope_state=state)
    assert bool(blockers) is expect_blocked, (state, blockers)
    if state == "unknown":
        assert undetermined, "確認できないことを黙って無視した"
    else:
        assert not undetermined, (state, undetermined)


def test_it_says_whether_firing_will_actually_happen(tmp_path):
    """**実際に発火するか** を報告すること (独立 AX レビュー high)。

    サーバ側の発火条件は `status == "open" AND truthy(server_tick)` の AND なので、
    server_tick=on だけを報告すると「これで自律発火する」と読まれる。status が
    open でない Operation に opt-in しても発火しない。
    """
    proj = _op_project(tmp_path, ("op-1", "open"), ("op-2", "todo"))
    # local モードなので envelope は「確認できない」 = 断定しないこと
    ok = _run(proj, "op-1", "--on")
    assert ok.returncode == 0, ok.stderr
    combined_ok = ok.stdout + ok.stderr
    assert "確認できていません" in combined_ok, combined_ok
    assert "発火します" not in combined_ok, (
        "envelope を確認できていないのに発火すると断定した:\n" + combined_ok)

    not_yet = _run(proj, "op-2", "--on")
    assert not_yet.returncode == 0, not_yet.stderr
    combined = not_yet.stdout + not_yet.stderr
    assert "まだ発火しません" in combined, combined
    assert "status が open ではありません" in combined, combined
    assert "activate" in combined, "open にする手順を案内していない"


def test_turning_it_off_says_it_will_not_fire(tmp_path):
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = _run(proj, "op-1", "--off")
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "server_tick が off" in combined, combined


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
    assert "余分な引数" in proc.stderr, proc.stderr
    assert "op-1" in proc.stderr and "op-2" in proc.stderr, proc.stderr


def test_every_operation_positional_arm_uses_the_one_shared_guard():
    """operation noun の中で位置引数の作法が 1 つに揃っていること。

    独立保守性レビュー medium: 新設の `server-tick` だけ「2 つ目を拒否」で、兄弟
    (`create` / `update` 等) は「最初が勝ち 2 つ目を黙って捨てる」形だった。同じ
    case の中で答えが予測できないと、次に sub-verb を足す人がどちらを真似ればよいか
    局所からは判断できない。

    さらに私は **引数順の違う重複定義** を足しかけた (bash は後の定義が勝つので、
    呼び出しが黙って逆順になる)。既存の `_guard_extra_positional` が唯一の定義で
    あること、operation の各腕がそれを通ることを構造で固定する。
    """
    import re as _re
    with open(os.path.join(ROOT, "bin", "beacon"), encoding="utf-8") as f:
        src = f.read()
    assert src.count("\n_guard_extra_positional() {") == 1, (
        "位置引数ガードの定義が 1 つでない (bash は後の定義が勝つので、"
        "引数順の違う重複は呼び出しを黙って壊す)")
    start = src.index("\n    operation)")
    nxt = _re.search(r"^    [a-z][a-z0-9|_-]*\)",
                     src[src.index("\n", start + 1) + 1:], _re.M)
    case = src[start: start + nxt.start()] if nxt else src[start:]
    # 「最初が勝ち 2 つ目を黙って捨てる」旧作法が残っていないこと
    silent = _re.findall(r'\*\) \[\[ -z "\$\w+" \]\] && \w+="\$1"; shift ;;', case)
    assert silent == [], (
        "余分な位置引数を黙って捨てる腕が残っている: " + repr(silent))
    # 位置引数を受ける腕は共通ガードを通ること
    arms = [ln for ln in case.splitlines()
            if _re.search(r'\*\)\s*_guard_extra_positional', ln)]
    assert len(arms) >= 6, (
        "共通ガードを通る位置引数の腕が少なすぎる: " + repr(len(arms)))


def test_an_unknown_operation_sub_verb_exits_non_zero(tmp_path):
    """知らない sub-verb で exit 0 を返さないこと (独立 AX レビュー high)。

    judge が実測: 他の noun の作法 (`channel opt-in`) から類推した
    `beacon operation opt-in <op-id>` が Usage を stdout に出して **exit 0** で
    終わり、終了コードだけを見る script / AI には「成功した」と読めた。
    兄弟 (`beacon review <知らない語>`) は exit 1。
    """
    proj = _op_project(tmp_path, ("op-1", "open"))
    for guess in ("opt-in", "opt-out", "server-tick-on", "bogus"):
        proc = subprocess.run(
            [os.path.join(ROOT, "bin", "beacon"), "operation", guess, "op-1"],
            capture_output=True, text=True, cwd=str(proj))
        assert proc.returncode != 0, f"{guess}: exit 0 で黙って成功した"
        assert guess in proc.stderr, (
            f"{guess}: 何が知らない語だったのか名指ししていない:\n" + proc.stderr)


def test_asking_for_the_operation_usage_still_succeeds(tmp_path):
    """一覧を見たいだけの呼び出しは成功のままであること。

    知らない語を非 0 にした副作用で、help 要求まで失敗扱いにしない
    (`[ -n "" ] && exit 1` が偽のとき終了コード 1 を残す罠を一度踏んだ)。
    """
    proj = _op_project(tmp_path, ("op-1", "open"))
    proc = subprocess.run(
        [os.path.join(ROOT, "bin", "beacon"), "operation"],
        capture_output=True, text=True, cwd=str(proj))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "server-tick" in proc.stdout, "一覧に新しい動詞が出ていない"


def test_no_subprocess_in_this_file_runs_from_the_repo_root():
    """この file の subprocess が **この repo を cwd にしない** こと。

    CI が実測した退行: 3 件が `cwd=ROOT` で走っており、ローカルには `.beacon/` が
    在るのに CI のチェックアウトには無いため `ensure_project` が先に落ちて
    「No Beacon project found here」になった —— **ローカル緑 / CI 赤** の
    非 hermetic な試験。測りたいのは引数処理 / 経路なので、project の有無に
    依存させない。

    構文木で `subprocess.run(..., cwd=...)` の実引数を見る (文字列一致では
    docstring やコメントの言及に反応する)。
    """
    import ast as _ast
    with open(__file__, encoding="utf-8") as f:
        tree = _ast.parse(f.read())
    offenders = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        name = getattr(node.func, "attr", "")
        if name != "run":
            continue
        for kw in node.keywords:
            if kw.arg != "cwd":
                continue
            # 許されるのは tmp_path 由来 (= 使い捨てプロジェクト) だけ。
            src = _ast.unparse(kw.value)
            if "ROOT" in src:
                offenders.append(f"line {node.lineno}: cwd={src}")
    assert offenders == [], (
        "この repo を cwd にして beacon を叩いている (ローカルだけ緑になる): "
        + repr(offenders))
