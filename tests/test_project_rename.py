"""Tests for `beacon project rename` (ms-122 e-4033): change a project's display
name after creation, preserving all other data."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


def _init(tmp_path, monkeypatch, name="old-name"):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(json.dumps({
        "name": name, "milestones": [{"id": "ms-1", "title": "T",
                                      "status": "todo", "entries": []}],
    }, ensure_ascii=False), encoding="utf-8")


def _read(tmp_path):
    return json.loads((tmp_path / ".beacon" / "project.json").read_text())


def test_rename_updates_name(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_NEW_NAME", "経理チーム")
    commands.cmd_project_rename()
    assert _read(tmp_path)["name"] == "経理チーム"
    assert "経理チーム" in capsys.readouterr().out


def test_rename_preserves_other_data(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_NEW_NAME", "new")
    commands.cmd_project_rename()
    data = _read(tmp_path)
    assert data["name"] == "new"
    assert data["milestones"][0]["id"] == "ms-1"   # unrelated data intact


def test_rename_same_name_is_noop(tmp_path, monkeypatch, capsys):
    _init(tmp_path, monkeypatch, name="same")
    monkeypatch.setenv("BEACON_NEW_NAME", "same")
    commands.cmd_project_rename()
    assert "変更なし" in capsys.readouterr().out


def test_rename_empty_name_errors(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    monkeypatch.setenv("BEACON_NEW_NAME", "   ")
    try:
        commands.cmd_project_rename()
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 1
