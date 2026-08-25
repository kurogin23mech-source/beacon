"""Tests for the seam→subgraph navigate query (ms-156 e-5541).

Covers lib/code_graph_query: pull the subgraph for a seam (members + contract +
guard_test + adjacency), classify internal vs boundary dependencies, and the
module-neighborhood query. Hermetic — a small hand-built graph.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import code_graph as cg  # noqa: E402
import code_graph_query as q  # noqa: E402


@pytest.fixture
def graph():
    """seam exec-auth = {auth, store}; seam other = {store, misc}.
    depends-on: auth→store (internal to exec-auth), auth→ext (boundary),
    misc→store (boundary into exec-auth from other-only member).
    """
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/auth.py", path="lib/auth.py", seam="exec-auth",
                       governs="§2", contract="tokens は id_token を返す",
                       guard_test="tests/test_auth.py"))
    g.add_node(cg.Node(id="lib/store.py", path="lib/store.py", seam="exec-auth,other"))
    g.add_node(cg.Node(id="lib/misc.py", path="lib/misc.py", seam="other"))
    g.add_node(cg.Node(id="lib/ext.py", path="lib/ext.py"))  # no seam
    g.add_node(cg.Node(id="seam:exec-auth", role="継ぎ目", seam="exec-auth"))
    g.add_edge(cg.Edge("lib/auth.py", "lib/store.py", "depends-on"))
    g.add_edge(cg.Edge("lib/auth.py", "lib/ext.py", "depends-on"))
    g.add_edge(cg.Edge("lib/misc.py", "lib/store.py", "depends-on"))
    return g


def test_predecessors_reverse_lookup(graph):
    # store is depended on by auth and misc
    preds = sorted(s for s, _ in graph.predecessors("lib/store.py", edge_type="depends-on"))
    assert preds == ["lib/auth.py", "lib/misc.py"]
    # auth depends on nothing-inward
    assert graph.predecessors("lib/auth.py", edge_type="depends-on") == []


def test_subgraph_for_seam_returns_members_with_contract_and_guard(graph):
    sub = q.subgraph_for_seam(graph, "exec-auth")
    assert sub["seam"] == "exec-auth"
    assert sub["member_count"] == 2
    ids = [m["id"] for m in sub["members"]]
    assert ids == ["lib/auth.py", "lib/store.py"]     # sorted, seam node excluded
    auth = next(m for m in sub["members"] if m["id"] == "lib/auth.py")
    assert auth["contract"] == "tokens は id_token を返す"
    assert auth["guard_test"] == "tests/test_auth.py"
    assert auth["governs"] == "§2"
    assert auth["depends_on"] == ["lib/ext.py", "lib/store.py"]
    # store is depended on by auth (within seam) and misc (outside)
    store = next(m for m in sub["members"] if m["id"] == "lib/store.py")
    assert store["depended_on_by"] == ["lib/auth.py", "lib/misc.py"]


def test_subgraph_accepts_seam_node_id_form(graph):
    # "seam:exec-auth" normalizes to "exec-auth"
    sub = q.subgraph_for_seam(graph, "seam:exec-auth")
    assert sub["member_count"] == 2


def test_subgraph_classifies_internal_vs_boundary_dependencies(graph):
    sub = q.subgraph_for_seam(graph, "exec-auth")
    internal = {(e["src"], e["dst"]) for e in sub["internal_dependencies"]}
    boundary = {(e["src"], e["dst"]) for e in sub["boundary_dependencies"]}
    # auth→store: both in seam → internal
    assert ("lib/auth.py", "lib/store.py") in internal
    # auth→ext: ext outside → boundary (out); misc→store: misc outside → boundary (in)
    assert ("lib/auth.py", "lib/ext.py") in boundary
    assert ("lib/misc.py", "lib/store.py") in boundary
    assert ("lib/auth.py", "lib/store.py") not in boundary


def test_subgraph_empty_for_unknown_seam(graph):
    sub = q.subgraph_for_seam(graph, "no-such-seam")
    assert sub["member_count"] == 0
    assert sub["members"] == []


def test_member_view_includes_surfaces_as():
    """module 断面に surfaces-as (露出する CLI/API 入口) が含まれる (e-5558)。"""
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/cmd_task.py", path="lib/cmd_task.py"))
    g.add_node(cg.Node(id="cli:beacon task add"))
    g.add_edge(cg.Edge("lib/cmd_task.py", "cli:beacon task add", "surfaces-as"))
    view = q.neighborhood_for_module(g, "lib/cmd_task.py")
    assert view["surfaces_as"] == ["cli:beacon task add"]


def test_module_neighborhood(graph):
    view = q.neighborhood_for_module(graph, "lib/store.py")
    assert view["found"] is True
    assert set(view["seams"]) == {"exec-auth", "other"}
    assert view["depended_on_by"] == ["lib/auth.py", "lib/misc.py"]

    missing = q.neighborhood_for_module(graph, "lib/nope.py")
    assert missing["found"] is False
