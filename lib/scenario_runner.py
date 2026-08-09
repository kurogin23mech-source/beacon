"""ms-136 e-4698 — local-mode scenario runner.

Runs a real use-case journey against the **real** Beacon CLI in a throwaway
local-mode project, and asserts on what the CLI / project actually produced.
This is the "安いネット" (cheap net) of the auto-debug基盤: it exercises the
system as a user would — black-box, real ``beacon`` subprocess, real data flow
— rather than at function boundaries the way unit tests do (the gap that let
"全テスト緑なのに本番で顧客獲得タブが出ない" happen, SPEC 背景).

Scenario shape (握った contract, ms-136 e-4698 / leader 案B refined). A
scenario is a declarative, diffable (方針7) asset with three honest kinds of
step — separating what the *persona* does from what the *environment* does to
them:

    {
      "name": "初回打診→返信→前進",
      "spec_ref": "<doc_id>",            # which SPEC this journey derives from
      "seed": {"profession": "sales", "name": "...", "objective": "..."},
      "steps": [
        # kind=persona_cli — the persona's own operation, run as the REAL
        # production CLI (black box = the system under test). argv is the
        # `beacon` sub-command + args (no leading "beacon").
        {"kind": "persona_cli", "argv": ["opportunity", "add", "Acme"],
         "label": "商談を立てる"},

        # kind=inbound_stimulus — an ENVIRONMENT stimulus (the customer replied)
        # — NOT a persona operation, so it is not a CLI step. The runner feeds
        # it through the real ingest path via the inward_inject lib seam
        # (in-process control plane only; never a production CLI verb, so a real
        # project can never have a fake reply injected — data-immutability整合).
        {"kind": "inbound_stimulus", "target": "opp-1",
         "summary": "検討します、と返信", "channel": "email"},

        # kind=assert — an observation check. Every assert MUST carry the SPEC
        # 出典一文 it verifies (方針3: オラクルは SPEC 由来 + 出典引用). The
        # runner refuses an assert without spec_source — 構造で規律を担保する.
        {"kind": "assert", "assert": "json_path", "path": "ball",
         "value": "self",
         "spec_source": "SPEC §6: 最新 inbound → ボールは自分 (BALL_SELF)"},
      ],
    }

Boundary (方針4 = L2 まで / AC #3 外向き効果ゼロ): the journey runs in a local
-mode project (no ``.beacon/cloud.json``), so the CLI writes only to that
throwaway file and reaches no cloud store. Sending (Gmail/Calendar) is a Skill
+ MCP + 人間承認 concern, never the CLI, so a persona_cli step fires nothing
外向き; inbound_stimulus uses the no-outward inward_inject seam. Nothing here can
transmit — by construction, not by a flag.

Discard (AC "破棄ができる"): the project lives in an isolated throwaway
directory (a ``tempfile.mkdtemp`` when the caller passes none) and is never
persisted to a real / cloud store. The runner does not itself delete the dir —
it returns ``workdir`` in the report so a failing run stays inspectable (feeds
the e-4700 データフロー層 bisect); ephemeral temp dirs are reaped by the OS, and
tests use pytest's ``tmp_path`` (managed cleanup).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import inward_inject
import store_local

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BEACON_BIN = str(REPO_ROOT / "bin" / "beacon")

STEP_PERSONA_CLI = "persona_cli"
STEP_INBOUND_STIMULUS = "inbound_stimulus"
STEP_ASSERT = "assert"
VALID_STEP_KINDS = {STEP_PERSONA_CLI, STEP_INBOUND_STIMULUS, STEP_ASSERT}

# quality_signals reason types (ms-136 e-4699 / leader 論点3): an AC that could
# not be turned into an executable, observable assert is reported — but WHY it
# could not must be categorized, so a correctly out-of-scope AC is never
# mis-reported as a SPEC defect.
QS_NEEDS_REWRITE = "needs-observable-rewrite"   # SPEC 品質欠陥: 観測可能に書けてない
QS_OUT_OF_SCOPE = "out-of-scope-boundary"       # 方針4 で正しく除外 (欠陥ではない)
VALID_QS_REASONS = {QS_NEEDS_REWRITE, QS_OUT_OF_SCOPE}


class ScenarioError(Exception):
    """A scenario is malformed (bad step kind, missing required field, assert
    without provenance, mis-categorized quality signal). Raised before/while
    running — distinct from a journey *failing* its assertions, which is
    reported, not raised."""


def validate_scenario(scenario: dict) -> None:
    """Structural + discipline validation of a scenario, independent of running
    it. Enforces the握った contract so a malformed scenario is rejected before
    it is run OR saved as a diffable asset (single source of validation, reused
    by scenario_store.save_scenario). Raises ScenarioError on any violation.
    """
    if not isinstance(scenario, dict) or not isinstance(scenario.get("steps"), list):
        raise ScenarioError("scenario must be a dict with a 'steps' list")
    for i, step in enumerate(scenario["steps"]):
        if not isinstance(step, dict):
            raise ScenarioError(f"step {i}: must be an object")
        kind = step.get("kind")
        if kind not in VALID_STEP_KINDS:
            raise ScenarioError(
                f"step {i}: unknown kind {kind!r} (valid: {sorted(VALID_STEP_KINDS)})")
        if kind == STEP_PERSONA_CLI:
            if not isinstance(step.get("argv"), list) or not step["argv"]:
                raise ScenarioError(f"step {i}: persona_cli needs a non-empty argv list")
        elif kind == STEP_INBOUND_STIMULUS:
            if not step.get("target") or not step.get("summary"):
                raise ScenarioError(
                    f"step {i}: inbound_stimulus needs 'target' and 'summary'")
        else:  # STEP_ASSERT — both provenance axes required (方針3 + 論点2)
            if not (step.get("spec_source") or "").strip():
                raise ScenarioError(
                    f"step {i}: assert needs 'spec_source' (期待値の SPEC 出典一文)")
            if not (step.get("observation_basis") or "").strip():
                raise ScenarioError(
                    f"step {i}: assert needs 'observation_basis' (観測 field + "
                    "なぜ SPEC 概念に対応するか)")
    # quality_signals: optional, but each must be categorized (論点3)
    for j, qs in enumerate(scenario.get("quality_signals", []) or []):
        if not isinstance(qs, dict):
            raise ScenarioError(f"quality_signals[{j}]: must be an object")
        if qs.get("reason_type") not in VALID_QS_REASONS:
            raise ScenarioError(
                f"quality_signals[{j}]: reason_type must be one of "
                f"{sorted(VALID_QS_REASONS)} (欠陥=needs-observable-rewrite か "
                f"正しくスコープ外=out-of-scope-boundary か を区別する)")


# ---------------------------------------------------------------------------
# Project setup (throwaway, local-mode) — via the real `beacon init`
# ---------------------------------------------------------------------------

def _seed_project(workdir: Path, seed: dict, beacon_bin: str, base_env: dict) -> Path:
    """Stand up a throwaway local-mode project in ``workdir`` by running the
    real ``beacon init`` (single source of truth for project shape) driven by
    env vars, with stdin closed so no prompt blocks. Creates no cloud.json → the
    project stays local. Returns the project.json path."""
    seed = seed or {}
    profession = str(seed.get("profession", "dev") or "dev")
    env = dict(base_env)
    # Machine session: scenario setup is automated, not a human at a prompt.
    env["BEACON_SESSION_KIND"] = "machine"

    # `beacon init` is driven by FLAGS (the init dispatch parses --name/… and
    # then hands them to cmd_init as BEACON_INIT_*, clobbering any inherited env
    # — so flags, not env vars, are the non-interactive route). --storage local
    # writes no cloud.json → the project stays local.
    argv = [
        beacon_bin, "init",
        "--name", seed.get("name", "Scenario Project"),
        "--objective", seed.get("objective", "auto-debug scenario"),
        "--profession", profession,
        "--storage", "local",
        "--retro-day", seed.get("retro_day", "monday"),
    ]
    proc = subprocess.run(
        argv,
        cwd=str(workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    pf = workdir / ".beacon" / "project.json"
    if proc.returncode != 0 or not pf.exists():
        raise ScenarioError(
            f"seed failed (beacon init, profession={profession}): "
            f"exit={proc.returncode} stderr={proc.stderr.strip()[:400]}")
    return pf


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

def _run_persona_cli(step: dict, workdir: Path, beacon_bin: str, env: dict) -> dict:
    """Run one persona operation as the real CLI (black box) and capture the
    observable result (exit / stdout / stderr). Unless the step declares an
    ``expect_exit``, a non-zero exit is a journey failure — a real user
    operation that errors is exactly the use-case break this基盤 exists to
    catch."""
    argv = step.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ScenarioError("persona_cli step needs a non-empty argv list")
    proc = subprocess.run(
        [beacon_bin, *[str(a) for a in argv]],
        cwd=str(workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    expect_exit = step.get("expect_exit", 0)
    ok = proc.returncode == expect_exit
    out = {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "ok": ok,
    }
    if not ok:
        out["reason"] = (
            f"exit {proc.returncode} != expected {expect_exit}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return out


def _run_inbound_stimulus(step: dict, project_file: Path) -> dict:
    """Feed a擬似着信 (environment stimulus) through the real ingest path via the
    inward_inject seam (in-process, no CLI, no outward I/O). Reloads the project
    from disk first so it sees whatever the preceding CLI step wrote."""
    target = step.get("target")
    summary = step.get("summary")
    if not target or not summary:
        raise ScenarioError(
            "inbound_stimulus step needs 'target' and 'summary'")
    store = store_local.LocalStore(str(project_file))
    data = store.load_project()
    try:
        res = inward_inject.inject_inbound_communication(
            data, target, summary,
            channel=step.get("channel", "email"),
            body=step.get("body", ""),
            source_ref=step.get("source_ref", ""),
            source_url=step.get("source_url", ""),
            occurred_at=step.get("occurred_at", ""),
            at=step.get("at", ""))
    except ValueError as e:
        return {"ok": False, "reason": f"inject failed: {e}"}
    store.save_project(data)
    ok = True
    reason = None
    # Optional discipline: assert the arrival actually returned the ball.
    if step.get("expect_ingested") is True and not res["ingested"]:
        ok = False
        reason = (f"expected ingest (ball→self) but ball_after="
                  f"{res['ball_after']!r}")
    out = {"inject": res, "ok": ok}
    if reason:
        out["reason"] = reason
    return out


def _walk_json_path(obj, path: str):
    """Walk a dotted path (keys + integer indices) into parsed JSON. Returns a
    sentinel-free (value, found) tuple so ``null`` values are distinguishable
    from missing ones."""
    cur = obj
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None, False
        elif isinstance(cur, dict):
            if part not in cur:
                return None, False
            cur = cur[part]
        else:
            return None, False
    return cur, True


def _run_assert(step: dict, last_cli: Optional[dict]) -> dict:
    """Evaluate one observation assertion against the preceding persona_cli's
    output.

    Every assert must carry BOTH provenance axes (ms-136 e-4699 / leader 論点2),
    or the runner refuses it — so no oracle can be smuggled in without a
    reviewable trail on either axis:

    - ``spec_source`` — the SPEC 一文 that makes the *expected value* true (方針3:
      オラクルは SPEC 由来). This is "what is true".
    - ``observation_basis`` — which CLI command / field the value is *observed*
      through, and why that field corresponds to the SPEC's user-visible concept.
      This is "how it is observed". It closes the residual leak where an
      implementer-named field (e.g. ``.owner`` standing in for the SPEC's
      "ball") could green-light buggy code: the基盤 (何が真か / どう観測するか)
      both stay diff-reviewable, so a human/leader can challenge
      "asserted ``.owner`` as ball — is that really the SPEC's ball concept?".
    """
    spec_source = (step.get("spec_source") or "").strip()
    if not spec_source:
        raise ScenarioError(
            "assert step must carry 'spec_source' (方針3: オラクルは SPEC 由来 "
            "+ 出典一文) — assertion without expected-value provenance is refused")
    observation_basis = (step.get("observation_basis") or "").strip()
    if not observation_basis:
        raise ScenarioError(
            "assert step must carry 'observation_basis' (leader 論点2: どの CLI "
            "field で観測するか + なぜ SPEC 概念に対応するか) — assertion without "
            "observation provenance is refused")
    kind = step.get("assert")
    if last_cli is None and kind in ("exit_code", "stdout_contains",
                                     "stdout_not_contains", "json_path"):
        raise ScenarioError(
            f"assert '{kind}' has no preceding persona_cli output to check")

    def result(ok, reason=None):
        out = {"ok": ok, "assert": kind, "spec_source": spec_source,
               "observation_basis": observation_basis}
        if reason:
            out["reason"] = reason
        return out

    if kind == "exit_code":
        want = step.get("value", 0)
        got = last_cli["returncode"]
        return result(got == want, None if got == want
                      else f"exit {got} != {want}")
    if kind == "stdout_contains":
        want = str(step.get("value", ""))
        return result(want in last_cli["stdout"], None
                      if want in last_cli["stdout"]
                      else f"stdout does not contain {want!r}")
    if kind == "stdout_not_contains":
        want = str(step.get("value", ""))
        absent = want not in last_cli["stdout"]
        return result(absent, None if absent
                      else f"stdout unexpectedly contains {want!r}")
    if kind == "json_path":
        try:
            parsed = json.loads(last_cli["stdout"])
        except (json.JSONDecodeError, TypeError) as e:
            return result(False, f"stdout is not JSON: {e}")
        got, found = _walk_json_path(parsed, step.get("path", ""))
        if not found:
            return result(False, f"path {step.get('path')!r} not found")
        want = step.get("value")
        return result(got == want,
                      None if got == want else f"{got!r} != {want!r}")
    raise ScenarioError(f"unknown assert type: {kind!r}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_scenario(scenario: dict, *, workdir: Optional[str] = None,
                 beacon_bin: Optional[str] = None,
                 env: Optional[dict] = None) -> dict:
    """Run ``scenario`` end-to-end and return a structured report.

    Report shape::

        {
          "name": ..., "spec_ref": ..., "workdir": "<abs path>",
          "passed": bool,                # all steps ok
          "steps": [ {index, kind, label, ok, ...}, ... ],
          "failure": None | {index, kind, reason, spec_source?},  # first fail
        }

    ``passed`` is False as soon as any step fails; remaining steps still run so
    the report shows the whole journey (the e-4700 bisect reads all layers), but
    ``failure`` pins the *first* divergence — the honest "where it broke".
    """
    # Fail fast on a malformed scenario (single source of validation).
    validate_scenario(scenario)

    beacon_bin = beacon_bin or DEFAULT_BEACON_BIN
    base_env = dict(env if env is not None else os.environ)

    owns_dir = workdir is None
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="beacon-scenario-"))
    workdir.mkdir(parents=True, exist_ok=True)

    project_file = _seed_project(workdir, scenario.get("seed", {}),
                                 beacon_bin, base_env)

    step_reports: list = []
    last_cli: Optional[dict] = None
    failure: Optional[dict] = None

    for i, step in enumerate(scenario["steps"]):
        kind = step["kind"]
        label = step.get("label", "")
        if kind == STEP_PERSONA_CLI:
            r = _run_persona_cli(step, workdir, beacon_bin, base_env)
            last_cli = r
        elif kind == STEP_INBOUND_STIMULUS:
            r = _run_inbound_stimulus(step, project_file)
        else:  # STEP_ASSERT
            r = _run_assert(step, last_cli)

        entry = {"index": i, "kind": kind, "label": label, **r}
        step_reports.append(entry)
        if not r.get("ok", False) and failure is None:
            failure = {"index": i, "kind": kind,
                       "reason": r.get("reason", "step failed")}
            if r.get("spec_source"):
                failure["spec_source"] = r["spec_source"]
            if r.get("observation_basis"):
                failure["observation_basis"] = r["observation_basis"]

    return {
        "name": scenario.get("name", ""),
        "spec_ref": scenario.get("spec_ref", ""),
        "workdir": str(workdir),
        "owns_workdir": owns_dir,
        "passed": failure is None,
        "steps": step_reports,
        "failure": failure,
    }
