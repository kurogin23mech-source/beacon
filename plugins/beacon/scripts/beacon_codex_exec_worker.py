"""Codex exec-worker fallback for autonomous DM replies (ms-93 / e-2519 AC 2).

Option D (``codex app-server`` supervisor) is the main autonomous push path,
but it is an experimental transport that can fail to start. Option B is the
stable fallback: when the app-server is unavailable AND the bridge is armed,
each incoming DM spawns a **separate, one-shot** ``codex exec`` worker that
produces the reply text, which the daemon then sends back as a Beacon DM.

**Semantic (must be stated wherever this is surfaced):** this does NOT wake
the user's existing Codex TUI. It launches a fresh, headless ``codex exec``
process to answer the DM. The user's interactive session is untouched.

Safety:
- runs only when the bridge is armed (= explicit opt-in + budget gate, the
  daemon's responsibility — this module assumes the caller already gated)
- the worker runs in a **conservative sandbox** (default ``read-only``) so an
  autonomous reply cannot mutate the workspace. Override via
  ``BEACON_CODEX_EXEC_SANDBOX`` only with intent.

This class is a **drop-in for ``BridgeAppServerClient``**: it exposes the same
``ensure_started`` / ``dispatch_dm_and_wait`` / ``stop`` / ``thread_id`` shape
and returns the same ``{"_notifications": [...]}`` result, so the daemon's
``dispatch_kept_event`` seam (= AC 7) drives either transport unchanged and the
same ``agent_message_text_from_notifications`` reconstructs the reply text.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


DEFAULT_EXEC_SANDBOX = "read-only"


def resolve_exec_sandbox() -> str:
    """The sandbox mode for the fallback worker. Conservative by default."""
    return (os.environ.get("BEACON_CODEX_EXEC_SANDBOX") or DEFAULT_EXEC_SANDBOX).strip()


def build_exec_prompt(event: dict) -> str:
    """Turn a Beacon DM event into a self-contained prompt for ``codex exec``.

    The worker has no conversation history, so the prompt states its role and
    embeds the incoming message. Kept short and instruction-first so the reply
    stays on-topic and concise.
    """
    payload = (event or {}).get("payload") or {}
    text = str(payload.get("text") or "").strip()
    sender = str((event or {}).get("sender_session_id") or "a teammate")
    return (
        "You are the Codex agent for this Beacon project, answering a direct "
        "message from another session while the human is away. Reply concisely "
        "and only with the message body (it is sent back verbatim as your DM "
        f"reply). Do not ask for confirmation.\n\n"
        f"From: {sender}\n"
        f"Message: {text}"
    )


def build_exec_argv(
    prompt: str,
    *,
    codex_bin: str,
    output_path: str,
    sandbox: str,
) -> list[str]:
    """Argv for a one-shot, non-interactive ``codex exec`` worker.

    - ``--json`` + ``--output-last-message`` capture the final agent message
      deterministically (we read the file, not the JSONL stream).
    - ``-s <sandbox>`` keeps the worker conservative (default read-only).
    - ``--skip-git-repo-check`` avoids a hard error outside a git repo.

    ``codex exec`` is already non-interactive (no approval prompts), so no
    ``-a/--ask-for-approval`` flag is passed — that flag only exists on the
    top-level ``codex`` command and ``codex exec`` rejects it (verified via a
    live daemon dogfood, 2026-07-08: ``unexpected argument '-a' found``).
    """
    return [
        codex_bin,
        "exec",
        "--json",
        "--output-last-message",
        output_path,
        "-s",
        sandbox,
        "--skip-git-repo-check",
        prompt,
    ]


def notifications_from_text(text: str) -> list[dict]:
    """Wrap a plain reply string in the app-server notification shape.

    Lets ``agent_message_text_from_notifications`` (the shared reconstruction
    used by the app-server path) extract it unchanged, so both transports feed
    ``dispatch_kept_event`` identically."""
    return [
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": text or ""}},
        }
    ]


class ExecWorkerClient:
    """Fallback autonomous-reply transport using one ``codex exec`` per DM.

    Drop-in for ``BridgeAppServerClient``. ``run_exec`` is injectable so tests
    drive the whole path without spawning a real Codex.
    """

    thread_id = "exec-worker"  # non-None so the daemon treats it as "ready"

    def __init__(
        self,
        *,
        codex_bin: str = "",
        sandbox: str = "",
        timeout_s: float = 120.0,
        run_exec=None,
    ):
        self.codex_bin = codex_bin or os.environ.get("CODEX_BIN") or "codex"
        self.sandbox = sandbox or resolve_exec_sandbox()
        self.timeout_s = timeout_s
        self._run_exec = run_exec or self._default_run_exec
        self.cwd = ""

    def ensure_started(self, cwd: str) -> None:
        """No long-lived child to start; just remember the cwd for workers."""
        self.cwd = cwd or ""

    @staticmethod
    def _default_run_exec(argv, *, cwd, timeout_s):
        return subprocess.run(
            argv,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )

    def dispatch_dm_and_wait(
        self, event: dict, *, timeout_s: float = None, on_dispatched=None
    ) -> dict:
        """Spawn a one-shot worker for this DM and return its reply.

        Returns the same ``{"_notifications": [...]}`` shape as the app-server
        path. Raises on a failed worker so the daemon's dispatch error handler
        runs (persist for the hook fallback); reply-send gating is the caller's.

        ``on_dispatched`` (= optional ``callable()``) fires once the DM has
        been handed to a worker (= injected / read), before we block on the
        worker completing — the same read-receipt seam as the app-server path
        (= ms-93 / e-3140). ``opened`` is a read-receipt, so it fires when the
        worker starts on the DM, not when the (arbitrarily long) worker turn
        finishes. Must be best-effort (= not raise).
        """
        prompt = build_exec_prompt(event)
        if on_dispatched is not None:
            on_dispatched()
        with tempfile.TemporaryDirectory(prefix="beacon-codex-exec-") as tmp:
            out_path = str(Path(tmp) / "last-message.txt")
            argv = build_exec_argv(
                prompt,
                codex_bin=self.codex_bin,
                output_path=out_path,
                sandbox=self.sandbox,
            )
            proc = self._run_exec(
                argv,
                cwd=self.cwd,
                timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
            )
            rc = getattr(proc, "returncode", 1)
            if rc != 0:
                stderr = (getattr(proc, "stderr", "") or "").strip()[:240]
                raise RuntimeError(
                    f"codex exec worker failed rc={rc} stderr={stderr!r}"
                )
            try:
                text = Path(out_path).read_text(encoding="utf-8").strip()
            except OSError:
                text = (getattr(proc, "stdout", "") or "").strip()
        return {"_notifications": notifications_from_text(text)}

    def stop(self) -> None:
        """No persistent child to tear down."""
        return None


__all__ = [
    "DEFAULT_EXEC_SANDBOX",
    "ExecWorkerClient",
    "build_exec_argv",
    "build_exec_prompt",
    "notifications_from_text",
    "resolve_exec_sandbox",
]
