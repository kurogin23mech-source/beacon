"""ms-164 e-5946: deploy record maps commits to the DELIVERABLE-BEARING Targets
generically and stamps ``target_ids`` on the record.

The old scan walked ``data['milestones']`` and hardcoded ``status in
("done","observing")`` as "newly completed" (→ major). Now it walks every
deliverable-bearing class (``occupation.deliverable_bearing_classes`` ×
``target_records``) — dev = ``["milestone"]`` so dev is byte-identical — and the
completion→major rule is a per-class DECLARATION table
(``_DEPLOY_COMPLETION_TERMINAL_STATES``). A class without declared terminal states
(what counts as "completed" for it is a product decision deferred to parent/user,
ms-164 DM 2026-09-03) contributes commit ATTRIBUTION only and never flips a deploy
to major.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import cmd_deploy  # noqa: E402
import git_read_port  # noqa: E402
import target_descriptor as td  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
        monkeypatch.chdir(tmp)
        # A single deployed commit, fed via the read port so no real git runs.
        monkeypatch.setattr(git_read_port, "log_commits",
                            lambda *a, **k: [{"hash": "cafef00", "message": "m"}])
        monkeypatch.setattr(git_read_port, "rev_parse_short", lambda h: h[:7])
        # Non-prod env → the deployed-prod marker (git push) is skipped.
        monkeypatch.setenv("BEACON_ENVIRONMENT", "staging")
        monkeypatch.setenv("BEACON_DESCRIPTION", "d")
        for k in ("BEACON_MODE", "BEACON_SEMVER", "BEACON_VERSION", "BEACON_HASH",
                  "BEACON_DATE", "BEACON_INSERT_BEFORE", "BEACON_TYPE",
                  "BEACON_REVISION", "BEACON_BACKEND", "BEACON_JSON"):
            monkeypatch.delenv(k, raising=False)
        # Keep the record hermetic — the map/graph trigger fires are best-effort
        # side channels, not part of what this test asserts.
        monkeypatch.setattr(cmd_deploy, "_fire_map_reconcile_trigger", lambda: None)
        monkeypatch.setattr(cmd_deploy, "_fire_graph_reseed_trigger", lambda: None)
        try:
            yield Path(tmp)
        finally:
            os.chdir(tempfile.gettempdir())


def _write(project_dir: Path, data: dict) -> None:
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps(data), encoding="utf-8")


def _last_deploy(project_dir: Path) -> dict:
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return data["deployments"][-1]


def _ms_with_commit(ms_id: str, status: str) -> dict:
    return {"id": ms_id, "title": ms_id, "status": status,
            "entries": [{"id": "e-1", "type": "commit",
                         "meta": {"hash": "cafef00"}}]}


def test_dev_newly_completed_is_major(project_dir):
    """A done milestone touched by the deployed commits → newly completed → major,
    and the record is stamped with target_ids (dev behaviour, unchanged)."""
    _write(project_dir, {"name": "t",
                         "milestones": [_ms_with_commit("ms-1", "done")]})
    cmd_deploy.cmd_deploy_record()
    dep = _last_deploy(project_dir)
    assert dep["type"] == "major"
    assert dep["newly_completed_ms"] == ["ms-1"]
    assert dep["patch_ms"] == []
    assert dep["target_ids"] == ["ms-1"]


def test_dev_in_progress_is_minor_patch(project_dir):
    """An in_progress milestone → patched, not newly completed → minor, but still
    attributed via target_ids."""
    _write(project_dir, {"name": "t",
                         "milestones": [_ms_with_commit("ms-2", "in_progress")]})
    cmd_deploy.cmd_deploy_record()
    dep = _last_deploy(project_dir)
    assert dep["type"] == "minor"
    assert dep["newly_completed_ms"] == []
    assert dep["patch_ms"] == ["ms-2"]
    assert dep["target_ids"] == ["ms-2"]


def test_previously_deployed_not_newly_completed(project_dir):
    """A done milestone already shipped in a prior deploy is patched, not
    re-counted as newly completed."""
    _write(project_dir, {"name": "t",
                         "milestones": [_ms_with_commit("ms-3", "done")],
                         "deployments": [{"id": "d-1", "type": "major",
                                          "git_hash": "0000000",
                                          "newly_completed_ms": ["ms-3"]}]})
    cmd_deploy.cmd_deploy_record()
    dep = _last_deploy(project_dir)
    assert dep["type"] == "minor"
    assert dep["newly_completed_ms"] == []
    assert dep["patch_ms"] == ["ms-3"]
    assert dep["target_ids"] == ["ms-3"]


def test_generic_over_non_milestone_deliverable_class(project_dir):
    """The milestone hardcode is gone: a sales project whose commits land on a
    deliverable-bearing Opportunity-like class ('deal') gets ATTRIBUTION
    (target_ids) even with no milestone at all — but the deploy stays MINOR because
    'deal' declares no completion terminal states (the product-semantics decision
    is deferred to parent/user, ms-164 DM 2026-09-03)."""
    deal = td.build_descriptor(
        kind="deal", label="商談", dtype="single-shot",
        id_prefix="deal-", collection="deals",
        deliverable={"kind": "pipeline", "projector": "rollup"})
    data = {"name": "t", "profession": "sales"}
    assert td.append_descriptor(data, deal) == []
    data["deals"] = [{"id": "deal-1", "status": "closed_won", "title": "big",
                      "entries": [{"id": "e-1", "type": "commit",
                                   "meta": {"hash": "cafef00"}}]}]
    _write(project_dir, data)

    cmd_deploy.cmd_deploy_record()
    dep = _last_deploy(project_dir)
    # Attribution is generic — the deal is a visible worked-target now.
    assert dep["target_ids"] == ["deal-1"]
    assert dep["patch_ms"] == ["deal-1"]
    # Completion is deferred — a class without declared terminal states never
    # triggers a major deploy, even when its status reads "done"-ish.
    assert dep["newly_completed_ms"] == []
    assert dep["type"] == "minor"
