"""Minimal JSON-RPC client skeleton for `codex app-server` (= ms-93 / e-2519 / SPEC §8-G option D spike).

Goal of this skeleton (= not a production driver):

- Confirm the wire shape (= JSON-RPC 2.0-ish over stdio with `thread/start` +
  `turn/start` + streamed notifications) so the next phase can extend it.
- Land enough scaffolding that a fresh Codex / Claude Code session can
  pick up from here without re-doing the schema dig.
- **NOT** wired into ``scripts/codex-receive-loop.py`` yet — the
  ``BridgeAppServerClient.dispatch_dm`` method has a TODO marker where
  the daemon would invoke it on DM receipt.

Background (= Codex 設計相談 2026-06-26):
- option D was Codex's recommended main target (cleaner than MCP wake +
  more explicit lifecycle than `codex exec` spawn). See ms-93 SPEC §8-G
  and DM thread (= my `0DJAosVqkhYXQ4yqZAZs` / Codex `dkO0ARY9vUGLI7nXyR0R`).
- The app-server protocol bundle was generated locally via
  ``codex app-server generate-json-schema --out <dir>`` and surfaced
  ``Thread/start`` / ``Turn/start`` / streamed notifications as the
  primary thread-and-turn lifecycle (= v2 schema).

What this skeleton does NOT do (= follow-up):
- No durable managed-daemon path. Codex 0.142.2 on this dogfood machine
  rejected ``codex app-server daemon start`` because the standalone install
  managed by the Codex installer was absent; direct ``codex app-server
  --stdio`` worked.
- No Beacon DM reply sender yet (= the autonomous response path).
- No armed-mode gate (= e-2519 AC 6 lives in a separate Skill).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


CODEX_BIN_DEFAULT = "/opt/homebrew/bin/codex"


@dataclass
class AppServerHandle:
    """A live subprocess pipe to ``codex app-server --stdio``.

    The handle owns the subprocess's stdin/stdout. JSON-RPC messages are
    newline-delimited JSON (= one message per line). Notifications and
    responses share the same stream; the caller dispatches by ``id``
    presence (= response if ``id`` present, notification otherwise).
    """

    proc: subprocess.Popen
    next_request_id: int = 1
    pending: dict[int, Any] = field(default_factory=dict)

    def stop(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def start_app_server(
    codex_bin: str = CODEX_BIN_DEFAULT,
    *,
    extra_env: dict[str, str] | None = None,
) -> AppServerHandle:
    """Spawn ``codex app-server --stdio`` and return the handle.

    The caller is responsible for invoking ``handle.stop()`` (or using
    a context manager wrapper — left as a follow-up).

    Raises FileNotFoundError if the codex binary isn't installed.
    """
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
        env=env,
    )
    return AppServerHandle(proc=proc)


def _send(handle: AppServerHandle, message: dict) -> None:
    """Write a JSON-RPC message to the server stdin."""
    if handle.proc.stdin is None:
        raise RuntimeError("app-server stdin closed")
    line = json.dumps(message, ensure_ascii=False) + "\n"
    handle.proc.stdin.write(line)
    handle.proc.stdin.flush()


def _recv_one(handle: AppServerHandle, timeout_s: float = 30.0) -> dict | None:
    """Read one newline-delimited JSON-RPC message from the server stdout.

    Returns ``None`` on EOF / timeout. Real production would use a
    selector or asyncio reader; this is a blocking helper for the spike.
    """
    if handle.proc.stdout is None:
        return None
    line = handle.proc.stdout.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        # Server may write non-JSON on stderr; stdout should be JSON-only.
        return {"_unparsed": line.rstrip("\n")}


def request(
    handle: AppServerHandle,
    method: str,
    params: dict | None = None,
) -> dict:
    """Send a request and read responses until our id is answered.

    Notifications (no ``id``) seen along the way are returned in the
    response under ``_notifications`` so the caller can inspect them
    (= the spike pattern; production would route them to handlers).
    """
    req_id = handle.next_request_id
    handle.next_request_id += 1
    msg = {
        "id": req_id,
        "method": method,
        "params": params or {},
    }
    _send(handle, msg)

    notifications: list[dict] = []
    while True:
        rsp = _recv_one(handle)
        if rsp is None:
            raise TimeoutError(f"no response to {method} (req_id={req_id})")
        if "id" not in rsp:
            notifications.append(rsp)
            continue
        if rsp.get("id") == req_id:
            rsp["_notifications"] = notifications
            return rsp


# ------------------------------------------------------------------ #
# High-level handshake helpers (= e-2519 AC 1 spike target)
# ------------------------------------------------------------------ #


def initialize(handle: AppServerHandle, client_info: dict | None = None) -> dict:
    """Send the standard JSON-RPC ``initialize`` request.

    NOTE: the actual ``InitializeRequest`` schema was not directly emitted
    by ``generate-json-schema`` (only the ClientRequest oneOf reference),
    so the params shape here is best-effort. The spike's first job is to
    confirm what the server accepts and adjust this helper accordingly.
    """
    return request(
        handle,
        "initialize",
        params={
            "clientInfo": client_info or {
                "name": "beacon-codex-bridge",
                "version": "0.1.0-spike",
            },
        },
    )


def thread_start(
    handle: AppServerHandle,
    *,
    cwd: str | None = None,
    base_instructions: str | None = None,
    approval_policy: str | None = None,
) -> dict:
    """Start a new thread; returns the response containing the thread id."""
    params: dict = {}
    if cwd is not None:
        params["cwd"] = cwd
    if base_instructions is not None:
        params["baseInstructions"] = base_instructions
    if approval_policy is not None:
        params["approvalPolicy"] = approval_policy
    return request(handle, "thread/start", params=params)


def extract_thread_id(response: dict) -> str:
    """Return the thread id from a ``thread/start`` response."""
    result = response.get("result") or {}
    thread = result.get("thread") or {}
    thread_id = thread.get("id") or result.get("threadId") or ""
    if not thread_id:
        raise RuntimeError(f"thread/start returned no thread id: {response}")
    return str(thread_id)


def turn_start(
    handle: AppServerHandle,
    *,
    thread_id: str,
    input_payload: list[dict],
) -> dict:
    """Start a turn within the thread.

    Dogfood against Codex CLI 0.142.2 confirmed ``input`` must be a sequence.
    A raw string is rejected with "expected a sequence"; the minimal accepted
    text shape is ``[{"type": "text", "text": "..."}]``.
    """
    return request(
        handle,
        "turn/start",
        params={
            "threadId": thread_id,
            "input": input_payload,
        },
    )


def text_input(text: str) -> list[dict]:
    """Return the minimal accepted app-server turn input shape."""
    return [{"type": "text", "text": text}]


def agent_message_text_from_notifications(notifications: Iterable[dict]) -> str:
    """Reconstruct final agent text from app-server notifications.

    The app-server stream emits ``item/agentMessage/delta`` chunks and later an
    ``item/completed`` with ``item.type == "agentMessage"`` and full ``text``.
    Prefer the completed item when present; otherwise concatenate deltas.
    """
    chunks: list[str] = []
    completed_text = ""
    for msg in notifications:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "item/agentMessage/delta":
            chunks.append(str(params.get("delta") or ""))
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                completed_text = str(item.get("text") or "")
    return completed_text or "".join(chunks)


# ------------------------------------------------------------------ #
# Bridge integration point (= TODO for e-2519 AC 2-4)
# ------------------------------------------------------------------ #


@dataclass
class BridgeAppServerClient:
    """Higher-level helper for the bridge to use.

    Lifecycle:
    - call ``ensure_started()`` lazily on the first DM
    - call ``dispatch_dm(event)`` from the receive-loop's poll iteration
      when the DM payload should produce an autonomous response
    - call ``stop()`` on bridge shutdown

    All of the below is **TODO**: the integration point exists in
    ``scripts/codex-receive-loop.py`` but is gated behind a non-default
    flag (= ``--app-server`` or similar) so that the existing pull-on-
    prompt path remains the default until D is validated.
    """

    handle: AppServerHandle | None = None
    thread_id: str | None = None
    on_agent_message: Callable[[dict], None] | None = None

    def ensure_started(self, cwd: str) -> None:
        if self.handle is not None:
            return
        self.handle = start_app_server()
        # Standard JSON-RPC initialize.
        initialize(self.handle)
        # One thread per bridge instance for now (= simplest model).
        rsp = thread_start(self.handle, cwd=cwd)
        self.thread_id = extract_thread_id(rsp)

    def dispatch_dm(self, event: dict) -> dict:
        """Convert a Beacon DM event into a Turn/start invocation.

        TODO (= e-2519 AC 2):
        - map ``event['payload']['text']`` into the proper ``TurnInput``
          content shape (= the schema's ``input`` field is non-trivial)
        - decide thread strategy: 1 thread per session, per DM, or per
          conversation_id (= envelope.conversation_id)
        - hook ``on_agent_message`` to send the final agent message back
          as a Beacon DM reply (= autonomous response loop)
        - respect armed-mode budget (= caller's responsibility, this
          method assumes the caller already gated)
        """
        if self.handle is None or self.thread_id is None:
            raise RuntimeError(
                "BridgeAppServerClient: call ensure_started() before dispatch_dm()"
            )
        payload = (event or {}).get("payload") or {}
        text = payload.get("text") or ""
        return turn_start(
            self.handle,
            thread_id=self.thread_id,
            input_payload=text_input(text),
        )

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.stop()
            self.handle = None
            self.thread_id = None


__all__ = [
    "AppServerHandle",
    "BridgeAppServerClient",
    "agent_message_text_from_notifications",
    "initialize",
    "extract_thread_id",
    "request",
    "start_app_server",
    "text_input",
    "thread_start",
    "turn_start",
]
