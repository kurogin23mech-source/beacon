"""decision 一覧の session / target 絞り込みの契約テスト (ms-164 e-6030)。

session-end が「このセッション / この作業対象(target)で下した判断だけ」を引けるよう、
``beacon decision list`` に ``--session`` / ``--target`` を足した。芯は 2 つ:

  1. **どこを読むか**: session = ``who.session_id`` / target = ``related.target_id``
     (top-level ``target_id`` を fallback)。この解決を単一 helper に閉じる。
  2. **窓こぼれしない (scale-contract-principle)**: 絞り込みは ``limit`` の *前* に
     掛かる。大量 backlog があっても対象 session/target の古い判断が limit 窓から
     こぼれず返る (= kind と同じ push-down)。

窓ロジックは 3 backend 共通の単一真実源 ``decision_event.window_decision_events``
なので、ここでその pure 関数を直接固定する = firestore/mysql/dynamodb が同時に守られる。
"""
import os
import sys

THIS = os.path.dirname(__file__)
SERVER = os.path.normpath(os.path.join(THIS, "..", "server"))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import decision_event  # noqa: E402
import dynamodb_client as ddb  # noqa: E402


def _row(did, *, session="", target="", top_target="", created="2026-09-01T10:00:00.000000Z",
         kind="log-backstop"):
    row = {"decision_id": did, "kind": kind, "decision": "x", "created_at": created}
    if session:
        row["who"] = {"session_id": session, "user_id": "", "agent": None}
    if target:
        row["related"] = {"target_id": target}
    if top_target:
        row["target_id"] = top_target
    return row


# --- row 抽出 helper (session/target がどこに載るかの単一定義) ------------------

def test_row_session_id_reads_who():
    assert decision_event._row_session_id(_row("d1", session="S9")) == "S9"
    assert decision_event._row_session_id({"decision_id": "d0"}) == ""  # who 無し


def test_row_target_id_prefers_related_then_top_level():
    assert decision_event._row_target_id(_row("d1", target="ms-5")) == "ms-5"
    # related 無し + top-level target_id → fallback で拾う
    assert decision_event._row_target_id(_row("d2", top_target="ms-7")) == "ms-7"
    assert decision_event._row_target_id({"decision_id": "d0"}) == ""


# --- session / target フィルタ ----------------------------------------------

def test_session_filters_to_one_session():
    rows = [_row("d1", session="S1"), _row("d2", session="S2"),
            _row("d3", session="S1")]
    got = decision_event.window_decision_events(rows, session="S1")
    assert {r["decision_id"] for r in got} == {"d1", "d3"}


def test_target_filters_to_one_target():
    rows = [_row("d1", target="ms-1"), _row("d2", target="ms-2"),
            _row("d3", top_target="ms-1")]  # top-level fallback も一致する
    got = decision_event.window_decision_events(rows, target="ms-1")
    assert {r["decision_id"] for r in got} == {"d1", "d3"}


def test_session_and_target_combine():
    rows = [_row("d1", session="S1", target="ms-1"),
            _row("d2", session="S1", target="ms-2"),
            _row("d3", session="S2", target="ms-1")]
    got = decision_event.window_decision_events(rows, session="S1", target="ms-1")
    assert [r["decision_id"] for r in got] == ["d1"]


def test_input_list_not_mutated():
    src = [_row("d1", session="S1"), _row("d2", session="S2")]
    before = list(src)
    decision_event.window_decision_events(src, session="S1")
    assert src == before


# --- scale-contract-principle: 絞り込みは limit の前 (窓こぼれ無し) ------------

def test_session_survives_large_backlog_before_limit():
    """芯の回帰: このセッションの判断が古くても、大量の他セッション backlog に
    limit 窓から押し出されずに返る。絞り込みが truncate の *前* に掛かる証拠。"""
    rows = []
    # 対象セッション S-me の判断は最古 (2026-07-01)
    for i in range(3):
        rows.append(_row(f"mine-{i}", session="S-me",
                         created=f"2026-07-01T00:00:{i:02d}.000000Z"))
    # その後に他セッションの判断を大量 (limit=100 を大きく超える)
    for i in range(500):
        rows.append(_row(f"other-{i:04d}", session="S-other",
                         created=f"2026-08-{(i % 27) + 1:02d}T00:00:{i % 60:02d}.000000Z"))
    got = decision_event.window_decision_events(rows, session="S-me", limit=100)
    # 旧来の「最新 limit 件を取ってから絞る」なら 0 件 (S-me は最古で窓外) になる。
    assert {r["decision_id"] for r in got} == {"mine-0", "mine-1", "mine-2"}


def test_target_survives_large_backlog_via_backend():
    """backend 経由 (dynamodb fallback) でも push-down が効く = 3 backend 共通。"""
    ddb._DECISION_EVENTS_FALLBACK.clear()
    rows = [_row("t-old", target="ms-goal",
                 created="2026-06-01T00:00:00.000000Z")]
    for i in range(400):
        rows.append(_row(f"noise-{i:04d}", target="ms-other",
                         created=f"2026-08-{(i % 27) + 1:02d}T00:00:{i % 60:02d}.000000Z"))
    ddb._DECISION_EVENTS_FALLBACK["p1"] = rows
    try:
        got = ddb.list_decision_events("p1", target="ms-goal", limit=100)
        assert [r["decision_id"] for r in got] == ["t-old"]
    finally:
        ddb._DECISION_EVENTS_FALLBACK.clear()
