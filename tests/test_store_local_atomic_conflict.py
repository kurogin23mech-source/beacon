"""Unit tests for the LocalStore stop-gap safety valves (ms-148 e-5410).

Local-mode ``.beacon/project.json`` writes used to (1) truncate-then-rewrite the
whole file, so a crash mid-write corrupted it, and (2) split the lock across
load and save, so two sessions doing read-modify-write silently lost each
other's changes. e-5410 is a *bridge* fix (the real fix is the SQLite store,
e-5411): it makes writes atomic and *detects* a concurrent overwrite instead of
silently clobbering it. These tests pin both valves.

Note the conflict baseline is class-level (keyed by absolute path) because the
CLI runs load and save on *separate* LocalStore instances — a fresh instance
must still see the baseline recorded at load time.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_local import LocalStore
from store_api import ConflictError


def _write(path, doc):
    path.write_text(json.dumps(doc), encoding="utf-8")


def _fresh_store(path):
    """A LocalStore with the class-level baseline cleared for this path, so
    tests don't leak state into each other (mirrors the cloud test isolation)."""
    LocalStore._save_baseline.pop(os.path.abspath(str(path)), None)
    return LocalStore(str(path))


# --- (a) atomic replace -----------------------------------------------------

def test_save_produces_valid_complete_file(tmp_path):
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "v0", "milestones": []})
    s = _fresh_store(p)
    data = s.load_project()
    data["summary"] = "mine"
    s.save_project(data)
    on_disk = json.loads(p.read_text())
    assert on_disk["summary"] == "mine"


def test_save_leaves_no_temp_files_behind(tmp_path):
    """The atomic temp file must be consumed by the rename, not left in .beacon/."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "", "milestones": []})
    s = _fresh_store(p)
    data = s.load_project()
    data["summary"] = "x"
    s.save_project(data)
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"orphan temp files left behind: {leftovers}"


def test_original_survives_when_replace_fails(tmp_path, monkeypatch):
    """If the atomic swap fails mid-write, the original file is untouched (never
    truncated) and the temp is cleaned up — the crash-safety guarantee, problem
    3. The whole point of temp-file + os.replace is that the original is only
    ever replaced by a fully-written file."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "original", "milestones": []})
    s = _fresh_store(p)
    data = s.load_project()
    data["summary"] = "would-be-new"

    import local_writer

    def boom(*a, **k):
        raise RuntimeError("crash during rename")

    monkeypatch.setattr(local_writer.os, "replace", boom)
    with pytest.raises(RuntimeError):
        s.save_project(data)

    # Original content intact and still valid JSON.
    assert json.loads(p.read_text())["summary"] == "original"
    # And the orphan temp was cleaned up.
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


# --- (b) conflict detection -------------------------------------------------

def test_save_after_clean_load_succeeds(tmp_path):
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "v0", "milestones": []})
    s = _fresh_store(p)
    data = s.load_project()
    data["summary"] = "mine"
    s.save_project(data)  # no concurrent change -> goes through
    assert json.loads(p.read_text())["summary"] == "mine"


def test_save_detects_concurrent_overwrite(tmp_path):
    """Another *process* overwrites the file after our load. save must refuse
    and NOT clobber their change (detection, not rescue).

    A separate process is modelled by writing the file bytes directly rather
    than via a second LocalStore.save_project(): in real usage each process has
    its own (class-level) baseline dict, so the loader's baseline stays at v0.
    Reusing the same in-process class dict would let the "other" save overwrite
    the loader's baseline and mask the conflict, which is a test artefact, not
    the cross-process behaviour."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "v0", "milestones": []})
    loader = _fresh_store(p)
    data = loader.load_project()

    # A different session commits v1 between our load and our save.
    _write(p, {"name": "p", "summary": "v1-by-other", "milestones": []})

    # Our in-memory copy is now stale. Saving it would erase v1-by-other.
    data["summary"] = "v0-mine"
    with pytest.raises(ConflictError):
        loader.save_project(data)

    # The other writer's change survived; ours was dropped, not merged.
    assert json.loads(p.read_text())["summary"] == "v1-by-other"


def test_save_without_prior_load_writes(tmp_path):
    """No baseline recorded (no load this invocation) -> no false positive."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "seed", "milestones": []})
    s = _fresh_store(p)
    s.save_project({"name": "p", "summary": "blind", "milestones": []})
    assert json.loads(p.read_text())["summary"] == "blind"


def test_baseline_refreshes_after_save(tmp_path):
    """A successful save updates the baseline so a follow-up save still works."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "v0", "milestones": []})
    s = _fresh_store(p)
    data = s.load_project()
    data["summary"] = "first"
    s.save_project(data)
    data["summary"] = "second"
    s.save_project(data)  # must not raise — baseline tracked our own write
    assert json.loads(p.read_text())["summary"] == "second"


def test_baseline_keyed_by_absolute_path_across_instances(tmp_path):
    """load on one instance, save on another (as the CLI does) must still see
    the baseline — even when the two reference the file by different strings."""
    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "v0", "milestones": []})

    LocalStore._save_baseline.pop(os.path.abspath(str(p)), None)
    # Loader uses an absolute path.
    loader = LocalStore(os.path.abspath(str(p)))
    data = loader.load_project()

    # A concurrent process bumps the file (direct byte write; see the
    # cross-process note in test_save_detects_concurrent_overwrite).
    _write(p, {"name": "p", "summary": "v1", "milestones": []})

    # Saver uses a relative path (cwd == tmp_path) but must resolve to the same
    # baseline key and detect the conflict.
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        saver = LocalStore("project.json")
        data["summary"] = "mine"
        with pytest.raises(ConflictError):
            saver.save_project(data)
    finally:
        os.chdir(cwd)


# --- both write paths share one lock (the reason for unifying, e-5410) -------

def test_both_write_paths_share_one_lock_no_lost_update(tmp_path, monkeypatch):
    """A path #1 write (LocalStore.save_project) and a path #2 write
    (apply_operation -> _apply_local) must serialise on the same stable lock and
    write atomically, so neither loses the other's change.

    Before unification the two paths locked *different* objects once one used an
    atomic os.replace, so a path #2 writer holding an fd on the pre-replace inode
    would silently write to an orphaned inode. This pins that they now cohere:
    apply_operation re-reads under the shared lock, so it sees a preceding
    save_project's committed change and appends to it rather than clobbering it.
    """
    import operations

    p = tmp_path / "project.json"
    _write(p, {"name": "p", "summary": "seed", "milestones": []})
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(p))
    # Force the local backend: another test in the same process may have left
    # store_router / firestore_client in sys.modules, which _detect_backend()
    # reads as "cloud". The env override is the intended test hook.
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

    # Path #1: a whole-document overwrite that adds ms-1.
    LocalStore._save_baseline.pop(os.path.abspath(str(p)), None)
    s = LocalStore(str(p))
    data = s.load_project()
    data["milestones"].append({"id": "ms-1", "title": "A", "status": "todo",
                               "entries": []})
    s.save_project(data)

    # Path #2: apply_operation re-reads fresh under the shared lock and adds ms-2.
    def add_b(d):
        d.setdefault("milestones", []).append(
            {"id": "ms-2", "title": "B", "status": "todo", "entries": []})
        return d, "ok"

    operations.apply_operation("p-test", add_b, project_file=str(p))

    # Both survive: path #2 did not clobber path #1's milestone.
    on_disk = json.loads(p.read_text())
    ids = {m["id"] for m in on_disk["milestones"]}
    assert ids == {"ms-1", "ms-2"}, (
        f"cross-path lost update: expected both ms-1 and ms-2, got {ids}")
    # And no orphan temp files from either atomic write.
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []
