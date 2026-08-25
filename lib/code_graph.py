"""code_graph.py — コード理解グラフ層の schema + adjacency モデル (ms-156 e-5539)

エージェントがコード全体を読まずに「どこに何があり・何に依存し・どんな契約を
守るか」を引くための **構造＋契約の投影 (グラフ)**。コードそのものではなく、
module を node・継ぎ目 (seam) を共有する関係を edge にした adjacency (＝隣接) を
常時メンテし、変更前にその部分グラフだけを引く (SPEC ms-156 doc VMo6Eu614RpSzUXaSlkx)。

このモジュールは **グラフの真値の型** を1本に固定する:

- **node schema** (SPEC 受入条件2): ``id`` / ``path`` / ``span`` / ``role`` /
  ``contract`` / ``seam`` / ``governs`` (spine§) / ``guard_test``。
- **edge type** (SPEC 受入条件2) と provenance (＝その辺が誰の真値か):

    ============  ==========  =========================================
    edge type     provenance  意味
    ============  ==========  =========================================
    depends-on    machine     import/call/route から自動導出 (e-5540)
    shares-seam   derived     同じ継ぎ目 (cluster/axis) を共有 (このタスクが種)
    implements-contract curated 同じ契約を実装している (人手, e-5542)
    surfaces-as   machine     module → CLI/API/Skill surface (application-map)
    ============  ==========  =========================================

格納は Beacon-native (dogfood, SPEC 方針6): グラフを Beacon の table-doc
(``lib/table_doc``) 2枚 — nodes と edges — として直列化する。まず table adjacency
で足り、traversal が維持コストに見合う価値を払うなら graph primitive へ昇格する。
このモジュールは cloud に触らず (pure)、table_doc の model 変換だけを持つので、
CLI からも script からも同じ型で読み書きできる (架構: architecture-tool-skill-separation)。

粒度は機械 default = module (SPEC 方針2)。巨大な core/commands/app だけ query 時に
AI が function 粒度へ動的 zoom する (e-5543) が、そこはこの層の外 (静的な全展開は
しない)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import table_doc

# --- schema (SPEC 受入条件2) -------------------------------------------------

# node cell の列 (順序は table-doc の列定義に一致させる)。
NODE_CELL_KEYS = ("id", "path", "span", "role", "contract", "seam", "governs", "guard_test")

# edge type → provenance の既定 (＝その辺が「誰の真値か」)。この対応で、機械層
# (自動導出・0-drift 検証可) と curated 層 (人手・hook で鮮度強制) を型として分ける。
EDGE_PROVENANCE = {
    "depends-on": "machine",           # import/call/route から導出 (e-5540)
    "shares-seam": "derived",          # cluster/axis 共有から導出 (このタスク)
    "implements-contract": "curated",  # 契約を人手で結ぶ (e-5542)
    "surfaces-as": "machine",          # application-map の surface 接続
}
EDGE_TYPES = tuple(EDGE_PROVENANCE)

# 向きの意味: depends-on / surfaces-as は有向 (A→B)、shares-seam /
# implements-contract は対称 (A—B)。neighbors() は既定でこの対称集合を
# 双方向に辿る。
UNDIRECTED_EDGE_TYPES = frozenset({"shares-seam", "implements-contract"})

# edge cell の列。
EDGE_CELL_KEYS = ("src", "dst", "type", "provenance", "label")


class CodeGraphError(ValueError):
    """schema 違反 / 不正な node・edge に対して送出。"""


# --- node / edge ------------------------------------------------------------

@dataclass
class Node:
    """1つの module (default 粒度) を表す node。

    ``id`` は既定で ``path`` と同じ (module パス) だが、function 粒度へ zoom した
    node (e-5543) は ``path:span`` を別 id に持てるよう分離してある。curated 層の
    ``role`` / ``contract`` は種の時点では空で、人手 + reconcile hook で埋める。
    """

    id: str
    path: str = ""
    span: str = ""          # "" = ファイル全体 / "L10-L50" 等
    role: str = ""          # curated: この module の責務 (一言)
    contract: str = ""      # curated: 隣人への契約
    seam: str = ""          # 所属する継ぎ目 (cluster/axis)、複数はカンマ区切り
    governs: str = ""       # 統べる spine§ (例 "§2,§4b")
    guard_test: str = ""    # 挙動を固定する characterization test

    def cells(self) -> dict:
        return {
            "id": self.id, "path": self.path, "span": self.span,
            "role": self.role, "contract": self.contract, "seam": self.seam,
            "governs": self.governs, "guard_test": self.guard_test,
        }

    @classmethod
    def from_cells(cls, cells: dict) -> "Node":
        return cls(**{k: (cells.get(k) or "") for k in NODE_CELL_KEYS})

    def seams(self) -> list[str]:
        """所属する継ぎ目 (seam) のリスト。"""
        return _split_csv(self.seam)


@dataclass
class Edge:
    """2つの node を結ぶ辺。``type`` が edge type、``provenance`` が真値の出所。"""

    src: str
    dst: str
    type: str
    provenance: str = ""
    label: str = ""

    def __post_init__(self):
        if not self.provenance:
            self.provenance = EDGE_PROVENANCE.get(self.type, "")

    def cells(self) -> dict:
        return {"src": self.src, "dst": self.dst, "type": self.type,
                "provenance": self.provenance, "label": self.label}

    @classmethod
    def from_cells(cls, cells: dict) -> "Edge":
        return cls(src=cells.get("src") or "", dst=cells.get("dst") or "",
                   type=cells.get("type") or "", provenance=cells.get("provenance") or "",
                   label=cells.get("label") or "")

    def key(self) -> tuple:
        """dedup 用のキー。対称 edge は端点順に依らず同一視する。"""
        if self.type in UNDIRECTED_EDGE_TYPES:
            a, b = sorted((self.src, self.dst))
            return (self.type, a, b, self.label)
        return (self.type, self.src, self.dst, self.label)


def validate_node(node: Node) -> None:
    if not (node.id or "").strip():
        raise CodeGraphError("node.id は必須です")


def validate_edge(edge: Edge) -> None:
    if edge.type not in EDGE_PROVENANCE:
        raise CodeGraphError(
            f"未知の edge type: {edge.type!r} (定義済: {', '.join(EDGE_TYPES)})")
    if not (edge.src or "").strip() or not (edge.dst or "").strip():
        raise CodeGraphError("edge の src / dst は必須です")
    if edge.src == edge.dst:
        raise CodeGraphError(f"自己ループは許可されません: {edge.src}")


# --- graph ------------------------------------------------------------------

class CodeGraph:
    """node + edge の集合と adjacency (＝隣接) を保持し、部分グラフを引ける器。

    ``add_node`` は同 id を merge (curated セルは非空で上書き) するので、機械層と
    curated 層を別々に足し込んでも 1 node に束ねられる。edge は ``Edge.key()`` で
    dedup する (同じ継ぎ目の同じ pair を二重に張らない)。
    """

    def __init__(self):
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []
        self._edge_keys: set[tuple] = set()

    # -- 書き込み ----------------------------------------------------------
    def add_node(self, node: Node) -> None:
        validate_node(node)
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
            return
        # merge: 非空セルを上書き、seam / governs は和集合。
        for k in ("path", "span", "role", "contract", "guard_test"):
            v = getattr(node, k)
            if v:
                setattr(existing, k, v)
        existing.seam = _merge_csv(existing.seam, node.seam)
        existing.governs = _merge_csv(existing.governs, node.governs)

    def add_edge(self, edge: Edge) -> bool:
        """edge を追加。dedup で既存なら False、新規なら True。"""
        validate_edge(edge)
        k = edge.key()
        if k in self._edge_keys:
            return False
        self._edge_keys.add(k)
        self._edges.append(edge)
        return True

    # -- 読み取り ----------------------------------------------------------
    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def edges(self) -> list[Edge]:
        return list(self._edges)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def neighbors(self, node_id: str, *, edge_type: str | None = None) -> list[tuple[str, Edge]]:
        """``node_id`` の隣接 ``(neighbor_id, edge)`` を返す。

        有向 edge は src == node_id のとき dst を、対称 edge は src/dst どちらが
        node_id でも他端を返す (＝双方向に辿れる)。
        """
        out: list[tuple[str, Edge]] = []
        for e in self._edges:
            if edge_type is not None and e.type != edge_type:
                continue
            if e.src == node_id:
                out.append((e.dst, e))
            elif e.dst == node_id and e.type in UNDIRECTED_EDGE_TYPES:
                out.append((e.src, e))
        return out

    def nodes_in_seam(self, seam: str) -> list[Node]:
        """継ぎ目 ``seam`` に所属する module node を返す (SPEC 受入条件4 の礎)。"""
        return [n for n in self._nodes.values() if seam in n.seams()]

    def seams(self) -> list[str]:
        """グラフ中に現れる継ぎ目の一覧 (ソート済)。"""
        found: set[str] = set()
        for n in self._nodes.values():
            found.update(n.seams())
        return sorted(found)

    # -- Beacon table-doc 直列化 (SPEC 方針6: dogfood) --------------------
    def to_node_table(self) -> dict:
        """node を table_doc model (columns + rows) にする。"""
        table = table_doc.new_table(_node_columns())
        for node in self._nodes.values():
            table_doc.add_row(table, node.cells(), actor="code-graph", at="")
        return table

    def to_edge_table(self) -> dict:
        table = table_doc.new_table(_edge_columns())
        for edge in self._edges:
            table_doc.add_row(table, edge.cells(), actor="code-graph", at="")
        return table

    @classmethod
    def from_tables(cls, node_table: dict, edge_table: dict | None = None) -> "CodeGraph":
        """table_doc model 2枚から CodeGraph を復元する (round-trip)。"""
        g = cls()
        for row in table_doc.active_rows(node_table or {"rows": []}):
            g.add_node(Node.from_cells(row.get("cells", {})))
        if edge_table:
            for row in table_doc.active_rows(edge_table):
                g.add_edge(Edge.from_cells(row.get("cells", {})))
        return g


def _node_columns() -> list[dict]:
    return [{"key": k, "label": k, "type": "text"} for k in NODE_CELL_KEYS]


def _edge_columns() -> list[dict]:
    return [{"key": k, "label": k, "type": "text"} for k in EDGE_CELL_KEYS]


# --- 小さな helper ----------------------------------------------------------

def _split_csv(value: str) -> list[str]:
    return [tok.strip() for tok in (value or "").split(",") if tok.strip()]


def _merge_csv(a: str, b: str) -> str:
    """カンマ区切りの和集合を、初出順を保って結合する。"""
    seen: list[str] = []
    for tok in _split_csv(a) + _split_csv(b):
        if tok not in seen:
            seen.append(tok)
    return ",".join(seen)
