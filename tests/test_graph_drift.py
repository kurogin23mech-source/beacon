"""Tests for the code-graph drift machinery (ms-156 e-5628).

Mirror of ``tests/test_map_drift.py``: makes ``scripts/check-graph-drift.py``'s
0-drift verification **CI-resident** so the machinery can't silently rot as
source evolves, and pins the deploy-time re-seed forcing function
(``cmd_deploy._fire_graph_reseed_trigger``).

The ms-156 target-close review (2026-08-25) found the graph had drifted from main
after merge (``lib/root_target.py`` missing from the node table) precisely because
``check-graph-drift`` was on-demand only — "0-drift *verified*" was really
"0-drift *verifiable*". This test closes that gap on the CI side.

Two surfaces:

  (A) lib/code_graph_derive.diff_against_source + scripts/check-graph-drift.py —
      the mechanical reconcile (module nodes + machine-layer depends-on /
      surfaces-as edges, "実在 vs graph"). The headline CI guarantee is the
      **round-trip invariant**: deriving the machine layer from the real repo
      source and diffing it back against source must be clean (0 drift). That runs
      the full derive pipeline over all of lib/server/channel on every CI, so an
      AST break or a derive/diff divergence turns CI red the moment it lands. On
      top of that we pin synthetic missing/phantom detection, table-doc loading,
      the source-project guard, and main()'s exit-code contract (0 clean / 1 drift
      / 2 fatal / 3 skip, plus --nodes-file/--edges-file pairing).

  (B) lib/cmd_deploy._fire_graph_reseed_trigger — the ship-time trigger that
      prompts a re-seed at deploy (mirror of _fire_map_reconcile_trigger).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import code_graph  # noqa: E402
import code_graph_derive as derive  # noqa: E402
import code_graph_store  # noqa: E402
import table_doc  # noqa: E402
from code_graph import CodeGraph, Edge, Node  # noqa: E402


def _load_check_graph_drift():
    path = REPO_ROOT / "scripts" / "check-graph-drift.py"
    spec = importlib.util.spec_from_file_location("check_graph_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CGD = _load_check_graph_drift()

# check-graph-drift.py fixes REPO to its own install location (= the beacon repo
# under test), and diff_against_source reads that same REPO. Keep the two aligned
# so file-based fixtures derived here diff against the same source the script does.
assert Path(CGD.REPO).resolve() == REPO_ROOT


# --------------------------------------------------------------- fixtures/helpers

_TABLE_FRONTMATTER = "---\nformat: table\n---\n\n"


def _derived_source_graph() -> CodeGraph:
    """The graph the machine layer *should* be: derived fresh from real source."""
    g = CodeGraph()
    derive.augment_with_machine_layer(g, CGD.REPO)
    return g


def _serialize_graph(graph: CodeGraph) -> tuple[str, str]:
    """Serialize a graph to (nodes_content, edges_content) parseable table-docs.

    ``serialize_table_body`` emits only the body; ``parse_table`` needs a
    ``format: table`` frontmatter, so we prepend it (matching the document write
    path that adds frontmatter in production)."""
    nodes = _TABLE_FRONTMATTER + table_doc.serialize_table_body(
        code_graph_store.NODES_TITLE, graph.to_node_table())
    edges = _TABLE_FRONTMATTER + table_doc.serialize_table_body(
        code_graph_store.EDGES_TITLE, graph.to_edge_table())
    return nodes, edges


# ============================================================ (A) derive machinery

def test_enumerate_source_modules_includes_known_and_excludes_dunder():
    mods = derive.enumerate_source_modules(CGD.REPO)
    # representative modules from each source dir
    assert "lib/commands.py" in mods
    assert "lib/core.py" in mods
    assert "server/app.py" in mods
    assert "channel/bus.mjs" in mods
    # package markers are not modules
    assert not any(m.endswith("/__init__.py") for m in mods)


def test_round_trip_derive_is_clean_against_real_source():
    """Headline CI guarantee: the machine layer derived from real source, diffed
    back against that source, has 0 drift. Runs the full derive pipeline over all
    of lib/server/channel every CI — an AST break or a derive/diff divergence
    turns this red immediately."""
    g = _derived_source_graph()
    diff = derive.diff_against_source(g, CGD.REPO)
    assert diff["clean"] is True, {
        k: diff[k] for k in (
            "missing_nodes", "phantom_nodes", "missing_edges", "phantom_edges",
            "missing_surfaces", "phantom_surfaces") if diff[k]
    }
    # sanity: the pipeline actually produced a non-trivial graph (not vacuously
    # clean because everything came back empty).
    assert diff["counts"]["source_modules"] > 50
    assert diff["counts"]["graph_depends_on"] > 50


def test_diff_detects_phantom_module_node():
    g = _derived_source_graph()
    g.add_node(Node(id="lib/ghost_xyz999.py", path="lib/ghost_xyz999.py"))
    diff = derive.diff_against_source(g, CGD.REPO)
    assert diff["clean"] is False
    assert "lib/ghost_xyz999.py" in diff["phantom_nodes"]


def test_diff_detects_phantom_depends_on_edge():
    g = _derived_source_graph()
    # an edge source never derives: real module → phantom module (dedup-safe key)
    g.add_node(Node(id="lib/ghost_xyz999.py", path="lib/ghost_xyz999.py"))
    g.add_edge(Edge(src="lib/core.py", dst="lib/ghost_xyz999.py", type="depends-on"))
    diff = derive.diff_against_source(g, CGD.REPO)
    assert diff["clean"] is False
    assert ("lib/core.py", "lib/ghost_xyz999.py") in diff["phantom_edges"]


def test_diff_detects_missing_module_node():
    """A graph that omits a real source module reports it as 書き漏れ (missing)."""
    g = CodeGraph()
    real = sorted(derive.enumerate_source_modules(CGD.REPO))
    dropped = real[0]
    for m in real[1:]:
        g.add_node(Node(id=m, path=m))
    diff = derive.diff_against_source(g, CGD.REPO)
    assert diff["clean"] is False
    assert dropped in diff["missing_nodes"]


# only seam / curated layers are exempt; machine layers are always checked
def test_seam_and_curated_layers_are_not_treated_as_drift():
    g = _derived_source_graph()
    # a curated seam node + a human implements-contract edge must NOT count as
    # phantom (diff only reconciles module nodes + machine edges).
    g.add_node(Node(id="seam:paradigm-migration", role="継ぎ目"))
    g.add_edge(Edge(src="lib/core.py", dst="lib/commands.py",
                    type="implements-contract"))
    diff = derive.diff_against_source(g, CGD.REPO)
    assert diff["clean"] is True


# ------------------------------------------------------------- (A) table-doc load

def test_load_graph_round_trips_module_nodes_and_edges():
    src = CodeGraph()
    src.add_node(Node(id="lib/a.py", path="lib/a.py"))
    src.add_node(Node(id="lib/b.py", path="lib/b.py"))
    src.add_edge(Edge(src="lib/a.py", dst="lib/b.py", type="depends-on"))
    nodes_content, edges_content = _serialize_graph(src)
    loaded = CGD._load_graph(nodes_content, edges_content)
    assert loaded.has_node("lib/a.py") and loaded.has_node("lib/b.py")
    assert ("lib/a.py", "lib/b.py") in {
        (e.src, e.dst) for e in loaded.edges() if e.type == "depends-on"}


# ------------------------------------------------------------------ (A) 文脈ガード

def test_guard_true_in_actual_beacon_repo():
    assert CGD._is_beacon_source_project(str(REPO_ROOT)) is True


def test_guard_false_in_foreign_dir(tmp_path):
    assert CGD._is_beacon_source_project(str(tmp_path)) is False


# -------------------------------------------------------------- (A) main() exits

def test_main_skips_when_not_beacon_source(monkeypatch, capsys):
    """When the guard says "not beacon source", main() skips with exit 3 — never
    exit 0, so a ``$?``-only caller can't read skip as "verified clean"."""
    monkeypatch.setattr(CGD, "_is_beacon_source_project", lambda repo: False)
    monkeypatch.setattr(sys, "argv", ["check-graph-drift.py", "--json"])
    rc = CGD.main()
    out = capsys.readouterr().out
    assert rc == CGD.SKIP_EXIT == 3
    assert json.loads(out)["skipped"] is True


def test_main_rejects_unpaired_nodes_file(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["check-graph-drift.py", "--nodes-file", "n.md"])
    with pytest.raises(SystemExit):
        CGD.main()


def test_main_rejects_unpaired_doc(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["check-graph-drift.py", "--nodes-doc", "only-nodes"])
    with pytest.raises(SystemExit):
        CGD.main()


def test_main_clean_against_derived_files(tmp_path, monkeypatch):
    """End-to-end CI shape: derive → serialize → main() over the files → exit 0.

    This is the file-based hermetic path (no cloud), the exact form a CI job runs
    against a committed / freshly-derived snapshot."""
    nodes_content, edges_content = _serialize_graph(_derived_source_graph())
    nf = tmp_path / "nodes.md"
    ef = tmp_path / "edges.md"
    nf.write_text(nodes_content, encoding="utf-8")
    ef.write_text(edges_content, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "check-graph-drift.py",
        "--nodes-file", str(nf), "--edges-file", str(ef),
    ])
    assert CGD.main() == 0


def test_main_drift_against_derived_files_exits_1(tmp_path, monkeypatch):
    g = _derived_source_graph()
    g.add_node(Node(id="lib/ghost_xyz999.py", path="lib/ghost_xyz999.py"))
    nodes_content, edges_content = _serialize_graph(g)
    nf = tmp_path / "nodes.md"
    ef = tmp_path / "edges.md"
    nf.write_text(nodes_content, encoding="utf-8")
    ef.write_text(edges_content, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "check-graph-drift.py",
        "--nodes-file", str(nf), "--edges-file", str(ef),
    ])
    assert CGD.main() == 1


# ================================================ (B) deploy-time re-seed trigger

import cmd_deploy  # noqa: E402


class _FakeStore:
    """A store whose graph nodes doc is ``nodes_doc`` (None = never seeded)."""

    def __init__(self, nodes_doc):
        self._nodes_doc = nodes_doc

    def get_document(self, doc_id):
        if doc_id == code_graph_store.NODES_DOC_ID:
            return self._nodes_doc
        return None


def test_graph_reseed_fires_when_graph_doc_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(cmd_deploy, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cmd_deploy, "get_store",
                        lambda: _FakeStore({"updated_at": "2026-08-01T00:00:00"}))
    cmd_deploy._fire_graph_reseed_trigger()
    p = tmp_path / "graph-reseed.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["kind"] == "graph-reseed"
    assert "check-graph-drift.py" in data["message"]


def test_graph_reseed_skips_when_graph_doc_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cmd_deploy, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cmd_deploy, "get_store", lambda: _FakeStore(None))
    cmd_deploy._fire_graph_reseed_trigger()
    assert not (tmp_path / "graph-reseed.json").exists()


def test_graph_reseed_degrades_silently_on_store_error(tmp_path, monkeypatch):
    def _boom():
        raise RuntimeError("cloud down")
    monkeypatch.setattr(cmd_deploy, "_get_triggers_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cmd_deploy, "get_store", _boom)
    # must not raise, must not write
    cmd_deploy._fire_graph_reseed_trigger()
    assert not (tmp_path / "graph-reseed.json").exists()
