#!/usr/bin/env python3
"""Codex receive-loop daemon (= ms-93 / e-2497 phase 1 entry point).

Runs continuously in the background while a Codex CLI session is open.
Maintains a stable session_id, heartbeats to the server so the
freshness threshold (3600s in lib/session.py) never expires, polls the
bus inbox every few seconds, and persists incoming events for the
inbox hook to inject on the next user prompt.

This is the Codex equivalent of Claude Code's ``channel/bus.mjs``
bridge — the responsibilities are the same but the delivery mechanism
(file → hook, not MCP notification) is adapted to what Codex exposes.

Usage:
    BEACON_BIN=/abs/path/to/beacon python3 scripts/codex-receive-loop.py [--cwd /repo]

Environment knobs:
    BEACON_HEARTBEAT_SECS — heartbeat tick (default 5)
    BEACON_POLL_SECS       — inbox poll tick (default 2)
    BEACON_BUS_SENDER      — exported by this script for child CLI calls

The daemon writes its pid to ``<cwd>/.beacon/codex/receive-loop.pid``
so a second invocation can detect the existing instance and refuse
gracefully (= avoid two daemons fighting for one sid).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _import_modules(install_root: Path):
    """Make ``lib`` importable, return (codex_session, codex_receive_loop,
    api_client, bus_protocol)."""
    lib_dir = str(install_root / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import codex_session as cs  # noqa: E402
    import codex_receive_loop as crl  # noqa: E402
    import api_client as ac  # noqa: E402
    import bus_protocol as bp  # noqa: E402
    return cs, crl, ac, bp


def _import_app_server_client(install_root: Path):
    """Late import of the app-server client (= ms-93 / e-2519, opt-in).

    Kept behind a function so the default daemon doesn't take an
    unconditional import dependency on the spike module.
    """
    plugin_scripts = str(install_root / "plugins" / "beacon" / "scripts")
    if plugin_scripts not in sys.path:
        sys.path.insert(0, plugin_scripts)
    import beacon_codex_app_server_client as ac_mod  # noqa: E402
    return ac_mod


def _load_cloud_config(cwd: Path) -> dict:
    """Read ``<cwd>/.beacon/cloud.json`` + locate the auth token.

    Required outputs: ``project_id``, ``api_url``, ``id_token``.

    The token resolution mirrors the bus.mjs / CLI convention so the
    receive loop works in any setup ``beacon auth login`` produces:

    1. ``BEACON_AUTH_TOKEN`` env var (= explicit override)
    2. ``<cwd>/.beacon/cloud.json`` ``id_token`` (= legacy local copy)
    3. ``~/.beacon/credentials.json`` ``token`` (= default, post auth login)
    4. The active profile's ``~/.beacon/profiles/<active>/credentials.json``
    """
    path = cwd / ".beacon" / "cloud.json"
    if not path.is_file():
        raise RuntimeError(
            f"{path} not found — receive loop needs a cloud-mode project. "
            "Run `beacon cloud setup` in this cwd first."
        )
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    for key in ("project_id", "api_url"):
        if not data.get(key):
            raise RuntimeError(f"{path} is missing '{key}'.")

    if not data.get("id_token"):
        env_token = os.environ.get("BEACON_AUTH_TOKEN", "").strip()
        if env_token:
            data["id_token"] = env_token
    if not data.get("id_token"):
        candidates = [
            Path(os.path.expanduser("~/.beacon/credentials.json")),
        ]
        # Active-profile path: best-effort, ignore IO errors.
        try:
            machine_path = Path(os.path.expanduser("~/.beacon/machine.json"))
            if machine_path.is_file():
                with machine_path.open("r", encoding="utf-8") as f:
                    m = json.load(f)
                active = m.get("active_profile") or m.get("profile") or ""
                if active:
                    candidates.append(
                        Path(os.path.expanduser(
                            f"~/.beacon/profiles/{active}/credentials.json"
                        ))
                    )
        except Exception:
            pass
        for cand in candidates:
            if not cand.is_file():
                continue
            try:
                with cand.open("r", encoding="utf-8") as f:
                    creds = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            token = creds.get("token") or creds.get("id_token") or ""
            if token:
                data["id_token"] = token
                break

    if not data.get("id_token"):
        raise RuntimeError(
            "No auth token found. Run `beacon auth login` first "
            "(checked BEACON_AUTH_TOKEN, cloud.json, ~/.beacon/credentials.json)."
        )
    return data


def _pid_file(cwd: Path) -> Path:
    return cwd / ".beacon" / "codex" / "receive-loop.pid"


def _session_pointer_file(cwd: Path) -> Path:
    """Cwd-level pointer to "the active receive-loop session".

    Codex 2026-06-26 dogfood (= MbZWuaiLRms2U7XF9Pum) found the hook
    cannot reach the session record via parent_pid because the hook
    runs in a different shell than the daemon (= different parent_pid).
    Publishing the sid + project + beacon_bin to a known cwd path lets
    the hook discover them without any pid math.
    """
    return cwd / ".beacon" / "codex" / "receive-loop.session.json"


def _write_pid_file(cwd: Path) -> None:
    path = _pid_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _write_session_pointer(cwd: Path, *, session_id: str, project_id: str,
                            beacon_bin: str, codex_thread_id: str = "",
                            app_server_proxy: bool = False,
                            app_server_url: str = "") -> None:
    """Publish the active session pointer for the hook to read."""
    path = _session_pointer_file(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "project_id": project_id,
        "beacon_bin": beacon_bin,
        "codex_thread_id": codex_thread_id,
        "app_server_proxy": app_server_proxy,
        "app_server_url": app_server_url,
        "written_at": _now_iso_for_pointer(),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _now_iso_for_pointer() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_existing_daemon(cwd: Path) -> int:
    """Return the live pid of an existing daemon, or 0 if none.

    A stale pid file (= process dead) is treated as none and overwritten.
    """
    path = _pid_file(cwd)
    if not path.is_file():
        return 0
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return 0
    except PermissionError:
        return pid
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Codex receive-loop daemon (= ms-93 / e-2497).",
    )
    parser.add_argument("--cwd", default="", help="Override working directory.")
    parser.add_argument(
        "--install-root", default="",
        help="Beacon source checkout (defaults to script-relative).",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration and exit (= for tests).")
    parser.add_argument(
        "--app-server", action="store_true",
        help=(
            "Opt-in: also dispatch each kept DM to a long-lived "
            "`codex app-server --stdio` child (= ms-93 / e-2519 push receive). "
            "Default off keeps the pull-on-prompt path identical to before."
        ),
    )
    parser.add_argument(
        "--armed", action="store_true",
        help=(
            "Opt-in: when `--app-server` is also on, send the Codex agent "
            "reply back to the original DM sender via `beacon bus send "
            "--in-reply-to` (= ms-93 / e-2519 AC 2 + AC 6 autonomous "
            "response loop). Requires the user to have granted bus budget "
            "first (= `beacon bus budget grant N`) — armed mode does not "
            "auto-grant from inside the daemon (= preserves user intent). "
            "Has no effect without --app-server."
        ),
    )
    parser.add_argument(
        "--codex-thread-id",
        default="",
        help=(
            "Opt-in remote UX: with --app-server, resume this existing Codex "
            "app-server thread and deliver DMs as new turns on it instead of "
            "creating a response-worker thread. Env equivalent: "
            "BEACON_CODEX_THREAD_ID."
        ),
    )
    parser.add_argument(
        "--app-server-proxy",
        action="store_true",
        help=(
            "Use `codex app-server proxy` to connect to a running app-server "
            "daemon instead of spawning an ephemeral `--stdio` server. This "
            "is required when targeting a remote Codex TUI thread."
        ),
    )
    parser.add_argument(
        "--app-server-sock",
        default="",
        help=(
            "Optional Unix socket path for `codex app-server proxy --sock`. "
            "Env equivalent: BEACON_CODEX_APP_SERVER_SOCK."
        ),
    )
    parser.add_argument(
        "--app-server-url",
        default="",
        help=(
            "Connect to an already-running Codex app-server endpoint, for "
            "example ws://127.0.0.1:39988. Env equivalent: "
            "BEACON_CODEX_APP_SERVER_URL."
        ),
    )
    args = parser.parse_args()
    if not args.app_server:
        # Env-var equivalent so the bridge CLI can flip the flag without
        # threading argv through the lifecycle subprocess invocation.
        args.app_server = os.environ.get("BEACON_CODEX_APP_SERVER", "").strip() in (
            "1", "true", "yes", "on",
        )
    if not args.armed:
        args.armed = os.environ.get("BEACON_CODEX_ARMED", "").strip() in (
            "1", "true", "yes", "on",
        )
    if args.armed and not args.app_server:
        sys.stderr.write(
            "codex-receive-loop: --armed has no effect without --app-server "
            "(= autonomous reply needs the app-server dispatch path); ignoring.\n"
        )
        args.armed = False
    if not args.codex_thread_id:
        args.codex_thread_id = os.environ.get("BEACON_CODEX_THREAD_ID", "").strip()
    if not args.app_server_proxy:
        args.app_server_proxy = os.environ.get(
            "BEACON_CODEX_APP_SERVER_PROXY", ""
        ).strip() in ("1", "true", "yes", "on")
    if not args.app_server_sock:
        args.app_server_sock = os.environ.get(
            "BEACON_CODEX_APP_SERVER_SOCK", ""
        ).strip()
    if not args.app_server_url:
        args.app_server_url = os.environ.get(
            "BEACON_CODEX_APP_SERVER_URL", ""
        ).strip()
    if args.codex_thread_id and not args.app_server:
        sys.stderr.write(
            "codex-receive-loop: --codex-thread-id has no effect without "
            "--app-server; ignoring.\n"
        )
        args.codex_thread_id = ""
    if args.codex_thread_id and not (args.app_server_proxy or args.app_server_url):
        sys.stderr.write(
            "codex-receive-loop: --codex-thread-id should be used with "
            "--app-server-url or --app-server-proxy so the bridge reaches "
            "the same app-server as the remote TUI; falling back to a local "
            "app-server process will not wake the visible TUI.\n"
        )

    install_root = Path(args.install_root or Path(__file__).resolve().parent.parent)
    cwd = Path(args.cwd or os.getcwd())
    cs, crl, ac, bp = _import_modules(install_root)

    config = _load_cloud_config(cwd)
    project_id = config["project_id"]
    api_url = config["api_url"]
    id_token = config.get("id_token", "")

    api = ac.ApiClient(api_url, token=id_token)

    # Existing-daemon guard.
    existing_pid = _check_existing_daemon(cwd)
    if existing_pid and existing_pid != os.getpid():
        sys.stderr.write(
            f"codex-receive-loop: another instance running (pid={existing_pid}).\n"
            "If you believe it is stale, remove .beacon/codex/receive-loop.pid.\n"
        )
        return 2

    session = cs.acquire_codex_session(
        project_id=project_id,
        cwd=str(cwd),
        parent_pid=os.getppid(),
        pid=os.getpid(),
        beacon_bin=os.environ.get("BEACON_BIN", ""),
    )
    session_file = cs.codex_session_path(
        project_id,
        cs.derive_stable_instance_key(str(cwd), os.getppid()),
    )

    # Export for any child CLI calls the user makes alongside.
    os.environ["BEACON_BUS_SENDER"] = session.session_id

    _write_pid_file(cwd)
    _write_session_pointer(
        cwd,
        session_id=session.session_id,
        project_id=project_id,
        beacon_bin=os.environ.get("BEACON_BIN", ""),
        codex_thread_id=args.codex_thread_id,
        app_server_proxy=args.app_server_proxy,
        app_server_url=args.app_server_url,
    )

    print(
        f"codex-receive-loop: started "
        f"sid={session.session_id} project={project_id} cwd={cwd}",
        flush=True,
    )

    # App-server opt-in (= ms-93 / e-2519 SPEC §8-G option D MVP wiring).
    # When ``--app-server`` is on we keep one ``codex app-server --stdio``
    # child alive for the daemon's lifetime and forward each kept DM to
    # it via ``BridgeAppServerClient.dispatch_dm``. The default pull-on-
    # prompt path is untouched (= callback stays ``None``); only the
    # autonomous push path is added. The armed-mode budget gate lives in
    # a separate Skill (= e-2519 AC 6), so this wiring is intentionally
    # naive about who is allowed to wake the bridge — production must
    # combine it with the gate.
    #
    # Codex 2026-06-26 blocker #2: cold-start ``since=""`` flooded the
    # inbox with weeks of broadcast traffic on the first poll. Pin the
    # in-memory watermark to "now" so the first poll only catches new
    # events; bus.mjs avoids this because it polls the per-recipient
    # queue (server-filtered to DMs), but our endpoint returns broader
    # history and the filter chain runs on our side.
    app_server_client = None
    if args.app_server:
        try:
            ac_mod = _import_app_server_client(install_root)
            app_server_client = ac_mod.BridgeAppServerClient(
                target_thread_id=args.codex_thread_id,
                use_proxy=args.app_server_proxy,
                proxy_sock=args.app_server_sock,
                remote_url=args.app_server_url,
            )
            app_server_client.ensure_started(cwd=str(cwd))
            mode = "remote-thread" if args.codex_thread_id else "worker-thread"
            transport = (
                "url" if args.app_server_url
                else "proxy" if args.app_server_proxy
                else "stdio"
            )
            print(
                "codex-receive-loop: app-server child started "
                f"(thread={app_server_client.thread_id} mode={mode} "
                f"transport={transport})",
                flush=True,
            )
        except Exception as exc:
            # Don't tear down the daemon — log and fall back to pull-only.
            print(
                f"codex-receive-loop: app-server start failed ({exc}); "
                "continuing with pull-on-prompt only.",
                file=sys.stderr,
                flush=True,
            )
            app_server_client = None

    def _on_kept_event(evt):
        """Dispatch a kept DM to the app-server child and log the result.

        Best-effort: any failure is logged and swallowed so the
        pull-on-prompt persistence (= already done by ``poll_inbox_once``
        before this callback fires) remains the authoritative path.

        When ``--armed`` is also on (= e-2519 AC 2 + AC 6), the agent
        reply is sent back to the original sender via ``beacon bus send
        --in-reply-to``. The user must have granted bus budget first;
        budget-exhausted sends fail loudly in the daemon log so the user
        can re-grant.
        """
        if app_server_client is None:
            return
        try:
            rsp = app_server_client.dispatch_dm_and_wait(evt)
            agent_text = ac_mod.agent_message_text_from_notifications(
                rsp.get("_notifications") or []
            )
            event_id = str((evt or {}).get("event_id") or "")
            preview = (agent_text or "").strip().replace("\n", " ")[:160]
            print(
                f"codex-receive-loop: app-server dispatched event={event_id} "
                f"agent_text_preview={preview!r}",
                flush=True,
            )
            crl.ack_event(
                api,
                project_id=project_id,
                event_id=event_id,
                stage="opened",
                recipient_session_id=session.session_id,
            )

            if not args.armed:
                return
            reply_argv = crl.build_dm_reply_args(
                event=evt,
                agent_text=agent_text,
                beacon_bin=os.environ.get("BEACON_BIN") or "beacon",
            )
            if reply_argv is None:
                print(
                    f"codex-receive-loop: armed reply skipped event={event_id} "
                    "(= empty agent_text or missing event_id / sender_session_id)",
                    flush=True,
                )
                return
            reply_env = {
                **os.environ,
                # Pin the sender to this daemon's session so the reply is
                # attributed to the Codex bridge, not whatever beacon CLI
                # would mint on its own.
                "BEACON_BUS_SENDER": session.session_id,
            }
            try:
                result = subprocess.run(
                    reply_argv,
                    env=reply_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception as exc:
                print(
                    f"codex-receive-loop: armed reply exception event={event_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            if result.returncode == 0:
                print(
                    f"codex-receive-loop: armed reply sent event={event_id} "
                    f"to={evt.get('sender_session_id')} (= in_reply_to)",
                    flush=True,
                )
            else:
                stderr_preview = (result.stderr or "").strip().replace("\n", " ")[:240]
                print(
                    f"codex-receive-loop: armed reply failed event={event_id} "
                    f"rc={result.returncode} stderr={stderr_preview!r}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            print(
                f"codex-receive-loop: app-server dispatch failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            # A dead long-lived websocket otherwise poisons every later DM.
            # Reconnect for the next event, but do not retry this event here:
            # turn/start may have reached the server before the transport
            # failed, so retrying could execute the same DM twice. Re-raise so
            # poll_inbox_once persists this event for the hook fallback.
            try:
                app_server_client.stop()
                app_server_client.ensure_started(cwd=str(cwd))
                print(
                    "codex-receive-loop: app-server reconnected after "
                    "dispatch failure",
                    flush=True,
                )
            except Exception as reconnect_exc:
                print(
                    "codex-receive-loop: app-server reconnect failed: "
                    f"{reconnect_exc}",
                    file=sys.stderr,
                    flush=True,
                )
            raise

    state = {
        "stop": False,
        "last_heartbeat_at": 0.0,
        "since": bp.initial_watermark_now(),
    }

    def _on_signal(_signum, _frame):
        state["stop"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Default 5s matches Claude Code's bus.mjs BEACON_BUS_POLL_MS=5000 so the
    # `--live --healthy` directory filter sees the Codex sid as alive on the
    # same cadence (= ms-93 / e-2508 Codex dogfood FINDING #1: 30s heartbeat
    # was below the healthy threshold and the daemon dropped from healthy
    # while still running).
    heartbeat_secs = int(os.environ.get("BEACON_HEARTBEAT_SECS", "5") or "5")
    poll_secs = int(os.environ.get("BEACON_POLL_SECS", "2") or "2")

    actor = {
        "machine": os.uname().nodename if hasattr(os, "uname") else "",
        "agent": "codex",
        "agent_kind": "codex",
    }

    try:
        while not state["stop"]:
            now = time.time()
            if now - state["last_heartbeat_at"] >= heartbeat_secs:
                ok = crl.heartbeat_to_server(
                    api, project_id=project_id,
                    session_id=session.session_id, actor=actor,
                    poll_interval_ms=poll_secs * 1000,
                )
                if ok:
                    cs.heartbeat_codex_session(session_file)
                    print(
                        f"codex-receive-loop: heartbeat ok",
                        flush=True,
                    )
                else:
                    print(
                        f"codex-receive-loop: heartbeat failed (transport)",
                        file=sys.stderr,
                        flush=True,
                    )
                state["last_heartbeat_at"] = now
            latest, persisted = crl.poll_inbox_once(
                api, project_id=project_id,
                session_id=session.session_id,
                since=state["since"], cwd=str(cwd),
                on_kept_event=_on_kept_event if app_server_client else None,
                persist_kept=app_server_client is None,
            )
            state["since"] = latest
            if persisted:
                print(
                    f"codex-receive-loop: persisted {persisted} new event(s)",
                    flush=True,
                )
            if args.once:
                break
            time.sleep(poll_secs)
    finally:
        # Stamp shutdown=true on the server (= bus.mjs equivalent of
        # graceful close) so the directory immediately stops listing
        # this Codex sid as live.
        crl.heartbeat_to_server(
            api, project_id=project_id,
            session_id=session.session_id, actor=actor,
            poll_interval_ms=poll_secs * 1000, shutdown=True,
        )
        if app_server_client is not None:
            try:
                app_server_client.stop()
            except Exception:
                pass
        cs.close_codex_session(session_file, reason="signal")
        try:
            _pid_file(cwd).unlink()
        except OSError:
            pass
        try:
            _session_pointer_file(cwd).unlink()
        except OSError:
            pass
        print(
            f"codex-receive-loop: stopped sid={session.session_id}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
