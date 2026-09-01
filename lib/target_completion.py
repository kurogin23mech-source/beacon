"""Generic completion seam — fire deliverable + decision capture for ANY target-class's
完遂 (ms-163 e-5879 / e-5880 / SPEC 方針4).

Before this, the produced-value capture (``deliverable_capture.capture_target_completion``)
and the 完遂 decision (目的達成 verdict) were wired ONLY at the dev-milestone completion
seams (and the shared review-gated approve path). A sales opportunity reaching 決着, an
operation retiring, an acquisition settling, a descriptor target closing — none produced a
deliverable entry or a completion decision. The capability-scope checker's 完遂 seam 被覆
invariant (ms-163 e-5877) surfaced exactly these gaps.

This module is the class-generic seam every terminal calls, so a target-class is no longer
silently dropped from capture. Both producers are SAFE to call from any class:

  - ``capture_target_completion`` is declaration-driven — it no-ops when the class declares
    no ``deliverable`` slot (opportunity / operation / acquisition today), and idempotent
    (dedup by target+category), so calling it at every terminal never double-counts.
  - the 完遂 decision is best-effort + cloud-only — it records "this target reached
    <verdict>" on the decision arm, or silently does nothing in local mode.

Kept a thin leaf: it imports ``deliverable_capture`` (which sits above ``occupation``) and
lazily imports ``commands_shared`` inside the decision helper (the same cycle-avoidance
``cmd_target._record_completion_verdict_decision`` uses), so wiring it into a terminal adds
no import cycle. The milestone seams keep their existing direct capture calls (equivalent);
this seam covers the four classes that had none.
"""
from __future__ import annotations

import deliverable_capture as _dc


def _record_completion_decision(target: dict, verdict: str, reason: str) -> None:
    """Record a 完遂 (目的達成) verdict for ``target`` on the decision arm — best-effort,
    cloud-only (ms-163 e-5880). ``verdict`` is what the completion settled to (the terminal
    phase / status / "done"); ``reason`` is the why. No-ops in local mode. decided_by follows
    the human/AI session signal (a completion verdict is human-owned; an AI-assisted session
    is AI-proposed-human-chose), matching ``cmd_target._decided_by_for_gate``.

    ms-166 e-5978: the write-failure contract (a failed audit write is LOGGED, not silently
    swallowed, and never breaks the completion flow) is the single source
    ``commands_shared.best_effort_completion_decision`` — shared with the milestone/target
    approve path so the two never drift."""
    from commands_shared import (best_effort_completion_decision, _is_cloud_mode,
                                 _get_api_client, _session_kind_is_human)
    tid = ((target or {}).get("id") or "").strip()
    with best_effort_completion_decision(tid, verdict):
        if not _is_cloud_mode():
            return
        if not tid:
            return
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            return
        decided_by = "human-delegated" if _session_kind_is_human() \
            else "AI-proposed-human-chose"
        client.record_decision(project_id, {
            "kind": "completion-verdict",
            "decision": (verdict or "done"),
            "rationale": (reason or None),
            "decided_by": decided_by,
            "evidence": [],
            "related": {"target_id": tid},
        })


def on_target_completion(data: dict, target: dict, *, verdict: str = "done",
                         reason: str = "", at: str | None = None,
                         actor: str | None = None) -> None:
    """The class-generic completion seam (ms-163 e-5879/5880): capture ``target``'s produced
    value (deliverable) AND record its 完遂 decision. Call from every target-class's
    completion terminal. ``verdict`` = what the completion settled to (terminal phase /
    status / "done"); ``reason`` = the why. Deliverable capture mutates ``data`` in-memory
    (the caller owns persistence, matching ``capture_target_completion``'s discipline); the
    decision is written to the cloud decision arm out-of-band. Both are best-effort — a
    class without a deliverable slot / a local-mode project simply produces less, never an
    error."""
    _dc.capture_target_completion(data, target, reason=reason, at=at, actor=actor)
    _record_completion_decision(target, verdict, reason)
