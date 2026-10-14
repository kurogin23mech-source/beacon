"""PR の識別が「番号だけ」でリポジトリを持っていなかったことの試験
(ms-166 e-6971 / e-6972)。

何が起きていたか
----------------
2026-10-13、別プロジェクトのセッション (営業ダッシュボード) から 2 件の報告を受け、
こちらで実コードに照合して事実と確認した。どちらも **同じ誤り** の別の現れで、
「同じ PR」と「同じ PR 番号」を同一視していた。単一リポジトリの前提では等価だが、
1 つのプロジェクトで複数リポジトリを管理すると崩れる。

1. **レビューに渡す差分が別物になる (e-6971)**。``beacon review context --pr N`` は
   ``gh pr diff <N>`` をリポジトリ指定なしで叩いていた。gh は cwd のリポジトリに
   対して番号を解決するので、別リポジトリの PR を見るつもりで叩くと
   「cwd のリポジトリの同じ番号」の差分が **エラーでなく成功として** 返る。
   しかも bundle の ``target_ref`` は ``"PR #3"`` だけで、どのリポジトリか記録に
   残らない。その上で ``beacon review done`` が「実施した」と記録して approve /
   merge の関門を外す —— **別のコードを真面目にレビューした結果が品質の証拠として
   通る**。独立レビューの価値は独立性だが、対象が違っていたら独立性以前に無意味。

2. **PR の記録が登録できない (e-6972)**。``beacon pr add`` の二重登録ガードが鍵を
   PR 番号だけにしていた。報告された症状: ms-33 のコードだけ別リポジトリにある
   構成で、別リポの PR#3 を登録しようとすると同じプロジェクトに在る PR#3 の記録と
   衝突して exit 2 で止まる。回避する旗が無く、既存側が #129 まで在るため
   **その MS の PR は beacon に一切登録できない**。

この file が留めるもの
----------------------
1. リポジトリを取り出す場所が 1 つで、足りない入力を推測で埋めないこと
2. 記録の読み取りが構造化フィールドを先に見ること (url 再解析に戻さない)
3. ``pr add`` が「別リポジトリの同番号」を通し、「同じリポジトリの同番号」を止め、
   「判定できない」を **別の理由で** 止めること
4. 突合 (``pr sync``) の重複の数え方がリポジトリを見ること
5. 差分の収集が 3 箇所すべてリポジトリ込みの 1 経路を通ること (1 箇所だけ直して
   「全域に効く」と名乗らない)
6. 関門を外す記録 (台帳 / 判断記録) が、どのリポジトリの差分に対する実施かを持つこと
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_pr  # noqa: E402
import commands_shared as cs  # noqa: E402
import core  # noqa: E402


# --- 1. リポジトリの取り出しは 1 箇所、推測で埋めない ------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/kurogin23mech-source/beacon/pull/797",
     "kurogin23mech-source/beacon"),
    # 末尾付きでも取れる (GitHub 自身が出す形)
    ("https://github.com/o/r/pull/3/files", "o/r"),
    ("https://github.enterprise.co/team/app/pull/12?w=1", "team/app"),
    # --- 取れないものは空文字。**ここで捏造しないことが要点** ---
    ("", ""),
    ("772", ""),                      # 裸の番号はリポジトリを含まない
    ("--intent-stdin", ""),           # url 欄に旗の文字列が入った記録 (e-6902)
    ("どの PR か分からない説明文", ""),
    # scheme / host が足りない文字列は owner に host が入る形で「もっともらしく」
    # 誤るので受けない。
    ("github.com/beacon/pull/3", ""),
    ("/o/r/pull/3", ""),
])
def test_repo_is_extracted_only_when_it_is_actually_there(url, expected):
    assert core.extract_pr_repo_from_url(url) == expected, url


def test_repo_slug_shape_is_checked_in_one_place():
    assert core.is_repo_slug("owner/repo")
    assert core.is_repo_slug("kurogin23mech-source/beacon")
    assert not core.is_repo_slug("owner")          # 段数不足
    assert not core.is_repo_slug("a/b/c")          # 段数過多
    assert not core.is_repo_slug("")
    assert not core.is_repo_slug("https://github.com/o/r")


# --- 2. 記録の読み取りは構造化フィールドを先に見る ---------------------------

def test_recorded_repo_prefers_the_structured_field():
    """``meta.pr_repo`` が権威。url は drift するので後退経路。

    番号について同じ誤り (url 再解析を先にする) を 1 度やって独立レビューに実測で
    指摘されている (PR #793)。同じ形を repo で繰り返さない。
    """
    entry = {"type": "pr", "meta": {
        "pr_repo": "real/repo",
        # url は壊れている / 別物を指している
        "url": "--intent-stdin"}}
    assert core.recorded_pr_repo(entry) == "real/repo"

    entry2 = {"type": "pr", "meta": {
        "pr_repo": "real/repo",
        "url": "https://github.com/other/repo/pull/3"}}
    assert core.recorded_pr_repo(entry2) == "real/repo", (
        "構造化フィールドが在るのに url を優先した")


def test_recorded_repo_falls_back_to_the_url_for_old_records():
    """構造化フィールドが無い古い記録は url から読む (後方互換)。

    これが無いと、この変更の前に登録された健全な記録がすべて「リポジトリ不明」に
    落ちて、二重登録ガードが「判定できない」で止まり続ける (= 黙って厳しくする)。
    """
    entry = {"type": "pr", "meta": {
        "url": "https://github.com/o/r/pull/3", "pr_number": 3}}
    assert core.recorded_pr_repo(entry) == "o/r"


def test_recorded_repo_is_empty_when_nothing_says_which_repo():
    assert core.recorded_pr_repo({"type": "pr", "meta": {"pr_number": 3}}) == ""
    assert core.recorded_pr_repo({}) == ""


def test_pr_add_writes_the_repo_as_a_structured_field():
    """``core.pr_add`` が保存時に ``meta.pr_repo`` を落とすこと。

    url からは取れるが、**保存時に構造化フィールドへ落とす** のが要点 (番号について
    meta.pr_number が果たしている役割と対)。後から url を再解析する形に戻すと、
    url が drift した記録で識別が崩れる。
    """
    data = {"milestones": [{"id": "ms-1", "status": "in_progress", "entries": []}]}
    eid = core.pr_add(data, ms_id="ms-1",
                      url="https://github.com/o/r/pull/3", title="t")
    entry = next(e for e in data["milestones"][0]["entries"] if e["id"] == eid)
    assert entry["meta"]["pr_repo"] == "o/r", entry["meta"]
    assert entry["meta"]["pr_number"] == 3


# --- 3. pr add のガード: 通す / 止める / 別の理由で止める --------------------

def _entry(eid, url, pr_number=None, repo=None, status="in_review"):
    meta = {"url": url, "pr_number": pr_number}
    if repo is not None:
        meta["pr_repo"] = repo
    return {"id": eid, "type": "pr", "status": status, "meta": meta}


def _data(*entries):
    return {"milestones": [{"id": "ms-1", "status": "in_progress",
                            "entries": list(entries)}]}


def test_identity_rows_classify_the_repository_match():
    data = _data(
        _entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r"),
        _entry("e-2", "https://github.com/other/app/pull/3", 3, repo="other/app"),
        _entry("e-3", "--intent-stdin", 3),      # リポジトリ不明
    )
    rows = {r["eid"]: r["repo_match"]
            for r in cmd_pr._entries_for_pr_identity(data, "3", "o/r")}
    assert rows == {"e-1": cmd_pr.REPO_SAME,
                    "e-2": cmd_pr.REPO_DIFFERENT,
                    "e-3": cmd_pr.REPO_UNKNOWN}, rows


def test_a_new_prs_unknown_repo_makes_every_match_undeterminable():
    """これから足す側のリポジトリが分からない場合も「判定できない」。

    片側だけ見て判定すると、分からないほうを無視したことになる。
    """
    data = _data(_entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r"))
    rows = cmd_pr._entries_for_pr_identity(data, "3", "")
    assert [r["repo_match"] for r in rows] == [cmd_pr.REPO_UNKNOWN], rows


# --- pr add を **実際に走らせて** 通る / 止まるを測る -----------------------
#
# 分類の述語だけを測ると、`cmd_pr_add` がそれを使うのをやめても緑のままになる
# (「足した」と「全域に効く」は別)。実際に関数を走らせ、exit するかどうかで測る。

def _run_pr_add(monkeypatch, data, url, *, ms_id="ms-1"):
    """`cmd_pr_add` を走らせ、``(exit_code or None, stderr)`` を返す。"""
    saved = {}
    monkeypatch.setattr(cmd_pr, "load_project", lambda: data)
    monkeypatch.setattr(cmd_pr, "save_project",
                        lambda d: saved.setdefault("data", d))
    # GitHub へは行かない (タイトルだけ返す)
    monkeypatch.setattr(cmd_pr, "_fetch_gh_pr_info",
                        lambda u: {"title": "t", "body": "b", "commits": []})
    monkeypatch.setattr(cmd_pr, "_check_ms_status_for_write",
                        lambda ms, what: True)
    monkeypatch.setattr(cmd_pr, "_derive_pr_intent_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(cmd_pr, "_fire_pr_open_review_triggers",
                        lambda *a, **k: None)
    monkeypatch.setattr(cmd_pr, "_resolve_session_id", lambda: "sv-test")
    monkeypatch.setenv("BEACON_URL", url)
    monkeypatch.setenv("BEACON_MS_ID", ms_id)
    monkeypatch.setenv("BEACON_INTENT", "i")
    monkeypatch.setenv("BEACON_AUTHOR", "a")
    monkeypatch.setenv("BEACON_JSON", "1")
    err = io.StringIO()
    out = io.StringIO()
    code = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cmd_pr.cmd_pr_add()
    except SystemExit as e:  # noqa: PERF203
        code = e.code
    return code, err.getvalue(), saved.get("data")


def test_a_different_repositorys_same_number_can_be_recorded(monkeypatch):
    """**AC1 の前半**: 別リポジトリの同じ番号は登録できること。

    これが落ちていたのが報告された症状そのもの。既存側が #1..#129 まで在ると
    別リポ側の PR が一切登録できなかった。
    """
    data = _data(_entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r",
                        status="in_review"))
    code, err, saved = _run_pr_add(
        monkeypatch, data, "https://github.com/other/app/pull/3")
    assert code is None, f"別リポジトリの同番号を止めた (exit={code}): {err}"
    assert saved is not None, "保存されていない"
    added = [e for e in saved["milestones"][0]["entries"]
             if e.get("meta", {}).get("pr_repo") == "other/app"]
    assert len(added) == 1, "別リポジトリの記録が足されていない"


def test_the_same_repositorys_same_number_is_still_refused(monkeypatch):
    """**AC1 の後半**: 同じリポジトリの同じ番号は従来どおり止めること。

    緩めた側だけ測ると、ガードを丸ごと外しても緑になる。
    """
    data = _data(_entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r",
                        status="in_review"))
    code, err, saved = _run_pr_add(
        monkeypatch, data, "https://github.com/o/r/pull/3")
    assert code == 2, f"同じリポジトリの二重登録を通した (exit={code})"
    assert saved is None, "拒否したのに保存している (保存前に止まっていない)"
    assert "o/r" in err, "どのリポジトリの話か言っていない:\n" + err


def test_an_undeterminable_repository_is_refused_for_a_different_reason(
        monkeypatch):
    """**AC3**: 判定できないものは「判定できない」と分かる形で止めること。

    推測で通すと二重登録ガードが黙って効かなくなる (url 欄が壊れた記録 17 件が
    「別リポジトリ」側に落ちて素通りする)。かつ「二重登録です」と言ってはいけない
    —— それは確定していない。名乗りと実態を合わせる。
    """
    data = _data(_entry("e-9", "--intent-stdin", 3, status="in_review"))
    code, err, saved = _run_pr_add(
        monkeypatch, data, "https://github.com/o/r/pull/3")
    assert code == 2, f"判定できないまま通した (exit={code})"
    assert saved is None
    assert "判定できません" in err, "判定できないことを言っていない:\n" + err
    assert "記録は既に" not in err, (
        "確定していない二重登録を断定した (名乗りと実態のズレ):\n" + err)


def test_a_cancelled_record_still_does_not_block(monkeypatch):
    """人が理由をつけて捨てた記録は止めない (既存の規則を壊していないこと)。"""
    data = _data(_entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r",
                        status="cancelled"))
    code, err, _saved = _run_pr_add(
        monkeypatch, data, "https://github.com/o/r/pull/3")
    assert code is None, f"取消済みの記録で止めた (exit={code}): {err}"


# --- 4. 突合の重複の数え方もリポジトリを見る --------------------------------

def _dups(actions):
    return [a for a in actions if a["action"] == "duplicate"]


def test_sync_does_not_report_different_repositories_as_duplicates():
    """**AC4**: 別リポジトリの同番号を重複として報告し続けないこと。

    見ないままだと、重複の組に入った記録は自動遷移も止まる (= 正常な記録が
    永久に揃わない)。
    """
    data = _data(
        _entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r"),
        _entry("e-2", "https://github.com/other/app/pull/3", 3,
               repo="other/app"),
    )
    actions = core.plan_pr_sync(
        data, [{"number": 3, "state": "OPEN",
                "url": "https://github.com/o/r/pull/3"}], fetched_floor=1)
    assert _dups(actions) == [], (
        "別リポジトリの同番号を重複として報告した: " + repr(_dups(actions)))
    assert not [a for a in actions if a["action"] == "blocked_by_duplicate"], (
        "別リポジトリの同番号で自動遷移を止めた")


def test_sync_still_reports_the_same_repositorys_duplicates():
    data = _data(
        _entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r"),
        _entry("e-2", "https://github.com/o/r/pull/3", 3, repo="o/r"),
    )
    actions = core.plan_pr_sync(
        data, [{"number": 3, "state": "MERGED",
                "url": "https://github.com/o/r/pull/3"}], fetched_floor=1)
    dups = _dups(actions)
    assert len(dups) == 1, repr(actions)
    assert sorted(dups[0]["duplicate_entry_ids"]) == ["e-1", "e-2"]
    assert "o/r" in dups[0]["reason"], dups[0]["reason"]
    assert [a for a in actions if a["action"] == "blocked_by_duplicate"], (
        "確実な重複で自動遷移を止めていない")


def test_sync_says_undeterminable_instead_of_guessing():
    """リポジトリ不明が混ざった組は「決められない」と言うこと (推測で埋めない)。"""
    data = _data(
        _entry("e-1", "https://github.com/o/r/pull/3", 3, repo="o/r"),
        _entry("e-9", "--intent-stdin", 3),
    )
    actions = core.plan_pr_sync(
        data, [{"number": 3, "state": "MERGED",
                "url": "https://github.com/o/r/pull/3"}], fetched_floor=1)
    dups = _dups(actions)
    assert len(dups) == 1, repr(actions)
    assert "判定できない" in dups[0]["reason"], dups[0]["reason"]
    assert sorted(dups[0]["duplicate_entry_ids"]) == ["e-1", "e-9"]


def test_a_lone_undeterminable_record_is_not_called_a_duplicate():
    """1 件しか無ければ重複ではない (境界: 0 件 / 1 件で誤報しない)。"""
    data = _data(_entry("e-9", "--intent-stdin", 3))
    actions = core.plan_pr_sync(
        data, [{"number": 3, "state": "OPEN",
                "url": "https://github.com/o/r/pull/3"}], fetched_floor=1)
    assert _dups(actions) == [], repr(actions)


# --- 5. 差分の収集は 3 箇所すべて 1 経路を通る ------------------------------

def test_the_diff_collection_names_the_repository():
    import commands
    gaps: list = []
    argv, target_ref, repo = commands._pr_diff_collection("3", "o/r", gaps)
    assert argv == ["gh", "pr", "diff", "3", "--repo", "o/r"], argv
    assert target_ref == "o/r#3", (
        "bundle の対象表記にリポジトリが入っていない —— judge も利用者も"
        "どのリポジトリの 3 番か知る術が無い")
    assert repo == "o/r"
    assert gaps == []


def test_an_unresolvable_repository_is_disclosed_not_guessed(monkeypatch):
    import commands

    class _Fail:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(commands.subprocess, "run",
                        lambda *a, **k: _Fail())
    gaps: list = []
    argv, target_ref, repo = commands._pr_diff_collection("3", "", gaps)
    assert repo == "", "解決できていないのにリポジトリ名を名乗った"
    assert "--repo" not in argv, argv
    assert "リポジトリ不明" in target_ref, target_ref
    assert gaps and "e-6971" in gaps[0], gaps


def test_every_pr_diff_collection_goes_through_the_one_path():
    """**3 箇所すべて** が ``_pr_diff_collection`` を通ること。

    ``gh pr diff <n>`` をリポジトリ指定なしで叩く形は 3 箇所に手で書かれていた
    (review context / review batch context / 目的達成 bundle)。1 箇所だけ直して
    「直した」と名乗るのが、この MS が掃除している型そのもの。

    **構文木から実際の呼び出しを採る。** 文字列の部分一致ではコメントや docstring に
    名前が出ているだけで素通りする (この repo で 4 回踏んでいる形)。
    """
    import commands
    tree = ast.parse(inspect.getsource(commands))

    def _calls_in(fn_name):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} が見つからない"
        out = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                out.add(getattr(node.func, "id", None)
                        or getattr(node.func, "attr", ""))
        return out

    for fn_name in ("cmd_review_context", "cmd_review_batch_context",
                    "_emit_attainment_context"):
        calls = _calls_in(fn_name)
        assert "_pr_diff_collection" in calls, (
            f"{fn_name} が共通経路を通っていない — リポジトリ指定なしの "
            f"`gh pr diff` に戻っている可能性がある。実際の呼び出し: "
            f"{sorted(calls)}")

    # そして **手書きの gh pr diff が他に残っていない** こと。上の 3 件が通って
    # いても、4 つ目を手で書いた人を捕まえられなければ同じ穴が開く。
    src = inspect.getsource(commands)
    handwritten = [ln for ln in src.splitlines()
                   if '"gh", "pr", "diff"' in ln]
    assert len(handwritten) == 1, (
        "gh pr diff の組み立てが 1 箇所に収まっていない:\n"
        + "\n".join(handwritten))


def test_an_empty_pr_diff_is_an_error_not_a_plausible_bundle(monkeypatch):
    """PR の差分が空なら落ちること (**AC3** のもう半分)。

    PR で差分が空になるのは「変更が無い」ではなく「差分を取れていない」。空の
    bundle を judge に渡すと、judge はそれを「レビューした」と答え、
    ``review done`` がそれを実施済みとして記録して関門を外す。
    """
    import commands

    class _Empty:
        returncode = 0
        stdout = "   \n"
        stderr = ""
    monkeypatch.setattr(commands.subprocess, "run", lambda *a, **k: _Empty())
    with pytest.raises(SystemExit) as ei:
        with redirect_stderr(io.StringIO()):
            commands._collect_change_diff(["gh"], "o/r#3", [],
                                          empty_is_error=True)
    assert ei.value.code == 1


def test_an_empty_diff_ref_is_still_only_a_gap(monkeypatch):
    """``--diff-ref`` の空差分は正当な答え (= 2 つの ref が本当に同じ)。

    PR 側を厳しくしたついでに、こちらまで落とすと使える操作を壊す。
    """
    import commands

    class _Empty:
        returncode = 0
        stdout = ""
        stderr = ""
    monkeypatch.setattr(commands.subprocess, "run", lambda *a, **k: _Empty())
    gaps: list = []
    assert commands._collect_change_diff(["git"], "a...b", gaps) == ""
    assert gaps and "差分が空" in gaps[0]


# --- 6. 関門を外す記録が「どのリポジトリの差分か」を持つ --------------------

def test_the_review_ledger_records_which_repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    assert cs.record_review_done_across_worktrees(
        "ax", "797", "o/r") == cs.RECORDED_SHARED
    recs = cs.review_done_provenance("797")
    assert len(recs) == 1, recs
    assert recs[0]["repo"] == "o/r", recs[0]


def test_a_different_repositorys_review_does_not_lift_this_gate(
        tmp_path, monkeypatch):
    """別リポジトリの PR#N の実施記録で、この PR#N の関門を外さないこと。

    これが無いと、2 リポジトリを扱うプロジェクトで片方のレビューがもう片方の
    関門を外す (= レビューせずに merge できる)。
    """
    monkeypatch.chdir(tmp_path)
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    (triggers / "ax-review-due-3.json").write_text(
        json.dumps({"review": "ax"}), encoding="utf-8")
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    cs.record_review_done_across_worktrees("ax", "3", "other/app")

    with redirect_stderr(io.StringIO()):
        pending_other = cs._pending_review_types_for_pr("3", "o/r")
        pending_same = cs._pending_review_types_for_pr("3", "other/app")
    assert pending_other == ["ax"], (
        "別リポジトリの実施記録で関門が外れた: " + repr(pending_other))
    assert pending_same == [], (
        "同じリポジトリの実施記録で関門が外れない: " + repr(pending_same))


def test_a_legacy_record_without_a_repository_still_lifts_the_gate(
        tmp_path, monkeypatch):
    """repo を持たない古い記録は従来どおり差し引くこと。

    黙って厳しくすると、実際に実施したレビューが未実施に戻り、しかも理由が
    利用者から見えない。
    """
    monkeypatch.chdir(tmp_path)
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    (triggers / "ax-review-due-3.json").write_text(
        json.dumps({"review": "ax"}), encoding="utf-8")
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    ledger = tmp_path / "ledger"
    monkeypatch.setattr(cs, "_review_done_dir", lambda: (str(ledger), True))
    ledger.mkdir()
    # repo フィールドが無い旧形式の記録を手で置く
    (ledger / "pr-3-ax.json").write_text(
        json.dumps({"pr": "3", "review_type": "ax",
                    "recorded_by": "someone"}), encoding="utf-8")
    with redirect_stderr(io.StringIO()):
        assert cs._pending_review_types_for_pr("3", "o/r") == []


def test_the_subtraction_disclosure_names_the_repository(tmp_path, monkeypatch):
    """差し引きの開示に「どのリポジトリの差分か」が出ること。

    関門を外した記録がどの差分のものか辿れない状態を残さない (**AC5**)。
    """
    monkeypatch.chdir(tmp_path)
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    (triggers / "ax-review-due-3.json").write_text(
        json.dumps({"review": "ax"}), encoding="utf-8")
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    cs.record_review_done_across_worktrees("ax", "3", "o/r")
    err = io.StringIO()
    with redirect_stderr(err):
        cs._pending_review_types_for_pr("3", "o/r")
    assert "o/r" in err.getvalue(), (
        "どのリポジトリの差分に対する実施で関門を外したか言っていない:\n"
        + err.getvalue())


def test_the_gate_check_is_given_the_records_own_repository():
    """``pr approve`` / ``pr merge`` の関門が、その記録のリポジトリで判定すること。

    関門に repo を渡せるようにしても、呼び出し側が渡さなければ何も変わらない
    (= 配線はあるが動かない、この MS の主題そのもの)。構文木で実引数を見る。
    """
    tree = ast.parse(inspect.getsource(cmd_pr))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and (getattr(n.func, "id", None)
                  or getattr(n.func, "attr", "")) == "_review_gate_check"]
    assert len(calls) == 2, f"関門の呼び出しが 2 箇所でない: {len(calls)}"
    for call in calls:
        kw = {k.arg for k in call.keywords}
        assert "pr_repo" in kw, (
            "関門にリポジトリを渡していない呼び出しがある (配線はあるが効かない): "
            f"keywords={sorted(kw)}")


# --- 7. 入口の検査: 無視される指定を受け取らない ----------------------------

def test_repo_without_pr_is_refused_not_silently_ignored(monkeypatch):
    """``--repo`` を ``--pr`` 無しで受け取って黙って無視しないこと。

    黙って無視すると、利用者は「リポジトリを指定した」と信じたまま別の対象の
    bundle を受け取る —— この課題が直した欠陥と **同じ形** を入口で作ることになる。
    """
    import commands
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    monkeypatch.delenv("BEACON_PR", raising=False)
    monkeypatch.setenv("BEACON_DIFF_REF", "origin/main...HEAD")
    monkeypatch.delenv("BEACON_TARGET_ID", raising=False)
    monkeypatch.delenv("BEACON_ORIGIN_DOC", raising=False)
    monkeypatch.delenv("BEACON_MODE", raising=False)
    err = io.StringIO()
    with pytest.raises(SystemExit) as ei:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            commands.cmd_review_context()
    assert ei.value.code == 1
    assert "--repo は --pr と一緒に" in err.getvalue(), err.getvalue()


@pytest.mark.parametrize("bad", ["owner", "a/b/c", "https://github.com/o/r"])
def test_a_malformed_repo_value_is_refused_at_the_entrance(bad):
    """形が違う値を gh に素通しさせないこと。

    素通しすると gh 側の見慣れないエラーになり、利用者が **自分の入力を疑えない**
    (memory feedback_never_reuse_truncated_sid と同じ教訓: CLI の refuse はまず
    自分の入力を疑わせる形であるべき)。
    """
    import commands
    err = io.StringIO()
    with pytest.raises(SystemExit) as ei:
        with redirect_stderr(err):
            commands._resolve_review_pr_repo(bad, [])
    assert ei.value.code == 1
    assert "owner/name" in err.getvalue(), err.getvalue()
