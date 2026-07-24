"""Single source of truth for the review 節目 (review-triggering moments) the
post-commit wake hook detects (ms-119 / e-4080).

The wake-hook grep patterns (``bin/beacon-post-commit-hook.sh``) and the
commands.py firing sites were two independent spellings of the same command set.
Renaming a command (e.g. ``milestone observe``) meant editing both; miss one and
the wake goes silent — exactly the drift this review machinery exists to prevent,
reappearing *inside* the machinery. This module is the ONE place the review 節目
command set is declared. The hook's grep patterns are drift-guarded against it
(``tests/test_review_node_manifest_drift.py``), and the hook's debug label is
derived from the node ``id`` so the hook log says WHICH 節目 matched, not just the
shared skill (both PR-open and target-close wake ``/beacon-review-run``, so the
old ``matched:/beacon-review-run`` label could not distinguish them).

Each node declares:
  ``id``            stable node id — also the hook debug-label suffix.
  ``grep_pattern``  the exact POSIX ERE the hook greps ``CMD_BARE`` with. Kept
                    byte-identical to the hook line so the drift guard is a plain
                    substring check (no regex-equivalence guessing).
  ``skill``         the MUST-run wake skill the hook injects.
  ``fires``         the review types this 節目 fires — documentation + a hook
                    for cross-checking against the commands.py firing spine.
  ``commands``      the concrete CLI command spellings this node covers (for the
                    target-close node these are the lifecycle commands whose
                    ``commands.py`` handlers call ``_fire_review_due_trigger``).
"""

# NOTE: if you edit a grep_pattern here you MUST edit the matching
# `grep -qE '<pattern>'` line in bin/beacon-post-commit-hook.sh (and vice versa).
# tests/test_review_node_manifest_drift.py fails the build if they diverge.
REVIEW_NODES = [
    {
        "id": "pr-open",
        "grep_pattern": r"beacon pr (add|create)|gh pr create",
        "skill": "/beacon-review-run",
        "fires": ["ax", "maintainability"],
        "commands": ["beacon pr add", "beacon pr create", "gh pr create"],
    },
    {
        "id": "target-close",
        "grep_pattern": r"beacon (milestone (done|close|observe)|operation close)( |$)",
        "skill": "/beacon-review-run",
        "fires": ["philosophy", "attainment"],
        "commands": [
            "beacon milestone done",
            "beacon milestone close",
            "beacon milestone observe",
            "beacon operation close",
        ],
    },
]


def node_by_id(node_id):
    """Return the review node with ``id == node_id``, or None."""
    for n in REVIEW_NODES:
        if n["id"] == node_id:
            return n
    return None


def grep_patterns():
    """[(id, grep_pattern), ...] for the drift guard."""
    return [(n["id"], n["grep_pattern"]) for n in REVIEW_NODES]
