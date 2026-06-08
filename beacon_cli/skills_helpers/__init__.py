"""Skill-side composition helpers (ms-54 follow-ups).

The /beacon-dm-send and /beacon-dm-reply Skills are markdown files that
describe how Claude should talk to the user. The actual CLI composition
they perform (turning the picked session_id + payload text into a concrete
`beacon bus send …` invocation, plus the budget-gate gymnastics on the
reply path) lives here so it can be unit-tested without spawning Claude.

The Skill markdown calls into these helpers conceptually: it asks Claude
to assemble an argv list following the same rules these helpers encode,
then to run it with the Bash tool. We keep the rules in one place so the
Skill prose and the tests can't drift.
"""

from __future__ import annotations

__all__ = [
    "dm_send",
    "dm_reply",
]
