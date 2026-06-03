r"""Branch name helpers for the ms-51 branch-driven workflow.

The single export is :func:`ms_branch_name`, which takes an MS id and an
MS title and returns the canonical branch name used by ``beacon ms start``
and ``beacon ms join --checkout``.

Format::

    ms-<id_number>-<slug>

Where ``<slug>`` is derived from the MS title:

* Lowercased.
* Non-word characters collapsed to ``-`` (covers spaces, punctuation,
  Japanese characters → underscore via ``\W``).
* Trimmed and de-duplicated dashes.
* Truncated to a soft cap so the full branch name stays comfortably
  under git's path-component limit and remains scannable in CLI lists.
* Falls back to ``"work"`` if the title is empty or generates no
  ASCII tokens at all (common for Japanese-only titles — we want a
  human-grep-able branch name, not raw transliteration, since the
  ms-id itself is the source of truth).

Why a dedicated module: this helper is shared between the CLI handler
(commands.py: ``cmd_milestone_start`` / ``cmd_milestone_join``) and any
future surface (Web UI suggestion, dispatcher pre-flight check). Putting
it next to ``lib/agent.py`` keeps the ms-51 primitives together.
"""

from __future__ import annotations

import re
from typing import Optional


# Branch slug soft cap. The branch name will be
#   ms-<n>-<slug>
# so 40 characters of slug + a few digits of MS id keeps the whole name
# under ~50 chars, well below git's per-component ceiling and within
# what most terminals show without truncation.
_SLUG_MAX_LEN = 40


def slugify_title(title: str) -> str:
    """Return an ASCII-safe slug for a milestone title.

    Examples::

        > slugify_title("MS への挙手 → 自動 branch + join")
        'branch-join'   # ASCII tokens only; the Japanese is dropped

        > slugify_title("Add OAuth flow")
        'add-oauth-flow'

        > slugify_title("")
        'work'

    Non-ASCII characters are intentionally **stripped** rather than
    transliterated: the branch name's job is to be a quick visual
    handle, and the MS id (``ms-51``) already carries the canonical
    identity. Transliterating Japanese into romaji would just add a
    point of disagreement between humans, git, and ourselves.
    """
    if not title:
        return "work"
    lowered = title.lower()
    # Replace any run of non-word characters with a single dash. \w covers
    # ASCII alnum + underscore (in default Python re), but Japanese chars
    # also satisfy \w in unicode mode. Pass re.ASCII so non-ASCII letters
    # are treated as separators — that gives us the "strip Japanese"
    # behaviour we want above.
    slug = re.sub(r"[^\w]+", "-", lowered, flags=re.ASCII)
    # Collapse double-underscores from any pre-existing underscores in the
    # title plus our dashes — keep dashes as the canonical separator.
    slug = slug.replace("_", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        return "work"
    if len(slug) > _SLUG_MAX_LEN:
        # Truncate on a word boundary if possible so we don't cut a token
        # in half (improves grep-ability of branch names).
        slug = slug[:_SLUG_MAX_LEN].rstrip("-")
        if "-" in slug:
            slug = slug.rsplit("-", 1)[0] if len(slug.rsplit("-", 1)[0]) >= 10 else slug
    return slug or "work"


def ms_branch_name(ms_id: str, title: Optional[str] = "") -> str:
    """Return the canonical branch name for an MS.

    ``ms_id`` is expected to look like ``ms-51``; we extract the digits to
    avoid a double ``ms-`` prefix if a caller mistakenly passes something
    like ``ms-ms-51`` (defensive).

    The slug component is purely cosmetic — two MSs with the same title
    would naturally produce two distinct branch names because of the id
    prefix. The slug is what humans grep for.
    """
    # Normalise ms-id: pull just the number, then re-emit as ms-N. This
    # handles the malformed-input case gracefully without raising; ms_id
    # validation is the caller's job (and core.py already enforces it).
    digits = re.findall(r"\d+", ms_id or "")
    n = digits[0] if digits else "0"
    slug = slugify_title(title or "")
    return f"ms-{n}-{slug}"


__all__ = ["ms_branch_name", "slugify_title"]
