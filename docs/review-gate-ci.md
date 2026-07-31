# Review gate as a GitHub required check (ms-119 / e-4073)

**Status: SCAFFOLD, default OFF.** Landing this changes nothing until a
repo-admin performs the two activation steps below. It spends no API money.

## Why

`beacon` fires an independent review (AX + maintainability) at PR-open and
refuses `beacon pr approve/merge` while any review is still owed (e-4060). But
that gate lives inside the beacon CLI — **`gh pr merge` and the GitHub UI merge
button bypass it**, and running the judge depends on an AI voluntarily calling
`/beacon-review-run`. So the review is skippable by merge route or by AI absence:
the "structural, non-bypassable" promise of ms-119 leaks at the last step.

This makes the gate **path-independent** by turning it into a GitHub commit
status (`beacon-review-gate`) that a branch-protection *required check* enforces.
Once required, **no merge route can complete** until the PR's reviews are
recorded — gh, UI, and beacon alike.

## What this scaffold does (and does not)

- **`set-pending` job**: sets `beacon-review-gate` = `pending` on PR open/sync
  (fast, no LLM), so the required check blocks the merge button immediately.
- **`run-judge` job** (ms-119 / e-4143): runs the independent judge **in CI** —
  for each required pr-open review type (AX + maintainability) it builds the
  review-kernel context (`beacon review context`), runs an LLM judge
  (`scripts/review-judge-ci.py`), posts the findings as an **advisory** PR
  comment, and — once every required review has run — flips the gate to
  `success`. If the judge cannot run (missing key, API error) the gate stays
  `pending` and the merge is blocked.
- The local flow (`beacon review done`, called by `/beacon-review-run`) can also
  flip the gate to `success` when a review is run interactively.

### Block policy (decided 2026-07-31, SPEC 方針4)

The gate blocks on **whether the review ran and was recorded**, *not* on whether
the judge found drift. Findings are **advisory** — posted as a comment, never
failing the check. This keeps AX / 思想 reviews advisory (方針4: gating findings
turns them into ignored drift warnings) while making the *running* of the review
a non-bypassable required check. (目的達成 stays human-gated — it is not a CI
judge run.)

## Activation (repo-admin — three steps)

1. **Add the judge's API key.** Set the repository *secret* (Settings → Secrets
   and variables → Actions → Secrets):

   ```
   ANTHROPIC_API_KEY = sk-ant-...
   ```

   The `run-judge` job fails without it (by design — a review that cannot run
   must not silently pass). Optional cost lever: set the repo *variable*
   `BEACON_REVIEW_JUDGE_MODEL` (alias `sonnet`/`opus`/`haiku`, or a full model
   id) to override each review type's `default_judge_model` (sonnet).

2. **Enable the workflow.** Set the repository *variable*:

   ```
   BEACON_REVIEW_GATE_CI = 1
   ```

   Until this is `1` both jobs are skipped (no runs, no cost).

3. **Require the check.** In branch protection for `main` (Settings → Branches →
   Branch protection rules), enable **"Require status checks to pass before
   merging"** and add the check context:

   ```
   beacon-review-gate
   ```

   A PR whose review hasn't run then shows the check as pending/expected and the
   merge button is blocked on every route (gh, UI, and `beacon` alike).

Optional, for the local flow to flip the check green automatically: export
`BEACON_REVIEW_GATE_CI=1` in the session where `/beacon-review-run` finishes
(otherwise flip it manually with `scripts/review-gate-ci.py set --state success
--sha <PR head sha>`).

### First-activation validation

Because the judge depends on `beacon review context` succeeding on the CI runner,
validate on one throwaway PR after activating: open a PR, confirm `run-judge`
posts an advisory comment for each type and the `beacon-review-gate` check goes
green, then confirm a PR with the judge disabled (or failing) leaves the check
pending and blocks merge.

## Cost

The judge runs once per required review type per PR sync. AX + maintainability =
2 LLM calls per push, on the sonnet-tier model by default. There is no cost until
`BEACON_REVIEW_GATE_CI=1` **and** `ANTHROPIC_API_KEY` are both set.
