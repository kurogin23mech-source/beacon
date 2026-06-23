"""Regression test pin for WS push 構造修復 (ms-84 / e-2303).

Background — 2026-06-23 dogfood (= e-2040 ms-84 Phase 6 検証) で確認した病理:
cloud mode で `beacon task add` を打っても production の Web UI に live 反映
されない。 server/app.py の `_ws_connections` は process-local dict
(L6942) で、 Cloud Run の default scaling が multi-instance に膨らむと

  * HTTP POST (= write) は LB 経由でランダム instance に着地
  * WS connection は別 instance に保持されている
  * 着地 instance の `_ws_connections[project_id]` は空
  * `_broadcast_project_after_write` (L6741) が early return
  * Client は永遠に dark のまま

という構造で broadcast が物理的に届かない経路が存在した。

修復方針 (= 応急処置 A、 SPEC `ms-84 / e-2303 SPEC: WS push 構造修復` 参照):
Cloud Run service を `--min-instances=1 --max-instances=1` で single-instance
に固定し、 process-local dict が全 client を見える状態を作る。 trade-off は
月額 $5〜$20 (= always-on 1 instance) + 水平 scale 不能。 1 人運用 / 低
トラフィックでは問題なし。 本格運用時には案 B (= Pub/Sub or Redis cross-
instance fanout) に置換する。

このテストは workflow YAML を直接 parse し、 Cloud Run deploy step の
gcloud コマンドに `--min-instances` / `--max-instances` が両方 `1` で
含まれていることを assert する。 別の retry 経路 (= step を split したり
gcloud run services update を別 step に分けたりする refactor) で
single-instance 制約が **silently 外れる** regression を構造的に防ぐ。
"""

from __future__ import annotations

import os
import re

import yaml


WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    ".github",
    "workflows",
    "deploy-cloud-run.yml",
)


def _load_workflow() -> dict:
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_deploy_step_run() -> str:
    """Return the `run:` script body of the Cloud Run deploy step."""
    workflow = _load_workflow()
    jobs = workflow.get("jobs", {})
    deploy_job = jobs.get("deploy")
    assert deploy_job is not None, "deploy job missing in workflow"
    steps = deploy_job.get("steps", [])
    for step in steps:
        if step.get("name") == "Deploy to Cloud Run":
            run = step.get("run") or ""
            assert isinstance(run, str), "deploy step run body is not a string"
            return run
    raise AssertionError("'Deploy to Cloud Run' step not found in workflow")


def test_deploy_step_pins_min_instances_to_one():
    """`--min-instances=1` is present (= cold start なく WS subscriber を常駐)."""
    run = _find_deploy_step_run()
    # `--min-instances=1` を単純文字列で検査。 後続の flag が同じ行 / 別行に
    # 折り返されても通る (= YAML scalar が改行を保持する場合があるため正規表現
    # で `\b1\b` ではなく `=1` 等値を見る)。
    assert re.search(r"--min-instances[=\s]+1\b", run), (
        "Cloud Run deploy step must pin --min-instances=1 to keep the WS "
        "subscriber-holding process alive across writes (ms-84 / e-2303). "
        f"Got run body:\n{run}"
    )


def test_deploy_step_caps_max_instances_to_one():
    """`--max-instances=1` is present (= process-local dict を fanout 不要に保つ)."""
    run = _find_deploy_step_run()
    assert re.search(r"--max-instances[=\s]+1\b", run), (
        "Cloud Run deploy step must cap --max-instances=1 so the LB never "
        "splits write traffic and WS traffic across instances "
        "(ms-84 / e-2303). Got run body:\n{run}"
    )


def test_deploy_step_keeps_cache_bust():
    """CACHE_BUST 既存 contract (= e-712) を壊していない regression guard."""
    run = _find_deploy_step_run()
    assert "CACHE_BUST=" in run, (
        "Cloud Run deploy step must still pass CACHE_BUST (e-712 stale layer "
        "guard). e-2303 修復は CACHE_BUST を温存したまま行うべき。 "
        f"Got run body:\n{run}"
    )
