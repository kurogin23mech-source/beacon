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
        assert 'action="add"' in body, (
            "audit call from the add endpoint must tag action='add' (= "
            "distinguishes from remove in log queries)."
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
        assert 'action="remove"' in body

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
        # Expected: cmd_trek_plan (lib/commands.py) + add_trek_scope_endpoint
        # (server/app.py). Doc / test references in test files are excluded
        # by the lib/ + server/ scope of _list_callers.
        caller_files = sorted({c[0] for c in callers})
        assert caller_files == sorted([
            "lib/commands.py",
            "server/app.py",
        ]), (
            f"e-2320 contract: trek.add_scope_entry must only be called from "
            f"lib/commands.py (cmd_trek_plan) and server/app.py "
            f"(add_trek_scope_endpoint). Found callers in: {caller_files!r}. "
            f"If you are adding an automated path on purpose, update this "
            f"test and add audit hooks per the e-2320 pattern."
        )
        # Defense in depth: also assert the count matches 1 caller each.
        per_file = {}
        for f, _ in callers:
            per_file[f] = per_file.get(f, 0) + 1
        assert per_file == {
            "lib/commands.py": 1,
            "server/app.py": 1,
        }, f"unexpected caller count: {per_file!r}"

    def test_remove_scope_entry_callers_only_documented_paths(self):
        callers = self._list_callers("remove_scope_entry")
        caller_files = sorted({c[0] for c in callers})
        assert caller_files == sorted([
            "lib/commands.py",
            "server/app.py",
        ]), (
            f"e-2320 contract: trek.remove_scope_entry must only be called "
            f"from lib/commands.py and server/app.py. Found: {caller_files!r}"
        )
