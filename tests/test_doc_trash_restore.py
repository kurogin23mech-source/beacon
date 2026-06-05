"""Tests for doc trash + restore (ms-14 e-973, local mode).

Covers:

- ``cmd_doc_delete`` (local) flips frontmatter ``status: cancelled``
  and stamps ``trashed_at`` / ``trashed_by`` rather than unlinking.
- ``cmd_doc_delete`` refuses re-trash of an already-cancelled doc.
- ``cmd_doc_trash`` lists cancelled docs (default 30-day window + --all).
- ``cmd_doc_restore`` (bare doc-id, no --rev) lifts the cancelled
  marker and stamps restore audit fields.
- ``cmd_doc_restore`` refuses to restore a non-cancelled doc.
- ``cmd_doc_list`` hides cancelled docs by default and shows them with
  ``BEACON_INCLUDE_TRASHED=1``.
- Cloud mode refuses the new trash listing / trash restore so the
  divergence is loud (it'll be lifted by a follow-up task).
- ``_append_changelog`` records ``op=doc_delete`` and ``op=doc_restore``.

The revision-history restore path (``BEACON_REV`` present, cloud
server API) is intentionally not exercised here — it requires a live
server. It's instead asserted indirectly: the polymorphic dispatcher
in ``cmd_doc_restore`` routes by ``BEACON_REV`` presence, and the
``--rev`` branch's code body is unchanged from before e-973.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_doc(title: str = "Sample Doc", scope: str = "memo", body: str = "Body.\n") -> str:
    """Return the on-disk content for a fresh (un-trashed) doc."""
    return (
        "---\n"
        f"scope: {scope}\n"
        "---\n"
        f"# {title}\n\n{body}"
    )


@pytest.fixture
def docs_project(monkeypatch):
    """A local-mode project with a `docs_dir` ready for doc commands.

    Each test gets a clean tempdir under which:
      - .beacon/project.json holds a minimal valid project,
      - .beacon/documents/ holds whatever docs the test seeds.
    """
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        docs_dir = beacon_dir / "documents"
        docs_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps(
                {"name": "demo", "milestones": [], "operations": []},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _reload_commands():
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands  # noqa: WPS433
    return commands


def _seed_doc(root: Path, doc_id: str, content: str | None = None) -> Path:
    """Write a doc file under .beacon/documents/<doc_id>.md, return its path."""
    fpath = root / ".beacon" / "documents" / f"{doc_id}.md"
    fpath.write_text(content or _make_doc(), encoding="utf-8")
    return fpath


def _read_doc_meta(fpath: Path) -> dict:
    """Parse just the frontmatter of an on-disk doc (test-side parser)."""
    text = fpath.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    header = text[4:end]
    meta = {}
    for line in header.split("\n"):
        line = line.strip()
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _read_changelog(root: Path) -> list[dict]:
    path = root / ".beacon" / "changelog.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# cmd_doc_delete — soft-delete in local mode
# ---------------------------------------------------------------------------


def test_doc_delete_local_soft_deletes_and_keeps_file(docs_project, monkeypatch, capsys):
    fpath = _seed_doc(docs_project, "design-foo")
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "superseded by design-bar")
    monkeypatch.delenv("BEACON_JSON", raising=False)

    commands = _reload_commands()
    commands.cmd_doc_delete()

    # File still exists at canonical path — trash is logical, not physical.
    assert fpath.exists()
    meta = _read_doc_meta(fpath)
    assert meta["status"] == "cancelled"
    assert meta["trashed_at"]
    assert meta["trashed_by"]
    assert meta["trash_reason"] == "superseded by design-bar"
    out = capsys.readouterr().out
    assert "Trashed" in out


def test_doc_delete_local_refuses_already_trashed(docs_project, monkeypatch, capsys):
    _seed_doc(docs_project, "design-foo")
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "")

    commands = _reload_commands()
    commands.cmd_doc_delete()

    # Second trash must refuse.
    monkeypatch.setenv("BEACON_REASON", "")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doc_delete()
    assert ei.value.code == 1
    err = capsys.readouterr().out
    assert "already in trash" in err


def test_doc_delete_writes_changelog_op(docs_project, monkeypatch):
    _seed_doc(docs_project, "design-foo")
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "wrong scope")

    commands = _reload_commands()
    commands.cmd_doc_delete()

    log = _read_changelog(docs_project)
    matched = [e for e in log if e.get("op") == "doc_delete"]
    assert matched, f"changelog missing doc_delete op: {log}"
    assert matched[0]["doc_id"] == "design-foo"
    assert matched[0]["reason"] == "wrong scope"


# ---------------------------------------------------------------------------
# cmd_doc_trash — listing
# ---------------------------------------------------------------------------


def test_doc_trash_lists_cancelled_doc(docs_project, monkeypatch, capsys):
    _seed_doc(docs_project, "design-foo")
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "")
    commands = _reload_commands()
    commands.cmd_doc_delete()
    capsys.readouterr()  # flush the "Trashed: ..." line from cmd_doc_delete

    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DAYS", "30")
    commands.cmd_doc_trash()
    items = json.loads(capsys.readouterr().out.strip())
    assert isinstance(items, list)
    assert any(it["doc_id"] == "design-foo" for it in items)
    item = next(it for it in items if it["doc_id"] == "design-foo")
    assert item["trashed_at"]
    assert item["trashed_by"]


def test_doc_trash_days_window_hides_old(docs_project, monkeypatch, capsys):
    """A doc trashed long ago is excluded by the default 30-day window
    but reappears when ``--all`` is passed.
    """
    # Hand-craft a doc that's been trashed 90 days ago by writing the
    # frontmatter directly — this matches what the e-973 trash flow
    # would have produced 90 days back.
    long_ago = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        "---\n"
        "scope: memo\n"
        "status: cancelled\n"
        f"trashed_at: {long_ago}\n"
        "trashed_by: alice\n"
        "---\n"
        "# Old\n\nbody\n"
    )
    _seed_doc(docs_project, "old-doc", content)

    commands = _reload_commands()

    # Default 30-day window: ancient doc hidden.
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DAYS", "30")
    commands.cmd_doc_trash()
    items = json.loads(capsys.readouterr().out.strip())
    assert all(it["doc_id"] != "old-doc" for it in items)

    # --all (BEACON_DAYS=all): ancient doc surfaces.
    monkeypatch.setenv("BEACON_DAYS", "all")
    commands.cmd_doc_trash()
    items = json.loads(capsys.readouterr().out.strip())
    assert any(it["doc_id"] == "old-doc" for it in items)


def test_doc_trash_empty_prints_friendly_message(docs_project, monkeypatch, capsys):
    monkeypatch.delenv("BEACON_JSON", raising=False)
    monkeypatch.setenv("BEACON_DAYS", "30")
    commands = _reload_commands()
    commands.cmd_doc_trash()
    out = capsys.readouterr().out
    assert "No trashed docs" in out


# ---------------------------------------------------------------------------
# cmd_doc_restore — trash restore (no --rev)
# ---------------------------------------------------------------------------


def test_doc_restore_from_trash_clears_marker(docs_project, monkeypatch, capsys):
    fpath = _seed_doc(docs_project, "design-foo")
    commands = _reload_commands()

    # Trash, then restore.
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "scope drift")
    commands.cmd_doc_delete()

    monkeypatch.setenv("BEACON_REASON", "reverted scope decision")
    monkeypatch.delenv("BEACON_REV", raising=False)
    monkeypatch.delenv("BEACON_JSON", raising=False)
    commands.cmd_doc_restore()

    meta = _read_doc_meta(fpath)
    # Cancel marker fully cleared.
    assert "status" not in meta
    assert "trashed_at" not in meta
    assert "trashed_by" not in meta
    assert "trash_reason" not in meta
    # Restore audit stamped.
    assert meta["restored_at"]
    assert meta["restored_by"]
    assert meta["restore_reason"] == "reverted scope decision"


def test_doc_restore_refuses_active_doc(docs_project, monkeypatch, capsys):
    """Restore must refuse non-cancelled docs so accidental re-runs
    don't silently mutate active state.
    """
    _seed_doc(docs_project, "design-foo")
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "")
    monkeypatch.delenv("BEACON_REV", raising=False)
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doc_restore()
    assert ei.value.code == 1
    err = capsys.readouterr().err + capsys.readouterr().out
    assert "not in trash" in err or "not in trash" in capsys.readouterr().err


def test_doc_restore_writes_changelog_op(docs_project, monkeypatch):
    _seed_doc(docs_project, "design-foo")
    commands = _reload_commands()
    monkeypatch.setenv("BEACON_DOC_ID", "design-foo")
    monkeypatch.setenv("BEACON_REASON", "")
    commands.cmd_doc_delete()

    monkeypatch.setenv("BEACON_REASON", "back on track")
    monkeypatch.delenv("BEACON_REV", raising=False)
    commands.cmd_doc_restore()

    log = _read_changelog(docs_project)
    matched = [e for e in log if e.get("op") == "doc_restore"]
    assert matched, f"changelog missing doc_restore op: {log}"
    assert matched[0]["doc_id"] == "design-foo"
    assert matched[0]["reason"] == "back on track"


def test_doc_restore_missing_doc_id_exits(docs_project, monkeypatch):
    monkeypatch.delenv("BEACON_DOC_ID", raising=False)
    monkeypatch.setenv("BEACON_DOC_ID", "")
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doc_restore()
    assert ei.value.code == 1


# ---------------------------------------------------------------------------
# cmd_doc_list — default hide + --include-trashed
# ---------------------------------------------------------------------------


def test_doc_list_default_hides_trashed(docs_project, monkeypatch, capsys):
    _seed_doc(docs_project, "alive-doc")
    _seed_doc(docs_project, "dead-doc")
    commands = _reload_commands()

    # Trash dead-doc.
    monkeypatch.setenv("BEACON_DOC_ID", "dead-doc")
    monkeypatch.setenv("BEACON_REASON", "")
    commands.cmd_doc_delete()

    # Default list: trashed doc hidden.
    capsys.readouterr()  # flush
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.delenv("BEACON_INCLUDE_TRASHED", raising=False)
    commands.cmd_doc_list()
    out = json.loads(capsys.readouterr().out.strip())
    ids = [d["doc_id"] for d in out]
    assert "alive-doc" in ids
    assert "dead-doc" not in ids


def test_doc_list_include_trashed_shows_all(docs_project, monkeypatch, capsys):
    _seed_doc(docs_project, "alive-doc")
    _seed_doc(docs_project, "dead-doc")
    commands = _reload_commands()
    monkeypatch.setenv("BEACON_DOC_ID", "dead-doc")
    monkeypatch.setenv("BEACON_REASON", "")
    commands.cmd_doc_delete()

    capsys.readouterr()
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_INCLUDE_TRASHED", "1")
    commands.cmd_doc_list()
    out = json.loads(capsys.readouterr().out.strip())
    ids = [d["doc_id"] for d in out]
    assert "alive-doc" in ids
    assert "dead-doc" in ids


# ---------------------------------------------------------------------------
# Cloud mode refusals — divergence is loud, not silent
# ---------------------------------------------------------------------------


def test_doc_trash_refuses_cloud_mode(docs_project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_CLOUD", "1")
    monkeypatch.setenv("BEACON_DAYS", "30")
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doc_trash()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "cloud mode" in err


def test_doc_restore_from_trash_refuses_cloud_mode(docs_project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_CLOUD", "1")
    monkeypatch.setenv("BEACON_DOC_ID", "anything")
    monkeypatch.delenv("BEACON_REV", raising=False)
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doc_restore()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "cloud mode" in err
