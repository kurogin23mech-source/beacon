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
    assert recs[0]["pr_repo"] == "o/r", recs[0]


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


# --- 8. 関門を外す **主経路** もリポジトリで絞る (独立 AX レビュー high) ----------
#
# 上の §6 は横断台帳の差し引きを測っていたが、**それは関門の第 2 経路**だった。
# 関門の主経路は cwd の review-due トリガーファイルで、`cmd_review_done` はそれを
# 番号だけで消していた。つまり台帳側に足した repo 判定は、その手前で一覧が空になる
# ため **一度も評価されない**。独立 AX レビューがサンドボックスで 3 段
# (pr add → 誤 repo で review done → pr approve) を実際に走らせ、別リポジトリの
# PR#3 の承認が両方 exit 0 で通ることを実測した。
#
# §6 の試験がこれを見逃したのは、`cmd_review_done` を呼ばず
# `record_review_done_across_worktrees` を直叩きしてトリガーを残したから。
# **helper を測って live な経路を測らない**形で、同じファイルの別の試験で自分が
# 構造チェックしていた型をここで踏んでいた。だからここは **実コマンドを走らせる**。

def test_the_trigger_filename_carries_the_repository():
    assert cs.review_due_filename("ax", "3", "o/r") == "ax-review-due-3@o~r.json"
    # リポジトリ不明は従来と同じ形 = 既存トリガーを孤児にしない
    assert cs.review_due_filename("ax", "3") == "ax-review-due-3.json"


@pytest.mark.parametrize("name,expected", [
    ("ax-review-due-3@o~r.json", ("ax", "3", "o/r")),
    ("maintainability-review-due-798@kurogin23mech-source~beacon.json",
     ("maintainability", "798", "kurogin23mech-source/beacon")),
    ("ax-review-due-3.json", ("ax", "3", "")),          # 旧式 = 不明
    ("review-due-ms-166.json", None),                   # target 用は別物
    ("spec-needed-ms-9.json", None),
    ("not-json", None),
])
def test_the_trigger_filename_parses_back(name, expected):
    assert cs.parse_review_due_filename(name) == expected, name


def _fire(tmp_path, monkeypatch, review_type, pr, repo=""):
    cs._fire_review_due_for_pr(review_type, review_type, pr, "t",
                               f"https://github.com/{repo or 'o/r'}/pull/{pr}",
                               pr_repo=repo)


def test_clearing_one_repos_trigger_leaves_the_other_repos_alone(
        tmp_path, monkeypatch):
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    _fire(tmp_path, monkeypatch, "ax", "3", "o/r")
    _fire(tmp_path, monkeypatch, "ax", "3", "other/app")
    with redirect_stderr(io.StringIO()):
        cs._clear_review_due_for_pr("ax", "3", pr_repo="o/r")
    left = sorted(p.name for p in triggers.iterdir())
    assert left == ["ax-review-due-3@other~app.json"], left


def test_clearing_without_a_repository_leaves_repo_qualified_triggers(
        tmp_path, monkeypatch):
    """呼び出し側が repo を知らないときは、関門を **残す** 側に倒すこと。"""
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    _fire(tmp_path, monkeypatch, "ax", "3", "o/r")
    with redirect_stderr(io.StringIO()):
        cs._clear_review_due_for_pr("ax", "3")
    assert [p.name for p in triggers.iterdir()] == ["ax-review-due-3@o~r.json"]


def test_clearing_a_legacy_trigger_is_disclosed(tmp_path, monkeypatch):
    """旧式 (リポジトリ無し) を消すときは、区別できないことを開示すること。

    黙って消すと、どのリポジトリのものか分からない関門が外れたことに気づけない。
    """
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    _fire(tmp_path, monkeypatch, "ax", "3")          # 旧式
    err = io.StringIO()
    with redirect_stderr(err):
        cs._clear_review_due_for_pr("ax", "3", pr_repo="o/r")
    assert list(triggers.iterdir()) == []
    assert "旧式" in err.getvalue() and "区別でき" in err.getvalue(), err.getvalue()


def test_pending_ignores_another_known_repositorys_trigger(
        tmp_path, monkeypatch):
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    _fire(tmp_path, monkeypatch, "ax", "3", "other/app")
    with redirect_stderr(io.StringIO()):
        assert cs._pending_review_types_for_pr("3", "o/r") == []
        assert cs._pending_review_types_for_pr("3", "other/app") == ["ax"]
        # repo を知らない呼び出しは絞り込めないので残す (関門を残す側)
        assert cs._pending_review_types_for_pr("3") == ["ax"]


def test_a_wrong_repo_review_done_does_not_lift_any_other_repos_gate(
        tmp_path, monkeypatch):
    """**独立 AX レビュー high の再現**: 誤った --repo で関門が外れないこと。

    実コマンド (`cmd_review_done`) を走らせて測る。helper を直叩きすると、
    コマンドが無条件にトリガーを消す副作用を踏まないので緑のままになる
    —— それが見逃した原因そのもの。
    """
    import commands
    triggers = tmp_path / "triggers"
    ledger = tmp_path / "ledger"
    for mod in (cs, commands):
        monkeypatch.setattr(mod, "_get_triggers_dir", lambda: str(triggers),
                            raising=False)
    monkeypatch.setattr(cs, "_review_done_dir", lambda: (str(ledger), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    _fire(tmp_path, monkeypatch, "ax", "3", "o/r")
    _fire(tmp_path, monkeypatch, "ax", "3", "other/app")

    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "3")
    monkeypatch.setenv("BEACON_PR_REPO", "yet-another/repo")
    for k in ("BEACON_REVIEW_ADJUDICATION_SUMMARY", "BEACON_REVIEW_ADJUDICATIONS"):
        monkeypatch.delenv(k, raising=False)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        commands.cmd_review_done()

    with redirect_stderr(io.StringIO()):
        assert cs._pending_review_types_for_pr("3", "o/r") == ["ax"], (
            "無関係なリポジトリの review done で o/r の関門が外れた")
        assert cs._pending_review_types_for_pr("3", "other/app") == ["ax"], (
            "無関係なリポジトリの review done で other/app の関門が外れた")


def test_the_right_repos_review_done_does_lift_its_own_gate(
        tmp_path, monkeypatch):
    """緩めた側だけでなく **効く側** も測る (でないと丸ごと壊しても緑)。"""
    import commands
    triggers = tmp_path / "triggers"
    ledger = tmp_path / "ledger"
    for mod in (cs, commands):
        monkeypatch.setattr(mod, "_get_triggers_dir", lambda: str(triggers),
                            raising=False)
    monkeypatch.setattr(cs, "_review_done_dir", lambda: (str(ledger), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    _fire(tmp_path, monkeypatch, "ax", "3", "o/r")
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", "3")
    monkeypatch.setenv("BEACON_PR_REPO", "o/r")
    for k in ("BEACON_REVIEW_ADJUDICATION_SUMMARY", "BEACON_REVIEW_ADJUDICATIONS"):
        monkeypatch.delenv(k, raising=False)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        commands.cmd_review_done()
    with redirect_stderr(io.StringIO()):
        assert cs._pending_review_types_for_pr("3", "o/r") == []


def test_every_review_due_caller_passes_the_repository():
    """関門に関わる 4 つの口すべてが repo を渡していること。

    1 つでも渡し忘れると、その経路から関門がリポジトリ非依存で外れる
    (= 独立 AX レビューが実測した穴)。構文木で実引数を見る。
    """
    import ast as _ast
    import importlib
    watched = {"_clear_review_due_for_pr", "_fire_review_due_for_pr",
               "_fire_pr_open_review_triggers", "_clear_pr_open_review_triggers"}
    offenders = []
    for modname in ("commands", "commands_shared", "cmd_pr", "cmd_trigger"):
        mod = importlib.import_module(modname)
        tree = _ast.parse(inspect.getsource(mod))
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            name = (getattr(node.func, "id", None)
                    or getattr(node.func, "attr", ""))
            if name not in watched:
                continue
            kw = {k.arg for k in node.keywords}
            # _fire_review_due_for_pr は位置引数 6 個目でも渡せるが、この repo では
            # どの呼び出しも keyword で渡す規約にしている (読み手が取り違えない)。
            if "pr_repo" not in kw:
                offenders.append(f"{modname}:{node.lineno} {name}")
    assert offenders == [], (
        "関門に関わる呼び出しがリポジトリを渡していない (配線はあるが効かない): "
        + repr(offenders))


def test_the_review_due_filename_is_built_and_parsed_in_one_place_each():
    """名前の組み立て / 解析が 1 箇所に収まっていること。

    以前この名前は 5 箇所で手組みされていた (fire / clear / pending / tick anchor /
    skip の義務確認)。1 箇所だけ repo 対応にしても、残り 4 箇所で同じ取り違えが
    起き続ける —— 実際そうなっていた。

    **構文木で数える。** 行の部分一致で `{pr_number}` を探す版を一度書いたが、
    変数名が `{num}` の手組み (tick anchor に実在した形) を素通りさせる。緩い一致で
    偽の緑を作るのはこの repo で何度も踏んでいる型なので、f-string の中に
    リテラル "-review-due-" を持つ式を構文木から拾って数える。
    """
    import ast as _ast
    import importlib
    inline, via_const = [], []
    for modname in ("commands", "commands_shared", "cmd_pr", "cmd_trigger"):
        mod = importlib.import_module(modname)
        tree = _ast.parse(inspect.getsource(mod))
        # 正準の 2 関数 (組み立て / 解析) の行範囲。ここでの使用だけが許される。
        canonical = [(n.lineno, getattr(n, "end_lineno", n.lineno))
                     for n in _ast.walk(tree)
                     if isinstance(n, _ast.FunctionDef)
                     and n.name in ("review_due_filename",
                                    "parse_review_due_filename")]

        def _ok(lineno):
            return any(a <= lineno <= b for a, b in canonical)

        for node in _ast.walk(tree):
            # (a) f-string の中に "-review-due-" のリテラルを持つ = 直接の手組み
            if isinstance(node, _ast.JoinedStr) and any(
                    isinstance(v, _ast.Constant) and isinstance(v.value, str)
                    and "-review-due-" in v.value for v in node.values):
                if not _ok(node.lineno):
                    inline.append(f"{modname}:{node.lineno}")
            # (b) 接尾辞の定数を使って名前を組む / 突き合わせる形。**これを見ないと
            #     緩い**: tick anchor の `f"{_REVIEW_DUE_SUFFIX}{num}.json"` は
            #     リテラルが定数の中に在るので (a) では捕まらず、反転 U25 が緑に
            #     なった (実測)。定数の参照そのものを数える。
            if (isinstance(node, _ast.Name)
                    and node.id == "_REVIEW_DUE_SUFFIX"
                    and isinstance(node.ctx, _ast.Load)
                    and not _ok(node.lineno)):
                via_const.append(f"{modname}:{node.lineno}")
    assert inline == [] and via_const == [], (
        "PR のレビュー未実施トリガー名を正準の 2 関数の外で組み立て / 突き合わせて"
        " いる箇所がある (1 箇所だけ直しても残りで同じ取り違えが起きる): "
        f"inline={inline!r} / via_const={via_const!r}")


def test_the_subtraction_headline_does_not_blame_only_worktrees(
        tmp_path, monkeypatch):
    """リポジトリ未記録の記録で差し引いたときは、見出しでそう言うこと。

    独立 AX レビュー medium: 見出しが常に「別の作業フォルダで実施済み」と名乗ると、
    読み手は「同じコードベースの別チェックアウトなら安全」と読み、repo 行の値を
    worktree の識別子だと誤読しうる。
    """
    triggers = tmp_path / "triggers"
    triggers.mkdir()
    (triggers / "ax-review-due-3@o~r.json").write_text(
        json.dumps({"review": "ax"}), encoding="utf-8")
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    monkeypatch.setattr(cs, "_review_done_dir", lambda: (str(ledger), True))
    # repo を持たない旧式の台帳記録で差し引かれる状態
    (ledger / "pr-3-ax.json").write_text(
        json.dumps({"pr": "3", "review_type": "ax", "recorded_by": "x"}),
        encoding="utf-8")
    err = io.StringIO()
    with redirect_stderr(err):
        assert cs._pending_review_types_for_pr("3", "o/r") == []
    out = err.getvalue()
    # **見出しの行だけを見る。** 出力全体への部分一致で測った版は、種別ごとの
    # 明細行が "(リポジトリ未記録)" を必ず出すので、見出しを固定文に戻しても緑に
    # なった (反転 U24 で実測)。測りたいのは見出しが原因を言い切っていないこと。
    headline = out.strip().splitlines()[0]
    assert "リポジトリ未記録" in headline, (
        "リポジトリ未記録の記録で差し引いたのに、見出しが作業フォルダだけを"
        "原因として名乗っている:\n" + headline)


def _run_review_done(tmp_path, monkeypatch, *, pr, repo, triggers_seed):
    """`cmd_review_done` を隔離して走らせ、``(stdout, stderr)`` を返す。"""
    import commands
    triggers = tmp_path / "triggers"
    triggers.mkdir(exist_ok=True)
    for rtype, trepo in triggers_seed:
        (triggers / cs.review_due_filename(rtype, pr, trepo)).write_text(
            json.dumps({"review": rtype, "pr_repo": trepo}), encoding="utf-8")
    for mod in (cs, commands):
        monkeypatch.setattr(mod, "_get_triggers_dir", lambda: str(triggers),
                            raising=False)
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    monkeypatch.setattr(commands, "_record_review_adjudication_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(commands, "_ci_flip_review_gate_success",
                        lambda *a, **k: None)
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv("BEACON_PR_NUMBER", pr)
    monkeypatch.setenv("BEACON_PR_REPO", repo)
    for k in ("BEACON_REVIEW_ADJUDICATION_SUMMARY", "BEACON_REVIEW_ADJUDICATIONS"):
        monkeypatch.delenv(k, raising=False)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        commands.cmd_review_done()
    return out.getvalue(), err.getvalue()


def test_review_done_does_not_claim_gate_passage_when_nothing_was_owed(
        tmp_path, monkeypatch):
    """義務が元から無いリポジトリに対して「gate を通過できます」と言わないこと。

    実機で踏んだ: 誤った ``--repo`` を渡すと、その番号の PR 記録 2 件は依然
    block されているのに「PR #3 の独立レビューは全て実施済み — gate を通過
    できます」と出た。番号だけで名乗る文面が、リポジトリで絞った判定に
    追いついていなかった (名乗りと実態のズレ = この MS が掃除している型)。
    """
    out, err = _run_review_done(
        tmp_path, monkeypatch, pr="3", repo="yet-another/repo",
        triggers_seed=[("ax", "o/r"), ("ax", "other/app")])
    combined = out + err
    assert "全て実施済み" not in combined, (
        "義務が無いのに全件実施済みと名乗った:\n" + combined)
    assert "gate を通過できます" not in combined, combined
    assert "元から在りませんでした" in combined, combined
    assert "--repo" in combined, "原因として最も疑わしい指定に触れていない"


def test_review_done_does_claim_gate_passage_when_it_really_cleared_the_last_one(
        tmp_path, monkeypatch):
    """効く側も測る (でないと報告を丸ごと消しても緑になる)。"""
    out, err = _run_review_done(
        tmp_path, monkeypatch, pr="3", repo="o/r",
        triggers_seed=[("ax", "o/r")])
    combined = out + err
    assert "o/r#3 の独立レビューは全て実施済み" in combined, combined
    assert "gate を通過できます" in combined, combined


# --- 9. 不変条件は「主張」でなく「コード」で閉じる (独立保守性レビュー high 2 件) ---
#
# 初版は review_due_filename の docstring で「`@` と `~` は repo slug に使えない
# 文字なので境目を取り違えない」と主張していた。だが形の検査 (core.is_repo_slug) は
# `--repo` 旗の 3 箇所にしか配線しておらず、**meta.pr_repo を埋める url 経路
# (= 通常の `beacon pr add <url>` 全件) には無かった**。judge が本番の入口を走らせて
# 実測: `https://github.com/o~r/x/pull/5` を登録すると、その PR 自身の関門が空になる。
#
# 同じ形で PR 番号も検証していなかった。番号に `-review-due-` を含む値が入ると、
# 名前が **無関係な PR の関門** に誤帰属し、しかも元の番号では消せない。

@pytest.mark.parametrize("url", [
    "https://github.com/o~r/x/pull/5",     # 区切り文字 ~ を含む
    "https://github.com/o@r/x/pull/5",     # 区切り文字 @ を含む
    "https://github.com/o r/x/pull/5",     # 空白
])
def test_a_repo_that_would_break_the_encoding_is_not_accepted(url):
    """名前の区切りと衝突する repo は、値が入る場所で落とすこと。

    ここで落とさないと、書いた名前と読み戻した repo が一致しなくなる
    (= 発火したのに自分の関門から見えない)。
    """
    assert core.extract_pr_repo_from_url(url) == "", url


def test_a_hand_written_record_with_a_bad_repo_reads_as_unknown():
    """手で書かれた ``meta.pr_repo`` も読み戻しで検査すること。

    url 経路を閉じても、記録を直接書く経路 (手編集 / 別ツール) が残る。
    「不明」の扱いは全経路で既に閉じているので、不明に倒すのが安全。
    """
    assert core.recorded_pr_repo({"meta": {"pr_repo": "o~r/x"}}) == ""
    assert core.recorded_pr_repo({"meta": {"pr_repo": "o/r"}}) == "o/r"


def test_the_filename_builder_refuses_a_repo_that_breaks_the_encoding():
    """組み立て側でも閉じること (名乗りを実装で守る)。

    値が入る場所で検査していても、この関数は「どんな値でも安全な名前を返す」と
    名乗っているので、ここでも守る。旧式 (リポジトリ無し) の名前に落とす。
    """
    assert cs.review_due_filename("ax", "5", "o~r/x") == "ax-review-due-5.json"
    assert cs.review_due_filename("ax", "5", "o/r") == "ax-review-due-5@o~r.json"


def test_the_gate_sees_the_triggers_it_just_fired(tmp_path, monkeypatch):
    """**judge が実測した迂回の再現**: 発火した関門が自分から見えること。

    judge は本番の入口 (`cmd_pr_add`) を走らせ、`o~r/x` の PR#5 で
    `_pending_review_types_for_pr('5','o~r/x')` が `[]` を返すことを実測した
    (= 発火直後なのに approve/merge の関門が空)。
    """
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    url = "https://github.com/o~r/x/pull/5"
    repo = core.extract_pr_repo_from_url(url)      # → "" (不明に倒れる)
    cs._fire_review_due_for_pr("ax", "ax", "5", "t", url, pr_repo=repo)
    with redirect_stderr(io.StringIO()):
        assert cs._pending_review_types_for_pr("5", repo) == ["ax"], (
            "発火したトリガーが自分の関門から見えない")
        # 壊れた repo 値で引いても、不明として扱われるので見える (関門を残す側)
        assert cs._pending_review_types_for_pr("5", "o~r/x") == ["ax"]


def test_a_non_digit_pr_number_is_not_written_as_a_trigger(tmp_path, monkeypatch):
    """番号が数字でないトリガーは書かないこと (誤帰属の種を作らない)。"""
    triggers = tmp_path / "triggers"
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(triggers))
    err = io.StringIO()
    with redirect_stderr(err):
        cs._fire_review_due_for_pr("ax", "ax", "5-review-due-9", "t", "u",
                                   pr_repo="o/r")
    assert not triggers.exists() or list(triggers.iterdir()) == []
    assert "数字ではない" in err.getvalue(), err.getvalue()


def test_a_malformed_filename_is_rejected_not_misattributed():
    """読めない名前は None にすること。**別の PR に帰属させない**。

    judge の実測: 旧実装は最後の ``-review-due-`` で割り番号の形を見なかったので、
    ``ax-review-due-5-review-due-9@o~r.json`` を「種別 ax-review-due-5 / 番号 9」と
    読み、無関係な PR#9 の関門に誤帰属した (しかも元の番号では消せない)。
    """
    assert cs.parse_review_due_filename(
        "ax-review-due-5-review-due-9@o~r.json") is None
    # 正常な形は従来どおり読める (拒否を広げすぎていないこと)
    assert cs.parse_review_due_filename("ax-review-due-5@o~r.json") == (
        "ax", "5", "o/r")
    assert cs.parse_review_due_filename("ax-review-due-5.json") == (
        "ax", "5", "")
    # 番号が数字でない名前も拒否
    assert cs.parse_review_due_filename("ax-review-due-abc.json") is None


@pytest.mark.parametrize("verb,env", [
    ("cmd_review_done", "BEACON_PR_NUMBER"),
    ("cmd_review_skip", "BEACON_PR_NUMBER"),
])
def test_a_non_digit_pr_is_refused_at_the_user_facing_entrance(
        verb, env, monkeypatch):
    """利用者が触る入口で落とすこと (警告だけで書かずに戻る形に頼らない)。"""
    import commands
    monkeypatch.setenv("BEACON_REVIEW_TYPE", "ax")
    monkeypatch.setenv(env, "5-review-due-9")
    monkeypatch.setenv("BEACON_REASON", "r")
    monkeypatch.delenv("BEACON_PR_REPO", raising=False)
    monkeypatch.delenv("BEACON_TARGET_ID", raising=False)
    err = io.StringIO()
    with pytest.raises(SystemExit) as ei:
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            getattr(commands, verb)()
    assert ei.value.code == 1
    assert "--pr は PR 番号 (数字)" in err.getvalue(), err.getvalue()


def test_the_reviewed_marker_addresses_are_enumerated_in_one_place():
    """再発火抑止マーカーの住所の列挙が 1 箇所であること (保守性 medium)。

    同じ 2 要素集合が 2 箇所で手組みされていた。3 つ目の形を足す / 旧式を廃止する
    ときに片方だけ直すと、再発火の抑止と merge 時の後片付けが「レビュー済みか」で
    食い違う —— この MS が直している欠陥の再発そのもの。
    """
    import ast as _ast
    import importlib
    offenders = []
    for modname in ("commands", "commands_shared", "cmd_pr", "cmd_trigger"):
        mod = importlib.import_module(modname)
        tree = _ast.parse(inspect.getsource(mod))
        enumerator = [(n.lineno, getattr(n, "end_lineno", n.lineno))
                      for n in _ast.walk(tree)
                      if isinstance(n, _ast.FunctionDef)
                      and n.name == "reviewed_marker_paths"]
        calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)
                 and (getattr(n.func, "id", None)
                      or getattr(n.func, "attr", ""))
                 == "_pr_open_reviewed_marker_path"]
        for c in calls:
            # 列挙器の中 (= 唯一の正準な場所) 以外で 2 回以上組むのが問題。
            if not any(a <= c.lineno <= b for a, b in enumerator):
                offenders.append(f"{modname}:{c.lineno}")
    # 残っていてよいのは「1 つの住所だけ要る」書き込み側 1 箇所
    # (review done が自分の repo のマーカーを押す経路)。
    assert len(offenders) <= 1, (
        "マーカー住所の列挙が複数箇所で手組みされている: " + repr(offenders))


def test_the_legacy_tolerance_decision_lives_in_one_place():
    """旧式を受け続ける判断と廃止条件が 1 箇所にあること (保守性 low)。

    以前は同じ判断の理由が 4 つの関数の散文に散っており、廃止してよいかを読む人が
    「同じ判断が 4 回繰り返されている」ことに気づけなかった (= 4 箇所を手で見直さ
    ないと廃止できない)。定数が在り、廃止条件が書かれ、各所がそれを指すこと。
    """
    assert hasattr(cs, "LEGACY_REVIEW_DUE_TOLERANCE")
    src = inspect.getsource(cs)
    head = src[:src.index("LEGACY_REVIEW_DUE_TOLERANCE = (")]
    anchor = head[head.rindex("#: 旧式"):]
    assert "廃止してよい条件" in anchor, "廃止条件が書かれていない"
    # 旧式を扱う各所が、理由を再論せず定数を指していること
    for fn in ("review_due_filename", "_clear_review_due_for_pr",
               "_pending_review_types_for_pr", "_pr_open_reviewed_marker_path"):
        doc = inspect.getdoc(getattr(cs, fn)) or ""
        assert "LEGACY_REVIEW_DUE_TOLERANCE" in doc, (
            f"{fn} が旧式許容の判断を独自の散文で持っている (1 箇所に寄せる)")
