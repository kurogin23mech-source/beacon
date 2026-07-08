"""Codex exec-worker fallback (ms-93 / e-2519 AC 2).

Pins option B: when the app-server (D) transport is unavailable but the bridge
is armed, each DM spawns a one-shot ``codex exec`` worker whose reply is sent
back. Because ``ExecWorkerClient`` is a drop-in for ``BridgeAppServerClient``,
the same ``dispatch_kept_event`` seam (= AC 7) drives it end-to-end — so this
also proves the fallback closes the idle→DM→autonomous-reply loop.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "beacon"


def _load_exec_worker_module():
    path = PLUGIN_ROOT / "scripts" / "beacon_codex_exec_worker.py"
    spec = importlib.util.spec_from_file_location(
        "beacon_codex_exec_worker_test", path
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _load_receive_loop_module():
    path = REPO_ROOT / "lib" / "codex_receive_loop.py"
    spec = importlib.util.spec_from_file_location(
        "codex_receive_loop_execw", path
    )
    m = importlib.util.module_from_spec(spec)
    if str(REPO_ROOT / "lib") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "lib"))
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _load_app_server_client_module():
    path = PLUGIN_ROOT / "scripts" / "beacon_codex_app_server_client.py"
    spec = importlib.util.spec_from_file_location(
        "beacon_codex_app_server_client_execw", path
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _evt(event_id="evt-1", sender="sv-peer", text="what's the status?"):
    return {
        "event_id": event_id,
        "sender_session_id": sender,
        "payload": {"text": text},
    }


# ------------------------------------------------------------------ #
# Argv + prompt shape
# ------------------------------------------------------------------ #


def test_build_exec_argv_is_json_output_last_message_conservative_sandbox():
    ew = _load_exec_worker_module()
    argv = ew.build_exec_argv(
        "the prompt", codex_bin="/bin/codex", output_path="/tmp/out.txt",
        sandbox="read-only",
    )
    assert argv[:2] == ["/bin/codex", "exec"]
    assert "--json" in argv
    assert argv[argv.index("--output-last-message") + 1] == "/tmp/out.txt"
    assert argv[argv.index("-s") + 1] == "read-only"
    # Non-interactive so a headless daemon never blocks on approval.
    assert argv[argv.index("-a") + 1] == "never"
    assert "--skip-git-repo-check" in argv
    # Prompt is the final positional argument.
    assert argv[-1] == "the prompt"


def test_default_sandbox_is_read_only(monkeypatch):
    ew = _load_exec_worker_module()
    monkeypatch.delenv("BEACON_CODEX_EXEC_SANDBOX", raising=False)
    assert ew.resolve_exec_sandbox() == "read-only"


def test_sandbox_env_override(monkeypatch):
    ew = _load_exec_worker_module()
    monkeypatch.setenv("BEACON_CODEX_EXEC_SANDBOX", "workspace-write")
    assert ew.resolve_exec_sandbox() == "workspace-write"


def test_build_prompt_embeds_message_and_sender():
    ew = _load_exec_worker_module()
    prompt = ew.build_exec_prompt(_evt(sender="sv-abc", text="ping"))
    assert "ping" in prompt
    assert "sv-abc" in prompt
    # Instruction-first so the reply is sent verbatim without confirmation.
    assert "concisely" in prompt.lower()


# ------------------------------------------------------------------ #
# dispatch_dm_and_wait: spawn worker, read last message
# ------------------------------------------------------------------ #


def test_dispatch_reads_last_message_file_and_wraps_notifications():
    ew = _load_exec_worker_module()
    captured = {}

    def fake_run(argv, *, cwd, timeout_s):
        # The worker writes its final message to the --output-last-message file.
        out_path = argv[argv.index("--output-last-message") + 1]
        Path(out_path).write_text("here is the status: all green", encoding="utf-8")
        captured["argv"] = argv
        captured["cwd"] = cwd

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        return _Proc()

    client = ew.ExecWorkerClient(codex_bin="/bin/codex", run_exec=fake_run)
    client.ensure_started(cwd="/work/proj")
    rsp = client.dispatch_dm_and_wait(_evt())

    text = _load_app_server_client_module().agent_message_text_from_notifications(
        rsp["_notifications"]
    )
    assert text == "here is the status: all green"
    assert captured["cwd"] == "/work/proj"
    assert "read-only" in captured["argv"]


def test_dispatch_raises_on_worker_failure():
    ew = _load_exec_worker_module()

    def fake_run(argv, *, cwd, timeout_s):
        class _Proc:
            returncode = 2
            stderr = "codex boom"
            stdout = ""

        return _Proc()

    client = ew.ExecWorkerClient(run_exec=fake_run)
    with pytest.raises(RuntimeError):
        client.dispatch_dm_and_wait(_evt())


def test_dispatch_falls_back_to_stdout_when_no_output_file():
    ew = _load_exec_worker_module()

    def fake_run(argv, *, cwd, timeout_s):
        # Worker succeeded but wrote nothing to the file; stdout carries text.
        class _Proc:
            returncode = 0
            stderr = ""
            stdout = "reply via stdout"

        return _Proc()

    client = ew.ExecWorkerClient(run_exec=fake_run)
    rsp = client.dispatch_dm_and_wait(_evt())
    text = _load_app_server_client_module().agent_message_text_from_notifications(
        rsp["_notifications"]
    )
    assert text == "reply via stdout"


# ------------------------------------------------------------------ #
# End-to-end via the shared dispatch seam (= B fallback closes the loop)
# ------------------------------------------------------------------ #


def test_exec_worker_closes_autonomous_loop_via_dispatch_seam():
    ew = _load_exec_worker_module()
    crl = _load_receive_loop_module()

    def fake_run(argv, *, cwd, timeout_s):
        out_path = argv[argv.index("--output-last-message") + 1]
        Path(out_path).write_text("on it", encoding="utf-8")

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        return _Proc()

    worker = ew.ExecWorkerClient(run_exec=fake_run)
    worker.ensure_started(cwd="/work")

    replies = []

    def capture_reply(reply_argv, *, sender_session_id):
        replies.append({"argv": reply_argv, "sender": sender_session_id})

        class _Proc:
            returncode = 0
            stderr = ""

        return _Proc()

    res = crl.dispatch_kept_event(
        _evt(event_id="evt-9", sender="sv-peer-2", text="status?"),
        app_server_client=worker,
        agent_text_fn=_load_app_server_client_module().agent_message_text_from_notifications,
        ack_fn=lambda eid: None,
        armed=True,
        sender_session_id="sv-codex",
        beacon_bin="/abs/beacon",
        run_reply=capture_reply,
    )

    assert res["replied"] is True
    argv = replies[0]["argv"]
    assert argv[argv.index("--to") + 1] == "sv-peer-2"
    assert argv[argv.index("--in-reply-to") + 1] == "evt-9"
    payload = json.loads(argv[argv.index("--payload") + 1])
    assert payload["text"] == "on it"
