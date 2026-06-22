"""ms-84 Phase 3 cut-over tests (e-2037).

Pins three structural promises:

1. ``_rename_local_project_json_for_cloud_cutover`` is idempotent and
   never overwrites an existing backup. Source file moves to
   ``project.json.before-cloud-YYYYMMDD`` (with ``.N`` suffix on collision).
2. The helper is a no-op when the source file does not exist (= already
   cut over or born cloud-only).
3. ``find_beacon_root`` / ``beacon-find-root`` accept ``cloud.json`` as a
   project root marker (= cloud mode no longer requires project.json on
   disk).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


@pytest.fixture
def beacon_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        bdir = Path(tmp) / ".beacon"
        bdir.mkdir()
        monkeypatch.chdir(tmp)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(bdir / "project.json"))
        yield bdir


def test_rename_moves_file_with_dated_suffix(beacon_dir):
    pf = beacon_dir / "project.json"
    pf.write_text('{"name": "test"}', encoding="utf-8")
    dest = commands._rename_local_project_json_for_cloud_cutover(str(pf))
    assert dest is not None
    assert not pf.exists()
    assert Path(dest).exists()
    assert "before-cloud-" in Path(dest).name


def test_rename_is_noop_when_source_missing(beacon_dir):
    pf = beacon_dir / "project.json"
    assert not pf.exists()
    dest = commands._rename_local_project_json_for_cloud_cutover(str(pf))
    assert dest is None
    assert not pf.exists()


def test_rename_does_not_overwrite_existing_backup(beacon_dir):
    """Second run on the same day should append a numeric suffix, never
    clobber the earlier backup. We must keep every recovery copy."""
    import datetime as _dt
    pf = beacon_dir / "project.json"
    pf.write_text('{"name": "first"}', encoding="utf-8")
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    pre_existing = beacon_dir / f"project.json.before-cloud-{stamp}"
    pre_existing.write_text('{"name": "earlier_backup"}', encoding="utf-8")

    dest = commands._rename_local_project_json_for_cloud_cutover(str(pf))
    assert dest is not None
    # Earlier backup must still exist and still hold its original payload.
    assert pre_existing.exists()
    assert "earlier_backup" in pre_existing.read_text(encoding="utf-8")
    # New backup landed at a sibling suffix.
    assert Path(dest) != pre_existing
    assert Path(dest).exists()
    assert "first" in Path(dest).read_text(encoding="utf-8")


def test_beacon_find_root_accepts_cloud_json_marker(tmp_path, monkeypatch):
    """beacon-find-root must locate a project whose only marker is
    cloud.json (= ms-84 Phase 3 cut-over end state)."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / ".beacon").mkdir()
    (proj / ".beacon" / "cloud.json").write_text(
        '{"project_id": "p1"}', encoding="utf-8"
    )
    subdir = proj / "subdir" / "deeper"
    subdir.mkdir(parents=True)
    repo_root = Path(__file__).resolve().parent.parent
    find_root = repo_root / "bin" / "beacon-find-root"
    if not find_root.exists():
        pytest.skip("bin/beacon-find-root not available in this checkout")
    result = subprocess.run(
        [str(find_root)], cwd=subdir, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    # Resolve symlinks for macOS /private/var vs /var equivalence.
    assert Path(result.stdout.strip()).resolve() == proj.resolve()


def test_beacon_find_root_still_accepts_project_json_marker(tmp_path):
    """Local mode (= no cloud.json, only project.json) still works."""
    proj = tmp_path / "localproj"
    proj.mkdir()
    (proj / ".beacon").mkdir()
    (proj / ".beacon" / "project.json").write_text("{}", encoding="utf-8")
    repo_root = Path(__file__).resolve().parent.parent
    find_root = repo_root / "bin" / "beacon-find-root"
    if not find_root.exists():
        pytest.skip("bin/beacon-find-root not available in this checkout")
    result = subprocess.run(
        [str(find_root)], cwd=proj, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert Path(result.stdout.strip()).resolve() == proj.resolve()


def test_beacon_find_root_exits_nonzero_outside_project(tmp_path):
    """No marker anywhere on the walk-up path → exit 1, no output."""
    outside = tmp_path / "outside"
    outside.mkdir()
    repo_root = Path(__file__).resolve().parent.parent
    find_root = repo_root / "bin" / "beacon-find-root"
    if not find_root.exists():
        pytest.skip("bin/beacon-find-root not available in this checkout")
    # Walk-up from tmp_path which has no .beacon/ markers anywhere.
    # On macOS /private/var/folders/... no Beacon root exists.
    result = subprocess.run(
        [str(find_root)], cwd=outside, capture_output=True, text=True
    )
    # Either no Beacon root (exit 1) or the system happens to have one
    # somewhere up the tree (= rare CI case). The contract here is that
    # when no marker is hit, exit 1 + no stdout.
    if result.returncode == 0:
        # Some ancestor has a marker — that's environmental, we accept it
        # but the stdout should be that ancestor, not `outside` itself.
        assert Path(result.stdout.strip()) != outside.resolve()
    else:
        assert result.returncode == 1
        assert result.stdout.strip() == ""
