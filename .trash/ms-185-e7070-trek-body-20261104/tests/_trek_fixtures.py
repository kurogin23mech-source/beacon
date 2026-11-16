"""The one place a test arranges an EXISTING trek (ms-185 e-7079).

## Why this module exists

`POST /api/treks` — the only creation entrance — was removed by e-7079. Tests
whose subject is behaviour *after* a trek exists (membership rules, blockers,
review gates, slot preconditions, listing filters) still need one to exist, so
their arrangement step writes it straight to the store.

That arrangement was hand-copied into four test files, five times counting the
one file that re-inlined it fifteen lines below its own helper. An independent
maintainability review of PR #827 named the cost: when e-7070 changes
`new_trek`'s signature or removes `db.save_trek`, five sites break; and when
someone corrects one copy (three of the five omitted `description` / `type_`,
so the docstring claim "the fields mirror what the removed endpoint produced"
was true of exactly one of them) the others drift.

## What does NOT belong here

`tests/test_dynamodb_treks.py` also calls `db.save_trek` directly and is
deliberately left alone: its subject **is** the store (round-tripping a trek
through DynamoDB), so writing to the store is the thing under test, not an
arrangement shortcut around a removed endpoint. Folding it in here would hide
the assertion inside a helper named for the opposite purpose. Same call, two
unrelated reasons — do not unify them.

## TEMPORARY — this module is scheduled for deletion

It is a bridge, not a design. e-7070 deletes the Trek body and these tests with
it, and this file goes at the same time. The coupling it contains — tests
reaching past the HTTP layer into `app_module.db` and the document shape
`lib/trek.new_trek` produces — is accepted **only** because it is confined to
one place with this note on it. Do not build on it, and do not copy it back
out into a test file.
"""
from __future__ import annotations


def seed_trek(app_module, *, title: str, uid: str, email: str,
              sid: str, description: str = "", type_: str = "persistent") -> str:
    """Write one trek to the store and return its ``trek_id``.

    ``description`` / ``type_`` default to what the removed ``POST /api/treks``
    stamped, so a caller that does not care gets the shape the endpoint used to
    produce rather than a thinner one. Callers that DO care pass their own.

    ``app_module`` is passed in rather than imported: the store is a module
    global that each test file's ``reset_store`` fixture swaps, so binding it
    here would write to whichever store happened to be installed at import
    time instead of the one the test set up.
    """
    import trek as trek_mod  # noqa: PLC0415  (test-local, mirrors the router)
    doc = trek_mod.new_trek(
        title=title,
        creator_user_id=uid,
        creator_email=email,
        creator_session_id=sid,
        description=description,
        type_=type_,
    )
    app_module.db.save_trek(doc["trek_id"], doc)
    return doc["trek_id"]
