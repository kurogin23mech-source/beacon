"""code_graph_seed.py — 既存台帳からコード理解グラフの種を作る (ms-156 e-5539)

グラフの材料は既に散らばって存在する (SPEC ms-156 背景): module 監査 (150 module の
6軸分類) / 移行台帳 (cluster＝継ぎ目) / application-map (機能の入口索引・value 文脈)。
このモジュールは **その散らばりを 1 つの adjacency に束ねる純粋な変換** で、cloud に
触らない (raw text / model を受け取り ``CodeGraph`` を返す)。取得 (beacon doc show) は
``scripts/seed-code-graph.py`` が担い、ここは parse に徹する (テスト可能性の分界)。

3つの種の役割 (SPEC 方針1/3):

- **module 監査** (150 module): node の骨格。path・所属継ぎ目 (seam=axes)・統べる
  spine§ (governs) を与える。
- **application-map**: value 文脈の補完。``file:<path>`` の楔で名指しされた裏方
  module に、その散文 (何が嬉しいか) を ``role`` として与える (方針1: app-map は
  value 文脈を添える側に回す)。
- **cluster (継ぎ目)**: 同じ seam を共有する module 間に ``shares-seam`` 辺を張る
  (方針3: cluster＝shares-seam edge を台帳から導出)。

機械層 (import/call/route の ``depends-on`` / ``surfaces-as``) の自動導出は e-5540、
契約 (``implements-contract``) の人手 curate は e-5542 が所有する。ここは種だけ。
"""

from __future__ import annotations

import re

from code_graph import CodeGraph, Edge, Node

# 監査 table の行: ``| `lib/x.py` | sev | axes | verdict | note |``
_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|(.*)$")
# 末尾集約 (conforms/not-relevant) の節見出し。
_CONFORMS_HEADER_RE = re.compile(r"^##\s+.*変更不要")
# spine§ 参照 (例 §2 / §4b / §2b)。
_SECTION_RE = re.compile(r"§\s*(\d+[a-z]?)")
# application-map の楔 ``file:path``。
_FILE_WEDGE_RE = re.compile(r"`file:([^`]+)`")
# 楔全般 (role 文から剥がす用)。
_ANY_WEDGE_RE = re.compile(r"`(?:cli|api|skill|file):[^`]+`")


def governs_from_note(note: str) -> str:
    """監査 note から統べる spine§ を抽出し ``§2,§4b`` の形で返す (初出順・重複除去)。"""
    seen: list[str] = []
    for m in _SECTION_RE.findall(note or ""):
        tok = "§" + m
        if tok not in seen:
            seen.append(tok)
    return ",".join(seen)


def _norm_axes(axes: str) -> str:
    """axes セル (``exec-auth,target-first``) を空白除去して正規化。"""
    return ",".join(t.strip() for t in (axes or "").split(",") if t.strip())


def nodes_from_inventory(markdown: str) -> list[Node]:
    """module 監査 doc の本文から 150 module の node を作る。

    ``needs-revision`` 系は表の行 (path/sev/axes/verdict/note)、``conforms`` /
    ``not-relevant`` は末尾の ``## 変更不要`` 節にバッククォート列挙。両方を拾う。
    """
    nodes: list[Node] = []
    seen_paths: set[str] = set()
    lines = markdown.splitlines()

    conforms_start = None
    for i, line in enumerate(lines):
        if _CONFORMS_HEADER_RE.match(line.strip()):
            conforms_start = i
            break
    table_lines = lines if conforms_start is None else lines[:conforms_start]

    for line in table_lines:
        m = _ROW_RE.match(line)
        if not m:
            continue
        path, _sev, axes, _verdict, note = (g.strip() for g in m.groups())
        if path in seen_paths:
            continue
        seen_paths.add(path)
        nodes.append(Node(
            id=path, path=path, seam=_norm_axes(axes),
            governs=governs_from_note(note),
        ))

    # 末尾集約: axes を持たない (継ぎ目未分類の) conforms/not-relevant module。
    if conforms_start is not None:
        tail = "\n".join(lines[conforms_start:])
        for path in re.findall(r"`([^`]+)`", tail):
            path = path.strip()
            if path and path not in seen_paths:
                seen_paths.add(path)
                nodes.append(Node(id=path, path=path))

    return nodes


def roles_from_app_map(text: str) -> dict[str, str]:
    """application-map の ``file:<path>`` 楔から path → value 文脈 (散文) を作る。

    方針1: app-map は「ユーザー機能を忘れないための保険」として value 文脈を添える
    側。楔で名指しされた裏方 module に、その行の散文を role として与える。
    """
    roles: dict[str, str] = {}
    for line in (text or "").splitlines():
        paths = _FILE_WEDGE_RE.findall(line)
        if not paths:
            continue
        prose = _ANY_WEDGE_RE.sub("", line).strip()
        prose = prose.lstrip("-").strip()
        if not prose:
            continue
        for p in paths:
            roles.setdefault(p.strip(), prose)
    return roles


def add_shares_seam_edges(graph: CodeGraph) -> int:
    """継ぎ目 (seam) を第一級 node にし、所属 module から ``shares-seam`` 辺を張る。

    方針2/3: cluster (継ぎ目) を「seam node」として台帳から自動作成し (``seam:<axis>``)、
    その seam に所属する各 module から seam node へ無向辺を張る。こうすると:

    - **継ぎ目が addressable** になり、``seam:<axis>`` を指定してその部分グラフ
      (所属 module) を 1 hop で引ける (e-5541「継ぎ目を指定すると部分グラフが返る」)。
    - **辺が O(所属数)** に収まる (全 module 対の clique = O(n²) の冗長を避ける)。
      「A と B が継ぎ目を共有するか」は同じ seam node を介した 2 hop で辿れる。

    継ぎ目を持たない末尾集約 module は孤立のまま残る (depends-on は e-5540 の機械導出、
    contract は e-5542 の curate で後から付く)。追加した辺数を返す。
    """
    added = 0
    for seam in graph.seams():  # snapshot: module node の seam から導出
        seam_node_id = seam_node(seam)
        graph.add_node(Node(id=seam_node_id, role=f"継ぎ目 (cluster): {seam}", seam=seam))
        for member in sorted(graph.nodes_in_seam(seam), key=lambda n: n.id):
            if member.id == seam_node_id:
                continue  # seam node 自身 (seam=自分) は除く
            if graph.add_edge(Edge(src=member.id, dst=seam_node_id,
                                   type="shares-seam", label=seam)):
                added += 1
    return added


def seam_node(seam: str) -> str:
    """継ぎ目名から seam node の id を作る (``seam:<axis>``)。"""
    return f"seam:{seam}"


def is_seam_node(node: Node) -> bool:
    """seam node (継ぎ目そのものの node) か。module node と区別する。"""
    return node.id.startswith("seam:") and not node.path


def build_seed_graph(inventory_markdown: str, app_map_text: str = "") -> CodeGraph:
    """3つの種から adjacency を組み立てた ``CodeGraph`` を返す。

    1. 監査から 150 node。 2. app-map から value 文脈 (role) を上書き補完。
    3. seam 共有から ``shares-seam`` 辺。
    """
    graph = CodeGraph()
    for node in nodes_from_inventory(inventory_markdown):
        graph.add_node(node)

    if app_map_text:
        roles = roles_from_app_map(app_map_text)
        for path, role in roles.items():
            existing = graph.get_node(path)
            if existing is not None and not existing.role:
                existing.role = role

    add_shares_seam_edges(graph)
    return graph
