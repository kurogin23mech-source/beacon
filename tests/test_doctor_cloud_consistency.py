"""Tests for `beacon doctor` cloud-state-divergence check (ms-61 / e-1776).

Verifies that doctor surfaces a WARN [cloud-state-divergence] line when the
two on-disk state files (.beacon/cloud.json existence and
.beacon/config.json["mode"]) narrate cloud vs local in disagreement, and
stays silent when both surfaces agree or when the mode field is absent.

Background: e-1861 made cloud.json existence the *sole* source of truth for
cloud-vs-local routing; config.json["mode"] is ignored at runtime. But a
stale or sub-agent-rewritten mode field still misleads humans / AI reading
state by hand and reproduces the symptom shape of the 2026-06-15 incident
("Web UI shows doc / CLI says Document not found"). This check makes the
divergence explicit at warning level, includes a mtime breadcrumb, and
suggests a one-line repair (AC #1–#4 of e-1776).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def in_tmp_cwd():
    """Run the check in a clean temp cwd so other .beacon/ files don't leak in."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".beacon").mkdir()
    original_cwd = os.getcwd()
    os.chdir(root)
    try:
        yield root
    finally:
        os.chdir(original_cwd)
        tmp.cleanup()


def _write_cloud_json(root: Path, payload: dict | None = None) -> None:
    payload = payload or {"project_id": "test-proj", "api_url": "https://example.test"}
    (root / ".beacon" / "cloud.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_config_json(root: Path, payload: dict) -> None:
    (root / ".beacon" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


# --- AC #1: cloud.json present + config.json mode != "cloud" ------------- #


def test_warns_when_cloud_exists_but_config_mode_is_local(in_tmp_cwd):
    """AC #1: cloud.json present + config.json mode=local = warning surfaced."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    _write_config_json(in_tmp_cwd, {"mode": "local"})

    warnings = _doctor_check_cloud_state_consistency()
    assert len(warnings) == 1
    w = warnings[0]
    assert "WARN [cloud-state-divergence]" in w
    # AC #1: names both the cloud.json presence and the stale mode value
    assert "cloud.json exists" in w
    assert "'local'" in w  # repr('local')
    # AC #4: includes a one-line repair command suggestion
    assert "Repair" in w
    assert "config.json" in w
    # AC #3: includes the mtime breadcrumb
    assert "last modified" in w


def test_warns_when_cloud_exists_but_config_mode_is_arbitrary_non_cloud(in_tmp_cwd):
    """Defensive: any mode != 'cloud' (e.g. typo or unknown value) also triggers."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    _write_config_json(in_tmp_cwd, {"mode": "offline"})

    warnings = _doctor_check_cloud_state_consistency()
    assert len(warnings) == 1
    assert "'offline'" in warnings[0]


# --- AC #2: cloud.json absent + config.json mode == "cloud" -------------- #


def test_warns_when_config_mode_is_cloud_but_cloud_json_missing(in_tmp_cwd):
    """AC #2: config.json says cloud but cloud.json is gone = warning surfaced."""
    from commands import _doctor_check_cloud_state_consistency

    _write_config_json(in_tmp_cwd, {"mode": "cloud"})
    # No cloud.json
    assert not (in_tmp_cwd / ".beacon" / "cloud.json").exists()

    warnings = _doctor_check_cloud_state_consistency()
    assert len(warnings) == 1
    w = warnings[0]
    assert "WARN [cloud-state-divergence]" in w
    # AC #2: names the broken-binding direction
    assert "cloud.json is missing" in w
    assert "LocalStore" in w  # explicitly names the runtime degradation path
    # AC #4: includes a one-line repair command (offers both directions)
    assert "Repair" in w
    assert "beacon cloud upload-initial" in w
    # AC #3: includes the mtime breadcrumb
    assert "last modified" in w


# --- Silent cases (consistent or no narration to compare) ---------------- #


def test_silent_when_both_files_absent(in_tmp_cwd):
    """No state files at all = nothing to reconcile, silent."""
    from commands import _doctor_check_cloud_state_consistency

    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


def test_silent_when_only_cloud_json_present(in_tmp_cwd):
    """Pure cloud project with no config.json = consistent, silent."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    # No config.json at all
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


def test_silent_when_config_has_no_mode_field(in_tmp_cwd):
    """config.json present but no mode key = nothing to diverge on."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    _write_config_json(in_tmp_cwd, {"some_other_setting": "value"})
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


def test_silent_when_both_agree_cloud(in_tmp_cwd):
    """cloud.json + config.json mode=cloud = legacy but consistent, silent.

    (The separate legacy-mode-field warning in check #5 still fires for the
    cleanliness hint; that's intentional and is a different check.)
    """
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    _write_config_json(in_tmp_cwd, {"mode": "cloud"})
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


def test_silent_when_both_agree_local(in_tmp_cwd):
    """No cloud.json + config.json mode=local = consistent, silent."""
    from commands import _doctor_check_cloud_state_consistency

    _write_config_json(in_tmp_cwd, {"mode": "local"})
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


# --- Defensive: malformed config.json ------------------------------------ #


def test_silent_when_config_json_unreadable(in_tmp_cwd):
    """Malformed config.json = skip silently (reported by other checks)."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    (in_tmp_cwd / ".beacon" / "config.json").write_text("not json {{{", encoding="utf-8")
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []


def test_silent_when_config_json_is_list(in_tmp_cwd):
    """Non-dict config.json (corrupted shape) = skip silently."""
    from commands import _doctor_check_cloud_state_consistency

    _write_cloud_json(in_tmp_cwd)
    (in_tmp_cwd / ".beacon" / "config.json").write_text("[1,2,3]", encoding="utf-8")
    warnings = _doctor_check_cloud_state_consistency()
    assert warnings == []
