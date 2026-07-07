"""ms-95 / e-2411 — beacon cloud list shows owner email.

Companion to e-2288 (`beacon member list` owner row). The CLI shows
"who do I ask about this project" before any per-project lookup.

Pinned shape:
  - server ``list_projects`` returns ``owner`` (user_id) + ``owner_email``
    on every row (additive; existing keys preserved).
  - text output renders ``(owner: <email>)`` suffix on the name line.
  - JSON output (= ``BEACON_JSON=1``) passes the new fields through.
  - Resolution failure (= missing user record) renders the row without
    the owner suffix instead of crashing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "server" / "app.py"
FIRESTORE_PY = ROOT / "server" / "firestore_client.py"
COMMANDS_PY = ROOT / "lib" / "commands.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestServerSideOwnerEmail:
    def setup_method(self, _method):
        self.src = _read(FIRESTORE_PY)

    def test_list_projects_resolves_owner_email(self):
        # Match the function body up to the next top-level def to extract
        # the implementation we care about.
        m = re.search(
            r"def list_projects\([^)]*\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate list_projects body"
        body = m.group(1)
        assert "_resolve_owner_email" in body, (
            "list_projects must resolve owner_email per row (e-2411)."
        )
        assert '"owner_email"' in body
        assert '"owner"' in body, (
            "owner user_id must also be exposed so CLI can fall back to "
            "user_id when email is empty."
        )

    def test_list_projects_uses_per_call_email_cache(self):
        m = re.search(
            r"def list_projects\([^)]*\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m
        body = m.group(1)
        # The N-projects-by-same-owner case must not hit get_user N times.
        assert "_email_cache" in body, (
            "list_projects should cache owner email per call to avoid "
            "N+1 user lookups (e-2411)."
        )

    def test_list_projects_swallows_lookup_errors(self):
        m = re.search(
            r"def list_projects\([^)]*\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m
        body = m.group(1)
        # Failure to resolve email returns "" rather than raising — AC #3.
        assert "except Exception:" in body


class TestCliRendersOwnerEmail:
    def setup_method(self, _method):
        self.src = _read(COMMANDS_PY)

    def test_cmd_cloud_list_renders_owner_suffix(self):
        m = re.search(
            r"def cmd_cloud_list\(\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate cmd_cloud_list"
        body = m.group(1)
        assert 'owner_email' in body
        # The suffix format used by the CLI (= keep stable so the
        # user-facing string doesn't regress silently).
        assert '"  (owner: ' in body

    def test_cmd_cloud_list_skips_when_email_missing(self):
        m = re.search(
            r"def cmd_cloud_list\(\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m
        body = m.group(1)
        # Empty-string truthiness check OR explicit if-branch on owner_email.
        assert "if owner_email" in body or "owner_email else" in body, (
            "Empty owner_email must yield no suffix, not the literal "
            "string '(owner: )' (e-2411 AC #3)."
        )

    def test_json_mode_passes_through_owner_email(self):
        """JSON output already returns server response verbatim; no
        post-processing needed. Confirm cmd_cloud_list still uses
        ``json.dumps(projects, ensure_ascii=False)``."""
        m = re.search(
            r"def cmd_cloud_list\(\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m
        body = m.group(1)
        assert "json.dumps(projects" in body
