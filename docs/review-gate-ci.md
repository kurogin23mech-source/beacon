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

- **Does**: `.github/workflows/review-gate.yml` sets `beacon-review-gate` =
  `pending` on PR open/sync (so the required check blocks the merge button), and
  `beacon review done` (called by `/beacon-review-run` once a judge produces a
  verdict) flips it to `success` via `gh`. The judge still runs through the
  existing AI review path.
- **Does NOT**: run an LLM in CI. Running an independent judge on GitHub's
  runners is the **deferred chunk** — it needs an `ANTHROPIC_API_KEY` secret,
  per-PR API cost, and a decision on which findings *block* vs merely report
  (AX / 思想 are advisory; 目的達成 is human-gated). Until that is designed, CI
  only enforces *that a review was run*, not the judge's verdict.

## Activation

### One command (ms-160 e-5805)

Both repo-admin steps are mechanized so activation is one auditable command
instead of manual settings clicks (needs `gh` with repo-admin auth):

```
# preview exactly what will change (no mutation):
python3 scripts/review-gate-ci.py activate --dry-run

# apply — sets the variable AND requires the check on main:
python3 scripts/review-gate-ci.py activate

# inspect current state / roll back:
python3 scripts/review-gate-ci.py status
python3 scripts/review-gate-ci.py deactivate
```

`activate` is **minimal by default**: it requires only the `beacon-review-gate`
check. It does not enforce admins or require PR-review approvals unless you ask:
`--enforce-admins` and `--require-pr-reviews N` opt into stricter policy. If
`main` already has branch protection, the gate check is **added** to the existing
required checks (never clobbering them); if not, minimal protection is created.
`activate` is idempotent — re-running when already active is a no-op.

> **Note:** turning this on blocks the merge button on `main` for *every* route
> (gh, UI, beacon) until each PR's reviews are recorded — including your own and
> external contributors'. That is the point (path-independent gate), but it is a
> repo-wide policy change, so review the `--dry-run` output before applying.

### Manual equivalent (Settings UI)

1. **Enable the workflow.** Set the repository *variable* (Settings → Secrets and
   variables → Actions → Variables):

   ```
   BEACON_REVIEW_GATE_CI = 1
   ```

   Until this is `1` the workflow job is skipped (no runs, no cost).

2. **Require the check.** In branch protection for `main` (Settings → Branches →
   Branch protection rules), enable **"Require status checks to pass before
   merging"** and add the check context:

   ```
   beacon-review-gate
   ```

   A PR with no recorded review then shows the check as pending/expected and the
   merge button is blocked on every route.

Optional, for the local flow to flip the check green automatically: export
`BEACON_REVIEW_GATE_CI=1` in the session where `/beacon-review-run` finishes
(otherwise flip it manually with `scripts/review-gate-ci.py set --state success
--sha <PR head sha>`).

## Deferred: LLM judge in CI (the "別の塊")

Running the judge itself in CI (so a review happens even with no AI session
present) is a separate task. Open questions to settle first: model + budget per
PR, blocking policy (which review types fail the check vs comment-only), and the
secret (`ANTHROPIC_API_KEY`). The scaffold above is forward-compatible: the CI
runner (`scripts/review-gate-ci.py`) already knows the required review types via
`review_spine.batch_review_types_for_node("pr-open")`, so the judge step slots in
where the `pending` status is set today.
