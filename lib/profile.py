"""Beacon profile resolution.

A profile is the unit of "which Beacon backend am I talking to right now"
(modeled after AWS CLI profiles and gcloud configurations).

Resolution order for profile name:
  1. --profile CLI arg
  2. BEACON_PROFILE env var
  3. cwd .beacon/cloud.json `profile` field
  4. "default" (reserved name)

Resolution order for api_url (within the selected profile):
  1. BEACON_API_URL env var       (developer debug override)
  2. cwd .beacon/cloud.json.api_url (per-project override, e.g. staging swap)
  3. ~/.beacon/profiles/<name>/profile.json.api_url (profile default)
  4. "https://beacon-ai.dev"      (final fallback)

Silent migration: when loading the "default" profile for the first time and
~/.beacon/credentials.json exists (legacy singleton layout), it is moved into
~/.beacon/profiles/default/credentials.json and the original is preserved as
~/.beacon/credentials.json.pre-profile-backup for manual rollback.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _beacon_home() -> Path:
    """Resolve BEACON_HOME (respects env var, like channel/bus.mjs)."""
    return Path(os.environ.get("BEACON_HOME") or (Path.home() / ".beacon"))


DEFAULT_PROFILE = "default"
DEFAULT_API_URL = "https://beacon-ai.dev"
LEGACY_BACKUP_SUFFIX = ".pre-profile-backup"
MIGRATION_MARKER_NAME = ".migrated_from_legacy"

# Profile names: lowercase alphanumeric + hyphens, 1-63 chars, must start
# with alphanumeric. Matches DNS label conventions, so profiles can later
# map to subdomains / k8s namespaces without surprises.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ProfileError(Exception):
    """Raised when a profile cannot be resolved or its name is invalid."""


@dataclass
class Profile:
    """Resolved Beacon backend identity.

    name: profile identifier (e.g. "default", "internal", "ga-aws")
    api_url: HTTPS endpoint the CLI/Skill/bus should talk to
    credentials_path: where OAuth refresh token lives for this profile
    firebase_config_path: OAuth Client ID file for the rare local-flow login;
                         None means "use Web Sign-In only"
    backend_type: reserved for future store-abstraction work (β). For now
                  "gcp" is assumed; readers should treat unknown values as gcp.
    extra: any other fields read from profile.json that resolver did not consume
    """

    name: str
    api_url: str
    credentials_path: Path
    firebase_config_path: Optional[Path] = None
    backend_type: str = "gcp"
    extra: dict = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.credentials_path.parent

    def credentials_exist(self) -> bool:
        return self.credentials_path.exists()


def validate_profile_name(name: str) -> None:
    """Raise ProfileError if name does not conform to the profile name grammar."""
    if not isinstance(name, str) or not _PROFILE_NAME_RE.match(name):
        raise ProfileError(
            f"Invalid profile name {name!r}: must be 1-63 chars, lowercase "
            f"alphanumeric with hyphens, starting with alphanumeric"
        )


def _read_cwd_cloud_json(cwd: Path) -> dict:
    """Return parsed .beacon/cloud.json under cwd, or {} if missing/unreadable."""
    cloud_json = cwd / ".beacon" / "cloud.json"
    if not cloud_json.exists():
        return {}
    try:
        with open(cloud_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_profile_name(
    cli_arg: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> str:
    """Resolve which profile name to use, in priority order.

    1. cli_arg (--profile <name>)
    2. BEACON_PROFILE env var
    3. cwd .beacon/cloud.json `profile` field
    4. DEFAULT_PROFILE
    """
    if cli_arg:
        validate_profile_name(cli_arg)
        return cli_arg

    env_val = os.environ.get("BEACON_PROFILE")
    if env_val:
        validate_profile_name(env_val)
        return env_val

    cwd = cwd if cwd is not None else Path.cwd()
    cloud = _read_cwd_cloud_json(cwd)
    profile = cloud.get("profile")
    if profile:
        validate_profile_name(profile)
        return profile

    return DEFAULT_PROFILE


def _profile_dir(name: str) -> Path:
    return _beacon_home() / "profiles" / name


def _legacy_credentials_path() -> Path:
    return _beacon_home() / "credentials.json"


def _maybe_silent_migrate_default() -> None:
    """Move legacy ~/.beacon/credentials.json into the default profile.

    Trigger conditions (all must hold):
      - we are about to load the `default` profile
      - ~/.beacon/profiles/default/ does NOT exist yet (= first time)
      - ~/.beacon/credentials.json exists (= legacy layout)

    Outcome:
      - profiles/default/credentials.json receives a copy
      - original is renamed to credentials.json.pre-profile-backup (rollback)
      - profiles/default/.migrated_from_legacy marks "do not re-run"

    All file ops are best-effort: any failure leaves the user able to
    re-run beacon auth login manually. We do NOT raise here, because the
    caller would still resolve to a valid (possibly empty) profile dir.
    """
    legacy = _legacy_credentials_path()
    if not legacy.exists():
        return

    target_dir = _profile_dir(DEFAULT_PROFILE)
    if target_dir.exists():
        return  # profile already initialized — do not touch legacy file

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "credentials.json"
        shutil.copy2(legacy, target)

        backup = legacy.with_suffix(legacy.suffix + LEGACY_BACKUP_SUFFIX)
        if not backup.exists():
            shutil.move(str(legacy), str(backup))

        marker = target_dir / MIGRATION_MARKER_NAME
        marker.write_text(
            f"migrated_at={int(time.time())}\nfrom={legacy}\n",
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: leave whatever partial state on disk, do not crash CLI.
        # User can re-run `beacon auth login --profile default` to recover.
        pass


def _resolve_api_url(profile_config: dict, cwd: Path) -> str:
    """Apply the api_url precedence chain. See module docstring for order.

    ms-64 e-1627: cwd cloud.json.profile が **default 以外** を指定している
    とき、その profile が「このディレクトリの帰属先」と宣言したとみなして
    profile.json.api_url を必ず採用する。cwd cloud.json.api_url の override
    は無視する (= profile と URL の組合せズレで認証経路が混乱するのを防ぐ
    構造防御)。

    profile == "default" のとき (= 旧 single-profile 時代の cloud.json
    フォーマット互換) は従来通り cwd cloud.json.api_url を尊重する。
    """
    env_url = os.environ.get("BEACON_API_URL")
    if env_url:
        return env_url.rstrip("/")

    cloud = _read_cwd_cloud_json(cwd)
    cloud_profile = cloud.get("profile")

    # profile を明示宣言した cloud.json で、profile == default 以外の場合は
    # profile.json.api_url を強制採用する (= cloud.json.api_url の override を
    # スキップ)。これで cd だけで自動切替が確実に動く。
    profile_is_explicit_non_default = (
        isinstance(cloud_profile, str)
        and cloud_profile
        and cloud_profile != DEFAULT_PROFILE
    )

    if not profile_is_explicit_non_default and cloud.get("api_url"):
        return str(cloud["api_url"]).rstrip("/")

    if profile_config.get("api_url"):
        return str(profile_config["api_url"]).rstrip("/")

    return DEFAULT_API_URL


def load_profile(name: str, cwd: Optional[Path] = None) -> Profile:
    """Load a profile by name.

    Triggers silent migration when loading `default` for the first time.
    Does NOT require credentials.json to exist — a freshly-resolved profile
    can have no credentials yet (the caller will surface an auth error).
    """
    validate_profile_name(name)
    cwd = cwd if cwd is not None else Path.cwd()

    if name == DEFAULT_PROFILE:
        _maybe_silent_migrate_default()

    pdir = _profile_dir(name)

    # Read profile.json (per-profile metadata: api_url, backend_type, extras).
    profile_config: dict = {}
    profile_json = pdir / "profile.json"
    if profile_json.exists():
        try:
            with open(profile_json, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                profile_config = loaded
        except (OSError, json.JSONDecodeError):
            profile_config = {}

    api_url = _resolve_api_url(profile_config, cwd)
    credentials_path = pdir / "credentials.json"
    firebase_config_path = pdir / "firebase_config.json"

    backend_type = str(profile_config.get("backend_type", "gcp"))
    extra = {
        k: v
        for k, v in profile_config.items()
        if k not in ("api_url", "backend_type")
    }

    return Profile(
        name=name,
        api_url=api_url,
        credentials_path=credentials_path,
        firebase_config_path=(
            firebase_config_path if firebase_config_path.exists() else None
        ),
        backend_type=backend_type,
        extra=extra,
    )


def resolve_active_profile(
    cli_arg: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Profile:
    """Top-level entry: resolve name, then load the corresponding Profile."""
    cwd = cwd if cwd is not None else Path.cwd()
    name = resolve_profile_name(cli_arg=cli_arg, cwd=cwd)
    return load_profile(name, cwd=cwd)


def list_profiles() -> list[str]:
    """Return profile names that have a directory on disk, sorted."""
    profiles_root = _beacon_home() / "profiles"
    if not profiles_root.exists():
        return []
    return sorted([p.name for p in profiles_root.iterdir() if p.is_dir()])


def list_logged_in_profiles() -> list[str]:
    """Return profile names that have a usable credentials.json on disk, sorted.

    A profile counts as "logged in" if either:
      - ~/.beacon/profiles/<name>/credentials.json exists, OR
      - name == DEFAULT_PROFILE and the legacy ~/.beacon/credentials.json
        exists (silent migration has not run yet but the user IS logged in)

    Used by cmd_init / first-cloud-push to decide whether to ask the user
    "which backend should this project use?". Single result = no prompt.
    """
    out: list[str] = []
    profiles_root = _beacon_home() / "profiles"
    if profiles_root.exists():
        for p in sorted(profiles_root.iterdir()):
            if p.is_dir() and (p / "credentials.json").exists():
                out.append(p.name)
    if DEFAULT_PROFILE not in out and _legacy_credentials_path().exists():
        out.append(DEFAULT_PROFILE)
    return sorted(out)


def prompt_choose_profile(
    candidates: list[str],
    *,
    input_fn=None,
    output_fn=print,
    default: Optional[str] = None,
) -> str:
    """Interactively pick a profile from candidates.

    Returns one of:
      - candidates[0] if len(candidates) == 1
      - DEFAULT_PROFILE if candidates is empty
      - the user-selected name otherwise (validated against candidates)

    Empty input picks `default` (or candidates[0] if default not given).
    Numeric input picks by 1-based index. Invalid input falls back to default.

    input_fn / output_fn are injectable for tests; default uses real stdin / print.
    """
    if not candidates:
        return DEFAULT_PROFILE
    if len(candidates) == 1:
        return candidates[0]

    default = default if default in candidates else candidates[0]

    if input_fn is None:
        input_fn = input

    output_fn("Multiple Beacon profiles are logged in:")
    for i, name in enumerate(candidates, 1):
        marker = " (default)" if name == default else ""
        output_fn(f"  {i}. {name}{marker}")
    try:
        raw = input_fn(f"Which profile should this project use? [{default}]: ")
    except EOFError:
        raw = ""
    raw = (raw or "").strip()
    if not raw:
        return default
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    if raw in candidates:
        return raw
    output_fn(f"Invalid choice {raw!r}, using {default}")
    return default
