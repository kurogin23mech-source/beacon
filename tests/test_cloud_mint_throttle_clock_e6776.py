"""ms-173 / e-6776 — スロットルの時計は「自分が前回いつ送ったか」でなければならない。

実害 (2026-10-01 実測): 稼働中 14 セッションのうち `heartbeat_fresh` が True なのは
1 件だけ、`last_heartbeat_at` の古さは 3.6 時間〜7 日 (一方 poll は 0〜8 秒)。つまり
健全性 3 軸のうち「人/AI が実際に動かしているか」が構造的に常に false だった。

真因: `/api/me/heartbeat` の呼び出しを間引く cache の freshness gate が `last_active` を
見ていたが、その値を書くのは **受信プロセス** (channel/bus.mjs が 60 秒ごとに書く) で、
このスロットルとは無関係な用途の stamp だった。受信プロセスが生きている間 `last_active`
は常に 60 秒以内なので TTL (既定 300 秒) を一度も超えず、cache が永久に当たって心拍が
二度と出なかった。サーバ側の `last_heartbeat_at` は最初の mint 時刻で凍結する。

「前回送ってから十分経ったか」を **他人の活動で判定していた** のが誤り。

固定する契約:

  * gate は `last_cloud_heartbeat_sent_at` (= 実際に心拍を送った経路だけが書く stamp) を見る。
  * `last_active` がいくら新しくても、心拍を送っていなければ cache は当たらない
    (= これが退行したら軸が再び死ぬ)。
  * stamp が無い session.json (古い beacon が書いたもの) は cache miss にして 1 回だけ
    network に出る = 自己治癒する。
  * 心拍成功時に stamp が書かれる。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import session as session_mod

TTL = 300


def _iso(dt):
    # lib/session._now_iso() と同じ秒精度にする。ミリ秒を付けると現在時刻より
    # わずかに未来の stamp になり、_is_fresh が負の差分を fresh と見なさず落ちる。
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_SESSION_CLOUD_MINT_TTL_SECONDS", str(TTL))
    return tmp_path


def _seed(cwd, **over):
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    payload = {
        "session_id": "sv-test-1",
        "machine_id": "mc-1",
        "parent_pid": 4242,
        "source": "server_minted",
        "last_active": _iso(now),
        "last_cloud_heartbeat_sent_at": _iso(now),
    }
    payload.update(over)
    (cwd / ".beacon" / "session.json").write_text(
        json.dumps(payload), encoding="utf-8")
    return payload


def _hit(cwd):
    return session_mod._cloud_mint_cache_hit("p1", "mc-1", 4242)


class TestThrottleClock:
    def test_fresh_heartbeat_stamp_hits_the_cache(self, cwd):
        _seed(cwd)
        assert _hit(cwd) is not None

    def test_stale_heartbeat_stamp_misses_even_when_last_active_is_fresh(self, cwd):
        """**これが e-6776 の本体**。受信プロセスが last_active を常時更新していても、
        心拍を送っていなければ cache は当たらず network に出る。ここが緑でなくなると
        健全性の『注目されているか』軸が再び構造的に死ぬ。"""
        now = datetime.now(timezone.utc)
        _seed(cwd,
              last_active=_iso(now),                                  # 受信プロセスが更新
              last_cloud_heartbeat_sent_at=_iso(now - timedelta(seconds=TTL + 60)))
        assert _hit(cwd) is None

    def test_missing_stamp_misses_the_cache(self, cwd):
        """古い beacon が書いた session.json は stamp を持たない。1 回だけ network に
        出て stamp が付き、以降は定常運転 = 自己治癒する。"""
        p = _seed(cwd)
        del p["last_cloud_heartbeat_sent_at"]
        (cwd / ".beacon" / "session.json").write_text(
            json.dumps(p), encoding="utf-8")
        assert _hit(cwd) is None

    def test_last_active_alone_can_no_longer_satisfy_the_gate(self, cwd):
        """last_active だけで通る経路が残っていないこと (退行の直接ガード)。"""
        now = datetime.now(timezone.utc)
        p = _seed(cwd, last_active=_iso(now))
        del p["last_cloud_heartbeat_sent_at"]
        (cwd / ".beacon" / "session.json").write_text(
            json.dumps(p), encoding="utf-8")
        assert _hit(cwd) is None, (
            "last_active が新しいだけで cache が当たっている = 時計が他人の活動に戻った")

    def test_other_gates_still_apply(self, cwd):
        """identity tuple / source の既存ゲートは不変。"""
        _seed(cwd, machine_id="mc-OTHER")
        assert _hit(cwd) is None
        _seed(cwd, parent_pid=9999)
        assert _hit(cwd) is None
        _seed(cwd, source="local")
        assert _hit(cwd) is None

    def test_ttl_zero_disables_the_cache(self, cwd, monkeypatch):
        monkeypatch.setenv("BEACON_SESSION_CLOUD_MINT_TTL_SECONDS", "0")
        _seed(cwd)
        assert _hit(cwd) is None


class TestStampIsWrittenOnlyBySender:
    """stamp を書くのは心拍を送った経路だけ、という所有関係を固定する。

    独立レビュー (保守性 M-4) の指摘を受けて、ここは **ソースの字面一致をやめ**
    振る舞いで確かめる形に置き換えた (元は '"last_cloud_heartbeat_sent_at": now' の
    逐語一致で、無害な整形変更でも赤くなった)。実際に書かれた session.json を見る
    振る舞いテストは tests/test_session_cloud_mint_cache.py が持つ (fixture がそこに
    揃っているため)。ここに残すのは「受信プロセス側が書かない」= 字面でしか確かめ
    られない否定の契約だけ。
    """

    def test_bridge_does_not_write_the_stamp(self):
        """受信プロセスがこの stamp を書かないこと。書いたら時計が再び他人のものになる。"""
        from pathlib import Path
        repo = Path(session_mod.__file__).resolve().parents[1]
        for rel in ("channel/bus.mjs", "channel/bus-local-heartbeat.mjs"):
            txt = (repo / rel).read_text(encoding="utf-8")
            assert "last_cloud_heartbeat_sent_at" not in txt, (
                f"{rel} が throttle の時計を書いている (e-6776 の再発)")
