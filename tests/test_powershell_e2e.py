"""End-to-end smoke tests for the PowerShell-native dispatch (ms-44 e-695).

Unlike ``test_powershell_dispatch.py`` (which patches ``subprocess.call``
to verify the env-var contract), these tests let the real
``python3 lib/commands.py`` subprocess run. That validates the full path:

    argv → dispatch.py → env vars → commands.py → .beacon/project.json

Each test runs in a fresh ``tmp_path`` with no bash available, mirroring
a Windows PowerShell user's first session.

Note on capture: dispatch shells out to a subprocess whose stdout goes
straight to fd 1, not Python's ``sys.stdout``. ``capfd`` (which captures
Python-level writes) won't see it. We use ``capfd`` (fd-level capture)
instead so the subprocess output is available to assertions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import main as main_mod  # noqa: E402


@pytest.fixture
def fresh_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    # Simulate Windows / PowerShell: no bash, no bin/beacon found.
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)
    yield tmp_path


def test_version_e2e(capfd, fresh_dir):
    rc = main_mod.main(["--version"])
    assert rc == 0
    out = capfd.readouterr().out
    assert "beacon 0." in out  # any 0.x version


def test_init_then_status_e2e(capfd, fresh_dir):
    """Full init → status loop must work with zero bash involvement."""
    rc = main_mod.main([
        "init",
        "--name", "smoke",
        "--objective", "Smoke test for PowerShell dispatch",
        "--retro-day", "monday",
    ])
    assert rc == 0
    pf = fresh_dir / ".beacon" / "project.json"
    assert pf.exists(), "init should have created .beacon/project.json"
    data = json.loads(pf.read_text())
    assert data["name"] == "smoke"
    assert data["objective"] == "Smoke test for PowerShell dispatch"

    capfd.readouterr()  # clear
    rc = main_mod.main(["status", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    parsed = json.loads(out)
    assert parsed["name"] == "smoke"
    assert parsed["milestones"] == []  # status before any milestones


def test_milestone_add_and_list_e2e(capfd, fresh_dir):
    main_mod.main([
        "init", "--name", "smoke", "--objective", "obj", "--retro-day", "monday",
    ])
    capfd.readouterr()

    rc = main_mod.main([
        "milestone", "add", "First MS",
        "--priority", "high",
        "--objective", "the goal",
        "--ac", "tests pass",
    ])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["status", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    data = json.loads(out)
    assert data["name"] == "smoke"
    assert len(data["milestones"]) == 1
    ms = data["milestones"][0]
    assert ms["title"] == "First MS"
    assert ms.get("priority") == "high"


def test_task_add_done_list_e2e(capfd, fresh_dir):
    main_mod.main([
        "init", "--name", "smoke", "--objective", "obj", "--retro-day", "monday",
    ])
    main_mod.main(["milestone", "add", "MS"])
    capfd.readouterr()

    # Find the MS id from status --json
    rc = main_mod.main(["status", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    data = json.loads(out)
    ms_id = data["milestones"][0]["id"]

    capfd.readouterr()
    rc = main_mod.main([
        "task", "add", "do the thing", "-m", ms_id,
        "--motivation", "because we must",
        "--ac", "thing is done",
    ])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["task", "list", "-m", ms_id, "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    parsed = json.loads(out)
    # task_list JSON shape: {milestone_id, milestone_title, entries: [...]}
    entries = parsed.get("entries") if isinstance(parsed, dict) else parsed
    assert isinstance(entries, list)
    assert len(entries) == 1
    task = entries[0]
    assert task["description"] == "do the thing"
    task_id = task["id"]

    capfd.readouterr()
    rc = main_mod.main(["task", "done", task_id, "-r", "done"])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["task", "list", "-m", ms_id, "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    parsed = json.loads(out)
    entries = parsed.get("entries") if isinstance(parsed, dict) else parsed
    assert entries[0]["status"] == "done"


def test_summary_e2e(capfd, fresh_dir):
    main_mod.main([
        "init", "--name", "smoke", "--objective", "obj", "--retro-day", "monday",
    ])
    capfd.readouterr()

    rc = main_mod.main(["summary", "new", "direction", "for", "smoke"])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["status", "--json"])
    out = capfd.readouterr().out
    data = json.loads(out)
    assert data["summary"] == "new direction for smoke"


def test_doc_add_list_show_e2e(capfd, fresh_dir):
    main_mod.main([
        "init", "--name", "smoke", "--objective", "obj", "--retro-day", "monday",
    ])
    # doc_add via save_entry requires an active milestone — create one.
    main_mod.main(["milestone", "add", "MS for docs"])
    capfd.readouterr()
    rc = main_mod.main(["status", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    ms_id = json.loads(out)["milestones"][0]["id"]
    main_mod.main(["milestone", "start", ms_id])
    capfd.readouterr()

    # Use markdown heading as content so commands.py derives the title
    # from the body (its established behavior — see _read_local_doc).
    rc = main_mod.main([
        "doc", "add", "Smoke Doc",
        "--scope", "memo",
        "--ms", ms_id,
        "--content", "# Smoke Doc\n\nhello body",
    ])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["doc", "list", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    docs = json.loads(out)
    assert isinstance(docs, list)
    assert len(docs) >= 1, f"no docs found: {docs!r}"
    doc = next((d for d in docs if d.get("title") == "Smoke Doc"), None)
    assert doc is not None, f"Smoke Doc not in {docs!r}"

    capfd.readouterr()
    rc = main_mod.main(["doc", "show", doc["doc_id"]])
    assert rc == 0
    out = capfd.readouterr().out
    assert "Smoke Doc" in out


def test_note_add_list_clear_e2e(capfd, fresh_dir):
    main_mod.main([
        "init", "--name", "smoke", "--objective", "obj", "--retro-day", "monday",
    ])
    capfd.readouterr()

    rc = main_mod.main(["note", "remember this"])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["note", "list", "--json"])
    assert rc == 0
    out = capfd.readouterr().out
    # note_list JSON is a list of {text, context, created_at}
    notes = json.loads(out)
    assert any("remember this" in (n.get("text") or "") for n in notes)

    capfd.readouterr()
    rc = main_mod.main(["note", "clear"])
    assert rc == 0

    capfd.readouterr()
    rc = main_mod.main(["note", "list", "--json"])
    out = capfd.readouterr().out
    notes_after = json.loads(out)
    assert notes_after == []
