"""code_graph_reconcile.py — module 編集時に reconcile を促す判定 (ms-156 e-5542)

SPEC 方針4: curated 層 (契約 / 意図) は編集の瞬間に促さないと即腐る。module を
Edit/Write したら、そのノードの契約 / 継ぎ目を実体と突き合わせて直す (reconcile) よう
促す。commit→beacon-log が「その時刻の hook」で効くのと同じ forcing function。

最小スライスでは hook は **「促す」まで** (強制ブロックは後続)。この module は判定を
純粋関数で持つ (テスト可能): 編集された path が code-graph の対象 module なら促し文を
返し、そうでなければ None。実際の hook I/O は ``scripts/beacon-code-graph-edit-hook.py``。

真値源に cloud を叩かない: 対象 module 集合は **ソース** (lib/*.py・server/*.py・
channel/*.mjs) から決める (``code_graph_derive.enumerate_source_modules``)。編集の
たびに cloud を読むと遅く脆いので、機械照合と同じ source-first を守る。
"""

from __future__ import annotations

import os

import code_graph_derive


def _rel_module_path(file_path: str, repo: str) -> str | None:
    """絶対 / 相対の編集 path を repo 相対の module path に正規化する。

    repo 外や repo 相対に落とせない path は None。
    """
    if not file_path:
        return None
    abspath = os.path.abspath(file_path)
    absrepo = os.path.abspath(repo)
    if abspath == absrepo or not abspath.startswith(absrepo + os.sep):
        return None
    return os.path.relpath(abspath, absrepo).replace(os.sep, "/")


def reminder_for_edit(file_path: str, repo: str) -> str | None:
    """編集された file が code-graph の対象 module なら promptを返す。それ以外は None。

    対象 = ソースに実在する module (lib/*.py・server/*.py・channel/*.mjs)。テスト
    ファイルや docs 等は対象外 (None) なので、hook は module 編集時だけ喋る。
    """
    rel = _rel_module_path(file_path, repo)
    if rel is None:
        return None
    if rel not in code_graph_derive.enumerate_source_modules(repo):
        return None
    return (
        f"BEACON: {rel} を編集しました。コード理解グラフ (code-graph) の "
        f"契約 / 継ぎ目がこの module について古くなったかもしれません。"
        f"契約 / 意図を直すには `python3 scripts/graph-curate.py --module {rel} "
        f"--contract \"…\"`、構造 (依存 / 新旧 module) のズレ確認は "
        f"`python3 scripts/check-graph-drift.py`。"
        f"(最小版 = 促すのみ、強制はしません)"
    )
