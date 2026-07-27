"""Windows/pipx-path parity for the `beacon org` noun (ms-118 / e-4242).

The `org` tenancy commands (create/list/show/add-member/remove-member/rehome/
delete) shipped on the bash path (bin/beacon) but were missing from the Python
dispatch (beacon_cli/dispatch.py), so Windows pipx users hit argparse
`invalid choice: 'org'` — the same cross-platform breakage class the
dispatch-parity lint exists for.

Strategy mirrors tests/test_dm_send_dispatch.py: patch subprocess.call inside
beacon_cli.dispatch to capture the (cmd, env) that would reach commands.py, then
assert each argv path produces the right commands.py subcommand + env contract
(the exact env var names bin/beacon exports).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402


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
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


def _run(argv):
    return main_mod.main(argv)


def test_org_create(project_dir, no_bash, captured_call):
    assert _run(["org", "create", "Acme Corp", "--json"]) == 0
    assert captured_call["cmd"][-1] == "org_create"
    assert captured_call["env"]["BEACON_ORG_NAME"] == "Acme Corp"
    assert captured_call["env"]["BEACON_JSON"] == "1"


def test_org_list_all(project_dir, no_bash, captured_call):
    assert _run(["org", "list", "--all", "--json"]) == 0
    assert captured_call["cmd"][-1] == "org_list"
    assert captured_call["env"]["BEACON_ORG_ALL"] == "1"
    assert captured_call["env"]["BEACON_JSON"] == "1"


def test_org_list_alias_ls(project_dir, no_bash, captured_call):
    assert _run(["org", "ls"]) == 0
    assert captured_call["cmd"][-1] == "org_list"
    assert captured_call["env"]["BEACON_ORG_ALL"] == ""


def test_org_show(project_dir, no_bash, captured_call):
    assert _run(["org", "show", "org-t-abcd"]) == 0
    assert captured_call["cmd"][-1] == "org_show"
    assert captured_call["env"]["BEACON_ORG_ID"] == "org-t-abcd"


def test_org_add_member_with_role(project_dir, no_bash, captured_call):
    assert _run(["org", "add-member", "org-t-abcd", "bob@x", "--role", "admin"]) == 0
    assert captured_call["cmd"][-1] == "org_add_member"
    env = captured_call["env"]
    assert env["BEACON_ORG_ID"] == "org-t-abcd"
    assert env["BEACON_ORG_EMAIL"] == "bob@x"
    assert env["BEACON_ORG_ROLE"] == "admin"


def test_org_invite_alias(project_dir, no_bash, captured_call):
    assert _run(["org", "invite", "org-t-abcd", "bob@x"]) == 0
    assert captured_call["cmd"][-1] == "org_add_member"
    assert captured_call["env"]["BEACON_ORG_EMAIL"] == "bob@x"


def test_org_remove_member(project_dir, no_bash, captured_call):
    assert _run(["org", "remove-member", "org-t-abcd", "bob@x"]) == 0
    assert captured_call["cmd"][-1] == "org_remove_member"
    assert captured_call["env"]["BEACON_ORG_MEMBER"] == "bob@x"


def test_org_rehome(project_dir, no_bash, captured_call):
    assert _run(["org", "rehome", "p1", "--to", "org-t-abcd"]) == 0
    assert captured_call["cmd"][-1] == "org_rehome"
    env = captured_call["env"]
    assert env["BEACON_ORG_REHOME_PROJECT"] == "p1"
    assert env["BEACON_ORG_REHOME_TARGET"] == "org-t-abcd"


def test_org_delete(project_dir, no_bash, captured_call):
    assert _run(["org", "delete", "org-t-abcd"]) == 0
    assert captured_call["cmd"][-1] == "org_delete"
    assert captured_call["env"]["BEACON_ORG_ID"] == "org-t-abcd"


def test_org_delete_alias_rm(project_dir, no_bash, captured_call):
    assert _run(["org", "rm", "org-t-abcd"]) == 0
    assert captured_call["cmd"][-1] == "org_delete"


# ---- usage / guard paths (no subprocess call expected) -------------------

def test_org_no_subcommand_is_usage(project_dir, no_bash, captured_call):
    rc = _run(["org"])
    assert rc == 2
    assert captured_call["cmd"] is None  # nothing dispatched


def test_org_rehome_missing_to_is_usage(project_dir, no_bash, captured_call):
    rc = _run(["org", "rehome", "p1"])
    assert rc == 1
    assert captured_call["cmd"] is None
