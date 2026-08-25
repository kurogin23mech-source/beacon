"""Tests for the machine layer of the code-understanding graph (ms-156 e-5540).

Covers lib/code_graph_derive: enumerate source modules, derive depends-on edges
from Python imports, augment a graph, and diff a stored graph against source
(0-drift check). Hermetic — builds a tiny throwaway source tree under tmp_path.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import code_graph as cg  # noqa: E402
import code_graph_derive as derive  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path):
    """lib/server/channel を持つ最小 source tree。

    依存: a→b (import b), b→c (from c import x), server/app→a (import a).
    os は外部なので辺にならない。__init__.py は module に数えない。
    """
    (tmp_path / "lib").mkdir()
    (tmp_path / "server").mkdir()
    (tmp_path / "channel").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("")
    (tmp_path / "lib" / "a.py").write_text("import b\nimport os\n")
    (tmp_path / "lib" / "b.py").write_text("from c import x\n")
    (tmp_path / "lib" / "c.py").write_text("pass\n")
    (tmp_path / "server" / "app.py").write_text("import a\nimport c\n")
    (tmp_path / "channel" / "bus.mjs").write_text("// js\n")
    return str(tmp_path)


def test_enumerate_source_modules_excludes_init(fake_repo):
    mods = derive.enumerate_source_modules(fake_repo)
    assert mods == {"lib/a.py", "lib/b.py", "lib/c.py",
                    "server/app.py", "channel/bus.mjs"}


def test_python_import_edges_resolves_within_module_set(fake_repo):
    mods = derive.enumerate_source_modules(fake_repo)
    edges = {(e.src, e.dst) for e in derive.python_import_edges(fake_repo, mods)}
    assert ("lib/a.py", "lib/b.py") in edges       # import b
    assert ("lib/b.py", "lib/c.py") in edges       # from c import x
    assert ("server/app.py", "lib/a.py") in edges  # server imports lib
    assert ("server/app.py", "lib/c.py") in edges
    # external (os) is not in the module set → no edge
    assert not any(dst == "os" for _, dst in edges)
    # every derived edge is typed depends-on
    assert all(e.type == "depends-on" for e in derive.python_import_edges(fake_repo, mods))


def test_augment_adds_module_nodes_and_import_edges(fake_repo):
    g = cg.CodeGraph()
    stats = derive.augment_with_machine_layer(g, fake_repo)
    assert stats["nodes_added"] == 5
    assert stats["edges_added"] == 4
    assert {n.id for n in g.nodes()} == {"lib/a.py", "lib/b.py", "lib/c.py",
                                         "server/app.py", "channel/bus.mjs"}
    # depends-on is directed: a→b, b→c, but the reverse edges do not exist
    assert [d for d, _ in g.neighbors("lib/a.py", edge_type="depends-on")] == ["lib/b.py"]
    assert [d for d, _ in g.neighbors("lib/b.py", edge_type="depends-on")] == ["lib/c.py"]
    assert "lib/a.py" not in [d for d, _ in g.neighbors("lib/b.py", edge_type="depends-on")]


def test_augment_preserves_existing_curated_cells(fake_repo):
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/a.py", path="lib/a.py", role="核", seam="s1"))
    derive.augment_with_machine_layer(g, fake_repo)
    a = g.get_node("lib/a.py")
    assert a.role == "核" and a.seam == "s1"   # not clobbered by the bare node


def test_diff_clean_after_augment(fake_repo):
    g = cg.CodeGraph()
    derive.augment_with_machine_layer(g, fake_repo)
    diff = derive.diff_against_source(g, fake_repo)
    assert diff["clean"] is True
    assert diff["missing_nodes"] == [] and diff["phantom_nodes"] == []
    assert diff["missing_edges"] == [] and diff["phantom_edges"] == []


def test_diff_reports_missing_and_phantom(fake_repo):
    g = cg.CodeGraph()
    derive.augment_with_machine_layer(g, fake_repo)
    # simulate drift: add a phantom module node that no longer exists in source
    g.add_node(cg.Node(id="lib/gone.py", path="lib/gone.py"))
    diff = derive.diff_against_source(g, fake_repo)
    assert diff["clean"] is False
    assert "lib/gone.py" in diff["phantom_nodes"]

    # a fresh graph missing everything → all source nodes/edges are "missing"
    empty = cg.CodeGraph()
    diff2 = derive.diff_against_source(empty, fake_repo)
    assert set(diff2["missing_nodes"]) == derive.enumerate_source_modules(fake_repo)
    assert ("lib/a.py", "lib/b.py") in [tuple(e) for e in diff2["missing_edges"]]


def test_seam_nodes_are_excluded_from_module_diff(fake_repo):
    """shares-seam の seam node (継ぎ目) は module 照合の対象外 (source 由来でない)。"""
    g = cg.CodeGraph()
    derive.augment_with_machine_layer(g, fake_repo)
    g.add_node(cg.Node(id="seam:exec-auth", role="継ぎ目"))
    diff = derive.diff_against_source(g, fake_repo)
    # seam node must NOT show up as a phantom module
    assert "seam:exec-auth" not in diff["phantom_nodes"]
    assert diff["clean"] is True
