"""ms-146 e-5336 — 照合しないと閉じられない。

e-5335 made the owner write a 十分ライン before starting. e-5345 put that line on
screen at the moment of the completion claim. This is the gate itself: the claim
does not go through until the line has actually been checked against, and WHICH
WAY the check came out survives as a record.

Three things are pinned here, matching the task's 受入条件:

  1. 終端フェイズへ前進するとき、着手時に書いた十分ラインが必ず提示される
     (the reference block, on BOTH routes into completion — advance and close).
  2. 照合を経ずに終端へ進む経路が存在しない — every route a declared class has
     into done is gated. ``close`` used to be a straight bypass: it marked a
     target done from ANY phase with no check at all, so the gate on the terminal
     phase could simply be walked around. Both routes are enumerated and driven
     here; a test that only drove ``advance`` would have called the gate closed
     while the hole was open (the ms-174 jump-bypass shape).
  3. 照合の結果 (満たした / 満たしていないが閉じる) が記録として残る, and the two
     endings stay distinguishable afterwards.

Plus the negative control the guard needs to be worth anything: a class that
declares NO 照合 keeps its old behaviour exactly, and the gate is demonstrated to
actually refuse (test-the-test) rather than passing because nothing reached it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import target_engine as te  # noqa: E402


# A class that declares the 照合: the verdict lives on the terminal phase, and
# only 満たした counts as having met the line.
UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [{"key": "purpose", "label": "上位目的", "type": "string",
                "required": True}],
    "phases": [
        {"key": "not_started", "label": "やってない"},
        {"key": "started", "label": "着手", "fields": [
            {"key": "enough_line", "label": "十分ライン", "type": "text",
             "required": True}]},
        {"key": "enough", "label": "十分やった", "terminal": True, "fields": [
            {"key": "enough_verdict", "label": "照合結果", "type": "string",
             "required": True,
             "choices": ["満たした", "満たしていないが閉じる"]}]},
    ],
    "completion_check": {"verdict_field": "enough_verdict",
                         "met_values": ["満たした"]},
}

# The same shape with NO 照合 declared — the negative control.
PLAIN = {
    "kind": "plain",
    "label": "ふつうの対象",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "pl-",
    "collection": "plains",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [{"key": "open", "label": "開"},
               {"key": "shut", "label": "閉", "terminal": True}],
}


def _started(desc=UNDERTAKING):
    data = {"name": "t"}
    rec = te.create_target(data, desc, label="セミナー原稿",
                           fields={"purpose": "9月セミナーで成約3件"}
                           if desc is UNDERTAKING else {})
    if desc is UNDERTAKING:
        te.advance_target(data, desc, rec["id"], to_phase="started",
                          fields={"enough_line": "事例3本まで。それ以上は磨かない"})
    return data, rec


# ---------------------------------------------------------------------------
# Route 1 — advance into the terminal phase.
# ---------------------------------------------------------------------------

def test_advance_into_terminal_refuses_without_the_verdict():
    data, rec = _started()
    with pytest.raises(te.TargetEngineError) as e:
        te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough")
    assert "照合" in str(e.value)
    # Refused BEFORE any write: the record has not crept into the terminal phase.
    assert te.current_phase(rec) == "started"


def test_advance_into_terminal_passes_with_the_verdict():
    data, rec = _started()
    _, _, new = te.advance_target(data, UNDERTAKING, rec["id"],
                                  to_phase="enough",
                                  fields={"enough_verdict": "満たした"})
    assert new == "enough"
    assert te.completion_check_status(UNDERTAKING, rec)["met"] is True


def test_advance_rejects_a_verdict_outside_the_declared_choices():
    data, rec = _started()
    with pytest.raises(te.TargetEngineError):
        te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                          fields={"enough_verdict": "だいたいOK"})
    assert te.current_phase(rec) == "started"


# ---------------------------------------------------------------------------
# Route 2 — close. The bypass this task exists to shut.
# ---------------------------------------------------------------------------

def test_close_refuses_from_a_non_terminal_phase():
    """The old hole: close marked a target done from ANY phase, so the terminal
    phase's 照合 could be skipped entirely by never going there."""
    data, rec = _started()
    with pytest.raises(te.TargetEngineError) as e:
        te.close_target(data, UNDERTAKING, rec["id"])
    assert "最終フェーズ" in str(e.value)
    assert rec.get("status") != "done"


def test_close_refuses_at_the_terminal_phase_without_a_verdict():
    """Belt and braces: even a record parked at the terminal phase with the
    verdict cleared out cannot be closed."""
    data, rec = _started()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                      fields={"enough_verdict": "満たした"})
    rec["enough_verdict"] = ""
    with pytest.raises(te.TargetEngineError) as e:
        te.close_target(data, UNDERTAKING, rec["id"])
    assert "照合" in str(e.value)
    assert rec.get("status") != "done"


def test_close_accepts_the_verdict_supplied_at_close_time():
    data, rec = _started()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                      fields={"enough_verdict": "満たした"})
    rec["enough_verdict"] = ""
    te.close_target(data, UNDERTAKING, rec["id"],
                    fields={"enough_verdict": "満たしていないが閉じる"})
    assert rec.get("status") == "done"
    assert rec["enough_verdict"] == "満たしていないが閉じる"


def test_close_refuses_to_write_any_field_but_the_verdict():
    """close is a completion verb, not a field writer — accepting arbitrary keys
    here would smuggle a general mutation path in through the gate."""
    data, rec = _started()
    te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                      fields={"enough_verdict": "満たした"})
    with pytest.raises(te.TargetEngineError) as e:
        te.close_target(data, UNDERTAKING, rec["id"],
                        fields={"purpose": "書き換え"})
    assert "purpose" in str(e.value)
    assert rec["purpose"] == "9月セミナーで成約3件"


# ---------------------------------------------------------------------------
# 受入条件3 — the result is kept, and the two endings stay apart.
# ---------------------------------------------------------------------------

def test_both_endings_are_recorded_and_remain_distinguishable():
    for verdict, met in (("満たした", True), ("満たしていないが閉じる", False)):
        data, rec = _started()
        te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                          fields={"enough_verdict": verdict}, actor="claude")
        te.close_target(data, UNDERTAKING, rec["id"], actor="claude")
        rows = [r for r in rec["phase_history"]
                if r.get("kind") == "completion_check"]
        assert len(rows) == 1, "照合の記録がちょうど1件残る"
        assert rows[0]["verdict"] == verdict
        assert rows[0]["met"] is met
        assert rows[0]["actor"] == "claude"
        assert rows[0]["at"]


def test_status_never_reads_an_unanswered_check_as_met():
    data, rec = _started()
    status = te.completion_check_status(UNDERTAKING, rec)
    assert status["recorded"] is False
    assert status["met"] is False


# ---------------------------------------------------------------------------
# Negative control — an undeclared class is untouched (test-the-test).
# ---------------------------------------------------------------------------

def test_class_without_completion_check_keeps_its_old_behaviour():
    """If this passed for the gated class too, the gate would be measuring
    nothing. A class that declares no 照合 still closes from any phase."""
    data, rec = _started(PLAIN)
    assert te.completion_check_config(PLAIN) == {}
    te.close_target(data, PLAIN, rec["id"])        # from 'open', non-terminal
    assert rec.get("status") == "done"


def test_undeclared_class_still_refuses_fields_at_close():
    data, rec = _started(PLAIN)
    with pytest.raises(te.TargetEngineError):
        te.close_target(data, PLAIN, rec["id"], fields={"anything": "x"})


def test_config_pointing_at_nothing_is_not_a_live_gate():
    """A completion_check with no verdict_field must not read as "gated" — a
    half-written declaration that silently blocked every close would be worse
    than no declaration."""
    assert te.completion_check_config({"completion_check": {}}) == {}
    assert te.completion_check_config(
        {"completion_check": {"verdict_field": "  "}}) == {}


# ---------------------------------------------------------------------------
# CLI — the close route end to end (bash passes --field through BEACON_FIELDS).
# ---------------------------------------------------------------------------

import json  # noqa: E402

import commands  # noqa: E402
import cmd_target  # noqa: E402


_ENV = ("BEACON_TARGET_CLASS", "BEACON_TARGET_ID", "BEACON_TO_PHASE",
        "BEACON_FIELDS", "BEACON_REASON", "BEACON_LABEL", "BEACON_JSON")


@pytest.fixture
def proj(tmp_path, monkeypatch):
    data, _rec = _started()
    data.update({"milestones": [], "target_classes": [UNDERTAKING]})
    (tmp_path / ".beacon").mkdir(exist_ok=True)
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_actor_str", lambda: "m/a")
    monkeypatch.setattr(cmd_target, "_actor_str", lambda: "m/a")
    # The ms-142 anti-self-close ban fires before this gate and would mask it;
    # the human signal is what a real owner supplies at this point.
    monkeypatch.setattr(cmd_target, "_ai_session_direct_completion_ban_active",
                        lambda: False)
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _close(monkeypatch, fields=""):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    if fields:
        monkeypatch.setenv("BEACON_FIELDS", fields)
    return cmd_target.cmd_target_close()


def test_cli_close_from_a_non_terminal_phase_shows_the_line_and_refuses(
        proj, monkeypatch, capsys):
    """The 十分ライン must be on screen on the close route too — and specifically
    on the attempt that bounces. Closing from 着手 is the case where a reference
    keyed off the CURRENT phase would hide the 着手 phase's own 十分ライン: the
    one line the 照合 is against."""
    with pytest.raises(SystemExit):
        _close(monkeypatch)
    cap = capsys.readouterr()
    assert "照合の材料" in cap.out
    assert "事例3本まで" in cap.out          # the 十分ライン itself, not just 上位目的
    assert "最終フェーズ" in cap.err


def test_cli_close_accepts_the_verdict_and_records_the_ending(
        proj, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_TO_PHASE", "enough")
    monkeypatch.setenv("BEACON_FIELDS", "enough_verdict=満たしていないが閉じる\n")
    cmd_target.cmd_target_advance()
    monkeypatch.delenv("BEACON_TO_PHASE")
    monkeypatch.delenv("BEACON_FIELDS")
    _close(monkeypatch)
    saved = json.loads((proj / ".beacon" / "project.json").read_text(
        encoding="utf-8"))
    rec = saved["undertakings"][0]
    assert rec["status"] == "done"
    rows = [r for r in rec["phase_history"] if r.get("kind") == "completion_check"]
    assert [r["verdict"] for r in rows] == ["満たしていないが閉じる"]
    assert rows[0]["met"] is False


# ---------------------------------------------------------------------------
# 独立レビュー由来 (PR #767, AX + 保守性) — 修正を固定する。
# ---------------------------------------------------------------------------

import target_descriptor as td  # noqa: E402


# completion_check を宣言しているのに終端フェーズが無い、壊れた記述子。CLI は
# これを作らせないが、target-class add --stdin / シード / 復元は CLI を通らない。
BROKEN_NO_TERMINAL = {
    "kind": "broken",
    "label": "こわれ",
    "profession": "dev",
    "type": "single-shot",
    "id_prefix": "bk-",
    "collection": "brokens",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [{"key": "enough_verdict", "label": "照合結果", "type": "string"}],
    "phases": [{"key": "open", "label": "開"}, {"key": "doing", "label": "作業中"}],
    "completion_check": {"verdict_field": "enough_verdict",
                         "met_values": ["満たした"]},
}


def test_no_terminal_phase_is_rejected_at_load_time():
    """保守性 review high: 検証が CLI の中だけに在ると、CLI を通らない書き込み
    経路 (--stdin / シード / 復元) が素通りする。記述子の検証器が持つべき。"""
    problems = td.validate_completion_check(BROKEN_NO_TERMINAL, "broken")
    assert problems, "終端フェーズ無しの completion_check は弾かれるべき"
    assert "terminal" in problems[0]


def test_verdict_field_off_the_terminal_phase_is_rejected():
    desc = dict(UNDERTAKING)
    desc["completion_check"] = {"verdict_field": "purpose"}   # 基本 field = 終端に無い
    problems = td.validate_completion_check(desc, "undertaking")
    assert problems and "purpose" in problems[0]


def test_met_value_outside_the_field_choices_is_rejected():
    desc = dict(UNDERTAKING)
    desc["completion_check"] = {"verdict_field": "enough_verdict",
                                "met_values": ["ぜんぶOK"]}
    problems = td.validate_completion_check(desc, "undertaking")
    assert problems and "選択肢" in problems[0]


def test_valid_declaration_passes_the_validator():
    assert td.validate_completion_check(UNDERTAKING, "undertaking") == []
    assert td.validate_completion_check(PLAIN, "plain") == []   # 未宣言も valid


def test_close_denies_by_default_when_the_class_has_no_terminal_phase():
    """保守性 review high (実測で再現した穴): 旧実装は
    `if terminals and current_phase(...) not in terminals` と書いており、終端が
    ゼロだと条件全体が False になってガードを素通りした。= ゲートが fail-open。
    終端ゼロは『制約なし』ではなく『記述子が壊れている』なので拒否する。"""
    data = {"name": "t"}
    rec = te.create_target(data, BROKEN_NO_TERMINAL, label="x",
                           fields={"enough_verdict": "満たした"})
    assert te.current_phase(rec) == "open"           # 終端ではない
    with pytest.raises(te.TargetEngineError) as e:
        te.close_target(data, BROKEN_NO_TERMINAL, rec["id"])
    assert "terminal" in str(e.value)
    assert rec.get("status") != "done"


def test_refusal_does_not_claim_material_is_on_screen_when_none_was_shown():
    """AX review (misleading): 照合の材料が空でも拒否文が常に『上に出ている
    照合の材料と突き合わせて』と言うと、AI は出ていない出力を探して壊れたと
    誤診する。材料を出していないときはそう言う。"""
    data, rec = _started()
    msg_shown = ""
    try:
        te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                          reference_shown=True)
    except te.TargetEngineError as e:
        msg_shown = str(e)
    msg_hidden = ""
    try:
        te.advance_target(data, UNDERTAKING, rec["id"], to_phase="enough",
                          reference_shown=False)
    except te.TargetEngineError as e:
        msg_hidden = str(e)
    assert "上に出ている" in msg_shown
    assert "上に出ている" not in msg_hidden
    assert "材料はありません" in msg_hidden
    # どちらの経路でも、記録の仕方は必ず示す (回復経路を落とさない)。
    for m in (msg_shown, msg_hidden):
        assert "enough_verdict" in m


def test_cli_advance_into_terminal_is_gated_too(proj, monkeypatch, capsys):
    """AX/保守性 review: close だけ CLI レベルで試験されており、advance 側の
    bash→環境変数→python の受け渡しが壊れても気づけない非対称があった。"""
    monkeypatch.setenv("BEACON_TARGET_CLASS", "undertaking")
    monkeypatch.setenv("BEACON_TARGET_ID", "ut-1")
    monkeypatch.setenv("BEACON_TO_PHASE", "enough")
    with pytest.raises(SystemExit):
        cmd_target.cmd_target_advance()
    cap = capsys.readouterr()
    assert "照合の材料" in cap.out
    assert "照合" in cap.err
    # BEACON_FIELDS 経由で照合結果を渡せば通る (受け渡しが生きていることの確認)。
    monkeypatch.setenv("BEACON_FIELDS", "enough_verdict=満たした\n")
    cmd_target.cmd_target_advance()
    assert "フェーズ進行" in capsys.readouterr().out
