"""Version endpoint router.

ms-127 e-4868 (B フェーズ scaffold): the first resource extracted from the
server/app.py god-module (13k+ lines) into a standalone APIRouter, establishing
the `make_router()` factory + `app.include_router(...)` pattern the rest of the
split follows. Mirrors the existing precedent `server/trailnode.py`
(`make_router(require_auth)`), except `/api/version` needs no auth, so this
factory takes no dependency — the minimal proof of the pattern.

Pure move: the route body is verbatim from app.py's `get_server_version`. No
behavior change (same path, same response shape `{"cli", "git_rev"}`).
"""
from __future__ import annotations

import os

from fastapi import APIRouter


def make_router() -> APIRouter:
    """Build the version router. Called once from app.py and mounted via
    ``app.include_router(make_router())``. No shared app state or auth is
    needed, so nothing is injected — auth-requiring routers (follow-up tasks)
    will take ``require_auth`` as an argument like ``trailnode.make_router``."""
    router = APIRouter()

    @router.get("/api/version")
    def get_server_version():
        """Server-side beacon CLI version + git revision.

        Returned to the Web UI so the header banner can show
            "Beacon 0.4.0 (rev abc1234)"
        and so the client can detect a stale tab when the server is upgraded.

        See e-587 for the UI hookup (server/static/index.html).
        """
        import subprocess
        try:
            # lib/commands.py is the source of truth for the version string.
            from commands import __version__ as cli_version  # type: ignore
        except Exception:
            cli_version = "unknown"

        git_rev = ""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            if result.returncode == 0:
                git_rev = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        return {"cli": cli_version, "git_rev": git_rev}

    return router
