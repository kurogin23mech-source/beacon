"""Regression: retro's weekly-deploy facet surfaces the generic worked-Target
attribution, not milestone-only (ms-164 e-5948).

Background
----------
``cmd_deploy.py`` (e-5946) stamps ``target_ids`` on every deploy record — the
AUTHORITATIVE set of deliverable-bearing Targets the deploy shipped, spanning
ALL classes. The legacy ``milestones`` field is milestone-only and stays empty
for a non-dev project (e.g. sales, whose deliverable-bearing class is the
opportunity). Before e-5948 the retro prepare payload read only
``dep.get("milestones", [])``, so a non-milestone project's deploys looked
Target-less in the weekly retro. This locks in that:

  * ``target_ids`` is read back generically (mirrors cmd_deploy's own re-read
    idiom), so a non-milestone Target attribution is preserved.
  * the legacy ``milestones`` field is still passed through unchanged for
    back-compat readers.
  * for a dev project the two agree (no behavior change there).
"""

import json

import cmd_retro


def _run_prepare(data, monkeypatch, capsys):
    monkeypatch.setattr(cmd_retro, "load_project", lambda: data)
    monkeypatch.setattr(cmd_retro, "_load_local_documents", lambda: [])
    monkeypatch.delenv("BEACON_SINCE", raising=False)
    monkeypatch.delenv("BEACON_UNTIL", raising=False)
    monkeypatch.delenv("BEACON_RETRO_CATCH_UP", raising=False)
    cmd_retro.cmd_retro_prepare()
    return json.loads(capsys.readouterr().out)


def test_weekly_deploy_surfaces_generic_target_ids(monkeypatch, capsys):
    """A sales-shaped deploy (milestone-only field empty, target_ids carries
    the opportunity) exposes its Target via ``target_ids`` in the retro payload."""
    data = {
        "name": "T",
        "profession": "dev",
        "milestones": [],
        "deployments": [
            {
                "id": "e-100",
                "type": "minor",
                "date": "2026-09-04T10:00:00Z",
                "milestones": [],          # legacy milestone-only field, empty
                "target_ids": ["opp-3"],   # generic authoritative attribution
                "newly_completed_ms": [],
                "description": "ship",
            }
        ],
    }
    out = _run_prepare(data, monkeypatch, capsys)
    assert len(out["deploys"]) == 1
    dep = out["deploys"][0]
    assert dep["target_ids"] == ["opp-3"]
    assert dep["milestones"] == []  # legacy field passed through, still empty


def test_weekly_deploy_missing_target_ids_defaults_empty(monkeypatch, capsys):
    """An old deploy record without a ``target_ids`` key defaults to [] rather
    than raising, keeping the read-back tolerant of pre-e-5946 records."""
    data = {
        "name": "T",
        "profession": "dev",
        "milestones": [],
        "deployments": [
            {
                "id": "e-99",
                "type": "major",
                "date": "2026-09-04T10:00:00Z",
                "milestones": ["ms-1"],
                # no target_ids key at all (pre-e-5946 record)
                "newly_completed_ms": [],
                "description": "old deploy",
            }
        ],
    }
    out = _run_prepare(data, monkeypatch, capsys)
    dep = out["deploys"][0]
    assert dep["target_ids"] == []
    assert dep["milestones"] == ["ms-1"]
