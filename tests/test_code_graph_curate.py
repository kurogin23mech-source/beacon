"""Tests for the curated layer + reconcile-hook decision (ms-156 e-5542).

Covers lib/code_graph_curate (human-writable contract/role/guard_test on a node
row, append-only, refusing machine-layer cells) and lib/code_graph_reconcile
(decide whether a just-edited file should prompt a reconcile). Hermetic.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import code_graph as cg  # noqa: E402
import code_graph_curate as curate  # noqa: E402
import code_graph_reconcile as reconcile  # noqa: E402
import table_doc  # noqa: E402


def _node_table_with(module_id, **cells):
    g = cg.CodeGraph()
    g.add_node(cg.Node(id=module_id, path=module_id, seam="exec-auth",
                       governs="§2", **cells))
    return g.to_node_table()


# --- curated write path -----------------------------------------------------

def test_set_curated_writes_contract_and_role():
    table = _node_table_with("lib/auth.py")
    changed = curate.set_curated(
        table, "lib/auth.py",
        {"contract": "id_token を発行する", "role": "認証の綻び点"},
        actor="t", at="2026-01-01")
    assert set(changed) == {"contract", "role"}
    view = curate.curated_view(table, "lib/auth.py")
    assert view["contract"] == "id_token を発行する"
    assert view["role"] == "認証の綻び点"
    # machine-layer cells untouched
    assert view["seam"] == "exec-auth" and view["governs"] == "§2"


def test_set_curated_is_append_only_history():
    table = _node_table_with("lib/auth.py")
    curate.set_curated(table, "lib/auth.py", {"contract": "v1"}, actor="t", at="t1")
    curate.set_curated(table, "lib/auth.py", {"contract": "v2"}, actor="t", at="t2")
    row = table_doc.get_row(table, table_doc.active_rows(table)[0]["id"])
    contract_ops = [h for h in row["history"] if h.get("key") == "contract"]
    assert [h["new"] for h in contract_ops] == ["v1", "v2"]   # both preserved
    assert contract_ops[-1]["old"] == "v1"


def test_set_curated_same_value_is_noop():
    table = _node_table_with("lib/auth.py", contract="same")
    changed = curate.set_curated(table, "lib/auth.py", {"contract": "same"},
                                 actor="t", at="t1")
    assert changed == []   # unchanged → no history churn


def test_set_curated_refuses_machine_layer_cells():
    table = _node_table_with("lib/auth.py")
    with pytest.raises(curate.CurateError):
        curate.set_curated(table, "lib/auth.py", {"seam": "hacked"},
                           actor="t", at="t1")


def test_set_curated_unknown_module_raises():
    table = _node_table_with("lib/auth.py")
    with pytest.raises(curate.CurateError):
        curate.set_curated(table, "lib/nope.py", {"contract": "x"},
                           actor="t", at="t1")


# --- reconcile hook decision ------------------------------------------------

@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "lib" / "auth.py").write_text("pass\n")
    (tmp_path / "tests" / "test_auth.py").write_text("pass\n")
    (tmp_path / "README.md").write_text("# doc\n")
    return str(tmp_path)


def test_reminder_fires_for_module_edit(fake_repo):
    msg = reconcile.reminder_for_edit(os.path.join(fake_repo, "lib", "auth.py"), fake_repo)
    assert msg is not None
    assert "lib/auth.py" in msg
    assert "graph-curate" in msg   # points at the write path


def test_reminder_silent_for_non_module(fake_repo):
    # test files and docs are not graph modules → no prompt
    assert reconcile.reminder_for_edit(
        os.path.join(fake_repo, "tests", "test_auth.py"), fake_repo) is None
    assert reconcile.reminder_for_edit(
        os.path.join(fake_repo, "README.md"), fake_repo) is None


def test_reminder_silent_for_outside_repo(fake_repo):
    assert reconcile.reminder_for_edit("/etc/hosts", fake_repo) is None
    assert reconcile.reminder_for_edit("", fake_repo) is None
