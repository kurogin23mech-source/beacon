"""fork の片付けを「消してよいか」の判定だけに切り出した層 (ms-166 e-6793)。

なぜ分けたか
------------
``cmd_session_fork_cleanup`` は 1 関数 (約 270 行) の中で、引数読み取り / fork 一覧の
取得 / 4 段のゲート判定 (各判定の内部で直接 ``subprocess.run(["git", ...])`` を呼ぶ) /
メモ退避の実ファイル IO / worktree と branch の削除 / JSON と人間向けの 2 系統の出力整形
を一手に行っていた。

その結果、``tests/test_fork_cleanup_preserves_notes_e6702.py`` の全ケースが「tmp_path に
実 git リポジトリを init し、worktree を add し、fork.json / session.json /
session_notes.jsonl を書き、CLI を subprocess で起動する」フルセットアップを経由する。
**ゲートの真偽表だけを検証する軽量な単体テストが 1 つも無かった** (PR #770 の独立保守性
レビューが指摘)。

実害は「未取り込みのときのメッセージ文言を 1 語直したい」程度の変更でも、実 git 操作を
伴う統合テスト以外に検証手段が無いこと。5 つ目のゲートを足す / 順序を変えるたびに、
そのテストが本当にそのゲートだけを見ているかを git 呼び出し込みで読み解く必要があり、
フィードバックが遅い。

どう分けたか
------------
* ``GitFacts`` — git に **何を訊いたか** ではなく **何が分かったか** を持つ。
  returncode を運ばないのが要点: 判定側が ``--is-ancestor`` の終了コードの意味
  (1 = 未取り込み / 128 = 比較不能) を知っている必要がなくなり、真偽表で試験できる。
* ``evaluate`` — 判定だけ。副作用は注入された ``backup`` の口からしか起こせない。
* ``collect_git_facts`` — git を実際に叩く側。ここだけが subprocess を知る。

退避 (gate 3) は「判定しつつ実行する」ので、純粋さは **注入** で担保した。``backup`` を
差し替えれば、退避の成功/失敗の両方を実 IO なしで試験できる。
"""

from __future__ import annotations

import datetime
import os
from typing import Callable, NamedTuple, Optional

# git に訊いた結果の 3 値。``"unknown"`` は「確認できなかった」で、
# **「問題なし」ではない** — このモジュールの判定はすべて「不明は拒否」に倒す。
MERGED = "merged"
NOT_MERGED = "not-merged"
MERGE_UNKNOWN = "unknown"

CLEAN = "clean"
DIRTY = "dirty"
DIRTY_UNKNOWN = "unknown"


class GitFacts(NamedTuple):
    """判定が要る git の事実。**終了コードは運ばない。**

    ``origin_main_found`` が False のとき ``merge_state`` は意味を持たない
    (比較対象が無いので訊いていない)。
    """
    origin_main_found: bool
    merge_state: str                 # MERGED / NOT_MERGED / MERGE_UNKNOWN
    merge_error: str                 # merge_state == MERGE_UNKNOWN のときの理由
    dirty_state: str                 # CLEAN / DIRTY / DIRTY_UNKNOWN
    dirty_files: tuple               # dirty_state == DIRTY のときの対象
    dirty_error: str                 # dirty_state == DIRTY_UNKNOWN のときの理由


class BackupResult(NamedTuple):
    path: str
    error: str


def evaluate(record: dict, *, force: bool, idle_threshold: float,
             facts: GitFacts,
             backup: Optional[Callable[[dict, str, int], BackupResult]] = None,
             branch: str = None) -> dict:
    """消してよいかを判定する。**git も file system も直接触らない。**

    返り値:
      ``blockers``      — 人が承知のうえで受け入れられる risk。``--force`` が上書きできる。
      ``hard_blockers`` — 「データを保全できない」。``--force`` でも上書きさせない。
      ``refusals``      — 実際に止める理由 (= hard + (force なら空 else blockers))。
      ``backup_path``   — メモの退避先 (取れた場合)。

    2 つの list を分けているのは PR #770 の AX レビュー由来。以前は 1 つの list を
    共有していたので ``--force`` が退避失敗のゲートも通り抜け、コメント・help・Skill が
    揃って「--force でも上書きできない」と約束していたのに実装がそうなっていなかった。
    **コードが提供しない保証を文書が主張するのは、保証が無いより悪い** — 人が --force に
    手を伸ばすのは、まさに復旧が要る場面だから。
    """
    if branch is None:
        branch = record.get("child_branch") or ""
    blockers: list = []
    hard_blockers: list = []

    # --- gate 1: branch の作業が main に入っているか -------------------------
    if not branch:
        # AX レビュー PR#770: ここは以前 `if branch:` で、child_branch が空の
        # fork.json は取り込み確認を **丸ごと飛ばしていた**。この verb が防ぐはずの
        # risk が、メタデータ欠落という入口から素通りしていた。不明は拒否する
        # (gate 2 と同じ理由)。
        blockers.append(
            "この fork に紐づく branch 情報が読めません (fork.json の "
            "child_branch が空)。安全に取り込み確認ができないため削除しません")
    elif not facts.origin_main_found:
        blockers.append(
            "origin/main が見つかりません (git fetch 済みか、origin remote が "
            "あるかを確認してください)。取り込み確認ができないため削除しません")
    elif facts.merge_state == NOT_MERGED:
        blockers.append(
            f"branch '{branch}' はまだ origin/main に取り込まれていません "
            f"(取り込み前に消すとコミットが失われます)")
    elif facts.merge_state == MERGE_UNKNOWN:
        # 「未取り込み」と「比較できない」を同じ文言で報せると、人は決して成立しない
        # 「取り込まれるのを待つ」ループに入る (AX レビュー PR#770)。
        blockers.append(
            f"branch '{branch}' の取り込み確認に失敗しました "
            f"({facts.merge_error or 'git error'})。確認できないため削除しません")

    # --- gate 1b: worktree 内の未コミットの作業 ------------------------------
    # gate 1 は git に **branch** が取り込まれたかを訊くが、``--is-ancestor`` は
    # コミット済みの履歴しか見ない。コミットされていない作業はそこから不可視。
    # 以前は削除の段で最悪の形で発覚していた: 素の ``git worktree remove`` が
    # exit 128 ("contains modified or untracked files, use --force") で落ち、
    # コードが **あらゆる失敗に対して** --force で再試行していたので、*未取り込み*
    # を理由に --force を渡した人が、気づかないまま未コミットの作業も消していた。
    # git 自身のエラー文がその破壊的な再試行を勧めてくるのが、呼び出し側を誘導する。
    # 2026-10-01 に実リポジトリで再現。
    #
    # だから **ゲートとして先に見つけ、対象を名前で出す**。--force は上書きできる
    # (自分の WIP を失うのは人が承知で受け入れられる risk — 退避の失敗と違う) が、
    # それは **情報を得た上での選択** でなければならない。
    if facts.dirty_state == DIRTY_UNKNOWN:
        blockers.append(
            f"このフォークに未コミットの変更が残っているかを確認できませんでした "
            f"({facts.dirty_error or 'git error'})。確認できないため削除しません")
    elif facts.dirty_state == DIRTY:
        names = list(facts.dirty_files)
        shown = "、".join(names[:5]) + ("ほか" if len(names) > 5 else "")
        blockers.append(
            f"このフォークに未コミットの変更が {len(names)} 件あります ({shown})。"
            f"削除すると失われます (git の履歴に入っていないので取り込み確認では"
            f"検出できません)")

    # --- gate 2: まだ誰か作業しているか -------------------------------------
    idle = record.get("idle_seconds")
    if idle is None:
        blockers.append(
            "このフォークで作業中のセッションが居るかを判定できません "
            "(.beacon/session.json の活動記録が読めません)。"
            "『判定できない』は『空いている』ではありません")
    elif idle < idle_threshold:
        blockers.append(
            f"{idle/60:.0f} 分前まで作業されています "
            f"(セッション {record.get('own_session_id') or '(不明)'}、"
            f"作業中とみなす閾値 {idle_threshold/60:.0f} 分)。"
            f"作業中のフォークを消すと、そのセッションの足元が外れます")

    # --- gate 3: 未昇格のメモは削除の **前** に退避する ----------------------
    # ``note clear`` と同じ順序保証 (ms-178 e-6656): 退避が無ければ削除しない。
    # 退避先は消える worktree の **外** (親リポジトリ側) — 消すディレクトリの中に
    # 置く退避は退避ではない。これは hard_blockers に入る: データを保全することは
    # 人が受け入れる risk ではないので、--force は届かない。
    n_notes = record.get("unpromoted_notes")
    backup_path = ""
    if n_notes is None:
        # 保守性レビュー PR#770: 件数が「ファイル無し」と「読めない」を 0 に潰して
        # いたので、一過性の IO エラーで退避の段が丸ごと飛ばされ、メモが消えていた。
        # 読めないことは空であることではない。
        hard_blockers.append(
            f"引き継ぎメモの件数を読めませんでした ({record.get('notes_path')})。"
            f"0 件と確定できないため削除しません")
    elif n_notes:
        res = (backup or _no_backup)(record, branch, n_notes)
        if res.error:
            hard_blockers.append(
                f"引き継ぎメモ {n_notes} 件の退避に失敗しました ({res.error})。"
                f"退避が取れないので削除しません (--force でも上書きできません)")
        else:
            backup_path = res.path

    return {
        "blockers": blockers,
        "hard_blockers": hard_blockers,
        "refusals": hard_blockers + ([] if force else blockers),
        "backup_path": backup_path,
    }


def _no_backup(record, branch, n_notes) -> BackupResult:
    """``backup`` が渡されなかった場合。**退避できていないので失敗として扱う。**

    「口が渡されていない」を「退避不要」に倒すと、呼び出し側の配線漏れが
    「メモごと削除してよい」に化ける。不明は拒否、の原則をここにも当てる。
    """
    return BackupResult("", "退避の口が配線されていません (呼び出し側の不具合)")


# ---------------------------------------------------------------------------
# ここから下だけが副作用を知っている (git / file system)
# ---------------------------------------------------------------------------

def collect_git_facts(repo_root: str, worktree_path: str, branch: str,
                      runner=None) -> GitFacts:
    """判定に要る git の事実を集める。``runner`` は試験で差し替えられる。

    終了コードの意味をここで **事実に翻訳する** のが役割: ``--is-ancestor`` は
    1 を「祖先でない」、128 を「比較できない」(origin remote が無い / fetch して
    いない) に使う。この区別を判定側に持ち込むと、判定が git の約束に結合する。
    """
    import subprocess
    run = runner or (lambda argv, cwd: subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True))

    if not branch:
        # branch が無ければ訊く相手がいない。判定側が gate 1 で拒否する。
        return GitFacts(False, MERGE_UNKNOWN, "", DIRTY_UNKNOWN, (), "")

    ref = run(["git", "rev-parse", "--verify", "--quiet", "origin/main"], repo_root)
    origin_found = ref.returncode == 0
    merge_state, merge_error = MERGE_UNKNOWN, ""
    if origin_found:
        m = run(["git", "merge-base", "--is-ancestor", branch, "origin/main"],
                repo_root)
        if m.returncode == 0:
            merge_state = MERGED
        elif m.returncode == 1:
            merge_state = NOT_MERGED
        else:
            merge_error = (m.stderr or "").strip() or f"git exit {m.returncode}"

    d = run(["git", "status", "--porcelain"], worktree_path)
    if d.returncode != 0:
        dirty_state, files, dirty_error = (
            DIRTY_UNKNOWN, (), (d.stderr or "").strip() or f"git exit {d.returncode}")
    else:
        files = tuple(ln[3:].strip() for ln in d.stdout.splitlines()
                      if ln[3:].strip())
        dirty_state = DIRTY if files else CLEAN
        dirty_error = ""
    return GitFacts(origin_found, merge_state, merge_error,
                    dirty_state, files, dirty_error)


def make_notes_backup(repo_root: str):
    """メモ退避の実装 (親リポジトリ側に置く)。``evaluate`` に注入する口。"""
    def _backup(record, branch, n_notes) -> BackupResult:
        import shutil
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_branch = (branch or "fork").replace("/", "-")
        backup_dir = os.path.join(repo_root, ".beacon", "fork-notes-backup")
        path = os.path.join(backup_dir, f"{safe_branch}-{stamp}.jsonl")
        try:
            os.makedirs(backup_dir, exist_ok=True)
            shutil.copyfile(record["notes_path"], path)
        except OSError as exc:
            return BackupResult("", str(exc))
        return BackupResult(path, "")
    return _backup
