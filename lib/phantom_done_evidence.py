"""Phantom-done evidence gate (ms-95 / e-2726).

When ``beacon task done`` is called, this module compares the task's
``description`` + ``acceptance_criteria`` + ``motivation`` keywords against
the most recent N commit entries' messages. If no commit references the task
(by id) AND keyword overlap is below the threshold, the task is flagged as
a candidate *phantom done* — the task was marked done with no physical
evidence in the commit log.

Design philosophy (from the e-2726 task spec):

* **Flag, not filter.** The gate does NOT reject the done. It emits a
  structured warning (Cloud Logging json line + audit log marker) so
  reviewers can audit phantom dones after the fact. "動かしながら考える"
  — we surface, the human decides.
* **Cheap and pure.** No I/O here, no Firestore reads, no git calls. The
  caller (= ``server/app.py`` ``done_entry`` endpoint) feeds the in-memory
  project data dict. This keeps the module trivially unit-testable and
  reusable from CLI / scripts in the future.
* **Two evidence signals.** (1) The commit message / description mentions
  the task id directly (= ``e-2726``). This is the strongest signal —
  ``/beacon-log`` Skill is supposed to attach the entry id. (2) Token
  overlap between the task's keywords and the commit text passes a
  threshold. The 2nd signal catches the case where the operator wrote a
  good commit message but forgot the id reference.

The companion task e-2650 enforces "Trek slot done requires project task
done" at the Trek view layer. e-2726 (this module) enforces "project task
done should have physical evidence" at the project pool layer. The two
defenses stack: Trek slot done → requires project task done → which now
flags when there is no commit evidence.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Stop tokens — extremely common short words / verbs / japanese particles
# that produce false-positive overlaps. The original ``core._tokenize``
# already drops 1-char tokens and the most aggressive particle splits, but
# at the keyword-extraction layer we want a slightly tighter filter so a
# task description like "do the thing" doesn't match every commit that
# contains "the". Kept conservative — better to over-warn than miss
# phantom dones (= the user can ignore false warnings, but a missed
# phantom done is invisible).
_STOP_TOKENS_EN = frozenset(
    {
        # english articles / conjunctions / pronouns
        "the", "and", "for", "but", "not", "you", "are", "with", "this",
        "that", "from", "have", "has", "had", "was", "were", "will", "can",
        "may", "all", "any", "any", "such", "via", "use", "uses", "used",
        "using", "into", "onto", "out", "out_of", "off", "per",
        # very generic technical words that appear in nearly every commit
        # message — keep these out so they don't dominate the score
        "fix", "add", "update", "remove", "change", "refactor", "test",
        "tests", "docs", "doc", "chore", "feat", "wip", "ci",
        # english common verbs
        "make", "made", "get", "got", "set", "run", "ran", "see", "saw",
    }
)

# Japanese stop tokens — common 2-char nouns / suffixes that show up
# everywhere. Keep tight.
_STOP_TOKENS_JA = frozenset(
    {
        "こと", "もの", "ため", "よう", "とき", "そう", "それ", "これ", "あれ",
        "場合", "対応", "実装", "修正", "追加", "削除", "更新", "確認",
    }
)


def _tokenize(text: str) -> set[str]:
    """Split ``text`` into comparable tokens (English + Japanese).

    Mirrors the heuristic in ``lib.core._tokenize`` but tightens the floor
    on English tokens to 3 characters (= drops "is" / "to" / "in" noise)
    and applies stop-word filtering. The Japanese splitter is intentionally
    coarse — we don't ship a morphological analyzer just for this gate.
    """
    if not text:
        return set()
    en_words = re.findall(r"[a-zA-Z][a-zA-Z_-]{2,}", text.lower())
    en_tokens = {w for w in en_words if w not in _STOP_TOKENS_EN}
    ja_text = re.sub(r"[a-zA-Z0-9_\s]+", " ", text)
    ja_chunks = re.split(r"[のをにはがでとからまでもへや、。・「」（）()【】\s]+", ja_text)
    ja_tokens = {c for c in ja_chunks if len(c) >= 2 and c not in _STOP_TOKENS_JA}
    return en_tokens | ja_tokens


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

# Fields on a task entry that contribute keywords to evidence matching.
# ``description`` is required; the rest are optional. ``acceptance_criteria``
# is the most useful field (= "Done when" text) but many older tasks only
# have ``description``.
_KEYWORD_FIELDS = ("description", "acceptance_criteria", "motivation", "behavior", "detail")


def extract_task_keywords(entry: dict) -> set[str]:
    """Extract a deduplicated keyword set from a task entry.

    Reads the conventional Beacon "necessary-sufficient" fields
    (``description`` + ``acceptance_criteria`` + ``motivation`` +
    ``behavior`` + ``detail``) and returns a single token set. Empty /
    missing fields are silently skipped. Returns an empty set when the
    entry has no usable text — callers should treat this as "no evidence
    possible to evaluate" (= skip warning).
    """
    if not isinstance(entry, dict):
        return set()
    parts: list[str] = []
    for field in _KEYWORD_FIELDS:
        v = entry.get(field)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    if not parts:
        return set()
    return _tokenize(" ".join(parts))


# ---------------------------------------------------------------------------
# Commit collection
# ---------------------------------------------------------------------------

# Default number of most recent commits to scan. Picked at 10 because the
# typical Beacon dogfood loop bundles 1-3 commits per task — 10 gives
# ample window for the matching commit to live without overwhelming the
# token-overlap computation.
DEFAULT_COMMIT_WINDOW = 10


def _iter_commit_entries(entries: list) -> list[dict]:
    """Recursively walk an entries tree, yielding ``type == "commit"`` rows."""
    out: list[dict] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "commit":
            out.append(e)
        children = e.get("entries")
        if isinstance(children, list) and children:
            out.extend(_iter_commit_entries(children))
    return out


def collect_recent_commits(data: dict, *, limit: int = DEFAULT_COMMIT_WINDOW) -> list[dict]:
    """Collect the most-recent ``limit`` commit entries across all Targets.

    ms-164 e-5951: walks ``occupation.iter_target_records`` instead of hardcoding
    ``data['milestones']`` — commit entries also live under Operations (a dev
    Target class), so the old milestone-only scan MISSED phantom-done evidence for
    OperationTask commits. Non-commit-bearing Target classes (a sales Opportunity)
    simply contribute nothing, so the broadening is safe (a project with no commits
    yields the same empty result). The ``ms_id`` output key is kept for back-compat
    but now carries the parent Target's id (a milestone OR an operation).

    Ordering: descending by ``created_at`` (falling back to ``date`` then
    empty string). Each returned dict has the shape
    ``{"id": ..., "text": <commit_text>, "created_at": ..., "ms_id": ...}``
    where ``text`` is the concatenation of the commit's ``description`` and
    ``meta.message`` (the latter is the raw git commit subject — usually
    richer than the auto-derived ``description``).
    """
    if not isinstance(data, dict):
        return []
    import occupation  # ms-164 e-5951: occupation-generic Target enumeration
    rows: list[dict] = []
    for ms in occupation.iter_target_records(data):
        if not isinstance(ms, dict):
            continue
        ms_id = ms.get("id", "")
        for c in _iter_commit_entries(ms.get("entries", [])):
            meta = c.get("meta", {}) or {}
            text = " ".join(
                [
                    str(c.get("description", "") or ""),
                    str(meta.get("message", "") or ""),
                ]
            ).strip()
            rows.append(
                {
                    "id": c.get("id", ""),
                    "text": text,
                    "created_at": c.get("created_at", "") or c.get("date", "") or "",
                    "ms_id": ms_id,
                    "hash": meta.get("hash", "") or "",
                }
            )
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Evidence evaluation
# ---------------------------------------------------------------------------

# Token-overlap threshold below which a commit is judged "not matching this
# task". 30% is the e-2726 spec hint — generous enough that a single
# semantic keyword + one filler in common is enough; strict enough that
# unrelated commits don't get auto-credited.
DEFAULT_OVERLAP_THRESHOLD = 0.3


def _id_matches_in_text(entry_id: str, text: str) -> bool:
    """Return True iff ``text`` mentions ``entry_id`` as a whole word.

    Word-boundary anchored so that ``e-27`` does not match ``e-2726``.
    """
    if not entry_id or not text:
        return False
    pattern = r"(?<![a-zA-Z0-9_-])" + re.escape(entry_id) + r"(?![a-zA-Z0-9_-])"
    return re.search(pattern, text) is not None


def evaluate_done_evidence(
    entry: dict,
    recent_commits: list[dict],
    *,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict:
    """Compare a task's keywords against recent commits, return an assessment.

    Returns a dict:

    .. code-block:: python

        {
            "has_evidence": bool,         # True iff at least one commit matches
            "match_type": str,            # "id_reference" | "keyword_overlap" | "none" | "skip_no_keywords"
            "task_id": str,
            "task_keywords": list[str],   # sorted for stable logging
            "matched_commits": [          # ranked by score, descending
                {"id": ..., "score": ..., "overlap": ..., "by": "id_reference"|"keyword_overlap"},
                ...
            ],
            "threshold": float,
            "window": int,                # how many commits scanned
        }

    The caller (= ``done_entry`` endpoint) uses ``has_evidence`` to decide
    whether to emit the phantom-done warning. ``match_type == "skip_no_keywords"``
    means the task description was empty — we treat that as "cannot judge"
    and the caller should NOT warn (= no false positive on legacy
    description-less tasks).
    """
    task_id = (entry.get("id", "") if isinstance(entry, dict) else "") or ""
    keywords = extract_task_keywords(entry)
    assessment: dict[str, Any] = {
        "task_id": task_id,
        "task_keywords": sorted(keywords),
        "matched_commits": [],
        "threshold": threshold,
        "window": len(recent_commits or []),
        "has_evidence": False,
        "match_type": "none",
    }

    if not keywords:
        # The task has nothing to compare against. Skip — emitting a warning
        # here would just spam noise for the entire "no description"
        # legacy long tail. Surface the skip in match_type so the caller can
        # still log at INFO if it wants.
        assessment["match_type"] = "skip_no_keywords"
        assessment["has_evidence"] = True  # don't warn
        return assessment

    matches: list[dict] = []
    for c in recent_commits or []:
        text = c.get("text", "") or ""
        if not text:
            continue
        # Signal 1: direct id reference. Strongest evidence.
        if task_id and _id_matches_in_text(task_id, text):
            matches.append(
                {
                    "id": c.get("id", ""),
                    "hash": c.get("hash", ""),
                    "score": 1.0,
                    "overlap": -1,  # n/a — id reference is a hard match
                    "by": "id_reference",
                }
            )
            continue
        # Signal 2: keyword overlap.
        commit_tokens = _tokenize(text)
        if not commit_tokens:
            continue
        overlap = len(keywords & commit_tokens)
        if overlap == 0:
            continue
        score = overlap / min(len(keywords), len(commit_tokens))
        if score >= threshold:
            matches.append(
                {
                    "id": c.get("id", ""),
                    "hash": c.get("hash", ""),
                    "score": round(score, 3),
                    "overlap": overlap,
                    "by": "keyword_overlap",
                }
            )

    if matches:
        # Rank: id_reference first, then by score descending.
        matches.sort(
            key=lambda m: (0 if m["by"] == "id_reference" else 1, -m.get("score", 0.0))
        )
        assessment["matched_commits"] = matches
        assessment["has_evidence"] = True
        assessment["match_type"] = matches[0]["by"]
    else:
        assessment["match_type"] = "none"
        assessment["has_evidence"] = False
    return assessment


__all__ = [
    "DEFAULT_COMMIT_WINDOW",
    "DEFAULT_OVERLAP_THRESHOLD",
    "extract_task_keywords",
    "collect_recent_commits",
    "evaluate_done_evidence",
]
