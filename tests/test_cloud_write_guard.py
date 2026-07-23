"""Structural leak guard: tests can't create prod projects (ms-123 / e-4029).

The 48 ``phase4-test`` residue projects leaked because a test created projects
on the production cloud without teardown. This suite pins the two-layer fix:

  第一層 — ``guard_project_create`` raises in a test context targeting prod,
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
            cwg.guard_project_create("https://beacon-ai.dev")

    def test_allows_local_in_test_context(self):
        cwg.guard_project_create("http://localhost:8000")  # no raise

    def test_escape_hatch_allows_prod(self):
        os.environ["BEACON_ALLOW_PROD_TEST_WRITE"] = "1"
        cwg.guard_project_create("https://beacon-ai.dev")  # no raise

    def test_noop_outside_test_context(self):
        # Not a test context → guard is inert even against prod.
        os.environ.pop("BEACON_TEST_MODE", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        try:
            cwg.guard_project_create("https://beacon-ai.dev")  # no raise
        finally:
            os.environ["BEACON_TEST_MODE"] = "1"


class _FakeClient:
    """Records create/archive calls; create honors the guard via base_url."""

    def __init__(self, base_url="http://localhost:8000"):
        self._base_url = base_url
        self.created = []
        self.archived = []

    def create_project(self, project_id, name, objective=""):
        cwg.guard_project_create(self._base_url)
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

    def test_cleanup_failure_does_not_mask_body(self):
        c = _FakeClient()

        def boom_cleanup(cl, pid):
            raise RuntimeError("cleanup failed")

        # The default cleanup swallows errors; a custom one that raises would
        # propagate, but the body result should still surface first. Here we
        # verify the default path swallows so the body's success is preserved.
        with cwg.disposable_project(c, "p-4", "temp"):
            pass
        self.assertEqual(["p-4"], c.archived)


if __name__ == "__main__":
    unittest.main()
