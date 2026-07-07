"""ms-95 / e-2320 — Trek scope mutation observability + caller boundary.

Two-part contract pinned by this file:

  (a) Server-side ``add_trek_scope_endpoint`` and ``remove_trek_scope_endpoint``
      emit a ``trek.scope.audit`` log line per mutation, carrying the caller's
      user_id / session_id and the affected entry. Without this, the silent
      project-wide re-add observed on 2026-06-23 (memo doc
      kINfY5a9LLnxHWWhtbZJ Finding 4) leaves no forensic trail in Cloud
      Logging.

  (b) The only sources that may call ``trek.add_scope_entry`` or
      ``trek.remove_scope_entry`` are the documented two paths:
        * ``lib/commands.py:cmd_trek_plan`` (= user-typed
          ``beacon trek plan --add-scope`` only)
        * ``server/app.py:add_trek_scope_endpoint`` (= HTTP)
      Any new caller in lib/ / server/ / scripts/ / channel/ that mutates
      scope would re-introduce a silent automated path and must be reviewed
      explicitly. This test fails if such a caller appears so the silent
      re-add pathology cannot regress.

This test is structural / grep-based; it does not exercise the FastAPI
endpoint end-to-end (= server requires Firestore creds for that). The
audit-line shape is pinned by reading the source body, which is cheaper
than booting the app.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "server" / "app.py"
COMMANDS_PY = ROOT / "lib" / "commands.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) Audit log shape on the server endpoints
# ---------------------------------------------------------------------------

class TestAuditLogShape:
    def setup_method(self, _method):
        self.src = _read(APP_PY)

    def test_audit_helper_exists(self):
        assert "def _log_trek_scope_audit(" in self.src, (
            "e-2320: server must emit a structured audit line on every "
            "trek scope mutation so the silent re-add (2026-06-23) can be "
            "traced from Cloud Logging."
        )

    def test_audit_helper_emits_required_fields(self):
        # Extract the function body by matching from def to the next
        # top-level @app. decorator (which marks the next route).
        m = re.search(
            r"def _log_trek_scope_audit\([^)]*\)[^:]*:(.*?)(?=^@app\.)",
            self.src,
            re.DOTALL | re.MULTILINE,
        )
        assert m, "could not locate _log_trek_scope_audit body"
        body = m.group(1)
        # Required fields on every audit record so a remove → re-add
        # sequence is fully reconstructible from logs.
        for field in ("trek_id", "user_id", "session_id", "entry", "action"):
            assert f'"{field}"' in body, (
                f"audit record must carry {field!r} (e-2320)"
            )
        assert "trek.scope.audit" in body, (
            "audit lines must carry the 'trek.scope.audit' event tag so "
            "Cloud Logging filters can grep for them."
        )

    def test_add_endpoint_calls_audit_helper(self):
        # Find the function body of add_trek_scope_endpoint and check it
        # ends with an audit call.
        m = re.search(
            r"def add_trek_scope_endpoint\([^)]*\)[^:]*:(.*?)(?=^@app\.)",
            self.src,
            re.DOTALL | re.MULTILINE,
        )
        assert m, "could not locate add_trek_scope_endpoint body"
        body = m.group(1)
        assert '_log_trek_scope_audit(' in body
        # ms-97 / e-2626 — scope-add now stages a pending op; the audit
        # action carries the stage tag so the log line can tell the
        # request-time stage ("add_pending") apart from the apply-time
        # stage ("scope_add_approved" emitted by the approve endpoint).
        assert ('action="add"' in body
                or 'action="add_pending"' in body), (
            "add endpoint must emit an audit line tagged with either "
            "'add' (pre-e-2626 immediate path) or 'add_pending' "
            "(e-2626 staging path)."
        )

    def test_remove_endpoint_calls_audit_helper(self):
        m = re.search(
            r"def remove_trek_scope_endpoint\([^)]*\)[^:]*:(.*?)(?=^@app\.)",
            self.src,
            re.DOTALL | re.MULTILINE,
        )
        assert m, "could not locate remove_trek_scope_endpoint body"
        body = m.group(1)
        assert '_log_trek_scope_audit(' in body
        # ms-97 / e-2611 — scope-remove now stages a pending op first; the
        # audit action carries the stage tag so the log line can tell the
        # request-time stage ("remove_pending") apart from the apply-time
        # stage ("scope_remove_approved" emitted by the approve endpoint).
        assert ('action="remove"' in body
                or 'action="remove_pending"' in body), (
            "remove endpoint must emit an audit line tagged with either "
            "'remove' (pre-e-2611 immediate path) or 'remove_pending' "
            "(e-2611 staging path)."
        )

    def test_endpoints_accept_request_for_session_header(self):
        # Both endpoints must take a Request param so the audit helper can
        # read X-Beacon-Session. The pattern is `request: Request` in the
        # signature.
        for fn in ("add_trek_scope_endpoint", "remove_trek_scope_endpoint"):
            m = re.search(
                rf"def {fn}\(([^)]*)\)",
                self.src,
                re.DOTALL,
            )
            assert m, f"could not locate {fn}"
            sig = m.group(1)
            assert "request: Request" in sig, (
                f"{fn} must accept request: Request so the audit helper "
                f"can read X-Beacon-Session (e-2320)."
            )


# ---------------------------------------------------------------------------
# (b) No undocumented automated callers
# ---------------------------------------------------------------------------

class TestNoUndocumentedScopeMutators:
    """If the silent project-wide re-add ever returns, the new caller will
    show up here and force a code review."""

    def _list_callers(self, fn_name: str) -> list[tuple[str, int]]:
        """Find every line in lib/ + server/ that calls ``fn_name``.

        Returns list of (relative_path, line_number) tuples. Excludes
        the definition site itself (= the line that starts ``def fn_name``).
        """
        hits: list[tuple[str, int]] = []
        for sub in ("lib", "server"):
            for py in (ROOT / sub).rglob("*.py"):
                try:
                    text = py.read_text(encoding="utf-8")
                except Exception:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    if f"{fn_name}(" not in line:
                        continue
                    # Skip the definition line itself.
                    if re.match(rf"\s*def\s+{re.escape(fn_name)}\(", line):
                        continue
                    rel = py.relative_to(ROOT).as_posix()
                    hits.append((rel, lineno))
        return hits

    def test_add_scope_entry_callers_only_documented_paths(self):
        callers = self._list_callers("add_scope_entry")
        caller_files = sorted({c[0] for c in callers})
        # ms-97 / e-2626 — scope-add is routed through the pending approval
        # flow (= AC23). ms-97 / Phase 7-C / e-2603 (AC24) — blanket
        # pre-approval adds ONE additional documented caller in
        # ``server/app.py`` (the ``add_trek_scope_endpoint`` auto-commit
        # branch, only reachable when ``is_blanket_approved`` matches).
        # Both callers route through trek.add_scope_entry which still
        # runs strict normalisation, so AC7 / AC23 invariants hold.
        # If a NEW direct caller appears outside these two files, AC23
        # has regressed.
        assert caller_files == ["lib/trek.py", "server/app.py"], (
            f"e-2320 contract (post ms-97 / e-2626 + e-2603 AC24): "
            f"trek.add_scope_entry callers must be exactly "
            f"['lib/trek.py', 'server/app.py'] (lib = "
            f"approve_pending_scope_op; server = blanket auto-commit "
            f"in add_trek_scope_endpoint). Found: {caller_files!r}. "
            f"Route any new caller through the pending approval flow "
            f"or the blanket approval gate."
        )

    def test_remove_scope_entry_callers_only_documented_paths(self):
        callers = self._list_callers("remove_scope_entry")
        caller_files = sorted({c[0] for c in callers})
        # ms-97 / e-2611 — scope-remove is now routed exclusively through
        # the pending approval flow. ``cmd_trek_plan`` no longer calls
        # ``remove_scope_entry`` directly (= it stages via
        # ``add_pending_scope_op``), and the server's DELETE endpoint
        # likewise stages instead of applying. The only remaining caller
        # is ``approve_pending_scope_op`` inside lib/trek.py itself — the
        # apply path runs through the approve endpoint, which in turn
        # invokes the helper. This narrowing is intentional and structural.
        assert caller_files == ["lib/trek.py"], (
            f"e-2320 contract (post ms-97 / e-2611): trek.remove_scope_entry "
            f"must only be called from lib/trek.py "
            f"(approve_pending_scope_op). Found: {caller_files!r}. "
            f"If a new direct mutation path appears, route it through the "
            f"pending approval flow instead so AC25 can't regress."
        )
