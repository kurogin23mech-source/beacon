"""release.yml follows the VPS migration — no dead Cloud Run fan-out (ms-105 e-4694).

Production moved to a pull-based VPS (scripts/vps-pull-deploy.sh); the auto-deploy
timer is intentionally disabled (manual operation since 2026-07-28). release.yml
used to fan out to deploy-cloud-run.yml — a dead target (beacon-ai.dev points at
the VPS), so releases looked "shipped" while prod stayed stale (v0.61.0 hit this:
prod was 3 days behind, missing the acquisition tab). These pin that release.yml
no longer deploys to Cloud Run and instead surfaces the manual VPS deploy command.
"""
from pathlib import Path

REL = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


def test_release_yml_does_not_fan_out_to_cloud_run():
    body = REL.read_text(encoding="utf-8")
    assert "deploy-cloud-run.yml --ref main" not in body, (
        "release.yml still dispatches deploy-cloud-run — that target is dead "
        "(prod is the VPS). Remove it (ms-105 e-4694)."
    )


def test_release_yml_surfaces_manual_vps_deploy():
    body = REL.read_text(encoding="utf-8")
    assert "beacon-deploy.service" in body, "release.yml must show the manual VPS deploy command"
    assert "本番反映は手動" in body


def test_release_yml_is_valid_yaml():
    import yaml
    yaml.safe_load(REL.read_text(encoding="utf-8"))
