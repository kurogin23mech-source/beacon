"""Cross-language probe test for Beacon profile resolution.

Pins the invariant that lib/profile.py (Python) and channel/profile-resolver.mjs
(Node) return the same resolution for the same (profile state, cwd, env)
inputs. This is the merge gate for ms-64 e-1459 — without this test, the
two implementations can silently drift the way bus.mjs drifted from
lib/auth.py before silent migration (incident e-1579).

The test pattern mirrors ms-54 e-1306 (CLI ↔ Skill cross-language probe).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_RESOLVER = REPO_ROOT / "channel" / "profile-resolver.mjs"


@pytest.fixture
def beacon_home(tmp_path: Path) -> Path:
    home = tmp_path / ".beacon"
    home.mkdir()
    return home


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "workdir"
    cwd.mkdir()
    return cwd


def _write_profile(beacon_home: Path, name: str, *, profile_json: dict | None = None,
                   credentials_json: dict | None = None) -> Path:
    """Pre-populate a profile directory under beacon_home/profiles/<name>/."""
    pdir = beacon_home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    if profile_json is not None:
        (pdir / "profile.json").write_text(json.dumps(profile_json), encoding="utf-8")
    if credentials_json is not None:
        (pdir / "credentials.json").write_text(json.dumps(credentials_json), encoding="utf-8")
    return pdir


def _write_cwd_cloud(cwd: Path, cloud: dict) -> None:
    beacon_dir = cwd / ".beacon"
    beacon_dir.mkdir(exist_ok=True)
    (beacon_dir / "cloud.json").write_text(json.dumps(cloud), encoding="utf-8")


def _run_python_resolver(*, beacon_home: Path, cwd: Path, env_overrides: dict | None = None) -> dict:
    """Invoke lib/profile.resolve_active_profile in a subprocess with a clean env."""
    env = {k: v for k, v in os.environ.items() if k not in {"BEACON_PROFILE", "BEACON_API_URL", "BEACON_AUTH_TOKEN"}}
    env["BEACON_HOME"] = str(beacon_home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if env_overrides:
        env.update(env_overrides)
    script = (
        "import json, sys\n"
        "from lib.profile import resolve_active_profile\n"
        "p = resolve_active_profile()\n"
        "print(json.dumps({\n"
        "    'name': p.name,\n"
        "    'apiUrl': p.api_url,\n"
        "    'credentialsPath': str(p.credentials_path),\n"
        "    'backendType': p.backend_type,\n"
        "}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"python resolver failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _run_node_resolver(*, beacon_home: Path, cwd: Path, env_overrides: dict | None = None) -> dict:
    """Invoke channel/profile-resolver.mjs resolveActiveProfile via node -e."""
    env = {k: v for k, v in os.environ.items() if k not in {"BEACON_PROFILE", "BEACON_API_URL", "BEACON_AUTH_TOKEN"}}
    env["BEACON_HOME"] = str(beacon_home)
    if env_overrides:
        env.update(env_overrides)
    # Use file:// import URL so Node's ESM loader accepts an absolute path.
    resolver_url = NODE_RESOLVER.resolve().as_uri()
    script = (
        f"import('{resolver_url}').then(({{resolveActiveProfile}}) => {{\n"
        "  const p = resolveActiveProfile()\n"
        "  console.log(JSON.stringify({\n"
        "    name: p.name,\n"
        "    apiUrl: p.apiUrl,\n"
        "    credentialsPath: p.credentialsPath,\n"
        "    backendType: p.backendType,\n"
        "  }))\n"
        "}).catch(e => { console.error(e); process.exit(1) })\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"node resolver failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_agree(*, beacon_home: Path, cwd: Path, env_overrides: dict | None = None) -> dict:
    """Run both resolvers, assert they agree, return the shared result."""
    py_result = _run_python_resolver(beacon_home=beacon_home, cwd=cwd, env_overrides=env_overrides)
    js_result = _run_node_resolver(beacon_home=beacon_home, cwd=cwd, env_overrides=env_overrides)
    assert py_result == js_result, (
        f"Python and Node resolvers disagree:\n"
        f"  python: {json.dumps(py_result, indent=2)}\n"
        f"  node:   {json.dumps(js_result, indent=2)}"
    )
    return py_result


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_default_profile_no_env_no_cloud_json(beacon_home: Path, cwd: Path) -> None:
    """Bare baseline: no env / no cwd cloud.json / no profile dir → 'default'."""
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["name"] == "default"
    assert result["credentialsPath"] == str(beacon_home / "profiles" / "default" / "credentials.json")
    assert result["apiUrl"] == "https://beacon-ai.dev"


def test_env_beacon_profile_overrides(beacon_home: Path, cwd: Path) -> None:
    """BEACON_PROFILE env beats cloud.json profile field."""
    _write_cwd_cloud(cwd, {"profile": "from-cloud-json"})
    result = _assert_agree(
        beacon_home=beacon_home, cwd=cwd,
        env_overrides={"BEACON_PROFILE": "from-env"},
    )
    assert result["name"] == "from-env"


def test_cwd_cloud_json_profile_field(beacon_home: Path, cwd: Path) -> None:
    """cwd .beacon/cloud.json.profile is used when no env override."""
    _write_cwd_cloud(cwd, {"profile": "ga-aws"})
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["name"] == "ga-aws"
    assert result["credentialsPath"] == str(beacon_home / "profiles" / "ga-aws" / "credentials.json")


def test_api_url_env_wins(beacon_home: Path, cwd: Path) -> None:
    """BEACON_API_URL env wins over cwd cloud.json.api_url and profile.json.api_url."""
    _write_cwd_cloud(cwd, {"api_url": "https://from-cloud-json.example.com"})
    _write_profile(beacon_home, "default", profile_json={"api_url": "https://from-profile-json.example.com"})
    result = _assert_agree(
        beacon_home=beacon_home, cwd=cwd,
        env_overrides={"BEACON_API_URL": "https://from-env.example.com/"},
    )
    assert result["apiUrl"] == "https://from-env.example.com"  # trailing slash stripped


def test_api_url_cwd_cloud_wins_over_profile_json(beacon_home: Path, cwd: Path) -> None:
    """cwd cloud.json.api_url wins over profile.json.api_url when no env."""
    _write_cwd_cloud(cwd, {"api_url": "https://from-cloud-json.example.com"})
    _write_profile(beacon_home, "default", profile_json={"api_url": "https://from-profile-json.example.com"})
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["apiUrl"] == "https://from-cloud-json.example.com"


def test_api_url_profile_json_used_when_no_cloud_no_env(beacon_home: Path, cwd: Path) -> None:
    """profile.json.api_url is the last per-profile setting before the global default."""
    _write_profile(beacon_home, "default", profile_json={"api_url": "https://from-profile-json.example.com"})
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["apiUrl"] == "https://from-profile-json.example.com"


def test_named_profile_path(beacon_home: Path, cwd: Path) -> None:
    """Non-default profile resolves credentials under profiles/<name>/."""
    _write_cwd_cloud(cwd, {"profile": "staging"})
    _write_profile(beacon_home, "staging", credentials_json={"token": "fake-staging-token"})
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["credentialsPath"] == str(beacon_home / "profiles" / "staging" / "credentials.json")


def test_backend_type_default_is_gcp(beacon_home: Path, cwd: Path) -> None:
    """When profile.json is absent, backend_type defaults to 'gcp'."""
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["backendType"] == "gcp"


def test_backend_type_read_from_profile_json(beacon_home: Path, cwd: Path) -> None:
    """profile.json.backend_type is propagated."""
    _write_cwd_cloud(cwd, {"profile": "ga-aws"})
    _write_profile(beacon_home, "ga-aws", profile_json={"backend_type": "aws"})
    result = _assert_agree(beacon_home=beacon_home, cwd=cwd)
    assert result["backendType"] == "aws"


def test_invalid_profile_name_in_env_rejected_by_both(beacon_home: Path, cwd: Path) -> None:
    """Invalid profile name (uppercase) must raise on both sides."""
    env = {k: v for k, v in os.environ.items() if k not in {"BEACON_PROFILE", "BEACON_API_URL", "BEACON_AUTH_TOKEN"}}
    env["BEACON_HOME"] = str(beacon_home)
    env["BEACON_PROFILE"] = "Invalid-Uppercase"
    env["PYTHONPATH"] = str(REPO_ROOT)

    py_proc = subprocess.run(
        [sys.executable, "-c", "from lib.profile import resolve_active_profile; resolve_active_profile()"],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert py_proc.returncode != 0
    assert "Invalid profile name" in py_proc.stderr

    resolver_url = NODE_RESOLVER.resolve().as_uri()
    js_script = (
        f"import('{resolver_url}').then(({{resolveActiveProfile}}) => {{\n"
        "  resolveActiveProfile()\n"
        "}).catch(e => { console.error(e.message || e); process.exit(1) })\n"
    )
    js_proc = subprocess.run(
        ["node", "--input-type=module", "-e", js_script],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert js_proc.returncode != 0
    assert "Invalid profile name" in js_proc.stderr


# ---------------------------------------------------------------------------
# loadToken: legacy fallback parity (default profile only)
# ---------------------------------------------------------------------------


def _read_token_python(*, beacon_home: Path, cwd: Path, env_overrides: dict | None = None) -> str:
    """Read token via Python lib/auth.py _read_token (= what CLI uses)."""
    env = {k: v for k, v in os.environ.items() if k not in {"BEACON_PROFILE", "BEACON_API_URL", "BEACON_AUTH_TOKEN"}}
    env["BEACON_HOME"] = str(beacon_home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if env_overrides:
        env.update(env_overrides)
    script = (
        "from lib.profile import resolve_active_profile\n"
        "import json, os\n"
        "p = resolve_active_profile()\n"
        "# Mirror Node loadToken: env > profile credentials > legacy fallback (default only).\n"
        "if os.environ.get('BEACON_AUTH_TOKEN'):\n"
        "    print(os.environ['BEACON_AUTH_TOKEN']); raise SystemExit\n"
        "try:\n"
        "    c = json.load(open(p.credentials_path))\n"
        "    tok = c.get('token') or c.get('id_token') or ''\n"
        "    if tok:\n"
        "        print(tok); raise SystemExit\n"
        "except (FileNotFoundError, OSError):\n"
        "    pass\n"
        "if p.name == 'default':\n"
        "    legacy = os.path.join(os.environ.get('BEACON_HOME'), 'credentials.json')\n"
        "    try:\n"
        "        c = json.load(open(legacy))\n"
        "        print(c.get('token') or c.get('id_token') or '')\n"
        "    except (FileNotFoundError, OSError):\n"
        "        print('')\n"
        "else:\n"
        "    print('')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"python loadToken failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def _read_token_node(*, beacon_home: Path, cwd: Path, env_overrides: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"BEACON_PROFILE", "BEACON_API_URL", "BEACON_AUTH_TOKEN"}}
    env["BEACON_HOME"] = str(beacon_home)
    if env_overrides:
        env.update(env_overrides)
    resolver_url = NODE_RESOLVER.resolve().as_uri()
    script = (
        f"import('{resolver_url}').then(({{resolveActiveProfile, loadToken}}) => {{\n"
        "  const p = resolveActiveProfile()\n"
        "  console.log(loadToken(p))\n"
        "}).catch(e => { console.error(e); process.exit(1) })\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"node loadToken failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def test_load_token_from_profile_credentials_default(beacon_home: Path, cwd: Path) -> None:
    """Both sides read token from profiles/default/credentials.json when present."""
    _write_profile(beacon_home, "default", credentials_json={"token": "real-default-token"})
    assert _read_token_python(beacon_home=beacon_home, cwd=cwd) == "real-default-token"
    assert _read_token_node(beacon_home=beacon_home, cwd=cwd) == "real-default-token"


def test_load_token_legacy_fallback_default_only(beacon_home: Path, cwd: Path) -> None:
    """Default profile with no profile dir but legacy credentials → both fall back."""
    (beacon_home / "credentials.json").write_text(
        json.dumps({"token": "legacy-token"}), encoding="utf-8"
    )
    assert _read_token_python(beacon_home=beacon_home, cwd=cwd) == "legacy-token"
    assert _read_token_node(beacon_home=beacon_home, cwd=cwd) == "legacy-token"


def test_load_token_named_profile_no_legacy_fallback(beacon_home: Path, cwd: Path) -> None:
    """A named profile that has no credentials.json must NOT fall back to legacy."""
    _write_cwd_cloud(cwd, {"profile": "staging"})
    (beacon_home / "credentials.json").write_text(
        json.dumps({"token": "legacy-token"}), encoding="utf-8"
    )
    # Both sides should return empty, not 'legacy-token'.
    assert _read_token_python(beacon_home=beacon_home, cwd=cwd) == ""
    assert _read_token_node(beacon_home=beacon_home, cwd=cwd) == ""


def test_load_token_env_var_wins(beacon_home: Path, cwd: Path) -> None:
    """BEACON_AUTH_TOKEN env beats both the profile dir and the legacy fallback."""
    _write_profile(beacon_home, "default", credentials_json={"token": "profile-token"})
    (beacon_home / "credentials.json").write_text(
        json.dumps({"token": "legacy-token"}), encoding="utf-8"
    )
    env = {"BEACON_AUTH_TOKEN": "env-token"}
    assert _read_token_python(beacon_home=beacon_home, cwd=cwd, env_overrides=env) == "env-token"
    assert _read_token_node(beacon_home=beacon_home, cwd=cwd, env_overrides=env) == "env-token"
