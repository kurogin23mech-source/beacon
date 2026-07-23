"""ms-96 — regression: `_scan` must stamp the PK into list rows so that
Firestore-migrated projects (whose ``data`` blob lacks ``project_id``) are not
silently dropped from cross-project surfaces.

Root cause (found 2026-07-23 while diagnosing "TrailNode session invisible in
`beacon bus directory`"): ``mysql_client._scan`` did ``SELECT data`` only and
``list_projects`` read the id via ``item.get("project_id")`` — i.e. from the
``data`` blob, NOT the row PK. Firestore stores the id as ``doc.id`` and never
puts it inside ``data`` (``firestore_client.save_project`` = ``.set(data)``), so
migrated rows produced ``project_id == ""``. Downstream the cross-project
directory (``/api/me/sessions``) does ``if not pid: continue`` and dropped every
migrated project — invisible in the session directory, the DM recipient picker,
and the disclosure boundary truth source (``list_projects``).

Fix: ``_scan(entity, id_field=...)`` stamps the PK into ``data[id_field]`` so the
id comes from the authoritative PK column (parity with
``firestore_client.list_projects`` using ``doc.id``).
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import mysql_client as mc  # noqa: E402


class _FakeCursor:
    def __init__(self, store):
        self.store = store          # {(table, pk, sk): data_json_str}
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        table = re.search(r"`([^`]+)`", s).group(1)
        if "WHERE" in s:
            # single-row get: SELECT data FROM `t` WHERE pk=%s AND sk=%s
            pk = params[0]
            sk = params[1] if len(params) > 1 else ""
            v = self.store.get((table, pk, sk))
            self._result = [{"data": v}] if v is not None else []
        elif s.startswith("SELECT pk, data FROM"):
            # whole-table scan carrying the PK (the fix)
            self._result = [{"pk": k[1], "data": v}
                            for k, v in self.store.items() if k[0] == table]
        elif s.startswith("SELECT data FROM"):
            # whole-table scan, data only (legacy _scan without id_field)
            self._result = [{"data": v}
                            for k, v in self.store.items() if k[0] == table]
        return len(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def autocommit(self, _v):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


OWNER = "100953901118675629504"


@pytest.fixture
def fake_db(monkeypatch):
    store: dict = {}
    ptable = mc._table_name("projects")
    # Firestore-migrated row: data blob has NO project_id (the id lived in
    # doc.id, which the migration did not fold into data).
    store[(ptable, "trailnode-cd652b", "")] = json.dumps({
        "name": "TrailNode", "owner": OWNER,
        "members": [{"user_id": "113315684036322209061", "role": "editor"}],
    })
    # MySQL-native row: save_project injected project_id into data.
    store[(ptable, "beacon-b95643", "")] = json.dumps({
        "project_id": "beacon-b95643", "name": "Beacon", "owner": OWNER,
        "members": [],
    })
    monkeypatch.setattr(mc, "_conn", lambda: _FakeConn(store))
    return store


def test_migrated_project_gets_id_from_pk(fake_db):
    rows = mc.list_projects(user_id=OWNER, include_archived=True)
    by_id = {r["project_id"]: r for r in rows}
    # The migrated project must surface under its real id, NOT "".
    assert "trailnode-cd652b" in by_id
    assert by_id["trailnode-cd652b"]["name"] == "TrailNode"
    # No blank-id row leaks through.
    assert all(r["project_id"] for r in rows)


def test_native_project_id_preserved(fake_db):
    rows = mc.list_projects(user_id=OWNER, include_archived=True)
    by_id = {r["project_id"]: r for r in rows}
    assert by_id["beacon-b95643"]["project_id"] == "beacon-b95643"


def test_list_all_projects_also_carries_pk(fake_db):
    rows = mc.list_all_projects()
    ids = {r["project_id"] for r in rows}
    assert ids == {"trailnode-cd652b", "beacon-b95643"}


def test_scan_without_id_field_is_unchanged(fake_db):
    # Legacy behaviour: no id_field → migrated row still lacks project_id.
    # (Guards that the fix is opt-in and does not alter other _scan callers.)
    rows = mc._scan("projects")
    migrated = [r for r in rows if r.get("name") == "TrailNode"]
    assert migrated and "project_id" not in migrated[0]


def test_scan_with_id_field_stamps_pk(fake_db):
    rows = mc._scan("projects", id_field="project_id")
    migrated = [r for r in rows if r.get("name") == "TrailNode"]
    assert migrated and migrated[0]["project_id"] == "trailnode-cd652b"
