"""Orphan / throwaway project detection (ms-123 / e-4030).

Tests and one-off migration checks used to hit the production cloud's
``create_project`` API and never clean up after themselves (no teardown).
The residue: 45× ``phase4-test`` plus ``test_beacon`` / ``beacon-test`` /
``Idle Wake Test`` — 48 throwaway projects sitting in the production
directory, indistinguishable at a glance from the real ones.

Before we archive anything (= the destructive step, e-4028), a human needs
a **read-only** way to see which projects are cleanup candidates and *why*
each was flagged — so a real project is never swept up by accident. This
module owns that classification as a pure function: given the project list
the API already returns, it labels each row with the signals that make it
look throwaway. The CLI wrapper (``cmd_project_orphans``) does the I/O; the
tests pin the signal logic here.

Signals (SPEC ms-123 方針 1 — deliberately multiple weak signals, not one
strong one, so the human can judge borderline rows):

  * ``ownerless``       — no ``owner`` field. A project nobody owns is an
                          orphan. NOTE: a non-admin user cannot even *see*
                          ownerless projects (the API denies them by
                          default, e-2794), so in practice this signal only
                          fires for an admin scan; it is kept for that case.
  * ``test_named``      — the name or id looks like a test fixture
                          (``phase4-test``, ``*-test``, ``test_*``,
                          ``beacon-test``, ``idle-wake-test`` …).
  * ``empty_created_at``— a ``created_at`` field is present but blank,
                          i.e. the project skipped the normal creation
                          path. Only evaluated when the field exists; the
                          standard project listing does NOT carry
                          ``created_at``, so this is a bonus signal for
                          callers that pass richer rows.

A row is a *candidate* when it carries at least one signal, is not already
archived (nothing left to do), and is not the project the command is being
run from (never propose archiving your own working project).

ms-125 e-4095 — two honesty gaps this module now closes:

  * **Signal degradation is made explicit.** In production the multi-signal
    design silently collapses to name-pattern only: a non-admin scan never
    even *receives* ownerless projects (the API hides them, e-2794), and the
    standard project listing carries no ``created_at`` field. So ``ownerless``
    / ``empty_created_at`` cannot fire, and every real candidate reads at
    "medium" confidence. Pretending the redundancy still holds is the danger.
    ``assess_signal_coverage`` names, per signal, whether it is *effective* or
    *degraded* for the rows actually in hand, so the human sees the reduced
    discrimination instead of trusting a phantom "high confidence".

  * **The human gate is the compensating control.** Because confidence is
    effectively pinned to "medium", the safety valve is not the classifier but
    the confirm gate — and that gate must archive **exactly the set the human
    reviewed**, not a freshly recomputed one. ``bind_confirmed_plan`` binds a
    confirmed run to the ids shown in the dry-run: candidates that appeared
    since the dry-run are refused (never archived unreviewed), and confirmed
    ids that are no longer candidates are skipped and reported.
"""

from __future__ import annotations

import re

# Patterns that mark a project name / id as a throwaway test fixture.
# Anchored / bounded so a genuine project ("beacon-b95643", "sales-crm")
# never matches — "test" must appear as its own token, not as a substring
# of an unrelated word.
_TEST_NAME_PATTERNS = (
    re.compile(r"phase\d*-?test", re.IGNORECASE),          # phase4-test, phase-test
    re.compile(r"(^|[-_ ])test([-_ ]|$)", re.IGNORECASE),  # test / *-test / test_* as a token
    re.compile(r"beacon[-_ ]test", re.IGNORECASE),         # beacon-test / beacon_test
    re.compile(r"idle[-_ ]?wake[-_ ]?test", re.IGNORECASE),  # idle-wake-test / Idle Wake Test
)


def _matches_test_name(text: str) -> bool:
    """True if ``text`` looks like a test-fixture name/id."""
    if not text:
        return False
    return any(p.search(text) for p in _TEST_NAME_PATTERNS)


def project_signals(project: dict) -> list[str]:
    """Return the throwaway signals a single project row carries.

    ``project`` is a row from the projects listing (keys seen in the wild:
    ``project_id`` / ``name`` / ``objective`` / ``owner`` / ``owner_email``
    / ``archived``; richer callers may add ``created_at``). Unknown keys are
    ignored; missing keys are treated as absent, never an error.
    """
    signals: list[str] = []

    name = project.get("name", "") or ""
    pid = project.get("project_id", "") or project.get("id", "") or ""
    if _matches_test_name(name) or _matches_test_name(pid):
        signals.append("test_named")

    if not project.get("owner"):
        signals.append("ownerless")

    # Only a signal when the field is actually present but blank — absence of
    # the key (the common listing shape) is NOT evidence of anything.
    if "created_at" in project and not project.get("created_at"):
        signals.append("empty_created_at")

    return signals


def detect_orphan_candidates(
    projects: list[dict],
    *,
    current_project_id: str | None = None,
) -> list[dict]:
    """Classify a project listing into read-only cleanup candidates.

    Returns one dict per *candidate* (a project carrying >=1 signal, not
    already archived, not the current project), shaped as::

        {
          "project_id": str,
          "name": str,
          "owner": str,          # "" when ownerless
          "signals": [str, ...], # non-empty
          "confidence": "high" | "medium",
        }

    ``confidence`` is "high" when two or more signals agree, else "medium" —
    a hint for the human eye, never an authorization to skip confirmation.
    The list is sorted by (confidence desc, name) for stable, scannable
    output. This function has no side effects and performs no I/O.
    """
    candidates: list[dict] = []
    for p in projects:
        pid = p.get("project_id", "") or p.get("id", "")
        if current_project_id and pid == current_project_id:
            continue
        if p.get("archived"):
            continue
        signals = project_signals(p)
        if not signals:
            continue
        candidates.append({
            "project_id": pid,
            "name": p.get("name", "") or "",
            "owner": p.get("owner", "") or "",
            "signals": signals,
            "confidence": "high" if len(signals) >= 2 else "medium",
        })

    _rank = {"high": 0, "medium": 1}
    candidates.sort(key=lambda c: (_rank.get(c["confidence"], 9), c["name"]))
    return candidates


def build_archive_plan(
    candidates: list[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """The ordered list of candidates to archive (ms-123 / e-4028).

    Pure: takes the read-only candidates from ``detect_orphan_candidates``
    and returns the subset that a confirmed run would archive, in order.
    ``limit`` (when a positive int) caps the batch — a safety valve so a
    first confirmed run can archive, say, 5 projects and be inspected before
    the full sweep. ``None`` / non-positive means "no cap" (whole batch).

    This function decides *what* would be archived; it never archives
    anything itself (no I/O). The confirm gate (whether to execute the plan
    at all) lives in the CLI command, mirroring the
    ``BEACON_PR_MERGE_USER_OVERRIDE`` human-confirmation pattern.
    """
    plan = list(candidates)
    if isinstance(limit, int) and limit > 0:
        plan = plan[:limit]
    return plan


def format_orphan_report(candidates: list[dict], total_scanned: int) -> str:
    """Human-readable, read-only report of cleanup candidates.

    Empty candidate list → a single reassuring line (no candidates found),
    mirroring the "empty means omit the noise" contract other Beacon
    helpers use. Always states it changed nothing.
    """
    if not candidates:
        return (
            f"掃除候補は見つかりませんでした（{total_scanned} プロジェクトを走査、"
            "変更なし）。"
        )

    lines = [
        f"掃除候補: {len(candidates)} 件 / {total_scanned} プロジェクト走査"
        "（read-only、まだ何も archive していません）",
        "",
    ]
    for c in candidates:
        owner = c["owner"][:14] if c["owner"] else "NONE"
        lines.append(
            f"  [{c['confidence']:>6}] {c['project_id']}"
            f"  «{c['name']}»  owner={owner}"
        )
        lines.append(f"           signals: {', '.join(c['signals'])}")
    lines.append("")
    lines.append(
        "→ 消す実行は別ステップ（e-4028: dry-run 目視 → 確認フラグ付きで一括 archive）。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ms-125 e-4095 — signal-coverage honesty
# ---------------------------------------------------------------------------

def assess_signal_coverage(projects: list[dict]) -> dict:
    """Report, per signal, whether it can actually discriminate on THESE rows.

    The multi-signal design (``test_named`` / ``ownerless`` /
    ``empty_created_at``) assumes all three can fire. In production two of them
    silently can't (a non-admin scan never receives ownerless rows; the standard
    listing carries no ``created_at``), so the classifier collapses to the name
    pattern alone. This function makes that collapse visible instead of letting
    a caller assume the redundancy still holds.

    Returns ``{signal: {"status": "effective" | "degraded", "reason": str}}``.
    "effective" = at least one row lets the signal be evaluated *and* it is not
    structurally suppressed. "degraded" = the signal cannot meaningfully fire on
    this listing (so its absence is NOT evidence of "clean"). Pure, no I/O.
    """
    rows = projects or []
    any_ownerless = any(not p.get("owner") for p in rows)
    any_created_at_field = any("created_at" in p for p in rows)

    coverage: dict = {
        # The name pattern reads off name/id, always present — always effective.
        "test_named": {
            "status": "effective",
            "reason": "name / id は常に listing に載るため判定可能",
        },
    }

    # ownerless: if not a single ownerless row is visible, we cannot tell "no
    # ownerless orphans" from "the API hid them from a non-admin caller".
    if any_ownerless:
        coverage["ownerless"] = {
            "status": "effective",
            "reason": "owner 欠落の project が listing に含まれる",
        }
    else:
        coverage["ownerless"] = {
            "status": "degraded",
            "reason": ("owner 欠落の project が 1 件も見えない。非 admin の scan は "
                       "ownerless project を API が隠す (e-2794) ため、"
                       "『ownerless の orphan は無い』とは断定できない"),
        }

    # empty_created_at: only evaluable when the field is carried at all.
    if any_created_at_field:
        coverage["empty_created_at"] = {
            "status": "effective",
            "reason": "created_at フィールドを持つ row があり空判定が可能",
        }
    else:
        coverage["empty_created_at"] = {
            "status": "degraded",
            "reason": ("標準 listing は created_at を運ばないため判定不能 "
                       "(richer な行を渡した caller のみ有効)"),
        }

    return coverage


def format_coverage_note(coverage: dict) -> str:
    """One short block naming which signals are degraded on this scan.

    Empty / all-effective → a single reassuring line. When any signal is
    degraded, spell out which and why, and state plainly that discrimination is
    reduced so the human leans on the confirm gate rather than the confidence
    label. Returns "" when ``coverage`` is falsy (nothing to say)."""
    if not coverage:
        return ""
    degraded = [(k, v) for k, v in coverage.items()
                if (v or {}).get("status") == "degraded"]
    if not degraded:
        return "シグナル網羅: 全シグナルが有効に効いています。"

    lines = [
        f"⚠ シグナル網羅: {len(degraded)} 件のシグナルが本番で縮退しています "
        "(= 判定の冗長性が下がっているため、confidence ラベルより人間の確認を信頼してください)：",
    ]
    for name, info in degraded:
        lines.append(f"  - {name}: {info.get('reason', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ms-125 e-4095 — bind a confirmed run to the reviewed set
# ---------------------------------------------------------------------------

def bind_confirmed_plan(
    candidates: list[dict],
    confirmed_ids,
    *,
    limit: int | None = None,
) -> dict:
    """Restrict a confirmed archive to EXACTLY the ids the human reviewed.

    The confirm step previously re-fetched the project list and re-ran
    detection, so a project that became a candidate *after* the dry-run (a new
    test-named leak, an owner field going blank) would be archived without ever
    having been shown to the human. This binds the plan to ``confirmed_ids`` —
    the set the dry-run printed:

      * ``plan``            — candidates whose id is in ``confirmed_ids``, in the
                              detector's ranked order, capped by ``limit``.
                              These are the ONLY projects a confirmed run
                              archives.
      * ``skipped_missing`` — ids the human confirmed that are no longer
                              candidates now (already archived, owner appeared,
                              renamed). Reported, never archived.
      * ``unreviewed_new``  — projects that are candidates now but were NOT in
                              the confirmed set. Refused: they must go through a
                              fresh dry-run before they can be archived.
      * ``dropped_by_limit``— confirmed candidates that ``limit`` cut from the
                              plan. Reported (never silently narrower than the
                              reviewed set) so the caller can surface "N of your
                              ids were NOT archived this run" instead of letting
                              "no error" read as "all archived" (ms-125 review).

    Pure: no I/O, no mutation of inputs. ``limit`` caps only ``plan`` (same
    contract as ``build_archive_plan``: None / non-positive = no cap).
    """
    confirmed = {c for c in (confirmed_ids or []) if c}
    by_id = {c.get("project_id", ""): c for c in candidates}
    candidate_ids = set(by_id)

    matched = [c for c in candidates if c.get("project_id", "") in confirmed]
    if isinstance(limit, int) and limit > 0:
        plan = matched[:limit]
        dropped_by_limit = [c.get("project_id", "") for c in matched[limit:]]
    else:
        plan = matched
        dropped_by_limit = []

    skipped_missing = sorted(confirmed - candidate_ids)
    unreviewed_new = [c for c in candidates
                      if c.get("project_id", "") not in confirmed]

    return {
        "plan": plan,
        "skipped_missing": skipped_missing,
        "unreviewed_new": unreviewed_new,
        "dropped_by_limit": dropped_by_limit,
    }
