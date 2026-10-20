"""Date-window classification — the one place that decides whether a record
falls inside a requested period (ms-184 / e-7016).

Why this module exists
======================
Every period filter in Beacon used to be written inline as::

    if from_date and created_at and created_at < from_date:
        return False

That line folds **absence into agreement**: a record whose timestamp is an
empty string skips both comparisons and is reported as matching, no matter
which period was asked for. Measured on 2026-10-15 against the Beacon project
itself, ``beacon search --from 2020-01-01 --to 2020-01-02 --type milestone``
returned 25 Targets — the same 25 it returned for "yesterday", because all 25
carry an empty ``created_at``. The true answer (the Target that took 8 commits
yesterday) was absent from both.

So a period question has **three** answers, not two (ms-184 SPEC 方針 8):

``IN_WINDOW``
    The record carries a timestamp and it lies inside the period.
``OUT_OF_WINDOW``
    The record carries a timestamp and it lies outside the period.
``UNDATED``
    The record carries no timestamp, so the period question *cannot be
    answered for it*. This is deliberately NOT ``IN_WINDOW`` (the old bug) and
    deliberately NOT silently equal to ``OUT_OF_WINDOW`` either: callers are
    expected to exclude it from results **and report how many they excluded**,
    so "we could not judge these" never reads as "these did not happen".

Day boundaries
==============
Beacon stores timestamps at two different precisions in the same field —
commits and PRs write a bare ``YYYY-MM-DD`` while tasks and approvals write a
full ``YYYY-MM-DDTHH:MM:SSZ``. The comparison is lexicographic (ISO-8601 sorts
correctly as text), so a bare bound compares wrong against a full timestamp at
the END of a range: ``"2026-10-14T10:00:00Z" > "2026-10-14"`` is true, so
``--to 2026-10-14`` used to drop everything that happened *during* the 14th
except the records that stored date-only values. "Through the 14th" has one
sane meaning, so a date-only upper bound is widened to the end of that day
(and a date-only lower bound to its start). That is the same hole as the
absence bug — a representation detail silently changing the answer — so it is
fixed here, in the same single place.

This module is pure (no I/O, no clock reads except the explicit ``today``
argument of :func:`parse_since`) so both the CLI and the server can share it.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional, Tuple

IN_WINDOW = "in_window"
OUT_OF_WINDOW = "out_of_window"
UNDATED = "undated"

#: Ordering sentinel appended to a date-only upper bound. ``~`` sorts after
#: every character ISO-8601 timestamps use (digits, ``-``, ``:``, ``.``, ``T``,
#: ``Z``, ``+``), so "YYYY-MM-DD~" is lexicographically greater than any
#: timestamp within that day regardless of precision or timezone suffix.
_END_OF_DAY = "~"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_calendar_date(value: str) -> bool:
    """Whether ``value`` is ``YYYY-MM-DD`` **and** a day that exists.

    The shape regex above only counts digits, so ``2026-13-01`` / ``2026-02-30``
    / ``2026-00-00`` used to sail through every period entry point: they sort
    like dates, so a bound built from one silently answered a period nobody
    could have meant. ``2026-13-01`` was worse than silent — being
    lexicographically large it tripped :func:`validate_range` instead, so the
    caller was told its range was *inverted* and went off swapping the flags,
    never learning that month 13 does not exist (measured by an independent AX
    review of PR #811). A misdirecting error is a wrong answer with a louder
    voice.

    Kept as a predicate rather than folded into the regex because both the
    parser and the explicit-range validator need it, and the point of this
    module is that they share one rule.
    """
    value = (value or "").strip()
    if not _DATE_ONLY.match(value):
        return False
    try:
        datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return False
    return True

# ---------------------------------------------------------------------------
# Which stamps a period question reads — DECLARED, because there are two
# legitimate questions and they must not look like one by accident.
# ---------------------------------------------------------------------------
#
# A Beacon record can carry several timestamps, and the field list chosen is
# what decides WHICH question gets answered:
#
# ``CREATION_STAMPS``  — "when did this record come into being". This is what
#     ``search --from/--to`` means, and has always meant: find the records
#     RAISED in this period. A task created in May and finished yesterday is
#     correctly NOT a match for yesterday.
# ``MOVEMENT_STAMPS``  — "when did anything happen to this record". This is
#     what ``recap`` means (ms-184): the same task IS yesterday's work,
#     because it was finished then.
#
# Both are right for their own question, and the difference is deliberate.
# It is declared here, in one place, because the two field lists previously
# lived as separate inline ``or``-chains in two modules that look like they
# solve the same problem — a maintainability review (PR#803) reproduced the
# disagreement and pointed out that a reader has to diff two files to notice
# it. Naming the sets makes a future change state which question it is
# changing, rather than silently moving one of them.
CREATION_STAMPS = ("created_at", "date", "pushed_at")

MOVEMENT_STAMPS = ("done_at", "created_at", "date", "pushed_at",
                   "deployed_at", "updated_at", "approved_at")

# ``RETRO_ENTRY_STAMPS`` — "when was this entry raised", as the weekly retro
# narrative asks it (ms-181 e-7101). It is CREATION_STAMPS minus ``pushed_at``,
# and it is named here rather than left as an inline ``or``-chain in
# ``core.collect_retro_entries`` because an unnamed third list is invisible to
# the next change: a maintainability review of PR #811 pointed out that adding a
# stamp to CREATION_STAMPS would reach ``search`` and silently miss retro, with
# nothing in either file to say so. The difference is deliberate — retro groups
# a Target's own entry tree, where a push record is attributed to the push
# facet, not to the narrative — so it gets a name instead of a coincidence.
RETRO_ENTRY_STAMPS = ("created_at", "date")


def stamps_of(record, fields) -> list:
    """Every non-empty timestamp ``record`` carries among ``fields``.

    Order follows ``fields``, so a caller that lists its preferred stamp first
    gets it first. Non-string and blank values are dropped — a record with a
    malformed stamp is treated as not carrying that one, which keeps the
    three-valued classification honest instead of raising on bad data.
    """
    out = []
    for f in fields or ():
        v = record.get(f) if isinstance(record, dict) else None
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    return out



def normalize_bound(value: str, *, end: bool) -> str:
    """Return ``value`` widened to a whole-day bound when it is date-only.

    ``end=False`` (lower bound) leaves ``2026-10-14`` as-is — it already sorts
    at or before every timestamp on that day. ``end=True`` (upper bound)
    returns ``2026-10-14~`` so the whole day is included.

    A value that already carries a time component is returned verbatim: the
    caller asked for an instant, not a day.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if end and _DATE_ONLY.match(value):
        return value + _END_OF_DAY
    return value


def validate_range(from_date: str, to_date: str, *,
                   lower_flag: str = "--from",
                   upper_flag: str = "--to") -> str:
    """Return an error message for an unusable explicit range, else "".

    The ONE place that knows an inverted range is nonsense. ``parse_since``
    already enforced this for the ``A..B`` spelling; the ``--from`` / ``--to``
    spelling needs the same rule, and writing the comparison a second time at
    the CLI boundary is how two copies of a period rule start (a test pinning
    "no period comparisons outside this module" caught exactly that during
    ms-184). Returns a message rather than raising so a CLI caller can print
    it on stderr with its own exit convention.

    ``lower_flag`` / ``upper_flag`` name the caller's own spelling of the two
    ends: ``recap`` says ``--from``/``--to`` while ``retro`` says
    ``--since``/``--until`` (ms-181 e-7101). The rule is shared; the flag
    names in the message must still be the ones the caller actually accepts,
    or the advice sends the reader to a flag their command does not have.
    """
    lo, hi = (from_date or "").strip(), (to_date or "").strip()
    # Calendar validity BEFORE ordering: a bound like 2026-13-01 sorts after
    # every real date, so the ordering check fired first and reported an
    # inverted range — sending the reader to swap the flags instead of telling
    # them month 13 does not exist (ms-181 e-7101, AX review of PR #811).
    for value, flag in ((lo, lower_flag), (hi, upper_flag)):
        if value and _DATE_ONLY.match(value) and not is_calendar_date(value):
            return f"{flag} {value} is not a real calendar date."
    if lo and hi and lo > hi:
        return f"{lower_flag} {lo} is after {upper_flag} {hi}."
    return ""


def classify(value: str, from_date: str = "", to_date: str = "") -> str:
    """Return ``IN_WINDOW`` / ``OUT_OF_WINDOW`` / ``UNDATED`` for one timestamp.

    ``value`` is an ISO-8601 date or datetime string, or empty / ``None`` when
    the record carries none. With no bounds requested every record — including
    an undated one — is ``IN_WINDOW``: asking no period question cannot fail to
    be answered.
    """
    has_window = bool((from_date or "").strip() or (to_date or "").strip())
    value = (value or "").strip()
    if not has_window:
        return IN_WINDOW
    if not value:
        return UNDATED
    lower = normalize_bound(from_date, end=False)
    upper = normalize_bound(to_date, end=True)
    if lower and value < lower:
        return OUT_OF_WINDOW
    if upper and value > upper:
        return OUT_OF_WINDOW
    return IN_WINDOW


def classify_any(values, from_date: str = "", to_date: str = "") -> str:
    """Classify a record that carries SEVERAL candidate timestamps.

    A Beacon record can be stamped more than once — a task has ``created_at``
    (when it was raised) and ``done_at`` (when it was finished), a push has
    ``pushed_at``. Each stamp is a separate moment at which that record moved,
    so the record counts as inside the period if **any** stamp is inside it.

    Returns ``UNDATED`` only when *every* candidate is missing: a record with
    one real stamp is judgeable even if its other fields are blank.
    """
    verdicts = [classify(v, from_date, to_date) for v in (values or [])]
    if not verdicts:
        return classify("", from_date, to_date)
    if IN_WINDOW in verdicts:
        return IN_WINDOW
    if OUT_OF_WINDOW in verdicts:
        return OUT_OF_WINDOW
    return UNDATED


def latest_in_window(values, from_date: str = "", to_date: str = "") -> str:
    """Return the newest of ``values`` that lies inside the window ("" if none).

    Used to date the evidence a record represents: when a task was raised
    before the window and finished inside it, the moment that belongs to the
    window is the completion.
    """
    inside = [
        (v or "").strip()
        for v in (values or [])
        if (v or "").strip() and classify(v, from_date, to_date) == IN_WINDOW
    ]
    return max(inside) if inside else ""


# ---------------------------------------------------------------------------
# Natural period specs ("yesterday", "7d", …)
# ---------------------------------------------------------------------------

_RELATIVE = re.compile(r"^(\d+)\s*([dwhm])$", re.I)

#: Accepted spellings, for the error message and for --help.
SINCE_FORMS = (
    "today", "yesterday", "this-week", "last-week", "this-month",
    "<N>d (N days back, e.g. 3d)", "<N>w (N weeks back)", "<N>h (N hours back)",
    "<N>m (N months back)", "YYYY-MM-DD (that day)",
    "YYYY-MM-DD..YYYY-MM-DD (explicit range)",
)


def parse_since(spec: str, *, today: Optional[datetime.date] = None) -> Tuple[str, str]:
    """Turn a human period spec into ``(from_date, to_date)`` bounds.

    The point is that a person asking "what did I do yesterday" must not be
    made to compute a date (ms-184 受入条件 7). Both returned bounds are
    inclusive and in the shape :func:`classify` expects; the upper bound is
    left date-only so :func:`normalize_bound` widens it to end-of-day in one
    place rather than two.

    Raises ``ValueError`` with the accepted forms listed for an unparseable
    spec — never falls back to a different period than the one asked for,
    because a silently-substituted window returns a confident wrong answer.
    """
    raw = (spec or "").strip().lower()
    if not raw:
        raise ValueError("empty period spec")
    base = today or datetime.date.today()

    def _d(day: datetime.date) -> str:
        return day.isoformat()

    if raw in ("today", "今日"):
        return _d(base), _d(base)
    if raw in ("yesterday", "昨日"):
        y = base - datetime.timedelta(days=1)
        return _d(y), _d(y)
    if raw in ("this-week", "week", "今週"):
        start = base - datetime.timedelta(days=base.weekday())
        return _d(start), _d(base)
    if raw in ("last-week", "先週"):
        this_start = base - datetime.timedelta(days=base.weekday())
        last_start = this_start - datetime.timedelta(days=7)
        return _d(last_start), _d(this_start - datetime.timedelta(days=1))
    if raw in ("this-month", "month", "今月"):
        return _d(base.replace(day=1)), _d(base)

    # Explicit range "A..B"
    if ".." in raw:
        lo, _, hi = raw.partition("..")
        lo, hi = lo.strip(), hi.strip()
        if is_calendar_date(lo) and is_calendar_date(hi):
            if lo > hi:
                raise ValueError(f"period start {lo} is after end {hi}")
            return lo, hi
        raise ValueError(f"unparseable range {spec!r}; expected YYYY-MM-DD..YYYY-MM-DD")

    # A single explicit day
    if _DATE_ONLY.match(raw):
        if not is_calendar_date(raw):
            raise ValueError(
                f"{raw!r} is not a real calendar date "
                f"(month 1-12, and a day that exists in that month)")
        return raw, raw

    m = _RELATIVE.match(raw)
    if m:
        n = int(m.group(1))
        if n < 1:
            raise ValueError(f"period length must be at least 1 (got {spec!r})")
        unit = m.group(2).lower()
        if unit == "d":
            return _d(base - datetime.timedelta(days=n - 1)), _d(base)
        if unit == "w":
            return _d(base - datetime.timedelta(weeks=n)), _d(base)
        if unit == "h":
            # Hours need a real instant, not a day: return a datetime lower
            # bound so "6h" does not silently mean "today".
            now = datetime.datetime.now()
            lo = now - datetime.timedelta(hours=n)
            return lo.replace(microsecond=0).isoformat(), _d(base)
        if unit == "m":
            # Calendar months back, clamped to the 1st (no day-of-month math).
            month_index = (base.year * 12 + base.month - 1) - n
            year, month = divmod(month_index, 12)
            return _d(datetime.date(year, month + 1, 1)), _d(base)

    raise ValueError(
        f"unparseable period {spec!r}. Accepted: " + "; ".join(SINCE_FORMS)
    )


def is_duration_spec(spec: str) -> bool:
    """Whether ``spec`` names a LENGTH of time rather than a point in it.

    ``3d`` / ``2w`` / ``6h`` / ``2m`` answer "how far back from now", so they
    define a period's lower end and leave its upper end at the present moment.
    That makes them meaningful for a "since" flag and meaningless for an
    "until" one: :func:`parse_since` returns today as the upper bound for every
    one of them, so ``--until 3d`` resolved to exactly ``--until today``.
    Accepted, echoed back, and silently ignored — an independent AX review of
    PR #811 measured ``--since 7d --until 3d`` and ``--since 7d --until today``
    producing byte-identical windows, with exit 0 and no warning.

    A caller who has just learned that ``--since 3d`` means "3 days ago" will
    read ``--until 3d`` as "up to 3 days ago" and get "up to right now"
    instead. So the caller is told, rather than quietly given a different
    period. Point-in-time forms (``today`` / ``yesterday`` / ``last-week`` /
    an explicit date or range) carry their own upper bound and stay accepted.
    """
    return bool(_RELATIVE.match((spec or "").strip().lower()))


def undated_block(tally) -> dict:
    """Render an "could not be judged by date" tally for a result payload.

    One renderer, so every verb reports this with the same keys. ``search`` and
    this function agree by construction; ``recap``/``progress_window`` still
    spell it ``count``/``by_kind`` and are migrated separately (an AX review of
    PR #811 found the two shapes and was right that a caller who learns one
    gets a ``KeyError`` on the other — that convergence touches recap's own
    consumers, so it is its own change rather than a drive-by here).
    """
    tally = tally or {}
    return {"excluded": sum(tally.values()),
            "by_type": dict(sorted(tally.items()))}


__all__ = [
    "IN_WINDOW", "OUT_OF_WINDOW", "UNDATED", "SINCE_FORMS",
    "CREATION_STAMPS", "MOVEMENT_STAMPS", "RETRO_ENTRY_STAMPS", "stamps_of",
    "classify", "classify_any", "latest_in_window", "validate_range",
    "normalize_bound", "parse_since", "is_calendar_date", "is_duration_spec",
    "undated_block",
]
