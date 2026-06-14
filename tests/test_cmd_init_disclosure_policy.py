"""Tests for ``beacon init`` disclosure_policy persistence (ms-63 e-1441 / e-1428).

Acceptance criteria pinned here:
  § 2  default sensitivity = high (= safe-default per SPEC § 設計方針 2)
  § 3  ``beacon init --sensitivity low`` writes the low posture
  e-1428 the policy is written to ``project.json`` at create time

These tests stay at the cmd_init layer (not the bash wrapper); the bash
forwarding is exercised through dispatch.py argparse defaults and a
separate integration-level test on the CLI dispatcher would be the right
place to pin the e2e wiring (out of scope here).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import commands  # noqa: E402


@pytest.fixture
def project_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".beacon").mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    # Required init env vars; tests focus on disclosure_policy so the
    # other fields are fixed.
    monkeypatch.setenv("BEACON_NAME", "test-proj")
    monkeypatch.setenv("BEACON_OBJECTIVE", "test obj")
    monkeypatch.setenv("BEACON_RETRO_DAY", "friday")
    # cmd_init may try to prompt for a profile choice on tty; force None.
    monkeypatch.setattr(commands, "_maybe_prompt_initial_profile",
                        lambda: None)
    # Drain potentially-leaky env from prior tests.
    monkeypatch.delenv("BEACON_SENSITIVITY", raising=False)
    return cwd


def _read_project_json(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text())


# ---------------------------------------------------------------------------
# Default posture: high (SPEC § 設計方針 2)
# ---------------------------------------------------------------------------

def test_init_default_sensitivity_is_high(project_cwd, capsys):
    """SPEC acceptance § 2: ``beacon init`` with no flags → high posture.

    Forgetting to configure must fail closed (= the AI clams up), not fail
    open (= the AI leaks). This is the structural realization of "default
    NDA, opt-out for OSS" from the NDA メタファー (SPEC § 設計方針 4).
    """
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert data["disclosure_policy"]["sensitivity"] == "high"
    assert data["disclosure_policy"]["t5_response_mode"] == "schema-only"
    assert data["disclosure_policy"]["t5_free_text"] is False


def test_init_default_prints_high_posture_banner(project_cwd, capsys):
    """User-visible signal that the safe default is in effect."""
    commands.cmd_init()
    out = capsys.readouterr().out
    assert "disclosure_policy.sensitivity = high" in out


# ---------------------------------------------------------------------------
# Opt-in low posture: --sensitivity low (SPEC acceptance § 3)
# ---------------------------------------------------------------------------

def test_init_sensitivity_low_via_env(project_cwd, monkeypatch, capsys):
    """``--sensitivity low`` (forwarded as BEACON_SENSITIVITY) opt-in to open."""
    monkeypatch.setenv("BEACON_SENSITIVITY", "low")
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    policy = data["disclosure_policy"]
    assert policy["sensitivity"] == "low"
    assert policy["t5_response_mode"] == "free"
    assert policy["t5_free_text"] is True


def test_init_low_posture_prints_open_banner(project_cwd, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_SENSITIVITY", "low")
    commands.cmd_init()
    out = capsys.readouterr().out
    assert "disclosure_policy.sensitivity = low" in out


# ---------------------------------------------------------------------------
# Fail-closed: typos / unknown values degrade to high
# ---------------------------------------------------------------------------

def test_init_unknown_sensitivity_falls_back_to_high(project_cwd, monkeypatch):
    """A typo'd env value (e.g. "LOW", "yes", "lo") must NOT silently open.

    The argparse layer at the CLI level rejects unknown choices, but the
    env-var passthrough at the cmd_init level is also fail-closed so a
    direct invocation can't sneak through with a near-miss value.
    """
    monkeypatch.setenv("BEACON_SENSITIVITY", "LOW-typo")
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert data["disclosure_policy"]["sensitivity"] == "high"


def test_init_empty_sensitivity_treated_as_default(project_cwd, monkeypatch):
    monkeypatch.setenv("BEACON_SENSITIVITY", "")
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert data["disclosure_policy"]["sensitivity"] == "high"


def test_init_sensitivity_low_uppercase_normalized(project_cwd, monkeypatch):
    """Case-insensitive on the env-var side. argparse upstream is case-strict;
    this is a forgiving back-stop so a Windows shell quirk doesn't silently
    promote a user-intended "low" to "high"."""
    monkeypatch.setenv("BEACON_SENSITIVITY", "LOW")
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert data["disclosure_policy"]["sensitivity"] == "low"


# ---------------------------------------------------------------------------
# Project.json shape: disclosure_policy is a top-level sibling, not nested
# ---------------------------------------------------------------------------

def test_init_disclosure_policy_is_top_level_field(project_cwd):
    """The field lives next to ``name`` / ``milestones`` / ``retro_day``.

    Nesting it under (e.g.) ``meta.disclosure_policy`` would force every
    reader to know the nesting path. Keeping it top-level matches the
    rest of the project schema's convention and lets legacy readers that
    don't know the field exist simply ignore it (forward-compat).
    """
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert "disclosure_policy" in data
    assert "meta" not in data or "disclosure_policy" not in data.get("meta", {})


def test_init_preserves_other_fields(project_cwd):
    commands.cmd_init()
    data = _read_project_json(project_cwd)
    assert data["name"] == "test-proj"
    assert data["objective"] == "test obj"
    assert data["retro_day"] == "friday"
    assert data["milestones"] == []
