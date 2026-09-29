"""`note clear` backs up BOTH stores before deleting (ms-178 e-6656).

The local file was always moved to `.bak`, but the cloud notes were deleted
outright. That asymmetry manufactured a false belief: ".bak exists, therefore
the notes are recovered" — and a cloud-only note really did stay lost after a
restore was reported as complete.

The guarantee pinned here is an ORDERING, not a best-effort attempt:
  snapshot cloud → snapshot local → delete.
If the cloud snapshot cannot be taken, NOTHING is deleted (neither leg), because
clearing local while the cloud survives leaves the two stores disagreeing about
what happened — the same class of split-truth bug as e-6655.

A backup nobody can restore from is not a backup, so `note restore` is a real
verb. It is additive and idempotent: notes already present are matched by
`_note_key` and skipped, so running it twice never duplicates.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_note  # noqa: E402

MINE = {"ts": "2026-09-29T10:00:00+0900", "text": "mine", "session_id": "sv-child"}
PARENT = {"ts": "2026-09-29T09:00:00+0900", "text": "parent handoff",
          "session_id": "sv-parent"}


def _write_local(beacon_dir, *notes):
    (beacon_dir / "session_notes.jsonl").write_text(
        "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes), encoding="utf-8")


@pytest.fixture(autouse=True)
def _confirmed(monkeypatch):
    """These tests are about the backup, not the e-6654 confirmation gate."""
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    monkeypatch.delenv("BEACON_JSON", raising=False)


@pytest.fixture
def local_only(tmp_path, monkeypatch):
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(json.dumps({"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    return beacon_dir


def _stub_cloud(monkeypatch, notes, error="", clear_raises=None):
    """Stub both cloud legs: the snapshot read and the destructive delete."""
    state = {"cleared": False, "pushed": []}

    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: (list(notes), error))
    monkeypatch.setattr(cmd_note, "_push_note_to_cloud",
                        lambda n: state["pushed"].append(n))

    class _Client:
        def __init__(self, *a, **k):
            pass

        def clear_notes(self, project_id):
            if clear_raises:
                raise clear_raises
            state["cleared"] = True

    import api_client
    monkeypatch.setattr(api_client, "ApiClient", _Client)
    import auth
    monkeypatch.setattr(auth, "load_credentials", lambda: {"id_token": "t"})
    return state


# --- the cloud snapshot exists and matches the pre-clear content ------------

def test_clear_snapshots_cloud_before_deleting(fake_cloud_config, monkeypatch, capsys):
    """AC: a cloud backup is taken, and it holds what the cloud had at clear
    time (not a subset, not the local view)."""
    _write_local(fake_cloud_config, MINE)
    state = _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    assert state["cleared"], "cloud delete never ran"
    backup = cmd_note._cloud_backup_path()
    saved = cmd_note._read_local_notes(backup)
    assert [n["text"] for n in saved] == ["mine", "parent handoff"], saved


def test_both_backups_hold_the_pre_clear_content(fake_cloud_config, monkeypatch, capsys):
    """AC: local .bak and the cloud snapshot BOTH carry the content as of the
    moment before the clear — that is what makes "recovered" checkable."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    local_bak = cmd_note._get_notes_path() + ".bak"
    assert [n["text"] for n in cmd_note._read_local_notes(local_bak)] == ["mine"]
    assert {n["text"] for n in
            cmd_note._read_local_notes(cmd_note._cloud_backup_path())} == {"mine", "parent handoff"}


# --- no backup ⇒ no delete (the ordering guarantee) -------------------------

def test_unreadable_cloud_aborts_without_deleting_anything(fake_cloud_config, monkeypatch, capsys):
    """If the cloud snapshot cannot be taken, BOTH legs are left alone. Clearing
    local here would destroy the only readable copy while the cloud survives."""
    _write_local(fake_cloud_config, MINE)
    state = _stub_cloud(monkeypatch, [], error="ConnectionError: down")
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_clear()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "何も削除していません" in err, err
    assert not state["cleared"], "cloud was deleted despite having no backup"
    assert os.path.exists(cmd_note._get_notes_path()), "local was cleared anyway"
    assert not os.path.exists(cmd_note._get_notes_path() + ".bak")


def test_failed_cloud_delete_is_reported_not_swallowed(fake_cloud_config, monkeypatch, capsys):
    """The old code caught every exception and still printed "cleared", so a
    failed cloud delete silently diverged the stores. It must say so."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE], clear_raises=RuntimeError("500"))
    cmd_note.cmd_note_clear()
    out = capsys.readouterr()
    assert "cloud のメモを削除できませんでした" in out.err, out.err
    assert "cloud 側は残っています" in out.err


def test_local_mode_clear_needs_no_cloud_snapshot(local_only, monkeypatch, capsys):
    """A local-only project has no second store, so it must not be blocked by
    (or wait on) a cloud snapshot."""
    _write_local(local_only, MINE)
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: pytest.fail("local mode reached for cloud"))
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    assert not os.path.exists(cmd_note._get_notes_path())
    assert os.path.exists(cmd_note._get_notes_path() + ".bak")
    assert not os.path.exists(cmd_note._cloud_backup_path())


# --- restore actually restores ---------------------------------------------

def test_restore_round_trip_local(local_only, monkeypatch, capsys):
    """AC: 退避から復元できる — the notes come back."""
    _write_local(local_only, MINE, PARENT)
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    cmd_note.cmd_note_restore()
    capsys.readouterr()
    got = cmd_note._read_local_notes(cmd_note._get_notes_path())
    assert {n["text"] for n in got} == {"mine", "parent handoff"}


def test_restore_reposts_cloud_only_notes(fake_cloud_config, monkeypatch, capsys):
    """The cloud leg must be restored too — that is the leg that had no backup
    and stayed lost. Only what the cloud is MISSING gets re-posted."""
    _write_local(fake_cloud_config, MINE)
    state = _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    # after the clear the cloud is empty; restore should re-post both
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))
    cmd_note.cmd_note_restore()
    capsys.readouterr()
    assert {n["text"] for n in state["pushed"]} == {"mine", "parent handoff"}, state["pushed"]


def test_restore_is_idempotent(local_only, capsys):
    """Running restore twice must not duplicate (a recovery path people will
    retry when unsure whether the first one worked)."""
    _write_local(local_only, MINE, PARENT)
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    cmd_note.cmd_note_restore()
    cmd_note.cmd_note_restore()
    capsys.readouterr()
    got = cmd_note._read_local_notes(cmd_note._get_notes_path())
    assert len(got) == 2, got


def test_restore_does_not_write_the_origin_view_field(fake_cloud_config, monkeypatch, capsys):
    """`origin` is a provenance tag the READ path adds for display; re-posting it
    would persist a view concern into the store."""
    _write_local(fake_cloud_config, MINE)
    state = _stub_cloud(monkeypatch, [dict(PARENT, origin="cloud")])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))
    cmd_note.cmd_note_restore()
    capsys.readouterr()
    assert all("origin" not in n for n in state["pushed"]), state["pushed"]


def test_restore_without_backups_refuses(local_only, capsys):
    """Nothing to restore is an error, not a silent success — otherwise "復元
    しました" would be printed when nothing happened."""
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_restore()
    assert exc.value.code == 1
    assert "復元できる退避がありません" in capsys.readouterr().err


def test_restore_keeps_cloud_backup_when_cloud_unreachable(fake_cloud_config, monkeypatch, capsys):
    """If the cloud can't be read during restore, the snapshot must be KEPT and
    the user told to retry — discarding it would lose the only copy."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes",
                        lambda pid: ([], "ConnectionError: down"))
    cmd_note.cmd_note_restore()
    err = capsys.readouterr().err
    assert "cloud への復元は行いません" in err, err
    assert os.path.exists(cmd_note._cloud_backup_path()), "snapshot was discarded"


def test_cloud_backup_path_sits_beside_the_local_one(local_only):
    """Both legs recover from one place; pin the naming so a human looking for
    the backup finds both files together."""
    notes = cmd_note._get_notes_path()
    assert cmd_note._cloud_backup_path() == notes.replace(".jsonl", "") + ".cloud.bak"
    assert os.path.dirname(cmd_note._cloud_backup_path()) == os.path.dirname(notes)
