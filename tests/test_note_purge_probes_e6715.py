"""`beacon note purge-probes` clears the probe garbage already in the store
(ms-160 e-6715).

The read-only gate stops NEW probe notes; this verb removes the backlog an
unguarded audit left behind (17 of the 48 notes in the live project store).

What is pinned here is mostly about what it must NOT take with it:
  * a note that merely MENTIONS the sentinel is a human note (this task's own
    handoff notes do) — exact match only, never a substring sweep;
  * it is a dry-run until --confirm, because the cloud leg is clear + re-post;
  * no backup ⇒ no delete, and the backup path is NOT the one `note clear`
    uses, or running one would destroy the other's only recovery route;
  * a failed cloud re-post is reported and exits non-zero, never counted as
    restored (the ms-178 e-6656 defect, which must not be reintroduced here).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
sys.path.insert(0, str(ROOT / "lib"))

import cli_surface  # noqa: E402
import cmd_note  # noqa: E402

SENTINEL = cli_surface.SURFACE_PROBE_SENTINEL

ROWS = [
    {"ts": "2026-10-01T10:00:00+0900", "text": "real handoff"},
    {"ts": "2026-10-01T10:01:00+0900", "text": SENTINEL},
    {"ts": "2026-10-01T10:02:00+0900", "text": "見つけ方: text が %s のメモ" % SENTINEL},
    {"ts": "2026-10-01T10:03:00+0900", "text": "  %s  " % SENTINEL},
]


@pytest.fixture
def project(tmp_path):
    """A local-mode project (no cloud.json) holding 2 probe notes, 1 real note
    and 1 real note that quotes the sentinel in its prose."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    notes = beacon / "session_notes.jsonl"
    notes.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ROWS),
        encoding="utf-8")
    return tmp_path, notes


def _run(cwd, *args):
    return subprocess.run([str(BIN), "note", "purge-probes", *args],
                          cwd=str(cwd), capture_output=True, text=True)


def _texts(notes_file):
    return [json.loads(l)["text"]
            for l in notes_file.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- the selection rule ----------------------------------------------------

def test_prose_mentioning_the_sentinel_is_kept(project):
    """A substring sweep would delete a human note that documents the probe —
    including the handoff notes written while fixing this very task."""
    cwd, notes = project
    r = _run(cwd, "--confirm")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _texts(notes) == ["real handoff", ROWS[2]["text"]], _texts(notes)


def test_surrounding_whitespace_still_counts_as_a_probe(project):
    """Row 4 is the sentinel with padding; it is garbage, not prose."""
    cwd, notes = project
    _run(cwd, "--confirm")
    assert ROWS[3]["text"] not in _texts(notes)


def test_is_probe_note_is_exact_not_substring():
    assert cmd_note._is_probe_note({"text": SENTINEL})
    assert cmd_note._is_probe_note({"text": " %s\n" % SENTINEL})
    assert not cmd_note._is_probe_note({"text": "about %s here" % SENTINEL})
    assert not cmd_note._is_probe_note({"text": ""})
    assert not cmd_note._is_probe_note({})


# --- the confirmation contract ---------------------------------------------

def test_dry_run_is_the_default(project):
    cwd, notes = project
    before = notes.read_text(encoding="utf-8")
    r = _run(cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "下見" in r.stdout, r.stdout
    assert notes.read_text(encoding="utf-8") == before, "dry run modified the store"
    assert not (cwd / ".beacon" / "session_notes.purge.bak").exists()


def test_nothing_to_do_says_so_and_writes_no_backup(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    (beacon / "session_notes.jsonl").write_text(
        json.dumps({"ts": "t", "text": "only real notes"}) + "\n", encoding="utf-8")
    r = _run(tmp_path, "--confirm")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "見つかりませんでした" in r.stdout, r.stdout
    assert not (beacon / "session_notes.purge.bak").exists()


# --- the backup contract ---------------------------------------------------

def test_backup_holds_everything_including_what_was_kept(project):
    cwd, notes = project
    _run(cwd, "--confirm")
    bak = cwd / ".beacon" / "session_notes.purge.bak"
    assert bak.exists()
    saved = [json.loads(l) for l in bak.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [n["text"] for n in saved] == [r["text"] for r in ROWS], saved


def test_backup_path_does_not_collide_with_note_clear(project):
    """`note restore` recovers from .bak / .cloud.bak. If purge wrote there it
    would silently destroy the recovery route for a previous clear."""
    cwd, _ = project
    assert cmd_note._purge_backup_path() != cmd_note._get_notes_path() + ".bak"
    assert cmd_note._purge_backup_path() != cmd_note._cloud_backup_path()
    _run(cwd, "--confirm")
    assert not (cwd / ".beacon" / "session_notes.jsonl.bak").exists()
    assert not (cwd / ".beacon" / "session_notes.cloud.bak").exists()


# --- cloud failure modes ---------------------------------------------------

def _cloud_project(tmp_path, monkeypatch, fetch, push=None, clear=None):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text('{"name":"t","milestones":[]}', encoding="utf-8")
    (beacon / "session_notes.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ROWS), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cmd_note, "_cloud_project_id", lambda: "proj-1")
    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", fetch)
    if push is not None:
        monkeypatch.setattr(cmd_note, "_push_note_to_cloud_or_error", push)
    if clear is not None:
        monkeypatch.setattr(cmd_note, "_note_api_client", clear)
    monkeypatch.setenv("BEACON_NOTE_PURGE_CONFIRM", "1")
    return tmp_path


def test_unreadable_cloud_aborts_both_legs(tmp_path, monkeypatch, capsys):
    """Same rule as note clear: a store we cannot read is a store we cannot
    back up, so nothing is deleted anywhere."""
    cwd = _cloud_project(tmp_path, monkeypatch,
                         fetch=lambda pid: ([], "ConnectionError: down"))
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_purge_probes()
    assert exc.value.code == 1
    notes = cwd / ".beacon" / "session_notes.jsonl"
    assert len(notes.read_text(encoding="utf-8").strip().splitlines()) == len(ROWS)
    assert not (cwd / ".beacon" / "session_notes.purge.bak").exists()


def test_failed_repost_is_reported_not_counted_as_restored(tmp_path, monkeypatch, capsys):
    """ms-178 e-6656 undid exactly this on `note restore`: counting the attempt
    reports a recovery that never happened, on the one path with no backup
    behind it. It must not come back here."""
    cloud_rows = [dict(r) for r in ROWS]
    _cloud_project(
        tmp_path, monkeypatch,
        fetch=lambda pid: (cloud_rows, ""),
        push=lambda note: "HTTPError: 503",
        clear=lambda: (type("C", (), {"clear_notes": lambda self, pid: None})(), ""),
    )
    with pytest.raises(SystemExit) as exc:
        cmd_note.cmd_note_purge_probes()
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "cloud に戻したメモ: 0 / 2 件" in out.out, out.out
    assert "再投稿に 2 件失敗" in out.err, out.err
