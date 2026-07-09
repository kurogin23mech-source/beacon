"""Regression guard for e-2997: the app-server WebSocket client must lift the
inbound frame size cap above the websockets default (2**20 = 1 MiB).

A resumed Codex TUI thread streams its full state back in one frame that grows
with the conversation. With the library default, once the thread passes ~1 MiB
the receive path closes with 1009 (message too big) and the DM is delivered but
never wakes the visible thread. This test pins the raised cap so the fix can't
silently regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = REPO_ROOT / "plugins" / "beacon" / "scripts"


def _load_client():
    if str(CLIENT_DIR) not in sys.path:
        sys.path.insert(0, str(CLIENT_DIR))
    pytest.importorskip("websockets.sync.client")
    import beacon_codex_app_server_client as ac  # noqa: E402

    return ac


def test_remote_connect_raises_max_size(monkeypatch):
    ac = _load_client()

    captured: dict = {}

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()  # dummy ws handle; start_app_server just stores it

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)

    handle = ac.start_app_server(remote_url="ws://127.0.0.1:12345")

    assert handle.ws is not None
    # The default websockets cap is 2**20 (1 MiB); the fix must exceed it.
    assert captured["kwargs"].get("max_size", 2 ** 20) > 2 ** 20
    assert captured["kwargs"]["max_size"] == ac._WS_MAX_SIZE
    # ping_interval must stay disabled (pre-existing keepalive behavior).
    assert captured["kwargs"].get("ping_interval", "unset") is None


def test_remote_connect_missing_websockets_raises_actionable(monkeypatch):
    # e-2536: when `websockets` is absent (fresh install under the system
    # python), the remote wake path must fail with a specific, actionable error
    # instead of a generic ImportError that the caller swallows into a silent
    # pull-only degradation.
    ac = _load_client()

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("websockets"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        ac.start_app_server(remote_url="ws://127.0.0.1:12345")

    msg = str(excinfo.value)
    assert "websockets" in msg
    assert "pip install websockets" in msg
    # The message must name the silent failure mode so the reader understands
    # what breaks, not just what to install.
    assert "pull-only" in msg
