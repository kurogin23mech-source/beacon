"""`note list` が読み込む総量に上限があること (ms-178 / e-7150 AC7).

``note list`` には件数・期間・文字数のどの上限も無く、全件を本文つきで返していた。
``/beacon-session-start`` は起動のたびにこれを実行するので、作業を 1 つも始める前に
ストア全体がコンテキストに入る (2026-10-25 実測: 180 件・本文合計 370,047 字。
日本語は 1〜2 字/token なので 1M コンテキストの 18〜37%)。セッションを跨いだ
引き継ぎという本来の価値より、圧迫による劣化が大きくなっていた。

この上限は同タスクの session_id 修正とは **独立に** 必要。clear が直れば 24 日分の
累積は止まるが、並走セッションが各自長文を書いた 1 日で同じ圧迫が再現する。

ここで固定する罠が 1 つある: 窓で全部落ちたときに「メモなし」と言ってはいけない。
読み手は「メモは無い」と結論し、存在する引き継ぎを読み落とす。省略は必ず件数つきで
申告する。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_note  # noqa: E402


def _iso(days_ago: float) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return ts.isoformat()


def _rows(n: int, days_ago: float):
    return [{"ts": _iso(days_ago), "text": "note %d" % i} for i in range(n)]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """ローカルのみのストア (cloud 無し) を用意して cwd を移す。"""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon / "project.json"))
    monkeypatch.chdir(tmp_path)
    for var in ("BEACON_JSON", "BEACON_NOTE_LIST_ALL",
                "BEACON_NOTE_LIST_SINCE_DAYS", "BEACON_NOTE_LIST_LIMIT"):
        monkeypatch.delenv(var, raising=False)

    def _write(rows):
        (beacon / "session_notes.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
    return _write


# --- 窓の算出 (純粋関数) -----------------------------------------------------

def test_default_window_is_bounded(monkeypatch):
    """既定で「無制限」にならない。ここが緩むと AC7 が実質無効になる。"""
    monkeypatch.delenv("BEACON_NOTE_LIST_ALL", raising=False)
    monkeypatch.delenv("BEACON_NOTE_LIST_SINCE_DAYS", raising=False)
    monkeypatch.delenv("BEACON_NOTE_LIST_LIMIT", raising=False)
    days, limit = cmd_note._list_window()
    assert days is not None and days > 0
    assert limit is not None and limit > 0


def test_all_disables_both_axes(monkeypatch):
    monkeypatch.setenv("BEACON_NOTE_LIST_ALL", "1")
    assert cmd_note._list_window() == (None, None)


def test_zero_disables_only_that_axis(monkeypatch):
    monkeypatch.delenv("BEACON_NOTE_LIST_ALL", raising=False)
    monkeypatch.setenv("BEACON_NOTE_LIST_SINCE_DAYS", "0")
    monkeypatch.setenv("BEACON_NOTE_LIST_LIMIT", "7")
    assert cmd_note._list_window() == (None, 7)


def test_garbage_values_fall_back_to_defaults(monkeypatch):
    """数値でない指定で例外を投げて一覧ごと落とさない。"""
    monkeypatch.delenv("BEACON_NOTE_LIST_ALL", raising=False)
    monkeypatch.setenv("BEACON_NOTE_LIST_SINCE_DAYS", "three")
    monkeypatch.setenv("BEACON_NOTE_LIST_LIMIT", "")
    days, limit = cmd_note._list_window()
    assert days == cmd_note.DEFAULT_LIST_SINCE_DAYS
    assert limit == cmd_note.DEFAULT_LIST_LIMIT


def test_count_limit_keeps_the_newest(monkeypatch):
    """落とすのは古い側。引き継ぎは新しい方が効くので順序は保つ。"""
    notes = [{"ts": _iso(0.1), "text": "n%d" % i} for i in range(10)]
    shown, omitted = cmd_note._apply_list_window(notes, None, 3)
    assert [n["text"] for n in shown] == ["n7", "n8", "n9"]
    assert omitted == 7


def test_day_window_drops_older_notes():
    notes = _rows(2, days_ago=30) + _rows(3, days_ago=0.5)
    shown, omitted = cmd_note._apply_list_window(notes, 3, None)
    assert len(shown) == 3
    assert omitted == 2


def test_both_axes_apply_together():
    """日で絞った後さらに件数で絞る (片方をすり抜ける形を塞ぐ)。"""
    notes = _rows(5, days_ago=30) + _rows(10, days_ago=0.5)
    shown, omitted = cmd_note._apply_list_window(notes, 3, 4)
    assert len(shown) == 4
    assert omitted == 11


def test_undated_notes_are_kept_not_dropped():
    """日付が読めないメモは残す。読めないことは「古い」証拠ではない。"""
    notes = [{"text": "no ts"}, {"ts": "garbage", "text": "bad ts"},
             {"ts": _iso(0.1), "text": "fresh"}]
    shown, omitted = cmd_note._apply_list_window(notes, 3, None)
    assert len(shown) == 3
    assert omitted == 0


def test_naive_timestamps_are_handled():
    """tz 無しの ts (ローカル書き込み形式) で例外にならない。"""
    naive = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).isoformat()
    shown, omitted = cmd_note._apply_list_window([{"ts": naive, "text": "x"}], 3, None)
    assert len(shown) == 1


# --- 出力の申告 --------------------------------------------------------------

def test_json_mode_is_capped_and_announces_the_shortfall(store, monkeypatch, capsys):
    """session-start が実際に呼ぶのは JSON モード。ここが効かないと無意味。"""
    store(_rows(50, days_ago=0.2))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_NOTE_LIST_LIMIT", "5")
    cmd_note.cmd_note_list()
    out = capsys.readouterr()
    payload = json.loads(out.out.strip())
    assert len(payload) == 5
    assert "他 45 件" in out.err


def test_human_mode_announces_before_the_list(store, monkeypatch, capsys):
    """省略の申告は一覧の前に出す (最初の画面で窓だと分かる)。"""
    store(_rows(30, days_ago=0.2))
    monkeypatch.setenv("BEACON_NOTE_LIST_LIMIT", "3")
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    notice_at = out.index("他 27 件")
    first_note_at = out.index("note ")
    assert notice_at < first_note_at


def test_windowed_out_is_not_reported_as_empty(store, monkeypatch, capsys):
    """窓で全部落ちたとき「メモなし」と言わない。

    これを言うと読み手は「引き継ぎは無い」と結論する。ストアが空なのか、
    窓の外なのかは、読み手の次の行動を変える。
    """
    store(_rows(4, days_ago=90))
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    assert "メモなし" not in out
    assert "表示範囲内のメモはありません" in out
    assert "他 4 件" in out


def test_genuinely_empty_store_still_says_so(store, monkeypatch, capsys):
    """本当に空のときは従来どおり「メモなし」と言う (逆方向の誤報を防ぐ)。"""
    store([])
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    assert "メモなし" in out


def test_all_flag_returns_everything_with_no_notice(store, monkeypatch, capsys):
    store(_rows(40, days_ago=90))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_NOTE_LIST_ALL", "1")
    cmd_note.cmd_note_list()
    out = capsys.readouterr()
    assert len(json.loads(out.out.strip())) == 40
    assert "省略" not in out.err
