"""Unit tests for `beacon project export --backup` and `project import`
(ms-14 e-828).

Focused on local mode since the CI environment doesn't have cloud auth.
Cloud-mode export reuses the same ZIP layout, validated indirectly by
the manifest schema checks.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def local_project(monkeypatch):
    """Set up a local .beacon/ tree with project, doc, changelog, retro."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        beacon_dir = root / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "documents").mkdir()
        (beacon_dir / "retro").mkdir()

        project = {
            "name": "demo",
            "id": "demo",
            "summary": "test snapshot",
            "milestones": [
                {
                    "id": "ms-1", "title": "first",
                    "status": "in_progress", "progress": 50,
                    "entries": [
                        {"id": "e-1", "type": "task", "status": "done",
                         "description": "do thing"},
                        {"id": "e-2", "type": "commit", "status": "done",
                         "description": "wire it up",
                         "meta": {"hash": "abc1234"}},
                    ],
                },
            ],
            "operations": [],
        }
        (beacon_dir / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        (beacon_dir / "documents" / "doc-one.md").write_text(
            "---\nscope: core\ntitle: doc-one\n---\nhello\n",
            encoding="utf-8",
        )
        (beacon_dir / "changelog.jsonl").write_text(
            '{"op":"milestone_add","ms_id":"ms-1"}\n', encoding="utf-8",
        )
        (beacon_dir / "retro" / "2026-W22.md").write_text(
            "# retro W22\n", encoding="utf-8",
        )
        (beacon_dir / "config.json").write_text(
            '{"retro_day":"friday"}\n', encoding="utf-8",
        )

        monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)

        yield root


def _run_export(monkeypatch, output: Path, json_mode: bool = False):
    monkeypatch.setenv("BEACON_OUTPUT", str(output))
    monkeypatch.setenv("BEACON_BACKUP", "1")
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "0")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    commands.cmd_project_export()


def _run_import(monkeypatch, inp: Path, target: Path, force: bool = False):
    monkeypatch.setenv("BEACON_INPUT", str(inp))
    monkeypatch.setenv("BEACON_TARGET", str(target))
    monkeypatch.setenv("BEACON_FORCE", "1" if force else "0")
    monkeypatch.setenv("BEACON_JSON", "1")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    commands.cmd_project_import()


def test_export_writes_manifest_and_payload(local_project, monkeypatch, capsys):
    output = local_project / "backup.zip"
    _run_export(monkeypatch, output)
    assert output.exists() and output.stat().st_size > 0

    with zipfile.ZipFile(output) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "project.json" in names
        assert "documents/doc-one.md" in names
        assert "changelog.jsonl" in names
        assert "retro/2026-W22.md" in names
        assert "config.json" in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert manifest["project_name"] == "demo"
        assert manifest["source_mode"] == "local"
        assert manifest["entry_counts"]["milestones"] == 1
        assert manifest["entry_counts"]["documents"] == 1
        assert manifest["entry_counts"]["changelog_lines"] == 1
        assert manifest["entry_counts"]["top_level_entries"] == 2


def test_export_refuses_existing_output(local_project, monkeypatch, capsys):
    output = local_project / "exists.zip"
    output.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("BEACON_OUTPUT", str(output))
    monkeypatch.setenv("BEACON_BACKUP", "1")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit) as exc:
        commands.cmd_project_export()
    assert exc.value.code == 1
    assert output.read_text() == "placeholder"  # untouched


def test_export_requires_output(monkeypatch, local_project):
    monkeypatch.setenv("BEACON_OUTPUT", "")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit):
        commands.cmd_project_export()


def test_round_trip_preserves_payload(local_project, monkeypatch):
    output = local_project / "round.zip"
    _run_export(monkeypatch, output)

    target = local_project / "restored"
    _run_import(monkeypatch, output, target)
    restored = target / ".beacon"
    assert restored.is_dir()

    # project.json identical (modulo whitespace)
    original = json.loads((local_project / ".beacon" / "project.json").read_text())
    extracted = json.loads((restored / "project.json").read_text())
    assert original == extracted

    # documents preserved
    assert (restored / "documents" / "doc-one.md").read_text() == \
        "---\nscope: core\ntitle: doc-one\n---\nhello\n"

    # changelog preserved
    assert "milestone_add" in (restored / "changelog.jsonl").read_text()

    # retro preserved
    assert (restored / "retro" / "2026-W22.md").read_text() == "# retro W22\n"

    # config preserved
    assert json.loads((restored / "config.json").read_text())["retro_day"] == "friday"

    # import marker present
    marker = json.loads((restored / ".import_manifest.json").read_text())
    assert marker["schema_version"] == 1
    assert marker["project_name"] == "demo"


def test_import_refuses_existing_target_without_force(local_project, monkeypatch):
    output = local_project / "preexist.zip"
    _run_export(monkeypatch, output)

    target = local_project / "preexisting"
    (target / ".beacon").mkdir(parents=True)
    (target / ".beacon" / "existing.txt").write_text("dont overwrite", encoding="utf-8")

    monkeypatch.setenv("BEACON_INPUT", str(output))
    monkeypatch.setenv("BEACON_TARGET", str(target))
    monkeypatch.setenv("BEACON_FORCE", "0")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit):
        commands.cmd_project_import()
    assert (target / ".beacon" / "existing.txt").read_text() == "dont overwrite"


def test_import_rejects_malformed_manifest(local_project, monkeypatch, tmp_path):
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("manifest.json", "{not json")
        zf.writestr("project.json", "{}")
    monkeypatch.setenv("BEACON_INPUT", str(bad_zip))
    monkeypatch.setenv("BEACON_TARGET", str(tmp_path / "out"))
    monkeypatch.setenv("BEACON_FORCE", "0")
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit):
        commands.cmd_project_import()


def test_import_rejects_unsupported_schema(local_project, monkeypatch, tmp_path):
    future_zip = tmp_path / "future.zip"
    with zipfile.ZipFile(future_zip, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema_version": 999}))
        zf.writestr("project.json", "{}")
    monkeypatch.setenv("BEACON_INPUT", str(future_zip))
    monkeypatch.setenv("BEACON_TARGET", str(tmp_path / "out2"))
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit):
        commands.cmd_project_import()


def test_import_rejects_unsafe_paths(local_project, monkeypatch, tmp_path):
    sneaky = tmp_path / "sneaky.zip"
    with zipfile.ZipFile(sneaky, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema_version": 1}))
        zf.writestr("project.json", "{}")
        zf.writestr("../escape.txt", "owned")
    monkeypatch.setenv("BEACON_INPUT", str(sneaky))
    monkeypatch.setenv("BEACON_TARGET", str(tmp_path / "out3"))
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    with pytest.raises(SystemExit):
        commands.cmd_project_import()
    assert not (tmp_path / "escape.txt").exists()
