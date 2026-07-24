"""ms-125 e-4088 (ms-122 目的達成レビュー指摘): `beacon target instances` (non-JSON
mode) must not crash when instances exist.

Background: cmd_target_instances read r["phase"], but target_engine.project_target
nests phase under detail (to match the shared-frame shape core/sales emit), so
the non-JSON listing raised KeyError 'phase' as soon as any instance existed.
--json worked (it dumps the raw rows), so no test caught it — the non-JSON CLI
path had no coverage. This exercises the real CLI end-to-end.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

BEACON = Path(__file__).resolve().parent.parent / "bin" / "beacon"

_CONTRACT_CLASS = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": []},
    "fields": [{"key": "counterparty", "label": "相手方", "type": "string",
                "required": True}],
    "phases": [{"key": "drafting", "label": "起草"},
               {"key": "signed", "label": "締結", "terminal": True}],
}


@pytest.fixture
def descriptor_project(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "DescriptorInstances",
        "summary": "",
        "profession": "backoffice",
        "milestones": [],
        "target_classes": [_CONTRACT_CLASS],
    }, ensure_ascii=False))
    return tmp_path


def _run(cwd, *args):
    return subprocess.run(
        ["bash", str(BEACON), *args],
        capture_output=True, text=True, cwd=str(cwd), env=dict(os.environ), timeout=30,
    )


def test_target_instances_nonjson_lists_without_crash(descriptor_project):
    # Create one instance, then list it in the non-JSON path.
    create = _run(descriptor_project, "target", "create", "--class", "contract",
                  "--label", "Acme 契約", "--field", "counterparty=Acme")
    assert create.returncode == 0, create.stdout + create.stderr

    listing = _run(descriptor_project, "target", "instances", "--class", "contract")
    combined = listing.stdout + listing.stderr
    assert listing.returncode == 0, combined
    assert "KeyError" not in combined, combined
    assert "ctr-" in listing.stdout, combined       # the instance id is shown
    assert "起草" in listing.stdout or "drafting" in listing.stdout, combined  # phase shown


def test_target_instances_nonjson_empty_is_clean(descriptor_project):
    listing = _run(descriptor_project, "target", "instances", "--class", "contract")
    combined = listing.stdout + listing.stderr
    assert listing.returncode == 0, combined
    assert "まだありません" in listing.stdout, combined
