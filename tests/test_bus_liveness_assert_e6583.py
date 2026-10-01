"""ms-173 / e-6583 — 生存主張の門 (channel/bus-liveness-assert.mjs)。

実害 (実測 2026-10-01): 親 bclaude が死んで孤児 (PPID=1) になった bus.mjs が 3 本、
25 日間 prod へ WS をつないだまま 30 秒ごとに ping を送り続けていた。server は ping を
生存の真値として Redis の生存キー (score=now+60 / key TTL 70s) を延命するので、死んだ
セッションが ws_live=true のまま 20 日以上居座った (last_poll_at は 500 時間前)。
e-6563 のガードは live=false に抑止するだけで、キー自体は生き続けていた。

固定する契約:

  * 生存主張の根拠は「REST で生存報告が *通った* こと」。しきい値を超えて通っていなければ
    主張を止める (= 生存キーが自然失効して directory から正しく消える)。
  * しきい値内なら主張を続ける。健全な bridge を誤って黙らせると DM が届かなくなり、
    嘘より重い害になるため、判定は「黙らせない」側に倒す。
  * 孤児判定は「起動時は親が居た → いま 1 になった」= 里親付けの証跡に限る。
    最初から PID 1 配下 (launchd / init / コンテナ) は孤児ではない。
  * Windows は検知対象外 — pid 生存確認 (kill(pid,0)) が対象を終了させる罠を避け、
    かつ Windows の孤児は ppid が 1 にならないため。検知できない側に倒す。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "channel" / "bus-liveness-assert.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not available — the bridge liveness gate is Node-only",
)


def _probe(expr_lines: str) -> dict:
    """Evaluate expressions against the real module and return JSON."""
    script = (
        f"import {{ livenessAssertionStale, isOrphanedBridge }} "
        f"from {json.dumps(str(MODULE))}\n"
        f"const out = {{}}\n{expr_lines}\n"
        "process.stdout.write(JSON.stringify(out))\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_module_exists():
    assert MODULE.exists(), "判断を置く純粋モジュールが無い"


# --- 生存主張: REST の心拍が通っているかだけを根拠にする ---------------------

class TestLivenessAssertionStale:
    def test_fresh_heartbeat_keeps_asserting(self):
        out = _probe("out.r = livenessAssertionStale(1000, 1000 + 60_000, 600_000)")
        assert out["r"] is False

    def test_just_inside_limit_keeps_asserting(self):
        """境界ちょうどは「まだ主張してよい」側。健全な bridge を黙らせない向き。"""
        out = _probe("out.r = livenessAssertionStale(0, 600_000, 600_000)")
        assert out["r"] is False

    def test_past_limit_stops_asserting(self):
        out = _probe("out.r = livenessAssertionStale(0, 600_001, 600_000)")
        assert out["r"] is True

    def test_observed_zombie_stops_asserting(self):
        """実測された 25 日ゾンビ: 心拍は 500 時間前で止まっていた。"""
        out = _probe(
            "out.r = livenessAssertionStale(0, 500 * 3600 * 1000, 600_000)")
        assert out["r"] is True

    def test_garbage_inputs_keep_asserting(self):
        """値が読めないときに黙ると、受信を自分で壊す。fail-safe は主張継続側。"""
        out = _probe(
            "out.a = livenessAssertionStale(NaN, 1, 600_000)\n"
            "out.b = livenessAssertionStale(1, NaN, 600_000)\n"
            "out.c = livenessAssertionStale(0, 10_000_000, 0)\n"
            "out.d = livenessAssertionStale(0, 10_000_000, -1)\n"
            "out.e = livenessAssertionStale(undefined, undefined, undefined)"
        )
        assert out == {"a": False, "b": False, "c": False, "d": False, "e": False}


# --- 孤児判定: 里親付けの証跡に限る ------------------------------------------

class TestIsOrphanedBridge:
    def test_reparented_to_pid1_is_orphan(self):
        """実測された形: 起動時は bclaude 配下 → 親が死んで PPID=1 になった。"""
        out = _probe("out.r = isOrphanedBridge('darwin', 9772, 1)")
        assert out["r"] is True

    def test_parent_still_alive_is_not_orphan(self):
        out = _probe("out.r = isOrphanedBridge('darwin', 9772, 9772)")
        assert out["r"] is False

    def test_born_under_pid1_is_not_orphan(self):
        """launchd / init / コンテナ配下で正当に起動した bridge を殺さない。
        ここが False でないと、そうした構成で起動直後に自滅し受信が死ぬ。"""
        out = _probe("out.r = isOrphanedBridge('linux', 1, 1)")
        assert out["r"] is False

    def test_comment_does_not_misattribute_the_windows_kill_trap(self):
        """Windows の kill(pid,0) 罠は **CPython の os.kill 固有**。Node の
        process.kill(pid,0) は libuv が signum 0 を特別扱いするので安全で、同じ
        ディレクトリの channel/bridge_detect.mjs がそう明記している。言語を限定せずに
        「kill(pid,0) は Windows で危険」と書くと、同一ディレクトリ内で矛盾した主張が
        並び、次の読み手が誤って学習する (独立レビュー 保守性 M-2)。"""
        src = MODULE.read_text(encoding="utf-8")
        assert "bridge_detect.mjs" in src, (
            "Node 側が安全である根拠 (bridge_detect.mjs) への参照が無い")
        assert "os.kill" in src, "危険なのが CPython 側であることを特定していない"

    def test_windows_never_detected(self):
        """Windows の孤児は ppid が 1 にならず、pid 生存確認は対象を終了させる罠が
        ある。検知できない側 (= 何もしない) に倒す。"""
        out = _probe("out.r = isOrphanedBridge('win32', 9772, 1)")
        assert out["r"] is False

    def test_unknown_ppid_is_not_orphan(self):
        out = _probe(
            "out.a = isOrphanedBridge('darwin', undefined, 1)\n"
            "out.b = isOrphanedBridge('darwin', 9772, undefined)\n"
            "out.c = isOrphanedBridge('darwin', NaN, 1)"
        )
        assert out == {"a": False, "b": False, "c": False}

    def test_other_reparent_target_is_not_orphan(self):
        """subreaper 構成では ppid が 1 以外の収容先になる。検知しない側に倒す
        (= 生きている bridge を誤って殺さない)。"""
        out = _probe("out.r = isOrphanedBridge('linux', 9772, 4242)")
        assert out["r"] is False


# ===========================================================================
# 配線 (wiring) — 判断が正しくても bus.mjs が呼んでいなければ意味が無い。
# 「掃除機を買った ≠ 掃除した」。実測されたゾンビを止めるには、生存を主張する
# *全ての* 経路に門が立っていなければならない (= 1 経路だけ塞いで構造が閉じたと
# 言わないための検査)。コメント行は剥がしてから見る (文面だけで素通りさせない)。
# ===========================================================================

BUS_MJS = REPO_ROOT / "channel" / "bus.mjs"


def _code_only(path: Path) -> str:
    """// 行コメントを剥がしたソース。説明文が検査を満たしてしまうのを防ぐ。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append(line.split("  // ")[0])
    return "\n".join(out)


def _region(code: str, start: str, end: str) -> str:
    """start から直後の end までを切り出す。無ければ即座に落とす (= 構造が変わったら
    テストが黙って素通りするのを防ぐ)。"""
    assert start in code, f"領域の開始が見つからない: {start!r}"
    i = code.index(start)
    rest = code[i:]
    assert end in rest, f"領域の終了が見つからない: {end!r}"
    return rest[: rest.index(end) + len(end)]


@pytest.fixture(scope="module")
def bus_code() -> str:
    return _code_only(BUS_MJS)


class TestBusMjsWiring:
    def test_imports_the_pure_gate(self, bus_code):
        assert "./bus-liveness-assert.mjs" in bus_code, (
            "bus.mjs が判断モジュールを import していない = 門が存在しない")

    def test_orphan_gate_on_every_liveness_asserting_path(self, bus_code):
        """孤児判定は 3 経路すべてに要る: WS ping (= 主張そのもの) / 再接続
        (= 登録し直し) / poll loop (= WS 無効時に唯一動く経路)。"""
        for where in ("'ws-ping'", "'ws-reconnect'", "'poll-loop'"):
            assert f"maybeExitIfOrphaned({where})" in bus_code, (
                f"孤児判定が {where} の経路に無い — その経路からゾンビが生き残る")

    def test_ping_is_gated_by_heartbeat_freshness(self, bus_code):
        """門は ping タイマーの *中* に立っていなければならない。

        注意 (このテスト自身の落とし穴): 単に述語名の出現数を数えると、関数 *定義* 行
        も部分一致して数に入る。実際その書き方では ping の門を丸ごと削っても定義＋
        残り 1 箇所で閾値を満たして緑のままだった (注入実験で検出)。だから領域を
        切り出して見る。
        """
        ping_block = _region(
            bus_code, "pingTimer = setInterval(", "}, 30000)")
        assert "isLivenessStaleNow()" in ping_block, (
            "ping タイマーの中に生存主張の門が無い — 心拍が止まっても ping が鳴り続ける")
        reconnect_block = _region(bus_code, "const openOnce = () => {", "let ws\n")
        assert "isLivenessStaleNow()" in reconnect_block, (
            "再接続経路に門が無い — 繋ぎ直して生存台帳に再登録され続ける")

    def test_pure_predicates_are_never_called_bare(self, bus_code):
        """純関数を引数なしで呼んでいないこと。

        実際に踏んだ事故 (独立レビュー 保守性 M-1 の rename 作業中、2026-10-01):
        wrapper を別名にしたとき `maybeExitIfOrphaned` の中が
        `if (!isOrphanedBridge()) return false` のまま残った。これは今や 3 引数の純関数
        なので引数なし呼び出しは全 undefined になり、ガード節が常に false を返して
        **孤児の退場が一切発火しなくなる**。TypeError も出ないので静かに死ぬ。
        既存の配線テストは `maybeExitIfOrphaned('...')` の存在だけを見ていたので
        これを検出できなかった。述語の呼び出し形そのものを固定する。
        """
        for pure in ("livenessAssertionStale", "isOrphanedBridge"):
            assert f"{pure}()" not in bus_code, (
                f"{pure} を引数なしで呼んでいる — 純関数は引数が要る。"
                f"状態込みの判定は wrapper (isLivenessStaleNow / isThisBridgeOrphaned) を使う")

    def test_orphan_exit_uses_the_stateful_wrapper(self, bus_code):
        """maybeExitIfOrphaned が wrapper 経由で判定していること (上の事故の直接ガード)。"""
        block = _region(bus_code, "function maybeExitIfOrphaned(where) {", "\n}")
        assert "isThisBridgeOrphaned()" in block, (
            "孤児判定が wrapper を経由していない — 退場が発火しなくなる")

    def test_heartbeat_success_is_stamped_only_on_success(self, bus_code):
        """「送ろうとした」ではなく「server が受け取った」を真値にする。
        stamp が catch 側にあると、失敗し続ける bridge が永遠に主張を続ける。"""
        stamp = "lastHeartbeatOkAt = Date.now()"
        # 宣言行 (`let lastHeartbeatOkAt = Date.now()`) は stamp ではないので差し引く。
        assigns = bus_code.count(stamp) - bus_code.count("let " + stamp)
        assert assigns == 1, f"心拍成功の stamp が 1 箇所でない (={assigns})"
        put_idx = bus_code.index("\n      " + stamp)
        # stamp の直前に apiPut があり、直後に catch が来る = try の成功経路にある
        before = bus_code[:put_idx]
        assert "await apiPut(" in before, "stamp が apiPut より前にある"
        after = bus_code[put_idx:]
        assert after.index("} catch (e) {") < after.index("\n  }"), (
            "stamp が try の成功経路に無い (catch 側だと失敗でも主張が続く)")

    def test_ping_timer_is_unrefd(self, bus_code):
        """unref が無いと、poll loop が死んだ bridge では ping タイマーだけが
        イベントループを生かし続け、SIGTERM でプロセスが終われない
        (実測: 孤児 3 本は SIGTERM を無視し SIGKILL でしか落ちなかった)。"""
        assert "pingTimer.unref" in bus_code, "ping タイマーが unref されていない"

    def test_signal_handler_closes_the_socket(self, bus_code):
        """開いている WS はイベントループを保持するので、stopping を立てるだけでは
        終了できない。SIGINT/SIGTERM でソケット本体を閉じる経路が要る。"""
        assert "closeBusWs()" in bus_code, (
            "シグナル経路で WS を閉じていない = SIGTERM で落ちないままになる")
        sig_idx = bus_code.index("process.on('SIGTERM'")
        handler_region = bus_code[max(0, sig_idx - 1200):sig_idx + 400]
        assert "closeBusWs()" in handler_region, (
            "closeBusWs がシグナルハンドラの近傍で呼ばれていない")

    def test_reconnect_is_suppressed_while_stopping(self, bus_code):
        """停止中に繋ぎ直すと生存台帳へ再登録され、ping を止めた意味が消える。"""
        assert "if (wsStopping) return" in bus_code
