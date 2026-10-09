"""終端遷移の監査契約が「強制層」だけでなく「宣伝面」にも届いていることを留める
(ms-166 e-6912)。

e-6893 は「終端遷移 (done / cancelled) は監査エントリ (理由) を運ぶ」を押印層の
性質にした。だが規則を **強制する層** に入れただけでは、利用者が読む 3 つの宣伝面は
何も言わない。実測すると:

- README の CLI 表で 9 行が監査の旗を書いていなかった
- help レジストリ (``beacon help --json``) で 9 行が ``flags`` に入れていなかった
- python フロント (Windows / pipx) は ``task done`` / ``milestone done`` /
  ``milestone observe`` に ``--acknowledge`` を持たず、bash だけが受けていた
  (= 「理由を意図して省く」が片方のフロントで argparse エラーになる)

つまり ms-166 が掃討している「配線はあるのに実際には動かない」の宣伝面版。しかも
独立レビューは README の欠落を **手で 1 件** 見つけたが、機械で数えたら 6 件だった
—— 手で数えた母集団は毎回ずれる。

ここで留めるのは 2 つ:

1. 台帳 (``terminal_gate.KNOWN_HANDWRITTEN_TERMINAL``) の PR 3 行が主張する
   「理由はどこに在るか」が、実装と一致していること
   (``terminal_gate.PR_RATIONALE_CONTRACT``)。
2. 監査の旗が 4 面 (python パーサ / bash / README / help レジストリ) で揃っていること
   (``check-cli-help-drift.collect_audit_contract_drift``)。

**どちらの留めも「壊して赤を見る」試験を同梱する。** この実装中に実際に偽の緑を
1 度作った (別名表のキー集合で弾いたため全動詞を飛ばしており、README を壊しても緑の
ままだった)。緑のガードは信頼されるので、**捕まえるべき形ごとに赤くなることを実測
するまで信用してはならない**。
"""

import importlib.util
import os
import re
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import terminal_gate as tg  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_drift_module():
    """``scripts/check-cli-help-drift.py`` を module として読む (ハイフン名なので)。"""
    path = os.path.join(REPO, "scripts", "check-cli-help-drift.py")
    spec = importlib.util.spec_from_file_location("cli_help_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pr_handler_source() -> str:
    return open(os.path.join(REPO, "lib", "cmd_pr.py"), encoding="utf-8").read()


def _pr_frontend_sources() -> dict:
    return {
        "bin/lib/cmd_pr.sh": open(
            os.path.join(REPO, "bin", "lib", "cmd_pr.sh"), encoding="utf-8").read(),
        "beacon_cli/dispatch.py": open(
            os.path.join(REPO, "beacon_cli", "dispatch.py"), encoding="utf-8").read(),
    }


# ---------------------------------------------------------------------------
# 軸1 — 台帳の PR 行の主張が実装と一致しているか
# ---------------------------------------------------------------------------

def test_pr_rationale_contract_matches_implementation():
    bad = tg.pr_rationale_contract_violations(
        _pr_handler_source(), _pr_frontend_sources())
    assert bad == [], (
        "terminal_gate.PR_RATIONALE_CONTRACT の宣言と実装が食い違っている。宣言を"
        "直すか実装を直すこと。**散文だけ直して済ませてはならない** —— "
        "KNOWN_HANDWRITTEN_TERMINAL の PR 3 行の理由文は、この宣言の言い換えである"
        "必要がある:\n" + "\n".join(f"  {b}" for b in bad))


def test_ledger_prose_and_contract_agree_on_which_pr_rows_are_debt():
    """台帳の散文が「債務」と書く行と、宣言が ``absent`` と言う行を一致させる。

    3 行を一律「未整理の債務」と書いて実測とずれたのが元の欠陥なので、散文と宣言が
    別々に腐らないよう結んでおく。

    照合は散文の言い回しでなく ``tg.LEDGER_TAG_DEBT`` / ``LEDGER_TAG_SETTLED`` の
    印で行う (独立保守性レビュー #788 low: 文章を読みやすく直しただけでテストが
    割れる / 別行に同じ語が偶然出れば誤数えする、を避ける)。
    """
    prose_debt = {
        key.split(".", 1)[1]
        for key, text in tg.KNOWN_HANDWRITTEN_TERMINAL.items()
        if key.startswith("core.pr_") and tg.LEDGER_TAG_DEBT in text
    }
    declared_debt = {v for v, claim in tg.PR_RATIONALE_CONTRACT.items()
                     if claim == "absent"}
    # 宣言側には台帳に行を持たない動詞 (approve / request_changes) も居るので、
    # 台帳に行がある PR 動詞だけで比べる。
    ledger_verbs = {key.split(".", 1)[1] for key in tg.KNOWN_HANDWRITTEN_TERMINAL
                    if key.startswith("core.pr_")}
    assert prose_debt == (declared_debt & ledger_verbs), (
        "台帳の散文が『未整理の債務』と書く PR 行と、PR_RATIONALE_CONTRACT が "
        f"'absent' と宣言する行が一致しない。散文={sorted(prose_debt)} / "
        f"宣言={sorted(declared_debt & ledger_verbs)}")


class TestThePrContractGuardActuallyFails:
    """軸1 の留めが、捕まえるべき 4 つの形で本当に赤くなるか実測する。"""

    def test_catches_a_required_verb_losing_its_refusal(self):
        src = _pr_handler_source()
        mutated = src.replace(
            '    if not rationale:\n'
            '        print("Error: rationale is required for approve. '
            'Decision trail must be complete.", file=sys.stderr)\n'
            '        sys.exit(1)\n', "", 1)
        assert mutated != src, "変異が当たっていない (実装が変わったら変異も直す)"
        bad = tg.pr_rationale_contract_violations(mutated, _pr_frontend_sources())
        assert any("pr_approve" in b for b in bad)

    def test_catches_an_absent_verb_starting_to_read_the_reason(self):
        src = _pr_handler_source()
        mutated = src.replace(
            'def cmd_pr_close():\n'
            '    entry_id = os.environ.get("BEACON_ENTRY_ID", "")',
            'def cmd_pr_close():\n'
            '    entry_id = os.environ.get("BEACON_ENTRY_ID", "")\n'
            '    rationale = os.environ.get("BEACON_RATIONALE", "")', 1)
        assert mutated != src
        bad = tg.pr_rationale_contract_violations(mutated, _pr_frontend_sources())
        assert any("pr_close" in b for b in bad)

    def test_catches_one_frontend_forwarding_the_reason_for_an_absent_verb(self):
        """片方のフロントだけが理由を渡す形 (= 弱い側が見落とされる) を捕まえる。"""
        fronts = _pr_frontend_sources()
        before = fronts["bin/lib/cmd_pr.sh"]
        fronts["bin/lib/cmd_pr.sh"] = before.replace(
            'BEACON_ENTRY_ID="$entry_id" BEACON_JSON="$json_flag" '
            'python3 "$COMMANDS_PY" pr_close',
            'BEACON_ENTRY_ID="$entry_id" BEACON_RATIONALE="$rationale" '
            'BEACON_JSON="$json_flag" python3 "$COMMANDS_PY" pr_close', 1)
        assert fronts["bin/lib/cmd_pr.sh"] != before
        bad = tg.pr_rationale_contract_violations(_pr_handler_source(), fronts)
        assert any("pr_close" in b and "cmd_pr.sh" in b for b in bad)

    def test_catches_a_renamed_handler(self):
        src = _pr_handler_source().replace(
            "def cmd_pr_approve():", "def cmd_pr_approve_renamed():", 1)
        bad = tg.pr_rationale_contract_violations(src, _pr_frontend_sources())
        assert any("見つからない" in b for b in bad)

    def test_catches_the_python_frontend_passing_the_reason_via_a_literal_call(self):
        """python フロントが literal の handler 名で理由を渡す形を捕まえる。

        bash だけ変異させていたので python 側の経路が未測定だった
        (独立保守性レビュー #788 medium)。テキスト窓でなく構文木で見るようにした
        ので、dict の鍵を直接確かめられる。
        """
        fronts = _pr_frontend_sources()
        before = fronts["beacon_cli/dispatch.py"]
        fronts["beacon_cli/dispatch.py"] = before.replace(
            '            root, "pr_merge",\n'
            '            {"BEACON_ENTRY_ID": args.entry_id, "BEACON_JSON": json_env},',
            '            root, "pr_merge",\n'
            '            {"BEACON_ENTRY_ID": args.entry_id, '
            '"BEACON_RATIONALE": args.rationale or "", "BEACON_JSON": json_env},', 1)
        assert fronts["beacon_cli/dispatch.py"] != before, (
            "変異が当たっていない (dispatch.py の pr_merge 起動の書き方が変わったなら"
            "この変異も直すこと)")
        bad = tg.pr_rationale_contract_violations(_pr_handler_source(), fronts)
        assert any("pr_merge" in b and "dispatch.py" in b for b in bad), bad

    def test_catches_an_absent_verb_routed_through_the_dynamic_dispatch(self):
        """'absent' の verb を動的 handler 名の分岐へ合流させる改修を捕まえる。

        これが実装前に塞いでおきたかった穴そのもの: ``subcmd = "pr_" + cmd`` の
        分岐に merge / close を寄せると、verb の literal が理由の鍵の近傍から消える。
        旧実装 (テキスト窓) はこの形で「誰も渡していない」と答えて **緑のまま通した**。
        """
        fronts = _pr_frontend_sources()
        before = fronts["beacon_cli/dispatch.py"]
        fronts["beacon_cli/dispatch.py"] = before.replace(
            'if cmd in ("approve", "reject", "request-changes"):',
            'if cmd in ("approve", "reject", "request-changes", "merge"):', 1)
        assert fronts["beacon_cli/dispatch.py"] != before, "変異が当たっていない"
        bad = tg.pr_rationale_contract_violations(_pr_handler_source(), fronts)
        assert any("pr_merge" in b for b in bad), (
            "動的 handler 名の分岐に 'absent' の verb が合流したのに黙っている "
            f"(= 偽の安全): {bad}")

    def test_reports_a_dynamic_dispatch_whose_scope_cannot_be_read(self):
        """到達 verb が静的に絞れない動的起動は、緑に丸めず晒すこと。"""
        fronts = _pr_frontend_sources()
        before = fronts["beacon_cli/dispatch.py"]
        fronts["beacon_cli/dispatch.py"] = before.replace(
            'if cmd in ("approve", "reject", "request-changes"):',
            'if cmd in SOME_RUNTIME_SET:', 1)
        assert fronts["beacon_cli/dispatch.py"] != before, "変異が当たっていない"
        bad = tg.pr_rationale_contract_violations(_pr_handler_source(), fronts)
        assert any("絞れない" in b for b in bad), (
            f"到達 verb が読めない動的起動を黙って通した: {bad}")

    def test_stays_quiet_when_the_dynamic_dispatch_only_reaches_required_verbs(self):
        """現に在る動的起動 (approve/reject/request-changes) では黙ること。

        「晒す」が常時点灯だと読み手が見なくなるので、問題にならない形では出さない。
        """
        assert tg.pr_rationale_contract_violations(
            _pr_handler_source(), _pr_frontend_sources()) == []

    def test_does_not_fire_on_the_env_name_in_a_docstring(self):
        """名前が docstring に出ただけでは赤くしない (緩い substring 一致の回避)。"""
        src = _pr_handler_source().replace(
            "def cmd_pr_close():\n",
            'def cmd_pr_close():\n    """BEACON_RATIONALE は受け取らない。"""\n', 1)
        assert tg.pr_rationale_contract_violations(
            src, _pr_frontend_sources()) == []


# ---------------------------------------------------------------------------
# 軸2 — 監査の旗が 4 面で揃っているか
# ---------------------------------------------------------------------------

def test_audit_contract_reaches_every_surface():
    mod = _load_drift_module()
    report = mod.collect_audit_contract_drift()
    assert report["ok"], (
        "終端遷移の監査の旗 (--reason / --acknowledge) が 4 面で揃っていない。"
        "`python3 scripts/check-cli-help-drift.py` が直し方を出す:\n"
        + "\n".join(f"  {k}: {v}" for k, v in report.items()
                    if k != "ok" and v))


def test_the_population_is_not_empty():
    """母集団が空でないこと —— 空なら全 verb を飛ばした偽の緑 (実際に 1 度踏んだ)。

    別名表のキー集合で弾いたとき、正準名詞も自分自身へ写るので全動詞が除外され、
    README を壊しても緑のままだった。「違反ゼロ」と「見ていない」を区別する。
    """
    mod = _load_drift_module()
    parser = mod._audit_parser()
    if parser is None:
        pytest.skip("python フロントのパーサを読めない環境")
    leaves = mod._audit_leaf_flags(parser)
    alias_map = mod._noun_alias_map(parser) or {}
    alias_nouns = {k for k, v in alias_map.items() if k != v}
    gated = [p for p, o in leaves.items()
             if p and "--acknowledge" in o and p[0] not in alias_nouns]
    assert len(gated) >= 8, (
        f"監査の旗を受ける動詞が {len(gated)} 件しか見えていない。検査が母集団を"
        "取り落としている疑い (= 違反ゼロではなく、見ていないだけ)")


class TestTheAuditSurfaceGuardActuallyFails:
    """軸2 の留めが、各面を壊したときに本当に赤くなるか実測する。"""

    @staticmethod
    def _readme_copy(tmp_path, transform):
        src = os.path.join(REPO, "README.md")
        text = open(src, encoding="utf-8").read()
        mutated = transform(text)
        assert mutated != text, "変異が当たっていない"
        dest = tmp_path / "README.md"
        dest.write_text(mutated, encoding="utf-8")
        return dest

    def test_catches_a_readme_row_losing_the_flags(self, tmp_path):
        mod = _load_drift_module()
        dest = self._readme_copy(
            tmp_path,
            lambda t: t.replace(
                "| `beacon task done <id> (--reason <text> \\| --acknowledge) "
                "[-p progress] [--outcome <text>]` |",
                "| `beacon task done <id> [-p progress]` |", 1))
        report = mod.collect_audit_contract_drift(readme_path=dest)
        assert "task done" in report["missing_from_readme"]

    def test_catches_a_readme_row_deleted_entirely(self, tmp_path):
        """境界: 行が 0 件になっても落ちること (0 件で緑になる罠を潰す)。"""
        mod = _load_drift_module()
        dest = self._readme_copy(
            tmp_path,
            lambda t: re.sub(r"^\| `beacon meeting cancel .*\n", "", t,
                             count=1, flags=re.M))
        report = mod.collect_audit_contract_drift(readme_path=dest)
        assert "meeting cancel" in report["missing_from_readme"]

    def test_catches_the_help_registry_losing_the_flags(self, tmp_path):
        mod = _load_drift_module()
        src = os.path.join(REPO, "lib", "commands.py")
        text = open(src, encoding="utf-8").read()
        mutated = text.replace(
            '{"command": "beacon meeting cancel <mtg-id>", '
            '"flags": ["--reason <text>", "--acknowledge"]',
            '{"command": "beacon meeting cancel <mtg-id>", "flags": []', 1)
        assert mutated != text, "変異が当たっていない"
        dest = tmp_path / "commands.py"
        dest.write_text(mutated, encoding="utf-8")
        report = mod.collect_audit_contract_drift(commands_path=dest)
        assert "meeting cancel" in report["missing_from_help"]

    def test_catches_bash_losing_the_acknowledge_flag(self, tmp_path):
        """フロント非対称 (python だけ受ける) を捕まえる。"""
        mod = _load_drift_module()
        shutil.copytree(os.path.join(REPO, "bin"), tmp_path / "bin")
        fam = tmp_path / "bin" / "lib" / "cmd_task.sh"
        text = fam.read_text(encoding="utf-8")
        mutated = text.replace(
            '            --acknowledge) acknowledge="1"; shift ;;', "", 1)
        assert mutated != text, "変異が当たっていない"
        fam.write_text(mutated, encoding="utf-8")
        report = mod.collect_audit_contract_drift(
            bin_path=tmp_path / "bin" / "beacon")
        assert "task done" in report["missing_from_bash"]

    def test_catches_a_stale_exemption(self):
        """免除が腐る (動詞が消えたのに免除だけ残る) のを捕まえる。"""
        mod = _load_drift_module()
        mod.AUDIT_SURFACE_EXEMPT["task nonexistent"] = "腐った免除"
        try:
            report = mod.collect_audit_contract_drift()
        finally:
            del mod.AUDIT_SURFACE_EXEMPT["task nonexistent"]
        assert "task nonexistent" in report["stale_exempt"]


def test_audit_drift_is_wired_into_the_overall_report():
    """検査を書いただけで ``collect_drift`` に繋いでいない、を防ぐ (= CI が走らない)。

    「機構はあるのに消費側の配線が無い」は ms-166 が掃討している型そのものなので、
    検査自身にも当てる。
    """
    mod = _load_drift_module()
    report = mod.collect_drift()
    for key in ("audit_missing_from_readme", "audit_missing_from_help",
                "audit_missing_from_bash", "audit_suspicious_acknowledge",
                "audit_stale_exempt"):
        assert key in report, f"collect_drift の報告に {key} が無い (配線漏れ)"


# ---------------------------------------------------------------------------
# 軸3 — python フロントの終端動詞を **実際に走らせて** 確かめる
#
# 独立 AX レビュー (#788, high) が実測で見つけた退行への回帰試験。私は
# `p_opp_contract` の `--acknowledge` を「どの verb も許可しない死んだ宣伝」と読んで
# 削除したが、`cancel` 分岐が `args.acknowledge` を読むため
# `beacon opportunity contract cancel --reason ...` が AttributeError で落ちた
# (= この動詞が python / Windows / pipx フロントで 100% 不能になった)。
#
# 差分を読むだけでは見えない型: 「属性の参照を足した / 消した」と「パーサの引数を
# 足した / 消した」が別の hunk に散るので、**パーサを組んで実際に呼ぶ** までわからない。
# judge の skill_gaps もまさにこれを指摘している。
# ---------------------------------------------------------------------------

def _dispatch_module():
    sys.path.insert(0, REPO)
    import importlib
    mod = importlib.import_module("beacon_cli.dispatch")
    return mod


def _run_opportunity(argv):
    """``beacon opportunity ...`` を実際に parse → handler まで通し (rc, stderr) を返す。"""
    import contextlib
    import io
    from pathlib import Path
    mod = _dispatch_module()
    ns = mod.build_parser().parse_args(argv)
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = mod._handle_opportunity(Path(REPO), ns)
    return rc, err.getvalue() + out.getvalue()


def test_contract_cancel_with_only_a_reason_does_not_crash():
    """`--reason` だけの正しい呼び出しが例外で落ちないこと (#788 high の回帰試験)。

    存在しない契約 ID を渡すので「見つからない」で終わるのが正しい。ここで
    AttributeError / TypeError が出たら、パーサの引数と handler の参照がずれている。
    """
    rc, text = _run_opportunity(
        ["opportunity", "contract", "cancel", "ctr-does-not-exist", "--reason", "x"])
    assert rc != 2, (
        "`--reason` だけの呼び出しが stray フラグとして拒否された "
        f"(rc=2)。正しい呼び出しを断ってはならない: {text}")
    assert "AttributeError" not in text and "Traceback" not in text, (
        "handler がパーサに無い属性を読んでいる (= この動詞が python フロントで"
        f"不能になっている):\n{text}")


def test_contract_cancel_refuses_the_acknowledge_waiver():
    """契約の取消に監査の免除経路が **無い** ことを挙動で固定する。

    ``AUDIT_SURFACE_EXEMPT["opportunity contract"]`` の免除は「断り続けている」こと
    を前提にしている。実際に免除経路を足したら (= 理由なしで取消できるようにしたら)
    この試験が赤くなり、README / help の記述も直す必要が出る。
    削除前は `_flag_set` に `--acknowledge` が無かったため stray 判定を素通りし、
    `BEACON_ACKNOWLEDGE=1` が押印層まで流れて **python だけ理由なし取消が通る**
    認可の非対称になっていた。
    """
    rc, text = _run_opportunity(
        ["opportunity", "contract", "cancel", "ctr-does-not-exist", "--acknowledge"])
    assert rc == 2, (
        "`contract cancel --acknowledge` が拒否されていない。免除経路を意図して"
        f"足したなら、この試験と README / help の記述を一緒に直すこと: rc={rc} {text}")
    assert "--acknowledge" in text and "--reason" in text, (
        f"拒否メッセージが「何が使えないか」と「代わりに何を渡すか」を言っていない:\n{text}")


def test_contract_add_refuses_the_audit_flag_too():
    """非終端の verb でも監査の旗を黙って捨てないこと (全 verb で断る)。"""
    rc, text = _run_opportunity(
        ["opportunity", "contract", "add", "opp-x", "desc", "--acknowledge"])
    assert rc == 2, f"`contract add --acknowledge` が黙って受理された: rc={rc} {text}"


def test_activity_add_form_refuses_the_audit_flags():
    """活動の追加形 (非終端) が監査の旗を受理して捨てないこと。

    union パーサが done/cancel 用の旗を追加形にも渡せるため、env に載せない =
    黙って捨てる形になっていた (bash は `_guard_positional` で拒否しており、
    python 側だけが素通りする非対称だった)。
    """
    for flag in ("--reason", "--acknowledge", "--outcome"):
        argv = ["opportunity", "activity", "opp-x", "説明"]
        argv += [flag, "v"] if flag != "--acknowledge" else [flag]
        rc, text = _run_opportunity(argv)
        assert rc == 2, (
            f"`activity <opp> <desc> {flag}` が黙って受理された (旗は捨てられる): "
            f"rc={rc} {text}")
