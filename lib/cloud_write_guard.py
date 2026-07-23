"""Structural guard: tests must not create projects on the production cloud.

ms-123 / e-4029. The root cause of the 48 throwaway ``phase4-test`` projects
that piled up in the production directory was simple: a test / one-off check
called the project-creation API against production and never cleaned up. When
the test crashed (or just forgot teardown), the project leaked — and 45 of
them did.

Cleaning up the residue (e-4028) without closing this tap means the same
garbage accumulates again. So this module closes the tap in two layers, per
the user's "構造で防ぐ" principle (close it with a code constraint, not a
prompt request):

  第一層 (structure) — ``guard_project_create``: a hard barrier at the single
    choke point (``ApiClient.create_project``). In a test context, creating a
    project on the production cloud raises instead of writing. A test
    physically cannot leak a prod project. This is the primary defense.

  第二層 (insurance) — ``disposable_project``: for the rare test that must hit
    a real (non-prod) cloud, a context manager that guarantees the created
    project is archived on exit — even if the test body raises. Teardown can't
    be forgotten because it's structural (a ``finally``), so a crash mid-test
    still cleans up.

The escape hatch (``BEACON_ALLOW_PROD_TEST_WRITE=1``) is deliberately noisy:
an author has to set it on purpose, which is the moment to also reach for
``disposable_project``.
"""

from __future__ import annotations

import contextlib
import os
import urllib.parse

# The production Beacon cloud. Writing test projects here is the leak.
_PROD_HOSTS = frozenset({"beacon-ai.dev", "www.beacon-ai.dev"})
_PROD_HOST_SUFFIXES = (".beacon-ai.dev",)


def is_test_context() -> bool:
    """True when the current process is a test / CI driver.

    ``PYTEST_CURRENT_TEST`` is set by pytest for the duration of each test;
    ``BEACON_TEST_MODE`` is set explicitly (conftest.py sets it for the whole
    suite, and CI can export it) so subprocess children that invoke the
    ``beacon`` CLI inherit the test context too.
    """
    if os.environ.get("BEACON_TEST_MODE") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def is_prod_api_url(url: str) -> bool:
    """True when ``url`` points at the production Beacon cloud."""
    if not url:
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in _PROD_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in _PROD_HOST_SUFFIXES)


def guard_prod_project_write(base_url: str) -> None:
    """Raise if a test context is about to write a project to production.

    Covers every project-creating write path: ``create_project`` (POST) and
    ``put_project`` (PUT upsert) both land on ``/api/projects/{id}`` and both
    can materialize a new project, so both are guarded. Guarding only one
    would leave the other as a leak bypass.

    No-op outside a test context (normal CLI use is unaffected) and no-op for
    non-prod targets (local mode, staging, a sandbox cloud). The escape hatch
    ``BEACON_ALLOW_PROD_TEST_WRITE=1`` lets a test opt in, but such a test must
    wrap creation in :func:`disposable_project` so teardown always archives it.
    """
    if not is_test_context():
        return
    if not is_prod_api_url(base_url):
        return
    if os.environ.get("BEACON_ALLOW_PROD_TEST_WRITE") == "1":
        return
    raise RuntimeError(
        "cloud_write_guard: refusing to write a project to the production "
        f"cloud ({base_url}) from a test context. Tests must use local mode "
        "or a sandbox/staging cloud. If this test genuinely must hit prod, "
        "set BEACON_ALLOW_PROD_TEST_WRITE=1 AND wrap creation in "
        "cloud_write_guard.disposable_project(...) so a crash can't leak it."
    )


def _default_cleanup(client, project_id: str) -> None:
    """Best-effort archive of a disposable project via the envelope path.

    Mirrors ``beacon project cleanup`` (e-4028): mint a T1 envelope authorizing
    ``project.archive`` and archive with it. Best-effort — any failure here is
    swallowed so it never masks the test's own result; the worst case degrades
    to the same leak we're guarding against, which the periodic cleanup catches.
    """
    try:
        env = client.issue_bus_envelope(
            project_id, tier="T1", actions_authorized=["project.archive"],
        )
        client.archive_project(project_id, env)
    except Exception:
        pass


@contextlib.contextmanager
def disposable_project(
    client,
    project_id: str,
    name: str,
    objective: str = "",
    *,
    cleanup=None,
):
    """Create a cloud project guaranteed to be cleaned up on exit (第二層).

    For a test that must exercise a real (non-prod) cloud. Creates the project,
    yields its id, and on exit — success OR exception — runs ``cleanup`` (an
    optional ``callable(client, project_id)``; defaults to archiving via the
    envelope path). The teardown is a ``finally``, so a crashing test body
    still cleans up: the leak vector is structurally removed.

    Note: ``create_project`` still routes through :func:`guard_project_create`,
    so pointing this at the production cloud from a test remains blocked unless
    the author has explicitly set ``BEACON_ALLOW_PROD_TEST_WRITE=1``.
    """
    client.create_project(project_id, name, objective)
    try:
        yield project_id
    finally:
        (cleanup or _default_cleanup)(client, project_id)
