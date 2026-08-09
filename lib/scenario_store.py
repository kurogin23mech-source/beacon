"""ms-136 e-4699 — scenario store: validate + save/load/list generated scenarios.

The deterministic half of the SPEC→scenario pipeline. Generation is an LLM step
(the /beacon-scenario-gen Skill drives Claude to read a SPEC and emit a
scenario) — but non-determinism is isolated to *generation time* (leader 論点1
芯): once produced, a scenario is a decision-free, replayable asset. This module
is that asset's gatekeeper and home — it validates a generated scenario against
the held contract and writes it as a diffable repo file (方針7), so it can serve
as a deterministic regression / attainment artifact (e-4702) forever after.

Storage layout (方針7 diffable): ``scenarios/<milestone>/<slug>.json`` under the
repo root, pretty-printed and key-stable so ``git diff`` shows exactly what an
oracle changed — the spec_source / observation_basis of every assertion is
reviewable in the diff (leader 論点2: 何が真か / どう観測するか の両軸を人間が
検分できる).

A scenario carries, on top of the runner's step contract:
  - ``milestone``   : the MS this journey belongs to (决定 the save path + link)
  - ``spec_ref``    : the SPEC doc id the oracles are traced to (方針3 provenance)
  - ``quality_signals`` (optional): ACs that could NOT be turned into an
    executable, observable assert — each categorized (論点3) as
    ``needs-observable-rewrite`` (SPEC 品質欠陥) or ``out-of-scope-boundary``
    (方針4 で正しく除外, not a defect). This is the SPEC-quality feedback loop.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import scenario_runner

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIRNAME = "scenarios"

# Re-export so callers have one import for the whole scenario surface.
ScenarioError = scenario_runner.ScenarioError
QS_NEEDS_REWRITE = scenario_runner.QS_NEEDS_REWRITE
QS_OUT_OF_SCOPE = scenario_runner.QS_OUT_OF_SCOPE
VALID_QS_REASONS = scenario_runner.VALID_QS_REASONS


def _slugify(name: str) -> str:
    """A filesystem-safe, diff-stable slug from a scenario name. Keeps unicode
    letters (Japanese names stay readable) but drops path/space/punctuation."""
    s = (name or "").strip().lower()
    s = re.sub(r"[\s/\\]+", "-", s)
    s = re.sub(r"[^0-9a-z぀-ヿ一-鿿\-_]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    return s or "scenario"


def validate_saveable(scenario: dict) -> None:
    """Runner-contract validation + the extra fields a *saved* scenario needs
    (milestone + spec_ref, so it can be filed under an MS and its oracles traced
    to a SPEC). Raises ScenarioError."""
    scenario_runner.validate_scenario(scenario)
    if not (scenario.get("milestone") or "").strip():
        raise ScenarioError(
            "saveable scenario needs a 'milestone' (どの MS の journey か — "
            "保存先 scenarios/<ms>/ と link に使う)")
    if not (scenario.get("spec_ref") or "").strip():
        raise ScenarioError(
            "saveable scenario needs a 'spec_ref' (オラクルの出所 SPEC doc id, 方針3)")


def scenario_path(scenario: dict, *, repo_root: Optional[Path] = None) -> Path:
    """The diffable file path a scenario saves to: scenarios/<ms>/<slug>.json."""
    root = Path(repo_root or REPO_ROOT)
    ms = str(scenario["milestone"]).strip()
    slug = _slugify(scenario.get("name", ""))
    return root / SCENARIOS_DIRNAME / ms / f"{slug}.json"


def save_scenario(scenario: dict, *, repo_root: Optional[Path] = None) -> Path:
    """Validate then persist ``scenario`` as a diffable JSON asset. Returns the
    written path. Overwrites an existing file of the same slug (regeneration =
    a reviewable diff, not a duplicate)."""
    validate_saveable(scenario)
    path = scenario_path(scenario, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8")
    return path


def load_scenario(path) -> dict:
    """Load and validate a saved scenario (so a hand-edited / drifted file is
    caught before it runs)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_saveable(data)
    return data


def list_scenarios(*, repo_root: Optional[Path] = None,
                   milestone: Optional[str] = None) -> list:
    """List saved scenario files (optionally for one MS). Returns dicts with
    ``path`` / ``milestone`` / ``name`` / ``spec_ref`` / ``step_count`` /
    ``quality_signal_count`` — a cheap index without running anything."""
    root = Path(repo_root or REPO_ROOT) / SCENARIOS_DIRNAME
    if not root.exists():
        return []
    globber = (root / milestone).glob("*.json") if milestone else root.glob("*/*.json")
    out = []
    for p in sorted(globber):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "path": str(p),
            "milestone": data.get("milestone", p.parent.name),
            "name": data.get("name", p.stem),
            "spec_ref": data.get("spec_ref", ""),
            "step_count": len(data.get("steps", []) or []),
            "quality_signal_count": len(data.get("quality_signals", []) or []),
        })
    return out
