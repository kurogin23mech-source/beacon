"""Unit tests for lib/profile.py.

Covers profile name resolution, api_url precedence, silent migration of the
legacy ~/.beacon/credentials.json layout, and listing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import profile as profile_mod  # noqa: E402
from profile import (  # noqa: E402
    DEFAULT_API_URL,
    DEFAULT_PROFILE,
    LEGACY_BACKUP_SUFFIX,
    MIGRATION_MARKER_NAME,
    Profile,
    ProfileError,
    list_profiles,
    load_profile,
    resolve_active_profile,
    resolve_profile_name,
    validate_profile_name,
)


@pytest.fixture(autouse=True)
def _isolated_beacon_home(tmp_path, monkeypatch):
    """Each test runs against a fresh ~/.beacon under tmp_path.

    Also clears BEACON_PROFILE / BEACON_API_URL so host env cannot leak in.
    """
    home = tmp_path / "beacon-home"
    home.mkdir()
    monkeypatch.setenv("BEACON_HOME", str(home))
    monkeypatch.delenv("BEACON_PROFILE", raising=False)
    monkeypatch.delenv("BEACON_API_URL", raising=False)
    yield home


@pytest.fixture
def _isolated_cwd(tmp_path, monkeypatch):
    """A cwd that has no .beacon/cloud.json by default."""
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    return cwd


def _write_cloud_json(cwd: Path, **fields) -> None:
    p = cwd / ".beacon" / "cloud.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(fields, f)


# --------------------------------------------------------------------------
# validate_profile_name
# --------------------------------------------------------------------------


class TestValidateProfileName:
    @pytest.mark.parametrize("name", ["default", "internal", "ga-aws", "a", "x1", "test-123"])
    def test_valid_names(self, name):
        validate_profile_name(name)  # must not raise

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "-leading-hyphen",
            "UPPER",
            "with_underscore",
            "with space",
            "with.dot",
            "x" * 64,  # 64 chars > 63
            None,
            123,
        ],
    )
    def test_invalid_names(self, name):
        with pytest.raises(ProfileError):
            validate_profile_name(name)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# resolve_profile_name: priority order
# --------------------------------------------------------------------------


class TestResolveProfileName:
    def test_cli_arg_wins_over_everything(self, _isolated_cwd, monkeypatch):
        monkeypatch.setenv("BEACON_PROFILE", "from-env")
        _write_cloud_json(_isolated_cwd, profile="from-cloud-json")
        assert resolve_profile_name(cli_arg="from-cli") == "from-cli"

    def test_env_wins_over_cloud_json_and_default(self, _isolated_cwd, monkeypatch):
        monkeypatch.setenv("BEACON_PROFILE", "from-env")
        _write_cloud_json(_isolated_cwd, profile="from-cloud-json")
        assert resolve_profile_name() == "from-env"

    def test_cloud_json_wins_over_default(self, _isolated_cwd):
        _write_cloud_json(_isolated_cwd, profile="from-cloud-json")
        assert resolve_profile_name() == "from-cloud-json"

    def test_default_when_nothing_set(self, _isolated_cwd):
        assert resolve_profile_name() == DEFAULT_PROFILE

    def test_invalid_cli_arg_raises(self, _isolated_cwd):
        with pytest.raises(ProfileError):
            resolve_profile_name(cli_arg="UPPER")

    def test_invalid_env_raises(self, _isolated_cwd, monkeypatch):
        monkeypatch.setenv("BEACON_PROFILE", "with space")
        with pytest.raises(ProfileError):
            resolve_profile_name()

    def test_invalid_cloud_json_value_raises(self, _isolated_cwd):
        _write_cloud_json(_isolated_cwd, profile="UPPER")
        with pytest.raises(ProfileError):
            resolve_profile_name()

    def test_missing_cloud_json_falls_through(self, tmp_path, monkeypatch):
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        assert resolve_profile_name() == DEFAULT_PROFILE

    def test_malformed_cloud_json_falls_through(self, _isolated_cwd):
        (_isolated_cwd / ".beacon" / "cloud.json").write_text("{ broken")
        assert resolve_profile_name() == DEFAULT_PROFILE


# --------------------------------------------------------------------------
# load_profile + api_url precedence
# --------------------------------------------------------------------------


class TestLoadProfileApiUrl:
    def test_default_api_url_when_nothing_set(self, _isolated_cwd):
        p = load_profile(DEFAULT_PROFILE)
        assert p.api_url == DEFAULT_API_URL
        assert p.name == DEFAULT_PROFILE

    def test_profile_json_api_url(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "internal"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(json.dumps({"api_url": "https://internal.example"}))
        p = load_profile("internal")
        assert p.api_url == "https://internal.example"

    def test_cwd_cloud_json_overrides_profile_json(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "internal"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(json.dumps({"api_url": "https://internal.example"}))
        _write_cloud_json(_isolated_cwd, api_url="https://staging.example")
        p = load_profile("internal")
        assert p.api_url == "https://staging.example"

    def test_env_overrides_everything(self, _isolated_beacon_home, _isolated_cwd, monkeypatch):
        pdir = _isolated_beacon_home / "profiles" / "internal"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(json.dumps({"api_url": "https://internal.example"}))
        _write_cloud_json(_isolated_cwd, api_url="https://staging.example")
        monkeypatch.setenv("BEACON_API_URL", "https://dev.example")
        p = load_profile("internal")
        assert p.api_url == "https://dev.example"

    def test_trailing_slash_stripped(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "x"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(json.dumps({"api_url": "https://x.example/"}))
        assert load_profile("x").api_url == "https://x.example"

    def test_malformed_profile_json_falls_back(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "broken"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text("{ not json")
        # Should fall through to DEFAULT_API_URL without raising
        assert load_profile("broken").api_url == DEFAULT_API_URL


# --------------------------------------------------------------------------
# load_profile + backend_type + extra + firebase_config
# --------------------------------------------------------------------------


class TestLoadProfileMeta:
    def test_backend_type_default_gcp(self, _isolated_cwd):
        assert load_profile(DEFAULT_PROFILE).backend_type == "gcp"

    def test_backend_type_from_profile_json(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "future"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(
            json.dumps({"backend_type": "self-hosted-pg"})
        )
        assert load_profile("future").backend_type == "self-hosted-pg"

    def test_extra_fields_preserved(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "x"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(
            json.dumps({
                "api_url": "https://x.example",
                "backend_type": "gcp",
                "display_name": "X corp",
                "created_at": "2026-06-11T00:00:00Z",
            })
        )
        p = load_profile("x")
        assert p.extra == {
            "display_name": "X corp",
            "created_at": "2026-06-11T00:00:00Z",
        }

    def test_firebase_config_none_when_absent(self, _isolated_cwd):
        p = load_profile(DEFAULT_PROFILE)
        assert p.firebase_config_path is None

    def test_firebase_config_set_when_present(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "ga"
        pdir.mkdir(parents=True)
        (pdir / "firebase_config.json").write_text("{}")
        p = load_profile("ga")
        assert p.firebase_config_path == pdir / "firebase_config.json"


# --------------------------------------------------------------------------
# Silent migration of legacy ~/.beacon/credentials.json
# --------------------------------------------------------------------------


class TestSilentMigration:
    def test_legacy_moved_into_default_profile(self, _isolated_beacon_home, _isolated_cwd):
        legacy = _isolated_beacon_home / "credentials.json"
        legacy.write_text(json.dumps({"token": "legacy-token"}))

        p = load_profile(DEFAULT_PROFILE)

        new_creds = _isolated_beacon_home / "profiles" / "default" / "credentials.json"
        assert new_creds.exists(), "credentials should be copied to default profile dir"
        assert json.loads(new_creds.read_text())["token"] == "legacy-token"
        assert p.credentials_path == new_creds
        assert p.credentials_exist()

    def test_legacy_renamed_to_backup(self, _isolated_beacon_home, _isolated_cwd):
        legacy = _isolated_beacon_home / "credentials.json"
        legacy.write_text(json.dumps({"token": "legacy-token"}))

        load_profile(DEFAULT_PROFILE)

        backup = _isolated_beacon_home / ("credentials.json" + LEGACY_BACKUP_SUFFIX)
        assert backup.exists(), "original legacy file should be preserved as backup"
        assert not legacy.exists(), "legacy file should be moved, not duplicated"
        assert json.loads(backup.read_text())["token"] == "legacy-token"

    def test_migration_marker_written(self, _isolated_beacon_home, _isolated_cwd):
        legacy = _isolated_beacon_home / "credentials.json"
        legacy.write_text(json.dumps({"token": "x"}))
        load_profile(DEFAULT_PROFILE)
        marker = _isolated_beacon_home / "profiles" / "default" / MIGRATION_MARKER_NAME
        assert marker.exists()

    def test_migration_does_not_run_when_default_dir_already_exists(
        self, _isolated_beacon_home, _isolated_cwd
    ):
        # Pre-existing default profile with its own credentials
        default_dir = _isolated_beacon_home / "profiles" / "default"
        default_dir.mkdir(parents=True)
        (default_dir / "credentials.json").write_text(json.dumps({"token": "new-token"}))

        # Legacy file also exists but should NOT be touched
        legacy = _isolated_beacon_home / "credentials.json"
        legacy.write_text(json.dumps({"token": "legacy-token"}))

        load_profile(DEFAULT_PROFILE)

        # default profile credentials must remain the "new" ones
        assert (
            json.loads((default_dir / "credentials.json").read_text())["token"]
            == "new-token"
        )
        # legacy file untouched
        assert legacy.exists()
        assert json.loads(legacy.read_text())["token"] == "legacy-token"

    def test_migration_only_triggers_for_default_profile(
        self, _isolated_beacon_home, _isolated_cwd
    ):
        legacy = _isolated_beacon_home / "credentials.json"
        legacy.write_text(json.dumps({"token": "legacy"}))

        # Loading a non-default profile must not consume the legacy file
        load_profile("internal")

        assert legacy.exists(), "non-default profile load should not touch legacy file"
        assert not (
            _isolated_beacon_home / "profiles" / "default"
        ).exists(), "default profile dir should not be created when loading 'internal'"

    def test_no_migration_when_no_legacy_file(self, _isolated_beacon_home, _isolated_cwd):
        # Fresh user with no legacy credentials.json
        p = load_profile(DEFAULT_PROFILE)
        # Profile loads cleanly without credentials yet
        assert not p.credentials_exist()
        # No backup file created
        backup = _isolated_beacon_home / ("credentials.json" + LEGACY_BACKUP_SUFFIX)
        assert not backup.exists()


# --------------------------------------------------------------------------
# resolve_active_profile (top-level integration)
# --------------------------------------------------------------------------


class TestResolveActiveProfile:
    def test_full_chain_default(self, _isolated_cwd):
        p = resolve_active_profile()
        assert p.name == DEFAULT_PROFILE
        assert p.api_url == DEFAULT_API_URL

    def test_cli_arg_drives_load(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "internal"
        pdir.mkdir(parents=True)
        (pdir / "profile.json").write_text(
            json.dumps({"api_url": "https://internal.example"})
        )
        p = resolve_active_profile(cli_arg="internal")
        assert p.name == "internal"
        assert p.api_url == "https://internal.example"

    def test_cwd_drives_load_when_no_cli_arg(self, _isolated_beacon_home, _isolated_cwd):
        pdir = _isolated_beacon_home / "profiles" / "from-cloud-json"
        pdir.mkdir(parents=True)
        _write_cloud_json(_isolated_cwd, profile="from-cloud-json")
        p = resolve_active_profile()
        assert p.name == "from-cloud-json"


# --------------------------------------------------------------------------
# list_profiles
# --------------------------------------------------------------------------


class TestListProfiles:
    def test_empty_when_no_profiles_dir(self, _isolated_beacon_home):
        assert list_profiles() == []

    def test_returns_sorted_dir_names(self, _isolated_beacon_home):
        for name in ["zeta", "alpha", "internal"]:
            (_isolated_beacon_home / "profiles" / name).mkdir(parents=True)
        assert list_profiles() == ["alpha", "internal", "zeta"]

    def test_skips_files(self, _isolated_beacon_home):
        profiles_root = _isolated_beacon_home / "profiles"
        profiles_root.mkdir()
        (profiles_root / "default").mkdir()
        (profiles_root / "stray-file.txt").write_text("noise")
        assert list_profiles() == ["default"]
