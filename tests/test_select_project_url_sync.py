"""ms-43 e-1275: regression test for Web UI project switch URL sync.

Background
----------
Switching projects via the in-app project switcher (``selectProject`` /
``menuSelectProject``) used to update ``state.projectId`` but leave
``window.location.search`` pointing at the previous project. The "Copy link"
button reads ``window.location.href``, so the shared URL pointed at the wrong
project after every switch.

The fix lives in ``selectProject`` (the single funnel every switch path goes
through): on entry, it calls ``history.replaceState`` to rewrite
``?project=<id>`` to match the new ``projectId``. replaceState (not pushState)
is used because the previous project's URL is not a meaningful Back target.

We can't spin up a real browser here, but we can pin structural shape of
``selectProject`` so the specific regression cannot return.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "server" / "static" / "index.html"


def _read_index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _extract_function_body(src: str, fn_name: str) -> str:
    """Return ``function fn_name(...) { ... }`` body (including braces).

    Simple brace-counter — works because ``selectProject`` is a well-formed
    top-level declaration in ``server/static/index.html``.
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


def test_select_project_syncs_url_via_history_replaceState():
    """selectProject must write the new projectId into window.location.search.

    Without this, Copy link / address bar keep pointing at the previously
    selected project after the in-app switcher swaps state (e-1275).
    """
    body = _extract_function_body(_read_index_html(), "selectProject")
    assert "history.replaceState" in body, (
        "selectProject must call history.replaceState to keep the URL in "
        "sync with state.projectId — otherwise Copy link shares a URL for "
        "the previous project (e-1275 regression)."
    )


def test_select_project_uses_replaceState_not_pushState_for_project_switch():
    """Project switch must not pollute browser history.

    pushState would make Back silently re-bind to the previous project,
    which is surprising UX. The previous project's URL is not a meaningful
    Back target; replaceState is correct here.
    """
    body = _extract_function_body(_read_index_html(), "selectProject")
    # Allow pushState elsewhere in the function (none exists today), but
    # the URL-sync write itself must be replaceState. We approximate this
    # by checking the URL-writing block uses replaceState.
    assert not re.search(
        r"history\.pushState\s*\([^)]*['\"][^'\"]*\?project=", body
    ), (
        "selectProject must use history.replaceState, not pushState, for "
        "the project-id URL sync. pushState would let Back silently rebind "
        "to the previous project."
    )


def test_select_project_writes_new_project_id_into_search_param():
    """The URL write must set ?project=<id> with URLSearchParams.

    Using URLSearchParams (rather than naive string concat) is what keeps
    other query params and the hash intact across switches — important
    because deep links like #doc/<id> can be active during a switch.
    """
    body = _extract_function_body(_read_index_html(), "selectProject")
    assert "URLSearchParams" in body, (
        "selectProject must build the new URL via URLSearchParams so that "
        "other query params and the hash are preserved across switches."
    )
    assert "params.set('project'" in body or 'params.set("project"' in body, (
        "selectProject must call params.set('project', projectId) on the "
        "URLSearchParams instance."
    )


def test_select_project_preserves_hash_when_rewriting_url():
    """Deep-link hashes (#doc/..., #ms/...) must survive a project switch.

    The URL builder must include ``window.location.hash`` in the new URL.
    Without it, switching projects while a deep link is open would silently
    drop the hash and the user would land on the bare dashboard.
    """
    body = _extract_function_body(_read_index_html(), "selectProject")
    assert "window.location.hash" in body, (
        "selectProject's URL sync must preserve window.location.hash so "
        "active deep links (#doc/..., #ms/..., #entry/...) survive a "
        "project switch."
    )


def test_select_project_url_sync_is_idempotent():
    """No-op when the URL already matches — avoids spurious history writes.

    init() calls selectProject(urlProjectId) at boot with the URL already
    set. The replaceState write should be guarded so the same value isn't
    rewritten on every call.
    """
    body = _extract_function_body(_read_index_html(), "selectProject")
    # The guard is `params.get('project') !== projectId`.
    assert re.search(
        r"params\.get\(\s*['\"]project['\"]\s*\)\s*!==\s*projectId", body
    ), (
        "selectProject must guard the replaceState write with a "
        "`params.get('project') !== projectId` check so it's a no-op when "
        "the URL already matches the new project id."
    )
