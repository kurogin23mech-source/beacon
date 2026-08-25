"""Tests for the code-understanding graph layer (ms-156 e-5539).

Covers lib/code_graph (node/edge schema, adjacency query, Beacon table-doc
round-trip) and lib/code_graph_seed (parsing the module audit + application-map
into a seeded adjacency). All hermetic — no cloud, small inline fixtures.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import code_graph as cg  # noqa: E402
import code_graph_seed as seed  # noqa: E402
import table_doc  # noqa: E402


# --- fixtures ---------------------------------------------------------------

INVENTORY_MD = """---
scope: spec
---
# module 監査 詳細

> 前置き。

| module | sev | axes | verdict | migration note |
|---|---|---|---|---|
| `lib/a.py` | high | exec-auth, target-first | needs-revision | spine §2 の綻び。§4b も関与。 |
| `lib/b.py` | medium | exec-auth | needs-revision | §2 の client 側入口。 |
| `lib/c.py` | low | target-first | needs-revision | target 中心の対象。 |

## 変更不要 (conforms/not-relevant, 2件)

`lib/d.py`, `server/e.py`
"""

APP_MAP_MD = """# 全貌マップ

## A. 見失わない
- ユーザーに嬉しい機能X `cli:beacon x` `file:lib/a.py`
- 裏方の仕組みE `file:server/e.py`
- 楔の無い行は無視される
"""


# --- schema / validation ----------------------------------------------------

def test_edge_type_provenance_defaults():
    assert cg.EDGE_TYPES == ("depends-on", "shares-seam",
                             "implements-contract", "surfaces-as")
    assert cg.Edge("x", "y", "depends-on").provenance == "machine"
    assert cg.Edge("x", "y", "shares-seam").provenance == "derived"
    assert cg.Edge("x", "y", "implements-contract").provenance == "curated"


def test_validate_node_requires_id():
    with pytest.raises(cg.CodeGraphError):
        cg.validate_node(cg.Node(id=""))


def test_validate_edge_rejects_unknown_type_and_self_loop():
    with pytest.raises(cg.CodeGraphError):
        cg.validate_edge(cg.Edge("a", "b", "no-such-type"))
    with pytest.raises(cg.CodeGraphError):
        cg.validate_edge(cg.Edge("a", "a", "depends-on"))
    with pytest.raises(cg.CodeGraphError):
        cg.validate_edge(cg.Edge("", "b", "depends-on"))


# --- CodeGraph model --------------------------------------------------------

def test_add_node_merges_seam_and_governs_union_and_curated_overwrite():
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/a.py", path="lib/a.py", seam="exec-auth", governs="§2"))
    g.add_node(cg.Node(id="lib/a.py", seam="target-first", governs="§4b", role="核"))
    node = g.get_node("lib/a.py")
    assert node.seams() == ["exec-auth", "target-first"]
    assert node.governs == "§2,§4b"
    assert node.role == "核"          # non-empty curated cell fills in
    assert node.path == "lib/a.py"    # preserved from first insert


def test_add_edge_dedups_undirected_regardless_of_endpoint_order():
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="a"))
    g.add_node(cg.Node(id="b"))
    assert g.add_edge(cg.Edge("a", "b", "shares-seam", label="s")) is True
    # same undirected pair + label, endpoints swapped → deduped
    assert g.add_edge(cg.Edge("b", "a", "shares-seam", label="s")) is False
    assert len(g.edges()) == 1


def test_neighbors_directed_vs_undirected():
    g = cg.CodeGraph()
    for nid in ("a", "b", "c"):
        g.add_node(cg.Node(id=nid))
    g.add_edge(cg.Edge("a", "b", "depends-on"))     # directed a->b
    g.add_edge(cg.Edge("a", "c", "shares-seam"))    # undirected a-c
    # depends-on: only reachable from src side
    assert [n for n, _ in g.neighbors("a", edge_type="depends-on")] == ["b"]
    assert g.neighbors("b", edge_type="depends-on") == []
    # shares-seam: reachable from either endpoint
    assert [n for n, _ in g.neighbors("a", edge_type="shares-seam")] == ["c"]
    assert [n for n, _ in g.neighbors("c", edge_type="shares-seam")] == ["a"]


def test_nodes_in_seam_and_seams_listing():
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="a", seam="exec-auth,target-first"))
    g.add_node(cg.Node(id="b", seam="exec-auth"))
    assert {n.id for n in g.nodes_in_seam("exec-auth")} == {"a", "b"}
    assert {n.id for n in g.nodes_in_seam("target-first")} == {"a"}
    assert g.seams() == ["exec-auth", "target-first"]


# --- Beacon table-doc round-trip (SPEC 方針6: dogfood 格納) ------------------

def test_table_model_roundtrip_preserves_nodes_and_edges():
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/a.py", path="lib/a.py", seam="exec-auth",
                       governs="§2", role="核", guard_test="tests/t.py"))
    g.add_node(cg.Node(id="lib/b.py", path="lib/b.py", seam="exec-auth"))
    g.add_edge(cg.Edge("lib/a.py", "lib/b.py", "shares-seam", label="exec-auth"))

    g2 = cg.CodeGraph.from_tables(g.to_node_table(), g.to_edge_table())
    a = g2.get_node("lib/a.py")
    assert a is not None and a.role == "核" and a.governs == "§2"
    assert a.guard_test == "tests/t.py"
    assert len(g2.edges()) == 1
    assert g2.edges()[0].type == "shares-seam"
    assert g2.edges()[0].provenance == "derived"


def test_node_table_serializes_to_beacon_table_format():
    """to_node_table → serialize → parse は Beacon-native table-doc として往復する。"""
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/a.py", path="lib/a.py", seam="exec-auth"))
    body = table_doc.serialize_table_body("nodes", g.to_node_table())
    content = f"---\nscope: spec\nformat: {table_doc.TABLE_FORMAT}\n---\n" + body
    assert table_doc.is_table_content(content)
    reparsed = table_doc.parse_table(content)
    rows = table_doc.active_rows(reparsed)
    assert len(rows) == 1
    assert rows[0]["cells"]["id"] == "lib/a.py"
    assert table_doc.column_keys(reparsed) == list(cg.NODE_CELL_KEYS)


# --- seed parsing -----------------------------------------------------------

def test_governs_from_note_extracts_spine_sections():
    assert seed.governs_from_note("spine §2 の綻び。§4b も。§2 再掲。") == "§2,§4b"
    assert seed.governs_from_note("no section here") == ""


def test_nodes_from_inventory_parses_table_rows_and_trailing_list():
    nodes = seed.nodes_from_inventory(INVENTORY_MD)
    ids = {n.id for n in nodes}
    # 3 table rows + 2 trailing-list modules = 5, header row excluded
    assert ids == {"lib/a.py", "lib/b.py", "lib/c.py", "lib/d.py", "server/e.py"}
    by_id = {n.id: n for n in nodes}
    assert by_id["lib/a.py"].seams() == ["exec-auth", "target-first"]
    assert by_id["lib/a.py"].governs == "§2,§4b"
    assert by_id["lib/b.py"].seams() == ["exec-auth"]
    # trailing-list (conforms) modules carry no seam
    assert by_id["lib/d.py"].seam == ""
    assert by_id["server/e.py"].seam == ""


def test_roles_from_app_map_lifts_value_context_for_file_wedges():
    roles = seed.roles_from_app_map(APP_MAP_MD)
    assert roles["lib/a.py"] == "ユーザーに嬉しい機能X"
    assert roles["server/e.py"] == "裏方の仕組みE"
    # a line with no file: wedge contributes nothing
    assert set(roles) == {"lib/a.py", "server/e.py"}


def test_build_seed_graph_creates_seam_hub_and_membership_edges():
    g = seed.build_seed_graph(INVENTORY_MD, APP_MAP_MD)
    module_nodes = [n for n in g.nodes() if not seed.is_seam_node(n)]
    seam_nodes = [n for n in g.nodes() if seed.is_seam_node(n)]
    assert len(module_nodes) == 5
    assert {n.id for n in seam_nodes} == {"seam:exec-auth", "seam:target-first"}

    # app-map value context lands on the module node (role was empty before)
    assert g.get_node("lib/a.py").role == "ユーザーに嬉しい機能X"

    # the seam is addressable: its members are one hop away (e-5541 の礎)
    members = [nid for nid, _ in g.neighbors("seam:exec-auth", edge_type="shares-seam")]
    assert set(members) == {"lib/a.py", "lib/b.py"}

    # conforms modules with no seam stay isolated
    assert g.neighbors("lib/d.py") == []


def test_seed_edges_are_bounded_not_clique():
    """seam hub は O(所属数)。全対 clique (O(n^2)) にはしない。"""
    g = seed.build_seed_graph(INVENTORY_MD, APP_MAP_MD)
    # memberships: a,b in exec-auth (2) + a,c in target-first (2) = 4 edges
    assert len(g.edges()) == 4
