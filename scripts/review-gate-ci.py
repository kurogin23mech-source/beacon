#!/usr/bin/env python3
"""CI-side review gate (ms-119 / e-4073) — SCAFFOLD, default OFF.

Makes the independent-review gate PATH-INDEPENDENT. Today e-4060's gate only
refuses inside `beacon pr merge`; `gh pr merge` and the GitHub UI merge button
bypass it, and running the judge depends on an AI voluntarily calling
/beacon-review-run. This turns the gate into a GitHub commit-status
(`beacon-review-gate`) that a branch-protection *required check* enforces, so NO
merge path can complete until the PR's required reviews are recorded — regardless
of merge route or whether an AI is present.

This scaffold does NOT run an LLM in CI. Running an independent judge on GitHub's
runners is the deferred chunk (it needs an ANTHROPIC_API_KEY secret, per-PR API
cost, and a blocking-policy decision). Instead the split is:

  * PR open/sync → the workflow sets `beacon-review-gate` = pending, so the
    required check blocks the merge button immediately.
  * the LOCAL review flow (`beacon review done`, called by /beacon-review-run
    once a judge produces a verdict) flips it to success via `gh`.

The judge still runs through the existing AI path; CI only makes the "was it
run?" question impossible to bypass by merge route.

Activation (repo-admin — see docs/review-gate-ci.md):
  1. set repo variable  BEACON_REVIEW_GATE_CI=1   (workflow no-ops until then)
  2. branch protection: require the status check `beacon-review-gate`

Usage:
  review-gate-ci.py plan  [--pr N]                 # print the pending-status payload (pure)
  review-gate-ci.py set   --state <pending|success|failure> --sha <SHA> [--pr N] [--desc ...]
                                                    # post the commit status via gh (best-effort)
  review-gate-ci.py status [--branch main]         # report activation state (repo var + required check)
  review-gate-ci.py activate [--branch main] [--dry-run] [--enforce-admins] [--require-pr-reviews N]
                                                    # ms-160 e-5805: do the two repo-admin steps in one command
  review-gate-ci.py deactivate [--branch main] [--dry-run]   # turn the gate back off (rollback)
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


# ---------------------------------------------------------------------------
# Activation (ms-160 e-5805) — mechanize the two repo-admin steps so the gate
# can be turned on by ONE auditable command instead of manual GitHub-settings
# archaeology. SPEC 方針3: enforce by mechanism, not by request.
#   1. repo variable BEACON_REVIEW_GATE_CI = 1   (the workflow no-ops until then)
#   2. branch protection on `main`: require the `beacon-review-gate` check
# The command builders below are PURE (return the exact gh argv) so they can be
# unit-tested without mutating a live repo; `activate`/`deactivate` execute them.
# ---------------------------------------------------------------------------


def _run_gh(argv, stdin=None, timeout=30):
    """Run a gh command. Returns (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              input=stdin, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _api_path(suffix):
    """`/repos/<owner>/<repo><suffix>`; falls back to gh's cwd-remote resolution
    (`repos/{owner}/{repo}<suffix>`) when GITHUB_REPOSITORY is unset (local run)."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    return (f"/repos/{repo}{suffix}" if repo
            else f"repos/{{owner}}/{{repo}}{suffix}")


def _extract_contexts(required_status_checks):
    """Read the required check contexts from a protection payload, tolerating
    both the legacy ``contexts`` list and the newer ``checks`` objects."""
    if not isinstance(required_status_checks, dict):
        return []
    ctx = required_status_checks.get("contexts")
    if ctx is None:
        ctx = [c.get("context") for c in (required_status_checks.get("checks") or [])
               if isinstance(c, dict)]
    return [c for c in (ctx or []) if c]


def gate_status(branch="main"):
    """Report activation state: the repo variable value + whether the gate
    context is a required status check on `branch`. Best-effort; fields are
    None when gh can't answer (no auth / offline)."""
    rc, out, _ = _run_gh(["gh", "variable", "get", "BEACON_REVIEW_GATE_CI"])
    var_val = out.strip() if rc == 0 else None
    rc, out, _ = _run_gh(
        ["gh", "api", _api_path(f"/branches/{branch}/protection/required_status_checks")])
    if rc == 0:
        protected = True
        try:
            contexts = _extract_contexts(json.loads(out))
        except (ValueError, TypeError):
            contexts = []
    else:
        # 404 "Branch not protected" (or no access) — treat as unprotected.
        protected = False
        contexts = []
    gate_required = GATE_CONTEXT in contexts
    return {
        "branch": branch,
        "variable_BEACON_REVIEW_GATE_CI": var_val,
        "branch_protected": protected,
        "required_checks": contexts,
        "gate_required": gate_required,
        "active": var_val == "1" and gate_required,
    }


def _protection_put_payload(*, contexts, enforce_admins, require_pr_reviews):
    """Full PUT /branches/{b}/protection body (GitHub requires every top-level
    key, nullable). Minimal by default: only the review-gate check is required;
    admin enforcement and PR-review count are opt-in so activating the gate does
    not silently impose unrelated merge policy."""
    return {
        "required_status_checks": {"strict": False,
                                    "contexts": sorted(set(contexts))},
        "enforce_admins": bool(enforce_admins),
        "required_pull_request_reviews": (
            {"required_approving_review_count": int(require_pr_reviews)}
            if require_pr_reviews else None),
        "restrictions": None,
    }


def build_activate_plan(status, *, branch="main", enforce_admins=False,
                        require_pr_reviews=0):
    """Pure: given a gate_status() dict, return the ordered list of steps to
    activate. Each step = {label, argv, stdin?}. Idempotent — steps already
    satisfied are omitted so re-running is safe."""
    plan = []
    if status.get("variable_BEACON_REVIEW_GATE_CI") != "1":
        plan.append({
            "label": "set repo variable BEACON_REVIEW_GATE_CI=1",
            "argv": ["gh", "variable", "set", "BEACON_REVIEW_GATE_CI",
                     "--body", "1"],
        })
    if not status.get("gate_required"):
        if status.get("branch_protected"):
            # Additive: keep existing required checks, add ours (PATCH only the
            # required_status_checks node so the rest of protection is untouched).
            merged = sorted(set(status.get("required_checks") or []) | {GATE_CONTEXT})
            plan.append({
                "label": f"add '{GATE_CONTEXT}' to existing branch protection",
                "argv": ["gh", "api", "--method", "PATCH",
                         _api_path(f"/branches/{branch}/protection/required_status_checks"),
                         "--input", "-"],
                "stdin": json.dumps({"contexts": merged}),
            })
        else:
            # No protection yet → create minimal protection requiring the gate.
            payload = _protection_put_payload(
                contexts=[GATE_CONTEXT], enforce_admins=enforce_admins,
                require_pr_reviews=require_pr_reviews)
            plan.append({
                "label": f"create branch protection on '{branch}' "
                         f"requiring '{GATE_CONTEXT}'",
                "argv": ["gh", "api", "--method", "PUT",
                         _api_path(f"/branches/{branch}/protection"),
                         "--input", "-"],
                "stdin": json.dumps(payload),
            })
    return plan


def build_deactivate_plan(status, *, branch="main"):
    """Pure: steps to turn the gate OFF — set the variable to 0 and drop the
    gate context from required checks (leaving any other checks in place)."""
    plan = []
    if status.get("variable_BEACON_REVIEW_GATE_CI") not in (None, "0"):
        plan.append({
            "label": "set repo variable BEACON_REVIEW_GATE_CI=0",
            "argv": ["gh", "variable", "set", "BEACON_REVIEW_GATE_CI",
                     "--body", "0"],
        })
    if status.get("gate_required"):
        remaining = sorted(c for c in (status.get("required_checks") or [])
                           if c != GATE_CONTEXT)
        plan.append({
            "label": f"remove '{GATE_CONTEXT}' from branch protection",
            "argv": ["gh", "api", "--method", "PATCH",
                     _api_path(f"/branches/{branch}/protection/required_status_checks"),
                     "--input", "-"],
            "stdin": json.dumps({"contexts": remaining}),
        })
    return plan


def _execute_plan(plan, *, dry_run):
    """Run each plan step via gh. Returns a results list; stops at the first
    failure so a half-applied activation is visible, not silently continued."""
    results = []
    for step in plan:
        if dry_run:
            results.append({**{k: step[k] for k in ("label", "argv")},
                            "dry_run": True})
            continue
        rc, out, err = _run_gh(step["argv"], stdin=step.get("stdin"))
        ok = rc == 0
        results.append({"label": step["label"], "ok": ok,
                        "error": (err or "").strip()[:200] if not ok else ""})
        if not ok:
            break
    return results


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
    p_status = sub.add_parser("status")
    p_status.add_argument("--branch", default="main")
    p_activate = sub.add_parser("activate")
    p_activate.add_argument("--branch", default="main")
    p_activate.add_argument("--enforce-admins", dest="enforce_admins",
                            action="store_true")
    p_activate.add_argument("--require-pr-reviews", dest="require_pr_reviews",
                            type=int, default=0)
    p_activate.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_deactivate = sub.add_parser("deactivate")
    p_deactivate.add_argument("--branch", default="main")
    p_deactivate.add_argument("--dry-run", dest="dry_run", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        print(json.dumps(status_payload(args.state, pr=args.pr), ensure_ascii=False))
        return 0
    if args.cmd == "set":
        payload = status_payload(args.state, pr=args.pr, description=args.desc)
        ok = _post_status(args.sha, payload)
        print(json.dumps({**payload, "posted": ok}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "status":
        print(json.dumps(gate_status(args.branch), ensure_ascii=False))
        return 0
    if args.cmd == "activate":
        st = gate_status(args.branch)
        plan = build_activate_plan(
            st, branch=args.branch, enforce_admins=args.enforce_admins,
            require_pr_reviews=args.require_pr_reviews)
        if not plan:
            print(json.dumps({"already_active": True, **st}, ensure_ascii=False))
            return 0
        results = _execute_plan(plan, dry_run=args.dry_run)
        after = st if args.dry_run else gate_status(args.branch)
        ok = args.dry_run or all(r.get("ok") for r in results)
        print(json.dumps({"steps": results, "status": after}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "deactivate":
        st = gate_status(args.branch)
        plan = build_deactivate_plan(st, branch=args.branch)
        if not plan:
            print(json.dumps({"already_inactive": True, **st}, ensure_ascii=False))
            return 0
        results = _execute_plan(plan, dry_run=args.dry_run)
        after = st if args.dry_run else gate_status(args.branch)
        ok = args.dry_run or all(r.get("ok") for r in results)
        print(json.dumps({"steps": results, "status": after}, ensure_ascii=False))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
