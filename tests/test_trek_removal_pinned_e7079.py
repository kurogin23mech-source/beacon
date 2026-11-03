"""The removal itself is pinned, not just argued for in comments (ms-185 e-7079).

An independent maintainability review of PR #827 found that the PR argues at
length — in comments, docstrings and a CLI notice — that Trek has no entry
point left, while **nothing turns red if one comes back**. The deleted tests
(`TestCreate`, `test_unauthenticated_create_returns_401`) were removed rather
than inverted, so restoring `routers_treks.py` from history during e-7070 work,
or dropping the `trek)` arm from a frontend, would pass CI silently.

These three tests invert them. Each asserts the ABSENCE of an entrance, which
is the contract the PR actually claims:

  1. no creation route on the server
  2. the verb is gone from both CLI frontends, with a reason, non-zero
  3. tagging a NEW doc to a trek is refused

They are expected to be deleted together with the body in e-7070 — at which
point the endpoints and the notice go too, and an absence test for something
that no longer exists in any form has nothing left to say.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "server"))


def test_no_creation_route_on_the_server():
    """POST /api/treks must not exist (it was the only creation entrance).

    The path set comes from ``app.openapi()["paths"]``, not from walking
    ``app.routes``. The trek endpoints live in ``server/routers_treks.py`` and
    are mounted with ``include_router``, and across FastAPI versions an
    included router surfaces in ``app.routes`` differently: the version pinned
    locally (0.128) *flattens* the routes in, while the newer one CI installs
    (0.141) keeps them nested inside an ``_IncludedRouter`` wrapper whose
    entries have no uniform ``.path``. A flat walk therefore reads the same
    repository two opposite ways — and it did, in both directions at once:

      * the read-path assertion went red on CI only (the route is there; the
        walk could not see it), which is how this was found;
      * the POST assertion was *falsely green* on CI, and would have stayed
        green if ``POST /api/treks`` came back inside the router — which is
        the only place it ever lived. A guard that cannot fail is worse than
        no guard, because the green is believed.

    The OpenAPI path map is stable across both versions and carries the
    methods, so it answers "is this routed" without depending on how routers
    are represented. Same reasoning, and same idiom, as
    ``tests/test_app_router_me_e4869.py::test_me_routes_are_mounted``.
    """
    import app as app_module  # noqa: PLC0415
    paths = app_module.app.openapi().get("paths", {})
    methods = {m.lower() for m in paths.get("/api/treks", {})}
    assert "post" not in methods, (
        "POST /api/treks is routed again — the creation entrance is back, so "
        "every 'nothing can create a trek any more' claim in this repo is now "
        "false (lib/cmd_pr.py, the CLI notice, the RETIRING banners)."
    )
    # The read path is deliberately still routed (SPEC 方針5).
    read_methods = {m.lower() for m in paths.get("/api/treks/{trek_id}", {})}
    assert "get" in read_methods, (
        "GET /api/treks/{trek_id} disappeared — existing records must stay "
        "readable; removing the read path is e-7070's call, not a side effect. "
        f"(routed methods: {sorted(read_methods)})"
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BEACON_SKIP_AUTO_UPDATE"] = "1"
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                          env=env, timeout=120)


def test_both_cli_frontends_refuse_the_verb_with_a_reason():
    """`beacon trek …` must exit non-zero and say why, on bash AND Python."""
    bash = _run(["bash", str(REPO / "bin" / "beacon"), "trek", "list"])
    # The Python frontend's console entry is beacon_cli.main:main
    # (pyproject.toml), not a package __main__ — driving it the wrong way
    # produces an import error that happens to be non-zero, which would
    # make this test pass for the wrong reason.
    py = _run([sys.executable, "-c",
               "import sys; from beacon_cli.main import main; sys.exit(main(['trek', 'list']))"])
    for label, r in (("bin/beacon", bash), ("beacon_cli", py)):
        out = r.stdout + r.stderr
        assert r.returncode != 0, f"{label}: exited 0 for a removed verb\n{out}"
        assert "was removed" in out, (
            f"{label}: refused without saying the verb was removed — "
            f'indistinguishable from a typo.\n{out}')
        assert "ms-185" in out, f"{label}: no reference to why.\n{out}"


def test_tagging_a_new_doc_to_a_trek_is_refused():
    """The write refusal in lib/cmd_doc.py must stay wired."""
    import cmd_doc  # noqa: PLC0415
    assert hasattr(cmd_doc, "_refuse_trek_write"), (
        "_refuse_trek_write is gone — writing new trek references would be "
        "accepted again while nothing can create or verify a trek id."
    )
    try:
        cmd_doc._refuse_trek_write("doc add", "tk-does-not-exist")
    except SystemExit as exc:
        assert exc.code == 2, f"expected exit 2, got {exc.code}"
    else:
        raise AssertionError("_refuse_trek_write did not exit")


UI_COPIES = ("server/static/index.html", "desktop/dist/index.html")
# The UI's only ways in were the two action-dispatcher arms and the markup that
# fed them. ``?trek=<id>`` was the third, read at init; its call site is gone
# (the init read was deleted with e-7079), so the deep link no longer opens
# anything on its own.
UI_ENTRANCES = (
    "case 'open-trek'",
    "case 'menu-open-trek'",
    'data-action="open-trek"',
    'data-action="menu-open-trek"',
)


def test_no_ui_entrance_in_either_copy():
    """The screen must have no way to open a trek — in BOTH UI copies.

    This is the fourth entrance, and it was the one entrance whose closing was
    argued only in a comment (``openTrek()`` carries "Nothing calls openTrek()
    any more... Do not wire a new caller to this"). An independent
    maintainability review measured that claim by putting
    ``case 'open-trek': openTrek(el.dataset.trekId); return true;`` back and
    running the whole relevant suite — 113 passed, 0 failed. Nothing turned
    red, including ``scripts/check-frontend-drift.py --strict``, which compares
    the two copies to each other and so stays green when an entrance comes
    back in *both*.

    So the comment was the only thing holding this shut, while the other three
    entrances each had an executable absence test above. e-7070 will restore
    from this feature's own history on purpose (in reverse), and a bad merge or
    a copy-paste from that history is exactly how this arm comes back.

    Both copies are checked because the parity guard cannot distinguish "absent
    from both" from "present in both" — checking one copy only would leave the
    desktop build able to carry the entrance alone.
    """
    for rel in UI_COPIES:
        text = (REPO / rel).read_text(encoding="utf-8")
        for needle in UI_ENTRANCES:
            assert needle not in text, (
                f"{rel}: {needle!r} is back — the screen can open a trek "
                "again, so every 'nothing can create or open one' claim in "
                "this repo is now false for the UI axis."
            )
