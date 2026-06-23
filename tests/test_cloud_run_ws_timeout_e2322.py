"""ms-84 / e-2322 — Cloud Run --timeout=3600 (WS connection lifetime) pin.

Verifies the deploy workflow includes ``--timeout=3600`` in the gcloud
run deploy invocation. The default Cloud Run request timeout is 5 minutes
(300 seconds), but WebSockets are treated as one long-running request,
so the default kills connections every ~5 min and triggers the
client-side reconnect loop. During the reconnect dead window any
broadcast misses the (now-disconnected) client, defeating the
single-instance fix from e-2303.

These are pure source-level structural pins — same regime as
``test_ws_push_cross_instance.py`` (e-2303): no Cloud Run RPC, no
external network. The pins only assert that the workflow YAML carries
the flag, so a future refactor that drops it surfaces in CI.
"""

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-cloud-run.yml"


def _read_workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_deploy_workflow_includes_timeout_flag():
    """The gcloud run deploy step must carry ``--timeout=3600`` so the
    WebSocket connection lifetime is 60 minutes instead of the 5-minute
    Cloud Run default."""
    src = _read_workflow()
    assert "--timeout=3600" in src, (
        "deploy-cloud-run.yml must pass --timeout=3600 to gcloud run deploy "
        "(ms-84 / e-2322 — extend Cloud Run request timeout to 60 min so the "
        "WebSocket reconnect loop doesn't tick every 5 min). Default 300s "
        "kills WS connections, dropping live broadcasts in the dead window."
    )


def test_timeout_flag_sits_inside_deploy_step():
    """Make sure the flag is part of the ``Deploy to Cloud Run`` step
    rather than e.g. a stray comment — a misplaced flag is a silent
    regression because gcloud would never see it.
    """
    src = _read_workflow()
    deploy_marker = "gcloud run deploy beacon-api-prod"
    assert deploy_marker in src
    deploy_start = src.index(deploy_marker)
    # The flag must appear in the next ~25 lines (= same multi-line command).
    deploy_window = src[deploy_start:deploy_start + 1500]
    assert "--timeout=3600" in deploy_window, (
        "--timeout=3600 must live inside the gcloud run deploy command, not "
        "elsewhere in the workflow file."
    )


def test_e2303_single_instance_flags_still_present():
    """Regression guard for e-2303: don't accidentally drop the
    single-instance pins while adding the timeout flag."""
    src = _read_workflow()
    assert "--min-instances=1" in src, (
        "e-2303 single-instance pin lost — broadcast fanout regression. "
        "Re-add --min-instances=1 to the deploy step."
    )
    assert "--max-instances=1" in src, (
        "e-2303 single-instance pin lost — broadcast fanout regression. "
        "Re-add --max-instances=1 to the deploy step."
    )
