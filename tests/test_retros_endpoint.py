"""ms-46 e-750: regression tests for the retros API + render contract.

Background
----------
Before this fix the Web UI / Tauri Documents tab would silently hide every
retro week (W22 being the reported symptom) when ``state.documents`` was
empty, because ``renderDocumentsSection`` bailed out with "No documents yet."
before the retros section was even considered. Two things are pinned down
here so the bug cannot quietly come back:

1. The server contract: ``GET /api/projects/{id}/retros`` must return every
   week as a flat list of ``{"week", "updated_at", "content"}`` in
   descending week order. Edge case: when multiple retros exist (W19..W23),
   none of them is dropped.

2. The render contract (assertions over the static HTML): the documents-tab
   renderer must (a) not short-circuit when ``state.documents`` is empty,
   (b) keep the retros section reachable independent of documents, and
   (c) hoist the retro detail-view early-return above the list builder.
"""

from __future__ import annotations

import copy
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402

# ---------------------------------------------------------------------------
# In-memory firestore stand-in (mirrors tests/test_api.py pattern but scoped
# to just the bits we need for the retros endpoint).
# ---------------------------------------------------------------------------

_project_store: dict[str, dict] = {}
_retro_store: dict[tuple[str, str], dict] = {}


def _mock_get_project(project_id: str):
    data = _project_store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _project_store[project_id] = copy.deepcopy(data)


def _mock_list_projects():
    return [
        {"project_id": pid, "name": d.get("name", ""), "objective": d.get("objective", "")}
        for pid, d in _project_store.items()
    ]


def _mock_list_retros(project_id: str):
    rows = [
        {"week": week, **copy.deepcopy(payload)}
        for (pid, week), payload in _retro_store.items()
        if pid == project_id
    ]
    # Real firestore_client.list_retros orders by week DESC; the API
    # contract surfaced to the frontend expects the same ordering so we
    # reproduce it faithfully here.
    rows.sort(key=lambda r: r["week"], reverse=True)
    return rows


def _mock_get_retro(project_id: str, week: str):
    payload = _retro_store.get((project_id, week))
    if payload is None:
        return None
    return {"week": week, **copy.deepcopy(payload)}


def _mock_save_retro(project_id: str, week: str, content: str):
    import datetime

    _retro_store[(project_id, week)] = {
        "week": week,
        "content": content,
        "updated_at": datetime.datetime.now().isoformat(),
    }


from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

# Disable auth for these functional tests. We deliberately do NOT reload
# app_module — other tests in the suite (test_api.py et al.) share the
# same imported module and reloading it would tear down their fixtures.
app_module._auth_enabled = False
client = TestClient(app_module.app)

PROJECT_ID = "retros-fixture"


@pytest.fixture(autouse=True)
def _install_mocks():
    """Install firestore mocks *per test* so we don't clobber the globals
    that test_api.py (and friends) install at module-import time.

    Previously this module patched ``firestore_client.<fn>`` at import
    time, which meant whichever test module pytest loaded last "won" the
    binding race and the other file's tests failed. Doing the patch per
    test, with explicit teardown, keeps the two suites independent.
    """
    saved = {
        "get_project": firestore_client.get_project,
        "save_project": firestore_client.save_project,
        "list_projects": firestore_client.list_projects,
        "list_retros": getattr(firestore_client, "list_retros", None),
        "get_retro": getattr(firestore_client, "get_retro", None),
        "save_retro": getattr(firestore_client, "save_retro", None),
    }
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    firestore_client.list_projects = _mock_list_projects
    firestore_client.list_retros = _mock_list_retros
    firestore_client.get_retro = _mock_get_retro
    firestore_client.save_retro = _mock_save_retro

    _project_store.clear()
    _retro_store.clear()
    _project_store[PROJECT_ID] = {"name": "Retro fixture", "milestones": []}
    try:
        yield
    finally:
        _project_store.clear()
        _retro_store.clear()
        for name, fn in saved.items():
            if fn is not None:
                setattr(firestore_client, name, fn)


# ---------------------------------------------------------------------------
# Server contract
# ---------------------------------------------------------------------------


def _seed_weeks(weeks: list[str]) -> None:
    for w in weeks:
        _mock_save_retro(PROJECT_ID, w, f"# Retro {w}\n\nplaceholder body for {w}.\n")


def test_retros_endpoint_returns_every_week_including_w22():
    """Reported symptom: cloud has W19/W20/W22/W23 but loadRetros loses W22.

    The API itself must never be the one dropping a week. We seed a noisy
    mixture (including W22 next to the W23 it was being hidden by) and
    assert every week round-trips back through ``/api/projects/{id}/retros``.
    """
    _seed_weeks(["2026-W19", "2026-W20", "2026-W22", "2026-W23"])

    resp = client.get(f"/api/projects/{PROJECT_ID}/retros")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert isinstance(payload, list), "loadRetros expects a flat array"

    weeks = [r["week"] for r in payload]
    assert weeks == ["2026-W23", "2026-W22", "2026-W20", "2026-W19"], (
        f"all four weeks must survive, in DESC week order; got {weeks}"
    )

    # Each row must carry the {week, updated_at, content} contract that the
    # frontend renderer destructures.
    for row in payload:
        assert set(row.keys()) >= {"week", "updated_at", "content"}, row


def test_retros_endpoint_is_empty_list_when_no_retros():
    """A project with zero retros must still return ``[]`` (not 404 / null).

    The Documents tab calls ``loadRetros`` unconditionally on first switch;
    a non-array response would crash ``state.retros.map(...)`` downstream.
    """
    resp = client.get(f"/api/projects/{PROJECT_ID}/retros")
    assert resp.status_code == 200
    assert resp.json() == []


def test_retros_endpoint_single_week_still_listed():
    """Minimum reproducible case: one retro must always be visible.

    Regression guard: a previous render path lost the *only* retro when
    ``state.documents`` was empty. The server must at least keep returning
    the row so the frontend has a chance to show it.
    """
    _seed_weeks(["2026-W22"])
    resp = client.get(f"/api/projects/{PROJECT_ID}/retros")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["week"] == "2026-W22"


def test_retros_endpoint_get_specific_week_w22():
    """``cloud_get_retro(week='2026-W22')`` must round-trip the content."""
    _seed_weeks(["2026-W19", "2026-W22", "2026-W23"])
    resp = client.get(f"/api/projects/{PROJECT_ID}/retros/2026-W22")
    assert resp.status_code == 200
    body = resp.json()
    assert body["week"] == "2026-W22"
    assert "placeholder body for 2026-W22" in body["content"]


# ---------------------------------------------------------------------------
# Render contract (static-source assertions)
#
# We can't spin up a real browser here, but we can pin the structural shape
# of ``renderDocumentsSection`` so the specific regression cannot return:
# the documents-empty short-circuit must be gone, and the retros section
# must remain reachable without depending on the documents section.
# ---------------------------------------------------------------------------


def _read_index_html() -> str:
    return (REPO_ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")


def _extract_function_body(src: str, fn_name: str) -> str:
    """Return the body (including braces) of ``function fn_name(...) { ... }``.

    Simple brace-counter — works because the helper functions in
    ``server/static/index.html`` are well-formed top-level declarations.
    """
    needle = f"function {fn_name}("
    start = src.find(needle)
    assert start != -1, f"{fn_name} not found in index.html"
    brace = src.find("{", start)
    assert brace != -1
    depth = 0
    for i in range(brace, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
    raise AssertionError(f"unbalanced braces in {fn_name}")


def test_render_documents_section_does_not_short_circuit_on_empty_documents():
    """The W22 root cause: bailing out on ``state.documents.length === 0``.

    That single early-return swallowed every retro even when ``state.retros``
    was already populated. The new code must never include that guard.
    """
    body = _extract_function_body(_read_index_html(), "renderDocumentsSection")
    forbidden = re.compile(
        r"if\s*\(\s*state\.documents\.length\s*===\s*0\s*\)\s*\{\s*return",
        re.MULTILINE,
    )
    assert not forbidden.search(body), (
        "renderDocumentsSection must NOT early-return on empty documents — "
        "doing so hides every retro (the e-750 W22 symptom)."
    )


def test_render_documents_section_retro_detail_branch_is_hoisted():
    """The ``state.retroContent`` branch must run BEFORE the list builder.

    Previously the documents-empty short-circuit could return "No documents
    yet." even when the user was already drilled into a retro detail view.
    Pinning the order keeps that class of bug out.
    """
    body = _extract_function_body(_read_index_html(), "renderDocumentsSection")
    retro_idx = body.find("state.retroContent")
    let_html_idx = body.find("let html")
    assert retro_idx != -1, "retroContent branch missing"
    assert let_html_idx != -1, "html accumulator missing"
    assert retro_idx < let_html_idx, (
        "the state.retroContent early-return must come before the html "
        "accumulator so it can never strand a partially built template."
    )


def test_render_documents_section_renders_retros_independent_of_documents():
    """Documents and retros are peers, not parent/child.

    The retros section header (``REVIEWS``) must live inside its own
    ``if (state.retros.length > 0)`` block that is *not* nested under any
    documents-related guard. We approximate the structural check by
    asserting that the retros block exists and that the ``REVIEWS`` literal
    appears after the documents block has closed (single ``let html = ''``
    accumulator pattern).
    """
    body = _extract_function_body(_read_index_html(), "renderDocumentsSection")
    assert "let html = ''" in body, (
        "the accumulator must start empty so each section opts in instead "
        "of opting out — required for documents/retros independence."
    )
    assert "if (state.retros.length > 0)" in body, "retros render branch missing"
    assert "REVIEWS" in body, "retros section header missing"

    # The retros guard must not be syntactically nested inside the documents
    # guard. We verify this by checking that the documents block closes
    # before the retros guard opens — a cheap textual proxy that catches
    # accidental re-nesting.
    docs_open = body.find("if (state.documents.length > 0)")
    retros_open = body.find("if (state.retros.length > 0)")
    assert docs_open != -1 and retros_open != -1
    # The documents block opens with `{` after the guard; find its matching `}`
    brace_after_docs = body.find("{", docs_open)
    depth = 0
    docs_close = -1
    for i in range(brace_after_docs, len(body)):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                docs_close = i
                break
    assert docs_close != -1, "documents block not balanced"
    assert docs_close < retros_open, (
        "retros guard must live OUTSIDE the documents guard so it renders "
        "even when state.documents is empty (e-750 root cause)."
    )
