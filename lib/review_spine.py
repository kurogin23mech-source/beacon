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
import re as _re

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
# ms-154 e-5595: "decision-audit" = the artifact is the declared decision-arm
# stream (what / why / evidence), not a code diff. A context-zero judge checks
# each declared rationale against the real code the evidence points to (= the
# independent-verification path that catches AI post-hoc rationalization).
MODE_DECISION_AUDIT = "decision-audit"
ARTIFACT_MODES = (MODE_DIFF, MODE_FULL_SURFACE, MODE_WHOLE_TARGET,
                  MODE_DECISION_AUDIT)

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


def install_root():
    """The checkout this capability is running from (the parent of ``lib/``).

    Public because more than one module needs it and the expression is easy to
    get subtly wrong (one ``dirname`` too few still returns a plausible path).
    ms-119 e-7345: ``commands._capability_root`` used to hold its own copy of
    this same expression under a different name — a second definition of one
    concept that a later edit would have to find by guessing the other's name.
    """
    return _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _install_root():
    """Deprecated alias kept for existing callers in this module."""
    return install_root()


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


# Paths whose diff is Beacon's own bookkeeping, not the change under review
# (ms-119 e-7352). `.beacon/project.json` carries `beacon log` behavior text,
# session-log summaries and task motivation — i.e. *the implementer session's
# explanation of its own intent*, which is exactly what
# ``INDEPENDENCE_CONTRACT`` tells the judge not to read. A bundle that carries
# it is the contract's author breaking the contract.
BOOKKEEPING_PATH_PREFIXES = (".beacon/",)


def _diff_section_paths(section_lines):
    """Paths a unified-diff section touches, from its own header lines.

    ``--- a/x`` / ``+++ b/y`` are preferred over the ``diff --git`` line because
    they survive paths with spaces; the ``diff --git`` line is the fallback for
    sections that carry no file headers (a pure mode/rename change).
    """
    paths = []
    for line in section_lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            ref = line[4:].strip()
            if ref == "/dev/null":
                continue
            if ref[:2] in ("a/", "b/"):
                ref = ref[2:]
            if ref:
                paths.append(ref)
    if not paths and section_lines:
        m = _re.match(r"^diff --git a/(.+?) b/(.+)$", section_lines[0].rstrip("\n"))
        if m:
            paths.extend([m.group(1), m.group(2)])
    return paths


def strip_bookkeeping_from_diff(diff_text, prefixes=BOOKKEEPING_PATH_PREFIXES):
    """``(filtered_diff, dropped)`` — drop Beacon's own bookkeeping from a diff.

    ``dropped`` is ``[(path, chars), ...]`` so the caller can **say what the
    judge was not shown**. Disclosure is half the point: a judge handed a
    silently-trimmed diff believes it saw the whole change, and the two findings
    that asked for this filter were themselves about a bundle that misrepresented
    its own contents.

    Why here and not "the judge skips it": three independent judges reported
    reading ``.beacon/project.json`` hunks in the artifact and *self-reported*
    skipping them (2026-11-02 / 2026-11-04 / 2026-11-05, 原典 26% / 16% / 70% of
    the diff by character count). Closing a hole by asking the reader not to look
    is the shape Beacon rejects everywhere else — if it must not be read, it must
    not be sent.

    A rename into or out of a bookkeeping path drops too (either side matching is
    enough): the section's content is the bookkeeping file's text either way.

    Pure: no file or process access, so the contract is unit-testable without a
    repo.
    """
    if not diff_text:
        return diff_text, []
    lines = diff_text.splitlines(keepends=True)
    # Sections start at each `diff --git`; anything before the first one is a
    # preamble that belongs to no file and is kept verbatim.
    starts = [i for i, L in enumerate(lines) if L.startswith("diff --git ")]
    if not starts:
        return diff_text, []
    kept = list(lines[:starts[0]])
    dropped = []
    bounds = starts + [len(lines)]
    for i in range(len(starts)):
        section = lines[bounds[i]:bounds[i + 1]]
        paths = _diff_section_paths(section)
        hit = next((p for p in paths
                    if any(p.startswith(pre) for pre in prefixes)), None)
        if hit is None:
            kept.extend(section)
        else:
            dropped.append((hit, sum(len(L) for L in section)))
    return "".join(kept), dropped


def bookkeeping_drop_gap(dropped):
    """The one-line disclosure for ``dropped``, or ``""`` when nothing was cut."""
    if not dropped:
        return ""
    total = sum(n for _, n in dropped)
    shown = ", ".join(f"{p} ({n:,} 字)" for p, n in dropped[:5])
    more = f" ほか {len(dropped) - 5} ファイル" if len(dropped) > 5 else ""
    return (f"artifact から Beacon 自身の作業記録 {len(dropped)} ファイル "
            f"(合計 {total:,} 字) を外しました: {shown}{more}。"
            f"これらは実装者セッションの意図説明 (作業記録の behavior / セッション"
            f"ログの summary / タスクの motivation) を含むため、独立性の契約が"
            f"読むなと言う対象です。**差分のこの部分は判定に入っていません。**")


def _origin_slot(origin_id, origin_content, provenance):
    """Build the bundle's ``origin`` slot, carrying provenance when the caller
    knows it (ms-119 e-7345).

    Kept separate so the shape lives in one place: a judge reads
    ``origin.resolved_from`` to know which working tree's 原典 it was handed, and
    a human reads it to know which version a past verdict was reached under.
    Callers that cannot say (a doc-origin 原典 has no checkout) pass nothing and
    the slot keeps its old two-key shape.
    """
    slot = {"id": origin_id, "content": origin_content}
    if provenance:
        slot.update({k: v for k, v in provenance.items() if v})
    return slot


def assemble_review_context(review_type, *, origin_id, origin_content,
                            diff_text, mode, target_ref, gaps=None,
                            known_judge_types=None, external_references=None,
                            implementer_model="", artifact=None,
                            origin_provenance=None, head_sha=""):
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
        origin_provenance: optional ``{"resolved_from": ..., "path": ...}`` saying
            WHICH checkout the 原典 was read from. A repo-file 原典 lives in a
            working tree, and more than one tree can hold a different version of
            it (a worktree being reviewed vs the installed capability), so the
            bundle states which one it carries instead of leaving the judge to
            assume (ms-119 e-7345 — a review silently judged by a stale 原典
            cannot be told apart from one judged by the current one).
        origin_content: the full text of the 原典 (never a summary).
        diff_text: the mechanically-collected unified diff (git / gh pr diff).
        mode: "diff" (change-scoped) or "full-surface" (snapshot audit).
        target_ref: what was reviewed (PR number / ref range / target id).
        head_sha: **どの差分を judge が見たか** を sha で名指す (ms-119 e-6859、
            独立 AX レビュー 4 周目 high)。``--reviewed-sha`` に渡すべき値の
            **唯一の構造的な供給源**。これが無いと、judge が読んでいる間に誰かが
            push した場合、``review done`` は「いまの head」を gh に聞き直して
            **judge が一度も見ていない sha** を実施済みとして記録する ——
            この課題が消すと宣言した欠陥クラスが、bundle → done の継ぎ目で
            そのまま再現する。judge は冒頭でこの値を自分の作業ツリーと照合できる。
            取れなければ空 (推測で埋めない)。
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
        # ms-119 e-6859 (独立 AX レビュー 4 周目 high): **judge が見た差分の sha**。
        # `--reviewed-sha` に渡すべき値がここに在ることで、「見た sha」と
        # 「記録される sha」が同じ変数の値になる (2 回の独立した gh 問い合わせの
        # 間に起きるドリフトが構造的に閉じる)。空 = 不明。
        "head_sha": str(head_sha or "").strip(),
        "origin": _origin_slot(origin_id, origin_content, origin_provenance),
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
    " さらに (ms-119 / e-4579): bundle の backlog に列挙された未着手の重要タスク "
    "(highest/high) は、target が『必要と符号化した既知作業』です。attainment は AC "
    "(アウトカム) 軸で判定しますが、未着手の重要タスクは目的未達の強い prior です "
    "(掃除機がある≠掃除した)。各 backlog タスクについて done / superseded[なぜ不要か"
    "の理由必須] / blocks-attainment の disposition を提案し、根拠を添えてください。"
    "『重要だがパスして良い』とするなら superseded + 理由で明示し、silent に飛ばさない"
    "でください。"
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
                                backlog=None, implementer_model=""):
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
        backlog: ms-119 / e-4579 — the target's UNSTARTED highest/high tasks
            (``[{"id","priority","description"}]``). The judge must reckon with each
            (掃除機がある≠掃除した): an important, encoded, untouched task is a strong
            "goal not yet met" prior. The judge proposes a disposition per task
            (done / superseded[理由] / blocks-attainment); the human confirms.
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
        # e-4579: the important, unstarted backlog the judge must dispose of.
        "backlog": list(backlog or []),
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


#: 実施記録が「どの差分に対する実施か」を持たないとき (= 旧形式 / 解決できなかった)
#: のしるし。推測で埋めず空のままにする —— 「不明」を特定の sha とみなすと、関門を
#: 外してよいかの判定を捏造することになる。
REVIEW_SHA_UNKNOWN = ""


def required_pr_open_review_types(registry=None):
    """PR が取り込まれる前に **記録されていなければならない** レビュー種別。

    ``batch_review_types_for_node("pr-open")`` から導く (= registry が唯一の
    真実源)。新しい pr-open 種別の descriptor を置けば、ここも関門も自動で追従する。
    ``scripts/review-gate-ci.py:required_review_types`` と同じ集合を指すので、
    ローカルの関門と CI の required check が別の集合を見ることがない。
    """
    return [d["review"]
            for d in batch_review_types_for_node("pr-open", registry=registry)]


def derive_pr_review_obligations(*, required, performed, skipped, head_sha):
    """PR の「まだ要るレビュー」を **導出する** (ms-119 e-6859)。純粋関数。

        owed = required − (head_sha に対する実施) − (理由付きの免除)

    **なぜ導出なのか。** 以前この問いは「義務ファイルが在るか」で答えていた。
    義務は PR-open で 1 回書かれ、``beacon review done`` が消す —— つまり
    「要る / 要らない」を **削除と復活の状態遷移** で表していた。新しいコミットを
    積んでも復活させる経路が無かったので、一度実施すれば以後どれだけ積んでも
    「実施済み」のままだった。実測 (PR #785 / #834 / #835 / #836): judge が一度も
    見ていない差分に対して ``beacon-review-gate`` が緑になり、しかも #835 では
    **AX の findings を直したコミット自身が未レビュー** のまま帳簿上「レビュー済み」
    だった。修正コミットこそ欠陥を作ることが 3 周連続で実測されているので、この穴は
    統計的にいちばん危ない領域を帳簿から隠していた。

    状態遷移の形を保ったまま「push で復活させる」経路を足しても、復活経路を 1 つ
    忘れれば同じ穴が残る。導出なら **忘れる経路が構造的に存在しない** —— head_sha が
    変わった瞬間に第 2 項が空になり、全種別が owed に戻る。

    Args:
        required: 必須種別の列 (``required_pr_open_review_types()``)。
        performed: 実施記録の列。各要素は
            ``{"review_type", "reviewed_sha", "sha_pinned"}``
            (余分なキーは無視する)。``sha_pinned`` の 3 値が要点 ——
            下の「3 値である理由」を参照。
        skipped: 理由付きで免除された種別の列。各要素は
            ``{"review_type", "skipped_at_sha"}`` か、種別の文字列。
        head_sha: いまレビュー対象になっている差分の sha。確かめられなければ
            ``REVIEW_SHA_UNKNOWN`` / None を渡す —— **呼び出し側は head が不明なら
            関門を閉じたままにする** (「現在の差分に対して実施済み」と言えないため)。

    **``sha_pinned`` が 3 値である理由** (ここを 2 値にすると穴が開く):

    ``True``
        この記録は「どの差分に対する実施か」を自分で pin している。
        ``reviewed_sha == head_sha`` のときだけ差し引く。
    ``None`` (= 記録にその欄が無い / 旧形式)
        この変更より前に書かれた記録。``LEGACY_REVIEW_DUE_TOLERANCE`` と同じ判断で
        **差し引くが開示する** —— 黙って厳しくして、実際に実施したレビューを
        まとめて未実施に戻さない。open な PR の寿命 (数日〜数週) を超えれば
        自然に居なくなる。
    ``False``
        **新しい記録なのに sha を pin できなかった** (= 実施は記録したが、head を
        確かめられなかった)。これは差し引かない。ここを ``None`` と同じ扱いに
        すると、旧記録のための寛容が **恒久的な抜け道** に変わる: gh が落ちている
        / offline で ``review done`` を打つたびに「sha 無しだから差し引く」記録が
        増え、以後の全差分を覆ってしまう。「緩めると言い換えで素通りする」型
        そのものなので、旧形式への寛容と「今回 pin できなかった」を分けて持つ。

    Returns:
        dict:
            ``head_sha`` / ``head_known`` — 判定の土台。
            ``required`` — 必須種別 (重複を畳んだ入力順)。
            ``owed`` — まだ要る種別 (required 順)。
            ``satisfied`` — head_sha に対する実施が在る種別。
            ``legacy_no_sha`` — 旧形式の記録 (``sha_pinned is None`` かつ sha 無し)
                のため **従来どおり差し引いた** 種別。呼び出し側は開示する。
            ``unpinned`` — ``sha_pinned is False`` の記録。差し引かない。
                ``[{"review", "reason"}]``。
            ``stale`` — 実施記録が **別の sha** に対するものだった種別。
                ``[{"review", "reviewed_sha"}]``。owed に戻る。
            ``skipped`` — 免除で差し引いた種別。``[{"review", "skipped_at_sha"}]``。
                免除は sha 単位にしない (人間が理由付きで出した判断を、push ごとに
                出し直させるのは現実的でない) が、**どの時点の免除かを開示する**。

        **バケツは 1 種別につき 1 つ** (排他)。強い根拠が勝つ順は
        ``satisfied`` > ``legacy_no_sha`` > ``skipped``、差し引かれなかった側は
        ``stale`` / ``unpinned`` のどちらか 1 つ。2 つ以上のバケツに同じ種別が
        載ると、開示が「実施済み」と「免除」を同時に言い、読み手がどちらを信じれば
        よいか決められない (独立 AX レビューが実測した形)。
    """
    req = []
    for t in (required or []):
        t = str(t or "").strip()
        if t and t not in req:
            req.append(t)

    head = str(head_sha or "").strip()
    head_known = bool(head)

    # --- 実施記録を 4 つに分ける ---
    satisfied = []
    legacy_no_sha = []
    unpinned = {}
    stale_by_type = {}
    for rec in (performed or []):
        if not isinstance(rec, dict):
            continue
        rtype = str(rec.get("review_type") or "").strip()
        if not rtype or rtype not in req:
            continue
        if rtype in satisfied:
            continue                    # head 一致が既に在る種別は上書きしない
        rsha = str(rec.get("reviewed_sha") or "").strip()
        pinned = rec.get("sha_pinned", None)

        if pinned is False:
            # 新しい記録だが sha を pin できなかった = 差し引かない。
            unpinned.setdefault(
                rtype,
                "実施は記録されていますが、どの差分に対する実施か pin できて"
                "いません (記録時に head を確かめられなかった)")
            continue
        if not rsha:
            if pinned is None:
                # 旧形式 —— 従来どおり差し引く (開示はする)。
                if rtype not in legacy_no_sha:
                    legacy_no_sha.append(rtype)
            else:
                # pinned=True を名乗りながら sha が空 = 名乗りが実装と合っていない。
                # 信用しない側に倒す。
                unpinned.setdefault(
                    rtype, "記録が sha を pin したと名乗っていますが空です")
            continue
        if head_known and rsha == head:
            satisfied.append(rtype)
            stale_by_type.pop(rtype, None)
            unpinned.pop(rtype, None)
        else:
            # head 不明のときも「確かめられないので信用しない」側に倒す。
            stale_by_type.setdefault(rtype, rsha)

    # --- 免除 ---
    #
    # **1 種別につき開示されるバケツは必ず 1 つ** (ms-119 e-6859、独立 AX レビュー)。
    # 初版は免除と旧形式を無条件に積んでいたので、同じ種別に「実施済み」と「免除」の
    # 両方の記録が在ると、開示に ``✓ ax: <sha> に対して実施`` と
    # ``⊘ ax: 人間が理由付きで免除`` が **同時に出た**。judge の指摘どおり
    # 「差し引いた根拠を必ず言う」という設計意図が、2 つの矛盾する根拠を並べることで
    # 「どちらを信じればいいか分からない」形に転化していた。
    #
    # 優先順位は **強い根拠が勝つ** 順: 実施 (この head を見た) > 旧形式の寛容
    # (見たが差分が不明) > 免除 (見ていないが人間が免除した)。弱い側を黙って
    # 落とすのではなく、強い根拠が在るならそれだけを言う。
    legacy_no_sha = [t for t in legacy_no_sha if t not in satisfied]
    _already = set(satisfied) | set(legacy_no_sha)
    skipped_out = []
    for s in (skipped or []):
        if isinstance(s, dict):
            rtype = str(s.get("review_type") or "").strip()
            at = str(s.get("skipped_at_sha") or "").strip()
        else:
            rtype, at = str(s or "").strip(), ""
        if not rtype or rtype not in req or rtype in _already:
            continue
        if any(x["review"] == rtype for x in skipped_out):
            continue
        skipped_out.append({"review": rtype, "skipped_at_sha": at})

    credited = (set(satisfied) | set(legacy_no_sha)
                | {x["review"] for x in skipped_out})
    owed = [t for t in req if t not in credited]
    stale = [{"review": t, "reviewed_sha": stale_by_type[t]}
             for t in req if t in stale_by_type and t not in credited]
    unpinned_out = [{"review": t, "reason": unpinned[t]}
                    for t in req if t in unpinned and t not in credited]

    return {
        "head_sha": head,
        "head_known": head_known,
        "required": req,
        "owed": owed,
        "satisfied": satisfied,
        "legacy_no_sha": legacy_no_sha,
        "unpinned": unpinned_out,
        "stale": stale,
        "skipped": skipped_out,
    }


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


def build_intra_range_move_reference(moves):
    """差分の範囲内で「移動してきた」ファイルを judge に伝える参照を組む
    (ms-119 e-7404)。

    **なぜ要るか** (2026-11-12 の実測で判明): judge に渡す差分は base..head の
    1 枚なので、**同じ範囲内でファイルが作られ、その後 別の場所へ移された**場合、
    base に元のパスが無いため「移動先に新規追加された」としか見えない。rename
    検出 (git の ``-M``) では復元できない —— rename の相手が base に存在しないため。

    実害 (PR#840 の 6 周目): `.trash/` へ退避した試験ファイルについて、独立 judge
    2 体が揃って「新しく書いた現役の試験を置き間違えた、tests/ へ戻すべき」と
    high で指摘した。渡された差分に対しては **judge の読みは正しかった** —— 欠けて
    いたのは「この範囲内で移動してきた」という履歴。このリポジトリは ``rm`` を
    使わず ``.trash/`` へ ``mv`` して退役させる運用なので、退役のたびに構造的に
    再発する。しかも全 judge が同じ入力を読むので **2 体一致が誤りを強める方向に
    働き、合意度では濾せない**。

    起票時の見立て (= rename 検出が効いていない) は実測で否定された: ``git show`` /
    ``git diff`` は ``diff.renames`` 既定 true で既に rename を検出しており、
    ``gh pr diff`` も検出する。``-M`` を足すのはバイト単位で同一の no-op だった。
    真因は上記の「範囲内の create → 移動」。

    ``moves`` は ``[{"path": 移動先, "from": 元パス, "commit": 移動した commit}]``。
    空なら None (= 載せるものが無い。空の参照を載せて「確認した」に見せない)。
    """
    items = [m for m in (moves or [])
             if (m or {}).get("path") and (m or {}).get("from")]
    if not items:
        return None
    lines = [
        "この差分の範囲内で **移動してきた** ファイル (差分では移動先への新規追加に"
        "見えるが、元は範囲内の別の場所に在った):",
    ]
    for m in items:
        commit = str(m.get("commit") or "").strip()
        suffix = f" (移動した commit: {commit})" if commit else ""
        lines.append(f"  - {m['path']} ← {m['from']}{suffix}")
        # 2 回以上動いた場合は **辿った道筋を commit ごと** 添える
        # (ms-119 / 独立 AX レビュー misleading)。単一の commit だけを載せると、
        # judge がそれを `git show` で裏取りしたときに中間パスとの rename しか
        # 見えず、注記が主張する出発点が現れない = 証拠が主張を裏付けない。
        # 中間パスは「現在の置き場所」ではなく **道筋の通過点** として示す。
        hops = [h for h in (m.get("hops") or [])
                if (h or {}).get("from") and (h or {}).get("to")]
        if len(hops) > 1:
            trail = " → ".join(
                [hops[0]["from"]]
                + [f"({h.get('commit') or '?'}) {h['to']}" for h in hops])
            lines.append(f"      道筋 (この範囲内で辿った通過点、現在の置き場所では"
                         f"ない): {trail}")
    lines.append(
        "判定の含意: 移動先が `.trash/` 等の退役置き場なら、これは **意図的な退役** "
        "であって『新しく書いたものを置き間違えた』ではない。元の場所へ戻す提案を"
        "する前に、移動が意図的かを差分の範囲の記録で確かめること。")
    return {
        "id": "intra-range-moves",
        "kind": "artifact-provenance",
        "content": "\n".join(lines),
        "moves": items,
    }
