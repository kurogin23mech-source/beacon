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
    """These tests are about the backup, not the e-6654 confirmation gate.

    ms-160 e-6714 scoped `note clear`'s DEFAULT to the calling session's own
    notes. What this file pins — snapshot both stores before deleting, abort
    when the snapshot cannot be taken, report a failed cloud delete instead of
    swallowing it, round-trip through `note restore` — is the contract for
    clearing the SHARED store, which is now spelled `--all`. The contract is
    unchanged; only the verb that reaches it is. Set here rather than per test
    so the file keeps testing one thing.
    """
    monkeypatch.setenv("BEACON_NOTE_CLEAR_YES", "1")
    monkeypatch.setenv("BEACON_NOTE_CLEAR_ALL", "1")
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

    def _push_ok(n):
        state["pushed"].append(n)
        return ""  # "" == success, per _push_note_to_cloud_or_error's contract

    monkeypatch.setattr(cmd_note, "_push_note_to_cloud_or_error", _push_ok)

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


# --- independent review findings (PR #766) ---------------------------------
#
# AX + maintainability judges BOTH flagged the same defect independently: the
# restore path counted an ATTEMPTED cloud re-post as a restored one, because
# `_push_note_to_cloud` swallows every exception and cannot report failure. On
# the one path with no backup behind it, that turns a silent write failure into
# an invented success. No test covered it — the only cloud-restore test stubbed
# the push with an always-succeeding lambda.

def _stub_failing_push(monkeypatch, error="ConnectionError: down"):
    calls = []

    def _fake(note):
        calls.append(note)
        return error

    monkeypatch.setattr(cmd_note, "_push_note_to_cloud_or_error", _fake)
    return calls


def test_restore_does_not_count_a_failed_cloud_repost(fake_cloud_config, monkeypatch, capsys):
    """A re-post that failed must NOT be reported as restored."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))
    attempted = _stub_failing_push(monkeypatch)
    cmd_note.cmd_note_restore()
    out = capsys.readouterr()
    assert attempted, "restore never tried to re-post"
    assert "cloud 0 件" in out.out, (
        "a failed re-post was counted as restored: " + out.out)
    assert "cloud への再投稿に" in out.err, out.err


def test_restore_keeps_backup_after_a_failed_repost(fake_cloud_config, monkeypatch, capsys):
    """The snapshot must survive a failed re-post — it is the only copy left, so
    discarding it would make the failure unrecoverable."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))
    _stub_failing_push(monkeypatch)
    cmd_note.cmd_note_restore()
    capsys.readouterr()
    assert os.path.exists(cmd_note._cloud_backup_path())


def test_restore_partial_success_counts_only_what_landed(fake_cloud_config, monkeypatch, capsys):
    """Mixed outcome: one note lands, one fails. The count must be the landed
    one, not the attempted two."""
    _write_local(fake_cloud_config)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    cmd_note.cmd_note_clear()
    capsys.readouterr()
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", lambda pid: ([], ""))

    def _half(note):
        return "" if note.get("text") == "mine" else "HTTPError: 500"

    monkeypatch.setattr(cmd_note, "_push_note_to_cloud_or_error", _half)
    cmd_note.cmd_note_restore()
    out = capsys.readouterr()
    assert "cloud 1 件" in out.out, out.out
    assert "1 件失敗" in out.err, out.err


def test_note_add_push_stays_silent_on_failure(fake_cloud_config, monkeypatch, capsys):
    """The asymmetry is deliberate and must be preserved: `note add` keeps the
    best-effort silence (the note is on local disk, so nothing is lost), while
    only the recovery path reports. A future edit that makes add noisy — or
    restore silent — breaks one half of the contract."""
    monkeypatch.setattr(cmd_note, "_note_api_client",
                        lambda: (None, "ConnectionError: down"))
    monkeypatch.setenv("BEACON_NOTE_TEXT", "hello")
    cmd_note.cmd_note_add()
    out = capsys.readouterr()
    assert "ConnectionError" not in out.err, (
        "note add surfaced a push failure; it is best-effort by design: " + out.err)
    assert cmd_note._read_local_notes(cmd_note._get_notes_path()), "note not stored"


def test_refusal_sizes_the_shared_cloud_store(fake_cloud_config, monkeypatch, capsys):
    """AX: the confirmation gate must state how many OTHER sessions' notes are at
    stake. "some others exist" cannot distinguish 0 from 200."""
    monkeypatch.delenv("BEACON_NOTE_CLEAR_YES", raising=False)
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE, PARENT])
    with pytest.raises(SystemExit):
        cmd_note.cmd_note_clear()
    err = capsys.readouterr().err
    assert "他セッション分 1 件" in err, err


def test_refusal_says_count_unknown_when_cloud_unreadable(fake_cloud_config, monkeypatch, capsys):
    """...and when the size cannot be fetched, say so rather than implying zero."""
    monkeypatch.delenv("BEACON_NOTE_CLEAR_YES", raising=False)
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [], error="ConnectionError: down")
    with pytest.raises(SystemExit):
        cmd_note.cmd_note_clear()
    err = capsys.readouterr().err
    assert "件数不明" in err, err


def test_one_api_client_factory_is_shared_by_all_three_legs(monkeypatch, fake_cloud_config):
    """Maintainability: the credential + ApiClient construction existed in three
    near-verbatim copies. Pin that they now route through ONE factory, so an
    auth/transport change lands in one place instead of drifting."""
    calls = []
    monkeypatch.setattr(cmd_note, "_note_api_client",
                        lambda: (calls.append("x"), (None, "stubbed"))[1])
    cmd_note._fetch_cloud_notes("proj-1")
    cmd_note._push_note_to_cloud_or_error({"ts": "t", "text": "x"})
    assert len(calls) == 2, (
        "a cloud leg bypassed the shared factory (re-introduced a copy)")
