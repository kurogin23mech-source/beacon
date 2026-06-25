"""Tests for credentials.json identity fallback (ms-61 / e-2132).

Background — ms-84 dogfood (2026-06-19) で観測された病理:
- fork session (= 新 terminal で立てた bclaude) には親 session の env が
  継承されない。 ``BEACON_USER_EMAIL`` が未設定だと ``beacon trek create``
  / ``join`` / ``dm send`` 等の identity-bound CLI が hard error で停止する。
- credentials.json には login 済 email + JWT (= sub claim) があるのに、
  CLI 側で読まずに env を要求する設計。

構造解 (= ms-61 / e-2132): ``_resolve_creator_identity`` に credentials.json
auto-read fallback を追加。 env override は最優先のまま (= test / CI /
multi-account 用に override 経路は残す)。

このテストは:
  * env 未設定時に credentials.json から auto-read される経路を検証
  * env override が credentials を上書きすることを検証 (= fixture / multi-acc)
  * JWT 2 形式 (= bcli prefix / 標準 3-segment) 両方を decode できる
  * credentials.json 不在 / malformed / 空 token のいずれでも crash しない
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


def _encode_payload(payload: dict) -> str:
    """Encode a dict as URL-safe base64 (= JWT payload format)。"""
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _bcli_token(sub: str, email: str) -> str:
    """Construct a 'bcli.<payload>.<sig>' token (= Beacon CLI long-lived)。"""
    payload_b64 = _encode_payload({"sub": sub, "email": email, "exp": 9999999999})
    return f"bcli.{payload_b64}.dummy_sig_hex"


def _jwt_token(sub: str, email: str) -> str:
    """Construct a standard 3-segment JWT '<header>.<payload>.<sig>'。"""
    header_b64 = _encode_payload({"alg": "RS256", "typ": "JWT"})
    payload_b64 = _encode_payload({"sub": sub, "email": email, "exp": 9999999999})
    return f"{header_b64}.{payload_b64}.dummy_sig"


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ~/.beacon at a tmp dir via BEACON_HOME (= profile.py reads it).

    Don't touch HOME — `Path.home()` caching across imports and OS-specific
    expansion makes it fragile. BEACON_HOME is the documented override for
    profile/credentials resolution (lib/profile.py:_beacon_home).
    """
    fake_beacon = tmp_path / "beacon-home"
    fake_beacon.mkdir()
    monkeypatch.setenv("BEACON_HOME", str(fake_beacon))
    # Clear identity env vars so we exercise the fallback path.
    for k in ("BEACON_USER_ID", "BEACON_USER_EMAIL", "BEACON_SESSION_ID",
              "BEACON_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    return fake_beacon


def _write_credentials(beacon_home: Path, profile: str, token: str, email: str) -> None:
    """Write credentials.json under <BEACON_HOME>/profiles/<profile>/。"""
    profile_dir = beacon_home / "profiles" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "credentials.json").write_text(json.dumps({
        "token": token,
        "email": email,
        "web_auth": True,
    }))


def _write_legacy_credentials(beacon_home: Path, token: str, email: str) -> None:
    """Pre-migration <BEACON_HOME>/credentials.json singleton location."""
    beacon_home.mkdir(parents=True, exist_ok=True)
    (beacon_home / "credentials.json").write_text(json.dumps({
        "token": token,
        "email": email,
        "web_auth": True,
    }))


def test_credentials_fallback_reads_email_and_user_id_from_bcli_token(isolated_home):
    """ms-61 / e-2132: env 未設定で credentials.json から email + user_id 補完。"""
    _write_credentials(
        isolated_home, "default",
        token=_bcli_token("uid-fork-1", "fork@example.com"),
        email="fork@example.com",
    )
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert uid == "uid-fork-1"
    assert email == "fork@example.com"


def test_credentials_fallback_reads_standard_jwt(isolated_home):
    """3-segment JWT (= Cognito / Google id_token) も decode できる。"""
    _write_credentials(
        isolated_home, "default",
        token=_jwt_token("google-sub-12345", "google@example.com"),
        email="google@example.com",
    )
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert uid == "google-sub-12345"
    assert email == "google@example.com"


def test_credentials_fallback_falls_back_to_legacy_path(isolated_home):
    """profile 不在時に legacy ~/.beacon/credentials.json が読まれる。"""
    _write_legacy_credentials(
        isolated_home,
        token=_bcli_token("legacy-uid", "legacy@example.com"),
        email="legacy@example.com",
    )
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert uid == "legacy-uid"
    assert email == "legacy@example.com"


def test_credentials_fallback_returns_empty_when_nothing_exists(isolated_home):
    """credentials.json 不在 → ("", "")、 crash しない。"""
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert uid == ""
    assert email == ""


def test_credentials_fallback_handles_malformed_json(isolated_home):
    """credentials.json の中身が JSON でも何でも crash しない。"""
    profile_dir = isolated_home / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "credentials.json").write_text("not valid json")
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert uid == ""
    assert email == ""


def test_credentials_fallback_handles_empty_token(isolated_home):
    """token が空 (= email のみ login) → email は読めるが user_id は空。"""
    # isolated_home is the fake BEACON_HOME, not the OS home.
    profile_dir = isolated_home / "profiles" / "default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "credentials.json").write_text(json.dumps({
        "email": "noauth@example.com",
        "token": "",
    }))
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email = commands._read_credentials_for_identity()
    assert email == "noauth@example.com"
    assert uid == ""


# ---------------------------------------------------------------------------
# _resolve_creator_identity end-to-end behavior
# ---------------------------------------------------------------------------

def test_resolve_uses_env_when_both_set(isolated_home, monkeypatch):
    """env が両方 set されていれば env が credentials を完全に上書き。"""
    _write_credentials(
        isolated_home, "default",
        token=_bcli_token("creds-uid", "creds@example.com"),
        email="creds@example.com",
    )
    monkeypatch.setenv("BEACON_USER_ID", "env-uid")
    monkeypatch.setenv("BEACON_USER_EMAIL", "env@example.com")
    monkeypatch.setenv("BEACON_SESSION_ID", "sv-env-1")
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email, sid = commands._resolve_creator_identity()
    assert uid == "env-uid"
    assert email == "env@example.com"
    assert sid == "sv-env-1"


def test_resolve_partial_env_combines_with_credentials(isolated_home, monkeypatch):
    """env で email だけ override → user_id は credentials から (= multi-account)。"""
    _write_credentials(
        isolated_home, "default",
        token=_bcli_token("creds-uid", "creds@example.com"),
        email="creds@example.com",
    )
    monkeypatch.setenv("BEACON_USER_EMAIL", "override@example.com")
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email, _sid = commands._resolve_creator_identity()
    assert uid == "creds-uid"        # ← credentials 由来
    assert email == "override@example.com"  # ← env override


def test_resolve_falls_back_to_whoami_when_no_env_and_no_credentials(isolated_home):
    """env もなく credentials もない → user_id は whoami fallback。email は空。"""
    import importlib
    # ms-95 e-2441: only reload `commands` here. The original code also
    # reloaded `profile`, but profile.py has no mutable module-level state to
    # reset — and the reload creates a NEW `ProfileError` class object inside
    # the same module, breaking adjacent tests (test_profile.py) that captured
    # the OLD class via `from profile import ProfileError`. After the reload,
    # validate_profile_name raises NEW ProfileError, but the test's
    # pytest.raises(OLD ProfileError) fails to match (isinstance check on
    # distinct class objects). commands module is preserved here because its
    # functions cache via closures and need a fresh module to honor HOME
    # changes; commands does not export classes used by adjacent tests.
    for modname in ("commands",):
        if modname in sys.modules:
            importlib.reload(sys.modules[modname])
    import commands
    uid, email, sid = commands._resolve_creator_identity()
    # whoami は OS user (= 何かしらの string)、 空ではない
    assert uid != ""
    assert email == ""
    assert sid == ""
