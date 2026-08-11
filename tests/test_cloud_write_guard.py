"""Structural leak guard: tests can't create prod projects (ms-123 / e-4029).

The 48 ``phase4-test`` residue projects leaked because a test created projects
on the production cloud without teardown. This suite pins the two-layer fix:

  第一層 — ``guard_prod_project_write`` raises in a test context targeting prod,
           and is a no-op for local/staging or normal (non-test) CLI use.
  第二層 — ``disposable_project`` always cleans up on exit, even when the test
           body raises (teardown can't be forgotten).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import cloud_write_guard as cwg  # noqa: E402


class TestIsProdApiUrl(unittest.TestCase):
    def test_prod_hosts_match(self):
        for url in (
            "https://beacon-ai.dev",
            "https://beacon-ai.dev/",
            "https://www.beacon-ai.dev",
            "https://beacon-ai.dev/api/projects/x",
        ):
            self.assertTrue(cwg.is_prod_api_url(url), url)

    def test_non_prod_not_matched(self):
        for url in (
            "http://localhost:8000",
            "http://127.0.0.1:8080",
            "https://staging.example.com",
            "https://beacon-ai.dev.evil.com",  # suffix trick must NOT match
            "",
        ):
            self.assertFalse(cwg.is_prod_api_url(url), url)


class TestGuardProjectCreate(unittest.TestCase):
    def setUp(self):
        # Snapshot + isolate the env this guard reads.
        self._saved = {k: os.environ.get(k) for k in (
            "BEACON_TEST_MODE", "PYTEST_CURRENT_TEST",
            "BEACON_ALLOW_PROD_TEST_WRITE",
        )}
        # Force a known test-context state (pytest sets PYTEST_CURRENT_TEST).
        os.environ["BEACON_TEST_MODE"] = "1"
        os.environ.pop("BEACON_ALLOW_PROD_TEST_WRITE", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_blocks_prod_in_test_context(self):
        with self.assertRaises(RuntimeError):
            cwg.guard_prod_project_write("https://beacon-ai.dev")

    def test_allows_local_in_test_context(self):
        cwg.guard_prod_project_write("http://localhost:8000")  # no raise

    def test_escape_hatch_allows_prod(self):
        os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = "1"
        cwg.guard_prod_project_write("https://beacon-ai.dev")  # no raise

    def test_noop_outside_test_context(self):
        # Not a test context → guard is inert even against prod.
        os.environ.pop("BEACON_TEST_MODE", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            cwg.guard_prod_project_write("https://beacon-ai.dev")  # no raise
        finally:
            os.environ["BEACON_TEST_MODE"] = "1"


class TestGuardBusWrite(unittest.TestCase):
    """ms-108 e-5194 follow-up: the bus-post choke point.

    guard_prod_project_write only covered project *creation*, so a test that
    posted to the bus of an already-existing prod project slipped through —
    the exact leak that let a non-hermetic operation-trigger unit test spray
    op-1 "test" events onto the live bus every suite run. Pin the same
    test-context / prod-target / escape-hatch semantics for bus posts.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "BEACON_TEST_MODE", "PYTEST_CURRENT_TEST",
            "BEACON_ALLOW_PROD_TEST_WRITE",
        )}
        os.environ["BEACON_TEST_MODE"] = "1"
        os.environ.pop("BEACON_ALLOW_PROD_TEST_WRITE", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_blocks_prod_in_test_context(self):
        with self.assertRaises(RuntimeError):
            cwg.guard_prod_bus_write("https://beacon-ai.dev")

    def test_allows_local_in_test_context(self):
        cwg.guard_prod_bus_write("http://localhost:8000")  # no raise

    def test_escape_hatch_allows_prod(self):
        os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = "1"
        cwg.guard_prod_bus_write("https://beacon-ai.dev")  # no raise

    def test_noop_outside_test_context(self):
        os.environ.pop("BEACON_TEST_MODE", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            cwg.guard_prod_bus_write("https://beacon-ai.dev")  # no raise
        finally:
            os.environ["BEACON_TEST_MODE"] = "1"


class TestApiClientBusPostGuarded(unittest.TestCase):
    """The guard is wired into ApiClient.post_bus_event itself, so even a test
    whose own mock is misconfigured (the failure mode that caused this leak)
    cannot reach the live bus."""

    def test_post_bus_event_blocks_prod_in_test_context(self):
        import api_client
        saved = os.environ.get("BEACON_TEST_MODE")
        os.environ["BEACON_TEST_MODE"] = "1"
        try:
            client = api_client.ApiClient("https://beacon-ai.dev", token="x")
            with self.assertRaises(RuntimeError):
                client.post_bus_event(
                    "beacon-b95643", "operation-trigger",
                    payload={"op_id": "op-1", "message": "test"},
                )
        finally:
            if saved is None:
                os.environ.pop("BEACON_TEST_MODE", None)
            else:
                os.environ["BEACON_TEST_MODE"] = saved


class _FakeClient:
    """Records create/archive calls; create honors the guard via base_url."""

    def __init__(self, base_url="http://localhost:8000"):
        self._base_url = base_url
        self.created = []
        self.archived = []

    def create_project(self, project_id, name, objective=""):
        cwg.guard_prod_project_write(self._base_url)
        self.created.append(project_id)
        return {"project_id": project_id}

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized):
        return {"tier": tier, "actions_authorized": actions_authorized,
                "project_id": project_id}

    def archive_project(self, project_id, envelope):
        self.archived.append(project_id)
        return {"status": "archived"}


class TestDisposableProject(unittest.TestCase):
    def test_cleans_up_on_success(self):
        c = _FakeClient()
        with cwg.disposable_project(c, "p-1", "temp") as pid:
            self.assertEqual("p-1", pid)
        self.assertEqual(["p-1"], c.created)
        self.assertEqual(["p-1"], c.archived)  # default cleanup archived it

    def test_cleans_up_even_on_exception(self):
        c = _FakeClient()
        with self.assertRaises(ValueError):
            with cwg.disposable_project(c, "p-2", "temp"):
                raise ValueError("boom")
        # teardown ran despite the exception — no leak
        self.assertEqual(["p-2"], c.archived)

    def test_custom_cleanup_callback(self):
        c = _FakeClient()
        seen = []
        with cwg.disposable_project(
            c, "p-3", "temp", cleanup=lambda cl, pid: seen.append(pid)
        ):
            pass
        self.assertEqual(["p-3"], seen)
        self.assertEqual([], c.archived)  # default cleanup NOT used

    def test_custom_cleanup_exception_propagates(self):
        # ms-123 review: a custom teardown that raises must NOT be hidden — it
        # propagates out of the context manager (the failure is visible).
        c = _FakeClient()

        def boom_cleanup(cl, pid):
            raise RuntimeError("cleanup failed")

        with self.assertRaises(RuntimeError):
            with cwg.disposable_project(c, "p-4", "temp", cleanup=boom_cleanup):
                pass

    def test_default_cleanup_swallows_but_warns_on_failure(self):
        # ms-123 review (AX+maintainability consensus): the DEFAULT cleanup
        # swallows so it never masks the test body's result, but it must SURFACE
        # the failure on stderr (a failed cleanup is the leak this guards) with a
        # manual-recovery command — not silently swallow.
        import io
        import contextlib

        class _FailingCleanupClient(_FakeClient):
            def issue_bus_envelope(self, project_id, *, tier, actions_authorized):
                raise RuntimeError("envelope refused")

        c = _FailingCleanupClient()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with cwg.disposable_project(c, "p-5", "temp"):
                pass
        msg = err.getvalue()
        self.assertIn("p-5", msg)
        self.assertIn("FAILED", msg)
        self.assertIn("beacon project cleanup --confirm", msg)
        self.assertEqual([], c.archived)  # archive never happened


if __name__ == "__main__":
    unittest.main()
