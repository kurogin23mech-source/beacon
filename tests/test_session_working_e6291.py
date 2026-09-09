"""ms-159 / e-6291 — the working_target declaration path.

A session declares WHICH target it works on via the same intent doc as
`session focus` (which doubles as the activity). We pin: the server model
accepts working_target, the api client forwards it, the dispatch (Windows-parity)
frontend translates argv → the same env contract bin/beacon uses, and the
`--target` spec parser maps kind:id / bare-id correctly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))


# ---------------------------------------------------------------------------
# server model — SessionIntentUpsert.working_target
# ---------------------------------------------------------------------------

class TestSessionIntentModel:
    def _model(self):
        sys.path.insert(0, str(ROOT / "server"))
        os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
        import routers_projects  # noqa: E402
        return routers_projects.SessionIntentUpsert

    def test_accepts_working_target(self):
        m = self._model()(
            working_target={"target": {"kind": "milestone", "id": "ms-159",
                                       "title": "統合オペUI"}})
        payload = {k: v for k, v in m.model_dump().items() if v is not None}
        assert payload["working_target"]["target"]["id"] == "ms-159"

    def test_working_target_optional(self):
        m = self._model()(text="doing X")
        payload = {k: v for k, v in m.model_dump().items() if v is not None}
        assert "working_target" not in payload
        assert payload["text"] == "doing X"

    def test_all_three_fields_coexist(self):
        m = self._model()(
            text="activity line", attention_required=True,
            working_target={"target": {"kind": "task", "id": "e-6291"}})
        payload = {k: v for k, v in m.model_dump().items() if v is not None}
        assert payload["text"] == "activity line"
        assert payload["attention_required"] is True
        assert payload["working_target"]["target"]["id"] == "e-6291"


# ---------------------------------------------------------------------------
# api client — upsert_session_intent forwards working_target
# ---------------------------------------------------------------------------

class TestApiClientForwardsWorkingTarget:
    def _client(self):
        import api_client  # noqa: E402
        # Bypass __init__ (no network/token needed — we monkeypatch post).
        return api_client.ApiClient.__new__(api_client.ApiClient)

    def test_working_target_in_body(self, monkeypatch):
        import api_client  # noqa: E402
        captured: Dict[str, Any] = {}

        def fake_post(self, path, body):
            captured["path"] = path
            captured["body"] = body
            return {"status": "ok"}

        monkeypatch.setattr(api_client.ApiClient, "post", fake_post)
        c = self._client()
        c.upsert_session_intent(
            "proj", "sv-1",
            working_target={"target": {"kind": "milestone", "id": "ms-159"}})
        assert captured["body"] == {
            "working_target": {"target": {"kind": "milestone", "id": "ms-159"}}}
        assert "/sessions/sv-1/intent" in captured["path"]

    def test_omitted_when_none(self, monkeypatch):
        import api_client  # noqa: E402
        captured: Dict[str, Any] = {}
        monkeypatch.setattr(api_client.ApiClient, "post",
                            lambda self, path, body: captured.update(body=body))
        c = self._client()
        c.upsert_session_intent("proj", "sv-1", text="hi")
        assert "working_target" not in captured["body"]


# ---------------------------------------------------------------------------
# --target spec parser
# ---------------------------------------------------------------------------

class TestParseWorkingTarget:
    def _parse(self, spec):
        import cmd_session  # noqa: E402
        return cmd_session._parse_working_target(spec)

    def test_kind_colon_id(self):
        assert self._parse("ms:ms-159") == ("milestone", "ms-159")
        assert self._parse("task:e-6291") == ("task", "e-6291")
        assert self._parse("opp:opp-3") == ("opportunity", "opp-3")

    def test_full_kind_names(self):
        assert self._parse("milestone:ms-1") == ("milestone", "ms-1")
        assert self._parse("opportunity:opp-1") == ("opportunity", "opp-1")

    def test_bare_id_infers_kind(self):
        assert self._parse("ms-159") == ("milestone", "ms-159")
        assert self._parse("e-6291") == ("task", "e-6291")

    def test_unknown_kind_passes_through(self):
        assert self._parse("account:acc-1") == ("account", "acc-1")

    def test_empty(self):
        assert self._parse("") == (None, None)
        assert self._parse("   ") == (None, None)

    def test_bare_unknown_prefix_id_only(self):
        kind, ident = self._parse("weird-9")
        assert kind is None and ident == "weird-9"


# ---------------------------------------------------------------------------
# dispatch (Windows parity) — session working env contract
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_call(monkeypatch):
    from beacon_cli import dispatch
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
        '{"name": "test", "milestones": []}')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    from beacon_cli import main as main_mod
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


class TestDispatchSessionWorking:
    def test_declare_target(self, project_dir, no_bash, captured_call):
        from beacon_cli import main as main_mod
        rc = main_mod.main([
            "session", "working", "--target", "ms:ms-159",
            "--title", "統合オペUI", "--json"])
        assert rc == 0
        env = captured_call["env"]
        assert env["BEACON_SESSION_WORKING_TARGET"] == "ms:ms-159"
        assert env["BEACON_SESSION_WORKING_TITLE"] == "統合オペUI"
        assert env["BEACON_JSON"] == "1"
        assert captured_call["cmd"][-1] == "session_working"

    def test_show_and_clear_flags(self, project_dir, no_bash, captured_call):
        from beacon_cli import main as main_mod
        main_mod.main(["session", "working", "--show"])
        assert captured_call["env"]["BEACON_SESSION_WORKING_SHOW"] == "1"
        main_mod.main(["session", "working", "--clear"])
        assert captured_call["env"]["BEACON_SESSION_WORKING_CLEAR"] == "1"

    def test_handler_registered(self):
        from beacon_cli import dispatch
        assert "session" in dispatch._HANDLERS


class TestWorkingMutualExclusion:
    """#739 review (AX high): --show / --clear / --target are mutually exclusive.
    A conflict must error (exit 2) BEFORE any API call, not silently pick one
    (e.g. `--clear --target ms:X` used to eat --target and report success)."""

    def _run(self, monkeypatch, **env):
        import cmd_session
        for k in ("SHOW", "CLEAR", "TARGET", "TITLE"):
            monkeypatch.setenv(f"BEACON_SESSION_WORKING_{k}", env.get(k, ""))
        monkeypatch.setenv("BEACON_JSON", "")
        return cmd_session.cmd_session_working

    def test_clear_and_target_conflict(self, monkeypatch):
        fn = self._run(monkeypatch, CLEAR="1", TARGET="ms:ms-1")
        with pytest.raises(SystemExit) as ei:
            fn()
        assert ei.value.code == 2

    def test_show_and_target_conflict(self, monkeypatch):
        fn = self._run(monkeypatch, SHOW="1", TARGET="ms:ms-1")
        with pytest.raises(SystemExit) as ei:
            fn()
        assert ei.value.code == 2

    def test_show_and_clear_conflict(self, monkeypatch):
        fn = self._run(monkeypatch, SHOW="1", CLEAR="1")
        with pytest.raises(SystemExit) as ei:
            fn()
        assert ei.value.code == 2

    def test_guard_fires_before_api_call(self, monkeypatch):
        # If the guard ran AFTER _get_api_client, this would raise the client's
        # error, not our exit 2. Patch the client to explode to prove the guard
        # short-circuits first.
        import cmd_session

        def _boom():
            raise AssertionError("_get_api_client must not be reached on conflict")

        monkeypatch.setattr(cmd_session, "_get_api_client", _boom)
        fn = self._run(monkeypatch, CLEAR="1", TARGET="ms:ms-1")
        with pytest.raises(SystemExit) as ei:
            fn()
        assert ei.value.code == 2
