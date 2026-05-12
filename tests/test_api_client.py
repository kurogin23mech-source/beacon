"""Unit tests for api_client.py and store_api.py."""

import sys
import os
import json
import http.server
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from api_client import ApiClient
from store_api import StoreApi


# ---------------------------------------------------------------------------
# Mock HTTP server
# ---------------------------------------------------------------------------

_mock_data = {}


class MockHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/projects":
            self._respond(200, list(_mock_data.values()))
        elif self.path.startswith("/api/projects/"):
            pid = self.path.split("/")[3]
            if pid in _mock_data:
                self._respond(200, _mock_data[pid])
            else:
                self._respond(404, {"detail": "Not found"})
        else:
            self._respond(404, {"detail": "Not found"})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/api/projects/"):
            pid = self.path.split("/")[3]
            _mock_data[pid] = body
            self._respond(200, {"status": "ok", "project_id": pid})
        else:
            self._respond(404, {"detail": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # suppress logs


_server = None
_base_url = ""


def setup_module():
    global _server, _base_url
    _server = http.server.HTTPServer(("127.0.0.1", 0), MockHandler)
    port = _server.server_address[1]
    _base_url = f"http://127.0.0.1:{port}"
    threading.Thread(target=_server.serve_forever, daemon=True).start()


def teardown_module():
    if _server:
        _server.shutdown()


def setup_function():
    _mock_data.clear()


import pytest

@pytest.fixture(autouse=True)
def _clear_data():
    _mock_data.clear()


# ---------------------------------------------------------------------------
# ApiClient tests
# ---------------------------------------------------------------------------

class TestApiClient:
    def test_list_empty(self):
        client = ApiClient(_base_url)
        assert client.list_projects() == []

    def test_put_and_get(self):
        client = ApiClient(_base_url)
        project = {"name": "Test", "milestones": []}
        client.put_project("test-1", project)
        result = client.get_project("test-1")
        assert result["name"] == "Test"

    def test_get_not_found(self):
        client = ApiClient(_base_url)
        try:
            client.get_project("nonexistent")
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "404" in str(e)

    def test_list_projects(self):
        _mock_data["p1"] = {"name": "P1", "milestones": []}
        _mock_data["p2"] = {"name": "P2", "milestones": []}
        client = ApiClient(_base_url)
        projects = client.list_projects()
        assert len(projects) == 2


# ---------------------------------------------------------------------------
# StoreApi tests
# ---------------------------------------------------------------------------

class TestStoreApi:
    def test_save_and_load(self):
        store = StoreApi(_base_url, "store-test")
        project = {"name": "Store Test", "milestones": []}
        store.save_project(project)
        result = store.load_project()
        assert result["name"] == "Store Test"

    def test_has_changed_initial(self):
        _mock_data["change-test"] = {"name": "X", "milestones": []}
        store = StoreApi(_base_url, "change-test")
        assert store.has_changed() is True
        assert store.has_changed() is False

    def test_has_changed_after_external_update(self):
        _mock_data["change-test2"] = {"name": "X", "milestones": []}
        store = StoreApi(_base_url, "change-test2")
        store.load_project()
        _mock_data["change-test2"] = {"name": "Y", "milestones": []}
        assert store.has_changed() is True

    def test_is_cloud(self):
        store = StoreApi(_base_url, "test")
        assert store.is_cloud() is True


# ---------------------------------------------------------------------------
# Connection error tests
# ---------------------------------------------------------------------------

class TestConnectionError:
    def test_api_client_connection_refused(self):
        client = ApiClient("http://127.0.0.1:1")  # nothing listening
        with pytest.raises(ConnectionError):
            client.get("/api/projects")

    def test_store_api_has_changed_offline(self):
        store = StoreApi("http://127.0.0.1:1", "test")
        # Should not crash, just return False
        assert store.has_changed() is False
