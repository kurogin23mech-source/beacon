"""code_graph_store.py — コード理解グラフの格納先ポインタ (ms-156 e-5539 / e-5544 review)

このプロジェクト固有の **格納場所** (生きた Beacon table-doc の doc id) の単一の真値源。
schema (lib/code_graph) は他プロジェクトでも使える純粋な型なので doc id を持たせず、
「この beacon project のグラフがどの doc に載っているか」だけをここに 1 箇所で固定する。

複数スクリプト (seed / drift / navigate / curate / migration-coverage) が別々に doc id を
ハードコードしていた保守性 finding (PR #675 独立レビュー maintainability-1) を解消する:
doc を作り直したら **ここ 1 箇所**を直せば全スクリプトが追随する。
"""

from __future__ import annotations

# e-5539 の --create で生きた Beacon project に作られた nodes / edges table-doc。
NODES_DOC_ID = "CaBxTvnd9RlOBLKwsVzS"
EDGES_DOC_ID = "ZMs2c7eXdBqHySRpV7qr"

# 移行台帳 (cluster = 継ぎ目の真値、e-5544 の顧客結合照合が読む)。
LEDGER_DOC_ID = "paradigm-migration-ledger"

# table-doc のタイトル (create / update で使う)。
NODES_TITLE = "コード理解グラフ: nodes (module)"
EDGES_TITLE = "コード理解グラフ: edges (adjacency)"
