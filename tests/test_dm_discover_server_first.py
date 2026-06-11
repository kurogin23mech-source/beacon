"""Tests for dm_discover server-first refactor (ms-62 / e-1502).

The refactor adds a server-first path: ``GET /api/me/projects`` -> per-project
``bus directory`` aggregation. Falls back to the legacy ps+lsof local
discovery on server failure (under ``BEACON_DISCOVERY_SOURCE=auto``).

Also exercises the invocation-scoped memo (= one HTTP per Python process).
"""

import os
import sys

import pytest

# Make beacon_cli importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from beacon_cli.skills_helpers import dm_discover


@pytest.fixture(autouse=True)
def _reset():
    """Reset module-level memo + env between tests."""
    dm_discover._reset_memo_for_tests()
    original_source = os.environ.pop("BEACON_DISCOVERY_SOURCE", None)
    yield
    dm_discover._reset_memo_for_tests()
    if original_source is not None:
        os.environ["BEACON_DISCOVERY_SOURCE"] = original_source
    else:
        os.environ.pop("BEACON_DISCOVERY_SOURCE", None)


def test_discovery_source_defaults_to_auto():
    assert dm_discover._discovery_source() == "auto"


def test_discovery_source_respects_env(monkeypatch):
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "server")
    assert dm_discover._discovery_source() == "server"
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "local")
    assert dm_discover._discovery_source() == "local"


def test_discovery_source_falls_back_to_auto_on_garbage(monkeypatch):
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "garbage-value")
    assert dm_discover._discovery_source() == "auto"


def test_query_me_projects_memoises(monkeypatch):
    """First call hits the network mock; second call returns memo."""
    call_count = {"n": 0}

    class _FakeClient:
        def me_list_projects(self):
            call_count["n"] += 1
            return [{"id": "proj-a", "name": "A", "role": "owner"}]

    def _fake_get_api_client():
        return _FakeClient(), {}

    # Monkey-patch by injecting into sys.modules so the lazy import works.
    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _fake_get_api_client
    monkeypatch.setitem(sys.modules, "commands", fake_mod)

    r1 = dm_discover.query_me_projects()
    r2 = dm_discover.query_me_projects()
    assert r1 == r2
    assert r1[0]["id"] == "proj-a"
    assert call_count["n"] == 1  # second call hit the memo


def test_discover_project_ids_via_server(monkeypatch):
    class _FakeClient:
        def me_list_projects(self):
            return [
                {"id": "proj-a", "name": "A", "role": "owner"},
                {"id": "proj-b", "name": "B", "role": "editor"},
                {"id": "", "name": "junk", "role": "x"},  # filtered
                {"id": "proj-a", "name": "dup", "role": "owner"},  # deduped
            ]

    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = lambda: (_FakeClient(), {})
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    ids = dm_discover.discover_project_ids_via_server()
    assert ids == ["proj-a", "proj-b"]


def test_discover_and_aggregate_server_first(monkeypatch):
    """Server returns projects → aggregate stamps project_name + project_role."""
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "server")

    class _FakeClient:
        def me_list_projects(self):
            return [
                {"id": "proj-a", "name": "Alpha", "role": "owner"},
                {"id": "proj-b", "name": "Beta", "role": "editor"},
            ]

    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = lambda: (_FakeClient(), {})
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    # Pretend each project has one session.
    monkeypatch.setattr(
        dm_discover, "query_project_directory",
        lambda pid, **kw: [{"session_id": f"sv-{pid}-1", "actor": {"email": "x"}}],
    )
    rows = dm_discover.discover_and_aggregate(healthy=True, since_min=5)
    assert len(rows) == 2
    by_pid = {r["project_id"]: r for r in rows}
    assert by_pid["proj-a"]["project_name"] == "Alpha"
    assert by_pid["proj-a"]["project_role"] == "owner"
    assert by_pid["proj-b"]["project_name"] == "Beta"


def test_discover_and_aggregate_auto_falls_back_on_server_failure(monkeypatch):
    """auto mode: server fails → silently fall back to local discovery."""
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "auto")

    def _fail_get_api_client():
        raise RuntimeError("API error 404: Not Found")

    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _fail_get_api_client
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    monkeypatch.setattr(
        dm_discover, "discover_local_project_ids", lambda: ["proj-fallback"]
    )
    monkeypatch.setattr(
        dm_discover, "query_project_directory",
        lambda pid, **kw: [{"session_id": "sv-local-1"}],
    )
    rows = dm_discover.discover_and_aggregate(healthy=True, since_min=5)
    assert len(rows) == 1
    assert rows[0]["project_id"] == "proj-fallback"


def test_discover_and_aggregate_strict_server_reraises(monkeypatch):
    """server mode: failure raises (no fallback)."""
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "server")

    def _fail_get_api_client():
        raise ConnectionError("Network unreachable")

    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _fail_get_api_client
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    with pytest.raises((RuntimeError, ConnectionError)):
        dm_discover.discover_and_aggregate(healthy=True, since_min=5)


def test_discover_and_aggregate_local_only(monkeypatch):
    """local mode: skips server entirely."""
    monkeypatch.setenv("BEACON_DISCOVERY_SOURCE", "local")

    def _should_not_be_called():
        raise AssertionError("server path should not be taken in local mode")

    import types
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _should_not_be_called
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    monkeypatch.setattr(
        dm_discover, "discover_local_project_ids", lambda: ["proj-local"]
    )
    monkeypatch.setattr(
        dm_discover, "query_project_directory",
        lambda pid, **kw: [{"session_id": "sv-local-1"}],
    )
    rows = dm_discover.discover_and_aggregate(healthy=True, since_min=5)
    assert len(rows) == 1
    assert rows[0]["project_id"] == "proj-local"
