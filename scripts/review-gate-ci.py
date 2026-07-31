#!/usr/bin/env python3
"""CI-side review gate (ms-119 / e-4073) — SCAFFOLD, default OFF.

Makes the independent-review gate PATH-INDEPENDENT. Today e-4060's gate only
refuses inside `beacon pr merge`; `gh pr merge` and the GitHub UI merge button
bypass it, and running the judge depends on an AI voluntarily calling
/beacon-review-run. This turns the gate into a GitHub commit-status
(`beacon-review-gate`) that a branch-protection *required check* enforces, so NO
merge path can complete until the PR's required reviews are recorded — regardless
of merge route or whether an AI is present.

This script sets/reads the gate status. The independent judge that flips the gate
now runs IN CI (ms-119 / e-4143, scripts/review-judge-ci.py) so a review happens
even with no AI session present. The lifecycle:

  * PR open/sync → set `beacon-review-gate` = pending, so the required check
    blocks the merge button immediately (fast, no LLM).
  * the CI judge runs each required pr-open review type, posts advisory findings,
    then flips the gate to success once every required review has run. The block
    policy is "did the review run?", never the verdict (SPEC 方針4).
  * the LOCAL review flow (`beacon review done`, called by /beacon-review-run)
    can also flip it to success via `gh` when a review is run interactively.

CI makes the "was it run?" question impossible to bypass by merge route.

Activation (repo-admin — see docs/review-gate-ci.md):
  1. set repo variable  BEACON_REVIEW_GATE_CI=1   (workflow no-ops until then)
  2. branch protection: require the status check `beacon-review-gate`
  3. add secret ANTHROPIC_API_KEY so the CI judge (e-4143) can run

Usage:
  review-gate-ci.py plan  [--pr N]                 # print the pending-status payload (pure)
  review-gate-ci.py set   --state <pending|success|failure> --sha <SHA> [--pr N] [--desc ...]
                                                    # post the commit status via gh (best-effort)
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import review_spine  # noqa: E402

GATE_CONTEXT = "beacon-review-gate"


def required_review_types():
    """The judge-run reviews that must be recorded before merge (the PR-open
    節目 pair: AX + maintainability). Data-driven from the review-type registry
    so a new pr-open review joins the gate with no edit here."""
    return [d["review"] for d in review_spine.batch_review_types_for_node("pr-open")]


def status_payload(state, *, pr="", description=""):
    """Shape the GitHub commit-status payload for the gate. Pure."""
    reviews = required_review_types()
    if not description:
        if state == "pending":
            description = ("独立レビュー (%s) 未記録 — /beacon-review-run で実行"
                           % ", ".join(reviews))
        elif state == "success":
            description = "独立レビュー (%s) 記録済み" % ", ".join(reviews)
        else:
            description = "独立レビュー gate: %s" % state
    return {
        "context": GATE_CONTEXT,
        "state": state,
        "description": description[:140],
        "required_reviews": reviews,
        "pr": pr,
    }


def _post_status(sha, payload):
    """Best-effort POST of the commit status via gh. Returns True on success,
    False otherwise (never raises — a CI/local status flip must not break the
    caller)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    # With GITHUB_REPOSITORY set (CI), address the repo explicitly; otherwise let
    # gh resolve {owner}/{repo} from the cwd remote (local `beacon review done`).
    endpoint = (f"/repos/{repo}/statuses/{sha}" if repo
                else f"repos/{{owner}}/{{repo}}/statuses/{sha}")
    argv = ["gh", "api", "-X", "POST", endpoint]
    argv += ["-f", f"state={payload['state']}",
             "-f", f"context={payload['context']}",
             "-f", f"description={payload['description']}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="review-gate-ci.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--pr", default="")
    p_plan.add_argument("--state", default="pending")
    p_set = sub.add_parser("set")
    p_set.add_argument("--state", required=True)
    p_set.add_argument("--sha", required=True)
    p_set.add_argument("--pr", default="")
    p_set.add_argument("--desc", default="")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        print(json.dumps(status_payload(args.state, pr=args.pr), ensure_ascii=False))
        return 0
    if args.cmd == "set":
        payload = status_payload(args.state, pr=args.pr, description=args.desc)
        ok = _post_status(args.sha, payload)
        print(json.dumps({**payload, "posted": ok}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
