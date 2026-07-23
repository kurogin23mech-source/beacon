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
