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


# --- e-5558: call / route / cli surfaces-as / .mjs ------------------------

@pytest.fixture
def machine_repo(tmp_path):
    """call / route / cli / .mjs の機械層を持つ最小 source tree。"""
    (tmp_path / "lib").mkdir()
    (tmp_path / "server").mkdir()
    (tmp_path / "channel").mkdir()
    # call graph: caller が helper を import して helper.do() を呼ぶ
    (tmp_path / "lib" / "helper.py").write_text("def do():\n    return 1\n")
    (tmp_path / "lib" / "caller.py").write_text(
        "import helper\n\ndef run():\n    return helper.do()\n")
    # cli surfaces: commands.py dispatch → cmd_thing の handler
    (tmp_path / "lib" / "cmd_thing.py").write_text(
        "def cmd_thing_add():\n    pass\ndef cmd_thing_list():\n    pass\n")
    (tmp_path / "lib" / "commands.py").write_text(
        "from cmd_thing import cmd_thing_add, cmd_thing_list\n\n"
        "def dispatch():\n"
        "    commands = {\n"
        '        "thing_add": cmd_thing_add,\n'
        '        "thing_list": cmd_thing_list,\n'
        "    }\n"
        "    return commands\n")
    # route surfaces: routers_x.py の @router 経路
    (tmp_path / "server" / "routers_x.py").write_text(
        "@router.get('/api/x/{id}')\ndef get_x():\n    pass\n"
        "@router.post('/api/x')\ndef make_x():\n    pass\n")
    # .mjs import
    (tmp_path / "channel" / "a.mjs").write_text(
        "import { z } from './b.mjs'\nimport './c.mjs'\n")
    (tmp_path / "channel" / "b.mjs").write_text("export const z = 1\n")
    (tmp_path / "channel" / "c.mjs").write_text("// c\n")
    return str(tmp_path)


def test_call_edges_derive_depends_on(machine_repo):
    mods = derive.enumerate_source_modules(machine_repo)
    edges = {(e.src, e.dst) for e in derive.call_edges(machine_repo, mods)}
    assert ("lib/caller.py", "lib/helper.py") in edges     # helper.do() 呼び出し
    assert all(e.type == "depends-on" for e in derive.call_edges(machine_repo, mods))


def test_mjs_import_edges(machine_repo):
    mods = derive.enumerate_source_modules(machine_repo)
    edges = {(e.src, e.dst) for e in derive.mjs_import_edges(machine_repo, mods)}
    assert ("channel/a.mjs", "channel/b.mjs") in edges     # import { z } from './b.mjs'
    assert ("channel/a.mjs", "channel/c.mjs") in edges     # import './c.mjs'


def test_route_surfaces(machine_repo):
    mods = derive.enumerate_source_modules(machine_repo)
    pairs = set(derive.route_surfaces(machine_repo, mods))
    assert ("server/routers_x.py", "api:GET /api/x/{}") in pairs   # {id} 正規化
    assert ("server/routers_x.py", "api:POST /api/x") in pairs


def test_cli_surfaces(machine_repo):
    mods = derive.enumerate_source_modules(machine_repo)
    pairs = set(derive.cli_surfaces(machine_repo, mods))
    # dispatch verb → handler → module, verb の _ は空白へ (app-map 楔と同じ体系)
    assert ("lib/cmd_thing.py", "cli:beacon thing add") in pairs
    assert ("lib/cmd_thing.py", "cli:beacon thing list") in pairs


def test_import_alias_map_both_styles(machine_repo):
    import ast
    mods = derive.enumerate_source_modules(machine_repo)
    tree = ast.parse(open(os.path.join(machine_repo, "lib", "commands.py")).read())
    alias = derive.import_alias_map(tree, mods, "lib")
    assert alias["cmd_thing_add"] == "lib/cmd_thing.py"
    assert alias["cmd_thing_list"] == "lib/cmd_thing.py"


def test_augment_adds_surfaces_and_diff_is_clean(machine_repo):
    g = cg.CodeGraph()
    stats = derive.augment_with_machine_layer(g, machine_repo)
    assert stats["surfaces_added"] >= 4          # 2 routes + 2 cli verbs
    # surface nodes are addressable, module → surface edge exists
    surf = [d for d, _ in g.neighbors("lib/cmd_thing.py", edge_type="surfaces-as")]
    assert "cli:beacon thing add" in surf
    # freshly-augmented graph has zero drift across all three edge families
    diff = derive.diff_against_source(g, machine_repo)
    assert diff["clean"] is True
    assert diff["missing_surfaces"] == [] and diff["phantom_surfaces"] == []


def test_diff_reports_missing_surface(machine_repo):
    g = cg.CodeGraph()
    derive.augment_with_machine_layer(g, machine_repo)
    # drop a surfaces-as by rebuilding without it → simulate via phantom instead:
    g.add_node(cg.Node(id="api:GET /api/ghost"))
    g.add_edge(cg.Edge("server/routers_x.py", "api:GET /api/ghost", "surfaces-as"))
    diff = derive.diff_against_source(g, machine_repo)
    assert diff["clean"] is False
    assert ("server/routers_x.py", "api:GET /api/ghost") in [tuple(x) for x in diff["phantom_surfaces"]]
