"""Tests for dynamic function-zoom of huge modules (ms-156 e-5543).

Covers lib/code_graph_zoom: parse a module's top-level functions/classes/methods
on demand from source, with per-symbol line spans, docstring first line, and the
other-module dependencies each symbol uses (both import styles). Hermetic.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import code_graph_zoom as zoom  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "store.py").write_text("def save(): pass\n")
    (tmp_path / "lib" / "auth.py").write_text("def token(): pass\n")
    (tmp_path / "lib" / "big.py").write_text(
        "import store\n"
        "from auth import token\n"
        "import os\n"
        '"""module doc."""\n'
        "\n"
        "def alpha():\n"
        '    """Alpha does X."""\n'
        "    return store.save()\n"
        "\n"
        "async def beta():\n"
        "    return token()\n"
        "\n"
        "def gamma():\n"
        "    return os.getpid()\n"
        "\n"
        "class Widget:\n"
        "    def method_a(self):\n"
        "        return store.save()\n"
    )
    return str(tmp_path)


def test_zoom_lists_top_level_symbols_with_spans(fake_repo):
    z = zoom.zoom_module(fake_repo, "lib/big.py")
    assert z["found"] is True
    names = [(s["name"], s["kind"]) for s in z["symbols"]]
    assert ("alpha", "function") in names
    assert ("beta", "async-function") in names
    assert ("gamma", "function") in names
    assert ("Widget", "class") in names
    assert ("Widget.method_a", "method") in names
    alpha = next(s for s in z["symbols"] if s["name"] == "alpha")
    assert alpha["lineno"] > 0 and alpha["end_lineno"] >= alpha["lineno"]
    assert alpha["doc"] == "Alpha does X."


def test_zoom_attributes_dependencies_per_function_both_import_styles(fake_repo):
    z = zoom.zoom_module(fake_repo, "lib/big.py")
    by = {s["name"]: s for s in z["symbols"]}
    # `import store` used in alpha → depends on lib/store.py
    assert by["alpha"]["depends_on"] == ["lib/store.py"]
    # `from auth import token` used in beta → depends on lib/auth.py (from-import style)
    assert by["beta"]["depends_on"] == ["lib/auth.py"]
    # gamma only uses os (external) → no in-graph deps
    assert by["gamma"]["depends_on"] == []
    # method attribution works too
    assert by["Widget.method_a"]["depends_on"] == ["lib/store.py"]


def test_zoom_is_dynamic_not_stored(fake_repo):
    """zoom はソースから毎回計算する。返り値に格納・table の痕跡は無い。"""
    z = zoom.zoom_module(fake_repo, "lib/big.py")
    assert "symbols" in z and "rows" not in z and "columns" not in z


def test_zoom_absent_or_non_python_returns_not_found(fake_repo):
    assert zoom.zoom_module(fake_repo, "lib/nope.py")["found"] is False
    (open(os.path.join(fake_repo, "lib", "notpy.txt"), "w")).write("x")
    assert zoom.zoom_module(fake_repo, "lib/notpy.txt")["found"] is False


def test_zoom_syntax_error_is_graceful(fake_repo):
    open(os.path.join(fake_repo, "lib", "broken.py"), "w").write("def (:\n")
    assert zoom.zoom_module(fake_repo, "lib/broken.py")["found"] is False
