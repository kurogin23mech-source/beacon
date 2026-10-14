"""fork で実施したレビューが親からも実施済みとして見える — ms-166 e-6899.

review-due トリガーは cwd ごとの ``.beacon/triggers/`` に発火し、
``beacon review done`` は **自分の cwd のぶんだけ** 消す。fork した作業フォルダ
(git worktree) で正しくレビューを実施しても、親の triggers にはファイルが残るので
親からは「未レビュー」に見え、approve / merge が止まる。

2026-10-08 の実例 (PR #787): レビューは fork で 3 巡 18 件実施され、採否はすべて
クラウドの判断記録に入り、CI の review-gate も pass していた。それでも親で
``beacon pr merge`` が止まり、目印は fork の triggers にだけ在った。

回復手段は 2 つあったが、どちらも筋が悪い:
  (1) 親で ``review done`` を打ち直す → 同一レビューの判断記録が重複して増える
  (2) ``BEACON_PR_REVIEW_OVERRIDE=1`` → 「飛ばした」痕跡が残るが実際には飛ばして
      いないので記録が嘘になる

直し方: **トリガーの発火場所は動かさず**、「実施した」という事実だけを作業フォルダ
横断の台帳に足し、関門がそれを差し引く。発火側を共有へ移すと、発火と読み取りが別
モジュールに在るため片方だけ移って namespace が割れる (この repo で過去に踏んだ罠)。
"""

import json
import os

import pytest

import commands_shared as cs


@pytest.fixture
def ledger_in_tmp(tmp_path, monkeypatch):
    """台帳と triggers dir を tmp に隔離する。

    実リポジトリの ``.git/`` に台帳を書かない (= このテスト自身が副作用で
    他のテストを壊さない)。
    """
    shared = tmp_path / "shared-git"
    shared.mkdir()
    triggers = tmp_path / "cwd-beacon" / "triggers"
    triggers.mkdir(parents=True)
    monkeypatch.setattr(cs, "_worktree_shared_dir", lambda: str(shared))
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    return {"shared": shared, "triggers": triggers,
            "ledger": shared / cs._REVIEW_DONE_DIR}


def _fire_due(triggers, review_type, pr_number):
    """review-due トリガーファイルを 1 件置く (PR-open の発火を模す)。"""
    path = triggers / cs.review_due_filename(review_type, pr_number)
    path.write_text(json.dumps({"review": review_type}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 台帳そのもの
# ---------------------------------------------------------------------------

class TestLedger:
    def test_records_and_reads_back(self, ledger_in_tmp):
        assert cs.record_review_done_across_worktrees("ax", "796") == \
            cs.RECORDED_SHARED
        assert cs._read_review_done_ledger() == {"796": ["ax"]}

    def test_is_idempotent(self, ledger_in_tmp):
        """同じ (PR, 種別) を 2 回足しても増えない。

        これが「重複した判断記録を作らずに済む」の土台 — 親で打ち直す必要が
        無くなるので、同一レビューに対する判断記録が二重に残らない。
        """
        cs.record_review_done_across_worktrees("ax", "796")
        cs.record_review_done_across_worktrees("ax", "796")
        assert cs._read_review_done_ledger()["796"] == ["ax"]

    def test_accumulates_multiple_types_per_pr(self, ledger_in_tmp):
        cs.record_review_done_across_worktrees("ax", "796")
        cs.record_review_done_across_worktrees("maintainability", "796")
        assert sorted(cs._read_review_done_ledger()["796"]) == [
            "ax", "maintainability"]

    def test_separates_prs(self, ledger_in_tmp):
        cs.record_review_done_across_worktrees("ax", "796")
        cs.record_review_done_across_worktrees("ax", "797")
        assert set(cs._read_review_done_ledger()) == {"796", "797"}

    def test_lives_in_the_shared_dir_not_the_cwd(self, ledger_in_tmp):
        """置き場が cwd ではなく共有ディレクトリであること。

        cwd に置くと worktree ごとに割れ、直したつもりで何も直らない。
        """
        cs.record_review_done_across_worktrees("ax", "796")
        assert (ledger_in_tmp["ledger"] / "pr-796-ax.json").exists()
        assert not (ledger_in_tmp["triggers"] / cs._REVIEW_DONE_DIR).exists()

    @pytest.mark.parametrize("rtype,pr", [("", "796"), ("ax", ""), ("", "")])
    def test_refuses_empty_inputs(self, ledger_in_tmp, rtype, pr):
        assert cs.record_review_done_across_worktrees(rtype, pr) == \
            cs.RECORDED_FAILED
        assert cs._read_review_done_ledger() == {}

    def test_missing_ledger_reads_as_empty_not_crash(self, ledger_in_tmp):
        assert cs._read_review_done_ledger() == {}

    def test_one_corrupt_file_does_not_blank_the_whole_ledger(self, ledger_in_tmp):
        """1 件の破損で台帳全体が空に見えないこと。

        全体が空に見えると、実施済みのレビューがまとめて未実施へ戻る。
        """
        cs.record_review_done_across_worktrees("ax", "796")
        (ledger_in_tmp["ledger"] / "pr-900-broken.json").write_text(
            "not json at all", encoding="utf-8")
        assert cs._read_review_done_ledger() == {"796": ["ax"]}

    def test_wrong_shape_file_is_ignored(self, ledger_in_tmp):
        ledger_in_tmp["ledger"].mkdir(parents=True, exist_ok=True)
        (ledger_in_tmp["ledger"] / "pr-901-x.json").write_text(
            '["ax"]', encoding="utf-8")
        assert cs._read_review_done_ledger() == {}

    def test_forget_drops_only_that_pr(self, ledger_in_tmp):
        cs.record_review_done_across_worktrees("ax", "796")
        cs.record_review_done_across_worktrees("ax", "797")
        cs.forget_review_done_across_worktrees("796")
        assert set(cs._read_review_done_ledger()) == {"797"}

    def test_forget_is_safe_when_absent(self, ledger_in_tmp):
        cs.forget_review_done_across_worktrees("999")   # 例外にならない
        assert cs._read_review_done_ledger() == {}


# ---------------------------------------------------------------------------
# AC 4: fork で打って親で関門が通ることを実測する
# ---------------------------------------------------------------------------

class TestParentSeesTheForksReview:
    """「いま赤になる経路が緑になる」を両方測る。

    ``triggers`` fixture は **親の** triggers dir を表す。fork が自分のぶんを
    消しても親のファイルは残る、という状況をそのまま再現している。
    """

    def test_parent_blocks_before_the_fork_records_it(self, ledger_in_tmp):
        """台帳が空なら親は止まる (= 修正前の挙動、赤い側)。"""
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        _fire_due(ledger_in_tmp["triggers"], "maintainability", "787")
        assert sorted(cs._pending_review_types_for_pr("787")) == [
            "ax", "maintainability"]

    def test_parent_passes_after_the_fork_records_it(self, ledger_in_tmp):
        """fork が台帳に足すと、親のトリガーが残っていても関門が通る (緑の側)。"""
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        _fire_due(ledger_in_tmp["triggers"], "maintainability", "787")
        # fork で実施 (fork は自分の triggers を消すが、親のファイルは残る)
        cs.record_review_done_across_worktrees("ax", "787")
        cs.record_review_done_across_worktrees("maintainability", "787")
        # 親のトリガーファイルは依然として在る —— ここが肝
        assert (ledger_in_tmp["triggers"]
                / cs.review_due_filename("ax", "787")).exists()
        assert cs._pending_review_types_for_pr("787") == []

    def test_partial_fork_review_still_blocks_on_the_rest(self, ledger_in_tmp):
        """1 種別だけ実施した状態では、残りで止まる (差し引きが過剰でない)。"""
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        _fire_due(ledger_in_tmp["triggers"], "maintainability", "787")
        cs.record_review_done_across_worktrees("ax", "787")
        assert cs._pending_review_types_for_pr("787") == ["maintainability"]

    def test_ledger_for_another_pr_does_not_unblock_this_one(self, ledger_in_tmp):
        """別 PR の実施で誤って通らないこと (境界)。"""
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        cs.record_review_done_across_worktrees("ax", "796")
        assert cs._pending_review_types_for_pr("787") == ["ax"]

    def test_no_triggers_means_nothing_pending(self, ledger_in_tmp):
        assert cs._pending_review_types_for_pr("787") == []


# ---------------------------------------------------------------------------
# AC 3: 「飛ばした」の抜け道を実施済みの再登録に流用しなくて済む
# ---------------------------------------------------------------------------

class TestNoNeedForTheSkipEscapeHatch:
    def test_override_is_not_needed_once_the_fork_recorded_it(self, ledger_in_tmp,
                                                              monkeypatch):
        """override 環境変数を立てずに関門が通ること。

        修正前の回復手段は ``BEACON_PR_REVIEW_OVERRIDE=1`` だったが、それは
        「レビューを飛ばした」という **嘘の監査痕跡** を残す。台帳があれば
        override を使わずに通る。
        """
        monkeypatch.delenv("BEACON_PR_REVIEW_OVERRIDE", raising=False)
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        cs.record_review_done_across_worktrees("ax", "787")
        assert cs._pending_review_types_for_pr("787") == []
        assert os.environ.get("BEACON_PR_REVIEW_OVERRIDE") is None


# ---------------------------------------------------------------------------
# 置き場の解決と、落ちたときの可視性
# ---------------------------------------------------------------------------

class TestSharedDirResolution:
    def test_resolves_inside_this_repository(self):
        """実リポジトリで共有ディレクトリが取れること (= 既定で効く)。"""
        shared = cs._worktree_shared_dir()
        assert shared, "git repo の中なのに共有ディレクトリが取れない"
        assert os.path.isabs(shared)

    def test_falls_back_to_cwd_triggers_when_unavailable(self, tmp_path,
                                                         monkeypatch):
        """git が無い / repo 外では cwd の triggers に落ち、落ちたことを返す。"""
        triggers = tmp_path / "triggers"
        triggers.mkdir()
        monkeypatch.setattr(cs, "_worktree_shared_dir", lambda: None)
        monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
        directory, shared = cs._review_done_dir()
        assert directory == str(triggers / cs._REVIEW_DONE_DIR)
        assert shared is False

    def test_fallback_is_announced_not_silent(self, monkeypatch, capsys):
        """git 呼び出しの失敗を黙らせない。

        間欠的に落ちると「fork のレビューが親に伝わる回と伝わらない回がある」
        という、分裂していた従来より診断しにくい状態になる。
        """
        def _boom(*_a, **_kw):
            raise OSError("git not found")
        monkeypatch.setattr(cs.subprocess, "run", _boom)
        assert cs._worktree_shared_dir() is None
        assert "fork で実施したレビューは親に伝わりません" in capsys.readouterr().err

    def test_outside_a_git_repo_is_quiet(self, monkeypatch):
        """repo 外は想定内なので警告を出さない (ノイズを作らない)。"""
        class _R:
            returncode = 128
            stdout = ""
        monkeypatch.setattr(cs.subprocess, "run", lambda *a, **k: _R())
        assert cs._worktree_shared_dir() is None


class TestProbeIsRegistered:
    def test_the_new_git_probe_is_in_the_census(self):
        """git に worktree を尋ねる箇所は台帳で数えられていること。

        台帳に足し忘れると ``scripts/check-worktree-probe-census.py`` が
        CI で落ちる。ここでは「落ちない状態である」ことを留める。
        """
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "scripts", "check-worktree-probe-census.py")
        spec = importlib.util.spec_from_file_location("_wpc", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert ("lib/commands_shared.py", "_worktree_shared_dir") in mod.CENSUS


# ---------------------------------------------------------------------------
# 独立レビュー (AX high ×3) が実測した 3 つの穴
# ---------------------------------------------------------------------------

class TestConcurrentWritesDoNotLoseRecords:
    """並行書き込みで記録が消えないこと (AX high)。

    初版は 1 つの JSON 辞書を読み込み → 自分のぶんを足す → 全体を書き戻す形で、
    judge がほぼ同時の 2 呼び出しで lost update を実測した。後から書いた側が古い
    スナップショットで上書きし、先の記録が跡形もなく消え、**エラーは一切出なかった**。

    この機能の想定ユースケースは「複数の fork worktree が同じ .git を共有しながら
    並行に書くこと」そのものなので、まさにその条件下で、直したはずの不具合が
    間欠的に自己再発する形だった。
    """

    def test_two_records_for_different_prs_both_survive(self, ledger_in_tmp):
        """judge の再現: A が書いた直後に B が書いても A が残る。"""
        cs.record_review_done_across_worktrees("ax", "787")
        cs.record_review_done_across_worktrees("maintainability", "500")
        ledger = cs._read_review_done_ledger()
        assert ledger.get("787") == ["ax"], "先に書いた記録が消えた"
        assert ledger.get("500") == ["maintainability"]

    def test_many_concurrent_writers_all_survive(self, ledger_in_tmp):
        """本当に並行に走らせて 1 件も落ちないことを測る。"""
        import threading
        pairs = [("ax", str(900 + i)) for i in range(12)]
        pairs += [("maintainability", str(900 + i)) for i in range(12)]
        threads = [threading.Thread(
            target=cs.record_review_done_across_worktrees, args=(t, pr))
            for t, pr in pairs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ledger = cs._read_review_done_ledger()
        for rtype, pr in pairs:
            assert rtype in (ledger.get(pr) or []), f"{pr}/{rtype} が落ちた"

    def test_one_fact_one_file(self, ledger_in_tmp):
        """read-modify-write が構造的に無いこと = 1 事実 1 ファイル。

        1 つの共有 blob に戻すと lost update が復活するので、形そのものを留める。
        """
        cs.record_review_done_across_worktrees("ax", "787")
        cs.record_review_done_across_worktrees("maintainability", "787")
        files = sorted(p.name for p in ledger_in_tmp["ledger"].iterdir())
        assert files == ["pr-787-ax.json", "pr-787-maintainability.json"]

    def test_no_tmp_file_is_left_behind(self, ledger_in_tmp):
        """tmp → rename で書くが、残骸を置かない。"""
        cs.record_review_done_across_worktrees("ax", "787")
        assert not [p for p in ledger_in_tmp["ledger"].iterdir()
                    if p.name.endswith(".tmp")]


class TestTheClaimMatchesReality:
    """「横断で記録した」と名乗れるのは、本当に横断で記録できたときだけ (AX high)。

    初版は書き込みの成否だけを True/False で返し、共有置き場が取れたかを見て
    いなかった。その結果、非 git プロジェクト (営業職種など正規サポート対象) で
    原理的に真になりえない名乗りが毎回出て、しかも「共有置き場が取れない」警告と
    反対の確信表明が **同一コマンド実行内で両立** していた。
    """

    def test_shared_is_reported_when_shared(self, ledger_in_tmp):
        assert cs.record_review_done_across_worktrees("ax", "787") == \
            cs.RECORDED_SHARED

    def test_cwd_only_is_reported_when_not_shared(self, tmp_path, monkeypatch):
        triggers = tmp_path / "triggers"
        triggers.mkdir()
        monkeypatch.setattr(cs, "_worktree_shared_dir", lambda: None)
        monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
        assert cs.record_review_done_across_worktrees("ax", "787") == \
            cs.RECORDED_CWD_ONLY

    def test_the_three_states_are_distinct(self):
        """3 値が潰れていないこと (真偽値に戻すとこの区別が消える)。"""
        assert len({cs.RECORDED_SHARED, cs.RECORDED_CWD_ONLY,
                    cs.RECORDED_FAILED}) == 3

    def test_failure_is_reported_when_the_write_fails(self, ledger_in_tmp,
                                                      monkeypatch):
        def _boom(*_a, **_kw):
            raise OSError("disk full")
        monkeypatch.setattr(cs.os, "makedirs", _boom)
        assert cs.record_review_done_across_worktrees("ax", "787") == \
            cs.RECORDED_FAILED


class TestSubtractionIsDisclosedWithProvenance:
    """台帳で関門を差し引いたら、誰の記録で差し引いたかを必ず言う (AX high)。

    台帳はローカルのファイルなので **手で書けば偽装できる**。judge が実測した通り、
    手書きで「実施済み」に見せると関門が通る。これは旧機構 (cwd の trigger ファイル
    を消す) でも同じだったが、横断台帳は全 worktree に効くので 1 回の偽装の影響範囲
    が広い —— labor は同じで被害が N 倍になる。

    ファイルの編集そのものは防げない (ローカルファイルは安全境界ではない)。
    できるのは **黙って関門が外れないようにすること**: 何を誰の記録で差し引いたかを
    開示し、出自の無い記録はそう言う。
    """

    def test_record_carries_provenance(self, ledger_in_tmp):
        cs.record_review_done_across_worktrees("ax", "787")
        prov = cs.review_done_provenance("787")
        assert len(prov) == 1
        rec = prov[0]
        assert rec["pr"] == "787" and rec["review_type"] == "ax"
        assert rec["recorded_at"] and rec["recorded_in_cwd"]
        assert rec["shared"] is True

    def test_subtraction_is_announced(self, ledger_in_tmp, capsys):
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        cs.record_review_done_across_worktrees("ax", "787")
        capsys.readouterr()
        assert cs._pending_review_types_for_pr("787") == []
        err = capsys.readouterr().err
        assert "関門から差し引きました" in err
        assert "ax:" in err

    def test_nothing_is_announced_when_nothing_is_subtracted(self, ledger_in_tmp,
                                                             capsys):
        """境界: 差し引きが無いときは黙る (ノイズを作らない)。"""
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        capsys.readouterr()
        cs._pending_review_types_for_pr("787")
        assert "差し引きました" not in capsys.readouterr().err

    def test_a_hand_written_record_is_flagged_as_provenance_less(
            self, ledger_in_tmp, capsys):
        """judge と同じ偽装を再現し、出自が無いことが開示されること。

        偽装自体は止められないが、**黙っては通らない**。
        """
        ledger_in_tmp["ledger"].mkdir(parents=True, exist_ok=True)
        (ledger_in_tmp["ledger"] / "pr-787-ax.json").write_text(
            json.dumps({"pr": "787", "review_type": "ax"}), encoding="utf-8")
        _fire_due(ledger_in_tmp["triggers"], "ax", "787")
        capsys.readouterr()
        assert cs._pending_review_types_for_pr("787") == []
        err = capsys.readouterr().err
        assert "出自不明" in err or "出自がありません" in err, (
            "出自の無い記録で関門が外れたのに、そのことが開示されていない\n" + err)
