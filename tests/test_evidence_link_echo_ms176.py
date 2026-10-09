"""ms-176 e-6604 — 証跡を『満たした予定』に溶接する検知 + echo。

営業の証跡 (Communication) は商談 (opp-) 直付けでも、満たした活動 (act-/nrt-) 直付けでも
記録できる。後者だけが下流に効く (``fold_phase_activities`` は linked_id のある活動を
証跡ベースで done にし、``communications_of(linked_id=…)`` が work-item 粒度の履歴になる)
のに、記録側は opp- を渡しても何も起きなかった — opp-4 の実データで 13 活動すべて evidence
空になった構造的原因。

ここでは 3 つを釘付けにする:

1. 検知 (``occupation.evidence_link_candidates``) が「target 直付け + 未消化な予定あり」を
   純粋読み取りで見分ける。
2. 整形 (``occupation.format_evidence_link_echo``) が空なら空文字 (= section ごと出さない、
   dm_pending と同じ contract)、出すときは綴じ直しコマンドに id を埋めて出す。
3. 記録 seam (CLI ``communication_add``) が echo を出し、しかし **止めない** (SPEC 方針3 =
   permissive、直付けも有効な選択)。全営業 Skill (メール / 議事録取込 / 日次取込) はこの
   1 つの seam を通るので、Skill ごとの prompt 追記ではなくここが構造の担保点。

最後の 2 本は drift ガード: CLI の証跡書き込み関数が検知を参照しなくなったら赤くなること
を AST で構造的に確かめ、その checker 自体が drift を見逃さないことも確かめる
(test-the-test — 緩い substring 一致は help 文や JSON タグで偽 pass する)。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402
import occupation  # noqa: E402
import sales_entities as se  # noqa: E402
import work_model as wm  # noqa: E402


def _sales_data():
    data = se.build_sales_project("Weld Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    opp = se.opportunity_add(data, "商談X", account_id="acc-1")
    return data, opp


@pytest.fixture
def sales_cwd(tmp_path, monkeypatch):
    data, opp = _sales_data()
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd, opp


def _record_via_cli(monkeypatch, *, target, summary="やり取り", direction="outbound",
                    channel="email"):
    for k in ("BEACON_COMM_SOURCE_REF", "BEACON_COMM_SOURCE_URL",
              "BEACON_COMM_BODY", "BEACON_COMM_OCCURRED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_COMM_TARGET", target)
    monkeypatch.setenv("BEACON_COMM_SUMMARY", summary)
    monkeypatch.setenv("BEACON_COMM_DIRECTION", direction)
    monkeypatch.setenv("BEACON_COMM_CHANNEL", channel)
    commands.cmd_communication_add()


# ---------------------------------------------------------------------------
# 1. 検知
# ---------------------------------------------------------------------------

def test_target_grain_lists_only_open_work_items():
    data, opp = _sales_data()
    a_open = se.activity_add(data, opp, "提案書を送る")
    a_done = se.activity_add(data, opp, "初回面談を実施")
    a_cancelled = se.activity_add(data, opp, "不要になった打診")
    _, done_item = se.find_activity(data, a_done)
    wm.mark_done(done_item, at="2026-09-01T00:00:00Z", actor="t", reason="t")
    se.activity_cancel(data, a_cancelled, reason="不要")

    res = occupation.evidence_link_candidates(data, opp)
    assert res["grain"] == "target"
    assert res["container_id"] == opp
    assert res["arm"] == "activities"
    assert [c["id"] for c in res["candidates"]] == [a_open]
    assert res["candidates"][0]["description"] == "提案書を送る"


def test_work_item_grain_has_nothing_to_propose():
    data, opp = _sales_data()
    act = se.activity_add(data, opp, "提案書を送る")
    res = occupation.evidence_link_candidates(data, act)
    # すでに予定へ溶接済 → 提案する候補は無い (grain で区別できる)
    assert res["grain"] == "work-item"
    assert res["candidates"] == []


def test_account_grain_reads_the_nurturing_arm():
    # 顧客 (acc-) は profession_manifest の Target-class ではない (ms-143 option A)
    # ので、manifest fallback 経由で nurturings を読めることを釘付けにする。
    data, _ = _sales_data()
    nrt = se.nurturing_add(data, "acc-1", "月次で情報提供")
    res = occupation.evidence_link_candidates(data, "acc-1")
    assert res["grain"] == "target" and res["arm"] == "nurturings"
    assert [c["id"] for c in res["candidates"]] == [nrt]


def test_unresolvable_parent_is_blank_not_an_error():
    data, _ = _sales_data()
    # 書き込み側 (add_evidence) が ValueError を投げる領域なので、検知は静かに空を返す。
    assert occupation.evidence_link_candidates(data, "mlst-1")["grain"] == ""
    assert occupation.evidence_link_candidates(data, "opp-99")["grain"] == ""


def test_detection_is_pure_read():
    data, opp = _sales_data()
    se.activity_add(data, opp, "提案書を送る")
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    occupation.evidence_link_candidates(data, opp)
    assert json.dumps(data, ensure_ascii=False, sort_keys=True) == before


# ---------------------------------------------------------------------------
# 2. 整形
# ---------------------------------------------------------------------------

def test_format_is_empty_when_there_is_nothing_to_say():
    data, opp = _sales_data()
    # 未消化の予定が無い商談 → 出さない (常に出る警告は読まれなくなる)
    assert occupation.format_evidence_link_echo(
        occupation.evidence_link_candidates(data, opp), evidence_id="comm-1") == ""
    act = se.activity_add(data, opp, "提案書を送る")
    # すでに予定へ溶接済 → 出さない
    assert occupation.format_evidence_link_echo(
        occupation.evidence_link_candidates(data, act), evidence_id="comm-1") == ""
    # 解決不能 → 出さない
    assert occupation.format_evidence_link_echo(
        occupation.evidence_link_candidates(data, "zzz-1"),
        evidence_id="comm-1") == ""


def test_format_names_the_refiling_command_with_ids_filled_in():
    data, opp = _sales_data()
    act = se.activity_add(data, opp, "提案書を送る")
    band = occupation.format_evidence_link_echo(
        occupation.evidence_link_candidates(data, opp), evidence_id="comm-7")
    assert act in band and "提案書を送る" in band
    # 綴じ直しが「思い出す作業」でなく「貼って実行」で済むこと (id が埋まっている)
    assert "beacon communication retarget comm-7" in band
    # 直付けも有効な選択である旨 (hard block でない) を明示する
    assert "直付け" in band


def test_format_truncates_a_long_candidate_list():
    data, opp = _sales_data()
    for i in range(11):
        se.activity_add(data, opp, f"活動{i}")
    band = occupation.format_evidence_link_echo(
        occupation.evidence_link_candidates(data, opp), evidence_id="comm-1",
        limit=3)
    assert "… 他 8 件" in band


# ---------------------------------------------------------------------------
# 3. 記録 seam (CLI) — echo は出るが止めない
# ---------------------------------------------------------------------------

def test_cli_echoes_candidates_but_still_records(sales_cwd, monkeypatch, capsys):
    cwd, opp = sales_cwd
    data = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    act = se.activity_add(data, opp, "提案書を送る")
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")

    _record_via_cli(monkeypatch, target=opp)
    out = capsys.readouterr().out
    assert "Recorded communication" in out          # 記録は通る (hard block しない)
    assert act in out and "retarget" in out         # 候補と綴じ直し口が出る

    saved = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    comms = se.communications_of(se.find_opportunity(saved, opp))
    assert len(comms) == 1 and comms[0]["linked_id"] == ""


def test_cli_is_silent_when_the_evidence_is_already_welded(sales_cwd, monkeypatch,
                                                          capsys):
    cwd, opp = sales_cwd
    data = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    act = se.activity_add(data, opp, "提案書を送る")
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")

    _record_via_cli(monkeypatch, target=act)
    out = capsys.readouterr().out
    assert "Recorded communication" in out
    assert "retarget" not in out                    # 溶接済なら何も言わない

    saved = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    comms = se.communications_of(se.find_opportunity(saved, opp))
    assert len(comms) == 1 and comms[0]["linked_id"] == act


def test_cli_is_silent_when_the_deal_has_no_open_plan(sales_cwd, monkeypatch,
                                                     capsys):
    _cwd, opp = sales_cwd
    _record_via_cli(monkeypatch, target=opp)
    out = capsys.readouterr().out
    assert "Recorded communication" in out and "retarget" not in out


# ---------------------------------------------------------------------------
# drift ガード — 証跡を書く CLI 関数は検知を必ず参照する (AST で構造抽出)
# ---------------------------------------------------------------------------

_EVIDENCE_WRITES = {"add_evidence", "communication_add"}
_DETECTION = "evidence_link_candidates"


def _cli_writers_missing_detection(source: str) -> list:
    """AST で「証跡を書くのに検知を参照しない CLI 関数」を返す (drift = 非空)。

    substring 一致 (``"evidence_link_candidates" in source``) では、help 文や
    コメントに名前が残っただけで偽 pass する。呼び出しノードを構造的に拾い、
    *同じ関数の中で* 書き込みと検知が対になっていることを確かめる。"""
    tree = ast.parse(source)
    offenders = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        called = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else "")
            if name:
                called.add(name)
        if called & _EVIDENCE_WRITES and _DETECTION not in called:
            offenders.append(fn.name)
    return offenders


def test_cli_evidence_writers_consult_the_detection():
    # 記録 seam は lib/commands.py の CLI verb 群。ここが唯一の user-facing 書き込み口
    # (営業 3 Skill も `beacon communication add` もここを通る)。lib/inward_inject.py
    # は擬似着信の注入 (シナリオ実行器が target を明示する検証用 harness) なので対象外、
    # sales_entities.communication_add は occupation へ委譲するだけの薄い frontend。
    offenders = _cli_writers_missing_detection(
        (LIB / "commands.py").read_text(encoding="utf-8"))
    assert offenders == [], (
        "証跡を書くのに紐付け候補の検知を通さない CLI 関数があります "
        f"(ms-176 e-6604 の echo が消える): {offenders}")


def test_the_guard_actually_fails_on_drift():
    drifted = (
        "import occupation\n"
        "def cmd_communication_add():\n"
        "    # evidence_link_candidates という名前はコメントにだけ在る\n"
        "    comm_id = occupation.add_evidence(data, t, summary=s)['id']\n"
        "    print('Recorded', comm_id)\n"
    )
    assert _cli_writers_missing_detection(drifted) == ["cmd_communication_add"]
    welded = (
        "import occupation\n"
        "def cmd_communication_add():\n"
        "    comm_id = occupation.add_evidence(data, t, summary=s)['id']\n"
        "    echo = occupation.format_evidence_link_echo(\n"
        "        occupation.evidence_link_candidates(data, t), evidence_id=comm_id)\n"
        "    print('Recorded', comm_id, echo)\n"
    )
    assert _cli_writers_missing_detection(welded) == []
