"""Beacon Rollback boundary (ms-55 e-1647).

The "止まる側" half of ms-55 SPEC §4: after a stop signal lands and an
AI session needs to undo work it did, what is the line between
"auto-undo safe" and "needs human judgment"?

The line drawn here matches SPEC §4:

  +------------------------------+-----------------------------------+
  | State                        | Action                            |
  +------------------------------+-----------------------------------+
  | uncommitted edits            | git stash (auto)                  |
  | local commits (un-pushed)    | git reset --soft HEAD~N (auto)    |
  | pushed (un-merged)           | report + compensation proposal    |
  | merged / deployed            | report + revert PR proposal       |
  | sent DM / push notif / charge| report + correction-only proposal |
  +------------------------------+-----------------------------------+

The split is principled: anything still inside the local repo can be
undone without affecting other developers or external systems. The
moment a commit is pushed, undoing it requires `--force` which would
discard other people's work — that's a human-judgment call, never an
automatic one.

This module:

  * Inspects the working tree + un-pushed history to classify the state.
  * Provides `plan_rollback(commits=N)` to build a structured plan that
    can be dry-run or executed.
  * Provides `execute_rollback(plan)` for the auto-safe portion only.
  * Generates report text for the unsafe portion (pushed / merged /
    deployed / sent DM) describing concrete compensation options.

The actual `beacon rollback` CLI lives in lib/commands.py; this module
holds the pure logic so it can be unit-tested without standing up a
real git repo for every assertion (the tests use a tmp_path repo for
the integration paths and direct function calls for the rest).

C10 thin-action audit (ms-142 e-5527, spine §5): this module is class C
(pure machinery) — it has **no record(L2) layer** (never touches the
ledger / save_project) and its git adapter is **already isolated** in the
single ``_git`` seam, with read (``inspect_repo_state``) and write
(``execute_rollback``) separated at the function boundary. So it is
already in the target record/business/adapter shape. It deliberately does
NOT route through the shared ``git_read_port`` / ``git_write_port``: those
declare a *raise-on-failure*, cwd-less contract for the record-carrying
handlers, whereas rollback needs a *returncode-tolerant, cwd-scoped* git
seam (it inspects ``returncode`` to classify state and drives tmp_path
repos in tests). ``_git`` is that local adapter. Consolidating the two
idioms into one port would fracture the port API for no behavioral gain,
so this seam is audited-and-conformant, not merged.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Subprocess helper — keeps stderr out of test output unless something
# actually broke.
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Optional[str] = None,
         check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

@dataclass
class RepoState:
    """A snapshot of the local repo's rollback-relevant state.

    Fields:
      working_tree_dirty: True iff `git status --porcelain` returns rows.
      working_tree_files: paths reported by `git status --porcelain`.
      local_commits_ahead: number of commits on HEAD that are not on the
        upstream branch (= HEAD..@{u} count, but only counting our side).
      upstream_branch: the resolved upstream ref (e.g. "origin/main"), or
        empty if HEAD has no upstream configured.
      head_hash: HEAD commit hash (7-char short form, empty if no HEAD).
      branch: current branch name, empty for detached HEAD.
    """
    working_tree_dirty: bool = False
    working_tree_files: list[str] = field(default_factory=list)
    local_commits_ahead: int = 0
    upstream_branch: str = ""
    head_hash: str = ""
    branch: str = ""


def inspect_repo_state(cwd: Optional[str] = None) -> RepoState:
    """Classify the local repo's rollback-relevant state.

    Safe to call on any working tree. Failures (no git, not a repo, no
    upstream configured) translate into empty-string / 0-count fields
    rather than raising — the rollback planner downstream needs to be
    able to say "nothing to do" cleanly.
    """
    state = RepoState()

    # HEAD hash + branch (=ground truth for "we're in a git repo").
    r = _git("rev-parse", "--short=7", "HEAD", cwd=cwd)
    if r.returncode != 0:
        # Not a git repo, or repo with no commits yet.
        return state
    state.head_hash = r.stdout.strip()

    r = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    branch = r.stdout.strip() if r.returncode == 0 else ""
    state.branch = branch if branch != "HEAD" else ""  # detached → empty

    # Working tree dirtiness.
    r = _git("status", "--porcelain", cwd=cwd)
    if r.returncode == 0 and r.stdout.strip():
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        state.working_tree_dirty = True
        # status --porcelain format: "XY path", where XY is 2 chars.
        # For renames it's "R  old -> new"; we keep the trailing path.
        for ln in lines:
            # Strip the 2-char status code and 1-space separator.
            if len(ln) > 3:
                state.working_tree_files.append(ln[3:].strip())

    # Upstream tracking + ahead count.
    r = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
             cwd=cwd)
    if r.returncode == 0 and r.stdout.strip():
        state.upstream_branch = r.stdout.strip()
        r = _git(
            "rev-list", "--count", f"{state.upstream_branch}..HEAD",
            cwd=cwd,
        )
        if r.returncode == 0:
            try:
                state.local_commits_ahead = int(r.stdout.strip() or "0")
            except ValueError:
                state.local_commits_ahead = 0
    else:
        # No upstream configured — every local commit is implicitly
        # un-pushed. Count from the root.
        r = _git("rev-list", "--count", "HEAD", cwd=cwd)
        if r.returncode == 0:
            try:
                state.local_commits_ahead = int(r.stdout.strip() or "0")
            except ValueError:
                state.local_commits_ahead = 0

    return state


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

#: The maximum number of local commits we'll auto-undo in a single
#: rollback call. Past this threshold the operator should look at the
#: change history themselves — auto-undoing dozens of commits without
#: review re-introduces the same "stop the runaway?" question this MS
#: is supposed to solve.
DEFAULT_MAX_AUTO_COMMITS = 5


@dataclass
class RollbackPlan:
    """A structured description of what `beacon rollback` will do.

    Fields broken into two halves:

    Auto half — actions this layer will execute when the user invokes
    `beacon rollback` without `--dry-run`:
      stash_working_tree:   True iff `git stash` will be issued.
      reset_commits:        N > 0 iff `git reset --soft HEAD~N` will be issued.

    Report half — actions this layer will NOT execute; they're surfaced
    in the report for human review:
      pushed_warning:       True iff HEAD is past upstream, so undoing
                            the commit would require force-push (= can
                            destroy other people's work).
      compensation_options: list of concrete sub-actions the human can
                            choose from (e.g. "open revert PR").

    Additional metadata:
      state:                the RepoState the plan was built from.
      reason:               human-readable why (mirrors `beacon stop --reason`).
      requested_commits:    N the caller asked for (post-clipping).
    """
    stash_working_tree: bool = False
    reset_commits: int = 0
    pushed_warning: bool = False
    compensation_options: list[str] = field(default_factory=list)
    state: RepoState = field(default_factory=RepoState)
    reason: str = ""
    requested_commits: int = 0


def plan_rollback(
    state: RepoState,
    *,
    commits: int = 0,
    reason: str = "",
    max_auto_commits: int = DEFAULT_MAX_AUTO_COMMITS,
) -> RollbackPlan:
    """Build a RollbackPlan from a RepoState snapshot.

    Inputs:
      state: snapshot returned by inspect_repo_state.
      commits: number of HEAD commits the caller wants to undo. 0 means
        "auto-decide" — undo only as many local-only commits as exist,
        capped at max_auto_commits. Negative or >state.local_commits_ahead
        values are clipped to safe bounds.
      reason: human-readable reason recorded onto the plan for the report.
      max_auto_commits: cap on commits to undo automatically. Defaults
        to DEFAULT_MAX_AUTO_COMMITS.

    Returns a plan. The plan never includes operations that require
    `--force` (= rewriting pushed history); those become
    compensation_options entries in the report half.
    """
    plan = RollbackPlan(state=state, reason=reason)

    # 1. Working tree.
    if state.working_tree_dirty:
        plan.stash_working_tree = True

    # 2. Commits.
    #
    # When the caller passes commits=0 we treat it as "auto-decide". Two
    # sub-cases:
    #   (a) An upstream is configured → undo HEAD..@{u}, capped at the
    #       auto cap. This is the principled "local-only" definition.
    #   (b) No upstream → we cannot distinguish "local-only" from "every
    #       commit ever made". Resetting the root of history is almost
    #       never what the user wants for an Andon-cord style halt, so
    #       we default to 0 and require explicit --commits if they
    #       really want to undo something. The dirty-tree stash still
    #       runs in this case.
    if commits < 0:
        commits = 0
    if commits == 0:
        if state.upstream_branch:
            target = min(state.local_commits_ahead, max_auto_commits)
        else:
            target = 0  # no upstream → require explicit --commits
    else:
        target = min(commits, state.local_commits_ahead, max_auto_commits)
    plan.reset_commits = target
    plan.requested_commits = commits

    # 3. Pushed boundary — if the caller asked to undo more commits than
    # we have un-pushed, the surplus is "past upstream" and requires
    # report + compensation.
    if commits and commits > state.local_commits_ahead:
        plan.pushed_warning = True
        surplus = commits - state.local_commits_ahead
        plan.compensation_options.append(
            f"open a revert PR for {surplus} commit(s) past upstream "
            f"({state.upstream_branch or '(no upstream)'}): "
            "use `git revert <hash>` + `gh pr create`"
        )

    # 4. Commit cap — auto-rollback only goes so deep; the rest needs
    # human review even though it's still local. State a hint in the
    # compensation list so the operator doesn't think auto handled it.
    cap_overflow = (
        state.local_commits_ahead > max_auto_commits
        and (commits == 0 or commits > max_auto_commits)
    )
    if cap_overflow:
        excess = state.local_commits_ahead - max_auto_commits
        plan.compensation_options.append(
            f"{excess} additional local commit(s) beyond the auto cap "
            f"of {max_auto_commits}: re-run `beacon rollback --commits N` "
            "with a larger N if you are sure, or step through manually"
        )

    return plan


def render_report(plan: RollbackPlan) -> str:
    """Format a plan as the user-facing dry-run / post-execution report.

    The report is intentionally verbose — this is the surface that
    decides whether the operator trusts the rollback or stops it. It
    includes every action the plan would take and every action the
    plan deliberately did NOT take (with compensation proposals).
    """
    lines: list[str] = []
    state = plan.state

    lines.append("Beacon rollback plan")
    if plan.reason:
        lines.append(f"  reason: {plan.reason}")
    lines.append(f"  branch: {state.branch or '(detached HEAD)'}")
    lines.append(f"  HEAD:   {state.head_hash or '(no commits)'}")
    if state.upstream_branch:
        lines.append(f"  upstream: {state.upstream_branch}")
    else:
        lines.append("  upstream: (none configured)")
    lines.append(f"  local commits ahead: {state.local_commits_ahead}")
    lines.append("")

    lines.append("Auto-rollback actions (safe — no external side effects):")
    if not plan.stash_working_tree and plan.reset_commits == 0:
        lines.append("  (nothing — working tree clean and no local commits to undo)")
    if plan.stash_working_tree:
        files_preview = ", ".join(state.working_tree_files[:3])
        more = "" if len(state.working_tree_files) <= 3 else f" +{len(state.working_tree_files) - 3} more"
        lines.append(
            f"  • git stash push -u  (= save {len(state.working_tree_files)} dirty path(s): "
            f"{files_preview}{more})"
        )
    if plan.reset_commits > 0:
        lines.append(
            f"  • git reset --soft HEAD~{plan.reset_commits}  "
            f"(= undo {plan.reset_commits} local commit(s), keep changes staged)"
        )
    lines.append("")

    if plan.pushed_warning or plan.compensation_options:
        lines.append("Report-only items (need human review — NOT auto-executed):")
        if plan.pushed_warning:
            lines.append(
                "  ⚠ requested rollback extends past upstream; auto-undo would "
                "require `git push --force` which can destroy other developers' work."
            )
        for opt in plan.compensation_options:
            lines.append(f"  - {opt}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

@dataclass
class RollbackResult:
    """Outcome of executing a RollbackPlan.

    Fields:
      stashed:           True iff `git stash` succeeded.
      stash_ref:         Output of `git stash list -n1` after the stash
                         (= the stash@{0} description), if stashed.
      reset_commits:     N committed reset (matches plan.reset_commits
                         on success).
      errors:            list of error strings. Non-empty means execution
                         halted partway; caller should surface to the
                         user. The auto half is intentionally
                         best-effort: a partial rollback is better than
                         no rollback when the runaway is still
                         producing damage.
    """
    stashed: bool = False
    stash_ref: str = ""
    reset_commits: int = 0
    errors: list[str] = field(default_factory=list)


def execute_rollback(
    plan: RollbackPlan,
    *,
    cwd: Optional[str] = None,
) -> RollbackResult:
    """Execute the auto-safe half of `plan` against the given repo.

    Returns a RollbackResult. Failures populate `errors` but do not
    raise — the caller composes the final exit code from the result
    so the report can still be printed alongside any partial actions
    that did succeed.
    """
    result = RollbackResult()

    # 1. Working tree.
    if plan.stash_working_tree:
        msg = f"beacon rollback: {plan.reason or 'unspecified reason'}"
        r = _git("stash", "push", "-u", "-m", msg, cwd=cwd)
        if r.returncode != 0:
            result.errors.append(
                f"git stash failed: {r.stderr.strip() or 'unknown error'}"
            )
        else:
            result.stashed = True
            # Capture the stash ref so the user can `git stash show` it.
            ls = _git("stash", "list", "-n", "1", cwd=cwd)
            if ls.returncode == 0 and ls.stdout.strip():
                result.stash_ref = ls.stdout.splitlines()[0].strip()

    # 2. Local commits — soft reset keeps the change content; the user
    # can re-stage / re-commit / discard as they see fit.
    if plan.reset_commits > 0:
        r = _git(
            "reset", "--soft", f"HEAD~{plan.reset_commits}", cwd=cwd,
        )
        if r.returncode != 0:
            result.errors.append(
                f"git reset --soft HEAD~{plan.reset_commits} failed: "
                f"{r.stderr.strip() or 'unknown error'}"
            )
        else:
            result.reset_commits = plan.reset_commits

    return result


# ---------------------------------------------------------------------------
# Convenience top-level helper
# ---------------------------------------------------------------------------

def rollback(
    *,
    cwd: Optional[str] = None,
    commits: int = 0,
    reason: str = "",
    dry_run: bool = True,
    max_auto_commits: int = DEFAULT_MAX_AUTO_COMMITS,
) -> tuple[RollbackPlan, Optional[RollbackResult], str]:
    """End-to-end helper: inspect → plan → (execute) → report.

    Returns a 3-tuple of (plan, result_or_None, report_text). When
    dry_run is True, result is None and no git mutation runs. When
    False, the auto half is executed and result carries the outcome.

    The CLI in lib/commands.py wraps this, but the helper is exported
    so tests + future Skill integrations can call the whole pipeline
    in one shot.
    """
    state = inspect_repo_state(cwd=cwd)
    plan = plan_rollback(
        state, commits=commits, reason=reason,
        max_auto_commits=max_auto_commits,
    )
    if dry_run:
        return plan, None, render_report(plan)
    result = execute_rollback(plan, cwd=cwd)
    return plan, result, render_report(plan)
