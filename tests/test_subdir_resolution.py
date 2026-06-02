"""Project-root resolution from subdirectories (e-862).

A beacon command issued from a subdirectory used to fail with
"No .beacon/project.json found" because both the bash dispatcher and the
Python dispatch only looked in CWD. These tests cover the Python side
(``beacon_cli/dispatch.py``): walk up like git, then chdir to the project
root before running the handler. The bash ``bin/beacon`` mirrors the same
contract via ``find_beacon_root`` + ``cd``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch  # noqa: E402  (sys.path mutated)


@pytest.fixture
def project_tree(tmp_path):
    """A project root with .beacon/ and a nested dataset/raw subdirectory."""
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "t", "milestones": []}', encoding="utf-8"
    )
    nested = tmp_path / "dataset" / "raw"
    nested.mkdir(parents=True)
    return tmp_path, nested


# ---------------------------------------------------------------------------
# _find_beacon_root
# ---------------------------------------------------------------------------


def test_find_beacon_root_from_nested(project_tree):
    root, nested = project_tree
    assert dispatch._find_beacon_root(nested) == root.resolve()


def test_find_beacon_root_from_root(project_tree):
    root, _ = project_tree
    assert dispatch._find_beacon_root(root) == root.resolve()


def test_find_beacon_root_none_when_absent(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert dispatch._find_beacon_root(sub) is None


# ---------------------------------------------------------------------------
# _relocate_to_project_root
# ---------------------------------------------------------------------------


def test_relocate_chdirs_from_subdir(project_tree, monkeypatch):
    root, nested = project_tree
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    monkeypatch.chdir(nested)
    dispatch._relocate_to_project_root("status")
    assert Path.cwd().resolve() == root.resolve()


def test_relocate_noop_when_at_root(project_tree, monkeypatch):
    root, _ = project_tree
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    monkeypatch.chdir(root)
    dispatch._relocate_to_project_root("status")
    assert Path.cwd().resolve() == root.resolve()


@pytest.mark.parametrize("cmd", ["init", "setup"])
def test_relocate_exempts_init_setup(project_tree, monkeypatch, cmd):
    """init/setup operate on the current directory — never relocate them."""
    root, nested = project_tree
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    monkeypatch.chdir(nested)
    dispatch._relocate_to_project_root(cmd)
    assert Path.cwd().resolve() == nested.resolve()


def test_relocate_respects_explicit_project_file(project_tree, monkeypatch, tmp_path):
    """An explicit BEACON_PROJECT_FILE pointing at a real file wins."""
    _root, nested = project_tree
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(other))
    monkeypatch.chdir(nested)
    dispatch._relocate_to_project_root("status")
    assert Path.cwd().resolve() == nested.resolve()


def test_relocate_noop_when_no_project_anywhere(tmp_path, monkeypatch):
    sub = tmp_path / "x"
    sub.mkdir()
    monkeypatch.delenv("BEACON_PROJECT_FILE", raising=False)
    monkeypatch.chdir(sub)
    dispatch._relocate_to_project_root("status")
    assert Path.cwd().resolve() == sub.resolve()
