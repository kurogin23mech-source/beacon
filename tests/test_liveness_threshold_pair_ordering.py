"""ms-173 独立レビュー AX-1 — 2 プロセスに分かれた閾値の順序を構造で固定する。

生存判定の閾値は **2 つのプロセス・2 つの言語** に分かれて住んでいる:

- bridge (Node, ``channel/bus.mjs``): ``BEACON_BUS_LIVENESS_STALL_MS`` 既定 10 分
  = 「生存報告が通らなくなったら自分で ping を止める」側。
- server (Python, ``server/app.py``): ``BEACON_RUNNING_DECL_STALE_AGE_S`` 既定 30 分
  = 「それでも作業中を主張し続ける行を server が信じない」側。

**bridge 側 < server 側** の順序で初めて意図通りに働く: 先に bridge が自分で黙り、
それが届かない古い bridge だけを server が捕まえる。逆転させると bridge が黙る前に
server が not-live にしてしまい、**健全なセッションを誤って落とす**。

前回ラウンド (= commit 09c460d9) はこの順序依存を `server/app.py` の **コメント** で
注意書きした。独立レビューはそれを「保護がコメントだけ = 構造的歯止めではない」と
再提起した (AX-1, severity high)。語彙が LIVENESS/STALL 系 と ZOMBIE/AGE 系 で丸ごと
分かれており、片方の env 名から相方に grep で辿り着けないため、**片側だけを調整する
タスクを渡された AI は相方の存在に気づけない**。だから散文でなく機械で止める。

**このガードが見るもの / 見ないもの (限界を明示する)**:

- 見る: 両ファイルの **既定値 (ソースに書かれたリテラル)** の順序。片側の既定値を
  変えた瞬間に CI が赤くなる = 現実に起きる失敗 (片側変更) を捕まえる。
- 見ない: 実行時に ``BEACON_*`` env で上書きされた値の順序。bridge は別プロセス
  (別マシンにも居る) なので、server 側から相手の実行時値は原理的に観測できない。
  env 上書きで順序を逆転させる運用は、このガードでは検出できない。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "server" / "app.py"
BUS_MJS = ROOT / "channel" / "bus.mjs"

BRIDGE_ENV = "BEACON_BUS_LIVENESS_STALL_MS"

#: bridge が黙るより **後** に発動しなければならない server 側の閾値 (秒単位)。
#: 新しく同種の閾値を足したらここに追加する — 追加を忘れると順序が守られていない
#: 閾値が 1 つだけ素通りするので、網羅もテストで固定する (下の test_registry_covers…)。
SERVER_ENVS = (
    "BEACON_WS_ZOMBIE_POLL_AGE_S",
    "BEACON_WS_ZOMBIE_NO_HISTORY_AGE_S",
    "BEACON_RUNNING_DECL_STALE_AGE_S",
)

#: 順序契約の **対象外**。除外は理由付きで明示する (黙って落とすと「検査したつもり」に
#: なる)。``BEACON_ATTENTIVE_MAX_AGE_S`` は「人が注目している窓」= 未取得の受信イベント
#: がどれだけ古くなるまで許すかの窓で、「bridge が先に黙る」という bridge↔server の
#: 順序とは別の契約。実際この値 (既定 5 分) は bridge の stall (既定 10 分) より小さく、
#: 小さいことが正しい。
NOT_ORDERING_THRESHOLDS = {
    "BEACON_ATTENTIVE_MAX_AGE_S",
}


def _server_default_seconds(src: str, env: str) -> int:
    """``os.environ.get("<env>", "1800")`` の既定値を抜く。

    緩い substring 一致にしない (コメント中の同名文字列で false-pass する)。
    env 名の直後に来る文字列リテラルだけを構造的に拾う。
    """
    m = re.search(
        r'os\.environ\.get\(\s*"' + env + r'"\s*,\s*"(\d+)"\s*\)', src)
    assert m, f"{env} の既定値を {APP.name} から構造的に抽出できない"
    return int(m.group(1))


def _bridge_default_ms(src: str) -> int:
    """``process.env.BEACON_BUS_LIVENESS_STALL_MS || '600000'`` の既定値を抜く。"""
    m = re.search(
        r"process\.env\." + BRIDGE_ENV + r"\s*\|\|\s*'(\d+)'", src)
    assert m, f"{BRIDGE_ENV} の既定値を {BUS_MJS.name} から構造的に抽出できない"
    return int(m.group(1))


@pytest.fixture(scope="module")
def app_src() -> str:
    return APP.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bus_src() -> str:
    return BUS_MJS.read_text(encoding="utf-8")


class TestThresholdPairOrdering:
    def test_both_defaults_are_extractable(self, app_src, bus_src):
        """抽出自体が壊れたら気付けるようにする (抽出不能を黙って pass にしない)。"""
        assert _bridge_default_ms(bus_src) > 0
        for env in SERVER_ENVS:
            assert _server_default_seconds(app_src, env) > 0

    @pytest.mark.parametrize("env", SERVER_ENVS)
    def test_bridge_falls_silent_before_the_server_stops_believing(
            self, app_src, bus_src, env):
        """**順序**: bridge 既定 < server 既定。逆転したらここで落とす。"""
        bridge_ms = _bridge_default_ms(bus_src)
        server_ms = _server_default_seconds(app_src, env) * 1000
        assert bridge_ms < server_ms, (
            f"生存判定の閾値の順序が逆転している: {BRIDGE_ENV}={bridge_ms}ms "
            f"(bridge, channel/bus.mjs) は {env}={server_ms}ms "
            f"(server, server/app.py) より小さくなければならない。\n"
            "逆転すると bridge が自分で黙る前に server が not-live にしてしまい、"
            "健全なセッションを誤って落とす。片方だけ変えないこと — どちらかを"
            "変えるなら両方を見直して、この順序を保ったまま調整する。"
        )

    def test_registry_covers_every_server_side_age_threshold(self, app_src):
        """server 側に同種の閾値を足して ``SERVER_ENVS`` への登録を忘れたら落とす。

        対象は「生存の裏付けが古すぎると判定する」秒単位の閾値 = env 名が
        ``_AGE_S`` で終わるもの。網羅を固定しないと、順序を守っていない閾値が
        1 つだけ静かに素通りする (= 偽の安全)。
        """
        found = set(re.findall(r'os\.environ\.get\(\s*"(BEACON_[A-Z_]*_AGE_S)"',
                               app_src))
        missing = found - set(SERVER_ENVS) - NOT_ORDERING_THRESHOLDS
        assert not missing, (
            f"server/app.py に順序未検査の閾値がある: {sorted(missing)} — "
            "bridge 側より後に発動すべきなら SERVER_ENVS に追加し、"
            "別の契約なら NOT_ORDERING_THRESHOLDS に理由付きで除外する")

    def test_each_side_points_at_the_other(self, app_src, bus_src):
        """名前から相方に辿り着けない以上、**互いの env 名を本文に書く** のを契約に
        する。これで片側のファイルを開いた AI が grep 1 発で相方に届く。"""
        assert BRIDGE_ENV in app_src, (
            f"{APP.name} が相方 {BRIDGE_ENV} に言及していない — "
            "名前の語彙が分かれているので、言及が唯一の導線になる")
        for env in SERVER_ENVS:
            assert env in bus_src, (
                f"{BUS_MJS.name} が相方 {env} に言及していない — "
                "名前の語彙が分かれているので、言及が唯一の導線になる")
