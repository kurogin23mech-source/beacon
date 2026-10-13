"""done 時に受入条件の各節を機械で突き合わせる — ms-166 e-6898.

これまで「Done when」の各節が満たされたかは誰も機械で見ておらず、判定は AI の
自己申告だけで通った。未達でも done にできた。

実例 (2026-10-08 に確定): e-6600 の受入条件は 3 節で、3 つ目は「規則が work-item
class 共通 (sales/dev で分岐しない)」だった。実際には cancel について開発と営業で
規則が分岐しており、実装者自身が最後のコミットでその主張を撤回したうえで done の
ままだった。**この実例に機構を当てて赤くなることが、このタスクの受入条件 4 項目目**
なので、下の ``TestTheRealUnmetExample`` がそれを測る。

機構が要求するのは「正しさ」ではなく「主張と根拠を出すこと」。機械は節が意味的に
満たされたかを判定できないので、できるのは記録義務を課すところまで。未達 (partial /
not-met) は done を止めない —— 止めると正直な部分報告が不利になり「全部 met」と
書く方に圧力がかかる。止めるのは判定を一切出さない素通りだけ。
"""

import os
import subprocess
import sys

import pytest

import acceptance
import terminal_gate
import work_model


# ---------------------------------------------------------------------------
# 節の切り出し — 実データで観測した 4 形式
# ---------------------------------------------------------------------------

class TestSplitClauses:
    def test_slash_separated_paragraph(self):
        """e-6911 / e-6898 / e-6899 がこの形。"""
        assert acceptance.split_clauses("A が成り立つ / B が成り立つ / C が成り立つ") == [
            "A が成り立つ", "B が成り立つ", "C が成り立つ"]

    def test_numbered_lines(self):
        """e-6918 / e-6937 / e-6923 がこの形。"""
        assert acceptance.split_clauses("1. 一つ目\n2. 二つ目\n3) 三つ目") == [
            "一つ目", "二つ目", "三つ目"]

    def test_bullet_lines(self):
        """e-6621 / e-6633 がこの形。"""
        assert acceptance.split_clauses("- 一つ目\n- 二つ目") == ["一つ目", "二つ目"]

    def test_inline_parenthesised_numbers(self):
        """e-6890 / e-6891 / e-6893 がこの形。"""
        assert acceptance.split_clauses("(1) 一つ目である (2) 二つ目である") == [
            "一つ目である", "二つ目である"]

    def test_separator_inside_parens_is_not_a_split(self):
        """節の途中で切らない。

        受入条件は括弧内に補足を持つことが多い。素の split はその中の区切りでも
        割ってしまい、関門が意味を成さない断片への判定を要求することになる。
        """
        text = ("警告を出す (どの種別が存在するか / 綴りの近い候補を添える) / "
                "既知の種別の挙動は変えない")
        clauses = acceptance.split_clauses(text)
        assert len(clauses) == 2
        assert "綴りの近い候補" in clauses[0]

    def test_wrapped_lines_are_folded(self):
        clauses = acceptance.split_clauses("(1) 長い節が\n    折り返している (2) 次の節")
        assert clauses[0] == "長い節が 折り返している"

    def test_empty_means_no_clauses(self):
        assert acceptance.split_clauses("") == []
        assert acceptance.split_clauses("   ") == []

    def test_unsplittable_text_is_one_clause_not_zero(self):
        """0 節にしない。

        0 節は「検査すべきものが無い」と読めてしまい、関門の抜け道になる。
        """
        assert acceptance.split_clauses("割れない 1 文") == ["割れない 1 文"]

    def test_inline_number_late_in_text_is_not_a_split(self):
        """本文中の番号参照を節区切りと誤読しない。"""
        text = "この規則は過去の判断 (1) と (2) を踏まえている"
        assert acceptance.split_clauses(text) == [text]


# ---------------------------------------------------------------------------
# 判定の受け取り
# ---------------------------------------------------------------------------

CRITERIA_3 = "A が成り立つ / B が成り立つ / C が成り立つ"


class TestVerdictParsing:
    def test_full_form(self):
        assert acceptance.parse_verdict_arg("2:partial:途中まで", 3) == (
            2, "partial", "途中まで")

    def test_evidence_is_optional(self):
        assert acceptance.parse_verdict_arg("1:met", 3) == (1, "met", "")

    def test_evidence_may_contain_colons(self):
        _, _, ev = acceptance.parse_verdict_arg("1:met:commit abc:def を見た", 3)
        assert ev == "commit abc:def を見た"

    @pytest.mark.parametrize("bad,needle", [
        ("", "空です"),
        ("met", "解けません"),
        ("x:met", "数字ではありません"),
        ("9:met", "範囲外"),
        ("0:met", "範囲外"),
        ("1:done", "未知"),
    ])
    def test_bad_forms_say_how_to_recover(self, bad, needle):
        """エラー文が次の一手を名指しする (原則 3)。"""
        with pytest.raises(ValueError) as exc:
            acceptance.parse_verdict_arg(bad, 3)
        assert needle in str(exc.value)


class TestAdjudicationCoversEveryClause:
    def test_all_clauses_adjudicated(self):
        adj = acceptance.build_adjudication(
            CRITERIA_3, ["1:met:根拠1", "2:met:根拠2", "3:not-met:まだ"])
        assert adj["state"] == "adjudicated"
        assert adj["clause_count"] == 3
        assert [c["verdict"] for c in adj["clauses"]] == ["met", "met", "not-met"]
        assert adj["clauses"][2]["clause"] == "C が成り立つ"

    def test_missing_clause_is_refused_and_named(self):
        with pytest.raises(ValueError) as exc:
            acceptance.build_adjudication(CRITERIA_3, ["1:met"])
        msg = str(exc.value)
        assert "2 節に判定がありません" in msg
        assert "B が成り立つ" in msg and "C が成り立つ" in msg

    def test_duplicate_verdict_for_one_clause_is_refused(self):
        with pytest.raises(ValueError) as exc:
            acceptance.build_adjudication(
                CRITERIA_3, ["1:met", "1:not-met", "2:met", "3:met"])
        assert "2 回" in str(exc.value)

    def test_verdicts_are_ordered_by_clause_not_by_argument(self):
        adj = acceptance.build_adjudication(
            CRITERIA_3, ["3:met", "1:partial", "2:met"])
        assert [c["index"] for c in adj["clauses"]] == [1, 2, 3]
        assert adj["clauses"][0]["verdict"] == "partial"


# ---------------------------------------------------------------------------
# 関門 — 止めるものと止めないもの
# ---------------------------------------------------------------------------

class TestGate:
    def test_no_verdicts_is_refused(self):
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired) as exc:
            terminal_gate.require_ac_adjudication("task done", CRITERIA_3)
        msg = str(exc.value)
        assert "3 節" in msg
        assert "--ac" in msg and "--ac-waive" in msg
        # 節が列挙されている (呼び出し側が受入条件を読み直さなくて済む)
        assert "A が成り立つ" in msg

    def test_unmet_clause_does_not_block(self):
        """未達でも done は通る。

        止めると正直な部分報告が不利になり、「全部 met」と書く方に圧力がかかる。
        隠さず残す方が監査価値が高い。
        """
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3,
            verdicts=["1:met", "2:not-met:未着手", "3:partial:半分"])
        assert adj["state"] == "adjudicated"
        assert len(acceptance.unmet_clauses(adj)) == 2

    def test_waive_is_recorded_as_debt_not_silence(self):
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3, waive="機械が解決を推定して閉じる経路")
        assert adj["state"] == acceptance.WAIVED
        assert adj["waive_reason"] == "機械が解決を推定して閉じる経路"
        # 何節あったかは残す (= あとから「何を見なかったか」を数えられる)
        assert adj["clause_count"] == 3

    def test_verdicts_and_waive_together_are_refused(self):
        with pytest.raises(ValueError) as exc:
            terminal_gate.require_ac_adjudication(
                "task done", CRITERIA_3, verdicts=["1:met"], waive="なぜか")
        assert "not both" in str(exc.value)

    def test_empty_criteria_is_recorded_not_waved_through(self):
        """受入条件が無いタスクを抜け道にしない (AC 3 項目目)。

        止めはしない (既存タスクの多くは受入条件を持たない) が、素通りもさせない。
        ``no-criteria`` として必ず記録に残るので、母集団を数えられる。
        """
        adj = terminal_gate.require_ac_adjudication("task done", "")
        assert adj["state"] == acceptance.NO_CRITERIA
        assert adj["clause_count"] == 0

    def test_gate_error_is_a_value_error(self):
        """本番 API が追加配線なしで 400 に変換できる。"""
        assert issubclass(terminal_gate.AcceptanceAdjudicationRequired, ValueError)


# ---------------------------------------------------------------------------
# 押印層に在ること — handler ではなく遷移そのものの性質にする (e-6893 と同じ形)
# ---------------------------------------------------------------------------

class TestGateLivesInTheStampingLayer:
    def _item(self, criteria=CRITERIA_3):
        return {"id": "e-1", "status": "todo", "acceptance_criteria": criteria}

    def test_mark_done_refuses_without_verdicts(self):
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired):
            work_model.mark_done(self._item(), reason="やった")

    def test_mark_done_records_the_adjudication(self):
        item = work_model.mark_done(
            self._item(), reason="やった",
            ac_verdicts=["1:met:根拠1", "2:met:根拠2", "3:met:根拠3"])
        adj = item["meta"]["ac_adjudication"]
        assert adj["state"] == "adjudicated"
        assert adj["clauses"][0]["evidence"] == "根拠1"

    def test_the_claim_is_retrievable_afterwards(self):
        """AC 2 項目目: 後から「どの節をどう満たしたと主張したか」を引ける。"""
        item = work_model.mark_done(
            self._item(), reason="やった",
            ac_verdicts=["1:met:commit abc1234", "2:partial:半分", "3:not-met:未着手"])
        adj = item["meta"]["ac_adjudication"]
        by_clause = {c["clause"]: (c["verdict"], c["evidence"])
                     for c in adj["clauses"]}
        assert by_clause["A が成り立つ"] == ("met", "commit abc1234")
        assert by_clause["C が成り立つ"] == ("not-met", "未着手")

    def test_acknowledge_on_the_reason_gate_does_not_waive_this_one(self):
        """理由の免除は受入条件の免除ではない。

        関門が 2 つある以上、片方の免除でもう片方が開いてはいけない。
        """
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired):
            work_model.mark_done(self._item(), acknowledge=True)

    def test_item_without_criteria_still_closes(self):
        item = work_model.mark_done(
            {"id": "e-2", "status": "todo"}, reason="やった")
        assert item["status"] == "done"
        assert item["meta"]["ac_adjudication"]["state"] == acceptance.NO_CRITERIA


# ---------------------------------------------------------------------------
# AC 4 項目目 — 現に未達だった実例に当てて赤くなることを測る
# ---------------------------------------------------------------------------

#: e-6600 の受入条件 (2026-10-08 時点の実データ)。3 つ目が未達だった。
E6600_CRITERIA = (
    "activity の done/cancel が reason を必須で受ける(dev task と同一規則) / "
    "遷移が outcome を記録に運ぶ / "
    "規則が work-item class 共通(sales/dev で分岐しない)"
)


class TestTheRealUnmetExample:
    """e-6600 の 3 つ目の節に当てる (このタスクの受入条件 4 項目目)。

    当時: 3 節のうち 3 つ目が未達で、実装者自身が最後のコミットで「全 work-item
    class で同一の規則」という主張を撤回していた。それでも done になった。
    機構が在れば何が起きるはずだったかを、実データの文面で測る。
    """

    def test_the_criteria_splits_into_three_clauses(self):
        clauses = acceptance.split_clauses(E6600_CRITERIA)
        assert len(clauses) == 3
        assert "work-item class 共通" in clauses[2]

    def test_closing_it_blind_is_refused(self):
        """当時の呼び出し方 (理由だけ) では閉じられない。"""
        item = {"id": "e-6600", "status": "todo",
                "acceptance_criteria": E6600_CRITERIA}
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired):
            work_model.mark_done(item, reason="3 節を満たしたと判断")

    def test_the_retracted_claim_is_recorded_as_not_met(self):
        """撤回した主張が、記録の上で未達として残る。"""
        item = work_model.mark_done(
            {"id": "e-6600", "status": "todo",
             "acceptance_criteria": E6600_CRITERIA},
            reason="2 節は満たしたが 3 つ目は未達",
            ac_verdicts=[
                "1:met:共有の理由関門に寄せた",
                "2:met:outcome を記録に運ぶようにした",
                "3:not-met:cancel について開発と営業で規則が分岐しており、"
                "しかも営業の方が厳しい逆向きの非対称が残っている",
            ])
        unmet = acceptance.unmet_clauses(item["meta"]["ac_adjudication"])
        assert len(unmet) == 1
        assert unmet[0]["index"] == 3
        assert "分岐" in unmet[0]["evidence"]

    def test_the_unmet_clause_is_surfaced_not_buried(self):
        """done は通るが、未達は出力で必ず見える。

        exit 0 で済ませると未達が記録の奥に沈み、「done になった」だけが
        読み手に残る。
        """
        item = work_model.mark_done(
            {"id": "e-6600", "status": "todo",
             "acceptance_criteria": E6600_CRITERIA},
            reason="部分達成",
            ac_verdicts=["1:met", "2:met", "3:not-met:規則が分岐している"])
        warning = acceptance.format_unmet_warning(item["meta"]["ac_adjudication"])
        assert warning
        assert "work-item class 共通" in warning
        assert "3." in warning

    def test_no_warning_when_every_clause_is_met(self):
        """境界: 全部 met のときに警告を出さない (ノイズを作らない)。"""
        item = work_model.mark_done(
            {"id": "e-6600", "status": "todo",
             "acceptance_criteria": E6600_CRITERIA},
            reason="全部満たした",
            ac_verdicts=["1:met", "2:met", "3:met"])
        assert acceptance.format_unmet_warning(
            item["meta"]["ac_adjudication"]) == ""


# ---------------------------------------------------------------------------
# この関門が覆わない範囲 — 1 経路だけ塞いで「閉じた」と言わない
# ---------------------------------------------------------------------------

class TestScopeIsStatedNotOverstated:
    """押印層に置いたからといって全経路を覆ったわけではない。

    ``work_model.mark_done`` は ``done`` を書く主経路だが、唯一ではない。
    ``rec["status"] = "done"`` を手で書く箇所が ``lib/`` に在り、
    ``terminal_gate.KNOWN_HANDWRITTEN_TERMINAL`` に理由付きで列挙されている
    (理由の関門 e-6893 が既に測った母集団)。それらは受入条件の判定も通らない。

    「全部塞いだ」と言わずに、**塞いでいない側が列挙されていること** を留める。
    e-6893 の docstring が一度同じ過大主張をして撤回しているので、同じ轍を
    踏まない。
    """

    def test_the_handwritten_bypass_population_is_enumerated(self):
        """手書き経路が台帳に在る (= 数えられる)。"""
        assert hasattr(terminal_gate, "KNOWN_HANDWRITTEN_TERMINAL"), (
            "手書き終端経路の台帳が無い。関門の覆う範囲を述べられない")
        ledger = terminal_gate.KNOWN_HANDWRITTEN_TERMINAL
        assert len(ledger) > 0, (
            "台帳が空。本当に 0 件ならこのテストを消してよいが、"
            "0 件だと主張する前に census で測ること")
        for key, reason in (ledger.items() if isinstance(ledger, dict)
                            else [(k, "") for k in ledger]):
            assert key, "台帳のキーが空"

    def test_this_module_does_not_claim_universal_coverage(self):
        """押印層の docstring が「唯一の経路」と言っていないこと。"""
        doc = work_model.mark_done.__doc__ or ""
        assert "NOT** MEAN" in doc or "NOT MEAN" in doc or "not the only way" in doc, (
            "mark_done の docstring が覆う範囲の限界を述べていない")


# ---------------------------------------------------------------------------
# 両フロント — 旗の入口が片側に無い非対称を作らない (e-6912 の型)
# ---------------------------------------------------------------------------

class TestBothFrontsCarryTheFlags:
    def test_bash_front_declares_the_flags(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "bin", "lib", "cmd_task.sh"),
                   encoding="utf-8").read()
        assert "--ac)" in src, "bash 側に --ac が無い"
        assert "--ac-waive)" in src, "bash 側に --ac-waive が無い"
        assert terminal_gate.AC_VERDICTS_ENV in src
        assert terminal_gate.AC_WAIVE_ENV in src

    def test_python_front_declares_the_flags(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "beacon_cli", "dispatch.py"),
                   encoding="utf-8").read()
        assert 'dest="ac_verdicts"' in src, "python 側に --ac が無い"
        assert 'dest="ac_waive"' in src, "python 側に --ac-waive が無い"
        assert terminal_gate.AC_VERDICTS_ENV in src

    def test_help_registry_lists_the_flags(self):
        """案内に載っていない旗は、AI からは存在しない (e-6911 で踏んだ型)。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        res = subprocess.run([os.path.join(root, "bin", "beacon"),
                              "help", "--json"],
                             capture_output=True, text=True, cwd=root,
                             timeout=120)
        assert res.returncode == 0, res.stderr
        import json as _json
        rows = [e for e in _json.loads(res.stdout)["commands"]
                if e.get("command", "").startswith("beacon task done")]
        assert rows, "help --json に beacon task done が無い"
        # 旗ごとに別の項目として載っていることを見る。連結した 1 本の文字列に
        # 部分一致させると、`--acknowledge` が `--ac` に一致して偽の緑になる
        # (このファイルの初版が実際にそうなっていた)。
        flags = [f.split()[0] for f in (rows[0].get("flags") or []) if f.strip()]
        assert "--ac" in flags, f"help に --ac が無い: {flags}"
        assert "--ac-waive" in flags, f"help に --ac-waive が無い: {flags}"
