"""ms-109 e-3591 段2 — the v3 atomic I/O path (get/save/apply/replace) switched
to the registry-driven generic decomposition, exercised WITHOUT a live MySQL.

A tiny in-memory fake connection interprets the handful of SQL shapes
mysql_client emits (SELECT data / SELECT sk,data / SELECT sk / INSERT … ON
DUPLICATE KEY UPDATE / DELETE). Monkeypatching ``mysql_client._conn`` to it lets
get_project_v3 / save_project_v3 / apply_project_op_v3 / replace_project_v3 run
their real code end-to-end, so this is integration-level coverage of the write
path (the planner _v3_plan_writes is also unit-tested directly).

Pins: dev (milestones/entries) behaves exactly as before — only milestones/
entries rows are written, no opportunities/accounts drift; sales opportunities/
accounts + their fat arms land in their own rows and round-trip; targeted writes
only touch changed rows; deletes remove gone rows.
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


# ---------------------------------------------------------------------------
# in-memory fake MySQL connection
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, store, log):
        self.store = store          # {(table, pk, sk): data_json_str}
        self.log = log              # list of ("insert"|"delete", table, sk)
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        table = re.search(r"`([^`]+)`", s).group(1)
        if s.startswith("SELECT data FROM"):
            # the apply lock query uses a literal sk='' (only pk is a param)
            pk = params[0]
            sk = params[1] if "sk=%s" in s else ""
            v = self.store.get((table, pk, sk))
            self._result = [{"data": v}] if v is not None else []
        elif s.startswith("SELECT sk, data FROM"):
            pk = params[0]
            self._result = [{"sk": k[2], "data": v}
                            for k, v in self.store.items()
                            if k[0] == table and k[1] == pk]
        elif s.startswith("SELECT sk FROM"):
            pk = params[0]
            self._result = [{"sk": k[2]} for k in self.store
                            if k[0] == table and k[1] == pk]
        elif s.startswith("INSERT INTO"):
            pk, sk, data = params
            self.store[(table, pk, sk)] = data
            self.log.append(("insert", table, sk))
        elif s.startswith("DELETE FROM"):
            pk, sk = params[0], params[1]
            self.store.pop((table, pk, sk), None)
            self.log.append(("delete", table, sk))
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
    def __init__(self, store, log):
        self.store = store
        self.log = log

    def cursor(self):
        return _FakeCursor(self.store, self.log)

    def autocommit(self, _v):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    store: dict = {}
    log: list = []
    monkeypatch.setattr(mc, "_conn", lambda: _FakeConn(store, log))
    return {"store": store, "log": log}


def _tables_touched(store):
    """Return the set of (entity-suffix) tables that hold at least one row."""
    return {k[0].rsplit("_", 1)[-1] for k in store}


# ---------------------------------------------------------------------------
# dev — unchanged behaviour (only milestones/entries rows)
# ---------------------------------------------------------------------------

def _dev():
    return {
        "project_id": "p1", "name": "Dev", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "First", "status": "in_progress",
             "entries": [
                 {"id": "e-1", "type": "task", "status": "todo",
                  "created_at": "2026-05-01T00:00:00Z", "description": "t"}]},
        ],
    }


def test_dev_save_writes_only_milestone_entry_tables(fake_db):
    mc.save_project_v3("p1", _dev())
    touched = _tables_touched(fake_db["store"])
    assert "milestones" in touched and "entries" in touched
    # no sales tables written for a dev project
    assert "opportunities" not in touched
    assert "communications" not in touched


def test_dev_round_trip_shape_has_no_sales_keys(fake_db):
    mc.save_project_v3("p1", _dev())
    got = mc.get_project_v3("p1")
    assert [m["id"] for m in got["milestones"]] == ["ms-1"]
    assert got["milestones"][0]["entries"][0]["id"] == "e-1"
    # dev hydrated shape must NOT gain empty opportunities/accounts
    assert "opportunities" not in got
    assert "accounts" not in got


def test_apply_targeted_write_only_touches_changed_rows(fake_db):
    mc.save_project_v3("p1", _dev())
    fake_db["log"].clear()

    def op(data):
        data["milestones"][0]["entries"].append(
            {"id": "e-2", "type": "commit", "status": "done",
             "created_at": "2026-05-02T00:00:00Z", "description": "c"})
        return data, {"ok": True}

    res = mc.apply_project_op_v3("p1", op)
    assert res == {"ok": True}
    inserts = [x for x in fake_db["log"] if x[0] == "insert"]
    # projects (anchor) + the ms-1 row (child count changed) + the new e-2 row.
    inserted_sks = {(t.rsplit("_", 1)[-1], sk) for _, t, sk in inserts}
    assert ("entries", "ms-1#e-2") in inserted_sks
    # e-1 was unchanged → not re-written
    assert ("entries", "ms-1#e-1") not in inserted_sks


# ---------------------------------------------------------------------------
# sales — opportunities/accounts + fat arms land in their own rows
# ---------------------------------------------------------------------------

def _sales():
    return {
        "project_id": "s1", "name": "Sales", "profession": "sales",
        "milestones": [],  # validate_project requires the key
        "opportunities": [
            {"id": "opp-1", "title": "Acme", "phase": "lead", "status": "open",
             "gates": [{"phase": "lead", "at": "T0"}],
             "activities": [
                 {"id": "act-1", "description": "面談", "status": "todo",
                  "created_at": "2026-07-01T00:00:00Z",
                  "communications": [
                      {"id": "comm-1", "linked_id": "act-1", "summary": "x"}]}],
             "communications": [
                 {"id": "comm-2", "linked_id": "", "summary": "cold",
                  "created_at": "2026-07-02T00:00:00Z"}]},
        ],
        "accounts": [
            {"id": "acc-1", "name": "Acme", "phase": "リード",
             "contacts": [{"name": "T"}], "nurturings": [
                 {"id": "nrt-1", "description": "挨拶",
                  "created_at": "2026-07-01T00:00:00Z"}],
             "communications": []},
        ],
    }


def test_sales_save_splits_targets_into_rows(fake_db):
    mc.save_project_v3("s1", _sales())
    store = fake_db["store"]
    # opportunity / account each their own row (sk = target id)
    assert any(k[2] == "opp-1" for k in store if k[0].endswith("opportunities"))
    assert any(k[2] == "acc-1" for k in store if k[0].endswith("accounts"))
    # fat-arm children arm-qualified
    assert any(k[2] == "opp-1#activities#act-1"
               for k in store if k[0].endswith("activities"))
    assert any(k[2] == "opp-1#communications#comm-2"
               for k in store if k[0].endswith("communications"))
    assert any(k[2] == "acc-1#nurturings#nrt-1"
               for k in store if k[0].endswith("nurturings"))
    # the projects meta row must NOT contain the fat opportunities array
    meta = json.loads(store[(mc._table_name("projects"), "s1", "")])
    assert "opportunities" not in meta
    assert "accounts" not in meta


def test_sales_round_trip(fake_db):
    mc.save_project_v3("s1", _sales())
    got = mc.get_project_v3("s1")
    opp = got["opportunities"][0]
    assert opp["id"] == "opp-1"
    assert [a["id"] for a in opp["activities"]] == ["act-1"]
    assert [c["id"] for c in opp["communications"]] == ["comm-2"]
    assert opp["gates"][0]["phase"] == "lead"
    # communication nested under the activity rode inline
    assert opp["activities"][0]["communications"][0]["id"] == "comm-1"
    assert got["accounts"][0]["nurturings"][0]["id"] == "nrt-1"
    assert got["milestones"] == []  # required key preserved


def test_sales_apply_adds_communication_as_own_row(fake_db):
    mc.save_project_v3("s1", _sales())
    fake_db["log"].clear()

    def op(data):
        data["opportunities"][0]["communications"].append(
            {"id": "comm-9", "linked_id": "", "summary": "follow-up",
             "created_at": "2026-07-05T00:00:00Z"})
        return data, "ok"

    mc.apply_project_op_v3("s1", op)
    inserted = {(t.rsplit("_", 1)[-1], sk)
                for kind, t, sk in fake_db["log"] if kind == "insert"}
    assert ("communications", "opp-1#communications#comm-9") in inserted
    # unchanged opp-level comm-2 not re-written
    assert ("communications", "opp-1#communications#comm-2") not in inserted


def test_replace_deletes_removed_rows(fake_db):
    mc.save_project_v3("s1", _sales())
    # replace with a version that drops the opp-level communication comm-2
    new = _sales()
    new["opportunities"][0]["communications"] = []
    mc.replace_project_v3("s1", new)
    got = mc.get_project_v3("s1")
    assert got["opportunities"][0]["communications"] == []
    # the row is physically gone
    assert not any(k[2] == "opp-1#communications#comm-2"
                   for k in fake_db["store"] if k[0].endswith("communications"))


# ---------------------------------------------------------------------------
# pure planner
# ---------------------------------------------------------------------------

def test_plan_writes_upserts_changed_and_deletes_gone():
    before = {
        "milestones": {"ms-1": {"id": "ms-1", "title": "A"}},
        "entries": {"ms-1#e-1": {"id": "e-1", "description": "old"}},
    }
    new_data = {
        "name": "p", "milestones": [
            {"id": "ms-1", "title": "A", "entries": [
                {"id": "e-1", "description": "NEW"}]}]}  # e-1 changed
    meta, upserts, deletes = mc._v3_plan_writes(before, new_data)
    assert "ms-1#e-1" in upserts.get("entries", {})   # changed → upsert
    assert "milestones" not in upserts                # ms-1 unchanged → skip
    assert meta["schema_version"] == mc.SCHEMA_V3_ENTRY


def test_plan_writes_deletes_removed_child():
    before = {
        "milestones": {"ms-1": {"id": "ms-1", "title": "A"}},
        "entries": {"ms-1#e-1": {"id": "e-1"}, "ms-1#e-2": {"id": "e-2"}},
    }
    new_data = {"name": "p", "milestones": [
        {"id": "ms-1", "title": "A", "entries": [{"id": "e-1"}]}]}  # e-2 gone
    _meta, _up, deletes = mc._v3_plan_writes(before, new_data)
    assert "ms-1#e-2" in deletes.get("entries", [])


# ---------------------------------------------------------------------------
# rollout safety: un-migrated (inline-in-meta) sales data reads through, and the
# first write splits it into rows (write-through migration)
# ---------------------------------------------------------------------------

def _unmigrated_meta():
    return {"project_id": "s2", "name": "Old", "profession": "sales",
            "milestones": [],
            "opportunities": [
                {"id": "opp-7", "title": "Legacy",
                 "activities": [{"id": "act-9", "description": "x",
                                 "status": "todo", "created_at": "T"}],
                 "communications": []}]}


def test_unmigrated_sales_reads_through_fallback(fake_db):
    store = fake_db["store"]
    store[(mc._table_name("projects"), "s2", "")] = mc._dumps(_unmigrated_meta())
    # no opportunity/activity rows exist yet — must NOT lose the inline data
    got = mc.get_project_v3("s2")
    assert got["opportunities"][0]["id"] == "opp-7"
    assert got["opportunities"][0]["activities"][0]["id"] == "act-9"


def test_first_write_migrates_inline_to_rows(fake_db):
    store = fake_db["store"]
    store[(mc._table_name("projects"), "s2", "")] = mc._dumps(_unmigrated_meta())

    def op(data):
        data["opportunities"][0]["title"] = "Migrated"
        return data, "ok"

    mc.apply_project_op_v3("s2", op)
    # opportunity + its activity now have their own rows
    assert any(k[2] == "opp-7" for k in store if k[0].endswith("opportunities"))
    assert any(k[2] == "opp-7#activities#act-9"
               for k in store if k[0].endswith("activities"))
    # projects meta no longer carries opportunities inline
    new_meta = json.loads(store[(mc._table_name("projects"), "s2", "")])
    assert "opportunities" not in new_meta
    # re-read is consistent and reflects the mutation
    got = mc.get_project_v3("s2")
    assert got["opportunities"][0]["title"] == "Migrated"


# ---------------------------------------------------------------------------
# ms-157 e-5747 — the server decomposition is DESCRIPTOR-driven, not seed-only.
# A descriptor-defined target-class (a collection the built-in seed has never
# heard of) splits into its own rows the moment its table exists — proving the
# live v3 path reads occupation.target_decomposition(data), not the static seed.
# And with no table yet (pre-DDL / e-5750) it rides inline safely (A-stage).
# ---------------------------------------------------------------------------

# A back-office target-class the built-in dev/sales seed does NOT declare.
_CONTRACT_DESC = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
}


def _descriptor_project():
    return {
        "project_id": "b1", "name": "BackOffice", "profession": "backoffice",
        "milestones": [],  # validate/assemble always-emit key
        "target_classes": [_CONTRACT_DESC],
        "contracts": [
            {"id": "ctr-1", "title": "NDA", "status": "open",
             "created_at": "2026-08-01T00:00:00Z",
             "clauses": [
                 {"id": "cl-1", "text": "秘密保持",
                  "created_at": "2026-08-01T00:00:00Z"}]},
        ],
    }


def test_descriptor_class_splits_into_rows_when_table_exists(fake_db, monkeypatch):
    # Simulate the DDL (e-5750) having created the descriptor's tables.
    monkeypatch.setitem(mc.TABLES, "contracts", "beacon_prod_contracts")
    monkeypatch.setitem(mc.TABLES, "clauses", "beacon_prod_clauses")

    mc.save_project_v3("b1", _descriptor_project())
    store = fake_db["store"]
    # The Target row (sk = ctr-1) and its fat-arm child (arm-qualified sk) each
    # land in their OWN table — impossible unless the code read the descriptor,
    # since the built-in seed knows nothing about "contracts"/"clauses".
    assert any(k[2] == "ctr-1" for k in store if k[0].endswith("contracts"))
    # single-arm collection → 2-segment child sk (same D2 rule as milestones)
    assert any(k[2] == "ctr-1#cl-1"
               for k in store if k[0].endswith("clauses"))
    # meta must NOT carry the fat contracts array inline (it was decomposed).
    meta = json.loads(store[(mc._table_name("projects"), "b1", "")])
    assert "contracts" not in meta
    # round-trips back to the unified shape
    got = mc.get_project_v3("b1")
    assert got["contracts"][0]["id"] == "ctr-1"
    assert got["contracts"][0]["clauses"][0]["id"] == "cl-1"


def test_descriptor_class_rides_inline_when_no_table(fake_db):
    # No contracts/clauses table exists (pre-DDL). The materialized guard must
    # keep the class inline in meta rather than KeyError on a missing table.
    assert "contracts" not in mc.TABLES  # precondition: seed/DDL unaware
    mc.save_project_v3("b1", _descriptor_project())
    store = fake_db["store"]
    meta = json.loads(store[(mc._table_name("projects"), "b1", "")])
    # rode inline: contracts stayed in the projects meta row, no own rows
    assert [c["id"] for c in meta.get("contracts", [])] == ["ctr-1"]
    # read-through returns it intact (data never lost)
    got = mc.get_project_v3("b1")
    assert got["contracts"][0]["clauses"][0]["id"] == "cl-1"
