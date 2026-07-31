#!/usr/bin/env python3
"""CI-side independent review judge (ms-119 / e-4143).

e-4073 landed the *gate* (a `beacon-review-gate` commit status a branch-protection
required check enforces), but left the judge itself running through the local AI
path (`/beacon-review-run` → `beacon review done`). That means the review only
happens if an AI session is present to run it. This script closes that gap: it
runs the independent judge **in CI**, so the review happens on every PR with no
AI session in the loop.

Block policy (ms-119 SPEC 方針4, decided 2026-07-31): the gate blocks on
*whether the review ran and was recorded*, NOT on whether the judge found drift.
Findings stay **advisory** — posted as a PR comment, never failing the check. The
forcing function is "a review provably happened on this PR"; acting on drift stays
the reviewer's call (AX/思想 = AI 完結・助言, per the review-kernel razor). So:

  * judge runs for each required pr-open review type (AX + maintainability) →
    findings posted as an advisory comment;
  * once every required type has run, the workflow flips `beacon-review-gate` to
    success via scripts/review-gate-ci.py;
  * if the judge cannot run (missing ANTHROPIC_API_KEY, API error), the step
    fails, the gate stays `pending`, and the required check blocks the merge —
    the review was NOT skippable, which is the whole point.

This script is the single-bundle core (one review type → judge result + comment).
It consumes the review-kernel bundle emitted by `beacon review context` verbatim
(原典 + mechanically-collected diff + gaps), so the CI judge sees exactly the
context-zero input a local judge subagent would — no implementer narrative. The
Anthropic call is isolated behind `run_judge(..., call_fn=)` so the shaping,
parsing, comment rendering, and model resolution are all unit-testable without a
network (tests/test_review_judge_ci_e4143.py).

Usage:
  review-judge-ci.py judge --context-file ctx.json \
      [--model <alias|full-id>] [--comment-out comment.md] [--result-out r.json]
      # reads the bundle JSON, runs the judge, prints the result JSON to stdout;
      # --comment-out writes the advisory PR-comment markdown;
      # exit 0 = judge ran, exit 1 = judge could not run (gate must stay pending).
"""

import argparse
import json
import os
import sys

# Alias → concrete model id. The review-type descriptor's `default_judge_model`
# is an alias ("sonnet") so the concrete id can move with the model catalog
# without editing every descriptor. Sonnet is the deliberate default for a
# cold structured-diff judge: it is the per-PR cost lever the e-4143 SPEC flags
# ("model + budget per PR"), and the AX / maintainability descriptors already
# declare default_judge_model="sonnet". Override per-run with --model / the
# BEACON_REVIEW_JUDGE_MODEL env var (accepts an alias or a full model id).
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_MODEL_ALIAS = "sonnet"

# How many tokens the judge may emit. Findings are a bounded structured list, not
# prose — 8000 is generous headroom while capping per-PR cost.
JUDGE_MAX_TOKENS = 8000


def resolve_model(bundle, override=""):
    """Concrete model id for the judge. Precedence: explicit --model/env override
    → the review type's descriptor default_judge_model → sonnet. An override or
    descriptor value is treated as an alias if it matches MODEL_ALIASES, else used
    verbatim (so a full id like 'claude-opus-4-8' passes through). Pure."""
    candidate = (override or "").strip()
    if not candidate:
        candidate = (bundle.get("judge_model_alias") or "").strip()
    if not candidate:
        candidate = DEFAULT_MODEL_ALIAS
    return MODEL_ALIASES.get(candidate, candidate)


def build_system_prompt(review_type):
    """Cold-judge framing. The judge inherits NONE of the implementer's context —
    only the 原典 + diff in the user message — so the system prompt states its
    role and the contract, never the change's intent. Pure."""
    return (
        "You are an independent, context-zero code-review judge running in CI. "
        "You did NOT write the change and have no prior conversation about it. "
        f"You are performing a '{review_type}' review.\n\n"
        "You are given a review-kernel bundle as JSON: `origin` is the 原典 (the "
        "written source of truth — principles / SPEC — the change is checked "
        "against), `artifact` is the mechanically-collected diff, `gaps` lists "
        "known blind spots in the bundle, and `external_references` may carry an "
        "application-map surface index. Judge ONLY the artifact against the "
        "origin. Do not invent code that is not in the diff; if the diff is "
        "insufficient to judge a point, say so rather than guessing.\n\n"
        "Your findings are ADVISORY — they inform, they do not block the merge. "
        "Report drift you can point to in the diff, with a concrete location. "
        "Be honest about uncertainty.\n\n"
        "Respond with a single JSON object and nothing else:\n"
        '{"summary": "<=2 sentence overall read", '
        '"findings": [{"severity": "high|medium|low", "title": "...", '
        '"where": "file:line or symbol", "detail": "what drifts from the origin '
        'and why"}]}\n'
        "Return an empty findings array if the change is faithful to the origin."
    )


def build_user_message(bundle):
    """The user turn: the bundle verbatim. It is already self-describing and
    carries no implementer narrative, so it is handed to the judge as-is. Pure."""
    return json.dumps(bundle, ensure_ascii=False, indent=2)


def parse_findings(text):
    """Best-effort extraction of {summary, findings} from the judge's reply.

    The gate flips on 'the review ran', not on parse success, so a judge reply
    that is not clean JSON must NOT fail the run — it is preserved verbatim as
    `raw` and surfaced in the comment. Returns (summary, findings, parsed_ok).
    Pure."""
    if not text or not text.strip():
        return "", [], False
    # Tolerate a fenced ```json block or leading prose by scanning for the first
    # balanced object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "", [], False
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return "", [], False
    if not isinstance(obj, dict):
        return "", [], False
    summary = obj.get("summary", "") if isinstance(obj.get("summary"), str) else ""
    findings = obj.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    # keep only well-shaped finding dicts; drop the rest silently (advisory).
    clean = [f for f in findings if isinstance(f, dict)]
    return summary, clean, True


def render_comment(review_type, target_ref, result):
    """Advisory PR-comment markdown. Explicitly labels itself non-blocking so a
    reader never mistakes an advisory finding for a merge blocker. Pure."""
    model = result.get("model", "")
    lines = []
    lines.append(f"### 独立レビュー (CI): {review_type} — {target_ref}")
    lines.append("")
    lines.append(
        "> _このコメントは **advisory (助言)** です。findings は merge を"
        "ブロックしません。gate (`beacon-review-gate`) が要求するのは『レビューが"
        "走って記録されたこと』であり、findings の合否ではありません (ms-119 方針4)。_"
    )
    lines.append(f"> _judge model: `{model}`_")
    lines.append("")
    if not result.get("ok"):
        lines.append(f"⚠ judge を実行できませんでした: {result.get('error', 'unknown')}")
        return "\n".join(lines)
    summary = result.get("summary", "").strip()
    if summary:
        lines.append(f"**要約**: {summary}")
        lines.append("")
    findings = result.get("findings", [])
    if not findings:
        lines.append("✅ 原典との drift は検出されませんでした。")
        if not result.get("parsed_ok", True):
            lines.append("")
            lines.append("<details><summary>judge raw output</summary>\n\n"
                         "```\n" + result.get("raw", "")[:4000] + "\n```\n</details>")
        return "\n".join(lines)
    order = {"high": 0, "medium": 1, "low": 2}
    for f in sorted(findings, key=lambda f: order.get(str(f.get("severity")), 3)):
        sev = str(f.get("severity", "?")).upper()
        title = str(f.get("title", "(no title)"))
        where = str(f.get("where", "")).strip()
        detail = str(f.get("detail", "")).strip()
        head = f"- **[{sev}]** {title}"
        if where:
            head += f" — `{where}`"
        lines.append(head)
        if detail:
            lines.append(f"  {detail}")
    return "\n".join(lines)


def _anthropic_call(system, user, model):
    """The real Anthropic Messages API call (lazy import so the package stays a
    CI-only dependency, never a runtime dep of beacon). Returns the reply text.

    Reads ANTHROPIC_API_KEY from the environment (the CI secret). Raises on any
    failure — the caller turns that into a non-zero exit so the gate stays
    pending (a review that could not run must not silently pass)."""
    import anthropic  # noqa: E402  (CI-only; not a beacon runtime dependency)

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=JUDGE_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def run_judge(bundle, *, model_override="", call_fn=None):
    """Run the judge for one review-kernel bundle. `call_fn(system, user, model)`
    is injectable for hermetic tests; defaults to the real Anthropic call.

    Returns a result dict: {review_type, target_ref, model, ok, summary,
    findings, parsed_ok, raw, error}. `ok=False` (with `error`) means the judge
    could not run — the caller must exit non-zero so the gate stays pending."""
    call_fn = call_fn or _anthropic_call
    review_type = bundle.get("review_type", "")
    target_ref = bundle.get("target_ref", "")
    model = resolve_model(bundle, model_override)
    system = build_system_prompt(review_type)
    user = build_user_message(bundle)
    base = {"review_type": review_type, "target_ref": target_ref, "model": model}
    try:
        raw = call_fn(system, user, model)
    except Exception as e:  # noqa: BLE001 — any failure = judge did not run
        return {**base, "ok": False, "summary": "", "findings": [],
                "parsed_ok": False, "raw": "", "error": f"{type(e).__name__}: {e}"}
    summary, findings, parsed_ok = parse_findings(raw)
    return {**base, "ok": True, "summary": summary, "findings": findings,
            "parsed_ok": parsed_ok, "raw": raw, "error": ""}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="review-judge-ci.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("judge", help="run the judge for one review-kernel bundle")
    p.add_argument("--context-file", required=True,
                   help="path to the `beacon review context` bundle JSON")
    p.add_argument("--model", default=os.environ.get("BEACON_REVIEW_JUDGE_MODEL", ""),
                   help="model alias (sonnet/opus/haiku) or full id; overrides the "
                        "review type's default_judge_model")
    p.add_argument("--comment-out", default="",
                   help="write the advisory PR-comment markdown to this path")
    p.add_argument("--result-out", default="",
                   help="write the full result JSON to this path (in addition to stdout)")
    args = ap.parse_args(argv)

    if args.cmd == "judge":
        try:
            with open(args.context_file, encoding="utf-8") as f:
                bundle = json.load(f)
        except (OSError, ValueError) as e:
            print(f"Error: cannot read context bundle {args.context_file!r}: {e}",
                  file=sys.stderr)
            return 1
        result = run_judge(bundle, model_override=args.model)
        out = json.dumps(result, ensure_ascii=False)
        print(out)
        if args.result_out:
            with open(args.result_out, "w", encoding="utf-8") as f:
                f.write(out)
        if args.comment_out:
            comment = render_comment(result.get("review_type", ""),
                                     result.get("target_ref", ""), result)
            with open(args.comment_out, "w", encoding="utf-8") as f:
                f.write(comment)
        # exit 1 when the judge could not run → the workflow leaves the gate
        # pending and the required check blocks the merge.
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
