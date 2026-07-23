"""ms-120 / e-3907 (open 3役 + noun-mutate sub-parts).

The base commit made `status` read-only and moved state changes to named intent
verbs. This covers the two deferred sub-parts:

  * `open` was a command verb in three lifecycle roles (create an Operation /
    the open state value / activate-target). `operation open` is now a
    deprecated alias of `operation create --status open`, and `incident open`
    an alias of the canonical `incident add` — so `open` names only a STATE.
  * noun-mutate: `account contact <acc> <name>` (a noun that creates) gains the
    canonical verb form `account contact add`; the bare form stays as an alias.
    (`summary` was also flagged but is already a loud-deprecated no-op — nothing
    to verbify — so it is intentionally left unchanged.)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
BASH = shutil.which("bash")
sys.path.insert(0, str(ROOT))
from beacon_cli import dispatch, main as main_mod  # noqa: E402


# --- bash alias / nudge behavior -------------------------------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({
            "name": "t", "milestones": [],
            "accounts": [{"id": "acc-1", "name": "A", "contacts": []}],
            "operations": [{"id": "op-1", "title": "o", "status": "open",
                            "entries": []}],
        }), encoding="utf-8")
    return tmp_path


def _run(proj, *args):
    return subprocess.run([BASH, str(BIN), *args], cwd=proj,
                          capture_output=True, text=True)


@pytestmark_bash
def test_operation_open_is_deprecated_alias_with_nudge(proj):
    r = _run(proj, "operation", "open", "watch svc")
    assert "operation create --status open" in r.stderr
    # It still creates (alias preserved): the op count grows.
    data = json.loads((proj / ".beacon" / "project.json").read_text())
    assert any(o.get("title") == "watch svc" for o in data["operations"])


@pytestmark_bash
def test_operation_create_is_canonical_no_nudge(proj):
    r = _run(proj, "operation", "create", "clean", "--status", "open")
    assert "alias" not in r.stderr and "統一" not in r.stderr


@pytestmark_bash
def test_incident_open_is_deprecated_alias_with_nudge(proj):
    r = _run(proj, "incident", "open", "svc down", "-o", "op-1")
    assert "incident add" in r.stderr
    data = json.loads((proj / ".beacon" / "project.json").read_text())
    # incident got created under op-1 despite the alias.
    op = next(o for o in data["operations"] if o["id"] == "op-1")
    assert any(e.get("type") == "incident" for e in op.get("entries", []))


@pytestmark_bash
def test_incident_add_is_canonical_no_nudge(proj):
    r = _run(proj, "incident", "add", "svc down", "-o", "op-1")
    assert "alias" not in r.stderr


@pytestmark_bash
def test_account_contact_bare_is_alias_with_nudge(proj):
    r = _run(proj, "account", "contact", "acc-1", "Jiro")
    assert "account contact add" in r.stderr
    assert r.returncode == 0
    data = json.loads((proj / ".beacon" / "project.json").read_text())
    acc = next(a for a in data["accounts"] if a["id"] == "acc-1")
    assert any(c.get("name") == "Jiro" for c in acc.get("contacts", []))


@pytestmark_bash
def test_account_contact_add_is_canonical_no_nudge(proj):
    r = _run(proj, "account", "contact", "add", "acc-1", "Taro")
    assert "統一されました" not in r.stderr
    assert r.returncode == 0
    data = json.loads((proj / ".beacon" / "project.json").read_text())
    acc = next(a for a in data["accounts"] if a["id"] == "acc-1")
    assert any(c.get("name") == "Taro" for c in acc.get("contacts", []))


# --- Windows dispatch parity for `account contact add` ----------------------

@pytest.fixture
def captured_call(monkeypatch):
    box: Dict[str, Any] = {"cmd": None, "env": None}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text('{"name":"t","milestones":[]}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


def test_dispatch_account_contact_add_shifts_verb(project_dir, no_bash, captured_call):
    rc = main_mod.main(["account", "contact", "add", "acc-1", "Taro", "--role", "CTO"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "account_contact"
    assert captured_call["env"]["BEACON_ACCOUNT_ID"] == "acc-1"
    assert captured_call["env"]["BEACON_CONTACT_NAME"] == "Taro"
    assert captured_call["env"]["BEACON_CONTACT_ROLE"] == "CTO"


def test_dispatch_account_contact_bare_still_works(project_dir, no_bash, captured_call):
    rc = main_mod.main(["account", "contact", "acc-1", "Jiro"])
    assert rc == 0
    assert captured_call["env"]["BEACON_ACCOUNT_ID"] == "acc-1"
    assert captured_call["env"]["BEACON_CONTACT_NAME"] == "Jiro"
