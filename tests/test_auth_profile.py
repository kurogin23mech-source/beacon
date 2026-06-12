"""Unit tests for ``lib/auth.py``'s profile-aware path resolution
(ms-64 / e-1457).

Pins the SPEC requirements:
- ``_credentials_path()`` reads from ``profile.resolve_active_profile()``
- ``_firebase_config_path()`` prefers the active profile's copy and falls
  back to the bundled file at ``lib/firebase_config.json``
- All BEACON_HOME / CREDENTIALS_PATH / FIREBASE_CONFIG_PATH module-level
  constants are gone (i.e. nothing reads ``~/.beacon/credentials.json``
  unconditionally any more)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import auth  # noqa: E402
import profile as _profile  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_profile_factory(tmp_path: Path):
    """Build a Profile that points at ``tmp_path`` instead of real ~/.beacon.

    Returns a callable ``make(name="default", api_url=..., with_firebase=False)``
    so individual tests can shape the fake profile as needed.
    """

    def make(name: str = "default", *, api_url: str = "https://example.com",
             with_firebase: bool = False) -> _profile.Profile:
        pdir = tmp_path / "profiles" / name
        pdir.mkdir(parents=True, exist_ok=True)
        fc_path = pdir / "firebase_config.json"
        if with_firebase:
            fc_path.write_text(json.dumps({"apiKey": "fake-key-from-profile"}),
                               encoding="utf-8")
        return _profile.Profile(
            name=name,
            api_url=api_url,
            credentials_path=pdir / "credentials.json",
            firebase_config_path=fc_path if with_firebase else None,
            backend_type="gcp",
            extra={},
        )

    return make


@pytest.fixture
def patch_resolve_active(monkeypatch):
    """Monkey-patch profile.resolve_active_profile to return a given Profile."""

    def install(p: _profile.Profile):
        monkeypatch.setattr(
            _profile, "resolve_active_profile",
            lambda cli_arg=None, cwd=None: p,
        )
        return p

    return install


# ---------------------------------------------------------------------------
# _credentials_path
# ---------------------------------------------------------------------------

def test_credentials_path_returns_active_profile_credentials(
    fake_profile_factory, patch_resolve_active
):
    p = patch_resolve_active(fake_profile_factory(name="default"))
    assert auth._credentials_path() == p.credentials_path


def test_credentials_path_changes_when_profile_changes(
    fake_profile_factory, patch_resolve_active
):
    """Switching the active profile must switch the credentials path too.

    Catches the regression where a leftover module-level constant freezes
    the path at import time and ignores --profile / BEACON_PROFILE.
    """
    a = patch_resolve_active(fake_profile_factory(name="default"))
    first = auth._credentials_path()
    assert first == a.credentials_path

    b = patch_resolve_active(fake_profile_factory(name="other"))
    second = auth._credentials_path()
    assert second == b.credentials_path
    assert first != second


# ---------------------------------------------------------------------------
# _firebase_config_path precedence
# ---------------------------------------------------------------------------

def test_firebase_config_prefers_profile_local(
    fake_profile_factory, patch_resolve_active
):
    p = patch_resolve_active(
        fake_profile_factory(name="default", with_firebase=True)
    )
    resolved = auth._firebase_config_path()
    assert resolved == p.firebase_config_path
    # round-trip the content to be sure it's the profile's, not the bundled one
    parsed = json.loads(Path(resolved).read_text(encoding="utf-8"))
    assert parsed.get("apiKey") == "fake-key-from-profile"


def test_firebase_config_falls_back_to_bundled_when_profile_has_none(
    fake_profile_factory, patch_resolve_active
):
    patch_resolve_active(
        fake_profile_factory(name="default", with_firebase=False)
    )
    resolved = auth._firebase_config_path()
    # Either we get the bundled lib/firebase_config.json (when it's shipped
    # in this checkout) or None (when it isn't). The behavior under test is
    # that we did NOT return the profile-local missing path.
    if auth._BUNDLED_FIREBASE_CONFIG_PATH.exists():
        assert resolved == auth._BUNDLED_FIREBASE_CONFIG_PATH
    else:
        assert resolved is None


def test_firebase_config_returns_none_when_neither_exists(
    monkeypatch, fake_profile_factory, patch_resolve_active
):
    """Pure absence: both profile-local and bundled missing → None.

    Patches ``_BUNDLED_FIREBASE_CONFIG_PATH`` to a temp path to be sure
    even on dev checkouts where the bundled file might be present.
    """
    patch_resolve_active(
        fake_profile_factory(name="default", with_firebase=False)
    )
    monkeypatch.setattr(
        auth, "_BUNDLED_FIREBASE_CONFIG_PATH",
        Path("/nonexistent/path/to/firebase_config.json"),
    )
    assert auth._firebase_config_path() is None


# ---------------------------------------------------------------------------
# Module-level constants must not exist (= regression catch)
# ---------------------------------------------------------------------------

def test_legacy_module_constants_removed():
    """The old globals must be gone so callers can't accidentally bypass
    the profile resolver.

    SPEC ms-64 AC: ``BEACON_HOME 直読み箇所がゼロ``.
    """
    assert not hasattr(auth, "BEACON_HOME"), \
        "auth.BEACON_HOME must be removed — use _credentials_path().parent"
    assert not hasattr(auth, "CREDENTIALS_PATH"), \
        "auth.CREDENTIALS_PATH must be removed — use _credentials_path()"
    assert not hasattr(auth, "FIREBASE_CONFIG_PATH"), \
        "auth.FIREBASE_CONFIG_PATH must be removed — use _firebase_config_path()"


# ---------------------------------------------------------------------------
# BEACON_PROFILE env var integration (= the per-shell switch)
# ---------------------------------------------------------------------------

def test_beacon_profile_env_switches_credentials_location(tmp_path, monkeypatch):
    """End-to-end: setting BEACON_PROFILE=alt must change the cwd's
    credentials.json target without code knowing it had.

    Uses ``BEACON_HOME`` env var (honored by ``profile._beacon_home``) to
    redirect the real-world resolver into ``tmp_path`` so the test runs
    hermetic.
    """
    monkeypatch.setenv("BEACON_HOME", str(tmp_path))

    monkeypatch.setenv("BEACON_PROFILE", "default")
    default_path = auth._credentials_path()
    assert default_path == tmp_path / "profiles" / "default" / "credentials.json"

    monkeypatch.setenv("BEACON_PROFILE", "alt")
    alt_path = auth._credentials_path()
    assert alt_path == tmp_path / "profiles" / "alt" / "credentials.json"
    assert alt_path != default_path
