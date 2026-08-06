"""Unit tests for `beacon deploy rollback` (e-581).

Focus areas:
  - rollback target selection (most recent vs penultimate, with/without
    service match, only-one-deploy refusal)
  - dry-mode default (no --execute) prints the gcloud command but does NOT
    void the record
  - --execute path voids the record with a structured reason
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_deploy  # noqa: E402  (ms-127 e-4815: deploy handlers live here)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_with_deploys(deploys):
    return {
        "name": "t",
        "summary": "",
        "milestones": [],
        "deployments": deploys,
    }


def deploy(id_, *, service="svc-prod", revision="rev-1",
           deployed_at="2026-05-28T10:00:00Z", voided=False):
    d = {
        "id": id_,
        "service": service,
        "revision": revision,
        "deployed_at": deployed_at,
    }
    if voided:
        d["voided"] = True
    return d


def _run_rollback(monkeypatch, *, project, execute=False, reason="oops",
                  service_override="", apply_op_fn=None):
    """Run cmd_deploy_rollback with mocked load_project / apply_operation /
    subprocess. Returns (exit_code, stdout, stderr, captured_op_name)."""

    monkeypatch.setattr(cmd_deploy, "load_project", lambda: project)
    monkeypatch.setenv("BEACON_REASON", reason)
    monkeypatch.setenv("BEACON_EXECUTE", "1" if execute else "")
    monkeypatch.setenv("BEACON_SERVICE", service_override)
    monkeypatch.setenv("BEACON_JSON", "")

    # subprocess.run gets called for gcloud only when --execute is set.
    call_log = []

    def fake_run(args, **kwargs):
        call_log.append(args)
        class R: returncode = 0
        return R()
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    # apply_operation: actually run the op so we know voiding works.
    captured = {"op_name": None, "reason": None}

    def default_apply_op(project_id, op, *, op_name="", actor="", reason="", project_file=None):
        captured["op_name"] = op_name
        captured["reason"] = reason
        new_data, result = op(project)
        return result

    fake = apply_op_fn or default_apply_op
    fake_ops_module = mock.MagicMock()
    fake_ops_module.apply_operation = fake
    monkeypatch.setitem(sys.modules, "operations", fake_ops_module)

    buf = io.StringIO()
    err = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            cmd_deploy.cmd_deploy_rollback()
    except SystemExit as e:
        code = e.code
    return code, buf.getvalue(), err.getvalue(), captured, call_log


# ---------------------------------------------------------------------------
# Reason required
# ---------------------------------------------------------------------------

def test_reason_required(monkeypatch):
    monkeypatch.setattr(cmd_deploy, "load_project",
                        lambda: _project_with_deploys([deploy("d-1")]))
    monkeypatch.setenv("BEACON_REASON", "")
    buf = io.StringIO()
    err = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf), redirect_stderr(err):
            cmd_deploy.cmd_deploy_rollback()
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "--reason is required" in err.getvalue()


# ---------------------------------------------------------------------------
# Rollback target selection
# ---------------------------------------------------------------------------

def test_only_one_deploy_refuses(monkeypatch):
    project = _project_with_deploys([deploy("d-1", deployed_at="2026-05-28T10:00:00Z")])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False,
    )
    assert code == 1
    assert "Roll forward" in err


def test_no_active_deploys_refuses(monkeypatch):
    project = _project_with_deploys([
        deploy("d-1", deployed_at="2026-05-28T10:00:00Z", voided=True),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False,
    )
    assert code == 1
    assert "no active deployments" in err


def test_two_deploys_picks_penultimate(monkeypatch):
    """When there are two active deploys, the older becomes the rollback target."""
    project = _project_with_deploys([
        deploy("d-1", revision="rev-1", deployed_at="2026-05-28T10:00:00Z"),
        deploy("d-2", revision="rev-2", deployed_at="2026-05-28T12:00:00Z"),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False,
    )
    # Dry mode: process exits 0 after printing the plan.
    assert code == 0
    assert "current  → d-2  rev=rev-2" in out
    assert "rollback → d-1  rev=rev-1" in out
    # In dry mode, no gcloud should have been invoked.
    assert not any(c[:2] == ["gcloud", "run"] for c in calls)


def test_skips_voided_deploys_when_selecting_target(monkeypatch):
    project = _project_with_deploys([
        deploy("d-1", revision="rev-1", deployed_at="2026-05-28T10:00:00Z"),
        deploy("d-2", revision="rev-2", deployed_at="2026-05-28T11:00:00Z", voided=True),
        deploy("d-3", revision="rev-3", deployed_at="2026-05-28T12:00:00Z"),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False,
    )
    assert code == 0
    # Voided d-2 must be skipped; d-3 → d-1 is the chosen pair
    assert "current  → d-3" in out
    assert "rollback → d-1" in out


# ---------------------------------------------------------------------------
# Dry-mode default vs --execute
# ---------------------------------------------------------------------------

def test_dry_mode_does_not_void(monkeypatch):
    project = _project_with_deploys([
        deploy("d-1", deployed_at="2026-05-28T10:00:00Z"),
        deploy("d-2", deployed_at="2026-05-28T12:00:00Z"),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False, reason="reverting bad config",
    )
    assert code == 0
    assert captured["op_name"] is None  # apply_operation never invoked
    # And the plan was printed
    assert "gcloud run services update-traffic" in out


def test_execute_runs_gcloud_and_voids(monkeypatch):
    project = _project_with_deploys([
        deploy("d-1", deployed_at="2026-05-28T10:00:00Z"),
        deploy("d-2", deployed_at="2026-05-28T12:00:00Z"),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=True, reason="reverting bad config",
    )
    assert code is None  # success exits None (or 0)
    # gcloud was called
    assert any(c[:3] == ["gcloud", "run", "services"] for c in calls)
    # And apply_operation was invoked to void
    assert captured["op_name"] == "deploy.rollback"
    assert "rolled back to d-1" in captured["reason"]


# ---------------------------------------------------------------------------
# Missing revision data
# ---------------------------------------------------------------------------

def test_refuses_when_previous_has_no_revision(monkeypatch):
    project = _project_with_deploys([
        deploy("d-1", revision="", deployed_at="2026-05-28T10:00:00Z"),
        deploy("d-2", revision="rev-2", deployed_at="2026-05-28T12:00:00Z"),
    ])
    code, out, err, captured, calls = _run_rollback(
        monkeypatch, project=project, execute=False,
    )
    assert code == 1
    assert "no `revision` recorded" in err
