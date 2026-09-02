#!/usr/bin/env python3
"""cmd_pr.py — the `beacon pr *` command family (ms-127 e-4856).

Extracted verbatim from commands.py (god-module split). The pr family drives the
GitHub-PR-driven review workflow: add / show / sync (record + reconcile PRs),
create (gh pr create + record), request-review / request-changes (reviewer
routing), approve / reject (verdict + auto-done judging), merge (merge-ban gate +
review-gate), close (trigger cleanup). Several handlers shell out to `gh`.

Depends only on commands_shared (upward) + leaf domain modules (core), never on
commands.py — acyclic (SPEC 方針4). This split promoted the review-due trigger
helpers that the pr handlers share with the `beacon review` family (which stays
in commands.py) into commands_shared: _fire_review_due_for_pr /
_fire_pr_open_review_triggers / _clear_review_due_for_pr /
_pending_review_types_for_pr / _pr_open_reviewed_marker_path + the
_REVIEW_DUE_SUFFIX constant. Both sides import them from there (no cmd_pr <->
commands cycle).

commands.py re-imports the PUBLIC handlers for dispatch + `commands.cmd_pr_*`;
the family-private helpers (_pr_number_from_url / _review_gate_check /
_fetch_gh_pr_info / ...) are NOT re-exported (patch them at cmd_pr.<name>).

Test patch target (the e-4320 rule) — TWO cases, because this family straddles
two modules:
  (a) HANDLER-ENTRY stubs (load_project / save_project / core): cmd_pr_*
      handlers resolve these in cmd_pr's OWN namespace (each
      `from commands_shared import name` binds an independent copy), so
      `monkeypatch.setattr(commands, "load_project", ...)` is a silent no-op on
      this call path. Patch `cmd_pr.load_project` instead. The outward gh/git
      calls now live behind the ports (ms-142 e-5527): the CANONICAL stub is at
      the port surface — `monkeypatch.setattr(gh_port, "pr_view", ...)` /
      `git_read_port.branch_show_current` etc (stubs the declared contract, stays
      stable across adapter changes). Patching `commands.subprocess.run` and
      dispatching by argv also works (the ports call the shared subprocess
      module) and is what older tests do, but prefer the port surface for new
      tests. (PR #690 review: one path declared canonical.)
  (b) TRIGGER-DIR stubs (_get_triggers_dir): the review-due trigger helpers
      (_fire_review_due_for_pr / _fire_pr_open_review_triggers /
      _clear_pr_open_review_triggers / ...) are DEFINED in commands_shared and
      resolve _get_triggers_dir in commands_shared's namespace. To redirect
      trigger-file I/O for a cmd_pr handler test, patch
      `commands_shared._get_triggers_dir` (patching cmd_pr._get_triggers_dir
      would be a silent no-op — cmd_pr does not even import that name).
"""

import os
import sys
import json
import re

import core  # noqa: F401
import gh_port
import git_read_port

# ms-142 e-5527 (spine §5): outward forge/vcs calls live behind the ports —
# gh (pr view/list/create) → gh_port, read-only git (branch/log for MS
# inference) → git_read_port. The soft-fail wrappers (_fetch_gh_pr_info /
# _fetch_gh_pr_list_all) keep their best-effort {}/[] policy (business); the
# raw `gh` call is the adapter behind the port. record(L2) stays in core /
# save_project / _record_review_decision.

from commands_shared import (  # noqa: F401
    _session_kind_is_human,
    _check_ms_status_for_write,
    _clear_pr_open_review_triggers,
    _fire_pr_open_review_triggers,
    _pending_review_types_for_pr,
    _resolve_session_id,
    load_project,
    save_project,
)


def _pr_number_from_url(url: str) -> str:
    """PR number as a string from a GitHub PR URL, or "" if none.

    Delegates to the canonical parser (``core._extract_pr_number_from_url``) so
    the two never drift; returns a string for use in the trigger filename."""
    n = core._extract_pr_number_from_url(url)
    return str(n) if n is not None else ""

def _review_gate_check(pr_number: str, *, action: str) -> None:
    """Refuse ``action`` (approve / merge) while independent reviews are still
    owed for this PR, unless BEACON_PR_REVIEW_OVERRIDE=1 (which proceeds but
    prints an audit line). Exits 2 on block. ms-119 e-4060."""
    pending = _pending_review_types_for_pr(pr_number)
    if not pending:
        return
    if os.environ.get("BEACON_PR_REVIEW_OVERRIDE") == "1":
        print(f"⚠ 独立レビュー未実施のまま PR #{pr_number} を {action} します "
              f"(未実施: {', '.join(pending)}) — この {action} は override であり "
              f"監査対象です。", file=sys.stderr)
        return
    runs = "\n".join(
        f"    /beacon-review-run --type {t} --pr {pr_number}" for t in pending)
    print(
        f"Error: PR #{pr_number} は独立レビューが未実施のため {action} できません "
        f"(未実施: {', '.join(pending)})。\n"
        f"  ms-119 e-4060: レビューは PR-open の節目で発火し、実行するまで "
        f"approve/merge を構造的に塞ぎます (発火だけで消費されない穴の是正)。\n"
        f"  文脈ゼロの独立 judge に原典+差分を渡して実行:\n{runs}\n"
        f"  実行後は `beacon review done --type <type> --pr {pr_number}` で解消 "
        f"(/beacon-review-run が最後に自動で叩きます)。\n"
        f"  やむを得ず飛ばす場合のみ: BEACON_PR_REVIEW_OVERRIDE=1 "
        f"(監査痕跡が残ります)。",
        file=sys.stderr)
    sys.exit(2)

def _fetch_gh_pr_info(url: str) -> dict:
    """Fetch PR title, body, and commits from GitHub. Returns {} on failure.

    Best-effort wrapper (business): the raw `gh pr view` is gh_port.pr_view;
    this keeps the soft-fail-to-{} policy the callers rely on."""
    try:
        return gh_port.pr_view(url)
    except Exception:
        return {}

def cmd_pr_add():
    import datetime
    url = os.environ.get("BEACON_URL", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    author = os.environ.get("BEACON_AUTHOR", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    date = os.environ.get("BEACON_DATE", "") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not url:
        print("Error: GitHub URL required", file=sys.stderr)
        sys.exit(1)

    # Fetch PR title, body, and commits from GitHub (before intent prompt so body can prefill)
    gh_info = _fetch_gh_pr_info(url)
    title = gh_info.get("title", "")
    pr_body = gh_info.get("body", "") or ""
    commits = gh_info.get("commits", [])
    if gh_info and not title:
        print("Warning: could not fetch PR title from GitHub", file=sys.stderr)

    if not intent:
        try:
            if pr_body.strip():
                # Show PR body as prefill hint so user can accept or edit
                print(f"PR body (prefill):\n  {pr_body.strip()[:300]}")
                prefill_hint = f" [{pr_body.strip()[:120]}]" if len(pr_body.strip()) <= 120 else ""
                raw = input(f"Intent (why was this PR created?){prefill_hint}: ").strip()
                intent = raw if raw else pr_body.strip()
            else:
                intent = input("Intent (why was this PR created?): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = pr_body.strip() if pr_body.strip() else ""

    data = load_project()
    # ms-81 e-1916: status gate. Surface the warning before adding a PR
    # entry to a non-write-authorised MS so the operator can re-target
    # rather than discover the issue later in retro.
    try:
        target_ms = core.find_target_milestone(data, ms_id)
    except ValueError:
        target_ms = None
    if target_ms is not None:
        if not _check_ms_status_for_write(
            target_ms, f"pr add {url}"
        ):
            sys.exit(1)
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=url, author=author,
                          intent=intent, date=date, title=title, commits=commits,
                          session_id=_resolve_session_id())  # ms-57 / e-1062
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    # ms-166 e-5972: write-through — derive a pr-intent decision from the PR's
    # declared intent at record time, so the "why" of this change lands on the
    # decision arm without a separate `beacon decision record` (成果物からの導出).
    _derive_pr_intent_decision(_pr_number_from_url(url), title, intent)

    # ms-119 e-4003: a recorded PR is the beacon-owned PR-open 契機 — auto-bind
    # every PR-bound review (AX + maintainability, data-driven from the registry)
    # so they fire structurally at the 節目 (not only when a human remembers
    # /beacon-review-run). Advisory, non-blocking.
    _fire_pr_open_review_triggers(_pr_number_from_url(url), title, url)

    # ms-80 e-1821: 同一 MS に並列で open PR が他にもあれば claim 競合の可能性
    # を author に知らせる (= 警告のみ、block しない)。
    conflicts = _detect_pr_claim_conflict(data, ms_id, eid)

    if json_mode:
        out = {"entry_id": eid, "url": url, "title": title, "intent": intent,
               "commits": len(commits)}
        if conflicts:
            out["claim_conflicts"] = conflicts
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Added PR [{eid}]: {title or url}")
        if commits:
            print(f"  Commits: {len(commits)} linked")
        if intent:
            print(f"  Intent: {intent}")
        if conflicts:
            print(f"")
            print(f"⚠ 同じ {ms_id} に並列で open な PR が {len(conflicts)} 件あります (= claim 競合の可能性):")
            for c in conflicts[:5]:
                author_str = f" by {c['author']}" if c.get("author") else ""
                intent_str = f" — {c['intent'][:60]}" if c.get("intent") else ""
                print(f"  [{c['eid']}] {c['title'] or c['url']}{author_str}{intent_str}")
            if len(conflicts) > 5:
                print(f"  (... and {len(conflicts) - 5} more)")
            print(f"  推奨: `beacon claim` で作業範囲を調整、または各 PR の intent を見直して重複を解消してください。")

def _detect_pr_claim_conflict(data: dict, ms_id: str, new_eid: str) -> list:
    """Return open PR entries under ms_id that may conflict with the newly
    added PR (= new_eid). Excludes the new entry itself.

    A "conflict" here means another PR is also in_review against the same MS.
    Author and reviewer should be aware to either coordinate via beacon claim
    (= ms-55 claim primitives) or split the MS into smaller pieces. The detection
    is structural (= same ms_id + status=in_review), not semantic — actual
    overlap is judged by the humans.
    """
    if not ms_id:
        return []
    conflicts = []
    for ms in data.get("milestones", []):
        if ms.get("id") != ms_id:
            continue
        for e in ms.get("entries", []):
            if e.get("type") != "pr":
                continue
            if e.get("id") == new_eid:
                continue
            if e.get("status") not in ("in_review", "open"):
                continue
            meta = e.get("meta") or {}
            if meta.get("pr_status") not in ("in_review", "open", None):
                continue
            conflicts.append({
                "eid": e.get("id"),
                "title": e.get("description", ""),
                "url": meta.get("url", ""),
                "author": meta.get("author", ""),
                "intent": meta.get("intent", ""),
                "date": e.get("date", ""),
            })
        break
    return conflicts

def cmd_pr_show():
    """Show a PR record's full detail (intent / commits / review history).

    Used by /review to pull intent before judging code changes (e-608).
    Resolution rules for the input identifier:
      - `e-NNN`         → entry id direct match
      - `<int>`         → PR number; looks up the matching entry
      - `https://…/pull/<n>` → URL match
    """
    ident = (os.environ.get("BEACON_PR_IDENT", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ident:
        print("Error: PR identifier required (e-id, PR number, or URL)", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # Find candidate PR entries across all milestones
    found = None
    found_ms_id = ""
    for ms in data.get("milestones", []):
        if not isinstance(ms, dict):
            continue
        for ent in ms.get("entries", []) or []:
            if not isinstance(ent, dict) or ent.get("type") != "pr":
                continue
            meta = ent.get("meta") or {}
            # Match by entry id
            if ent.get("id") == ident:
                found, found_ms_id = ent, ms.get("id", "")
                break
            # Match by PR number (int or numeric string)
            try:
                if ident.isdigit() and int(ident) == meta.get("pr_number"):
                    found, found_ms_id = ent, ms.get("id", "")
                    break
            except (ValueError, AttributeError):
                pass
            # Match by URL
            if meta.get("url") and (meta["url"] == ident or meta["url"].rstrip("/") == ident.rstrip("/")):
                found, found_ms_id = ent, ms.get("id", "")
                break
        if found:
            break

    if not found:
        print(f"Error: no PR entry matches '{ident}'", file=sys.stderr)
        sys.exit(1)

    meta = found.get("meta") or {}
    payload = {
        "entry_id": found.get("id"),
        "ms_id": found_ms_id,
        "description": found.get("description"),
        "status": found.get("status"),
        "url": meta.get("url"),
        "pr_number": meta.get("pr_number"),
        "author": meta.get("author"),
        "intent": meta.get("intent") or "",
        "pr_status": meta.get("pr_status"),
        "review_status": meta.get("review_status"),
        "review_rationale": meta.get("review_rationale"),
        # e-609: review back-and-forth visibility — full transition history
        # so the timeline can render "pending → changes_requested → pending → approved"
        "review_history": meta.get("review_history") or [],
        "commits": [
            {
                "id": c.get("id"),
                "hash": (c.get("meta") or {}).get("hash"),
                "message": c.get("description"),
            }
            for c in (found.get("entries") or [])
            if isinstance(c, dict) and c.get("type") == "commit"
        ],
    }

    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"PR {payload['entry_id']} (ms: {found_ms_id})")
    if payload["url"]:
        print(f"  URL:    {payload['url']}")
    print(f"  Title:  {payload['description']}")
    print(f"  Status: {payload['status']} / review: {payload['review_status']}")
    if payload["intent"]:
        print(f"  Intent: {payload['intent']}")
    else:
        print(f"  Intent: (none recorded — /review cannot do intent-vs-impl check)")
    if payload["review_rationale"]:
        print(f"  Rationale: {payload['review_rationale']}")
    if payload["review_history"]:
        # e-609: surface review back-and-forth count + the transition trail
        roundtrips = sum(
            1 for i, h in enumerate(payload["review_history"][1:], 1)
            if h["status"] == "pending"
        )
        print(f"  Review history ({len(payload['review_history'])} transitions, "
              f"{roundtrips} round-trip(s)):")
        for h in payload["review_history"]:
            at = h.get("at", "")[:19]  # trim to "YYYY-MM-DDTHH:MM:SS"
            tail = f" — {h['rationale'][:60]}" if h.get("rationale") else ""
            print(f"    {at}  {h.get('status', '?'):<19}{tail}")
    if payload["commits"]:
        print(f"  Commits ({len(payload['commits'])}):")
        for c in payload["commits"]:
            print(f"    {c['hash']}  {c['message']}")

def cmd_pr_close():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_close(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    # ms-119 e-4003: the interface-change 節目 is resolved — stop re-surfacing the
    # AX review nudge for this PR.
    _clear_pr_open_review_triggers(_pr_number_from_url(entry.get("meta", {}).get("url", "")))

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "closed"}, ensure_ascii=False))
    else:
        print(f"Closed PR [{entry_id}]: {entry.get('description', '')}")

def _collect_pr_bound_task_ids(pr_entry: dict, data: dict) -> list:
    """ms-95 / e-2369 — collect the task IDs bound to this PR's commits.

    Two signal sources are unioned, in order:

    1. **Hash-join with beacon-logged commits' ``meta.resolves``** —
       ``pr_add`` stores child commits with only ``meta.hash``, but the
       beacon-logged side (= the entry created by ``beacon log``) carries
       ``meta.resolves = "e-XXX"`` whenever the developer passed
       ``--resolves <task-id>``. Joining by 7-char hash lifts that binding
       up to the PR.
    2. **``e-\\d+`` regex on commit messages** — for projects that don't
       pass ``--resolves`` but mention the task id in the commit message
       (``feat(ms-X): e-YYY ...``), this fallback still catches the
       binding. Common in this very repo's commit log.

    Returns a de-duplicated list of task IDs (strings). Order: stable on
    first encounter so the auto-done report is deterministic.
    """
    bound: list = []
    seen: set = set()

    def _record(tid: str):
        if tid and tid not in seen:
            seen.add(tid)
            bound.append(tid)

    # Build hash → resolves map from beacon-logged commit entries.
    hash_to_resolves: dict = {}
    for ms in data.get("milestones", []):
        if not isinstance(ms, dict):
            continue
        for ent in ms.get("entries", []) or []:
            if not isinstance(ent, dict) or ent.get("type") != "commit":
                continue
            meta = ent.get("meta") or {}
            h = (meta.get("hash") or "")[:7]
            r = (meta.get("resolves") or "").strip()
            if h and r:
                hash_to_resolves[h] = r

    # Walk PR's commit children.
    for child in pr_entry.get("entries", []) or []:
        if not isinstance(child, dict) or child.get("type") != "commit":
            continue
        meta = child.get("meta") or {}
        h = (meta.get("hash") or "")[:7]
        # Signal 1: hash-joined resolves
        if h and h in hash_to_resolves:
            _record(hash_to_resolves[h])
        # Signal 1b: a resolves field may have been set on the PR child
        # directly (forward-compat — currently pr_add doesn't, but future
        # callers might).
        r_direct = (meta.get("resolves") or "").strip()
        if r_direct:
            _record(r_direct)
        # Signal 2: regex on the commit message text.
        msg = child.get("description", "") or ""
        for m in re.findall(r"e-\d+", msg):
            _record(m)

    return bound

def _judge_pr_approve_auto_done(pr_entry: dict, data: dict) -> list:
    """ms-95 / e-2369 — produce per-task judgement for PR approve auto-done.

    Mirrors `/beacon-log` Skill Step 1.9's HIGH / MID / LOW × DONE / SKIP
    judgement, but lives Python-side because `beacon pr approve` is a CLI
    call (not an AI Skill prompt). The matching corpus is the PR
    title/body/intent + every child commit message. The candidate task
    set comes from `_collect_pr_bound_task_ids`.

    Confidence rules (mirrors Step 1.9):
      * **HIGH** — task ID is explicitly mentioned in the PR title /
        intent / one of the commit messages, AND at least one
        description / AC keyword overlaps with the same corpus.
      * **MID**  — keyword overlap only (≥ 1 description / AC keyword
        matches), but no explicit task-id mention in the corpus. Surface
        as a candidate but do not auto-done.
      * **LOW**  — only the task-id was found via a weak signal (e.g.
        resolves was set in beacon log but the description doesn't even
        share a token). Silently dropped.

    Returns a list of dicts:
        {"task_id": str, "confidence": "HIGH"|"MID"|"LOW",
         "description": str, "ac_matched": [str, ...],
         "reason": str}

    Skips tasks that are already done / cancelled (idempotent).
    """
    # Compose the matching corpus.
    pr_meta = pr_entry.get("meta") or {}
    pr_title = pr_entry.get("description", "") or ""
    pr_intent = pr_meta.get("intent", "") or ""
    commit_msgs = [
        (c.get("description") or "")
        for c in (pr_entry.get("entries") or [])
        if isinstance(c, dict) and c.get("type") == "commit"
    ]
    corpus_text = " \n".join([pr_title, pr_intent, *commit_msgs])
    corpus_lower = corpus_text.lower()
    corpus_tokens = core._tokenize(corpus_text)

    bound = _collect_pr_bound_task_ids(pr_entry, data)
    judgements: list = []

    for tid in bound:
        result = core.find_entry(data, tid)
        if not result:
            # Bound id points to a nonexistent / removed entry — skip
            # silently (matches PR-not-found fail-soft style).
            continue
        _ms, _parent, task_entry, _idx = result
        if task_entry.get("type") != "task":
            # The bind pointed at a commit / note / PR — not actionable.
            continue
        if task_entry.get("status") in ("done", "cancelled"):
            # Idempotent: a re-approve doesn't re-done.
            continue

        # Build task-side text for keyword matching.
        task_desc = task_entry.get("description", "") or ""
        ac_text = task_entry.get("acceptance_criteria", "") or ""
        motivation = task_entry.get("motivation", "") or ""
        task_side_text = " ".join([task_desc, ac_text, motivation])
        task_tokens = core._tokenize(task_side_text)
        overlap = task_tokens & corpus_tokens
        ac_matched = sorted(overlap)[:5]

        # HIGH: task-id explicit in corpus AND ≥1 keyword overlap.
        # MID:  keyword overlap only, no explicit mention.
        # LOW:  no overlap (drops silently).
        explicit_mention = tid in re.findall(r"e-\d+", corpus_text)
        if explicit_mention and overlap:
            confidence = "HIGH"
        elif overlap:
            confidence = "MID"
        elif explicit_mention:
            # Explicit binding but zero keyword overlap → conservative
            # MID (= surface for user review, don't auto-done).
            confidence = "MID"
        else:
            confidence = "LOW"

        if confidence == "LOW":
            continue

        pr_num = pr_meta.get("pr_number")
        pr_label = f"#{pr_num}" if pr_num else pr_entry.get("id", "")
        kw_list = ", ".join(ac_matched) if ac_matched else "(no AC keywords)"
        reason = (
            f"Auto-done from PR approve [{pr_label}]: "
            f"{(pr_title or '').strip()[:120]}. "
            f"AC keywords matched: {kw_list}"
        )
        judgements.append({
            "task_id": tid,
            "confidence": confidence,
            "description": task_desc,
            "ac_matched": ac_matched,
            "reason": reason,
        })

    return judgements


def _decided_by_for_review() -> str:
    """The decided_by for a PR review adjudication, derived from the SESSION KIND
    (ms-154 e-5669) rather than hardcoded ``autonomous-AI``.

    Before this, ``_record_review_decision`` recorded ``autonomous-AI`` for every
    approve/reject/re-work — even when a HUMAN pressed the button — so the audit
    field mis-attributed human decisions to the AI. An AI session adjudicating on
    the independent-judge verdicts is the most-audit-critical ``autonomous-AI``
    owner (the docstring's original intent); a human terminal session decided it
    directly. Mirrors ``cmd_target._decided_by_for_gate`` for cross-capability
    consistency (its ``human-delegated`` gloss-vs-usage tension is a known
    follow-up, e-5670 — NOT re-litigated here).

    ms-166 e-5971 (保守性 M1): this is the single source for the 採否 decided_by
    mapping — ``commands._record_review_adjudication_decision`` (review-adjudication)
    imports and calls THIS function rather than re-implementing it, so the PR-verdict
    採否 and the finding-level 採否 cannot drift."""
    return "human-delegated" if _session_kind_is_human() else "autonomous-AI"


def _review_evidence_from_env() -> list:
    """Real evidence links a caller cites for a review adjudication, from
    ``BEACON_EVIDENCE`` (newline-separated, empties dropped) — ms-154 e-5669.

    The point is that evidence is now CAPTURABLE at all: the params existed but
    no call site or CLI flag fed them, so review decisions were structurally
    evidence-empty even at approve time (when findings docs exist). Empty stays
    honest-allowed (e-5650) — this only makes non-empty POSSIBLE. Never carries
    the PR self-reference (``related.task_id`` already does)."""
    raw = os.environ.get("BEACON_EVIDENCE", "")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _record_review_decision(entry_id: str, verdict: str, rationale: str,
                            decided_by: str = "autonomous-AI",
                            evidence=None) -> None:
    """ms-154 e-5593 — best-effort: record a PR review verdict to the decision arm.

    review 採否 (approve / re-work / reject) は CLI 側の判断で server の書き込み口
    (= POST /api/projects/{id}/decisions) を通して統一 decision stream に載せる。
    cloud mode 専用 (= decision stream は server 側)。承認フローを絶対に壊さない:
    offline / 未ログイン / server error は全て握りつぶす (= flag not gate)。

    ``verdict`` は ``approve`` / ``re-work`` / ``reject``。``decided_by`` は誰が採否を
    決めたか (default ``autonomous-AI`` = 独立レビューは AI judge verdict が主で最も監査
    が要る。人間が採否を指示した場合は呼び出し側が上書き)。``evidence`` は採否を裏付ける
    **実 link (findings doc / 会話 / commit) のみ** で、空でも通す (ms-154 e-5650): 対象
    PR 自身への参照 (``pr:<id>``) は ``related.task_id`` が運ぶので自己参照は積まない。
    who は server が token から stamp する。
    """
    try:
        from commands_shared import _is_cloud_mode, _get_api_client
        if not _is_cloud_mode():
            return
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            return
        client.record_decision(project_id, {
            "kind": "review-adjudication",
            "decision": verdict,
            "rationale": rationale or None,
            "decided_by": decided_by,
            "evidence": [ln for ln in (evidence or []) if ln],
            "related": {"task_id": entry_id},
        })
    except BaseException:
        # best-effort: _get_api_client may sys.exit (SystemExit) when creds are
        # missing; swallow everything so decision recording never breaks the
        # approve / reject / request-changes flow.
        pass


def _derive_pr_intent_decision(pr_number, title: str, intent: str) -> None:
    """ms-166 e-5972 — write-through: derive a ``pr-intent`` decision from the PR's
    declared intent at record time, so a change's "why" reaches the decision arm as
    a DERIVED product (no separate ``beacon decision record``). No-op when the PR
    carries no intent (nothing to derive). cloud-only, best-effort but LOGGED on
    failure (``best_effort_decision_write``) so it never breaks ``pr add``."""
    import decision_derive
    payload = decision_derive.build_pr_intent_decision(
        pr_number, title, intent, decided_by=_decided_by_for_review())
    if payload is None:
        return
    from commands_shared import (best_effort_decision_write, _is_cloud_mode,
                                 _get_api_client)
    with best_effort_decision_write(f"pr-intent for PR #{pr_number}"):
        if not _is_cloud_mode():
            return
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if project_id:
            client.record_decision(project_id, payload)


def cmd_pr_approve():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    no_auto_done = os.environ.get("BEACON_NO_AUTO_DONE", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("承認の根拠・受け入れたトレードオフは？ (Rationale for approval): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for approve. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_approve(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # ms-119 e-4060: refuse to approve while independent reviews (AX /
    # maintainability) are still owed for this PR. Runs BEFORE save_project, so a
    # blocked approve persists nothing (the in-memory pr_approve is discarded on
    # exit). This is the GATE half of closing the review loop — firing a review
    # trigger is meaningless unless approve/merge structurally wait on it.
    _review_gate_check(_pr_number_from_url(entry.get("meta", {}).get("url", "")),
                       action="approve")

    # ms-95 / e-2369 — auto-done tasks bound to this PR's commits via
    # `meta.resolves` (or `e-XXX` mention in the commit messages). HIGH
    # confidence → done with explicit `done_reason`; MID → surface as a
    # warning for the user to follow up; LOW → silent. Skipped entirely
    # when --no-auto-done is set (= BEACON_NO_AUTO_DONE=1).
    auto_done_results: list = []
    mid_warnings: list = []
    if not no_auto_done:
        try:
            judgements = _judge_pr_approve_auto_done(entry, data)
        except Exception:
            # Fail-soft: never break the approve flow because of the
            # auto-done helper. The save below still records the approve.
            judgements = []
        for j in judgements:
            if j["confidence"] == "HIGH":
                try:
                    core.task_done(
                        data, j["task_id"], reason=j["reason"]
                    )
                    auto_done_results.append(j)
                except ValueError:
                    # Task disappeared between collect and done — skip.
                    pass
            elif j["confidence"] == "MID":
                mid_warnings.append(j)

    save_project(data)
    _record_review_decision(entry_id, "approve", rationale,
                            decided_by=_decided_by_for_review(),
                            evidence=_review_evidence_from_env())

    if json_mode:
        out = {
            "entry_id": entry_id,
            "review_status": "approved",
            "review_rationale": rationale,
            "auto_done": [
                {"task_id": j["task_id"], "reason": j["reason"]}
                for j in auto_done_results
            ],
            "mid_candidates": [
                {"task_id": j["task_id"], "description": j["description"]}
                for j in mid_warnings
            ],
        }
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Approved PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")
        for j in auto_done_results:
            print(
                f"  ✓ Auto-done: {j['task_id']} ({j['description'][:60]}) "
                f"— HIGH confidence"
            )
        for j in mid_warnings:
            print(
                f"  ⚠ Candidate: {j['task_id']} ({j['description'][:60]}) "
                f"— MID confidence, run "
                f"'beacon task done {j['task_id']} --reason ...' if AC is met"
            )

def cmd_pr_reject():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("却下の理由・懸念点は？ (Rationale for rejection): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for reject. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_reject(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    _record_review_decision(entry_id, "reject", rationale,
                            decided_by=_decided_by_for_review(),
                            evidence=_review_evidence_from_env())

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "rejected",
                          "review_rationale": rationale}, ensure_ascii=False))
    else:
        print(f"Rejected PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")

def cmd_pr_request_review():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms, entry = core.pr_request_review(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "in_review"}, ensure_ascii=False))
    else:
        print(f"In review: [{entry_id}]: {entry.get('description', '')}")

def cmd_pr_request_changes():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("修正要求の理由・具体的な懸念点は？ (Reason for requesting changes): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for request-changes. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_request_changes(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    # ms-154 e-5652 (naming 対照): the same "send it back to be fixed" act wears two
    # deliberately-distinct names because it lives in two vocabularies:
    #   - PR review STATE machine → ``changes_requested`` (GitHub-aligned:
    #     pending → changes_requested → approved; see cmd_pr.py:297).
    #   - decision-arm VERDICT enum → ``re-work`` (approve / re-work / reject, shared
    #     with the Trek leader-review verdict; see server/decision_event.py:377).
    # They are NOT unified: the state machine and the verdict vocabulary are separate
    # axes. This mapping is the one point they meet, so it is named here explicitly.
    _record_review_decision(entry_id, "re-work", rationale,
                            decided_by=_decided_by_for_review(),
                            evidence=_review_evidence_from_env())

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "changes_requested"}, ensure_ascii=False))
    else:
        print(f"Changes requested on PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Reason: {rationale}")

def _trek_finalize_consent_active() -> bool:
    """ms-92 / e-2169 — Trek 終結 1 confirm path opt-in detection.

    The structural ban on AI-session pr-merge (see below) has one explicit
    escape hatch: the ``/beacon-trek-finalize`` 1-confirm collective
    merge path. That Skill exports ``BEACON_TREK_FINALIZE_CONSENT=1``
    before delegating to ``beacon pr merge`` so the ban knows the merge
    is happening with user collective approval, not as an AI-side
    individual-PR self-merge.

    The env var is **per-process** (not persisted), so a forgotten leak
    cannot turn a future AI session into an unrestricted merger.
    """
    return os.environ.get("BEACON_TREK_FINALIZE_CONSENT", "") == "1"

def _ai_session_merge_ban_active() -> bool:
    """ms-92 / e-2169 — refuse AI-session individual PR merge.

    See CORE doc ``pr-review-autonomy-boundary`` for the rationale: AI
    sessions are authorised to approve / reject / request-changes on
    PRs, but **merge** belongs to the Trek-unit user collective
    confirmation (= ``/beacon-trek-finalize``) so the AI can't
    self-loop "I wrote it, I approved it, I merged it" without the
    user ever exercising codebase ownership.

    The ban is on by default for AI sessions and bypassed only when
    one of three explicit signals is present:

      * ``BEACON_TREK_FINALIZE_CONSENT=1`` — Trek-finalize Skill is
        the merger (= the 1-confirm collective approval path).
      * ``BEACON_PR_MERGE_USER_OVERRIDE=1`` — user explicit opt-in
        escape hatch (= user prompt phrased the merge directly).
      * ``BEACON_SESSION_KIND=human`` — non-AI session (= straight
        terminal usage). Default ``BEACON_SESSION_KIND`` (unset) is
        treated as AI for safety; humans wanting straight-line merge
        can either set the env var globally or use the override.

    Returns True if the ban should fire (= refuse the merge).
    """
    if _trek_finalize_consent_active():
        return False
    if os.environ.get("BEACON_PR_MERGE_USER_OVERRIDE", "") == "1":
        return False
    kind = (os.environ.get("BEACON_SESSION_KIND", "") or "").strip().lower()
    if kind == "human":
        return False
    return True

def cmd_pr_merge():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    # ms-92 / e-2169 — AI-session merge ban. Refuses the call when the
    # 3 escape hatches (Trek-finalize consent / user override / human
    # session kind) are all absent. The error message names every escape
    # so a stuck user can pick the right one for their context.
    if _ai_session_merge_ban_active():
        print(
            "Error: individual PR merge from an AI session is refused "
            "(ms-92 / e-2169 structural ban).\n"
            "  See CORE doc `pr-review-autonomy-boundary` for the role "
            "split (= AI approves, Trek-unit user merges, user releases).\n"
            "  Bypass paths (= one of these makes the merge proceed):\n"
            "    1. /beacon-trek-finalize <trek-id> — 1-confirm "
            "collective merge with the rest of the Trek's PRs.\n"
            "    2. BEACON_PR_MERGE_USER_OVERRIDE=1 — explicit user "
            "opt-in for one-off merges.\n"
            "    3. BEACON_SESSION_KIND=human — declare the calling "
            "session is human-driven (= straight terminal use).",
            file=sys.stderr,
        )
        sys.exit(2)
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = load_project()
    try:
        ms, entry = core.pr_merge(data, entry_id, date=today)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    # ms-119 e-4060: refuse to merge while independent reviews are still owed for
    # this PR (same gate as approve). BEFORE save_project so a blocked merge
    # persists nothing. The Trek-finalize / user-override escape hatches above
    # are orthogonal (they gate the AI-merge-autonomy boundary, not the review).
    _review_gate_check(_pr_number_from_url(entry.get("meta", {}).get("url", "")),
                       action="merge")
    save_project(data)
    # ms-119 e-4003: PR merged — interface change resolved, clear its AX nudge.
    _clear_pr_open_review_triggers(_pr_number_from_url(entry.get("meta", {}).get("url", "")))
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "merged"}, ensure_ascii=False))
    else:
        print(f"Merged PR [{entry_id}]: {entry.get('description', '')}")

def _fetch_gh_pr_list_all() -> list:
    """ms-61 / e-2005 — query `gh pr list --state all` and return a list
    of dicts with at least `number`, `state`, `url`, `mergedAt`.

    Returns [] if `gh` is unavailable, the repo isn't recognised, or the
    output isn't parsable. Soft-fails so the caller can degrade gracefully.
    """
    try:
        rows = gh_port.pr_list_all()
    except Exception:
        return []
    return rows if isinstance(rows, list) else []

def cmd_pr_sync():
    """ms-61 / e-2005 — sync beacon PR entries with GitHub state.

    Walks every PR entry in the project, queries `gh pr list --state all`,
    and for each entry whose GitHub state has advanced past beacon's
    record:
      - GitHub MERGED + beacon not-yet-done → `beacon pr merge`
      - GitHub CLOSED + beacon not-yet-cancelled → `beacon pr close`

    Read-only when `BEACON_DRY_RUN=1` (= prints plan only).
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    dry_run = os.environ.get("BEACON_DRY_RUN", "") == "1"

    gh_prs = _fetch_gh_pr_list_all()
    if not gh_prs:
        msg = ("Warning: could not fetch GitHub PR list "
               "(gh not configured / outside repo / no PRs).")
        if json_mode:
            print(json.dumps({"actions": [], "summary": {
                "merged": 0, "closed": 0, "skipped": 0, "errors": []
            }, "warning": msg}, ensure_ascii=False))
        else:
            print(msg, file=sys.stderr)
        return

    data = load_project()
    actions = core.plan_pr_sync(data, gh_prs)

    # Sort actions so non-skip transitions surface first.
    actions_sorted = sorted(
        actions, key=lambda a: (0 if a.get("action") != "skip" else 1, a.get("pr_number", 0))
    )

    if dry_run:
        if json_mode:
            print(json.dumps({"dry_run": True, "actions": actions_sorted},
                             ensure_ascii=False))
        else:
            actionable = [a for a in actions_sorted if a.get("action") != "skip"]
            if not actionable:
                print("All beacon PR entries are already aligned with GitHub.")
            else:
                print(f"PR sync plan (dry-run, {len(actionable)} actionable):")
                for a in actionable:
                    print(f"  [{a['entry_id']}] PR#{a['pr_number']}: "
                          f"{a['from_status']} → {a['to_status']} "
                          f"({a['action']}; {a['reason']})")
        return

    summary = core.apply_pr_sync(data, actions_sorted)
    save_project(data)

    if json_mode:
        print(json.dumps({"actions": actions_sorted, "summary": summary},
                         ensure_ascii=False))
    else:
        if summary["merged"] == 0 and summary["closed"] == 0:
            print("All beacon PR entries are already aligned with GitHub.")
        else:
            print(f"PR sync: {summary['merged']} merged, "
                  f"{summary['closed']} closed, "
                  f"{summary['skipped']} skipped.")
            for a in actions_sorted:
                if a.get("action") in ("merge", "close"):
                    print(f"  [{a['entry_id']}] PR#{a['pr_number']}: "
                          f"{a['from_status']} → {a['to_status']}")
        if summary["errors"]:
            print(f"  ⚠ {len(summary['errors'])} errors:", file=sys.stderr)
            for err in summary["errors"]:
                print(f"    [{err['entry_id']}] {err['error']}", file=sys.stderr)

def _infer_pr_ms_id(data: dict) -> tuple[str, str]:
    """Infer the most likely target milestone ID for a PR being created.

    Returns (ms_id, reason). ms_id is "" when nothing matched confidently.
    Priority (highest first):
      1. Current branch name matches `ms-<n>` → use it if that MS exists.
      2. Last 5 commit messages contain `ms-<n>` → use it.
      3. Last 5 commit messages contain `(e-<n>)` → look up which MS owns that task.
      4. Exactly one milestone has status == "in_progress" → use it.
      5. Otherwise: return "" and let the caller prompt.

    This is best-effort and read-only. The caller MUST surface the `reason`
    to the user (no silent guessing).
    """
    if not isinstance(data, dict):
        return "", ""

    ms_ids = {m.get("id") for m in data.get("milestones", []) if isinstance(m, dict)}

    # 1) Branch name
    try:
        branch = git_read_port.branch_show_current()
    except Exception:
        branch = ""
    if branch:
        m = re.search(r"ms-(\d+)", branch)
        if m:
            cand = f"ms-{m.group(1)}"
            if cand in ms_ids:
                return cand, f"branch name `{branch}` → {cand}"

    # 2) Recent commit messages for ms-N
    try:
        subjects = git_read_port.log_subjects(5)
    except Exception:
        subjects = []
    for line in subjects:
        m = re.search(r"ms-(\d+)", line)
        if m:
            cand = f"ms-{m.group(1)}"
            if cand in ms_ids:
                return cand, f"recent commit `{line[:60]}` → {cand}"

    # 3) Recent commits for e-N → reverse-lookup owning MS
    for line in subjects:
        m = re.search(r"\be-(\d+)\b", line)
        if m:
            eid = f"e-{m.group(1)}"
            for ms in data.get("milestones", []):
                if not isinstance(ms, dict):
                    continue
                for ent in ms.get("entries", []) or []:
                    if isinstance(ent, dict) and ent.get("id") == eid:
                        return ms.get("id", ""), f"task {eid} in commit `{line[:60]}` → {ms.get('id')}"

    # 4) Single in-progress milestone
    active = [m for m in data.get("milestones", [])
              if isinstance(m, dict) and m.get("status") == "in_progress"]
    if len(active) == 1:
        return active[0].get("id", ""), f"sole active milestone ({active[0].get('id')})"

    return "", ""

def cmd_pr_create():
    """Wrapper for gh pr create that auto-records the PR in beacon."""
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    gh_args_json = os.environ.get("BEACON_GH_ARGS_JSON", "")
    gh_args = os.environ.get("BEACON_GH_ARGS", "")

    # e-607: auto-infer ms_id when -m was omitted.
    if not ms_id:
        try:
            _data_for_infer = load_project()
        except Exception:
            _data_for_infer = {}
        inferred, reason = _infer_pr_ms_id(_data_for_infer)
        if inferred:
            ms_id = inferred
            print(f"Beacon: inferred -m {ms_id} ({reason})", file=sys.stderr)
        else:
            # We DO NOT abort — gh pr create can still run, but the PR record
            # will not be linked to any MS. Warn the caller so the user knows.
            print(
                "Beacon: -m was not given and no MS could be inferred from "
                "branch/commits/active state. PR record will be unattached.",
                file=sys.stderr,
            )

    # Run gh pr create and capture the URL from stdout.
    #
    # argv is forwarded as a JSON array (BEACON_GH_ARGS_JSON). This keeps
    # non-ASCII (e.g. Japanese PR titles) intact across the env-var hop:
    # json.loads yields the exact str list the caller built, and
    # subprocess re-encodes it to the child argv without any lossy
    # shell-quote round-trip. The legacy BEACON_GH_ARGS + shlex path is
    # kept only for backward compatibility with older dispatchers and
    # cannot reliably carry non-ASCII (bash `printf %q` emits $'...'
    # quoting that Python shlex does not understand).
    cmd = ["gh", "pr", "create"]
    if gh_args_json:
        try:
            parsed = json.loads(gh_args_json)
        except json.JSONDecodeError as exc:
            print(f"Error: BEACON_GH_ARGS_JSON is invalid JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(parsed, list) or not all(isinstance(a, str) for a in parsed):
            print("Error: BEACON_GH_ARGS_JSON must be a JSON array of strings", file=sys.stderr)
            sys.exit(2)
        cmd += parsed
    elif gh_args:
        import shlex
        cmd += shlex.split(gh_args)

    # Handler owns argv construction (input validation above); the port owns
    # the outward execution (ms-142 e-5527, spine §5).
    result = gh_port.run(cmd)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Extract PR URL from gh output (last line that looks like a URL)
    pr_url = ""
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("https://github.com/") and "/pull/" in line:
            pr_url = line
            break

    if not pr_url:
        print("Warning: could not detect PR URL from gh output", file=sys.stderr)
        return

    if not intent:
        try:
            intent = input(f"Intent for beacon PR record (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = ""

    gh_info = _fetch_gh_pr_info(pr_url)
    title = gh_info.get("title", "")
    commits = gh_info.get("commits", [])

    date = __import__("datetime").date.today().isoformat()
    data = load_project()
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=pr_url, intent=intent, date=date,
                          title=title, commits=commits,
                          session_id=_resolve_session_id())  # ms-57 / e-1062
    except ValueError as e:
        print(f"Warning: beacon pr record failed: {e}", file=sys.stderr)
        return
    save_project(data)
    print(f"Beacon: PR recorded [{eid}]: {title or pr_url}")
    if commits:
        print(f"  Commits: {len(commits)} linked")
