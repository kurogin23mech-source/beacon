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
        assert "--ac-verdict" in msg and "--ac-waive" in msg
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

class TestWaiveBoundary:
    """``--ac-waive`` の境界。空文字で関門が開かないこと。

    独立レビュー (low) の指摘: ``if waive:`` が正しく空を落とすことは静的に
    読めば分かるが、機械で保証されていなかった。``if waive is not None:`` の
    ような退行で空文字 waive が無条件に通っても、既存テストは緑のままだった。
    """

    @pytest.mark.parametrize("empty", ["", "   ", "\n", "\t "])
    def test_empty_waive_does_not_bypass_the_gate(self, empty):
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired):
            terminal_gate.require_ac_adjudication(
                "task done", CRITERIA_3, waive=empty)

    def test_empty_waive_with_verdicts_is_not_a_conflict(self):
        """空の waive は「渡していない」と同じ (両方指定の拒否に当たらない)。"""
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3, verdicts=["1:met", "2:met", "3:met"],
            waive="   ")
        assert adj["state"] == "adjudicated"


class TestUncertainSplitIsNotSilent:
    """節の切り出しに確信が持てないとき、黙って進めないこと。

    独立レビュー (AX high) が実データで測った事故: 番号で列挙されているのに
    規則的に割れない文面を ``" / "`` で割ると、項目の**内側**を切る。実例
    e-2445 は「CLI / Web UI」の内側で切れ、節 1 が文の途中で終わり、節 2 が
    文の途中から始まる断片になっていた。関門が意味を成さない断片への判定を
    要求する = 機構が無いより悪い。
    """

    #: e-2445 の原文 (2026-10-08 時点の実データ、冒頭部分)。
    E2445 = ("1) beacon CLI の write 系コマンドに最小 inter-call delay を入れる、"
             " または 2) server 側で user-scope rate limit を CLI / Web UI で"
             "別バケット化する、 のいずれか。 4) ms-95 で確認する")

    def test_irregular_numbering_is_not_split_on_slashes(self):
        clauses, confidence = acceptance.split_clauses_with_confidence(self.E2445)
        assert confidence == acceptance.UNCERTAIN
        assert len(clauses) == 1, (
            "番号が不規則なのに割ってしまった。項目の内側を切る危険がある: "
            + repr(clauses))

    def test_regular_bare_numbering_does_split(self):
        """境界の反対側: 1 からの連番なら割る (割らなさすぎない)。"""
        clauses, confidence = acceptance.split_clauses_with_confidence(
            "1) 一つ目をする 2) 二つ目をする 3) 三つ目をする")
        assert confidence == acceptance.CONFIDENT
        assert len(clauses) == 3

    def test_inline_number_reference_is_not_a_split(self):
        """本文中の番号参照で割らない (e-519 の形)。"""
        text = ("packaging/homebrew/beacon.rb の caveats が: "
                "(1) cloud 利用なら beacon setup (2) local なら init")
        clauses, _ = acceptance.split_clauses_with_confidence(text)
        assert len(clauses) == 1

    def test_uncertainty_is_recorded_on_the_adjudication(self):
        adj = terminal_gate.require_ac_adjudication(
            "task done", self.E2445, verdicts=["1:met:確認した"])
        assert adj["split_confidence"] == acceptance.UNCERTAIN

    def test_uncertainty_is_surfaced_to_the_reader(self):
        adj = terminal_gate.require_ac_adjudication(
            "task done", self.E2445, verdicts=["1:met:確認した"])
        warning = acceptance.format_adjudication_warning(adj)
        assert "確信が持てません" in warning

    def test_uncertainty_is_named_in_the_refusal(self):
        """判定を出す前のエラー文でも不確実を伝える。"""
        with pytest.raises(terminal_gate.AcceptanceAdjudicationRequired) as exc:
            terminal_gate.require_ac_adjudication("task done", self.E2445)
        assert "確信が持てません" in str(exc.value)


class TestWaiveIsDisclosedNotSilent:
    """判定を出さずに通したことを、出力でも必ず見せること。

    独立レビュー (AX high) の指摘: 初版は waive を記録にだけ残し、CLI 出力にも
    API 応答にも出していなかった。しかもこの変更が新設した自動完了経路は毎回
    waive するので、黙っていることが常態になる形だった。
    """

    def test_waive_appears_in_the_warning(self):
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3, waive="機械が解決を推定して閉じる経路")
        warning = acceptance.format_adjudication_warning(adj)
        assert warning, "waive が出力に一切出ない"
        assert "判定を出さずに完了" in warning
        assert "機械が解決を推定" in warning

    def test_waive_warning_names_how_many_clauses_went_unchecked(self):
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3, waive="なぜか")
        assert "3 節" in acceptance.format_adjudication_warning(adj)

    def test_fully_met_adjudication_warns_about_nothing(self):
        """境界: 全部 met かつ確信ありなら何も出さない (ノイズを作らない)。"""
        adj = terminal_gate.require_ac_adjudication(
            "task done", CRITERIA_3, verdicts=["1:met", "2:met", "3:met"])
        assert acceptance.format_adjudication_warning(adj) == ""


class TestExemptionLedgersAreSeparate:
    """理由の免除と受入条件の免除を 1 つの台帳に束ねないこと。

    独立レビュー (low) の指摘: 共有すると「理由を持ちようがない」判断で登録した
    キーが、誰も判断していない「節ごとの判定を持ちようがない」まで自動的に
    免除する。
    """

    def test_two_distinct_ledgers_exist(self):
        assert terminal_gate.EXEMPT_CALL_SITES is not \
            terminal_gate.AC_EXEMPT_CALL_SITES

    def test_reason_exemption_key_does_not_waive_the_ac_gate(self):
        """理由の台帳のキーを AC 関門に渡しても通らない。"""
        with pytest.raises(ValueError) as exc:
            terminal_gate.require_ac_adjudication(
                "task done", CRITERIA_3, ac_exempt="some.reason_only_key")
        assert "AC_EXEMPT_CALL_SITES" in str(exc.value)


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
# 「通るが満たせない」罠を作らない
# ---------------------------------------------------------------------------

class TestNoReachableDeadEnd:
    """関門が効く経路には、必ず満たす手段が在ること。

    独立レビュー (保守性 medium) の指摘: 関門は ``item["acceptance_criteria"]``
    を汎用に読むので、記述子駆動 (``target_descriptor``) で作られた作業項目が
    同名 field を宣言すれば ``beacon target complete-work-item`` / ``close``
    経路でも関門が効く。ところがそれらの CLI には判定を渡す旗が無く、エラーは
    ``--ac-verdict`` を案内するのに **その旗が存在しない** = 詰みになる。

    今日この経路は到達不能 (どの記述子も ``acceptance_criteria`` を宣言して
    いない、実測) なので、CLI 旗を 2 動詞ぶん増やすより、**到達可能になった瞬間に
    赤くする** 方を選んだ。宣言した人が、満たす手段を併せて用意することになる。
    """

    @staticmethod
    def _descriptor_fields():
        """記述子が宣言する field 名を全部集める。"""
        import json as _json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        names = set()
        for base, _dirs, files in os.walk(root):
            if any(part in base for part in
                   (".git", ".worktrees", "node_modules", ".venv")):
                continue
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                path = os.path.join(base, fn)
                try:
                    with open(path, encoding="utf-8") as fh:
                        blob = _json.load(fh)
                except Exception:
                    continue
                stack = [blob]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        for k, v in node.items():
                            if k in ("fields", "work_item_fields",
                                     "item_fields", "extra_fields"):
                                if isinstance(v, dict):
                                    names.update(v.keys())
                                elif isinstance(v, list):
                                    names.update(
                                        x for x in v if isinstance(x, str))
                            stack.append(v)
                    elif isinstance(node, list):
                        stack.extend(node)
        return names

    def test_no_descriptor_declares_acceptance_criteria_yet(self):
        """宣言された瞬間に赤くなる。

        赤くなったら、この関門が ``beacon target`` 経路でも効くようになった、
        という意味。``cmd_target`` の ``complete-work-item`` / ``close`` に
        判定を渡す旗 (``--ac-verdict`` / ``--ac-waive``) を足してから、この
        テストを更新すること。旗を足さずに記述子を足すと「エラーは旗を案内するが
        その旗が無い」詰みになる。
        """
        declared = self._descriptor_fields()
        assert "acceptance_criteria" not in declared, (
            "記述子が acceptance_criteria を宣言した。beacon target の "
            "complete-work-item / close に --ac-verdict / --ac-waive を足して "
            "から、このテストを更新すること (いまは旗が無く、関門に当たると"
            "回復手段が無い)")


# ---------------------------------------------------------------------------
# 両フロント — 旗の入口が片側に無い非対称を作らない (e-6912 の型)
# ---------------------------------------------------------------------------

class TestBothFrontsCarryTheFlags:
    def test_bash_front_declares_the_flags(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "bin", "lib", "cmd_task.sh"),
                   encoding="utf-8").read()
        assert "--ac-verdict)" in src, "bash 側に --ac-verdict が無い"
        assert "--ac-waive)" in src, "bash 側に --ac-waive が無い"
        assert terminal_gate.AC_VERDICTS_ENV in src
        assert terminal_gate.AC_WAIVE_ENV in src

    def test_python_front_declares_the_flags(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "beacon_cli", "dispatch.py"),
                   encoding="utf-8").read()
        assert 'dest="ac_verdicts"' in src, "python 側に --ac-verdict が無い"
        assert '"--ac-verdict"' in src, "python 側の旗名が --ac-verdict でない"
        assert 'dest="ac_waive"' in src, "python 側に --ac-waive が無い"
        assert terminal_gate.AC_VERDICTS_ENV in src

    @pytest.mark.allow_repo_beacon_write
    def test_help_registry_lists_the_flags(self):
        """案内に載っていない旗は、AI からは存在しない (e-6911 で踏んだ型)。

        このテストは実リポジトリで本物の ``bin/beacon help --json`` を走らせる
        (help 面の真値を見るのが目的なので、一時プロジェクトでは測れない)。
        beacon CLI は atomic-write (tmp 書き込み → rename) をするので、スイート
        全体で走らせると ``.beacon/project.json.tmp`` が leak-guard の
        スナップショット比較と競合し、**無関係なテストの失敗として現れる**
        ことがあった (独立レビューが実測、2026-10-13)。副作用を起こし得ることを
        marker で宣言し、次の人が無関係なテストを疑わなくて済むようにする。
        """
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
        assert "--ac-verdict" in flags, f"help に --ac-verdict が無い: {flags}"
        assert "--ac-waive" in flags, f"help に --ac-waive が無い: {flags}"
