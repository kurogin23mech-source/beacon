"""ms-136 e-4701 — cloud-ephemeral scenario runner.

The "高いネット" (expensive net) of the auto-debug基盤: for journeys whose
fidelity is only reachable on real cloud (store 並行 / server API / schema —
paths local-mode cannot reproduce, SPEC 方針6), stand up a throwaway cloud
project, run the journey against it, and tear it down — every run disposable.

This deliberately does NOT build permanent auth/billing infra (SPEC scope: 最小の
立て→teardown から始める). It rides the safety基盤 ms-123 e-4029 already built:

  - ``cloud_write_guard.disposable_project`` — creates the project and
    GUARANTEES archive-on-exit via a ``finally`` (structural teardown: a crash
    mid-journey still cleans up). Auth = the logged-in user's credentials
    (reused, not rebuilt).
  - ``cloud_write_guard.guard_prod_project_write`` — a test-context write to
    production physically raises. We ADD an explicit non-prod refusal here too
    (belt-and-suspenders), so a cloud-ephemeral run can never target production
    regardless of context.
  - the ephemeral project is named with a ``-test-`` token so the existing
    orphan classifier (``project_cleanup`` / ``beacon project orphans``)
    reliably flags any residue as a backstop, and ``disposable_project``'s
    cleanup surfaces a failure to stderr (never a silent leak).

Verification boundary (ms-136 e-4701, leader 裁定): dev tests mock the ApiClient
— no live cloud is touched during development; the real cloud-ephemeral run is
user-verified. The prod refusal + disposable teardown are unit-tested; the live
journey is not run in CI.

Minimal start (leader 承認, YAGNI): cloud-tier scenarios are persona_cli + assert
only. ``inbound_stimulus`` (擬似着信) is local-data based (inward_inject) and is
refused here — cloud 固有 paths (schema / concurrency / API) rarely need reply
injection; a cloud inject seam is added when a concrete journey needs it.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import cloud_write_guard
import scenario_runner

ScenarioError = scenario_runner.ScenarioError

# Ephemeral project name prefix. The ``-test-`` token is deliberate: the
# existing orphan classifier (lib/project_cleanup) flags ``*-test-*`` as residue,
# so a leaked ephemeral project is reliably caught by ``beacon project orphans``
# even if teardown somehow fails (leader 裁定: 確実に拾える識別性 + no silent leak).
TEST_RESIDUE_PREFIX = "scenario-ephemeral-test-"


def _default_client_factory():
    """Build an ApiClient from the logged-in user's credentials + the active
    (non-prod expected) api_url. Returns ``(client, api_url)``. Raises
    ScenarioError if not logged in."""
    from auth import load_credentials
    from api_client import ApiClient
    from commands_shared import _resolve_active_api_url, _extract_token
    creds = load_credentials()
    if creds is None:
        raise ScenarioError("not logged in — run: beacon auth login "
                            "(cloud-ephemeral needs cloud credentials)")
    api_url = _resolve_active_api_url()
    return ApiClient(api_url, _extract_token(creds)), api_url


def _run_cloud_steps(scenario: dict, workdir: Path, ephemeral_id: str,
                     beacon_bin: Optional[str], env: Optional[dict]) -> dict:
    """Run the journey's steps against the (already-created) cloud project.
    Reuses the local runner's persona_cli / assert executors — the only
    difference is the cwd carries a cloud.json, so the real CLI operates in
    cloud mode (talks to the server API)."""
    bin_ = beacon_bin or scenario_runner.DEFAULT_BEACON_BIN
    import os as _os
    base_env = dict(env if env is not None else _os.environ)
    step_reports: list = []
    last_cli: Optional[dict] = None
    failure: Optional[dict] = None
    for i, step in enumerate(scenario["steps"]):
        kind = step["kind"]
        if kind == scenario_runner.STEP_PERSONA_CLI:
            r = scenario_runner._run_persona_cli(step, workdir, bin_, base_env)
            last_cli = r
        else:  # STEP_ASSERT (inbound_stimulus already rejected upfront)
            r = scenario_runner._run_assert(step, last_cli, workdir)
        entry = {"index": i, "kind": kind, "label": step.get("label", ""), **r}
        step_reports.append(entry)
        if not r.get("ok", False) and failure is None:
            failure = {"index": i, "kind": kind,
                       "reason": r.get("reason", "step failed")}
            for k in ("spec_source", "observation_basis"):
                if r.get(k):
                    failure[k] = r[k]
    return {"steps": step_reports, "failure": failure, "passed": failure is None}


def run_cloud_scenario(scenario: dict, *,
                       client_factory: Optional[Callable] = None,
                       beacon_bin: Optional[str] = None,
                       env: Optional[dict] = None,
                       now: Optional[Callable[[], float]] = None,
                       _step_runner: Optional[Callable] = None) -> dict:
    """Run a cloud-ephemeral scenario against a throwaway NON-PROD cloud project.

    Returns the same report shape as the local runner, plus ``mode="cloud"`` and
    ``ephemeral_project_id``. Teardown (archive) is guaranteed by
    ``disposable_project`` even if the journey raises.
    """
    scenario_runner.validate_scenario(scenario)
    # Minimal start: inbound_stimulus is not supported in cloud tier yet.
    for i, step in enumerate(scenario["steps"]):
        if step.get("kind") == scenario_runner.STEP_INBOUND_STIMULUS:
            raise ScenarioError(
                f"step {i}: inbound_stimulus is not supported in cloud-ephemeral "
                "tier yet (use local tier; a cloud inject seam is added when a "
                "journey genuinely needs it — YAGNI, SPEC 方針6 最小開始)")

    client, api_url = (client_factory or _default_client_factory)()
    # Explicit non-prod refusal (belt with guard_prod_project_write): a cloud
    # -ephemeral run must never materialize a throwaway project on production.
    if cloud_write_guard.is_prod_api_url(api_url):
        raise ScenarioError(
            f"refusing cloud-ephemeral run against production ({api_url}) — "
            "point at a non-prod endpoint (the throwaway project must not land "
            "in the production directory)")

    stamp = int((now or time.time)())
    ephemeral_id = f"{TEST_RESIDUE_PREFIX}{stamp}"
    seed = scenario.get("seed", {}) or {}
    name = seed.get("name", "scenario ephemeral")
    objective = seed.get("objective", "cloud-ephemeral scenario")

    runner = _step_runner or _run_cloud_steps
    # disposable_project: create the non-prod project, GUARANTEE archive on exit.
    with cloud_write_guard.disposable_project(client, ephemeral_id, name, objective):
        workdir = Path(tempfile.mkdtemp(prefix="beacon-cloud-scenario-"))
        (workdir / ".beacon").mkdir(parents=True, exist_ok=True)
        (workdir / ".beacon" / "cloud.json").write_text(
            json.dumps({"project_id": ephemeral_id, "api_url": api_url}),
            encoding="utf-8")
        inner = runner(scenario, workdir, ephemeral_id, beacon_bin, env)

    return {
        "name": scenario.get("name", ""),
        "spec_ref": scenario.get("spec_ref", ""),
        "workdir": str(workdir),
        "mode": "cloud",
        "ephemeral_project_id": ephemeral_id,
        "passed": inner["passed"],
        "steps": inner["steps"],
        "failure": inner["failure"],
    }
