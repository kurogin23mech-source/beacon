#!/usr/bin/env python3
"""git_cwd.py — 「どのディレクトリで git を叩くか」の唯一の定義 (ms-160 e-7423)。

なぜ別モジュールか
------------------
``bin/beacon`` は ``lib/commands.py`` を呼ぶ前に、プロジェクトのルート (= ``.beacon/``
が在る親リポジトリ) へ移動する。そのため git を素で叩くと、**worktree から叩いても親の
リポジトリを読み書きする**。呼び出し元の cwd は ``BEACON_INVOCATION_CWD`` に export
されていて、``bin/lib/cmd_log.sh`` は ``git -C "$_git_cwd"`` でそれを使っている
(e-1227 / ms-17)。

最初はこの解決を ``git_read_port`` の中だけに置いた。独立保守性レビューが実機で
**姉妹ポートの ``git_write_port`` が取り残されている**ことを見つけた: ``cmd_deploy``
の ``cmd_deploy_record()`` は同じ関数の中で両方を呼ぶので、読み取ったコミット範囲は
worktree 基準なのに ``git tag`` は親の HEAD に付く。判定が自分と矛盾し、例外も警告も
出ない。片方のポートに置いた時点で「もう片方に写す」余地を作っていた。

規則は 1 箇所に置き、両ポートがここを呼ぶ。新しいポートや新しいメソッドが増えたときも
参照先が 1 つなので、写し忘れではなく **呼び忘れ** になる (= 構文木検査で機械的に
見つかる。``tests/test_git_read_port_invocation_cwd.py`` が両ポートを走査する)。

実測した呼び出し元 (2026-11-11): ``cmd_push.py`` 4 / ``cmd_pr.py`` 4 /
``cmd_deploy.py`` 3 の 11 箇所。``rollback.py`` は **意図的に使わない** (自身の
docstring に「cwd-scoped / returncode-tolerant な別契約が要る」と書いてある) ので、
ここに合わせようとしないこと。``verb_ledger_data.py`` も呼んでいない。
当初この数を「12 箇所 (push/pr/deploy/rollback/台帳)」と書いていたが、``grep -c
git_read_port.`` の素の件数を内訳を確かめずに写したもので、数も内訳も誤りだった
(独立 AX レビューが指摘)。

なぜ「ディレクトリが在る」では足りないか
----------------------------------------
最初は ``isdir`` だけを見ていた。独立 AX レビューが実機で、**存在するが git リポジトリ
でないディレクトリ**を指したときの非対称を見つけた: ``current_branch`` や
``rev_parse_short`` は例外になるのに、``config_user_name`` だけは ``git config
user.name`` がグローバル設定へカスケードするので **例外を投げず実在する値を返す**。
``cmd_push`` の ``try/except`` はそれを成功と見なし、無関係な identity を記録の
``pushed_by`` に書く。

そこで「git の作業ツリーか」まで確かめてから返す。確かめないと、6 つのうち 1 つだけが
自分の docstring (「失敗したら raise する」) を黙って破る形が残る。

検証は 1 プロセス 1 回だけ (値は実行中に変わらない)。6 メソッド × 呼び出し回数ぶん
subprocess を増やさないため。

「未設定」と「設定されているが使えない」を分ける
------------------------------------------------
AX レビューの指摘: 当初は「未設定」「存在しないパス」「git でないパス」「権限が無い」を
すべて同じ *黙って親を読む* に潰していた。呼び出し側から見て「override する気が無い」と
「override に失敗した」が区別できない。これはこの修正が直したはずの症状 (叩いた場所と
違う場所の git 事実を読む) を、別の入力から再現できる経路でもある。

いまは **設定されているのに使えなかった場合だけ** stderr に 1 行出す。未設定は正常な
経路なので黙る。例外にはしない — この口を通るのは push / deploy / pr の *記録* で、
古い環境変数のせいで記録そのものが落ちる方が害が大きい。

``cmd_log.sh`` との意図的な違い
------------------------------
``cmd_log.sh`` は ``${BEACON_INVOCATION_CWD:-$PWD}`` で、未設定・空のときだけ ``$PWD``
へ落ちる。変数が**消えた worktree を指している**場合は ``git -C <無い場所>`` がそのまま
エラーになる。こちらは落ちずにプロセスの cwd を使う。

**揃っていないこと自体が意図** (``cmd_log.sh`` は「作業ツリーを調べられないなら止める」
側に意味がある)。どちらかを直す人がもう片方へ機械的に転記しないよう、ここに書いておく。
"""

from __future__ import annotations

import os
import subprocess
import sys

#: 呼び出し元の cwd を運ぶ環境変数。``bin/beacon`` が relocate の前に export し、
#: ``beacon_cli/dispatch.py`` の ``_run_commands_py`` が子プロセスへ渡す。
ENV_VAR = "BEACON_INVOCATION_CWD"

#: 検証結果の記憶。``(環境変数の値) -> (使える cwd か None)``。値は実行中に変わらない
#: ので 1 プロセス 1 回で足りる。
_resolved: dict = {}


def _is_work_tree(path) -> bool:
    """``path`` が git の作業ツリーの中か。判定できなければ False。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # 権限が無い / git が無い / タイムアウト。使えないものとして扱う
        # (PermissionError が呼び出し側へ素で漏れるのを止める意味もある)。
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def git_cwd():
    """git を叩くディレクトリ。``None`` はプロセスの cwd を継承する意味。

    ``subprocess`` の ``cwd=`` にそのまま渡せる形で返す (``None`` が「継承」なので
    呼び出し側に分岐が要らない)。
    """
    raw = os.environ.get(ENV_VAR) or ""
    key = raw.strip()
    if not key:
        return None  # 未設定 = override する気が無い。正常な経路なので黙る
    if key in _resolved:
        return _resolved[key]

    path = os.path.expanduser(key)
    usable = None
    if os.path.isdir(path) and _is_work_tree(path):
        usable = path
    else:
        # 設定されているのに使えなかった。黙って別の場所を読むと、記録が
        # 「叩いた場所」と食い違ったまま残る。1 行だけ出す。
        print(
            "Warning: %s=%r は git の作業ツリーとして使えません。"
            "この git 操作はプロセスの作業ディレクトリで実行します。" % (ENV_VAR, key),
            file=sys.stderr,
        )
    _resolved[key] = usable
    return usable


def reset_cache() -> None:
    """検証結果の記憶を捨てる (試験用)。"""
    _resolved.clear()
