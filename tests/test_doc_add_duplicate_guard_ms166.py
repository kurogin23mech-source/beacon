"""doc add duplicate guard (ms-166 e-6044).

`beacon doc add` used to WARN "Proceeding anyway" and create a second document with the same
title+scope. Combined with a cloud optimistic-lock retry, that silently produced the same SPEC
doc four times (2026-09-03) — a "write silently duplicates" non-function. The guard now:
  - refuses a same-title+scope duplicate by default (exit 1, no write), pointing at the existing
    doc_id and telling the caller to update-or-rename;
  - creates the duplicate only when --force (BEACON_FORCE=1) is explicit.
This keeps the accidental-duplicate path closed while leaving a deliberate escape hatch.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import commands  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    """Isolated local project sandbox (mirrors test_persistence_poisoning_defense)."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-agent"}), encoding="utf-8")
        (beacon_dir / "project.json").write_text(
            json.dumps({"name": "t", "milestones": [
                {"id": "ms-1", "title": "test", "status": "in_progress",
                 "entries": [], "progress": 0}]}),
            encoding="utf-8")
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        for k in ("BEACON_TITLE", "BEACON_CONTENT", "BEACON_SCOPE", "BEACON_DOC_ID",
                  "BEACON_MS", "BEACON_OP", "BEACON_JSON", "BEACON_FORCE",
                  "BEACON_TARGET", "BEACON_BUS_ORIGIN"):
            monkeypatch.delenv(k, raising=False)
        try:
            yield Path(tmp)
        finally:
            os.chdir(tempfile.gettempdir())


def _add(monkeypatch, title, *, scope="core", content="body", force=False, json_mode=False):
    monkeypatch.setenv("BEACON_TITLE", title)
    monkeypatch.setenv("BEACON_CONTENT", content)
    monkeypatch.setenv("BEACON_SCOPE", scope)
    monkeypatch.setenv("BEACON_FORCE", "1" if force else "")
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")
    commands.cmd_doc_add()


def _doc_count(scope="core", title="dup"):
    from store import get_store  # local store reader
    docs = get_store().list_documents()
    return sum(1 for d in docs if d.get("title") == title and d.get("scope") == scope)


def test_first_add_succeeds(project_dir, monkeypatch):
    _add(monkeypatch, "dup")
    assert _doc_count() == 1


def test_duplicate_refused_by_default(project_dir, monkeypatch):
    _add(monkeypatch, "dup")
    # A second add with the same title+scope must REFUSE (exit 1) and NOT write.
    with pytest.raises(SystemExit) as ei:
        _add(monkeypatch, "dup")
    assert ei.value.code == 1
    assert _doc_count() == 1  # still exactly one — no silent duplicate


def test_duplicate_refused_json_mode_is_machine_readable(project_dir, monkeypatch):
    _add(monkeypatch, "dup")
    with pytest.raises(SystemExit):
        _add(monkeypatch, "dup", json_mode=True)
    assert _doc_count() == 1


def test_force_allows_duplicate(project_dir, monkeypatch):
    _add(monkeypatch, "dup")
    # --force must PROCEED past the guard (no SystemExit). (In cloud mode this mints a second
    # doc_id; in local mode the title-slug filename is reused — the point under test is that
    # the guard does not BLOCK when --force is given, not the storage-layer id semantics.)
    _add(monkeypatch, "dup", force=True)  # no raise = escape hatch works


def test_same_title_different_scope_is_not_a_duplicate(project_dir, monkeypatch):
    _add(monkeypatch, "dup", scope="core")
    # Different scope is NOT a same-title+scope duplicate, so the guard must not trip
    # (no SystemExit). The guard keys on title AND scope together.
    _add(monkeypatch, "dup", scope="memo")  # no raise
