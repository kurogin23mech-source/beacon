"""`note list` reads BOTH stores in cloud mode (ms-178 e-6655).

Notes are WRITTEN to two places (local .beacon/session_notes.jsonl + the cloud
`notes` subcollection) but were only ever READ back from the local file. A fork
worktree has its own .beacon/, so notes the parent session wrote were invisible
there and read as "no notes exist" — an assignment was missed for a day, and the
2026-09-28 note loss had the same root: the two stores disagreed about truth.

Pinned here:
  - cloud mode merges both stores, deduped on (ts, text, session_id) — the same
    note written by this CLI must appear ONCE, not twice.
  - local mode is untouched (no cloud call, no behaviour change).
  - a note only in the cloud is reachable AND marked as another session's, so a
    fork does not read the parent's notes as its own (AC4).
  - a cloud read FAILURE is surfaced, never silently rendered as "empty". That
    conflation is the actual defect: "I could not read" must not print as
    "there is nothing". The write path may stay silent (the note is on disk);
    the read path may not (it would invent absence).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cmd_note  # noqa: E402


def _write_local(beacon_dir, *notes):
    (beacon_dir / "session_notes.jsonl").write_text(
        "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes),
        encoding="utf-8",
    )


MINE = {"ts": "2026-09-29T10:00:00+0900", "text": "mine", "session_id": "sv-child"}
PARENT = {"ts": "2026-09-29T09:00:00+0900", "text": "parent handoff",
          "session_id": "sv-parent"}


@pytest.fixture
def local_only(tmp_path, monkeypatch):
    """A local-mode project: project.json but NO cloud.json."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(json.dumps({"name": "t", "milestones": []}))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.delenv("BEACON_JSON", raising=False)
    return beacon_dir


def _stub_cloud(monkeypatch, notes, error=""):
    calls = []

    def _fake(project_id):
        calls.append(project_id)
        return list(notes), error

    monkeypatch.setattr(cmd_note, "_fetch_cloud_notes", _fake)
    return calls


# --- local mode is unchanged ------------------------------------------------

def test_local_mode_does_not_call_cloud(local_only, monkeypatch, capsys):
    """No cloud.json = no cloud call at all (offline / local projects must not
    pay a network round trip, and must not warn about a cloud they don't use)."""
    _write_local(local_only, MINE)
    calls = _stub_cloud(monkeypatch, [PARENT])  # would leak in if called
    cmd_note.cmd_note_list()
    out = capsys.readouterr()
    assert calls == [], "local mode reached for cloud notes"
    assert "mine" in out.out
    assert "parent handoff" not in out.out
    assert out.err == ""


# --- cloud mode merges both stores -----------------------------------------

def test_cloud_mode_surfaces_parent_note(fake_cloud_config, monkeypatch, capsys):
    """The named breakage: a note written by ANOTHER session (the parent) is
    readable from this working directory."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [PARENT, MINE])
    monkeypatch.delenv("BEACON_JSON", raising=False)
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    assert "parent handoff" in out, out
    assert "(他セッション)" in out, "parent's note was not marked as another session's"


def test_same_note_in_both_stores_is_not_duplicated(fake_cloud_config, monkeypatch, capsys):
    """A note this CLI wrote exists in BOTH stores (write is two-legged), so the
    merge must join on (ts, text, session_id) and show it once."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [MINE])
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_note.cmd_note_list()
    notes = json.loads(capsys.readouterr().out)
    assert len(notes) == 1, notes
    assert notes[0]["origin"] == "both", notes


def test_json_origin_tags_each_note(fake_cloud_config, monkeypatch, capsys):
    """Machine consumers (session-start / session-end read --json) need to tell
    their own notes from another session's without parsing display strings."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [PARENT])
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_note.cmd_note_list()
    notes = json.loads(capsys.readouterr().out)
    by_text = {n["text"]: n["origin"] for n in notes}
    assert by_text == {"mine": "local", "parent handoff": "cloud"}, by_text


def test_merged_notes_are_time_ordered(fake_cloud_config, monkeypatch, capsys):
    """Interleaving two stores must not scramble the timeline — the parent's
    09:00 note precedes this session's 10:00 note."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [PARENT])
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_note.cmd_note_list()
    notes = json.loads(capsys.readouterr().out)
    assert [n["text"] for n in notes] == ["parent handoff", "mine"]


def test_empty_local_says_notes_are_from_elsewhere(fake_cloud_config, monkeypatch, capsys):
    """AC4: a fork worktree with nothing of its own must not read the parent's
    notes as its own — the output states whose they are."""
    _stub_cloud(monkeypatch, [PARENT])
    monkeypatch.delenv("BEACON_JSON", raising=False)
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    assert "この作業フォルダのメモはありません" in out, out
    assert "parent handoff" in out


# --- the core AX contract: unreadable != empty -----------------------------

def test_cloud_failure_is_surfaced_not_rendered_as_empty(fake_cloud_config, monkeypatch, capsys):
    """The defect being fixed is a CONFLATION. If the cloud cannot be read, the
    output must say so; printing a bare "(メモなし)" is how a whole day's
    assignment got missed."""
    _stub_cloud(monkeypatch, [], error="ConnectionError: boom")
    monkeypatch.delenv("BEACON_JSON", raising=False)
    cmd_note.cmd_note_list()
    out = capsys.readouterr()
    assert "cloud のメモを取得できませんでした" in out.err, out.err
    assert "存在しない、とは判断できません" in out.err, out.err


def test_cloud_failure_warns_even_in_json_mode(fake_cloud_config, monkeypatch, capsys):
    """JSON consumers must see the warning too (on stderr, so stdout stays
    parseable) — otherwise a Skill reading --json silently believes the list."""
    _write_local(fake_cloud_config, MINE)
    _stub_cloud(monkeypatch, [], error="HTTPError: 500")
    monkeypatch.setenv("BEACON_JSON", "1")
    cmd_note.cmd_note_list()
    out = capsys.readouterr()
    assert json.loads(out.out) == [dict(MINE, origin="local")]
    assert "取得できませんでした" in out.err


def test_genuinely_empty_both_stores_is_stated_as_such(fake_cloud_config, monkeypatch, capsys):
    """The other side of the conflation: when both stores really were checked
    and are empty, say that — so "empty" carries evidence, not assumption."""
    _stub_cloud(monkeypatch, [])
    monkeypatch.delenv("BEACON_JSON", raising=False)
    cmd_note.cmd_note_list()
    out = capsys.readouterr().out
    assert "cloud も空です" in out, out


# --- the dedup key itself ---------------------------------------------------

def test_note_key_joins_on_ts_text_session():
    """Pin the join key: the cloud copy carries no stable id of its own (the
    Firestore document id is not stored in the document), so ts+text+session_id
    is what makes the two legs of a two-legged write recognisable as one note."""
    assert cmd_note._note_key(MINE) == cmd_note._note_key(dict(MINE))
    assert cmd_note._note_key(MINE) != cmd_note._note_key(PARENT)
    # a note from a different session with identical text is NOT the same note
    assert cmd_note._note_key(MINE) != cmd_note._note_key(
        dict(MINE, session_id="sv-other"))


def test_local_read_tolerates_corrupt_lines(local_only):
    """A half-written JSONL line must not take the whole list down (notes are
    append-only and a session can die mid-write)."""
    (local_only / "session_notes.jsonl").write_text(
        json.dumps(MINE) + "\n{broken\n" + json.dumps(PARENT) + "\n", encoding="utf-8")
    got = cmd_note._read_local_notes(str(local_only / "session_notes.jsonl"))
    assert [n["text"] for n in got] == ["mine", "parent handoff"]
