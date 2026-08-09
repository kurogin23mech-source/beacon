"""ms-136 e-4700 — dataflow-layer bisect (障害層の局所化).

When a scenario fails, this names **the boundary where the observed value first
diverged** — turning the "6段手探り" (2026-07-31: 顧客獲得タブ不在→真因まで6段の
手動調査) into a one-shot layer localization (SPEC 方針8 達成基準). This is the
machine form of CLAUDE.md's debug principle (まずデータフロー全体 → 誰が値を
変えられるか → 構造で防ぐ).

This is a **localization** tool, not a correctness re-judgment (leader 裁定):
the oracle already said *what is true* (SPEC-derived, e-4699). Two provenance
sources with strictly separate roles (do not conflate — a green-but-wrong
baseline as authority would re-open the盲点 e-4699 closed):

- **SPEC concept** = the AUTHORITY on "is the FINAL boundary SPEC-wrong". The
  failing assert already carries this (spec_source). expected_provenance="spec".
- **passing-run differential** = a LOCALIZATION ACCELERATOR only — "where along
  the way did the failing run diverge from a known-green run". It never decides
  correctness; expected_provenance="differential" and the expected value is
  labelled "baseline behaved thus", NOT "SPEC requires thus".

Layers (local-mode = the runner's土俵): the server-API layer is intentionally
absent — in local mode store = the one project.json the engine writes directly,
so fabricating an API layer would be dishonest (added in e-4701 for cloud).

    L_cli     CLI dispatch / arg parsing   — observed via exit code + stderr
    L_engine  commands.py computation      — observed via stdout + raw store
    L_store   local persistence            — observed via raw project.json re-read

Non-invasive by design (方針1 黒箱忠実度): observes only at boundaries
(exit / stderr / stdout / raw-store re-read). It never imports or calls the
code-under-test's engine (e.g. derive_ball) to compute an expectation —
instrumenting would couple the debugger to the implementation and re-open the
leak at the layer-prob level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

LAYER_CLI = "L_cli"
LAYER_ENGINE = "L_engine"
LAYER_STORE = "L_store"
# L_api (ms-136 e-4701): in cloud mode the store IS the server API/DB, an
# independent layer. In local mode store = the one project.json the engine
# writes, so no API layer exists (we do not fabricate one — e-4700 黒箱誠実さ).
LAYER_API = "L_api"

# stderr signatures that mean the persona's operation is not valid against the
# CLI surface (arg / dispatch / interface gap) — the 顧客獲得タブ / milestone
# -priority class of use-case break. Distinct from an engine computation error.
_CLI_SURFACE_SIGNS = ("usage:", "unknown ", "unknown flag", "invalid choice",
                      "is required", "unrecognized", "no such")
_ENGINE_SIGNS = ("traceback", "error:")


def _load_raw_store(workdir: str) -> Optional[dict]:
    """Re-read the throwaway project's raw project.json (store layer). Returns
    None if unreadable (itself a store-layer signal)."""
    if not workdir:
        return None
    pf = Path(workdir) / ".beacon" / "project.json"
    try:
        return json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _all_communications(store: dict) -> list:
    """Every communication in the raw store (target-level + nested under work
    items), read structurally — no engine call."""
    out = []
    for coll in ("opportunities", "accounts"):
        for tgt in store.get(coll, []) or []:
            out.extend(tgt.get("communications", []) or [])
            for child_key in ("activities", "nurturings"):
                for wi in tgt.get(child_key, []) or []:
                    out.extend(wi.get("communications", []) or [])
    return out


def _has_injected_communication(store: dict) -> bool:
    """Structurally: does the raw store contain a擬似着信 (source.injected=True)?
    Proof the injection persisted, without asking the engine."""
    return any((c.get("source") or {}).get("injected")
               for c in _all_communications(store))


def _diagnose_persona_cli(fstep: dict) -> dict:
    stderr = (fstep.get("stderr") or "").lower()
    stdout = (fstep.get("stdout") or "").lower()
    blob = stderr + "\n" + stdout
    if any(s in blob for s in _CLI_SURFACE_SIGNS):
        layer = LAYER_CLI
        why = ("the persona's operation is not valid against the CLI surface "
               "(arg/dispatch/interface gap) — the operation a real user would "
               "do is unsupported or changed")
    elif any(s in blob for s in _ENGINE_SIGNS):
        layer = LAYER_ENGINE
        why = "the CLI dispatched but the engine (commands.py) errored while computing"
    else:
        layer = LAYER_ENGINE
        why = "the command failed after dispatch (no CLI-surface signature); engine-side"
    return {
        "responsible_layer": layer,
        "boundary": "persona_cli exit",
        "observed": f"exit {fstep.get('returncode')}: "
                    f"{(fstep.get('stderr') or fstep.get('stdout') or '').strip()[:200]}",
        "expected": "exit 0 (a persona operation in the journey should succeed)",
        "expected_provenance": "journey-contract",
        "why": why,
    }


def _diagnose_inbound(fstep: dict, store: Optional[dict]) -> dict:
    # The inbound_stimulus step itself failed. Distinguish injection failure
    # from ingest-processing failure (leader 裁定 addition).
    inj = fstep.get("inject") or {}
    reason = fstep.get("reason", "")
    if store is not None and _has_injected_communication(store):
        # The擬似着信 IS persisted, yet the step flagged failure (e.g. ball did
        # not return) → ingest / derivation processing is wrong, not injection.
        return {
            "responsible_layer": LAYER_ENGINE,
            "boundary": "ingest processing (derive after inbound)",
            "observed": f"ball_after={inj.get('ball_after')!r} (ingested={inj.get('ingested')})",
            "expected": "the injected inbound arrival returns the ball to us",
            "expected_provenance": "spec",
            "why": ("the擬似着信 IS in the store (injection persisted) but the "
                    "derived outcome is wrong — the ingest/derivation processing "
                    "is at fault (who_has_the_ball-class hole)"),
        }
    return {
        "responsible_layer": LAYER_STORE if store is not None else LAYER_ENGINE,
        "boundary": "inbound injection persistence",
        "observed": f"no injected communication in store; reason={reason!r}",
        "expected": "the injected inbound communication is persisted to the store",
        "expected_provenance": "spec",
        "why": ("the擬似着信 did not reach the store — injection/persistence "
                "failed (harness or store layer), not ingest processing"),
    }


def _diagnose_assert(fstep: dict, steps: list, failure: dict, store: Optional[dict],
                     baseline_report: Optional[dict]) -> dict:
    # Final-boundary authority is ALWAYS the SPEC (the assert's spec_source).
    observed_reason = fstep.get("reason", "")
    out = {
        "responsible_layer": None,
        "boundary": "observation (assert)",
        "observed": observed_reason,
        "expected": f"per SPEC: {fstep.get('spec_source', '')}",
        "expected_provenance": "spec",
        "why": "",
    }

    # Ingest special case (leader 裁定): a failed assert downstream of an
    # inbound_stimulus, where the擬似着信 is in the store but the observed value
    # is wrong → the reading/derivation (engine/observation) is at fault, not
    # the data. This localizes the who_has_the_ball-class hole in one shot.
    had_inbound = any(s.get("kind") == "inbound_stimulus"
                      for s in steps[:failure["index"]])
    if had_inbound and store is not None and _has_injected_communication(store):
        out["responsible_layer"] = LAYER_ENGINE
        out["boundary"] = "read/derive (observation command)"
        out["why"] = ("the擬似着信 IS in the store, so the underlying data is "
                      "present — the observation command's read/derivation "
                      "produced the wrong value (e.g. reading a stale field "
                      "instead of the SPEC concept's realization)")
        return _with_differential(out, fstep, baseline_report)

    # General case: re-read store; if the store is unreadable the fault is store
    # layer; otherwise, without a code call we can only place it at the
    # observation boundary and lean on the differential to localize further.
    if store is None:
        out["responsible_layer"] = LAYER_STORE
        out["why"] = "the raw store (project.json) is unreadable — store-layer fault"
        return out
    out["responsible_layer"] = LAYER_ENGINE
    out["why"] = ("observed value diverged from the SPEC expectation; the raw "
                  "store is readable, so localize between engine (wrote wrong) "
                  "and read (derived wrong) via the differential below")
    return _with_differential(out, fstep, baseline_report)


def _with_differential(out: dict, fstep: dict, baseline_report: Optional[dict]) -> dict:
    """Attach a localization hint from a passing baseline, if given. This NEVER
    overrides the SPEC authority — it only says "where the failing run diverged
    from a known-green run", labelled as differential provenance."""
    if not baseline_report:
        return out
    # find the same-index step in the baseline for a per-step diff
    idx = None
    for s in baseline_report.get("steps", []):
        if s.get("kind") == "assert" and s.get("spec_source") == fstep.get("spec_source"):
            idx = s
            break
    if idx is not None:
        out["differential"] = {
            "expected_provenance": "differential",
            "baseline_behaved": f"passing baseline's same assert: ok={idx.get('ok')}",
            "note": ("localization accelerator only — 'baseline behaved thus', "
                     "NOT 'SPEC requires thus'"),
        }
    return out


def diagnose_failure(scenario: dict, report: dict, *,
                     baseline_report: Optional[dict] = None) -> dict:
    """Localize the dataflow layer a failed scenario diverged at.

    Returns a structured diagnosis::

        {
          "diagnosable": True,
          "failing_step": {index, kind, label},
          "responsible_layer": "L_cli" | "L_engine" | "L_store",
          "boundary": "...",            # where observed vs expected split
          "observed": "...",
          "expected": "...",
          "expected_provenance": "spec" | "journey-contract",
          "differential": {...} | (absent),   # localization hint, if baseline given
          "why": "...",
          "summary": "<non-developer-readable one line>",
        }

    ``diagnosable`` is False when the scenario passed (nothing to bisect).
    """
    if report.get("passed"):
        return {"diagnosable": False,
                "reason": "scenario passed — nothing to localize"}
    failure = report.get("failure") or {}
    steps = report.get("steps", [])
    idx = failure.get("index")
    fstep = steps[idx] if isinstance(idx, int) and 0 <= idx < len(steps) else {}
    store = _load_raw_store(report.get("workdir", ""))

    kind = failure.get("kind")
    if kind == "persona_cli":
        core = _diagnose_persona_cli(fstep)
    elif kind == "inbound_stimulus":
        core = _diagnose_inbound(fstep, store)
    elif kind == "assert":
        core = _diagnose_assert(fstep, steps, failure, store, baseline_report)
    else:
        core = {"responsible_layer": None, "boundary": "unknown",
                "observed": failure.get("reason", ""), "expected": "",
                "expected_provenance": "", "why": "unrecognized failure kind"}

    # In cloud mode the store layer is the server API/DB (e-4701): remap so the
    # localization names L_api, not the local-file store layer.
    if report.get("mode") == "cloud" and core.get("responsible_layer") == LAYER_STORE:
        core["responsible_layer"] = LAYER_API
        core["boundary"] = core.get("boundary", "") + " (cloud: server API/DB)"

    layer = core.get("responsible_layer") or "?"
    summary = (f"層 {layer} が原因: {core['boundary']} で "
               f"observed={core['observed']} / expected={core['expected']} "
               f"(期待の出所: {core.get('expected_provenance') or '?'})")
    return {
        "diagnosable": True,
        "failing_step": {"index": idx, "kind": kind,
                         "label": fstep.get("label", "")},
        **core,
        "summary": summary,
    }
