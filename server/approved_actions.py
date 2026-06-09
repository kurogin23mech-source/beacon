"""Approved-actions syntax for T2 envelopes (Operation scope authorization).

ms-60 / e-1339 — SPEC frontmatter parser + action syntax validator + matcher.

Syntax
------
::

  <action>  ::= <segment>(":" <segment>)*
  <segment> ::= one or more chars from [A-Za-z0-9_=.+\\- ]

The **last** segment may additionally take one of these wildcard forms:
  - ``"*"`` alone (any value in this subscope)
  - ``"<prefix>*"`` (any value starting with ``<prefix>``)

Examples
--------
OK::

    extract:profile:*          # last segment is *
    task done:e-*              # last segment ends with *
    deploy:v0.21.x             # no wildcard
    doc add:scope=memo         # qualifier syntax

NG::

    *:profile:daily            # verb (first segment) wildcard
    extract:*:user             # middle wildcard
    extract:profile**          # double *
    "anything goes"            # natural language (`"` is not in segment chars)

Tier policy
-----------
Wildcards are allowed **only for T2 envelopes** (Operation scope), where the
SPEC author has documented the subscope contract. T1/T3/T5 require strict
enumeration per CORE doc ``1UGomhHqCQo0iYSRtCdB`` (``§ scope 自然言語の曖昧性``).
ms-60 SPEC ``§ 設計方針 4`` records the T2 carve-out.
"""

from __future__ import annotations

import re
from typing import Optional


# Per-segment char class. Alphanumeric plus structural punctuation observed
# in real CLI verbs / qualifiers / version strings. No control chars, no
# colon (the segment separator), no quotes, no shell metachars.
_SEGMENT_CHARS = r"[A-Za-z0-9_=.+\- ]"
_NON_LAST_SEGMENT_RE = re.compile(rf"^{_SEGMENT_CHARS}+$")
_LAST_SEGMENT_NOWILD_RE = re.compile(rf"^{_SEGMENT_CHARS}+$")
_LAST_SEGMENT_WILD_RE = re.compile(rf"^(\*|{_SEGMENT_CHARS}+\*?)$")

# Frontmatter delimiter at start of doc. Tolerate optional whitespace before
# the trailing newline of each "---" line.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


class ApprovedActionsError(ValueError):
    """Raised on invalid ``approved_actions`` syntax or parser error."""


def validate_action(
    action: str, *, allow_last_segment_wildcard: bool = False
) -> None:
    """Validate one action string. Raises ``ApprovedActionsError`` on invalid.

    Set ``allow_last_segment_wildcard=True`` only for T2 envelopes.
    """
    if not isinstance(action, str):
        raise ApprovedActionsError(f"action must be a string: {action!r}")
    if not action or action != action.strip():
        raise ApprovedActionsError(
            f"action must be non-empty with no leading/trailing whitespace: "
            f"{action!r}"
        )
    if any(c in action for c in "\n\r\t"):
        raise ApprovedActionsError(
            f"action must not contain control chars: {action!r}"
        )

    segments = action.split(":")
    if not all(segments):
        raise ApprovedActionsError(
            f"action has empty segment (double colon or leading/trailing ':'): "
            f"{action!r}"
        )

    *non_last, last = segments
    for seg in non_last:
        if "*" in seg or "?" in seg:
            raise ApprovedActionsError(
                f"wildcards allowed only in last segment: {action!r}"
            )
        if not _NON_LAST_SEGMENT_RE.match(seg):
            raise ApprovedActionsError(
                f"segment contains forbidden chars: {seg!r} in {action!r}"
            )

    last_re = (
        _LAST_SEGMENT_WILD_RE
        if allow_last_segment_wildcard
        else _LAST_SEGMENT_NOWILD_RE
    )
    if not last_re.match(last):
        if allow_last_segment_wildcard:
            raise ApprovedActionsError(
                f"last segment must be '*', '<chars>', or '<chars>*': "
                f"{last!r} in {action!r}"
            )
        raise ApprovedActionsError(
            f"wildcards forbidden for this tier (T1/T3/T5 require strict "
            f"enumeration): {action!r}"
        )


def validate_actions(
    actions: list[str], *, allow_last_segment_wildcard: bool = False
) -> None:
    """Validate a list of action strings. Raises on first invalid entry."""
    if not isinstance(actions, list):
        raise ApprovedActionsError(
            f"actions must be a list: got {type(actions).__name__}"
        )
    for a in actions:
        validate_action(a, allow_last_segment_wildcard=allow_last_segment_wildcard)


def matches(authorized: list[str], requested: str) -> bool:
    """Return True iff ``requested`` is matched by any pattern in ``authorized``.

    Matching rules:
      - Segment counts must equal (no cross-depth wildcards).
      - Non-last segments compared exactly.
      - Last segment: ``*`` matches anything; ``<prefix>*`` matches any value
        starting with ``<prefix>``; otherwise exact match.

    ``requested`` itself must not contain wildcards — it's the action the AI
    actually wants to perform, not a pattern.
    """
    if not isinstance(requested, str) or not requested:
        return False
    if "*" in requested or "?" in requested:
        # Defensive: a requested action with wildcards would smuggle scope.
        return False
    req_segs = requested.split(":")
    if not all(req_segs):
        return False
    for pat in authorized:
        pat_segs = pat.split(":")
        if len(pat_segs) != len(req_segs):
            continue
        if any(p != r for p, r in zip(pat_segs[:-1], req_segs[:-1])):
            continue
        p_last, r_last = pat_segs[-1], req_segs[-1]
        if p_last == "*":
            return True
        if p_last.endswith("*"):
            if r_last.startswith(p_last[:-1]):
                return True
        elif p_last == r_last:
            return True
    return False


def parse_spec_frontmatter(content: str) -> Optional[list[str]]:
    """Extract ``approved_actions`` from YAML frontmatter, or ``None``.

    Returns the raw list of action strings (not yet validated — caller should
    call :func:`validate_actions`). Returns ``None`` if the doc has no
    frontmatter or no ``approved_actions`` field. Returns ``[]`` if the field
    is present but empty.

    Supports two YAML forms:
      - Block:::

            approved_actions:
              - extract:profile:*
              - task done:e-*

      - Inline (square-bracket list):::

            approved_actions: [extract:profile:*, task done:e-*]

    Avoids depending on PyYAML — Beacon's existing frontmatter handling is
    line-based for the same reason.
    """
    if not isinstance(content, str):
        return None
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.startswith("approved_actions:"):
            i += 1
            continue
        after = line[len("approved_actions:"):].strip()
        if after:
            # Inline form.
            if after.startswith("[") and after.endswith("]"):
                inner = after[1:-1].strip()
                if not inner:
                    return []
                return [
                    s.strip().strip('"').strip("'")
                    for s in inner.split(",")
                    if s.strip()
                ]
            # Anything else inline is malformed for our purposes.
            raise ApprovedActionsError(
                f"approved_actions inline value must be a YAML list "
                f"[a, b]: got {after!r}"
            )
        # Block form: collect "- item" entries.
        i += 1
        items: list[str] = []
        while i < len(lines):
            entry = lines[i]
            stripped = entry.strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("- "):
                items.append(stripped[2:].strip().strip('"').strip("'"))
                i += 1
                continue
            break
        return items
    return None
