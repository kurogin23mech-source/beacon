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
import select
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


CODEX_BIN_DEFAULT = "/opt/homebrew/bin/codex"

# e-2997: max inbound WebSocket frame size for the app-server connection.
# The websockets default is 2**20 (1 MiB); a resumed Codex TUI thread streams
# its full state back in one frame that grows with the conversation, so the
# default 1009-kills DM wake once the thread passes ~1 MiB. The peer is a local,
# trusted `codex app-server` on 127.0.0.1, so a generous 64 MiB ceiling keeps a
# sane bound while comfortably covering realistic thread sizes.
_WS_MAX_SIZE = 64 * 1024 * 1024
BRIDGE_BASE_INSTRUCTIONS = (
    "You are the Beacon Codex DM bridge response worker. "
    "Respond to the DM text only. Do not run shell commands, edit files, "
    "inspect the repository, grant bus budget, send Beacon messages, or take "
    "any external action. The parent bridge daemon is responsible for sending "
    "your final text as a Beacon reply. Keep responses concise and include any "
    "exact phrase the sender requested."
)


@dataclass
class AppServerHandle:
    """A live subprocess pipe to ``codex app-server --stdio``.

    The handle owns the subprocess's stdin/stdout. JSON-RPC messages are
    newline-delimited JSON (= one message per line). Notifications and
    responses share the same stream; the caller dispatches by ``id``
    presence (= response if ``id`` present, notification otherwise).
    """

    proc: subprocess.Popen | None = None
    ws: Any | None = None
    next_request_id: int = 1
    pending: dict[int, Any] = field(default_factory=dict)

    def stop(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
            return
        if self.proc is None:
            return
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
    proxy: bool = False,
    sock: str = "",
    remote_url: str = "",
) -> AppServerHandle:
    """Spawn a Codex app-server JSON-RPC pipe and return the handle.

    ``remote_url`` connects directly to an already-running app-server endpoint
    such as ``ws://127.0.0.1:39988``. That is the most practical shared-UX
    route on Codex 0.142.2 because the TUI can also be launched with
    ``codex --remote <same-url>``.

    ``proxy=False`` starts an ephemeral ``codex app-server --stdio`` child.
    ``proxy=True`` connects stdio to the already-running app-server daemon via
    ``codex app-server proxy``. The proxy path is the one that can reproduce
    ClaudeCode-style UX for a remote Codex TUI: the bridge and the TUI talk to
    the same app-server daemon, and the bridge can ``thread/resume`` +
    ``turn/start`` the TUI's existing thread.

    The caller is responsible for invoking ``handle.stop()`` (or using
    a context manager wrapper — left as a follow-up).

    Raises FileNotFoundError if the codex binary isn't installed.
    """
    if remote_url:
        if not remote_url.startswith(("ws://", "wss://")):
            raise ValueError(
                "remote_url currently supports ws:// or wss:// app-server endpoints"
            )
        try:
            from websockets.sync.client import connect
        except ModuleNotFoundError as exc:
            # e-2536: the standard same-machine wake path uses --app-server-url
            # ws://127.0.0.1 (bin/bcodex → watcher.upgrade()), which needs the
            # `websockets` package. Beacon declares no dependencies, and the
            # daemon runs under the system python (/usr/bin/python3), so a fresh
            # install may lack websockets entirely. Without this raise the caller
            # swallows the ImportError into a generic "app-server start failed"
            # and DMs silently degrade to pull-only (received, never woken).
            # Make the failure specific and actionable instead.
            raise ModuleNotFoundError(
                "the 'websockets' package is required to wake Codex over an "
                f"app-server WebSocket URL ({remote_url}), but it is not "
                f"installed for this Python ({sys.executable}). Without it, DMs "
                "are received but only caught up on the next prompt (pull-only), "
                "never woken live. Install it with: "
                f"{sys.executable} -m pip install websockets"
            ) from exc

        # A Codex turn can keep the shared app-server busy longer than the
        # websockets client's default 20-second keepalive window. In that
        # state the client closes an otherwise usable connection before the
        # next Beacon DM arrives. JSON-RPC requests already have bounded
        # response timeouts, so use those as the liveness check instead.
        #
        # e-2997: on `thread/resume` + `turn/start` against an existing TUI
        # thread, the app-server streams the resumed thread state back as a
        # single frame. Once the Codex conversation grows past the websockets
        # default max_size (2**20 = 1 MiB) the receive path rejects that frame
        # with close code 1009 (message too big) and the DM is delivered but
        # never wakes the visible thread (delivered ✓ / opened ✗). The frame is
        # inbound from a local, trusted app-server (127.0.0.1), so lift the cap
        # generously instead of compacting our (already tiny) turn input — the
        # 2 MiB is what we RECEIVE, not what we send.
        return AppServerHandle(
            ws=connect(remote_url, ping_interval=None, max_size=_WS_MAX_SIZE)
        )

    env = {
        **os.environ,
        # The app-server worker must not be able to self-grant autonomous
        # send budget if it chooses to run a Beacon CLI command anyway.
        "BEACON_OPERATION_AUTO_EXECUTE": "1",
        "BEACON_CODEX_APP_SERVER_BRIDGE": "1",
        **(extra_env or {}),
    }
    argv = [codex_bin, "app-server", "proxy"] if proxy else [
        codex_bin, "app-server", "--stdio"
    ]
    if proxy and sock:
        argv.extend(["--sock", sock])
    proc = subprocess.Popen(
        argv,
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
    if handle.ws is not None:
        handle.ws.send(json.dumps(message, ensure_ascii=False))
        return
    if handle.proc is None:
        raise RuntimeError("app-server handle is not connected")
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
    if handle.ws is not None:
        try:
            raw = handle.ws.recv(timeout=timeout_s)
        except TimeoutError:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_unparsed": str(raw)}
    if handle.proc is None:
        return None
    if handle.proc.stdout is None:
        return None
    ready, _, _ = select.select([handle.proc.stdout], [], [], timeout_s)
    if not ready:
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


def thread_resume(
    handle: AppServerHandle,
    *,
    thread_id: str,
    cwd: str | None = None,
) -> dict:
    """Resume or rejoin an existing app-server thread.

    Per the generated Codex app-server schema, if ``threadId`` identifies a
    running thread the server rejoins that thread. This is the key primitive
    for Beacon DM push UX: the receive loop does not create a fresh worker
    persona, it sends the DM as the next turn on the existing Codex thread.
    """
    params: dict = {"threadId": thread_id}
    if cwd is not None:
        params["cwd"] = cwd
    return request(handle, "thread/resume", params=params)


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


def extract_turn_id(response: dict) -> str:
    """Return the turn id from a ``turn/start`` response."""
    result = response.get("result") or {}
    turn = result.get("turn") or {}
    turn_id = turn.get("id") or result.get("turnId") or ""
    if not turn_id:
        raise RuntimeError(f"turn/start returned no turn id: {response}")
    return str(turn_id)


def drain_until_idle(
    handle: AppServerHandle,
    *,
    thread_id: str,
    turn_id: str | None = None,
    timeout_s: float = 60.0,
) -> list[dict]:
    """Read app-server notifications until the target thread is idle.

    ``turn/start`` returns as soon as the turn is accepted; the useful agent
    text arrives later as notifications. The receive-loop needs a bounded
    drain so ``agent_text_preview`` reflects the actual response instead of
    the pre-response notification prefix.
    """
    end = time.monotonic() + timeout_s
    notifications: list[dict] = []
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        msg = _recv_one(handle, timeout_s=remaining)
        if msg is None:
            break
        notifications.append(msg)
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "thread/status/changed":
            if params.get("threadId") != thread_id:
                continue
            status = params.get("status") or {}
            if status.get("type") == "idle":
                break
        elif method == "turn/completed":
            if turn_id and params.get("turnId") == turn_id:
                break
    return notifications


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
    target_thread_id: str = ""
    use_proxy: bool = False
    proxy_sock: str = ""
    remote_url: str = ""

    def ensure_started(self, cwd: str) -> None:
        if self.handle is not None:
            return
        self.handle = start_app_server(
            proxy=self.use_proxy,
            sock=self.proxy_sock,
            remote_url=self.remote_url,
        )
        # Standard JSON-RPC initialize.
        initialize(self.handle)
        if self.target_thread_id:
            rsp = thread_resume(
                self.handle,
                thread_id=self.target_thread_id,
                cwd=cwd,
            )
        else:
            # One response-worker thread per bridge instance for the fallback
            # autonomous path. This is intentionally separate from the
            # remote-thread UX path above.
            rsp = thread_start(
                self.handle,
                cwd=cwd,
                base_instructions=BRIDGE_BASE_INSTRUCTIONS,
            )
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

    def dispatch_dm_and_wait(
        self, event: dict, *, timeout_s: float = 60.0, on_dispatched=None
    ) -> dict:
        """Dispatch a DM and drain notifications until the turn is idle.

        ``on_dispatched`` (= optional ``callable()``) fires right after the DM
        is handed to the app-server turn (= injected via ``dispatch_dm``) and
        before we block on the turn completing. The receive loop uses this
        seam to stamp the ``opened`` receipt at read-time rather than
        turn-completion time (= ms-93 / e-3140): ``opened`` is a read-receipt,
        and the AI reads the DM when it enters the turn, not when the
        (arbitrarily long) turn finishes. A failed inject (= ``dispatch_dm``
        raising) happens before ``on_dispatched``, so ``opened`` is never
        stamped for a DM that did not reach a turn. ``on_dispatched`` must be
        best-effort (= not raise); a receipt-stamp failure must not abort the
        turn.
        """
        if self.handle is None or self.thread_id is None:
            raise RuntimeError(
                "BridgeAppServerClient: call ensure_started() before dispatch_dm_and_wait()"
            )
        rsp = self.dispatch_dm(event)
        if on_dispatched is not None:
            on_dispatched()
        turn_id = extract_turn_id(rsp)
        rsp.setdefault("_notifications", [])
        rsp["_notifications"].extend(
            drain_until_idle(
                self.handle,
                thread_id=self.thread_id,
                turn_id=turn_id,
                timeout_s=timeout_s,
            )
        )
        return rsp

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.stop()
            self.handle = None
            self.thread_id = None


__all__ = [
    "AppServerHandle",
    "BridgeAppServerClient",
    "agent_message_text_from_notifications",
    "drain_until_idle",
    "initialize",
    "extract_thread_id",
    "extract_turn_id",
    "request",
    "start_app_server",
    "text_input",
    "thread_resume",
    "thread_start",
    "turn_start",
]
