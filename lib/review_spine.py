"""Review firing spine — which reviews a target lifecycle transition binds
(ms-119 / e-3911).

The review-kernel (ms-119) fires reviews at 節目 (= the moments where the cost
of a wrong call jumps: a lifecycle transition, a completion claim). Beacon owns
the target lifecycle, so it can emit a trigger GitHub never could — a target's
phase transition / close. This module is the *pure* binding table: given a
transition, which review(s) apply. The trigger writer + the wiring into the
concrete transition commands live in commands.py.

Two of the four reviews bind to a target transition:

- **目的達成 (attainment)**: owned verdict — the human confirms the target met
  its goal. Its *blocking* mechanism is the approval entry from e-3912
  (`beacon target review-request` → human approve). So the spine does NOT
  re-fire it when the transition is already going through that gate
  (``gated=True``). It fires only as an *advisory nudge* when a completion
  transition was applied WITHOUT the gate — a forcing function toward the
  reviewed path, never a second blocking mechanism.
- **思想 (philosophy)**: discoverable drift vs the written 原典 (SPEC / vision).
  Fires on a completion claim of a SPEC-bearing target, advisory (did the
  implementation drift from what the SPEC promised?).

**AX is intentionally absent here**: it binds to interface-change (diff / PR)
events, not to a target lifecycle transition. A milestone going done is not an
interface change.
"""

import json as _json
import os as _os

import transition_approval as _ta

# Review-type identifiers (stable strings — trigger payloads persist them).
REVIEW_ATTAINMENT = "attainment"   # 目的達成: owned verdict, human approval
REVIEW_PHILOSOPHY = "philosophy"   # 思想: drift vs SPEC / vision (advisory)
REVIEW_AX = "ax"                   # AX: AI-Experience interface drift (advisory)

# Artifact scoping modes (ms-119 / e-4196 review): the single source of the mode
# vocabulary. The kernel validation, the CLI wiring, and the judge instruments all
# reference these instead of bare literals, so renaming / adding a mode cannot drift
# across the layers (the AX + maintainability reviews of PR #503 flagged the literal
# duplication). "diff" = change-scoped (one PR / ref range); "full-surface" = a
# command-surface snapshot audit; "whole-target" = a target's whole recorded commit
# ledger (a close 節目 reviews the whole target, not one change).
MODE_DIFF = "diff"
MODE_FULL_SURFACE = "full-surface"
MODE_WHOLE_TARGET = "whole-target"
ARTIFACT_MODES = (MODE_DIFF, MODE_FULL_SURFACE, MODE_WHOLE_TARGET)

# --- data-driven review-type registry (ms-119 / e-4009) -------------------
# The judge-run review types are NOT a hardcoded whitelist. Each is described by
# a `skills/<type>/review-type.json` descriptor discovered on disk, so a NEW
# review type is added by dropping a descriptor (+ its 原典 + SKILL) with no edit
# to this module or commands.py (AC5: 原典差し替えだけで review type を追加できる
# plug-in 構造). The built-ins below are only a fallback for pure unit tests /
# partial checkouts where the skills tree is absent — on-disk descriptors are
# authoritative and override them.
_BUILTIN_REVIEW_TYPES = {
    REVIEW_AX: {
        "id": REVIEW_AX,
        "judge_run": True,
        "label": "AX (AI-Experience interface drift)",
        "origin": {"kind": "repo-file", "ref": "skills/ax-review/principles.md"},
    },
    REVIEW_PHILOSOPHY: {
        "id": REVIEW_PHILOSOPHY,
        "judge_run": True,
        "label": "思想 (drift vs SPEC / vision)",
        "origin": {"kind": "doc", "required": True},
    },
}


def _install_root():
    return _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _default_skills_dir():
    return _os.path.join(_install_root(), "skills")


def load_review_types(skills_dir=None):
    """Discover review-type descriptors from ``skills/*/review-type.json``.

    Returns ``{id: descriptor}``. On-disk descriptors override the built-in
    fallback, so adding a NEW judge-run review type requires only a new
    descriptor file (+ 原典 + SKILL) — no code edit here or in commands.py.

    A descriptor is a dict: ``{"id", "judge_run", "label", "origin": {...}}``.
    ``origin.kind`` is ``"repo-file"`` (原典 is a fixed repo file that travels
    with the capability) or ``"doc"`` (原典 is a doc supplied at review time via
    ``--origin-doc``).

    Malformed / unreadable descriptors are skipped (best-effort discovery must
    not crash a review over one bad file).
    """
    reg = {k: dict(v) for k, v in _BUILTIN_REVIEW_TYPES.items()}
    d = skills_dir or _default_skills_dir()
    try:
        names = _os.listdir(d)
    except OSError:
        return reg
    for name in names:
        path = _os.path.join(d, name, "review-type.json")
        if not _os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                desc = _json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(desc, dict):
            continue
        tid = (desc.get("id") or "").strip()
        if not tid:
            continue
        reg[tid] = desc
    return reg


def judge_run_review_types(skills_dir=None):
    """The subset of the registry whose reviews are run by an independent judge
    subagent (``judge_run: true``). Human-gated 目的達成 is intentionally absent."""
    return {k: v for k, v in load_review_types(skills_dir).items()
            if v.get("judge_run")}

# The independence contract handed to the judge subagent. It is the *structural*
# form of AX 原典 §2 (計器の必然): the judge is a context-zero instrument, so the
# bundle below must be its COMPLETE input. If the judge reaches for the
# implementer's session narrative / commit intent, the instrument is tainted and
# the review is void (ms-119 e-3947).
INDEPENDENCE_CONTRACT = (
    "この bundle が判定の完全な入力です。実装者セッションの会話文脈・コミットの"
    "意図説明・『本当はこうするつもり』を一切参照しないでください (AX 原典 §2 "
    "計器の必然)。原典 (origin) と機械採取した差分 (artifact) だけから判定する。"
    "文脈ゼロでない judge は計器として無効です。"
)


def surface_snapshot_artifact(probes, *, target_ref):
    """Shape a surface-snapshot artifact from mechanically-collected CLI probes
    (ms-119 / e-3987).

    AX 原典 §3 wants a *full-surface* audit (every command's help / error / exit
    code), not just a diff — the diff mode misses defects in code that didn't
    change this PR. Each probe is a dict as produced by
    ``commands._collect_surface_snapshot``:
    ``{"cmd": "milestone", "probe_argv": "milestone __ax_surface_probe__",
    "exit_code": 0, "stdout": "...", "stderr": "...", "silent_no_op": true}``
    (or ``{"cmd": ..., "probe_argv": ..., "error": "..."}`` if the probe failed).
    Pure: the caller runs the probes; this only shapes the artifact so the shaping
    is unit-testable without spawning subprocesses.
    """
    return {
        "kind": "surface-snapshot",
        "ref": target_ref,
        "probes": list(probes or []),
    }


def whole_target_artifact(commits, *, target_id, truncated=False):
    """Shape a whole-target artifact from a target's recorded commits (ms-119 /
    e-4196).

    A target-close 思想 (philosophy) review checks the WHOLE target's
    implementation against its SPEC, but the diff artifact was change-scoped
    (--pr / --diff-ref). A whole-MS close had neither, so the review ran against
    nothing (照合対象が空) — the hole this closes. A target OWNS its
    implementation ledger: Beacon logs every commit under the target as an entry.
    That recorded commit set IS the mechanically-collectable "what this target
    changed", and it is implementer-session-INDEPENDENT (git history + the project
    ledger, never the implementer's narrative), so it satisfies the same
    independence contract (§2 計器の必然) as a diff.

    Each commit is a dict: ``{"hash", "subject", "diff"}`` (or ``{"hash",
    "subject", "error"}`` if the commit is recorded but could not be shown locally
    — logged on another machine, not fetched; or ``{"hash", "subject", "diff": "",
    "omitted": true}`` when the character budget was spent and only the subject
    travels). Pure: the caller runs ``git show`` and shapes each commit; this only
    shells the envelope so the shaping is unit-testable without a repo.

    ``truncated``: True when the caller stopped attaching per-commit diffs after a
    character budget (later commits carry subject-only). The judge is then told the
    artifact is partial in ``gaps``, never silently short.
    """
    return {
        "kind": "whole-target",
        "ref": target_id,
        "commits": list(commits or []),
        "truncated": bool(truncated),
    }


def assemble_review_context(review_type, *, origin_id, origin_content,
                            diff_text, mode, target_ref, gaps=None,
                            known_judge_types=None, external_references=None,
                            implementer_model="", artifact=None):
    """Assemble the review-kernel bundle handed to an independent judge subagent.

    This is the *structural* enforcement of reviewer independence (ms-119
    e-3947): the bundle carries ONLY the 原典 (origin — a written source such as
    the AX principles or a target's SPEC) and the mechanically-collected diff
    (artifact). It deliberately carries no implementer narrative, so a judge fed
    this bundle cannot self-review even if the same human drives both sessions.

    Pure — callers collect origin_content (file / doc read) and diff_text (git)
    mechanically and pass them in; this function only shapes the bundle so the
    shaping is unit-testable without a repo or a subagent.

    Args:
        review_type: REVIEW_AX / REVIEW_PHILOSOPHY (attainment is human-gated,
            not a subagent judge — see review_bindings_for_transition).
        origin_id: identifier of the 原典 (doc-id or repo-relative file path).
        origin_content: the full text of the 原典 (never a summary).
        diff_text: the mechanically-collected unified diff (git / gh pr diff).
        mode: "diff" (change-scoped) or "full-surface" (snapshot audit).
        target_ref: what was reviewed (PR number / ref range / target id).
        gaps: optional list of gentle gaps to surface (e.g. philosophy review of
            a SPEC-less target — the missing 原典 is itself a finding, 方針5).
        known_judge_types: optional set of allowed judge-run type ids. Defaults
            to the on-disk registry (ms-119 / e-4009 — data-driven, not a
            hardcoded pair). Passing an explicit set keeps this function pure for
            unit tests that don't want disk IO.
        external_references: optional list of named external facts to expose to
            the judge (ms-119 / e-3997) — e.g. an application-map index. This is
            the ONLY sanctioned way to add non-diff context: it must be an
            implementer-session-INDEPENDENT fact (a repo/registry artifact),
            never the implementer's narrative / L2 memory (that would reopen the
            self-review hole the kernel exists to close). Each item is a dict
            like ``{"id": ..., "kind": ..., "content": ...}``.
    """
    if known_judge_types is None:
        known_judge_types = set(judge_run_review_types().keys())
    if review_type not in known_judge_types:
        allowed = ", ".join(sorted(repr(t) for t in known_judge_types)) or "(none)"
        raise ValueError(
            f"assemble_review_context: unsupported review_type {review_type!r} "
            f"(judge-run reviews are {allowed}; register a new one by adding a "
            f"skills/<type>/review-type.json descriptor; {REVIEW_ATTAINMENT!r} "
            f"is human-gated, not a subagent judge)."
        )
    if mode not in ARTIFACT_MODES:
        raise ValueError(f"assemble_review_context: mode must be one of "
                         f"{ARTIFACT_MODES}, got {mode!r}.")
    if artifact is None:
        # default artifact is the mechanically-collected diff (diff mode). A
        # full-surface caller passes a surface_snapshot_artifact() instead.
        artifact = {"kind": "diff", "ref": target_ref, "content": diff_text}
    return {
        "review_type": review_type,
        "mode": mode,
        "target_ref": target_ref,
        "origin": {"id": origin_id, "content": origin_content},
        "artifact": artifact,
        "independence_contract": INDEPENDENCE_CONTRACT,
        "external_references": list(external_references or []),
        "implementer_model": implementer_model or "",
        "gaps": list(gaps or []),
    }


# The attainment judge's contract (ms-119 / e-4005). Unlike AX / philosophy
# (which produce advisory findings), the attainment judge produces the *evidence*
# a human weighs before pressing approve. Two invariants make this non-circular:
# the judge verifies each SPEC criterion against REAL code / behavior (not the
# implementer's self-report), and it owns ONLY the evidence — the final verdict
# stays with the human (SPEC § 方針2, enforced by the e-4006 approve guard).
ATTAINMENT_JUDGE_CONTRACT = (
    "あなたは、対象 target が SPEC の目的 (§やる) / 受入条件を実際に満たしたかを、"
    "実装者の自己申告ではなく実コード・実挙動から独立に検証する judge です。"
    "この bundle (SPEC 原典 + 受入条件 + 対象参照 + 任意の差分) を入力に、各受入条件を "
    "met / partial / not-met で判定し、判定ごとに『どのコード / テスト / 実挙動を"
    "確認したか』の根拠を必ず添え、総合 verdict (attained / not-attained) を出して"
    "ください。重要: 最終 verdict の確定権は human にあります (SPEC 方針2)。あなたは"
    "証拠と判定案を生成するだけで、target の状態遷移を確定させません。実装者セッション"
    "の会話文脈・コミットの意図説明・『本当はこうするつもり』を一切参照しないでください "
    "(文脈ゼロの計器)。"
)


def judge_model_independence(implementer_model, judge_model):
    """Structural check that the judge model differs from the implementer model
    (ms-119 / e-3988).

    Reviewer independence (AX 原典 §2) is weaker when the same model both writes
    and judges — a shared prior blind spot survives. The /beacon-review-run Skill
    defaults the judge to a different model, but that was散文-only; this makes the
    check callable so it can run structurally.

    Returns a dict: ``{"ok": bool, "reason": str}``. ``ok`` is False (independence
    at risk) when both models are known and equal. Unknown implementer model
    (``""``) → ``ok`` True with an advisory reason (can't compare), so the check
    never hard-blocks on missing info.
    """
    im = (implementer_model or "").strip().lower()
    jm = (judge_model or "").strip().lower()
    if not im:
        return {"ok": True, "reason": "implementer_model unknown — cannot compare "
                "(set BEACON_IMPLEMENTER_MODEL to enable the check)."}
    if not jm:
        return {"ok": True, "reason": "judge_model unknown — cannot compare."}
    if im == jm:
        return {"ok": False, "reason": f"judge model ({jm}) == implementer model "
                f"({im}); a shared prior blind spot survives. Pick a different "
                f"judge model (--models) or confirm explicitly."}
    return {"ok": True, "reason": f"judge ({jm}) != implementer ({im})."}


def assemble_attainment_context(*, target_id, spec_origin_id, spec_content,
                                criteria, target_ref, diff_text="", gaps=None,
                                implementer_model=""):
    """Assemble the 目的達成 evidence-generation bundle for an independent judge
    (ms-119 / e-4005).

    The 目的達成 review's verdict is the human's, but its *evidence* must not be
    the implementer's self-report (this session over-claimed ms-119 done before
    an independent judge caught it). This bundle carries ONLY the SPEC 原典, the
    criteria to check, and an optional supporting diff — never implementer
    narrative — so a context-zero subagent can verify each criterion against real
    code and produce met/partial/not-met evidence. The human then approves via
    ``beacon target approve`` (human-gated by e-4006).

    Pure: callers collect spec_content / diff_text mechanically and pass them in.

    Args:
        target_id: the target being reviewed (ms-XX / op-X).
        spec_origin_id: doc-id of the SPEC 原典 (or "" if the target has none).
        spec_content: full SPEC text (never a summary); "" if no SPEC.
        criteria: list of criterion dicts to verify, e.g.
            ``[{"source": "acceptance_criteria", "text": "..."}]``.
        target_ref: what was reviewed (target id / ref range).
        diff_text: optional mechanically-collected diff for supporting context.
        gaps: optional gentle gaps (e.g. missing SPEC 原典, 方針5).
    """
    return {
        "review_type": REVIEW_ATTAINMENT,
        "target_id": target_id,
        "target_ref": target_ref,
        "origin": {"id": spec_origin_id, "content": spec_content},
        "criteria": list(criteria or []),
        "artifact": {"kind": "diff", "ref": target_ref, "content": diff_text},
        "verdict_ownership": "human",
        "judge_contract": ATTAINMENT_JUDGE_CONTRACT,
        "independence_contract": INDEPENDENCE_CONTRACT,
        "implementer_model": implementer_model or "",
        "gaps": list(gaps or []),
    }


def batch_review_types_for_node(node, *, registry=None):
    """Which reviews fire together at a 節目 (ms-119 / e-4125).

    A 節目 fires a PAIR, not a single review: a PR-open fires AX + maintainability
    (interface + change-experience drift); a target-close fires 目的達成 +
    思想 (goal + spirit). Running them as separate one-off invocations means the
    human sees two reports and no consensus across them. This returns the review
    ids that bind to a node so a single batch call can fan them out in parallel.

    ``node``: "pr-open" (data-driven: every judge-run type whose descriptor
    ``fires_on == "pr-open"``) or "target-close" (思想 [judge-run, needs SPEC]
    + 目的達成 [human-gated evidence review]).

    Returns a list of ``{"review": id, "judge_run": bool}`` in a stable order.
    """
    if node == "pr-open":
        reg = judge_run_review_types() if registry is None else {
            k: v for k, v in registry.items() if v.get("judge_run")}
        out = [{"review": tid, "judge_run": True}
               for tid, desc in sorted(reg.items())
               if desc.get("fires_on") == "pr-open"]
        return out
    if node == "target-close":
        # This is the MENU (which review types CAN bind at a completion 節目),
        # not the per-target firing decision. review_bindings_for_transition /
        # review_bindings_for_completion make 思想 conditional on the target
        # actually having a SPEC 原典 (has_spec) — so a SPEC-less target fires
        # only 目的達成. Keep the two in step: whatever type is listed here must
        # be a type that CAN bind in review_bindings_for_completion (asserted by
        # tests/test_review_batch_fold.py). 思想 is judge-run; 目的達成 is
        # human-gated (judge produces evidence, human owns the verdict — see
        # ATTAINMENT_JUDGE_CONTRACT).
        return [
            {"review": REVIEW_PHILOSOPHY, "judge_run": True},
            {"review": REVIEW_ATTAINMENT, "judge_run": False},
        ]
    return []


def _finding_key(finding):
    """Normalized dedup key for a finding: (file, line, lowercased title stripped
    of trailing punctuation/whitespace). Pure."""
    f = (finding.get("file") or "").strip()
    line = finding.get("line")
    line = str(line).strip() if line is not None else ""
    title = " ".join((finding.get("title") or "").lower().split()).rstrip(".。 ")
    return (f, line, title)


def aggregate_review_reports(reports):
    """Fold N per-review reports into ONE deduped, consensus-scored report
    (ms-119 / e-4125).

    Each report is ``{"review_type": id, "findings": [ {title, file?, line?,
    severity?}, ... ]}``. Findings that the SAME 節目's reviews independently
    raise (same file/line/title) are merged into one, carrying which review
    types flagged it and a ``consensus`` count — a finding two independent judges
    both raise is stronger signal than one raised alone. Pure: the caller (the
    /beacon-review-run Skill) runs the judges and hands their structured findings
    here; this only folds them so the fold is unit-testable without subagents.

    Returns ``{"findings": [...], "by_type": {id: count}, "reviews": [ids]}``,
    findings ordered by descending consensus then first-seen.
    """
    merged = {}
    order = []
    by_type = {}
    review_ids = []
    for rep in reports or []:
        rtype = rep.get("review_type") or rep.get("review") or "?"
        if rtype not in review_ids:
            review_ids.append(rtype)
        findings = rep.get("findings") or []
        by_type[rtype] = len(findings)
        for fnd in findings:
            key = _finding_key(fnd)
            if key not in merged:
                merged[key] = {
                    "title": fnd.get("title", ""),
                    "file": fnd.get("file", ""),
                    "line": fnd.get("line"),
                    "severity": fnd.get("severity", ""),
                    "raised_by": [],
                    "consensus": 0,
                }
                order.append(key)
            m = merged[key]
            if rtype not in m["raised_by"]:
                m["raised_by"].append(rtype)
                m["consensus"] = len(m["raised_by"])
            # keep the FIRST non-empty severity seen (severity strings aren't
            # orderable lexically, so don't attempt a max; detailed ranking is
            # left to the Skill that presents the folded report).
            if not m["severity"] and fnd.get("severity"):
                m["severity"] = fnd["severity"]
    findings = [merged[k] for k in order]
    findings.sort(key=lambda m: -m["consensus"])
    return {"findings": findings, "by_type": by_type, "reviews": review_ids}


def build_surface_index_reference(map_content, *, updated_at="", stale=False):
    """Shape an application-map surface-index external reference for the judge
    bundle (ms-119 / e-4096).

    A context-zero judge is stronger when it knows WHAT the product's surface is
    (where features live, what already exists) — otherwise it either misses that
    a diff duplicates an existing surface, or it reaches into the repo (tainting
    the instrument). The application-map (CORE doc `application-map`) is exactly
    that index and is an implementer-INDEPENDENT artifact, so it is the sanctioned
    non-diff context (see assemble_review_context.external_references). This
    shapes it as one reference; the caller reads the map content mechanically.

    Returns None when there is no map content (the caller then records a gap so
    the judge knows the surface index is absent, not silently complete).
    """
    if not (map_content or "").strip():
        return None
    return {
        "id": "application-map",
        "kind": "surface-index",
        "content": map_content,
        "updated_at": updated_at or "",
        "stale": bool(stale),
        "note": ("プロダクトの全 surface (機能の入口) 索引。judge が『この差分に近い"
                 "既存機能があるか』『surface が重複していないか』を repo を読まずに"
                 "判定するための、実装者非依存の参照。stale=true の時は地図が古い可能性。"),
    }


def build_review_skip_record(*, review_type, ref_kind, ref, reason, actor,
                             gate, at):
    """Build a durable audit record for a HUMAN-intended review skip (ms-119 /
    e-4124).

    A skip is a first-class governance decision — "a human looked at this 節目
    and decided this particular review is not needed, here is why" — and is
    semantically DISTINCT from ``BEACON_PR_REVIEW_OVERRIDE`` (the AI-ban
    circumvention flag, which means "force a merge past an review that is still
    owed"). The two must not be conflated: an override forces past an unmet
    obligation; a skip records that the obligation was deliberately waived, with
    a reason and an owner.

    Like the approval gate record (e-4006), the value is not "an AI cannot skip"
    but "a skip cannot HIDE who waived it and why": ``gate`` carries the signal
    that authorised the skip (human-session vs explicit user override) so an
    AI-recorded skip leaves a grep-able footprint rather than looking identical
    to a human's.

    Pure: returns the record dict; the caller owns persistence (append to the
    durable ``.beacon/review-skips.jsonl`` audit) and clearing the review-due
    trigger.
    """
    return {
        "kind": "review-skip",
        "review_type": review_type,
        "ref_kind": ref_kind,          # "pr" | "target"
        "ref": ref,                    # PR number / target id
        "reason": reason,
        "actor": actor,
        "gate": gate,                  # {signal, session_kind} — see e-4006
        "at": at,
    }


def review_bindings_for_transition(target_kind, old_state, new_state, *,
                                   has_spec=False, gated=False):
    """Return the review bindings that apply to a target lifecycle transition.

    A binding is a dict: ``{"review": <id>, "blocking": bool, "origin": str}``.
    ``origin`` names the 原典 (the written source the judge checks against) so
    the downstream message can explain what the review compares against.

    Empty list = no review fires (routine / reversible transitions like
    todo -> active, -> waiting carry no completion claim). Note: -> observing
    DOES carry a completion claim in this codebase (運用改善フェーズ =
    基本目的達成が前提) and fires the reviews like done — see
    transition_approval module docstring.

    Args:
        target_kind: "milestone" / "operation" / "opportunity" (prefix-derived).
        old_state / new_state: the transition's endpoints.
        has_spec: whether the target has a SPEC / vision 原典 attached.
        gated: True when this transition is going through the 目的達成 approval
            gate (``beacon target ... --review``). Both reviews still bind (ms-119
            e-4005: the 目的達成 evidence review must auto-fire at the close 節目
            too, alongside 思想); ``gated`` only changes the attainment message —
            "evidence for the pending approval" vs "retrospective / you bypassed
            the gate". Carried on the binding so the trigger writer can phrase it.
    """
    if not _ta.is_attainment_transition(target_kind, old_state, new_state):
        return []
    return review_bindings_for_completion(has_spec=has_spec, gated=gated)


def review_bindings_for_completion(*, has_spec=False, gated=False):
    """The review bindings for a completion-claim 節目, independent of target
    KIND (ms-119 / e-4087).

    ``review_bindings_for_transition`` decides *whether* a built-in milestone /
    operation transition is a completion claim (via the transition_approval
    truth table) and then delegates here for *which* reviews bind. A
    data-defined target (ms-122 記述子で定義した職種の対象) reaching done / a
    terminal phase is the same completion claim — its KIND is a descriptor class
    name the built-in truth table doesn't know, but the 節目 is identical — so it
    calls this directly with ``has_spec`` resolved from its own SPEC lookup.

    Both 目的達成 and 思想 fire at the completion 節目 (ms-119 e-4005). They share
    the SPEC 原典 and the moment: 目的達成 = did we reach the goal (evidence for a
    human verdict); 思想 = did we honour the SPEC's spirit getting there
    (advisory). Neither is suppressed — the human wants both auto-fired at close.
    """
    bindings = [{
        "review": REVIEW_ATTAINMENT,
        "blocking": False,
        "origin": "owner intent (target が目的を果たしたか)",
        "gated": gated,
    }]
    if has_spec:
        bindings.append({
            "review": REVIEW_PHILOSOPHY,
            "blocking": False,
            "origin": "SPEC / vision",
        })
    return bindings
