"""Tests for migration-cluster navigate coverage (ms-156 e-5544).

Covers lib/code_graph_query.seam_coverage: cross-reference the migration ledger's
clusters against the code-graph's seams, proving each migration cluster resolves
to a navigable subgraph (customer coupling, 受入条件6). Hermetic.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import code_graph as cg  # noqa: E402
import code_graph_query as q  # noqa: E402


def _graph():
    g = cg.CodeGraph()
    g.add_node(cg.Node(id="lib/auth.py", path="lib/auth.py", seam="exec-auth",
                       contract="id_token を発行", guard_test="tests/test_auth.py"))
    g.add_node(cg.Node(id="lib/store.py", path="lib/store.py", seam="exec-auth"))
    g.add_node(cg.Node(id="lib/core.py", path="lib/core.py", seam="phase-cyclic"))
    return g


def test_coverage_reports_navigable_clusters():
    # both ledger clusters map to seams with members → all covered
    report = q.seam_coverage(_graph(), ["exec-auth", "phase-cyclic"])
    assert report["all_covered"] is True
    assert report["covered_count"] == 2 and report["cluster_count"] == 2
    exec_auth = next(c for c in report["clusters"] if c["cluster"] == "exec-auth")
    assert exec_auth["member_count"] == 2
    assert exec_auth["with_contract"] == 1        # only auth has a contract
    assert exec_auth["with_guard_test"] == 1


def test_coverage_flags_uncovered_migration_cluster():
    # a ledger cluster with no graph seam is a coverage gap (graph not serving it)
    report = q.seam_coverage(_graph(), ["exec-auth", "no-such-cluster"])
    assert report["all_covered"] is False
    gap = next(c for c in report["clusters"] if c["cluster"] == "no-such-cluster")
    assert gap["covered"] is False and gap["member_count"] == 0


def test_coverage_accepts_seam_prefixed_names():
    report = q.seam_coverage(_graph(), ["seam:exec-auth"])
    assert report["clusters"][0]["cluster"] == "exec-auth"
    assert report["clusters"][0]["covered"] is True


def test_ledger_axis_parsing():
    """台帳 table-doc の axis を初出順・重複除去で抜く (script のパース関数)。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "graph_migration_coverage",
        os.path.join(os.path.dirname(__file__), "..", "scripts", "graph-migration-coverage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    content = (
        "---\nformat: table\n---\n# ledger\n\n```beacon-table\n"
        '{"columns":[{"key":"axis","label":"axis","type":"text"}],'
        '"rows":['
        '{"id":"r1","cells":{"axis":"exec-auth"}},'
        '{"id":"r2","cells":{"axis":"phase-cyclic"}},'
        '{"id":"r3","cells":{"axis":"exec-auth"}}]}'
        "\n```\n"
    )
    assert mod.ledger_cluster_axes(content) == ["exec-auth", "phase-cyclic"]
