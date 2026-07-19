"""ms-109 e-3591 incident (2026-07-19) regression: the server startup ensures
the MySQL schema so a schema addition can't outrun its DDL on the VPS deploy
(git pull → restart, no create_mysql_tables step)."""

from __future__ import annotations

import asyncio
import os
import sys

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app  # noqa: E402


def test_startup_hook_noop_for_non_mysql(monkeypatch):
    called = {"n": 0}
    import mysql_client
    monkeypatch.setattr(mysql_client, "create_mysql_tables",
                        lambda: called.__setitem__("n", called["n"] + 1) or 0)
    monkeypatch.setenv("BEACON_STORE_BACKEND", "firestore")
    asyncio.run(app._ensure_mysql_schema())
    assert called["n"] == 0  # not called for firestore backend


def test_startup_hook_ensures_schema_for_mysql(monkeypatch):
    called = {"n": 0}
    import mysql_client
    monkeypatch.setattr(mysql_client, "create_mysql_tables",
                        lambda: called.__setitem__("n", called["n"] + 1) or 3)
    monkeypatch.setenv("BEACON_STORE_BACKEND", "mysql")
    asyncio.run(app._ensure_mysql_schema())
    assert called["n"] == 1  # create_mysql_tables invoked once


def test_startup_hook_non_fatal_on_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    import mysql_client
    monkeypatch.setattr(mysql_client, "create_mysql_tables", boom)
    monkeypatch.setenv("BEACON_STORE_BACKEND", "mysql")
    # must not raise — a boot-time DB blip should not crash the service
    asyncio.run(app._ensure_mysql_schema())
