"""Creator (= 起票者) propagation tests for ms-43 / e-2246.

Pins three acceptance criteria from the task description:

  AC 1. ``core.milestone_add(..., author={...})`` stamps ``meta.author``
        on the new milestone so MS lists / detail can surface a
        creator label.
  AC 3. ``core.operation_open(..., author={...})`` stamps ``meta.author``
        on the new Operation so Operation lists / detail can surface a
        creator label.
  AC 6. ``_clean_author`` semantics carry through (= the shape stays
        ``{user_id, email, display_name}`` and empty fields drop so the
        Web UI's renderName fallback path stays well-defined).

The pure ``core`` paths are exercised against in-memory dicts (no I/O).
Server-side stamping is covered by ``test_display_name_propagation.py``
for the entry / log paths; this module pins the equivalent contract on
the milestone / operation entry points that ms-43 / e-2246 introduces.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (mirror test_core.py's minimal helpers)
# ---------------------------------------------------------------------------


def _project():
    return {"name": "test", "milestones": []}


# ---------------------------------------------------------------------------
# Milestone author stamping
# ---------------------------------------------------------------------------


class TestMilestoneAuthor:
    def test_milestone_add_with_full_author_stamps_meta(self):
        data = _project()
        author = {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }
        ms_id = core.milestone_add(data, "Plan v1", author=author)
        ms = data["milestones"][0]
        assert ms["id"] == ms_id
        assert ms["meta"]["author"] == author

    def test_milestone_add_without_author_omits_meta_author(self):
        # AC #6 — when no author is passed (= local CLI path) the MS must
        # not gain a ``meta.author`` field; the UI then falls back to
        # ``created_by`` / leaves the column blank.
        data = _project()
        core.milestone_add(data, "Local-only MS")
        ms = data["milestones"][0]
        assert "author" not in ms.get("meta", {})

    def test_milestone_add_strips_empty_author_fields(self):
        # AC #6 — _clean_author drops empty strings / Nones so the UI never
        # sees a sparse shape like {"user_id": "u-1", "email": ""}.
        data = _project()
        core.milestone_add(
            data, "Sparse author MS",
            author={"user_id": "u-bob", "email": "", "display_name": None},
        )
        ms = data["milestones"][0]
        assert ms["meta"]["author"] == {"user_id": "u-bob"}

    def test_milestone_add_ignores_non_dict_author(self):
        # _clean_author returns {} for non-dict input, which means no
        # author key is written — same shape as the no-author case.
        data = _project()
        core.milestone_add(data, "Robust", author="not-a-dict")  # type: ignore[arg-type]
        ms = data["milestones"][0]
        assert "author" not in ms.get("meta", {})

    def test_milestone_add_drops_unknown_author_keys(self):
        # The allowlist is exactly {user_id, email, display_name}; any
        # other key (e.g. role / org) is silently dropped to keep the
        # on-disk shape stable.
        data = _project()
        core.milestone_add(
            data, "Strict shape",
            author={
                "user_id": "u-c", "email": "c@x", "display_name": "C",
                "role": "owner", "secret": "no",
            },
        )
        ms = data["milestones"][0]
        assert set(ms["meta"]["author"].keys()) == {"user_id", "email", "display_name"}


# ---------------------------------------------------------------------------
# Operation author stamping
# ---------------------------------------------------------------------------


class TestOperationAuthor:
    def test_operation_open_with_full_author_stamps_meta(self):
        data = _project()
        author = {
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        }
        data, op = core.operation_open(
            data, "Weekly Health Check",
            schedule="weekly", log_source="health",
            author=author,
        )
        assert op["meta"]["author"] == author
        # The operation must also be appended to data.operations.
        assert data["operations"][0]["meta"]["author"] == author

    def test_operation_open_without_author_omits_meta_author(self):
        data = _project()
        data, op = core.operation_open(data, "CLI-only Op")
        assert "author" not in op.get("meta", {})

    def test_operation_open_strips_empty_author_fields(self):
        data = _project()
        data, op = core.operation_open(
            data, "Sparse Op",
            author={"user_id": "", "email": "z@x", "display_name": ""},
        )
        assert op["meta"]["author"] == {"email": "z@x"}

    def test_operation_open_todo_status_still_stamps_author(self):
        # status='todo' (= outline-only Operation) shouldn't suppress
        # author stamping. Catches a regression where author was only
        # threaded through the "open" branch.
        data = _project()
        data, op = core.operation_open(
            data, "Outline Op", status="todo",
            author={"user_id": "u-x", "display_name": "X"},
        )
        assert op["meta"]["author"] == {"user_id": "u-x", "display_name": "X"}


# ---------------------------------------------------------------------------
# _clean_author parity — keep the canonical contract pinned at the helper
# level so any future regression at the call site shows up here too.
# ---------------------------------------------------------------------------


class TestCleanAuthor:
    def test_clean_author_allowlists_three_keys(self):
        # The helper is private (_) but the AC #6 promise rides on its
        # shape, so we pin it directly. Empty / non-string values drop.
        cleaned = core._clean_author({
            "user_id": "u-1",
            "email": "u@x",
            "display_name": "U",
            "role": "admin",        # not allowlisted
            "session_id": "sid-1",  # not allowlisted
        })
        assert cleaned == {"user_id": "u-1", "email": "u@x", "display_name": "U"}

    def test_clean_author_handles_none(self):
        assert core._clean_author(None) == {}

    def test_clean_author_trims_whitespace(self):
        cleaned = core._clean_author({"display_name": "  spaced  "})
        assert cleaned == {"display_name": "spaced"}
