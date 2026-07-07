"""PowerShell-native Beacon dispatcher (bash-less Python entry-point).

This module reproduces the argv-parsing layer of ``bin/beacon`` (the
1000+ line bash script) for the Phase-1 subcommand set that a Windows
PowerShell user needs on Day 1:

* ``beacon init``
* ``beacon status``
* ``beacon log [...]``
* ``beacon task add|done|list|update|show|cancel|delete|detail``
* ``beacon milestone add|list|start|done|observe|show|update``
* ``beacon doc add|list|show|update|delete``
* ``beacon summary "text"``
* ``beacon note "<text>"`` / ``note list`` / ``note clear``
* ``beacon trigger fire|check|clear``
* ``beacon search "query"``
* ``beacon cycle status``
* ``beacon push record|list``
* ``beacon deploy record|list``
* ``beacon save``  ``beacon entry move``  ``beacon sync``
* ``beacon doctor``  ``beacon project archive|unarchive``
* ``beacon --version`` / ``--help`` / ``help``

It does **not** cover full-fat curses dashboards, tmux launchers, or any
flow that fundamentally needs a TTY shell (these stay bash-only). For
those commands users on bash-less systems get a clear error pointing to
ms-44 follow-up tasks.

Design contract (judgement trail)
---------------------------------

* commands.py reads ``sys.argv[1]`` for the subcommand name plus a long
  list of ``BEACON_*`` env vars for the payload. We MUST preserve that
  exact contract so the bash and Python paths stay swappable. dispatch.py
  is therefore a thin argv→env translator; the business logic stays in
  commands.py.

* When this module is invoked, ``main.py`` has already decided we are on
  the Python path (bash unavailable or ``bin/beacon`` missing). dispatch
  returns the subprocess exit code; it never raises for user errors
  (parser errors print and exit with code 2 — argparse default).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ._version import __version__


# ---------------------------------------------------------------------------
# Subprocess helper — single point of truth for invoking commands.py
# ---------------------------------------------------------------------------


def _run_commands_py(
    root: Path,
    subcmd: str,
    env_overrides: Dict[str, str],
    *,
    extra_args: Optional[Sequence[str]] = None,
) -> int:
    """Spawn ``python3 commands.py <subcmd>`` with the bash-compatible env.

    Parameters
    ----------
    root:
        Repo root (source) or the ``beacon_cli`` package directory (wheel).
        ``commands.py`` and the import-search ``lib/`` directory are
        resolved from this.
    subcmd:
        The first positional argument that commands.py inspects via
        ``sys.argv[1]`` (e.g. ``"task_add"``, ``"milestone_update"``).
    env_overrides:
        Mapping of ``BEACON_*`` keys to string values. Empty strings are
        passed through unchanged so commands.py can detect "not set" via
        ``os.environ.get("BEACON_X", "")``.
    extra_args:
        Any trailing argv tokens to pass after ``subcmd`` (rare — most
        commands.py handlers consume only env vars, but a few like
        ``cloud_check_project`` read ``sys.argv[2]``).
    """
    commands_py = _resolve_commands_py(root)
    lib_dir = _resolve_lib_dir(root)
    if commands_py is None or lib_dir is None:
        _eprint(f"Error: cannot locate commands.py under {root}")
        return 2

    cmd: List[str] = [sys.executable, str(commands_py), subcmd]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env.setdefault("BEACON_PROJECT_FILE", ".beacon/project.json")
    env["BEACON_DIR"] = str(root)

    # #19 follow-up: main.py reconfigures sys.stdout in *this* process at
    # import time, but commands.py runs in a fresh child process where that
    # reconfigure never executes. Force PYTHONUTF8 + PYTHONIOENCODING so
    # Windows cp932 / cp1252 consoles still get UTF-8 stdout/stderr for
    # every subcommand (status, milestone list, search results, …) and
    # not just --help / --version. PEP 540 UTF-8 mode also makes the
    # default open() encoding UTF-8 inside the child, which is a
    # belt-and-suspenders companion to the explicit encoding= audit (#21).
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # commands.py uses flat imports (``from store import ...``). Inject
    # the directory that actually holds ``commands.py`` so those resolve
    # in both the source layout (``lib/``) and the wheel layout
    # (``beacon_cli/_bundled_lib/``).
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{lib_dir}{os.pathsep}{existing_pp}" if existing_pp else str(lib_dir)
    )

    # Apply user overrides on top of inherited env so that an explicit
    # empty string from the parser ("--ms" not given) clears any
    # inherited BEACON_MS_ID from a parent invocation.
    for k, v in env_overrides.items():
        env[k] = v if v is not None else ""

    try:
        return subprocess.call(cmd, env=env)
    except OSError as exc:
        _eprint(f"Error launching commands.py dispatch: {exc}")
        return 2


def _resolve_commands_py(root: Path) -> Optional[Path]:
    candidate = root / "lib" / "commands.py"
    if candidate.exists():
        return candidate
    candidate = root / "_bundled_lib" / "commands.py"
    if candidate.exists():
        return candidate
    return None


def _resolve_lib_dir(root: Path) -> Optional[Path]:
    if (root / "lib" / "commands.py").exists():
        return root / "lib"
    if (root / "_bundled_lib" / "commands.py").exists():
        return root / "_bundled_lib"
    return None


def _eprint(*args, **kwargs) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# ensure_project — same guard bin/beacon uses for project-scoped commands
# ---------------------------------------------------------------------------


def _ensure_project() -> Optional[int]:
    """Return None if a project is present, else an exit code to bubble up.

    Mirrors the bash ``ensure_project`` helper. Uses ``BEACON_PROJECT_FILE``
    so callers (or tests) can redirect to a fixture path.
    """
    project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
    if not Path(project_file).exists():
        print("Error: No .beacon/project.json found.")
        print("Run 'beacon init' first.")
        return 1
    return None


def _purge_dispatch(root: Path, subcmd: str, *, id_value: str, id_env: str,
                    reason: str, index: str, json_flag: bool, usage: str) -> int:
    """Shared driver for the *purge recovery commands (e-893).

    purge is a recovery path: it must run even when the project fails
    validation, so — unlike every other handler — it does NOT call
    _ensure_project. commands.py loads via load_project_unsafe. ``reason``
    is mandatory (audit trail per data-immutability-principle).
    """
    if not id_value:
        print(f"Usage: {usage}")
        return 1
    if not reason:
        print(f"Error: --reason is required for {subcmd.replace('_', ' ')} "
              "(audit trail per data-immutability-principle).")
        return 1
    env = {
        id_env: id_value,
        "BEACON_REASON": reason or "",
        "BEACON_INDEX": index or "",
        "BEACON_JSON": "1" if json_flag else "",
    }
    return _run_commands_py(root, subcmd, env)


def _find_beacon_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for ``.beacon/project.json``.

    Returns the directory that contains ``.beacon/`` (the project root), or
    None if no ancestor has one. Same contract as the bash ``find_beacon_root``
    and the postcompact hook's ``_find_beacon_root``: beacon commands should
    work from any subdirectory of the project, just like ``git`` does (e-862).
    """
    cur = start.resolve()
    while True:
        if (cur / ".beacon" / "project.json").is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


# e-1227 (ms-17): the cwd at the time the user invoked ``beacon``. We have
# to stash this before ``_relocate_to_project_root`` chdirs us up to the
# beacon project root, because in worktree mode the project root is the
# *parent* repo (only the parent has ``.beacon/``). Any handler that needs
# to read git HEAD must use this cwd via ``git -C``, otherwise it captures
# the parent repo's HEAD instead of the worktree's HEAD — silently
# attaching the wrong commit hash to beacon entries.
_invocation_cwd: Optional[Path] = None


def get_invocation_cwd() -> Path:
    """Return the cwd from which the user invoked ``beacon``.

    Falls back to ``Path.cwd()`` if called before ``dispatch()`` had a
    chance to stash it (e.g. unit tests calling handlers directly).
    """
    return _invocation_cwd if _invocation_cwd is not None else Path.cwd()


def _relocate_to_project_root(command: str) -> None:
    """chdir to the project root when invoked from a subdirectory (e-862).

    Without this, every project-scoped command fails with
    "No .beacon/project.json found" the moment the user cd's into a
    subdirectory, even though the project root is right above them. We walk
    up like git and relocate so all relative ``.beacon/...`` paths (and the
    commands.py subprocess that inherits this cwd) resolve correctly.

    Exemptions:
      * ``init`` / ``setup`` intentionally create/operate on the *current*
        directory — never relocate them.
      * An explicit ``BEACON_PROJECT_FILE`` pointing at an existing file
        (tests, fixtures, advanced users) wins — leave cwd untouched.
      * If ``.beacon/project.json`` already exists in cwd, nothing to do.

    Note: callers that need to resolve git HEAD from the *original* cwd
    (e.g. ``beacon log`` from inside a git worktree where ``.beacon/`` lives
    in the parent repo) should read ``get_invocation_cwd()`` and pass it
    explicitly to ``git -C`` — see e-1227 / ms-17 / ``_handle_log``.
    """
    if command in ("init", "setup"):
        return
    explicit = os.environ.get("BEACON_PROJECT_FILE")
    if explicit and Path(explicit).exists():
        return
    if Path(".beacon/project.json").exists():
        return
    root = _find_beacon_root(Path.cwd())
    if root is not None:
        os.chdir(root)


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse tree for Phase-1 commands.

    The structure intentionally mirrors ``bin/beacon``'s case statements.
    When a subcommand has further nested subcommands (e.g. ``task add``),
    we use nested subparsers.

    Help strings are kept terse — the canonical docs are in
    ``bin/beacon``'s ``usage`` function. ``beacon help`` still routes to
    that text when bash is available.
    """
    p = argparse.ArgumentParser(
        prog="beacon",
        description="Beacon — AI-driven milestone tracker (PowerShell-native dispatch)",
        add_help=False,  # we handle --help/-h ourselves to keep both paths consistent
    )
    p.add_argument("--version", "-V", action="store_true", help="Show version")
    p.add_argument("--help", "-h", action="store_true", help="Show help")
    # ms-64 / e-1458: top-level --profile <name>. Exported as BEACON_PROFILE
    # before any subcommand runs, so the profile resolver (lib/profile.py)
    # honors it for api_url + credentials path resolution. Parity with
    # bin/beacon's bash-side parser.
    p.add_argument("--profile", default="", help="Beacon profile to use (overrides BEACON_PROFILE env)")

    sub = p.add_subparsers(dest="command", metavar="<command>")

    # ---- init ----
    p_init = sub.add_parser("init", help="Initialize .beacon/ in current dir", add_help=False)
    p_init.add_argument("--name")
    p_init.add_argument("--objective")
    p_init.add_argument("--retro-day", dest="retro_day")
    p_init.add_argument("--storage", default="local")
    # ms-63 / e-1441: project disclosure posture at init time. Default is
    # ``high`` per SPEC § 設計方針 2 — opt-in low only when the project is
    # explicitly open (= OSS, public docs).
    p_init.add_argument(
        "--sensitivity",
        choices=("high", "low"),
        default="high",
        help="disclosure posture: high (default, schema-only T5 replies) "
             "or low (open project, free-text T5 replies)",
    )
    p_init.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- status ----
    p_status = sub.add_parser("status", help="Show milestone status", add_help=False)
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--all", "-a", action="store_true")
    p_status.add_argument("--ms", action="append", default=[])
    p_status.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- summary ----
    p_summary = sub.add_parser("summary", help="Update project summary", add_help=False)
    p_summary.add_argument("text", nargs="*")
    p_summary.add_argument("--json", action="store_true")
    p_summary.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- log ----
    p_log = sub.add_parser("log", help="Record HEAD commit to active milestone", add_help=False)
    p_log.add_argument("-m", "--ms", dest="ms_id", default="")
    p_log.add_argument("-p", "--progress", default="")
    p_log.add_argument("--json", action="store_true")
    p_log.add_argument("--prepare", action="store_true")
    p_log.add_argument("--finalize", action="store_true")
    p_log.add_argument("--summary", default="")
    p_log.add_argument("--hash", dest="explicit_hash", default="")
    p_log.add_argument("--behavior", default="")
    p_log.add_argument("--resolves", default="")
    p_log.add_argument("message", nargs="?", default="")
    p_log.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- save ----
    p_save = sub.add_parser("save", help="Manual progress note", add_help=False)
    p_save.add_argument("description", nargs="?", default="")
    p_save.add_argument("-m", "--ms", dest="ms_id", default="")
    p_save.add_argument("--source", default="")
    p_save.add_argument("--url", default="")
    p_save.add_argument("--revision-id", dest="revision_id", default="")
    p_save.add_argument("--hash", dest="hash", default="")
    p_save.add_argument("-p", "--progress", default="")
    p_save.add_argument("--json", action="store_true")
    p_save.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- sync ----
    p_sync = sub.add_parser("sync", help="Auto-sync recent git commits", add_help=False)
    p_sync.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- task ----
    p_task = sub.add_parser("task", help="Task operations", add_help=False)
    p_task.add_argument("--help", "-h", action="store_true", dest="show_help")
    task_sub = p_task.add_subparsers(dest="task_cmd", metavar="<subcmd>")

    p_task_add = task_sub.add_parser("add", add_help=False)
    # ms-44 e-1192: default=None so the handler can distinguish
    # "user supplied --flag" from "user did not supply this flag, fall
    # through to any pre-set BEACON_* env var". Previously default=""
    # forced every flag to override the inherited env, which on Windows
    # silently zeroed BEACON_DESCRIPTION etc when users tried to seed the
    # values via env var. See _handle_task for the matching read path.
    p_task_add.add_argument("description", nargs="?", default=None)
    p_task_add.add_argument("-m", "--ms", dest="ms_id", default=None)
    p_task_add.add_argument("-t", "--type", dest="entry_type", default=None)
    p_task_add.add_argument("-d", "--detail", default=None)
    p_task_add.add_argument("--from", dest="requested_by", default=None)
    p_task_add.add_argument("--priority", default=None)
    p_task_add.add_argument("--motivation", "--why", dest="motivation", default=None)
    p_task_add.add_argument(
        "--acceptance-criteria", "--ac", dest="acceptance_criteria", default=None
    )

    p_task_done = task_sub.add_parser("done", add_help=False)
    p_task_done.add_argument("entry_id", nargs="?", default="")
    p_task_done.add_argument("-p", "--progress", default="")
    # e-976: default=None — see p_ms_observe.
    p_task_done.add_argument("-r", "--reason", default=None)

    p_task_list = task_sub.add_parser("list", aliases=["ls"], add_help=False)
    p_task_list.add_argument("-m", "--ms", dest="ms_id", default="")
    p_task_list.add_argument("--json", action="store_true")
    p_task_list.add_argument("--all", "-a", action="store_true")
    p_task_list.add_argument("--type", "-t", dest="type_filter", default="")

    p_task_show = task_sub.add_parser("show", add_help=False)
    p_task_show.add_argument("entry_id", nargs="?", default="")
    p_task_show.add_argument("--json", action="store_true")

    p_task_detail = task_sub.add_parser("detail", add_help=False)
    p_task_detail.add_argument("entry_id", nargs="?", default="")
    p_task_detail.add_argument("detail_text", nargs="?", default="")

    p_task_update = task_sub.add_parser("update", add_help=False)
    p_task_update.add_argument("entry_id", nargs="?", default="")
    p_task_update.add_argument("--json", action="store_true")
    p_task_update.add_argument("--description", default="")
    p_task_update.add_argument("--status", default="")
    p_task_update.add_argument("-d", "--detail", default="")
    p_task_update.add_argument("-m", "--ms", dest="ms_id", default="")
    p_task_update.add_argument("--motivation", "--why", dest="motivation", default="")
    p_task_update.add_argument(
        "--acceptance-criteria", "--ac", dest="acceptance_criteria", default=""
    )
    p_task_update.add_argument("--behavior", default="")
    p_task_update.add_argument("--priority", "-p", default="")

    p_task_cancel = task_sub.add_parser("cancel", add_help=False)
    p_task_cancel.add_argument("entry_id", nargs="?", default="")
    p_task_cancel.add_argument("-r", "--reason", default="")

    p_task_delete = task_sub.add_parser("delete", add_help=False)
    p_task_delete.add_argument("entry_id", nargs="?", default="")
    p_task_delete.add_argument("--json", action="store_true")

    # ---- milestone ----
    p_ms = sub.add_parser("milestone", aliases=["ms"], help="Milestone operations", add_help=False)
    p_ms.add_argument("--help", "-h", action="store_true", dest="show_help")
    ms_sub = p_ms.add_subparsers(dest="ms_cmd", metavar="<subcmd>")

    p_ms_add = ms_sub.add_parser("add", add_help=False)
    p_ms_add.add_argument("title", nargs="?", default="")
    p_ms_add.add_argument("-d", dest="target_date", default="")
    p_ms_add.add_argument(
        "--description", "--desc", dest="description", default=""
    )
    p_ms_add.add_argument("--priority", default="")
    p_ms_add.add_argument("--objective", default="")
    p_ms_add.add_argument(
        "--acceptance-criteria", "--ac", dest="acceptance_criteria", default=""
    )
    p_ms_add.add_argument("--owner", default="")
    p_ms_add.add_argument("--assignee", default="")

    ms_sub.add_parser("list", aliases=["ls"], add_help=False)

    p_ms_start = ms_sub.add_parser("start", add_help=False)
    p_ms_start.add_argument("ms_id", nargs="?", default="")
    p_ms_start.add_argument("--no-branch", dest="no_branch", action="store_true")
    p_ms_start.add_argument("--no-assignee", dest="no_assignee", action="store_true")

    p_ms_done = ms_sub.add_parser("done", aliases=["close"], add_help=False)
    p_ms_done.add_argument("ms_id", nargs="?", default="")
    # e-976: default=None — see p_ms_observe.
    p_ms_done.add_argument("-r", "--reason", default=None)

    p_ms_join = ms_sub.add_parser("join", add_help=False)
    p_ms_join.add_argument("ms_id", nargs="?", default="")
    p_ms_join.add_argument("--checkout", dest="checkout", action="store_true")

    p_ms_observe = ms_sub.add_parser("observe", add_help=False)
    p_ms_observe.add_argument("ms_id", nargs="?", default="")
    # e-976: default=None so the env builder can tell --reason omitted
    # ('refuse') apart from --reason "" (explicit waiver).
    p_ms_observe.add_argument("-r", "--reason", default=None)

    p_ms_show = ms_sub.add_parser("show", add_help=False)
    p_ms_show.add_argument("ms_id", nargs="?", default="")
    p_ms_show.add_argument("--json", action="store_true")

    p_ms_update = ms_sub.add_parser("update", add_help=False)
    p_ms_update.add_argument("ms_id", nargs="?", default="")
    p_ms_update.add_argument("--json", action="store_true")
    p_ms_update.add_argument("--title", default="")
    p_ms_update.add_argument("-p", "--progress", default="")
    p_ms_update.add_argument("--target-date", dest="target_date", default="")
    p_ms_update.add_argument("--status", default="")
    p_ms_update.add_argument(
        "--description", "--desc", dest="description", default=""
    )
    p_ms_update.add_argument("--priority", default="")
    p_ms_update.add_argument("--objective", default="")
    p_ms_update.add_argument(
        "--acceptance-criteria", "--ac", dest="acceptance_criteria", default=""
    )
    p_ms_update.add_argument("--owner", default="")
    p_ms_update.add_argument("--assignee", default="")
    p_ms_update.add_argument("-r", "--reason", default="")

    # Dependency graph + worktree lifecycle (used by /beacon-dispatch). These
    # exist in commands.py but were missing from the PowerShell-native dispatch
    # (e-842), making /beacon-dispatch unusable on Windows/OSS.
    p_ms_graph = ms_sub.add_parser("graph", add_help=False)
    p_ms_graph.add_argument("--json", action="store_true")

    p_ms_workspace = ms_sub.add_parser("workspace", add_help=False)
    p_ms_workspace.add_argument("ms_id", nargs="?", default="")
    p_ms_workspace.add_argument("--executor", default="")
    p_ms_workspace.add_argument("--workspace", "--dir", dest="workspace", default="")
    p_ms_workspace.add_argument("--clear", action="store_true")
    p_ms_workspace.add_argument("--no-git", dest="no_git", action="store_true")
    p_ms_workspace.add_argument("--json", action="store_true")

    p_ms_wscleanup = ms_sub.add_parser("workspace-cleanup", add_help=False)
    p_ms_wscleanup.add_argument("ms_id", nargs="?", default="")
    p_ms_wscleanup.add_argument("--merge-to", dest="merge_to", default="")
    p_ms_wscleanup.add_argument("--json", action="store_true")

    p_ms_purge = ms_sub.add_parser("purge", add_help=False)
    p_ms_purge.add_argument("ms_id", nargs="?", default="")
    p_ms_purge.add_argument("-r", "--reason", default="")
    p_ms_purge.add_argument("--index", default="")
    p_ms_purge.add_argument("--json", action="store_true")

    # ---- doc ----
    p_doc = sub.add_parser("doc", aliases=["document"], help="Document operations", add_help=False)
    p_doc.add_argument("--help", "-h", action="store_true", dest="show_help")
    doc_sub = p_doc.add_subparsers(dest="doc_cmd", metavar="<subcmd>")

    p_doc_add = doc_sub.add_parser("add", add_help=False)
    p_doc_add.add_argument("title", nargs="?", default="")
    p_doc_add.add_argument("--id", dest="doc_id", default="")
    p_doc_add.add_argument("--scope", "-s", dest="scope", default="")
    p_doc_add.add_argument("--ms", dest="doc_ms", default="")
    p_doc_add.add_argument("--op", dest="doc_op", default="")
    p_doc_add.add_argument("--trek", dest="doc_trek", default="")
    p_doc_add.add_argument("--json", action="store_true")
    p_doc_add.add_argument("--content", default="")
    p_doc_add.add_argument("--stdin", action="store_true")
    # ms-54 / e-1293: persistence poisoning defense — callers that took
    # bus-derived input MUST pass this flag so the handler refuses.
    p_doc_add.add_argument("--bus-origin", dest="bus_origin",
                           action="store_true")

    p_doc_list = doc_sub.add_parser("list", aliases=["ls"], add_help=False)
    p_doc_list.add_argument("--json", action="store_true")
    p_doc_list.add_argument("--scope", "-s", dest="scope", default="")
    p_doc_list.add_argument("--ms", dest="doc_ms", default="")
    p_doc_list.add_argument("--op", dest="doc_op", default="")
    # ms-75 / e-1866: filter to docs tagged with a specific trek_id
    # (= frontmatter trek_id == <value>). Mirrors the ``--ms`` / ``--op``
    # filters; orthogonal to scope/ms/op narrowing so the user can ask
    # "show all spec docs tagged to this trek".
    p_doc_list.add_argument("--trek", dest="doc_trek", default="")

    p_doc_show = doc_sub.add_parser("show", aliases=["get"], add_help=False)
    p_doc_show.add_argument("doc_id", nargs="?", default="")
    p_doc_show.add_argument("--json", action="store_true")

    p_doc_update = doc_sub.add_parser("update", add_help=False)
    p_doc_update.add_argument("doc_id", nargs="?", default="")
    p_doc_update.add_argument("--content", default="")
    p_doc_update.add_argument("--title", default="")
    p_doc_update.add_argument("--scope", "-s", dest="scope", default="")
    p_doc_update.add_argument("--ms", dest="doc_ms", default="")
    p_doc_update.add_argument("--op", dest="doc_op", default="")
    p_doc_update.add_argument("--trek", dest="doc_trek", default="")
    p_doc_update.add_argument("--json", action="store_true")
    p_doc_update.add_argument("--stdin", action="store_true")
    # ms-54 / e-1293: persistence poisoning defense.
    p_doc_update.add_argument("--bus-origin", dest="bus_origin",
                              action="store_true")

    p_doc_delete = doc_sub.add_parser("delete", aliases=["rm"], add_help=False)
    p_doc_delete.add_argument("doc_id", nargs="?", default="")
    p_doc_delete.add_argument("--json", action="store_true")

    # ---- note ----
    p_note = sub.add_parser("note", help="Session note operations", add_help=False)
    p_note.add_argument("text_or_sub", nargs="?", default="")
    p_note.add_argument("--context", default="")
    p_note.add_argument("--json", action="store_true")
    # ms-54 / e-1293: persistence poisoning defense.
    p_note.add_argument("--bus-origin", dest="bus_origin",
                        action="store_true")
    p_note.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- trigger ----
    p_trigger = sub.add_parser("trigger", help="Trigger queue operations", add_help=False)
    p_trigger.add_argument("--help", "-h", action="store_true", dest="show_help")
    trigger_sub = p_trigger.add_subparsers(dest="trigger_cmd", metavar="<subcmd>")

    p_trig_fire = trigger_sub.add_parser("fire", add_help=False)
    p_trig_fire.add_argument("name", nargs="?", default="")
    p_trig_fire.add_argument("message", nargs="?", default="")

    trigger_sub.add_parser("check", add_help=False)

    p_trig_clear = trigger_sub.add_parser("clear", add_help=False)
    p_trig_clear.add_argument("name", nargs="?", default="")

    # ---- search ----
    p_search = sub.add_parser("search", help="Unified search", add_help=False)
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("-m", "--ms", dest="ms_id", default="")
    p_search.add_argument("-o", "--op", dest="op_id", default="")
    p_search.add_argument("--id", dest="entry_id", default="")
    p_search.add_argument("--scope", default="")
    p_search.add_argument("--assignee", default="")
    p_search.add_argument("--owner", default="")
    p_search.add_argument("--from", dest="from_date", default="")
    p_search.add_argument("--to", dest="to_date", default="")
    p_search.add_argument("--type", default="")
    p_search.add_argument("--status", default="")
    p_search.add_argument("--priority", default="")
    p_search.add_argument("--limit", default="")
    p_search.add_argument("--offset", default="")
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--help", "-h", action="store_true", dest="show_help")

    # ---- cycle ----
    p_cycle = sub.add_parser("cycle", help="Cycle status", add_help=False)
    p_cycle.add_argument("--help", "-h", action="store_true", dest="show_help")
    cycle_sub = p_cycle.add_subparsers(dest="cycle_cmd", metavar="<subcmd>")
    p_cycle_status = cycle_sub.add_parser("status", add_help=False)
    p_cycle_status.add_argument("--json", action="store_true")

    # ---- push ----
    p_push = sub.add_parser("push", help="Push log", add_help=False)
    p_push.add_argument("--help", "-h", action="store_true", dest="show_help")
    push_sub = p_push.add_subparsers(dest="push_cmd", metavar="<subcmd>")

    p_push_record = push_sub.add_parser("record", add_help=False)
    p_push_record.add_argument("--prepare", action="store_true")
    p_push_record.add_argument("--finalize", action="store_true")
    p_push_record.add_argument("--from", dest="from_hash", default="")
    p_push_record.add_argument("--to", dest="to_hash", default="")
    p_push_record.add_argument("--branch", default="")
    p_push_record.add_argument("--desc", default="")
    p_push_record.add_argument("-m", "--ms", dest="ms_id", default="")

    p_push_list = push_sub.add_parser("list", add_help=False)
    p_push_list.add_argument("--json", action="store_true")

    # ---- deploy ----
    p_deploy = sub.add_parser("deploy", help="Deploy log", add_help=False)
    p_deploy.add_argument("--help", "-h", action="store_true", dest="show_help")
    deploy_sub = p_deploy.add_subparsers(dest="deploy_cmd", metavar="<subcmd>")

    p_dep_record = deploy_sub.add_parser("record", add_help=False)
    p_dep_record.add_argument("--prepare", action="store_true")
    p_dep_record.add_argument("--finalize", action="store_true")
    p_dep_record.add_argument("--revision", default="")
    p_dep_record.add_argument("--semver", default="")
    p_dep_record.add_argument("--desc", default="")
    p_dep_record.add_argument("--hash", dest="hash", default="")
    p_dep_record.add_argument("--date", default="")
    p_dep_record.add_argument("--insert-before", dest="insert_before", default="")
    p_dep_record.add_argument("--type", default="")
    p_dep_record.add_argument("--env", default="")

    p_dep_list = deploy_sub.add_parser("list", add_help=False)
    p_dep_list.add_argument("--env", default="")
    p_dep_list.add_argument("--json", action="store_true")

    # ---- entry move ----
    p_entry = sub.add_parser("entry", help="Entry operations", add_help=False)
    p_entry.add_argument("--help", "-h", action="store_true", dest="show_help")
    entry_sub = p_entry.add_subparsers(dest="entry_cmd", metavar="<subcmd>")
    p_entry_move = entry_sub.add_parser("move", add_help=False)
    p_entry_move.add_argument("entry_id", nargs="?", default="")
    p_entry_move.add_argument("-t", "--task", dest="task_id", default="")
    p_entry_move.add_argument("-m", "--ms", dest="ms_id", default="")

    p_entry_purge = entry_sub.add_parser("purge", add_help=False)
    p_entry_purge.add_argument("entry_id", nargs="?", default="")
    p_entry_purge.add_argument("-r", "--reason", default="")
    p_entry_purge.add_argument("--index", default="")
    p_entry_purge.add_argument("--json", action="store_true")

    # ---- operation (purge + envelope verify — full CRUD remains bash-only) ----
    p_op = sub.add_parser(
        "operation", help="Operation operations (purge, envelope verify)",
        add_help=False,
    )
    p_op.add_argument("--help", "-h", action="store_true", dest="show_help")
    op_sub = p_op.add_subparsers(dest="op_cmd", metavar="<subcmd>")
    p_op_purge = op_sub.add_parser("purge", add_help=False)
    p_op_purge.add_argument("op_id", nargs="?", default="")
    p_op_purge.add_argument("-r", "--reason", default="")
    p_op_purge.add_argument("--index", default="")
    p_op_purge.add_argument("--json", action="store_true")

    # ms-73 / e-1764: AI self-check primitive for the envelope path
    # (ms-60 e-1340). `beacon operation envelope verify <op-id> <action>`
    # asks "is this action permitted by the active envelope?" — the
    # /beacon-operation-execute Skill calls it before running each
    # action. Without Win parity, autonomous operations on Windows pipx
    # could not perform the pre-flight authorization check.
    p_op_envelope = op_sub.add_parser("envelope", add_help=False)
    p_op_envelope_sub = p_op_envelope.add_subparsers(
        dest="op_envelope_cmd", metavar="<subcmd>"
    )
    p_op_envelope_verify = p_op_envelope_sub.add_parser("verify", add_help=False)
    p_op_envelope_verify.add_argument("op_id", nargs="?", default="")
    p_op_envelope_verify.add_argument("action", nargs="?", default="")
    p_op_envelope_verify.add_argument("--json", action="store_true")

    # ---- trek (ms-69) ----
    # Top-level cross-project collaboration area. Storage is local-only
    # (~/.beacon/treks/) until the HTTP path lands in e-1656.
    p_trek = sub.add_parser("trek", help="Trek operations (ms-69)", add_help=False)
    p_trek.add_argument("--help", "-h", action="store_true", dest="show_help")
    trek_sub = p_trek.add_subparsers(dest="trek_cmd", metavar="<subcmd>")

    p_trek_create = trek_sub.add_parser("create", add_help=False)
    p_trek_create.add_argument("title", nargs="?", default="")
    p_trek_create.add_argument("--type", dest="trek_type", default="")
    p_trek_create.add_argument("--description", "--desc", dest="trek_desc", default="")
    # ms-75 / e-1865: optional acceptance criterion at creation time.
    p_trek_create.add_argument("--goal-state", dest="goal_state", default="")
    p_trek_create.add_argument("--json", action="store_true")

    p_trek_list = trek_sub.add_parser("list", aliases=["ls"], add_help=False)
    p_trek_list.add_argument("--status", default="")
    p_trek_list.add_argument("--all", "--include-archived",
                             dest="include_archived", action="store_true")
    p_trek_list.add_argument("--all-actors", dest="all_actors", action="store_true")
    # ms-75 / e-1813: filter to treks the current user has actually joined
    # (= joined_at non-empty for the calling user). Skipping invitees that
    # have not accepted yet — they show up via plain list / status.
    p_trek_list.add_argument("--joined", dest="joined_only", action="store_true",
                             help="Show only treks the current user has joined")
    p_trek_list.add_argument("--json", action="store_true")

    p_trek_show = trek_sub.add_parser("show", aliases=["get"], add_help=False)
    p_trek_show.add_argument("trek_id", nargs="?", default="")
    # ms-75 / e-1864: uncap the recent-task / commit lists in the aggregation
    # block. Default caps lists at 10 / 5 for readability.
    p_trek_show.add_argument("--all", "--detail", dest="show_all",
                             action="store_true",
                             help="Expand all entries in the aggregation block")
    p_trek_show.add_argument("--json", action="store_true")

    p_trek_start = trek_sub.add_parser("start", add_help=False)
    p_trek_start.add_argument("trek_id", nargs="?", default="")
    p_trek_start.add_argument("--json", action="store_true")

    p_trek_archive = trek_sub.add_parser("archive", add_help=False)
    p_trek_archive.add_argument("trek_id", nargs="?", default="")
    p_trek_archive.add_argument("--json", action="store_true")

    p_trek_invite = trek_sub.add_parser("invite", add_help=False)
    p_trek_invite.add_argument("trek_id", nargs="?", default="")
    p_trek_invite.add_argument("--actor", default="")
    p_trek_invite.add_argument("--notify", action="store_true")
    p_trek_invite.add_argument("--json", action="store_true")

    p_trek_join = trek_sub.add_parser("join", add_help=False)
    p_trek_join.add_argument("trek_id", nargs="?", default="")
    p_trek_join.add_argument("--json", action="store_true")
    # ms-88 / e-2090 — Trek 参加 = scope 内 DM blanket 自動承認 (= ms-70 / e-1854)
    # + autonomous loop 入場の合算で turn 制限なく AI を動かす権限委譲。 CLI 直叩きで
    # 無音成立すると user が consequence を理解せず参加する構造的危険があるため、
    # 明示同意 gate を挟む。 long flag は意味理解の forcing function (= short flag
    # では typo / 反射的 -y で bypass される温床)。
    p_trek_join.add_argument(
        "--i-understand-the-implications", dest="consent_ack",
        action="store_true",
        help="Skip the typed-ack prompt (= 自動化 / bot 用 bypass)。 "
             "Trek 参加が turn 制限なく AI を走らせる権限委譲であることを "
             "理解している前提で使う。",
    )
    # ms-75 / e-2047 — opt-out flag for the post-join auto-arm sequence.
    p_trek_join.add_argument("--no-arm", dest="no_arm", action="store_true")

    p_trek_leave = trek_sub.add_parser("leave", add_help=False)
    p_trek_leave.add_argument("trek_id", nargs="?", default="")
    p_trek_leave.add_argument("--json", action="store_true")

    p_trek_plan = trek_sub.add_parser("plan", add_help=False)
    p_trek_plan.add_argument("trek_id", nargs="?", default="")
    p_trek_plan.add_argument("--add-scope", dest="add_scope", default="")
    p_trek_plan.add_argument("--remove-scope", dest="remove_scope", default="")
    # ms-75 / e-1865: optional goal_state setter. Sentinel value separately
    # records "user passed --goal-state" so we can distinguish empty string
    # (= clear) from "did not pass" (= preserve existing).
    p_trek_plan.add_argument("--goal-state", dest="goal_state", default=None)
    p_trek_plan.add_argument("--json", action="store_true")

    # ms-75 / e-1867: chronological view of trek-scoped events.
    p_trek_timeline = trek_sub.add_parser("timeline", add_help=False)
    p_trek_timeline.add_argument("trek_id", nargs="?", default="")
    p_trek_timeline.add_argument("--limit", default="50",
                                 help="Cap event count (default: 50)")
    p_trek_timeline.add_argument("--json", action="store_true")

    p_trek_stop = trek_sub.add_parser("stop", add_help=False)
    p_trek_stop.add_argument("trek_id", nargs="?", default="")
    p_trek_stop.add_argument("--reason", default="")
    p_trek_stop.add_argument("--json", action="store_true")

    p_trek_resume = trek_sub.add_parser("resume", add_help=False)
    p_trek_resume.add_argument("trek_id", nargs="?", default="")
    p_trek_resume.add_argument("--json", action="store_true")

    p_trek_xfer = trek_sub.add_parser("transfer-leader", add_help=False)
    p_trek_xfer.add_argument("trek_id", nargs="?", default="")
    p_trek_xfer.add_argument("--to", dest="to_session_id", default="")
    p_trek_xfer.add_argument("--json", action="store_true")

    # ms-88 / e-2089 — fresh session recovery (dead leader_session_id 引き継ぎ)
    p_trek_take_over = trek_sub.add_parser("take-over", add_help=False)
    p_trek_take_over.add_argument("trek_id", nargs="?", default="")
    p_trek_take_over.add_argument("--json", action="store_true")

    # ms-88 / e-2138 — Kickoff Ritual completion stamp (= /beacon-trek-pulse Step 0.4)
    p_trek_kickoff = trek_sub.add_parser("kickoff", add_help=False)
    p_trek_kickoff.add_argument("trek_id", nargs="?", default="")
    p_trek_kickoff.add_argument("--session-id", dest="session_id_override",
                                 default="")
    p_trek_kickoff.add_argument("--kickoff-dm-event-id",
                                 dest="kickoff_dm_event_id", default="")
    p_trek_kickoff.add_argument("--json", action="store_true")

    # ms-88 / e-2106 — pulse-ack (= /beacon-trek-pulse self-report)
    p_trek_pulse = trek_sub.add_parser("pulse-ack", add_help=False)
    p_trek_pulse.add_argument("trek_id", nargs="?", default="")
    p_trek_pulse.add_argument("--picked-choice", dest="picked_choice",
                              default="")
    p_trek_pulse.add_argument("--note", default="")
    p_trek_pulse.add_argument("--json", action="store_true")

    # ms-88 / e-2167 — reconcile task pool ↔ Trek stamp 同期
    p_trek_reconcile = trek_sub.add_parser("reconcile", add_help=False)
    p_trek_reconcile.add_argument("trek_id", nargs="?", default="")
    p_trek_reconcile.add_argument("--apply", action="store_true")
    p_trek_reconcile.add_argument("--json", action="store_true")

    # ms-75 / e-2048 — Trek task state machine declaration
    p_trek_state = trek_sub.add_parser("task-state", add_help=False)
    p_trek_state.add_argument("trek_id", nargs="?", default="")
    p_trek_state.add_argument("task_id", nargs="?", default="")
    p_trek_state.add_argument("state", nargs="?", default="")
    p_trek_state.add_argument("--note", default="")
    p_trek_state.add_argument("--json", action="store_true")

    # ms-97 / e-2626 (AC23) — canonical scope-add verb (= flag-style alias of
    # plan --add-scope <project>:<ref>). The action delegates to cmd_trek_plan
    # so the staging machinery (= pending_scope_ops + scope-approve flow) is
    # shared with the existing --add-scope path.
    p_trek_scope_add = trek_sub.add_parser("scope-add", add_help=False)
    p_trek_scope_add.add_argument("trek_id", nargs="?", default="")
    p_trek_scope_add.add_argument("--project", dest="scope_add_project",
                                  default="")
    p_trek_scope_add.add_argument("--milestone", "--ms",
                                  dest="scope_add_milestone", default="")
    p_trek_scope_add.add_argument("--operation", "--op",
                                  dest="scope_add_operation", default="")
    p_trek_scope_add.add_argument("--task", "--e",
                                  dest="scope_add_task", default="")
    p_trek_scope_add.add_argument("--json", action="store_true")

    # ms-97 / e-2611 (AC25) — commit a staged scope op.
    p_trek_scope_approve = trek_sub.add_parser("scope-approve", add_help=False)
    p_trek_scope_approve.add_argument("trek_id", nargs="?", default="")
    p_trek_scope_approve.add_argument("pending_id", nargs="?", default="")
    p_trek_scope_approve.add_argument("--json", action="store_true")

    # ms-97 / e-2611 (AC25) — drop a staged scope op without applying.
    p_trek_scope_reject = trek_sub.add_parser("scope-reject", add_help=False)
    p_trek_scope_reject.add_argument("trek_id", nargs="?", default="")
    p_trek_scope_reject.add_argument("pending_id", nargs="?", default="")
    p_trek_scope_reject.add_argument("--json", action="store_true")

    # ms-95 / e-2308 — extend TTL on a single Trek task (= leader 代替 reaffirm
    # for Agent-tool subagent dispatch path)
    p_trek_extend = trek_sub.add_parser("extend-ttl", add_help=False)
    p_trek_extend.add_argument("trek_id", nargs="?", default="")
    p_trek_extend.add_argument("task_id", nargs="?", default="")
    p_trek_extend.add_argument("--minutes", default="")
    p_trek_extend.add_argument("--reason", default="")
    p_trek_extend.add_argument("--json", action="store_true")

    # ms-99 / e-2829 — slot verbs (nested subcommand). Trek scope entries
    # become first-class slots under Trek schema v2; the four CLI verbs
    # (add / amend / claim / list) all flow through the pending_scope_ops
    # queue so AC 15 (= all slot CLI ops via staging) holds.
    p_trek_slot = trek_sub.add_parser("slot", add_help=False)
    slot_sub = p_trek_slot.add_subparsers(dest="slot_cmd", metavar="<subcmd>")

    p_slot_add = slot_sub.add_parser("add", add_help=False)
    p_slot_add.add_argument("trek_id", nargs="?", default="")
    p_slot_add.add_argument("--project", dest="slot_project", default="")
    p_slot_add.add_argument("--milestone", "--ms", dest="slot_milestone",
                            default="")
    p_slot_add.add_argument("--operation", "--op", dest="slot_operation",
                            default="")
    p_slot_add.add_argument("--task", "--e", dest="slot_task", default="")
    # ``--children e-A,e-B`` — MS slot opt-in child list. Comma-separated
    # so scriptable; empty string = legacy null (= all-include).
    p_slot_add.add_argument("--children", dest="slot_children", default="")
    p_slot_add.add_argument("--json", action="store_true")

    p_slot_amend = slot_sub.add_parser("amend", add_help=False)
    p_slot_amend.add_argument("trek_id", nargs="?", default="")
    p_slot_amend.add_argument("slot_id", nargs="?", default="")
    p_slot_amend.add_argument("--add-child", dest="slot_add_child",
                              action="append", default=[])
    p_slot_amend.add_argument("--remove-child", dest="slot_remove_child",
                              action="append", default=[])
    p_slot_amend.add_argument("--json", action="store_true")

    p_slot_claim = slot_sub.add_parser("claim", add_help=False)
    p_slot_claim.add_argument("trek_id", nargs="?", default="")
    p_slot_claim.add_argument("slot_id", nargs="?", default="")
    # ``--session <sid>`` overrides the resolved session; empty string
    # via ``--unclaim`` clears the claim.
    p_slot_claim.add_argument("--session", dest="slot_session", default="")
    p_slot_claim.add_argument("--unclaim", dest="slot_unclaim",
                              action="store_true")
    p_slot_claim.add_argument("--json", action="store_true")

    p_slot_list = slot_sub.add_parser("list", add_help=False)
    p_slot_list.add_argument("trek_id", nargs="?", default="")
    p_slot_list.add_argument("--json", action="store_true")

    # ---- doctor / project / help ----
    sub.add_parser("doctor", add_help=False)
    p_project = sub.add_parser("project", add_help=False)
    p_project.add_argument("--help", "-h", action="store_true", dest="show_help")
    project_sub = p_project.add_subparsers(dest="project_cmd", metavar="<subcmd>")
    project_sub.add_parser("archive", add_help=False)
    project_sub.add_parser("unarchive", add_help=False)

    # ---- skill install (ms-44 e-777) ----
    # `beacon skill install` is the cross-platform install path used by
    # `beacon update` and the `/beacon-init` flow. Bash dispatches it via
    # `python3 commands.py skill_install`; we mirror that with no flags
    # — commands.py reads BEACON_FORCE / BEACON_SETTINGS_PATH from env if
    # present, so we forward those untouched.
    p_skill = sub.add_parser("skill", help="Skill operations", add_help=False)
    p_skill.add_argument("--help", "-h", action="store_true", dest="show_help")
    skill_sub = p_skill.add_subparsers(dest="skill_cmd", metavar="<subcmd>")
    p_skill_install = skill_sub.add_parser("install", add_help=False)
    p_skill_install.add_argument("--force", action="store_true")
    p_skill_install.add_argument("--adopt", action="store_true")
    p_skill_install.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_skill_install.add_argument("--prune", action="store_true")
    p_skill_install.add_argument("--json", action="store_true")
    p_skill_install.add_argument("--target", choices=("claude", "codex", "both"), default="")
    p_skill_install.add_argument("--name", default="")
    p_skill_install.add_argument("--settings-path", dest="settings_path", default="")

    # ---- session id (ms-54 e-1150 / e-1152 / ms-44 e-1171) ----
    # `beacon session id` mints/refreshes .beacon/session.json and prints
    # the current session_id. Used by channel/bus.mjs at startup and by
    # /beacon-session-start for the bus heartbeat. Wired here so Windows
    # pipx users (Python entry-point) get the same command as bash users
    # (was previously bash-only — Win cross-machine DM blocker #4).
    p_session = sub.add_parser("session", help="Session id / lifecycle", add_help=False)
    p_session.add_argument("--help", "-h", action="store_true", dest="show_help")
    session_sub = p_session.add_subparsers(dest="session_cmd", metavar="<subcmd>")
    session_sub.add_parser("id", add_help=False)

    # ms-54 / e-1369 Layer 4: AI-authored intent ("what am I doing right now")
    # and the attention_required flag. Distinct from the bridge-stamped
    # Layer 0-3 fields because intent is the only narrative input — the
    # bridge cannot observe what the AI is trying to accomplish.
    p_session_focus = session_sub.add_parser("focus", add_help=False)
    p_session_focus.add_argument("text", nargs="?", default="")
    p_session_focus.add_argument("--clear", action="store_true",
                                  help="Clear current intent text")
    p_session_focus.add_argument("--show", action="store_true",
                                  help="Show current intent (read-only)")
    p_session_focus.add_argument("--json", action="store_true")

    p_session_attention = session_sub.add_parser("attention", add_help=False)
    p_session_attention.add_argument("--set", dest="attention_set",
                                      choices=["true", "false"], default="")
    p_session_attention.add_argument("--json", action="store_true")

    # ms-73 / e-1762: Win parity for session lifecycle / forensics verbs.
    # `end` aggregates this session's notes/commits/PRs into the session log
    # (graceful close path). `rescue` aggregates all OTHER sessions (used when
    # a sibling bclaude died without calling end). `fork` spawns a sibling
    # worktree for parallel work on a target ms (ms-67). `log` enumerates or
    # shows session log entries (used by /beacon-session-start to restore
    # context). The Python paths translate argv → env exactly like the bash
    # dispatcher in bin/beacon (lines 2659/2677/2688/2770) so commands.py
    # observes the same env contract regardless of OS.
    p_session_end = session_sub.add_parser("end", add_help=False)
    p_session_end.add_argument("--summary", default="")
    p_session_end.add_argument("--session-id", dest="session_id", default="")
    p_session_end.add_argument("--bus-origin", dest="bus_origin",
                                action="store_true",
                                help="Refuse the write (poisoning defense, e-1293)")
    p_session_end.add_argument("--json", action="store_true")

    p_session_rescue = session_sub.add_parser("rescue", add_help=False)
    p_session_rescue.add_argument("--json", action="store_true")

    # `session fork <ms-id>` and `session fork list` share a subparser; the
    # handler dispatches on the positional. Mirrors the bash branch in
    # bin/beacon @ line 2770.
    p_session_fork = session_sub.add_parser("fork", add_help=False)
    p_session_fork.add_argument("ms_id_or_list", nargs="?", default="",
                                 help="Target ms-id, or literal 'list'")
    p_session_fork.add_argument("--json", action="store_true")

    p_session_log = session_sub.add_parser("log", add_help=False)
    p_session_log_sub = p_session_log.add_subparsers(
        dest="session_log_cmd", metavar="<list|show>"
    )
    p_session_log_list = p_session_log_sub.add_parser(
        "list", aliases=["ls"], add_help=False
    )
    p_session_log_list.add_argument("--limit", default="0")
    p_session_log_list.add_argument("--json", action="store_true")
    p_session_log_show = p_session_log_sub.add_parser(
        "show", aliases=["get"], add_help=False
    )
    p_session_log_show.add_argument("session_id", nargs="?", default="")
    p_session_log_show.add_argument("--json", action="store_true")

    # ---- profile (ms-64 e-1461) ----
    # `beacon profile list` enumerates ~/.beacon/profiles/* with active marker.
    # Mirrors bin/beacon's bash dispatch for Windows pipx parity.
    p_profile = sub.add_parser(
        "profile",
        help="Beacon profile management (e-1461)",
        add_help=False,
    )
    p_profile.add_argument("--help", "-h", action="store_true", dest="show_help")
    profile_sub = p_profile.add_subparsers(dest="profile_cmd", metavar="<subcmd>")
    p_profile_list = profile_sub.add_parser("list", add_help=False)
    p_profile_list.add_argument("--json", action="store_true")

    # ---- sessions (ms-54 e-1587) ----
    # `beacon sessions` is the cross-project counterpart to `bus directory`.
    # Lists the calling user's live sessions across all their projects in
    # one shot, so /beacon-dm-send can pick recipients without cd-ing into
    # each candidate project.
    p_sessions = sub.add_parser(
        "sessions",
        help="Cross-project session directory (e-1587)",
        add_help=False,
    )
    p_sessions.add_argument("--help", "-h", action="store_true", dest="show_help")
    p_sessions.add_argument("list_arg", nargs="?", default="")
    p_sessions.add_argument("--live", action="store_true")
    p_sessions.add_argument("--healthy", action="store_true")
    p_sessions.add_argument("--since-min", dest="since_min", default="5")
    p_sessions.add_argument("--machine", default="")
    p_sessions.add_argument("--agent", default="")
    p_sessions.add_argument("--json", action="store_true")

    # ---- channel install (ms-54 e-1152 / ms-44 e-1171) ----
    # `beacon channel install` writes a project-level .mcp.json that
    # wires the Claude Code beacon-bus MCP server to channel/bus.mjs.
    # Wired here for Win parity — same reason as `session id` above.
    p_channel = sub.add_parser("channel", help="Claude Code Channel install", add_help=False)
    p_channel.add_argument("--help", "-h", action="store_true", dest="show_help")
    channel_sub = p_channel.add_subparsers(dest="channel_cmd", metavar="<subcmd>")
    channel_sub.add_parser("install", add_help=False)

    # `beacon retro [--prepare|--catch-up] [--since X] [--until Y]`
    # `beacon retro save --week YYYY-WNN [--content T] [--stdin] [--json]`
    # `beacon retro done`
    #
    # ms-61 / e-2348: Win parity for /beacon-retro Skill. The Skill calls
    # `beacon retro --prepare`, `beacon retro --catch-up`, `beacon retro save`,
    # and `beacon retro done`. Without this parser, Windows beacon.exe (dispatch.py
    # path) failed with `argparse invalid choice: 'retro'` and the Skill was
    # unusable end-to-end on Win. Mirrors bin/beacon's `retro)` case
    # (lines 4119-4151) plus cmd_retro() (lines 1421-1474).
    p_retro = sub.add_parser(
        "retro", help="Weekly retrospective (prepare/save/done)", add_help=False,
    )
    p_retro.add_argument("--help", "-h", action="store_true", dest="show_help")
    # Top-level flags handled when no subcommand (= equivalent to bash's
    # `cmd_retro` default branch) — these drive `retro_prepare`.
    p_retro.add_argument("--prepare", action="store_true", dest="retro_prepare_flag")
    p_retro.add_argument("--catch-up", action="store_true", dest="retro_catch_up")
    p_retro.add_argument("--since", default="")
    p_retro.add_argument("--until", default="")
    retro_sub = p_retro.add_subparsers(dest="retro_cmd", metavar="<subcmd>")
    # `beacon retro save --week ... [--content T] [--stdin] [--json]`
    p_retro_save = retro_sub.add_parser("save", add_help=False)
    p_retro_save.add_argument("--week", default="")
    p_retro_save.add_argument("--content", default="")
    p_retro_save.add_argument("--stdin", action="store_true")
    p_retro_save.add_argument("--json", action="store_true")
    # `beacon retro done` — no flags.
    retro_sub.add_parser("done", add_help=False)

    # ---- ms-55 coordination signals (e-1735) ----
    # Win parity for the 6 ms-55 verbs (stop / resume / rollback / claim /
    # stuck / morning). Each subparser mirrors bin/beacon's env layout
    # so the same commands.py subcommand runs end-to-end. Without this,
    # Windows pipx users hit `argparse invalid choice` for everything in
    # the 走る / 止まる両輪 surface.

    # `beacon stop scoped|global|status` (ms-55 e-1646).
    p_stop = sub.add_parser("stop", help="Broadcast STOP signal (Andon cord)", add_help=False)
    p_stop.add_argument("--help", "-h", action="store_true", dest="show_help")
    stop_sub = p_stop.add_subparsers(dest="stop_cmd", metavar="<subcmd>")
    p_stop_scoped = stop_sub.add_parser("scoped", add_help=False)
    p_stop_scoped.add_argument("--target", default="")
    p_stop_scoped.add_argument("--reason", default="")
    p_stop_scoped.add_argument("--reason-kind", dest="reason_kind", default="")
    p_stop_scoped.add_argument("--machine-reason", dest="machine_reason", default="")
    p_stop_scoped.add_argument("--json", action="store_true")
    p_stop_global = stop_sub.add_parser("global", add_help=False)
    p_stop_global.add_argument("--reason", default="")
    p_stop_global.add_argument("--reason-kind", dest="reason_kind", default="")
    p_stop_global.add_argument("--machine-reason", dest="machine_reason", default="")
    p_stop_global.add_argument("--json", action="store_true")
    p_stop_status = stop_sub.add_parser("status", add_help=False)
    p_stop_status.add_argument("--json", action="store_true")

    # `beacon resume scoped|global` (paired with stop).
    p_resume = sub.add_parser("resume", help="Clear a STOP signal", add_help=False)
    p_resume.add_argument("--help", "-h", action="store_true", dest="show_help")
    resume_sub = p_resume.add_subparsers(dest="resume_cmd", metavar="<subcmd>")
    p_resume_scoped = resume_sub.add_parser("scoped", add_help=False)
    p_resume_scoped.add_argument("--target", default="")
    p_resume_scoped.add_argument("--reason", default="")
    p_resume_scoped.add_argument("--json", action="store_true")
    p_resume_global = resume_sub.add_parser("global", add_help=False)
    p_resume_global.add_argument("--reason", default="")
    p_resume_global.add_argument("--json", action="store_true")

    # `beacon rollback` (ms-55 e-1647).
    p_rollback = sub.add_parser(
        "rollback", help="Safe-boundary rollback (e-1647)", add_help=False,
    )
    p_rollback.add_argument("--help", "-h", action="store_true", dest="show_help")
    p_rollback.add_argument("--commits", default="0")
    p_rollback.add_argument("--reason", default="")
    p_rollback.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_rollback.add_argument(
        "--no-record", dest="no_record", action="store_true",
    )
    p_rollback.add_argument("--json", action="store_true")

    # `beacon claim ...` (ms-55 e-1648).
    p_claim = sub.add_parser("claim", help="Claim primitives", add_help=False)
    p_claim.add_argument("--help", "-h", action="store_true", dest="show_help")
    claim_sub = p_claim.add_subparsers(dest="claim_cmd", metavar="<subcmd>")
    # request / handoff / post share argument shape; only the bash side
    # name maps to a different commands.py verb (claim_request /
    # claim_handoff / claim_post).
    # Loop var must NOT be named `p` — that would shadow the top-level
    # parser captured at the top of build_parser() and `return p` at the
    # bottom would silently return claim_sub's last child instead.
    for _cs in ("request", "handoff", "post"):
        _pc = claim_sub.add_parser(_cs, add_help=False)
        _pc.add_argument("--target", default="")
        _pc.add_argument("--intent", default="")
        _pc.add_argument("--to", default="")
        _pc.add_argument("--expires-at", dest="expires_at", default="")
        _pc.add_argument("--json", action="store_true")
    p_claim_respond = claim_sub.add_parser("respond", add_help=False)
    p_claim_respond.add_argument("claim_id", nargs="?", default="")
    p_claim_respond.add_argument("--accept", action="store_true")
    p_claim_respond.add_argument("--decline", action="store_true")
    p_claim_respond.add_argument("--reason", default="")
    p_claim_respond.add_argument("--json", action="store_true")
    p_claim_release = claim_sub.add_parser("release", add_help=False)
    p_claim_release.add_argument("claim_id", nargs="?", default="")
    p_claim_release.add_argument("--outcome", default="")
    p_claim_release.add_argument("--reason", default="")
    p_claim_release.add_argument("--json", action="store_true")
    p_claim_list = claim_sub.add_parser("list", aliases=["ls"], add_help=False)
    p_claim_list.add_argument("--mine", action="store_true")
    p_claim_list.add_argument("--target", default="")
    p_claim_list.add_argument("--json", action="store_true")

    # `beacon stuck check` (ms-55 e-1649).
    p_stuck = sub.add_parser(
        "stuck", help="STUCK signal detector", add_help=False,
    )
    p_stuck.add_argument("--help", "-h", action="store_true", dest="show_help")
    stuck_sub = p_stuck.add_subparsers(dest="stuck_cmd", metavar="<subcmd>")
    p_stuck_check = stuck_sub.add_parser("check", add_help=False)
    p_stuck_check.add_argument(
        "--telemetry-inline", dest="telemetry_inline", default="",
    )
    p_stuck_check.add_argument(
        "--telemetry-file", dest="telemetry_file", default="",
    )
    p_stuck_check.add_argument(
        "--timeout-minutes", dest="timeout_minutes", default="",
    )
    p_stuck_check.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
    )
    p_stuck_check.add_argument("--json", action="store_true")

    # `beacon morning` (ms-55 e-1650 + e-1733 --no-doc).
    p_morning = sub.add_parser(
        "morning", help="Overnight briefing", add_help=False,
    )
    p_morning.add_argument("--help", "-h", action="store_true", dest="show_help")
    p_morning.add_argument(
        "--since-hours", dest="since_hours", default="",
    )
    p_morning.add_argument(
        "--events-file", dest="events_file", default="",
    )
    p_morning.add_argument(
        "--no-doc", dest="no_doc", action="store_true",
    )
    p_morning.add_argument("--json", action="store_true")

    # ---- monitor (ms-44 e-854) ----
    # `beacon monitor context` invokes the Stop hook context-usage monitor on
    # the current cwd's transcript payload, mirroring the bash entrypoint
    # `bin/context-usage-monitor.sh` so Win users can register a Python-only
    # hook command. `--dry-run` skips the side effects (note/state write) for
    # diagnostic use.
    p_monitor = sub.add_parser(
        "monitor",
        help="Context-usage Stop hook (e-854)",
        add_help=False,
    )
    p_monitor.add_argument("--help", "-h", action="store_true", dest="show_help")
    monitor_sub = p_monitor.add_subparsers(dest="monitor_cmd", metavar="<subcmd>")
    p_monitor_ctx = monitor_sub.add_parser("context", add_help=False)
    p_monitor_ctx.add_argument("--dry-run", action="store_true", dest="dry_run")

    # ---- bus (ms-54 e-1151) ----
    # `beacon bus send / listen / receive / ack / directory / budget`.
    # Before e-1151 the `bus` subcommand was bash-only (ALLOW_BASH_ONLY_DISPATCH).
    # That left Windows pipx users unable to `beacon bus directory` (no way to
    # discover cross-project DM targets) and unable to grant budget without
    # bash. Wired here for parity + `--project <id>` flag for cross-project
    # operations (writes BEACON_BUS_PROJECT_ID which commands.py honors).
    p_bus = sub.add_parser("bus", help="Bus / event channel ops", add_help=False)
    p_bus.add_argument("--help", "-h", action="store_true", dest="show_help")
    bus_sub = p_bus.add_subparsers(dest="bus_cmd", metavar="<subcmd>")

    p_bus_send = bus_sub.add_parser("send", add_help=False)
    p_bus_send.add_argument("--channel", default="")
    p_bus_send.add_argument("--payload", default="")
    p_bus_send.add_argument("--sender", default="")
    p_bus_send.add_argument("--delivery", default="")
    p_bus_send.add_argument("--in-reply-to", dest="in_reply_to", default="")
    p_bus_send.add_argument("--project", dest="bus_project_id", default="")
    # ms-73 / e-1763: DM-routing + envelope-issue flags. `--to <recipient>`
    # is the cross-user DM target (bash: BEACON_BUS_RECIPIENT_SESSION).
    # `--action <name>` is the Tier-1 envelope authorized action; bash
    # accepts repeated flags and comma-joins them server-side (e-1290).
    # `--no-envelope` opts out of the envelope issuance path (debug /
    # legacy). Without these flags Windows pipx users could not send
    # authorized cross-user DMs at all (W1 in ms-73 SPEC).
    # ms-75 / e-1858: ``--to`` becomes repeatable so a single send can
    # address multiple session ids. The legacy single-value form keeps
    # working (= bash dispatcher still passes BEACON_BUS_RECIPIENT_SESSION
    # as comma-joined when given multiple). ``--to-trek <trek-id>`` expands
    # the trek's joined members into one send per member (= cheap fan-out,
    # built atop the existing single-send path so envelope / budget gates
    # apply per recipient).
    p_bus_send.add_argument("--to", dest="bus_to", action="append", default=[])
    p_bus_send.add_argument("--to-trek", dest="bus_to_trek", default="")
    p_bus_send.add_argument("--action", dest="bus_action",
                             action="append", default=[])
    p_bus_send.add_argument("--no-envelope", dest="no_envelope",
                             action="store_true")
    p_bus_send.add_argument("--json", action="store_true")

    p_bus_listen = bus_sub.add_parser("listen", add_help=False)
    p_bus_listen.add_argument("--recipient", default="")
    p_bus_listen.add_argument("--channel", default="")
    p_bus_listen.add_argument("--interval", default="")
    p_bus_listen.add_argument("--auto-ack", dest="auto_ack", action="store_true")
    p_bus_listen.add_argument("--once", action="store_true")
    p_bus_listen.add_argument("--project", dest="bus_project_id", default="")

    p_bus_receive = bus_sub.add_parser("receive", add_help=False)
    p_bus_receive.add_argument("--recipient", default="")
    p_bus_receive.add_argument("--channel", default="")
    p_bus_receive.add_argument("--interval", default="")
    p_bus_receive.add_argument("--timeout", default="")
    p_bus_receive.add_argument("--auto-ack", dest="auto_ack", action="store_true")
    p_bus_receive.add_argument("--project", dest="bus_project_id", default="")

    p_bus_ack = bus_sub.add_parser("ack", add_help=False)
    p_bus_ack.add_argument("--recipient", default="")
    p_bus_ack.add_argument("--last-seen-at", dest="last_seen_at", default="")
    p_bus_ack.add_argument("--project", dest="bus_project_id", default="")
    # ms-73 / e-1763: explicit event-id ack (e-1423). Used by
    # /beacon-operation-execute to structurally clear the triggering
    # event from the next inbox-hook inject (forcing function).
    p_bus_ack.add_argument("--event", dest="bus_ack_event_id", default="")
    p_bus_ack.add_argument("--json", action="store_true")

    # ms-54 / e-1348: per-event receipt status (sent / delivered / opened).
    # Senders use this to localize where a DM stalled — "did it reach the
    # bridge? did the AI see it?" — without scraping bus logs.
    p_bus_status = bus_sub.add_parser("status", add_help=False)
    p_bus_status.add_argument("event_id", nargs="?", default="")
    p_bus_status.add_argument("--project", dest="bus_project_id", default="")
    p_bus_status.add_argument("--json", action="store_true")

    p_bus_dir = bus_sub.add_parser("directory", aliases=["dir"], add_help=False)
    p_bus_dir.add_argument("--user", default="")
    p_bus_dir.add_argument("--machine", default="")
    p_bus_dir.add_argument("--agent", default="")
    p_bus_dir.add_argument("--live", action="store_true")
    p_bus_dir.add_argument("--since-min", dest="since_min", default="")
    # e-1318: --healthy filters to sessions whose bridge poll loop is
    # currently pumping events. Distinct from --live (which checks the
    # secondary last_active heartbeat).
    p_bus_dir.add_argument("--healthy", action="store_true")
    p_bus_dir.add_argument("--project", dest="bus_project_id", default="")
    p_bus_dir.add_argument("--json", action="store_true")

    p_bus_budget = bus_sub.add_parser("budget", add_help=False)
    bus_budget_sub = p_bus_budget.add_subparsers(dest="bus_budget_cmd", metavar="<subcmd>")
    p_bus_budget_grant = bus_budget_sub.add_parser("grant", add_help=False)
    p_bus_budget_grant.add_argument("--turns", "-n", dest="turns", default="")
    p_bus_budget_grant.add_argument("--json", action="store_true")
    p_bus_budget_show = bus_budget_sub.add_parser("show", add_help=False)
    p_bus_budget_show.add_argument("--json", action="store_true")
    bus_budget_sub.add_parser("clear", add_help=False)

    # ---- dm respond / dm log (ms-70 / e-1716, e-1923) ----
    # Cross-user DM action decision (respond) + audit-trail viewer (log).
    # Win mirror for the bash `dm)` dispatch case. Adding here closes
    # the drift gap that the scripts/check-cli-help-drift.py guard
    # flagged after e-1716 (= bin/beacon `dm` verb shipped without a
    # Python sibling, so Windows pipx users hit `argparse invalid
    # choice: 'dm'`).
    p_dm = sub.add_parser("dm", help="DM approval ops (ms-70)", add_help=False)
    p_dm.add_argument("--help", "-h", action="store_true", dest="show_help")
    dm_sub = p_dm.add_subparsers(dest="dm_cmd", metavar="<subcmd>")

    p_dm_respond = dm_sub.add_parser("respond", add_help=False)
    # respond accepts approve|deny + event_id in either positional order;
    # we capture them as raw positionals and let the handler sort it out
    # (same pattern bin/beacon uses).
    p_dm_respond.add_argument("respond_args", nargs="*", default=[])
    p_dm_respond.add_argument("--project", dest="dm_project_id", default="")
    p_dm_respond.add_argument("--json", action="store_true")

    p_dm_log = dm_sub.add_parser("log", add_help=False)
    p_dm_log.add_argument("project_id", nargs="?", default="")
    p_dm_log.add_argument("--limit", default="")
    p_dm_log.add_argument("--project", dest="dm_project_id", default="")
    p_dm_log.add_argument("--json", action="store_true")

    # ---- auth login / logout / status (cloud OAuth) ----
    # `beacon auth login` opens a browser and signs in with Google so the
    # cloud project APIs (firestore / WS) become reachable. commands.py
    # delegates straight to the `auth` module — no env vars in play, the
    # interactive flow is owned by `auth.login()` itself.
    p_auth = sub.add_parser("auth", help="Cloud authentication", add_help=False)
    p_auth.add_argument("--help", "-h", action="store_true", dest="show_help")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", metavar="<subcmd>")
    # `login` accepts --dev / --email / --name for IdP-free local dev login
    # against a docker-compose cloud server (ms-12 e-2041). Without --dev the
    # flow is unchanged (Google / Cognito / web-mediated).
    p_auth_login = auth_sub.add_parser("login", add_help=False)
    p_auth_login.add_argument("--dev", action="store_true", dest="auth_dev")
    p_auth_login.add_argument("--email", dest="auth_email", default="")
    p_auth_login.add_argument("--name", dest="auth_name", default="")
    auth_sub.add_parser("logout", add_help=False)
    auth_sub.add_parser("status", add_help=False)

    # ---- cloud list / status / open / join / off / push / pull (#20) ----
    # Authenticated users (Windows pipx) need a full path to migrate local
    # projects to cloud and sync the other way. The architectural concern
    # behind cloud push (= two-master divergence) is already handled in
    # commands.py: cloud_push auto-switches to cloud mode after the
    # initial upload (ms-36 cloud-first cache design), and a SECOND push
    # while already in cloud mode is refused unless --force is given and
    # logged to the changelog (ms-24). We can wire push/pull safely.
    p_cloud = sub.add_parser("cloud", help="Cloud project navigation", add_help=False)
    p_cloud.add_argument("--help", "-h", action="store_true", dest="show_help")
    cloud_sub = p_cloud.add_subparsers(dest="cloud_cmd", metavar="<subcmd>")
    cloud_sub.add_parser("list", add_help=False)
    cloud_sub.add_parser("status", add_help=False)
    # e-1861 (ms-61): cloud off is sandbox / verification-only and requires
    # explicit --confirm <project_id> to prevent silent invocation.
    p_cloud_off = cloud_sub.add_parser("off", add_help=False)
    p_cloud_off.add_argument("--confirm", default="",
                             help="cloud project_id (exact match required)")
    p_cloud_join = cloud_sub.add_parser("join", add_help=False)
    p_cloud_join.add_argument("project_id", nargs="?", default="")
    p_cloud_open = cloud_sub.add_parser("open", add_help=False)
    p_cloud_open.add_argument("project_id", nargs="?", default="")
    p_cloud_open.add_argument("--no-browser", action="store_true",
                              help="Don't auto-launch the browser/desktop UI")
    p_cloud_push = cloud_sub.add_parser("push", add_help=False)
    p_cloud_push.add_argument("-f", "--force", action="store_true",
                              help="Override the cloud-mode safety block")
    cloud_sub.add_parser("pull", add_help=False)

    # ---- pr show / add / close / approve / reject / create / request-review / request-changes / review / merge ----
    p_pr = sub.add_parser("pr", help="Pull-request operations", add_help=False)
    p_pr.add_argument("--help", "-h", action="store_true", dest="show_help")
    pr_sub = p_pr.add_subparsers(dest="pr_cmd", metavar="<subcmd>")

    p_pr_show = pr_sub.add_parser("show", add_help=False)
    p_pr_show.add_argument("ident", nargs="?", default="")
    p_pr_show.add_argument("--json", action="store_true")

    p_pr_add = pr_sub.add_parser("add", add_help=False)
    p_pr_add.add_argument("url", nargs="?", default="")
    p_pr_add.add_argument("-m", "--ms", dest="ms_id", default="")
    p_pr_add.add_argument("--intent", default="")
    p_pr_add.add_argument("--author", default="")
    p_pr_add.add_argument("--json", action="store_true")

    p_pr_close = pr_sub.add_parser("close", add_help=False)
    p_pr_close.add_argument("entry_id", nargs="?", default="")
    p_pr_close.add_argument("--json", action="store_true")

    p_pr_approve = pr_sub.add_parser("approve", add_help=False)
    p_pr_approve.add_argument("entry_id", nargs="?", default="")
    p_pr_approve.add_argument("--rationale", default="")
    p_pr_approve.add_argument("--json", action="store_true")

    p_pr_reject = pr_sub.add_parser("reject", add_help=False)
    p_pr_reject.add_argument("entry_id", nargs="?", default="")
    p_pr_reject.add_argument("--rationale", default="")
    p_pr_reject.add_argument("--json", action="store_true")

    p_pr_create = pr_sub.add_parser("create", add_help=False)
    p_pr_create.add_argument("-m", "--ms", dest="ms_id", default="")
    p_pr_create.add_argument("--intent", default="")
    p_pr_create.add_argument("gh_args", nargs=argparse.REMAINDER)

    p_pr_rr = pr_sub.add_parser("request-review", add_help=False)
    p_pr_rr.add_argument("entry_id", nargs="?", default="")
    p_pr_rr.add_argument("--json", action="store_true")

    p_pr_rc = pr_sub.add_parser("request-changes", add_help=False)
    p_pr_rc.add_argument("entry_id", nargs="?", default="")
    p_pr_rc.add_argument("--rationale", default="")
    p_pr_rc.add_argument("--json", action="store_true")

    pr_sub.add_parser("review", add_help=False)  # prints "use /review Skill"

    p_pr_merge = pr_sub.add_parser("merge", add_help=False)
    p_pr_merge.add_argument("entry_id", nargs="?", default="")
    p_pr_merge.add_argument("--json", action="store_true")

    # ---- issue import / sync / list ----
    p_issue = sub.add_parser("issue", help="GitHub Issue import", add_help=False)
    p_issue.add_argument("--help", "-h", action="store_true", dest="show_help")
    issue_sub = p_issue.add_subparsers(dest="issue_cmd", metavar="<subcmd>")

    p_issue_import = issue_sub.add_parser("import", add_help=False)
    p_issue_import.add_argument("issue_number", nargs="?", default="")
    p_issue_import.add_argument("-m", "--ms", dest="ms_id", default="")
    p_issue_import.add_argument("--json", action="store_true")

    p_issue_sync = issue_sub.add_parser("sync", add_help=False)
    p_issue_sync.add_argument("-m", "--ms", dest="ms_id", default="")

    p_issue_list = issue_sub.add_parser("list", add_help=False)
    p_issue_list.add_argument("--json", action="store_true")

    # ---- member add / list / remove / role ----
    p_member = sub.add_parser("member", help="Project member management", add_help=False)
    p_member.add_argument("--help", "-h", action="store_true", dest="show_help")
    member_sub = p_member.add_subparsers(dest="member_cmd", metavar="<subcmd>")

    p_member_add = member_sub.add_parser("add", add_help=False)
    p_member_add.add_argument("member_id", nargs="?", default="")
    p_member_add.add_argument("--name", dest="member_name", default="")
    p_member_add.add_argument("--email", dest="member_email", default="")
    p_member_add.add_argument(
        "--role", dest="member_role", default="contributor"
    )
    p_member_add.add_argument("--json", action="store_true")

    for alias in ("list", "ls"):
        p_member_list = member_sub.add_parser(alias, add_help=False)
        p_member_list.add_argument("--json", action="store_true")

    for alias in ("remove", "rm"):
        p_member_remove = member_sub.add_parser(alias, add_help=False)
        p_member_remove.add_argument("member_id", nargs="?", default="")
        p_member_remove.add_argument("-r", "--reason", default="")
        p_member_remove.add_argument("--json", action="store_true")

    p_member_role = member_sub.add_parser("role", add_help=False)
    p_member_role.add_argument("member_id", nargs="?", default="")
    p_member_role.add_argument("new_role", nargs="?", default="")

    sub.add_parser("help", add_help=False)

    return p


# ---------------------------------------------------------------------------
# Handlers — argv → env vars → commands.py
# ---------------------------------------------------------------------------


def _handle_init(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        _print_init_help()
        return 0

    project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
    if Path(project_file).exists():
        print(".beacon/project.json already exists.")
        return 1

    # Home-directory guard mirrors bash. We do NOT prompt interactively on
    # the Python path — non-interactive init must be explicit via flags.
    try:
        cwd = Path.cwd()
    except OSError:
        cwd = None
    home = Path(os.path.expanduser("~"))
    if cwd is not None and cwd == home and not (args.name and args.objective):
        _eprint(
            "Refusing to initialise beacon in your home directory. "
            "Pass --name and --objective explicitly if this is intentional."
        )
        return 1

    name = args.name or ""
    objective = args.objective or ""
    retro_day = args.retro_day or "friday"

    # Interactive prompts only if a TTY is available AND fields are missing.
    if (not name or not objective) and sys.stdin.isatty():
        print("Beacon - Project Initialization")
        print("================================")
        if not name:
            name = input("Project name: ").strip()
        if not objective:
            objective = input("Project objective: ").strip()
        if not args.retro_day:
            print("Weekly retro day (1=Mon ... 7=Sun) [5]:")
            raw = input("> ").strip() or "5"
            mapping = {
                "1": "monday",
                "2": "tuesday",
                "3": "wednesday",
                "4": "thursday",
                "5": "friday",
                "6": "saturday",
                "7": "sunday",
            }
            retro_day = mapping.get(raw, "friday")

    if not name or not objective:
        _eprint(
            "Error: --name and --objective are required when running "
            "non-interactively (e.g. PowerShell pipelines or CI)."
        )
        return 2

    env = {
        "BEACON_NAME": name,
        "BEACON_OBJECTIVE": objective,
        "BEACON_RETRO_DAY": retro_day,
        # ms-63 / e-1441: forward sensitivity choice. cmd_init reads
        # BEACON_SENSITIVITY and falls back to "high" on any unknown value
        # (= forgot-to-configure → safe default).
        "BEACON_SENSITIVITY": getattr(args, "sensitivity", "high") or "high",
    }
    rc = _run_commands_py(root, "init", env)
    if rc != 0:
        return rc

    if args.storage == "cloud":
        # e-1861 (ms-61): cloud.json existence is sole source of truth.
        # cloud_push above already materialised .beacon/cloud.json — no
        # need to write the legacy `{"mode": "cloud"}` marker into config.json.
        _run_commands_py(root, "cloud_push", {})
        print("Cloud mode enabled.")
    return 0


def _print_init_help() -> None:
    print(
        "Usage: beacon init [--name NAME] [--objective TEXT] "
        "[--retro-day mon|tue|...|sun|monday|...] [--storage local|cloud] "
        "[--sensitivity high|low]"
    )


def _handle_status(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon status [--json] [--all] [--ms <ms-id>]...")
        return 0
    if (rc := _ensure_project()) is not None:
        return rc
    env = {
        "BEACON_JSON": "1" if args.json else "",
        "BEACON_ALL": "1" if args.all else "",
        "BEACON_MS_FILTER": ",".join(args.ms) if args.ms else "",
    }
    return _run_commands_py(root, "milestone_list", env)


def _handle_summary(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon summary \"text\" [--json]")
        return 0
    if (rc := _ensure_project()) is not None:
        return rc
    env = {
        "BEACON_SUMMARY_TEXT": " ".join(args.text) if args.text else "",
        "BEACON_JSON": "1" if args.json else "",
    }
    return _run_commands_py(root, "summary", env)


def _handle_log(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print(
            "Usage: beacon log [-m MS] [-p PROG] [--json] [--prepare|--finalize] "
            "[--summary TEXT] [--hash REF] [--behavior TEXT] [--resolves IDS]"
        )
        return 0
    if (rc := _ensure_project()) is not None:
        return rc

    # e-1227 (ms-17): resolve git operations from the *original* invocation
    # cwd, not the post-relocate cwd. When invoked from a worktree where
    # ``.beacon/`` lives in the parent repo, ``_relocate_to_project_root``
    # has already chdir'd us into the parent. Reading HEAD without ``-C``
    # would then capture the parent's HEAD instead of the worktree's,
    # silently attaching the wrong commit hash to every beacon entry.
    invocation_cwd = str(get_invocation_cwd())
    git_C = ["git", "-C", invocation_cwd]

    # Mirror bash: require git + read HEAD info via subprocess.
    try:
        subprocess.check_output(
            git_C + ["rev-parse", "--is-inside-work-tree"],
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Error: Not a git repository")
        return 1

    custom_summary = args.summary or args.message or ""
    target_ref = args.explicit_hash or "HEAD"
    try:
        commit_hash = subprocess.check_output(
            git_C + ["rev-parse", "--short", target_ref], text=True
        ).strip()
        commit_msg = subprocess.check_output(
            git_C + ["log", "-1", "--pretty=%s", target_ref], text=True
        ).strip()
        commit_date = subprocess.check_output(
            git_C + ["log", "-1", "--pretty=%ci", target_ref], text=True
        ).strip().split(" ", 1)[0]
    except subprocess.CalledProcessError as exc:
        _eprint(f"Error: git lookup for {target_ref} failed: {exc}")
        return 1

    # Beacon-only skip guard (avoid infinite loop). Only auto modes get
    # this guard — --finalize is explicit and always proceeds.
    mode = "prepare" if args.prepare else ("finalize" if args.finalize else "normal")
    if mode != "finalize":
        try:
            changed = subprocess.check_output(
                git_C + ["diff-tree", "--no-commit-id", "--name-only", "-r", target_ref],
                text=True,
            ).strip().splitlines()
        except subprocess.CalledProcessError:
            changed = []
        if changed and all(p.startswith(".beacon/") for p in changed):
            print(f"Skipped: commit {commit_hash} only changes .beacon/ (avoiding infinite loop)")
            return 0

    env = {
        "BEACON_HASH": commit_hash,
        "BEACON_MESSAGE": commit_msg,
        "BEACON_DATE": commit_date,
        "BEACON_SUMMARY": custom_summary,
        "BEACON_MS_ID": args.ms_id or "",
        "BEACON_PROGRESS": args.progress or "",
        "BEACON_JSON": "1" if args.json else "",
        "BEACON_BEHAVIOR": args.behavior or "",
        "BEACON_RESOLVES": args.resolves or "",
    }
    if mode == "prepare":
        return _run_commands_py(root, "log_prepare", env)
    if mode == "finalize":
        env["BEACON_NEW_SUMMARY"] = os.environ.get("BEACON_NEW_SUMMARY", custom_summary)
        return _run_commands_py(root, "log_finalize", env)
    return _run_commands_py(root, "log", env)


def _handle_save(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon save \"desc\" [-m MS] [--source S] [--url U] "
              "[--revision-id R] [--hash H] [-p PROG] [--json]")
        return 0
    if (rc := _ensure_project()) is not None:
        return rc
    env = {
        "BEACON_DESCRIPTION": args.description or "",
        "BEACON_MS_ID": args.ms_id or "",
        "BEACON_SOURCE": args.source or "manual",
        "BEACON_URL": args.url or "",
        "BEACON_REVISION_ID": args.revision_id or "",
        "BEACON_HASH": args.hash or "",
        "BEACON_PROGRESS": args.progress or "",
        "BEACON_DATE": _today(),
        "BEACON_JSON": "1" if args.json else "",
    }
    return _run_commands_py(root, "save", env)


def _handle_sync(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon sync")
        return 0
    if (rc := _ensure_project()) is not None:
        return rc
    return _run_commands_py(root, "sync", {})


# ---- task handlers ----


def _handle_task(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.task_cmd is None:
        print(
            "Usage: beacon task [add|done|list|show|detail|update|cancel|delete] "
            "[options]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc

    cmd = args.task_cmd
    if cmd == "add":
        # e-1192: only override BEACON_* env vars that the user passed via
        # flag (or positional, for description). args.X is None when the
        # flag was omitted — in that case keep whatever the caller set in
        # the surrounding environment so seeding via `set BEACON_X=...`
        # still works on Windows / cmd.exe / PowerShell.
        env: Dict[str, str] = {"BEACON_DATE": _today()}
        if args.description is not None:
            env["BEACON_DESCRIPTION"] = args.description
        if args.ms_id is not None:
            env["BEACON_MS_ID"] = args.ms_id
        # entry_type defaults to "task" on a true omission so existing
        # callers that never pass -t keep working without a BEACON_TYPE
        # env var set in advance.
        env["BEACON_TYPE"] = args.entry_type if args.entry_type else "task"
        if args.detail is not None:
            env["BEACON_DETAIL"] = args.detail
        if args.requested_by is not None:
            env["BEACON_REQUESTED_BY"] = args.requested_by
        if args.priority is not None:
            env["BEACON_PRIORITY"] = args.priority
        if args.motivation is not None:
            env["BEACON_MOTIVATION"] = args.motivation
        if args.acceptance_criteria is not None:
            env["BEACON_ACCEPTANCE_CRITERIA"] = args.acceptance_criteria
        return _run_commands_py(root, "task_add", env)

    if cmd == "done":
        if not args.entry_id:
            print("Usage: beacon task done <entry-id> --reason <text> [-p <progress>]")
            return 1
        # e-976: only inject BEACON_REASON when --reason was passed
        # (parser default=None), so the python gate (_require_reason_or_skip)
        # can refuse missing-env and accept explicit "".
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_PROGRESS": args.progress or "",
        }
        if args.reason is not None:
            env["BEACON_REASON"] = args.reason
        return _run_commands_py(root, "task_done", env)

    if cmd in ("list", "ls"):
        env = {
            "BEACON_MS_ID": args.ms_id or "",
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_ALL": "1" if args.all else "",
            "BEACON_TYPE_FILTER": args.type_filter or "",
        }
        return _run_commands_py(root, "task_list", env)

    if cmd == "show":
        if not args.entry_id:
            print("Usage: beacon task show <entry-id> [--json]")
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "task_show", env)

    if cmd == "detail":
        if not args.entry_id:
            print("Usage: beacon task detail <entry-id> [detail-text]")
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_DETAIL": args.detail_text or "",
        }
        return _run_commands_py(root, "task_detail", env)

    if cmd == "update":
        if not args.entry_id:
            print(
                "Usage: beacon task update <entry-id> [--description D] "
                "[--status S] [--detail D] [--ms MS] "
                "[--motivation TEXT] [--acceptance-criteria TEXT] "
                "[--behavior TEXT] [--priority P] [--json]"
            )
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_DESCRIPTION": args.description or "",
            "BEACON_STATUS": args.status or "",
            "BEACON_DETAIL": args.detail or "",
            "BEACON_MS_ID": args.ms_id or "",
            "BEACON_MOTIVATION": args.motivation or "",
            "BEACON_ACCEPTANCE_CRITERIA": args.acceptance_criteria or "",
            "BEACON_BEHAVIOR": args.behavior or "",
            "BEACON_PRIORITY": args.priority or "",
        }
        return _run_commands_py(root, "task_update", env)

    if cmd == "cancel":
        if not args.entry_id:
            print("Usage: beacon task cancel <entry-id> [--reason <text>]")
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_REASON": args.reason or "",
        }
        return _run_commands_py(root, "task_cancel", env)

    if cmd == "delete":
        if not args.entry_id:
            print("Usage: beacon task delete <entry-id> [--json]")
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "task_delete", env)

    print(f"Unknown task subcommand: {cmd}")
    return 1


# ---- milestone handlers ----


def _handle_milestone(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.ms_cmd is None:
        print(
            "Usage: beacon milestone "
            "[add|list|start|done|close|observe|show|update|graph|workspace|"
            "workspace-cleanup] [options]"
        )
        return 0 if args.show_help else 2
    cmd = args.ms_cmd
    if cmd == "purge":
        return _purge_dispatch(
            root, "milestone_purge", id_value=args.ms_id, id_env="BEACON_MS_ID",
            reason=args.reason, index=args.index, json_flag=args.json,
            usage="beacon milestone purge <ms-id> --reason <text> [--index <n>] [--json]",
        )

    if (rc := _ensure_project()) is not None:
        return rc

    if cmd == "add":
        env = {
            "BEACON_TITLE": args.title or "",
            "BEACON_TARGET_DATE": args.target_date or "",
            "BEACON_DESCRIPTION": args.description or "",
            "BEACON_PRIORITY": args.priority or "",
            "BEACON_OBJECTIVE": args.objective or "",
            "BEACON_ACCEPTANCE_CRITERIA": args.acceptance_criteria or "",
            "BEACON_OWNER": args.owner or "",
            "BEACON_ASSIGNEE": args.assignee or "",
        }
        return _run_commands_py(root, "milestone_add", env)

    if cmd in ("list", "ls"):
        return _run_commands_py(root, "milestone_list", {})

    if cmd == "start":
        if not args.ms_id:
            print("Usage: beacon milestone start <ms-id> [--no-branch] [--no-assignee]")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_NO_BRANCH": "1" if getattr(args, "no_branch", False) else "",
            "BEACON_NO_ASSIGNEE": "1" if getattr(args, "no_assignee", False) else "",
        }
        return _run_commands_py(root, "milestone_start", env)

    if cmd in ("done", "close"):
        if not args.ms_id:
            print("Usage: beacon milestone done <ms-id> --reason <text>")
            return 1
        # e-976: BEACON_REASON only when --reason was passed (parser default=None).
        env = {"BEACON_MS_ID": args.ms_id}
        if args.reason is not None:
            env["BEACON_REASON"] = args.reason
        return _run_commands_py(root, "milestone_done", env)

    if cmd == "join":
        if not args.ms_id:
            print("Usage: beacon milestone join <ms-id> [--checkout]")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_CHECKOUT": "1" if getattr(args, "checkout", False) else "",
        }
        return _run_commands_py(root, "milestone_join", env)

    if cmd == "observe":
        if not args.ms_id:
            print("Usage: beacon milestone observe <ms-id> --reason <text>")
            return 1
        # e-976: route to the dedicated milestone_observe handler with the
        # --reason gate. Only forward BEACON_REASON when --reason was
        # explicitly passed (default=None in the parser), so the python gate
        # can distinguish "flag missing" (refuse) from "--reason ''"
        # (explicit waiver, accepted but discouraged).
        env = {"BEACON_MS_ID": args.ms_id}
        if args.reason is not None:
            env["BEACON_REASON"] = args.reason
        return _run_commands_py(root, "milestone_observe", env)

    if cmd == "show":
        if not args.ms_id:
            print("Usage: beacon milestone show <ms-id> [--json]")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "milestone_show", env)

    if cmd == "update":
        if not args.ms_id:
            print("Usage: beacon milestone update <ms-id> [--title T] ...")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_TITLE": args.title or "",
            "BEACON_PROGRESS": args.progress or "",
            "BEACON_TARGET_DATE": args.target_date or "",
            "BEACON_STATUS": args.status or "",
            "BEACON_DESCRIPTION": args.description or "",
            "BEACON_PRIORITY": args.priority or "",
            "BEACON_OBJECTIVE": args.objective or "",
            "BEACON_ACCEPTANCE_CRITERIA": args.acceptance_criteria or "",
            "BEACON_OWNER": args.owner or "",
            "BEACON_ASSIGNEE": args.assignee or "",
            "BEACON_REASON": args.reason or "",
        }
        return _run_commands_py(root, "milestone_update", env)

    if cmd == "graph":
        return _run_commands_py(
            root, "milestone_graph", {"BEACON_JSON": "1" if args.json else ""}
        )

    if cmd == "workspace":
        if not args.ms_id:
            print(
                "Usage: beacon milestone workspace <ms-id> "
                "[--executor ai|human] [--dir <path>] [--clear] [--no-git] [--json]"
            )
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_EXECUTOR": args.executor or "",
            "BEACON_WORKSPACE": args.workspace or "",
            "BEACON_CLEAR": "1" if args.clear else "",
            "BEACON_NO_GIT": "1" if args.no_git else "",
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "milestone_workspace", env)

    if cmd == "workspace-cleanup":
        if not args.ms_id:
            print(
                "Usage: beacon milestone workspace-cleanup <ms-id> "
                "[--merge-to <branch>] [--json]"
            )
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_MERGE_TO": args.merge_to or "",
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "milestone_workspace_cleanup", env)

    print(f"Unknown milestone subcommand: {cmd}")
    return 1


# ---- doc handlers ----


def _handle_doc(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.doc_cmd is None:
        print(
            "Usage: beacon doc [add|list|show|update|delete] [options]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc

    cmd = args.doc_cmd
    if cmd == "add":
        if not args.title:
            print(
                "Usage: beacon doc add \"title\" [--scope core|spec|memo|retro|report] "
                "[--ms ms-id] [--op op-id] [--id slug] [--content text] [--json] "
                "[--bus-origin]\n"
                "  --bus-origin: refuse the write (persistence poisoning defense, "
                "ms-54 / e-1293)"
            )
            return 1
        env = {
            "BEACON_TITLE": args.title,
            "BEACON_DOC_ID": args.doc_id or "",
            "BEACON_CONTENT": args.content or "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
            "BEACON_TREK_ID": getattr(args, "doc_trek", "") or "",
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_BUS_ORIGIN": "1" if getattr(args, "bus_origin", False) else "",
        }
        return _run_commands_py(root, "doc_add", env)

    if cmd in ("list", "ls"):
        env = {
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
            # ms-75 / e-1866: --trek <trek-id> filter forwarded as
            # BEACON_TREK_ID to cmd_doc_list. Empty string = no filter.
            "BEACON_TREK_ID": getattr(args, "doc_trek", "") or "",
        }
        return _run_commands_py(root, "doc_list", env)

    if cmd in ("show", "get"):
        if not args.doc_id:
            print("Usage: beacon doc show <doc-id> [--json]")
            return 1
        env = {
            "BEACON_DOC_ID": args.doc_id,
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "doc_show", env)

    if cmd == "update":
        if not args.doc_id:
            print(
                "Usage: beacon doc update <doc-id> [--title text] "
                "[--scope ...] [--ms ms-id] [--op op-id] [--content text] [--json] "
                "[--bus-origin]\n"
                "  --bus-origin: refuse the write (persistence poisoning defense, "
                "ms-54 / e-1293)"
            )
            return 1
        env = {
            "BEACON_DOC_ID": args.doc_id,
            "BEACON_TITLE": args.title or "",
            "BEACON_CONTENT": args.content or "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
            "BEACON_TREK_ID": getattr(args, "doc_trek", "") or "",
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_BUS_ORIGIN": "1" if getattr(args, "bus_origin", False) else "",
        }
        return _run_commands_py(root, "doc_update", env)

    if cmd in ("delete", "rm"):
        if not args.doc_id:
            print("Usage: beacon doc delete <doc-id>")
            return 1
        env = {
            "BEACON_DOC_ID": args.doc_id,
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "doc_delete", env)

    print(f"Unknown doc subcommand: {cmd}")
    return 1


# ---- note handlers ----


def _handle_note(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print(
            "Usage: beacon note \"<text>\" [--context \"<label>\"] [--bus-origin]\n"
            "       beacon note list [--json]\n"
            "       beacon note clear\n"
            "  --bus-origin: refuse the write (persistence poisoning defense, ms-54 / e-1293)"
        )
        return 0
    if (rc := _ensure_project()) is not None:
        return rc

    sub = args.text_or_sub
    if sub == "list":
        env = {"BEACON_JSON": "1" if args.json else ""}
        return _run_commands_py(root, "note_list", env)
    if sub == "clear":
        return _run_commands_py(root, "note_clear", {})
    if not sub:
        print(
            "Usage: beacon note \"<text>\" [--context \"<label>\"] [--bus-origin]\n"
            "       beacon note list [--json]\n"
            "       beacon note clear\n"
            "  --bus-origin: refuse the write (persistence poisoning defense, ms-54 / e-1293)"
        )
        return 1
    env = {
        "BEACON_NOTE_TEXT": sub,
        "BEACON_NOTE_CONTEXT": args.context or "",
        "BEACON_BUS_ORIGIN": "1" if getattr(args, "bus_origin", False) else "",
    }
    return _run_commands_py(root, "note_add", env)


# ---- trigger handlers ----


def _handle_trigger(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.trigger_cmd is None:
        print("Usage: beacon trigger [fire|check|clear] <name> [message]")
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc

    cmd = args.trigger_cmd
    if cmd == "fire":
        env = {
            "BEACON_TRIGGER_NAME": args.name or "",
            "BEACON_TRIGGER_MESSAGE": args.message or "",
        }
        return _run_commands_py(root, "trigger_fire", env)
    if cmd == "check":
        return _run_commands_py(root, "trigger_check", {})
    if cmd == "clear":
        env = {"BEACON_TRIGGER_NAME": args.name or ""}
        return _run_commands_py(root, "trigger_clear", env)
    return 1


# ---- search ----


def _handle_search(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon search \"query\" [--ms id] [--op id] [--scope ...] [--json] ...")
        return 0
    if (rc := _ensure_project()) is not None:
        return rc
    env = {
        "BEACON_QUERY": args.query or "",
        "BEACON_MS_ID": args.ms_id or "",
        "BEACON_OPERATION_ID": args.op_id or "",
        "BEACON_ENTRY_ID": args.entry_id or "",
        "BEACON_SCOPE": args.scope or "",
        "BEACON_ASSIGNEE": args.assignee or "",
        "BEACON_OWNER": args.owner or "",
        "BEACON_FROM": args.from_date or "",
        "BEACON_TO": args.to_date or "",
        "BEACON_TYPE": args.type or "",
        "BEACON_STATUS": args.status or "",
        "BEACON_PRIORITY": args.priority or "",
        "BEACON_LIMIT": args.limit or "",
        "BEACON_OFFSET": args.offset or "",
        "BEACON_JSON": "1" if args.json else "",
    }
    return _run_commands_py(root, "search", env)


# ---- cycle ----


def _handle_cycle(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.cycle_cmd is None:
        print("Usage: beacon cycle status [--json]")
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    if args.cycle_cmd == "status":
        env = {"BEACON_JSON": "1" if args.json else ""}
        return _run_commands_py(root, "cycle_status", env)
    return 1


# ---- push ----


def _handle_push(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.push_cmd is None:
        print(
            "Usage: beacon push record [--from <hash>] [--to <hash>] "
            "[--desc <text>] [-m <ms-id>]\n"
            "       beacon push list [--json]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc

    if args.push_cmd == "record":
        mode = "prepare" if args.prepare else ("finalize" if args.finalize else "")
        env = {
            "BEACON_MODE": mode,
            "BEACON_FROM": args.from_hash or "",
            "BEACON_TO": args.to_hash or "",
            "BEACON_BRANCH": args.branch or "",
            "BEACON_DESCRIPTION": args.desc or "",
            "BEACON_MS": args.ms_id or "",
        }
        return _run_commands_py(root, "push_record", env)
    if args.push_cmd == "list":
        env = {"BEACON_JSON": "1" if args.json else ""}
        return _run_commands_py(root, "push_list", env)
    return 1


# ---- deploy ----


def _handle_deploy(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.deploy_cmd is None:
        print(
            "Usage: beacon deploy record [--env <env>] [--revision <rev>] "
            "[--semver <v1.0.0>] [--desc <text>]\n"
            "       beacon deploy list [--env <env>] [--json]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    if args.deploy_cmd == "record":
        mode = "prepare" if args.prepare else ("finalize" if args.finalize else "")
        env = {
            "BEACON_MODE": mode,
            "BEACON_REVISION": args.revision or "",
            "BEACON_SEMVER": args.semver or "",
            "BEACON_DESCRIPTION": args.desc or "",
            "BEACON_HASH": args.hash or "",
            "BEACON_DATE": args.date or "",
            "BEACON_INSERT_BEFORE": args.insert_before or "",
            "BEACON_TYPE": args.type or "",
            "BEACON_ENVIRONMENT": args.env or "",
        }
        return _run_commands_py(root, "deploy_record", env)
    if args.deploy_cmd == "list":
        env = {
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_ENVIRONMENT": args.env or "",
        }
        return _run_commands_py(root, "deploy_list", env)
    return 1


# ---- entry / project / doctor ----


def _handle_entry(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.entry_cmd is None:
        print("Usage: beacon entry move <entry-id> -t <task-id> | -m <ms-id>")
        print("       beacon entry purge <e-id> --reason <text> [--index <n>]")
        return 0 if args.show_help else 2
    if args.entry_cmd == "purge":
        return _purge_dispatch(
            root, "entry_purge", id_value=args.entry_id, id_env="BEACON_ENTRY_ID",
            reason=args.reason, index=args.index, json_flag=args.json,
            usage="beacon entry purge <e-id> --reason <text> [--index <n>] [--json]",
        )
    if (rc := _ensure_project()) is not None:
        return rc
    if args.entry_cmd == "move":
        env = {
            "BEACON_ENTRY_ID": args.entry_id or "",
            "BEACON_TASK_ID": args.task_id or "",
            "BEACON_MS_ID": args.ms_id or "",
        }
        return _run_commands_py(root, "entry_move", env)
    return 1


def _handle_operation(root: Path, args: argparse.Namespace) -> int:
    """Operation dispatch — `purge` (e-893) + `envelope verify` (ms-73 e-1764).

    Full operation management (open/close/list/show/run/incident) is still
    bash-only; we expose the duplicate-ID recovery path (purge) and the
    AI self-check primitive (envelope verify, ms-60 e-1340) which the
    autonomous /beacon-operation-execute Skill calls before each action.
    Without envelope verify on Windows pipx, autonomous operations could
    not perform the pre-flight authorization check (W1 in ms-73 SPEC).
    """
    if args.show_help or args.op_cmd is None:
        print("Usage: beacon operation purge <op-id> --reason <text> [--index <n>]")
        print("       beacon operation envelope verify <op-id> <action> [--json]")
        print("  (Full operation management is available via the bash CLI.)")
        return 0 if args.show_help else 2
    if args.op_cmd == "purge":
        return _purge_dispatch(
            root, "operation_purge", id_value=args.op_id, id_env="BEACON_OP_ID",
            reason=args.reason, index=args.index, json_flag=args.json,
            usage="beacon operation purge <op-id> --reason <text> [--index <n>] [--json]",
        )
    if args.op_cmd == "envelope":
        env_sub = getattr(args, "op_envelope_cmd", None)
        if env_sub is None:
            print("Usage: beacon operation envelope verify <op-id> <action> [--json]")
            return 2
        if env_sub == "verify":
            op_id = (getattr(args, "op_id", "") or "").strip()
            action = (getattr(args, "action", "") or "").strip()
            if not op_id or not action:
                _eprint(
                    "Usage: beacon operation envelope verify <op-id> <action> [--json]"
                )
                return 2
            # commands.py:cmd_operation_envelope_verify reads:
            #   BEACON_OPERATION_ID, BEACON_ACTION, BEACON_JSON
            # and routes to server/approved_actions for the actual match.
            # cloud / local mode branching lives there (returns rc=2 with
            # a clear error if local mode); we don't pre-check it here so
            # the error message stays identical across OS.
            return _run_commands_py(root, "operation_envelope_verify", {
                "BEACON_OPERATION_ID": op_id,
                "BEACON_ACTION": action,
                "BEACON_JSON": "1" if getattr(args, "json", False) else "",
            })
        _eprint(f"Unknown operation envelope subcommand: {env_sub}")
        return 2
    return 1


def _handle_project(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or args.project_cmd is None:
        print("Usage: beacon project archive|unarchive")
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    if args.project_cmd == "archive":
        return _run_commands_py(root, "project_archive", {})
    if args.project_cmd == "unarchive":
        return _run_commands_py(root, "project_unarchive", {})
    return 1


def _handle_trek(root: Path, args: argparse.Namespace) -> int:
    """`beacon trek <create|list|show|start|archive>` (ms-69 / e-1653).

    Mirrors the bash dispatch in bin/beacon. Trek is a top-level
    cross-project entity so we do **not** call ``_ensure_project`` —
    the storage layer (~/.beacon/treks/) is project-agnostic.

    BEACON_USER_EMAIL / BEACON_SESSION_ID are inherited from the parent
    process environment for the ``create`` path (= identity gating lives
    in commands.py).
    """
    if args.show_help or args.trek_cmd is None:
        print(
            "Usage: beacon trek <subcommand> [args]\n"
            "\n"
            "Subcommands:\n"
            "  create \"<title>\" [--type temporary|persistent] "
            "[--description \"...\"] [--goal-state \"<criterion>\"]\n"
            "  list [--status S] [--all] [--all-actors] [--joined] [--json]\n"
            "  show <trek-id> [--all] [--json]   (= aggregation block: tasks "
            "/ commits / docs)\n"
            "  start <trek-id>\n"
            "  archive <trek-id>\n"
            "  invite <trek-id> --actor <email> [--notify]\n"
            "  join <trek-id>\n"
            "  leave <trek-id>\n"
            "  plan <trek-id> --add-scope <project[:ref]>\n"
            "  plan <trek-id> --remove-scope <project[:ref]>\n"
            "  plan <trek-id> --goal-state \"<criterion>\"   "
            "(= ms-75 / e-1865, '' clears)\n"
            "  scope-add <trek-id> --project <pid> "
            "[--milestone <ms-id> | --operation <op-id> | --task <e-id>]   "
            "(= canonical, ms-97 / AC23)\n"
            "  scope-approve <trek-id> <pending-id>   "
            "(= commit staged op, ms-97 / AC25)\n"
            "  scope-reject  <trek-id> <pending-id>   "
            "(= drop staged op, ms-97 / AC25)\n"
            "  slot add   <trek-id> --project <pid> "
            "--milestone|--task|--operation <id> [--children e-A,e-B]\n"
            "  slot amend <trek-id> <slot-id> "
            "[--add-child <e-id> ...] [--remove-child <e-id> ...]\n"
            "  slot claim <trek-id> <slot-id> [--session <sid> | --unclaim]\n"
            "  slot list  <trek-id> [--json]   "
            "(= materialize view, ms-99 / e-2829)\n"
            "  timeline <trek-id> [--limit N] [--json]   "
            "(= chronological view)\n"
            "  stop <trek-id> [--reason \"...\"]    (= Andon cord、halt 信号)\n"
            "  resume <trek-id>                     (= halt 信号を clear)\n"
            "  transfer-leader <trek-id> --to <session-id>\n"
            "\n"
            "Env (creator identity, required for 'create'):\n"
            "  BEACON_USER_EMAIL    creator email\n"
            "  BEACON_SESSION_ID    creator session id (becomes initial leader)\n"
            "  BEACON_USER_ID       creator user_id (fallback: whoami)"
        )
        return 0 if args.show_help else 2
    cmd = args.trek_cmd
    json_env = "1" if getattr(args, "json", False) else ""

    if cmd == "create":
        return _run_commands_py(
            root, "trek_create",
            {
                "BEACON_TREK_TITLE": args.title or "",
                "BEACON_TREK_TYPE": args.trek_type or "",
                "BEACON_TREK_DESCRIPTION": args.trek_desc or "",
                "BEACON_TREK_GOAL_STATE": getattr(args, "goal_state", "") or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd in ("list", "ls"):
        return _run_commands_py(
            root, "trek_list",
            {
                "BEACON_TREK_STATUS": args.status or "",
                "BEACON_TREK_INCLUDE_ARCHIVED": "1" if args.include_archived else "",
                "BEACON_TREK_ALL_ACTORS": "1" if args.all_actors else "",
                "BEACON_TREK_JOINED_ONLY": (
                    "1" if getattr(args, "joined_only", False) else ""
                ),
                "BEACON_JSON": json_env,
            },
        )
    if cmd in ("show", "get"):
        return _run_commands_py(
            root, "trek_show",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_ALL": "1" if getattr(args, "show_all", False) else "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "start":
        return _run_commands_py(
            root, "trek_start",
            {"BEACON_TREK_ID": args.trek_id or "", "BEACON_JSON": json_env},
        )
    if cmd == "archive":
        return _run_commands_py(
            root, "trek_archive",
            {"BEACON_TREK_ID": args.trek_id or "", "BEACON_JSON": json_env},
        )
    if cmd == "invite":
        return _run_commands_py(
            root, "trek_invite",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_ACTOR": args.actor or "",
                "BEACON_TREK_NOTIFY": "1" if args.notify else "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "join":
        return _run_commands_py(
            root, "trek_join",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_NO_ARM": "1" if getattr(args, "no_arm", False) else "",
                # ms-88 / e-2090 — consent gate bypass。 CLI から渡された場合のみ
                # commands.py 側の typed-ack prompt を skip する。 環境変数で
                # bypass する経路は意図しない (= テスト fixture など特殊用途は
                # 別途 BEACON_TREK_CONSENT_ACK=1 を明示する手があるが、 default
                # は flag 経由のみ通る運用)。
                "BEACON_TREK_CONSENT_ACK": "1" if getattr(args, "consent_ack", False) else "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "leave":
        return _run_commands_py(
            root, "trek_leave",
            {"BEACON_TREK_ID": args.trek_id or "", "BEACON_JSON": json_env},
        )
    if cmd == "plan":
        goal_state_val = getattr(args, "goal_state", None)
        goal_state_explicit = goal_state_val is not None
        return _run_commands_py(
            root, "trek_plan",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_SCOPE_ADD": args.add_scope or "",
                "BEACON_TREK_SCOPE_REMOVE": args.remove_scope or "",
                "BEACON_TREK_GOAL_STATE": goal_state_val or "",
                "BEACON_TREK_GOAL_STATE_SET": "1" if goal_state_explicit else "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "timeline":
        return _run_commands_py(
            root, "trek_timeline",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_LIMIT": args.limit or "50",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "stop":
        return _run_commands_py(
            root, "trek_stop",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_REASON": args.reason or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "resume":
        return _run_commands_py(
            root, "trek_resume",
            {"BEACON_TREK_ID": args.trek_id or "", "BEACON_JSON": json_env},
        )
    if cmd == "transfer-leader":
        return _run_commands_py(
            root, "trek_transfer_leader",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_TO": args.to_session_id or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "take-over":
        return _run_commands_py(
            root, "trek_take_over",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "kickoff":
        # session_id override is optional — when absent, cmd_trek_kickoff
        # reads BEACON_SESSION_ID from the inherited env (= same default
        # as take-over / pulse-ack). Passing through "" is safe; the
        # commands-side resolver only honors a non-empty override.
        env = {
            "BEACON_TREK_ID": args.trek_id or "",
            "BEACON_TREK_KICKOFF_DM_EVENT_ID":
                getattr(args, "kickoff_dm_event_id", "") or "",
            "BEACON_JSON": json_env,
        }
        sid_override = getattr(args, "session_id_override", "") or ""
        if sid_override:
            env["BEACON_SESSION_ID"] = sid_override
        return _run_commands_py(root, "trek_kickoff", env)
    if cmd == "pulse-ack":
        return _run_commands_py(
            root, "trek_pulse_ack",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_PICKED_CHOICE": getattr(args, "picked_choice", "") or "",
                "BEACON_TREK_NOTE": getattr(args, "note", "") or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "task-state":
        return _run_commands_py(
            root, "trek_task_state",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_TASK_ID": args.task_id or "",
                "BEACON_TREK_STATE": args.state or "",
                "BEACON_TREK_NOTE": args.note or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "extend-ttl":
        # ms-95 / e-2308 — leader-side primitive for Agent-tool subagent
        # dispatch path. See lib/commands.cmd_trek_extend_ttl docstring.
        return _run_commands_py(
            root, "trek_extend_ttl",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_TASK_ID": args.task_id or "",
                "BEACON_TREK_TTL_MINUTES": getattr(args, "minutes", "") or "",
                "BEACON_TREK_TTL_REASON": getattr(args, "reason", "") or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "reconcile":
        return _run_commands_py(
            root, "trek_reconcile",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_APPLY": "1" if getattr(args, "apply", False) else "",
                "BEACON_JSON": json_env,
            },
        )
    # ms-97 / e-2626 (AC23) — canonical scope-add verb. Delegates to
    # cmd_trek_plan with BEACON_TREK_SCOPE_ADD assembled from the flag-style
    # args. Requires at least one narrowing key (= --milestone / --operation /
    # --task) since cmd_trek_plan / parse_scope_arg rejects bare project-wide
    # adds under AC7 strict mode.
    if cmd == "scope-add":
        project = getattr(args, "scope_add_project", "") or ""
        milestone = getattr(args, "scope_add_milestone", "") or ""
        operation = getattr(args, "scope_add_operation", "") or ""
        task = getattr(args, "scope_add_task", "") or ""
        if not project:
            print("Error: --project <pid> is required", file=sys.stderr)
            return 1
        ref = milestone or operation or task
        if not ref:
            print(
                "Error: one of --milestone | --operation | --task is required",
                file=sys.stderr,
            )
            return 1
        return _run_commands_py(
            root, "trek_plan",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_TREK_SCOPE_ADD": f"{project}:{ref}",
                "BEACON_JSON": json_env,
            },
        )
    # ms-97 / e-2611 (AC25) — commit a staged scope op.
    if cmd == "scope-approve":
        return _run_commands_py(
            root, "trek_scope_approve",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_PENDING_ID": getattr(args, "pending_id", "") or "",
                "BEACON_JSON": json_env,
            },
        )
    # ms-97 / e-2611 (AC25) — drop a staged scope op.
    if cmd == "scope-reject":
        return _run_commands_py(
            root, "trek_scope_reject",
            {
                "BEACON_TREK_ID": args.trek_id or "",
                "BEACON_PENDING_ID": getattr(args, "pending_id", "") or "",
                "BEACON_JSON": json_env,
            },
        )
    # ms-99 / e-2829 — slot verbs (nested subcommand). All four flow
    # through commands.py so the same code path serves bash and pipx
    # front-ends.
    if cmd == "slot":
        slot_cmd = getattr(args, "slot_cmd", None) or ""
        if not slot_cmd:
            print(
                "Usage: beacon trek slot <add|amend|claim|list> [args]",
                file=sys.stderr,
            )
            return 2
        if slot_cmd == "add":
            return _run_commands_py(
                root, "trek_slot_add",
                {
                    "BEACON_TREK_ID": args.trek_id or "",
                    "BEACON_SLOT_PROJECT": getattr(args, "slot_project", "") or "",
                    "BEACON_SLOT_MILESTONE": getattr(args, "slot_milestone", "") or "",
                    "BEACON_SLOT_OPERATION": getattr(args, "slot_operation", "") or "",
                    "BEACON_SLOT_TASK": getattr(args, "slot_task", "") or "",
                    "BEACON_SLOT_CHILDREN": getattr(args, "slot_children", "") or "",
                    "BEACON_JSON": json_env,
                },
            )
        if slot_cmd == "amend":
            add_children = getattr(args, "slot_add_child", []) or []
            remove_children = getattr(args, "slot_remove_child", []) or []
            return _run_commands_py(
                root, "trek_slot_amend",
                {
                    "BEACON_TREK_ID": args.trek_id or "",
                    "BEACON_SLOT_ID": getattr(args, "slot_id", "") or "",
                    "BEACON_SLOT_ADD_CHILDREN": ",".join(add_children),
                    "BEACON_SLOT_REMOVE_CHILDREN": ",".join(remove_children),
                    "BEACON_JSON": json_env,
                },
            )
        if slot_cmd == "claim":
            return _run_commands_py(
                root, "trek_slot_claim",
                {
                    "BEACON_TREK_ID": args.trek_id or "",
                    "BEACON_SLOT_ID": getattr(args, "slot_id", "") or "",
                    "BEACON_SLOT_SESSION": getattr(args, "slot_session", "") or "",
                    "BEACON_SLOT_UNCLAIM": "1" if getattr(args, "slot_unclaim", False) else "",
                    "BEACON_JSON": json_env,
                },
            )
        if slot_cmd == "list":
            return _run_commands_py(
                root, "trek_slot_list",
                {
                    "BEACON_TREK_ID": args.trek_id or "",
                    "BEACON_JSON": json_env,
                },
            )
        print(
            f"Error: unknown slot subcommand {slot_cmd!r}",
            file=sys.stderr,
        )
        return 2
    return 1


def _handle_doctor(root: Path, args: argparse.Namespace) -> int:
    return _run_commands_py(root, "doctor", {})


def _handle_pr(root: Path, args: argparse.Namespace) -> int:
    """Mirror of bash cmd_pr (10 subcommands).

    Each branch sets the env vars bash uses, then delegates to commands.py.
    `review` is a stub that points to the /review Skill (same as bash).
    """
    if args.show_help or args.pr_cmd is None:
        print(
            "Usage: beacon pr [create|add|show|review|approve|request-changes|reject|merge|close|request-review]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    cmd = args.pr_cmd
    json_env = "1" if getattr(args, "json", False) else ""

    if cmd == "show":
        if not args.ident:
            print("Usage: beacon pr show <entry-id|pr-number|url> [--json]")
            return 1
        return _run_commands_py(
            root, "pr_show", {"BEACON_PR_IDENT": args.ident, "BEACON_JSON": json_env}
        )
    if cmd == "add":
        if not args.url:
            print(
                "Usage: beacon pr add <github-url> [-m <ms-id>] "
                "[--intent \"text\"] [--author user]"
            )
            return 1
        return _run_commands_py(
            root,
            "pr_add",
            {
                "BEACON_URL": args.url,
                "BEACON_MS_ID": args.ms_id or "",
                "BEACON_INTENT": args.intent or "",
                "BEACON_AUTHOR": args.author or "",
                "BEACON_DATE": _today(),
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "close":
        if not args.entry_id:
            print("Usage: beacon pr close <entry-id> [--json]")
            return 1
        return _run_commands_py(
            root, "pr_close",
            {"BEACON_ENTRY_ID": args.entry_id, "BEACON_JSON": json_env},
        )
    if cmd in ("approve", "reject", "request-changes"):
        if not args.entry_id:
            print(f"Usage: beacon pr {cmd} <entry-id> [--rationale \"text\"]")
            return 1
        subcmd = "pr_" + cmd.replace("-", "_")
        return _run_commands_py(
            root, subcmd,
            {
                "BEACON_ENTRY_ID": args.entry_id,
                "BEACON_RATIONALE": args.rationale or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "create":
        # gh_args is a REMAINDER list. Forward it as a JSON array, not a
        # shell-quoted string: shell quoting cannot round-trip non-ASCII
        # (Japanese PR titles) cleanly across the env-var hop into
        # commands.py:cmd_pr_create, which reads BEACON_GH_ARGS_JSON.
        gh_args_json = json.dumps(args.gh_args or [], ensure_ascii=False)
        return _run_commands_py(
            root, "pr_create",
            {
                "BEACON_MS_ID": args.ms_id or "",
                "BEACON_INTENT": args.intent or "",
                "BEACON_GH_ARGS_JSON": gh_args_json,
            },
        )
    if cmd == "request-review":
        if not args.entry_id:
            print("Usage: beacon pr request-review <entry-id> [--json]")
            return 1
        return _run_commands_py(
            root, "pr_request_review",
            {"BEACON_ENTRY_ID": args.entry_id, "BEACON_JSON": json_env},
        )
    if cmd == "review":
        print("beacon pr review is now handled by the /review Claude Code Skill.")
        print("Use: /review <PR-number>")
        return 0
    if cmd == "merge":
        if not args.entry_id:
            print("Usage: beacon pr merge <entry-id> [--json]")
            return 1
        return _run_commands_py(
            root, "pr_merge",
            {"BEACON_ENTRY_ID": args.entry_id, "BEACON_JSON": json_env},
        )
    print(f"Unknown pr subcommand: {cmd}")
    return 1


def _handle_issue(root: Path, args: argparse.Namespace) -> int:
    """`beacon issue import|sync|list` — GitHub Issue → beacon task."""
    if args.show_help or args.issue_cmd is None:
        print(
            "Usage: beacon issue import <number> [-m <ms-id>]\n"
            "       beacon issue sync [-m <ms-id>]\n"
            "       beacon issue list [--json]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    cmd = args.issue_cmd
    json_env = "1" if getattr(args, "json", False) else ""

    if cmd == "import":
        if not args.issue_number:
            print("Usage: beacon issue import <number> [-m <ms-id>] [--json]")
            return 1
        return _run_commands_py(
            root, "issue_import",
            {
                "BEACON_ISSUE_NUMBER": args.issue_number,
                "BEACON_MS_ID": args.ms_id or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "sync":
        return _run_commands_py(
            root, "issue_sync", {"BEACON_MS_ID": args.ms_id or ""}
        )
    if cmd == "list":
        return _run_commands_py(root, "issue_list", {"BEACON_JSON": json_env})
    print(f"Unknown issue subcommand: {cmd}")
    return 1


def _handle_member(root: Path, args: argparse.Namespace) -> int:
    """`beacon member add|list|remove|role` — project members."""
    if args.show_help or args.member_cmd is None:
        print(
            "Usage: beacon member [add|list|remove|role]\n"
            "  add <id> [--name N] [--email E] [--role R]\n"
            "  list [--json]\n"
            "  remove <id> --reason <text>\n"
            "  role <id> <owner|maintainer|contributor|viewer>"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc
    cmd = args.member_cmd
    json_env = "1" if getattr(args, "json", False) else ""

    if cmd == "add":
        if not args.member_id:
            print(
                "Usage: beacon member add <id> [--name N] [--email E] "
                "[--role owner|maintainer|contributor|viewer] [--json]"
            )
            return 1
        return _run_commands_py(
            root, "member_add",
            {
                "BEACON_MEMBER_ID": args.member_id,
                "BEACON_MEMBER_NAME": args.member_name or "",
                "BEACON_MEMBER_EMAIL": args.member_email or "",
                "BEACON_MEMBER_ROLE": args.member_role or "contributor",
                "BEACON_JSON": json_env,
            },
        )
    if cmd in ("list", "ls"):
        return _run_commands_py(root, "member_list", {"BEACON_JSON": json_env})
    if cmd in ("remove", "rm"):
        if not args.member_id:
            print("Usage: beacon member remove <id> --reason <text> [--json]")
            return 1
        return _run_commands_py(
            root, "member_remove",
            {
                "BEACON_MEMBER_ID": args.member_id,
                "BEACON_REASON": args.reason or "",
                "BEACON_JSON": json_env,
            },
        )
    if cmd == "role":
        if not args.member_id or not args.new_role:
            print(
                "Usage: beacon member role <id> "
                "<owner|maintainer|contributor|viewer>"
            )
            return 1
        return _run_commands_py(
            root, "member_role",
            {
                "BEACON_MEMBER_ID": args.member_id,
                "BEACON_MEMBER_ROLE": args.new_role,
            },
        )
    print(f"Unknown member subcommand: {cmd}")
    return 1


def _handle_cloud(root: Path, args: argparse.Namespace) -> int:
    """`beacon cloud list|status|join|open|off` — cloud project navigation.

    `push` / `pull` deliberately stay deferred (Phase 2/3 — destructive
    operations need ms-24-level safety review and a PowerShell-native
    confirmation flow). The five subcommands implemented here are all
    read-or-bind operations safe for non-interactive use.

    `open` is the only one that involves opening a UI; on bash-less
    systems we open the Web UI in the system browser (Tauri Desktop
    can be launched manually if the user has it installed — there's
    no portable cross-platform way to detect+launch it from Python).
    """
    if args.show_help or args.cloud_cmd is None:
        print(
            "Usage: beacon cloud [list|status|open <id>|join <id>|push|pull]\n"
            "  list                 List cloud projects\n"
            "  status               Show current cloud mode + project_id\n"
            "  open <project-id>    Bind cwd to a cloud project + open Web UI\n"
            "  join <project-id>    Bind cwd to a cloud project (no UI launch)\n"
            "  push [-f|--force]    Upload local project to cloud\n"
            "  pull                 Sync cloud state into the local read-only cache\n"
            "\n"
            "Sandbox / verification only (e-1861, ms-61):\n"
            "  off --confirm <id>   Disable cloud sync (archives cloud.json into .beacon/.trash/)"
        )
        return 0 if args.show_help else 2

    cmd = args.cloud_cmd
    if cmd == "list":
        return _run_commands_py(root, "cloud_list", {})
    if cmd == "status":
        return _run_commands_py(root, "cloud_status", {})
    if cmd == "join":
        if not args.project_id:
            print("Usage: beacon cloud join <project-id>")
            return 1
        return _run_commands_py(
            root, "cloud_join", {"BEACON_CLOUD_PROJECT_ID": args.project_id}
        )
    if cmd == "off":
        # e-1861 (ms-61): "cloud off" is sandbox / verification-only. Beacon
        # = cloud-only in normal operation (Claude Code requires internet).
        # The subcommand requires --confirm <project_id> to prevent silent
        # invocation by sub-agents (the same pattern that caused the
        # 2026-06-15 data-loss panic / e-1776). Mirrors bash bin/beacon.
        cloud_path = Path(".beacon/cloud.json")
        if not cloud_path.exists():
            print("Not in cloud mode (no .beacon/cloud.json found).")
            return 0
        try:
            import json as _json
            expected_pid = _json.loads(
                cloud_path.read_text(encoding="utf-8")
            ).get("project_id", "")
        except (OSError, ValueError):
            expected_pid = ""
        if not expected_pid:
            _eprint("Error: .beacon/cloud.json is malformed (no project_id).")
            return 1
        given_pid = getattr(args, "confirm", "") or ""
        if given_pid != expected_pid:
            print("Refusing to disable cloud sync without two-factor confirmation.")
            print()
            print("  Sandbox / verification only — Beacon assumes cloud mode in production.")
            print(f"  To disable: beacon cloud off --confirm \"{expected_pid}\"")
            print()
            print("  This guard exists because a 2026-06-15 incident (e-1776) saw a")
            print("  sub-agent silently flip cloud → local and trigger a data-loss panic.")
            return 1
        # Move cloud.json into .beacon/.trash/ with timestamp (never delete).
        from datetime import datetime as _dt
        trash_dir = Path(".beacon/.trash")
        trash_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime("%Y%m%d-%H%M%S")
        dest = trash_dir / f"cloud.json.disabled-{ts}"
        try:
            cloud_path.rename(dest)
        except OSError as exc:
            _eprint(f"Error archiving cloud.json: {exc}")
            return 1
        print("Cloud sync disabled for this directory.")
        print(f"  .beacon/cloud.json -> {dest}")
        print(f"  To re-enable: beacon cloud join {expected_pid}  (or beacon cloud setup)")
        return 0
    if cmd == "open":
        if not args.project_id:
            print("Usage: beacon cloud open <project-id>")
            return 1
        return _do_cloud_open(root, args.project_id, args.no_browser)
    if cmd == "push":
        # cmd_cloud_push reads BEACON_FORCE from env. It already enforces the
        # ms-24 cloud-mode block (refuses without --force). The historical
        # ms-36 "config.json mode -> cloud after initial migration" auto-
        # switch was retired in e-1861 (ms-61) — cloud.json existence is
        # now the sole source of truth, so no mode-write happens here.
        # Dispatch handler is intentionally thin.
        return _run_commands_py(
            root, "cloud_push",
            {"BEACON_FORCE": "1" if args.force else ""},
        )
    if cmd == "pull":
        return _run_commands_py(root, "cloud_pull", {})

    print(f"Unknown cloud subcommand: {cmd}")
    return 1


def _do_cloud_open(root: Path, project_id: str, no_browser: bool) -> int:
    """Non-interactive port of bash `cmd_cloud_launch`.

    Differences from bash:
      - No interactive `read -rp` prompts — project_id is required.
      - No tmux + curses dashboard (impossible without bash on Windows;
        also curses doesn't ship on stock Windows Python).
      - UI launch is just `webbrowser.open(beacon-ai.dev/?project=…)`
        instead of trying to spawn Tauri. Users with Beacon Desktop
        installed can launch it manually.
    """
    # 1. Verify the project exists (and the user can see it).
    rc = _run_commands_py(
        root, "cloud_check_project", {}, extra_args=[project_id]
    )
    if rc != 0:
        _eprint(
            f"Error: project {project_id!r} not found in cloud "
            f"(or you don't have access). Try `beacon cloud list`."
        )
        return rc

    # 2. Warn if cloud.json points elsewhere (no interactive confirm —
    #    --force semantics belong on a separate flag if we want them).
    cloud_path = Path(".beacon/cloud.json")
    existing_id = ""
    if cloud_path.exists():
        try:
            import json as _json
            existing_id = _json.loads(
                cloud_path.read_text(encoding="utf-8")
            ).get("project_id", "")
        except (OSError, ValueError):
            existing_id = ""
    if existing_id and existing_id != project_id:
        _eprint(
            f"Warning: .beacon/cloud.json already points to "
            f"{existing_id!r}; overwriting with {project_id!r}."
        )

    # ms-64 / e-1461: resolve api_url via the profile resolver so cloud.json
    # init + Web UI launch URL track the active profile (BEACON_PROFILE env or
    # --profile flag). Falls back to beacon-ai.dev when profile.py is not
    # available — keeps the original default behavior for fresh installs.
    api_url = "https://beacon-ai.dev"
    try:
        # lib/ is on sys.path via the entry point's runtime setup, but be
        # defensive in case this Python entry point runs before that wiring.
        import sys as _sys
        _lib_path = str(Path(__file__).resolve().parents[1] / "lib")
        if _lib_path not in _sys.path:
            _sys.path.insert(0, _lib_path)
        import profile as _profile  # type: ignore[import-not-found]
        api_url = _profile.resolve_active_profile().api_url
    except Exception:
        pass

    # 3. Write the cloud / project skeleton.
    # e-1861 (ms-61): cloud.json existence is the sole source of truth.
    # The legacy `{"mode": "cloud"}` write into config.json is no longer
    # needed (closes the silent-drift attack surface).
    Path(".beacon").mkdir(exist_ok=True)
    cloud_path.write_text(
        '{\n  "project_id": "' + project_id + '",\n'
        '  "api_url": "' + api_url + '"\n}\n',
        encoding="utf-8",
    )
    project_file = os.environ.get(
        "BEACON_PROJECT_FILE", ".beacon/project.json"
    )
    if not Path(project_file).exists():
        Path(project_file).write_text(
            '{"name":"cloud","milestones":[]}\n', encoding="utf-8"
        )

    print(f"Bound this directory to cloud project {project_id!r}.")

    # 4. Launch the Web UI in the system browser unless suppressed.
    web_url = f"{api_url}/?project={project_id}"
    if no_browser:
        print(f"Web UI: {web_url}")
        return 0
    try:
        import webbrowser
        opened = webbrowser.open(web_url)
        if opened:
            print(f"Opened {web_url} in your default browser.")
        else:
            print(f"Web UI: {web_url}  (open it manually in your browser)")
    except Exception as exc:
        # Never let UI launch failure break the bind.
        _eprint(f"(couldn't auto-launch browser: {exc})")
        print(f"Web UI: {web_url}")
    return 0


def _handle_auth(root: Path, args: argparse.Namespace) -> int:
    """`beacon auth login|logout|status` — Google OAuth for cloud projects.

    commands.py routes each to the `auth` module which opens a browser
    (login), revokes the local token (logout), or prints the current
    state (status). No env vars are forwarded because the auth module
    owns its own state (token cache under ~/.beacon/).
    """
    if args.show_help or args.auth_cmd is None:
        print("Usage: beacon auth [login|logout|status]")
        return 0 if args.show_help else 2
    if args.auth_cmd not in ("login", "logout", "status"):
        print(f"Unknown auth subcommand: {args.auth_cmd}")
        return 2
    # --dev / --email / --name → BEACON_* env (= auth.login() が読む契約)。
    # 他の subcommand / 非 dev login では空なので従来挙動のまま。
    env: Dict[str, str] = {}
    if args.auth_cmd == "login":
        if getattr(args, "auth_dev", False):
            env["BEACON_AUTH_DEV"] = "1"
        email = getattr(args, "auth_email", "") or ""
        if email:
            env["BEACON_DEV_EMAIL"] = email
        name = getattr(args, "auth_name", "") or ""
        if name:
            env["BEACON_DEV_NAME"] = name
    return _run_commands_py(root, f"auth_{args.auth_cmd}", env)


def _handle_skill(root: Path, args: argparse.Namespace) -> int:
    """`beacon skill install [--force] [--settings-path PATH]`.

    Delegates to ``commands.py skill_install`` after surfacing optional
    flags as ``BEACON_*`` env vars. The bash path uses
    ``python3 commands.py skill_install`` with no env, so the Python
    path stays env-superset-compatible.
    """
    if args.show_help or args.skill_cmd is None:
        print("Usage: beacon skill install [--target claude|codex|both --name NAME] [--dry-run] [--prune] [--force|--adopt]")
        return 0 if args.show_help else 2
    if args.skill_cmd != "install":
        print(f"Unknown skill subcommand: {args.skill_cmd}")
        return 2
    env: Dict[str, str] = {}
    if getattr(args, "force", False):
        env["BEACON_FORCE"] = "1"
    if getattr(args, "adopt", False):
        env["BEACON_ADOPT"] = "1"
    if getattr(args, "dry_run", False):
        env["BEACON_DRY_RUN"] = "1"
    if getattr(args, "prune", False):
        env["BEACON_PRUNE"] = "1"
    if getattr(args, "json", False):
        env["BEACON_JSON"] = "1"
    target = getattr(args, "target", "") or ""
    name = getattr(args, "name", "") or ""
    if target:
        env["BEACON_SKILL_TARGET"] = target
    if name:
        env["BEACON_SKILL_NAME"] = name
    settings_path = getattr(args, "settings_path", "") or ""
    if settings_path:
        env["BEACON_SETTINGS_PATH"] = settings_path
    return _run_commands_py(root, "skill_install", env)


def _handle_session(root: Path, args: argparse.Namespace) -> int:
    """`beacon session <id|focus|attention|end|rescue|fork|log>`.

    Mirrors the bash dispatcher (bin/beacon) so Windows pipx users get the
    same behavior. Initial 3 verbs (id/focus/attention) landed in ms-44
    e-1171 + ms-54 e-1369. The remaining 4 (end/rescue/fork/log) were
    bash-only until ms-73 e-1762 added Win parity here.

    Each branch translates argv → BEACON_* env vars exactly like
    bin/beacon, then delegates to the matching commands.py entry. lib/
    modules being pure-Python is what makes this a thin wrapper.
    """
    if args.show_help or args.session_cmd is None:
        print("Usage: beacon session id")
        print("       beacon session focus \"<text>\" | --clear | --show [--json]")
        print("       beacon session attention --set true|false [--json]")
        print("       beacon session end [--summary <text>] [--session-id <id>] "
              "[--bus-origin] [--json]")
        print("       beacon session rescue [--json]")
        print("       beacon session fork <ms-id> [--json]")
        print("       beacon session fork list [--json]")
        print("       beacon session log list [--limit N] [--json]")
        print("       beacon session log show <session-id> [--json]")
        return 0 if args.show_help else 2
    if args.session_cmd == "id":
        return _run_commands_py(root, "session_id", {})
    if args.session_cmd == "focus":
        env: Dict[str, str] = {
            "BEACON_SESSION_FOCUS_TEXT": args.text or "",
            "BEACON_SESSION_FOCUS_CLEAR": "1" if args.clear else "",
            "BEACON_SESSION_FOCUS_SHOW": "1" if args.show else "",
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "session_focus", env)
    if args.session_cmd == "attention":
        env = {
            "BEACON_SESSION_ATTENTION_SET": args.attention_set or "",
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "session_attention", env)
    # ----- ms-73 e-1762 additions -----
    if args.session_cmd == "end":
        env = {
            "BEACON_SUMMARY": getattr(args, "summary", "") or "",
            "BEACON_SESSION_ID": getattr(args, "session_id", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
            "BEACON_BUS_ORIGIN": "1" if getattr(args, "bus_origin", False) else "",
        }
        return _run_commands_py(root, "session_end", env)
    if args.session_cmd == "rescue":
        env = {
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        }
        return _run_commands_py(root, "session_rescue", env)
    if args.session_cmd == "fork":
        # `session fork list [--json]` enumerates active forks; otherwise
        # `session fork <ms-id> [--json]` spawns one. The bash side uses
        # positional discrimination on `list`, mirrored here so Skills can
        # call either path identically.
        positional = (getattr(args, "ms_id_or_list", "") or "").strip()
        if positional == "list":
            env = {
                "BEACON_JSON": "1" if getattr(args, "json", False) else "",
            }
            return _run_commands_py(root, "session_fork_list", env)
        if not positional:
            _eprint("Usage: beacon session fork <ms-id> [--json]")
            _eprint("       beacon session fork list [--json]")
            return 2
        env = {
            "BEACON_SESSION_FORK_MS_ID": positional,
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        }
        return _run_commands_py(root, "session_fork", env)
    if args.session_cmd == "log":
        log_sub = getattr(args, "session_log_cmd", None)
        if log_sub in ("list", "ls"):
            env = {
                "BEACON_LIMIT": str(getattr(args, "limit", "0") or "0"),
                "BEACON_JSON": "1" if getattr(args, "json", False) else "",
            }
            return _run_commands_py(root, "session_log_list", env)
        if log_sub in ("show", "get"):
            sid = (getattr(args, "session_id", "") or "").strip()
            if not sid:
                _eprint("Error: beacon session log show <session-id>")
                return 1
            env = {
                "BEACON_SESSION_ID": sid,
                "BEACON_JSON": "1" if getattr(args, "json", False) else "",
            }
            return _run_commands_py(root, "session_log_show", env)
        _eprint("Usage: beacon session log [list|show]")
        return 2
    print(f"Unknown session subcommand: {args.session_cmd}")
    return 2


def _handle_channel(root: Path, args: argparse.Namespace) -> int:
    """`beacon channel install` — write project-level .mcp.json (ms-44 e-1171).

    Bash path: `python3 COMMANDS_PY channel_install` with no env.
    Mirror that exactly so Windows pipx users get the same behavior.
    """
    if args.show_help or args.channel_cmd is None:
        print("Usage: beacon channel install")
        return 0 if args.show_help else 2
    if args.channel_cmd != "install":
        print(f"Unknown channel subcommand: {args.channel_cmd}")
        return 2
    return _run_commands_py(root, "channel_install", {})


# ---------------------------------------------------------------------------
# ms-55 coordination signal handlers (e-1735) — Win parity for the 6 new
# verbs (stop / resume / rollback / claim / stuck / morning). The bash side
# is the source of truth for argv → env shape; each handler replays the
# same translation a Bash dispatcher would do, then delegates to the
# matching commands.py subcommand. Lib modules (stop_signal / rollback /
# claims / stuck_detect / morning) are pure-Python, so the underlying
# logic runs identically on Windows pipx — we only need to bridge argv.
# ---------------------------------------------------------------------------

def _split_target(target: str) -> tuple[str, str]:
    """Split `<kind>:<id>` into (kind, id). Empty string → empty tuple."""
    if not target:
        return "", ""
    if ":" not in target:
        return target, target  # mirrored bash bug-shape; CLI catches it
    k, _, i = target.partition(":")
    return k, i


def _handle_retro(root: Path, args: argparse.Namespace) -> int:
    """`beacon retro [--prepare|--catch-up] [--since X] [--until Y]` (= prepare).
    `beacon retro save --week ... [--content T] [--stdin] [--json]` (= save).
    `beacon retro done` (= done).

    ms-61 / e-2348: Windows beacon.exe (= dispatch.py PowerShell-native path)
    had no retro subcommand registered, so /beacon-retro Skill failed end-to-end
    on Win with `argparse invalid choice: 'retro'`. Mirrors bin/beacon's
    ``retro)`` case (lines 4119-4151) and ``cmd_retro`` (lines 1421-1474).

    Dispatch table:

      ``retro_cmd`` value | env vars set                            | commands.py verb
      ------------------- | --------------------------------------- | ---------------------
      ``None`` (top)      | ``BEACON_SINCE`` / ``BEACON_UNTIL`` /  | ``retro_prepare``
                          | ``BEACON_RETRO_CATCH_UP``               |
      ``save``            | ``BEACON_RETRO_WEEK`` /                 | ``retro_save``
                          | ``BEACON_CONTENT`` / ``BEACON_JSON``    |
      ``done``            | (none)                                  | ``retro_done``

    --since defaulting mirrors bash: if absent, call ``cmd_retro_default_since``
    via the commands.py subprocess (= same code path the bash wrapper uses).
    --until defaulting: today (or yesterday if today is Monday, so we cover the
    week ending Sunday).
    """
    if args.show_help:
        print(
            "Usage: beacon retro [--prepare] [--catch-up] "
            "[--since YYYY-MM-DD] [--until YYYY-MM-DD]"
        )
        print(
            "       beacon retro save --week YYYY-WNN [--content T | --stdin] [--json]"
        )
        print("       beacon retro done")
        return 0

    sub_cmd = getattr(args, "retro_cmd", None)

    if sub_cmd == "save":
        week = getattr(args, "week", "") or ""
        if not week:
            _eprint(
                "Usage: beacon retro save --week YYYY-WNN [--content text | --stdin] [--json]"
            )
            _eprint("  Content can be piped via stdin.")
            return 1
        return _run_commands_py(root, "retro_save", {
            "BEACON_RETRO_WEEK": week,
            "BEACON_CONTENT": getattr(args, "content", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })

    if sub_cmd == "done":
        return _run_commands_py(root, "retro_done", {})

    # Default branch: `beacon retro [--prepare|--catch-up] [--since X] [--until Y]`
    # → retro_prepare. --prepare is accepted as an explicit no-op flag for
    # parity with the bash CLI; presence does not change behaviour.
    since = getattr(args, "since", "") or ""
    until = getattr(args, "until", "") or ""
    catch_up = getattr(args, "retro_catch_up", False)

    if not since:
        since = _compute_default_since()
    if not until:
        until = _compute_default_until()

    return _run_commands_py(root, "retro_prepare", {
        "BEACON_SINCE": since,
        "BEACON_UNTIL": until,
        "BEACON_RETRO_CATCH_UP": "1" if catch_up else "",
    })


def _compute_default_since() -> str:
    """Compute the default ``--since`` for ``beacon retro --prepare``.

    Mirrors bin/beacon's logic: if today is Monday, anchor on a week ago;
    otherwise anchor on the most recent Monday. This is the fallback when
    the (preferred) ``cmd_retro_default_since`` helper isn't reachable; we
    skip the .reviewed marker / retro_day inspection here because that
    requires reading project state, and the bash wrapper itself falls back
    to the same Monday-anchor math when the helper returns empty.
    """
    import datetime
    today = datetime.date.today()
    # weekday(): Mon=0 ... Sun=6 ; bash's date +%u: Mon=1 ... Sun=7
    dow_bash = today.weekday() + 1
    if dow_bash == 1:
        since = today - datetime.timedelta(days=7)
    else:
        since = today - datetime.timedelta(days=dow_bash - 1)
    return since.strftime("%Y-%m-%d")


def _compute_default_until() -> str:
    """Compute the default ``--until`` for ``beacon retro --prepare``.

    Mirrors bin/beacon: today, except on Monday return yesterday so the
    cover-window ends on Sunday (= end of the prior ISO week).
    """
    import datetime
    today = datetime.date.today()
    if today.weekday() == 0:  # Monday
        until = today - datetime.timedelta(days=1)
    else:
        until = today
    return until.strftime("%Y-%m-%d")


def _handle_stop(root: Path, args: argparse.Namespace) -> int:
    """`beacon stop scoped|global|status` (ms-55 e-1646)."""
    if args.show_help or getattr(args, "stop_cmd", None) is None:
        print(
            "Usage: beacon stop scoped --target <kind>:<id> [--reason \"...\"] "
            "[--reason-kind X] [--machine-reason '<json>'] [--json]"
        )
        print(
            "       beacon stop global [--reason \"...\"] "
            "[--reason-kind X] [--machine-reason '<json>'] [--json]"
        )
        print("       beacon stop status [--json]")
        return 0 if args.show_help else 2

    if args.stop_cmd == "scoped":
        kind, sid = _split_target(getattr(args, "target", "") or "")
        if getattr(args, "target", "") and ":" not in args.target:
            _eprint("Error: --target must be <kind>:<id> (e.g. ms:ms-55)")
            return 1
        return _run_commands_py(root, "stop_scoped", {
            "BEACON_STOP_TARGET_KIND": kind,
            "BEACON_STOP_TARGET_ID": sid,
            "BEACON_STOP_REASON": getattr(args, "reason", "") or "",
            "BEACON_STOP_REASON_KIND": getattr(args, "reason_kind", "") or "",
            "BEACON_STOP_MACHINE_REASON": getattr(args, "machine_reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if args.stop_cmd == "global":
        return _run_commands_py(root, "stop_global", {
            "BEACON_STOP_REASON": getattr(args, "reason", "") or "",
            "BEACON_STOP_REASON_KIND": getattr(args, "reason_kind", "") or "",
            "BEACON_STOP_MACHINE_REASON": getattr(args, "machine_reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if args.stop_cmd == "status":
        return _run_commands_py(root, "stop_status", {
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    _eprint(f"Unknown stop subcommand: {args.stop_cmd}")
    return 2


def _handle_resume(root: Path, args: argparse.Namespace) -> int:
    """`beacon resume scoped|global` (ms-55 e-1646 / paired with stop)."""
    if args.show_help or getattr(args, "resume_cmd", None) is None:
        print(
            "Usage: beacon resume scoped --target <kind>:<id> [--reason \"...\"] [--json]"
        )
        print("       beacon resume global [--reason \"...\"] [--json]")
        return 0 if args.show_help else 2

    if args.resume_cmd == "scoped":
        kind, sid = _split_target(getattr(args, "target", "") or "")
        if getattr(args, "target", "") and ":" not in args.target:
            _eprint("Error: --target must be <kind>:<id>")
            return 1
        return _run_commands_py(root, "resume_scoped", {
            "BEACON_STOP_TARGET_KIND": kind,
            "BEACON_STOP_TARGET_ID": sid,
            "BEACON_STOP_REASON": getattr(args, "reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if args.resume_cmd == "global":
        return _run_commands_py(root, "resume_global", {
            "BEACON_STOP_REASON": getattr(args, "reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    _eprint(f"Unknown resume subcommand: {args.resume_cmd}")
    return 2


def _handle_rollback(root: Path, args: argparse.Namespace) -> int:
    """`beacon rollback` (ms-55 e-1647 + e-1727 --no-record)."""
    if args.show_help:
        print(
            "Usage: beacon rollback [--commits N] [--reason \"...\"] "
            "[--dry-run] [--no-record] [--json]"
        )
        return 0
    return _run_commands_py(root, "rollback", {
        "BEACON_ROLLBACK_COMMITS": str(getattr(args, "commits", "0") or "0"),
        "BEACON_ROLLBACK_REASON": getattr(args, "reason", "") or "",
        "BEACON_ROLLBACK_DRY_RUN": "1" if getattr(args, "dry_run", False) else "",
        "BEACON_ROLLBACK_NO_RECORD": "1" if getattr(args, "no_record", False) else "",
        "BEACON_JSON": "1" if getattr(args, "json", False) else "",
    })


def _handle_claim(root: Path, args: argparse.Namespace) -> int:
    """`beacon claim request|handoff|post|respond|release|list` (ms-55 e-1648).

    The bash dispatcher remaps subcommand → commands.py verb name (e.g.
    `request` → `claim_request`). We do the same; lib/claims.py is pure
    Python so the underlying logic runs identically on Windows pipx.
    """
    sub_cmd = getattr(args, "claim_cmd", None)
    if args.show_help or sub_cmd is None:
        print("Usage:")
        print(
            "  beacon claim post     --target <kind>:<id> [--intent \"...\"] [--json]"
        )
        print(
            "  beacon claim request  --target <kind>:<id> --to <sid> [--intent \"...\"]"
        )
        print(
            "  beacon claim handoff  --target <kind>:<id> --to <sid> [--intent \"...\"]"
        )
        print(
            "  beacon claim respond  <claim-id> [--accept|--decline] [--reason \"...\"]"
        )
        print(
            "  beacon claim release  <claim-id> [--outcome completed|abandoned] [--reason \"...\"]"
        )
        print("  beacon claim list     [--mine] [--target <k>:<id>] [--json]")
        return 0 if args.show_help else 2

    if sub_cmd in ("request", "handoff", "post"):
        kind, sid = _split_target(getattr(args, "target", "") or "")
        if getattr(args, "target", "") and ":" not in args.target:
            _eprint("Error: --target must be <kind>:<id> (e.g. task:e-1648)")
            return 1
        return _run_commands_py(root, f"claim_{sub_cmd}", {
            "BEACON_CLAIM_TARGET_KIND": kind,
            "BEACON_CLAIM_TARGET_ID": sid,
            "BEACON_CLAIM_INTENT": getattr(args, "intent", "") or "",
            "BEACON_CLAIM_TO": getattr(args, "to", "") or "",
            "BEACON_CLAIM_EXPIRES_AT": getattr(args, "expires_at", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if sub_cmd == "respond":
        if not args.claim_id:
            _eprint("Error: beacon claim respond <claim_id> [--accept|--decline]")
            return 1
        decision = ""
        if args.accept:
            decision = "accept"
        elif args.decline:
            decision = "decline"
        return _run_commands_py(root, "claim_respond", {
            "BEACON_CLAIM_ID": args.claim_id,
            "BEACON_CLAIM_DECISION": decision,
            "BEACON_CLAIM_REASON": getattr(args, "reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if sub_cmd == "release":
        if not args.claim_id:
            _eprint("Error: beacon claim release <claim_id> [--outcome completed|abandoned]")
            return 1
        return _run_commands_py(root, "claim_release", {
            "BEACON_CLAIM_ID": args.claim_id,
            "BEACON_CLAIM_OUTCOME": getattr(args, "outcome", "") or "",
            "BEACON_CLAIM_REASON": getattr(args, "reason", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    if sub_cmd in ("list", "ls"):
        kind, sid = _split_target(getattr(args, "target", "") or "")
        return _run_commands_py(root, "claim_list", {
            "BEACON_CLAIM_MINE": "1" if getattr(args, "mine", False) else "",
            "BEACON_CLAIM_TARGET_KIND": kind,
            "BEACON_CLAIM_TARGET_ID": sid,
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        })
    _eprint(f"Unknown claim subcommand: {sub_cmd}")
    return 2


def _handle_stuck(root: Path, args: argparse.Namespace) -> int:
    """`beacon stuck check` (ms-55 e-1649)."""
    if args.show_help or getattr(args, "stuck_cmd", None) is None:
        print(
            "Usage: beacon stuck check (--telemetry-inline '<json>' | "
            "--telemetry-file <path>) [--timeout-minutes N] [--dry-run] [--json]"
        )
        return 0 if args.show_help else 2
    if args.stuck_cmd != "check":
        _eprint(f"Unknown stuck subcommand: {args.stuck_cmd}")
        return 2
    return _run_commands_py(root, "stuck_check", {
        "BEACON_STUCK_TELEMETRY_INLINE": getattr(args, "telemetry_inline", "") or "",
        "BEACON_STUCK_TELEMETRY_FILE": getattr(args, "telemetry_file", "") or "",
        "BEACON_STUCK_TIMEOUT_MINUTES": getattr(args, "timeout_minutes", "") or "30",
        "BEACON_STUCK_DRY_RUN": "1" if getattr(args, "dry_run", False) else "",
        "BEACON_JSON": "1" if getattr(args, "json", False) else "",
    })


def _handle_morning(root: Path, args: argparse.Namespace) -> int:
    """`beacon morning` (ms-55 e-1650 + e-1733 --no-doc)."""
    if args.show_help:
        print(
            "Usage: beacon morning [--since-hours N] [--events-file <path>] "
            "[--no-doc] [--json]"
        )
        return 0
    return _run_commands_py(root, "morning", {
        "BEACON_MORNING_SINCE_HOURS": getattr(args, "since_hours", "") or "12",
        "BEACON_MORNING_EVENTS_FILE": getattr(args, "events_file", "") or "",
        "BEACON_MORNING_NO_DOC": "1" if getattr(args, "no_doc", False) else "",
        "BEACON_JSON": "1" if getattr(args, "json", False) else "",
    })


def _handle_monitor(root: Path, args: argparse.Namespace) -> int:
    """`beacon monitor context [--dry-run]` (ms-44 e-854).

    Delegates to ``beacon_cli.hooks.context_monitor.main`` so the same
    code that Claude Code's Stop hook calls can be invoked from the CLI
    (useful for one-shot debugging on Windows where users can't easily
    pipe a JSON payload through Bash). ``--dry-run`` toggles the env var
    the module reads to skip note/state writes.

    Note: in normal operation, Claude Code calls
    ``beacon-hook-context-monitor`` (the console-script entry-point)
    directly — this CLI subcommand is the discoverable equivalent for
    users who want to test the hook without configuring it.
    """
    if args.show_help or args.monitor_cmd is None:
        print("Usage: beacon monitor context [--dry-run]")
        return 0 if args.show_help else 2
    if args.monitor_cmd != "context":
        print(f"Unknown monitor subcommand: {args.monitor_cmd}")
        return 2

    if getattr(args, "dry_run", False):
        os.environ["BEACON_CONTEXT_MONITOR_DRY_RUN"] = "1"

    try:
        from beacon_cli.hooks.context_monitor import main as _cm_main
    except Exception as exc:  # pragma: no cover — defensive only
        _eprint(f"context-monitor entry-point not importable: {exc}")
        return 0
    return _cm_main()


# ms-54 e-1151: bus subcommand handler. Each branch mirrors the bash dispatcher's
# env layout exactly (BEACON_BUS_* family) so commands.py reads the same
# variables regardless of which entry point spawned it. The --project flag on
# every bus_X subparser is collapsed here into BEACON_BUS_PROJECT_ID which
# commands.py:_resolve_bus_project_id consumes; cross-project sends/receives
# become possible without flipping cwd.
def _handle_profile(root: Path, args: argparse.Namespace) -> int:
    if args.show_help or getattr(args, "profile_cmd", None) is None:
        print("Usage: beacon profile list [--json]")
        print("")
        print("List Beacon profiles under ~/.beacon/profiles/. The active")
        print("profile (per --profile / BEACON_PROFILE / cwd cloud.json) is")
        print("marked with `*`.")
        return 0 if args.show_help else 2

    if args.profile_cmd == "list":
        return _run_commands_py(root, "profile_list", {
            "BEACON_JSON": "1" if args.json else "",
        })

    print(f"Unknown profile subcommand: {args.profile_cmd}")
    return 2


def _handle_sessions(root: Path, args: argparse.Namespace) -> int:
    if args.show_help:
        print("Usage: beacon sessions [list] [--live] [--healthy] "
              "[--since-min <N>] [--machine <name>] [--agent <name>] [--json]")
        print("")
        print("Cross-project session directory. Lists the calling user's")
        print("bclaude sessions across ALL projects they own or are a member")
        print("of. Use this for picking DM recipients (/beacon-dm-send) and")
        print("diagnosing cross-project heartbeat-stop incidents.")
        print("")
        print("For \"who in this project is live\" use `beacon bus directory --live`.")
        return 0

    env: Dict[str, str] = {
        "BEACON_SESSIONS_LIVE": "1" if args.live else "",
        "BEACON_SESSIONS_HEALTHY": "1" if args.healthy else "",
        "BEACON_SESSIONS_SINCE_MIN": args.since_min or "5",
        "BEACON_SESSIONS_MACHINE": args.machine or "",
        "BEACON_SESSIONS_AGENT": args.agent or "",
        "BEACON_JSON": "1" if args.json else "",
    }
    return _run_commands_py(root, "sessions_list", env)


def _expand_trek_recipients(root: Path, trek_id: str) -> Optional[list]:
    """Expand ``--to-trek <trek-id>`` into the trek's joined member sessions.

    Returns a list of session ids (= ``leader_session_id`` if leader is in
    the trek, plus any session ids stored against joined members). On error
    (= trek not found, no joined members, lookup failure) returns ``None``
    after printing a user-facing error to stderr, matching the dispatch
    convention used by ``_ensure_project``.

    Local mode resolution: walks ``~/.beacon/treks/<trek_id>.json`` and
    extracts the ``leader_session_id``. Member-level session ids are not
    persisted at this layer (ms-69 schema keeps membership at user grain
    + a single leader session); for fan-out beyond the leader we add a
    todo and a graceful warning so the surface is honest about what it
    can do today. Cloud mode does the equivalent via the API client.

    ms-75 / e-1858: this exists as a *thin wrapper* over the legacy single
    recipient path (= per-recipient subprocess) instead of teaching
    ``cmd_bus_send`` itself about fan-out. Each send still passes through
    the envelope / budget gates independently, so multi-target broadcasts
    don't bypass any single-recipient safety check.
    """
    import json
    import subprocess

    # Try local store first; fall back to a beacon trek show invocation
    # when local lookup misses (= cloud-only treks).
    try:
        treks_dir = os.environ.get("BEACON_TREKS_DIR") or \
            os.path.expanduser("~/.beacon/treks")
        local_path = os.path.join(treks_dir, f"{trek_id}.json")
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                trek_doc = json.load(f)
        else:
            # Cloud-mode resolution: ask `beacon trek show <id> --json` —
            # the same dispatch path used by /beacon-trek-execute.
            res = subprocess.run(
                ["beacon", "trek", "show", trek_id, "--json"],
                capture_output=True, text=True, check=False,
            )
            if res.returncode != 0 or not res.stdout.strip():
                print(
                    f"Error: --to-trek {trek_id} → trek not found "
                    f"(local store empty, `beacon trek show` returned "
                    f"rc={res.returncode}).",
                    file=sys.stderr,
                )
                return None
            trek_doc = json.loads(res.stdout)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Error: --to-trek {trek_id} → failed to load trek doc: {e}",
            file=sys.stderr,
        )
        return None

    recipients: list[str] = []
    leader_sid = (trek_doc.get("leader_session_id") or "").strip()
    if leader_sid:
        recipients.append(leader_sid)

    # Membership is at user grain (ms-69 SPEC 設計方針 3): a "session id
    # per member" field doesn't live in the trek doc. The Tier-1 use case
    # for --to-trek today is "page the trek leader + any other live
    # sessions belonging to joined members". Live-session resolution per
    # member is a follow-up — we surface a one-line note so the caller
    # knows what fan-out actually happened.
    joined_count = sum(
        1 for m in trek_doc.get("members") or [] if m.get("joined_at")
    )
    if joined_count > 1:
        print(
            f"Note: --to-trek {trek_id} fanned out to the leader session "
            f"({leader_sid or '(none)'}) only. Member-level session id "
            f"resolution lands in the cloud-mode follow-up; for now, the "
            f"other {joined_count - 1} joined member(s) need to be "
            f"addressed individually with --to <session-id>.",
            file=sys.stderr,
        )

    if not recipients:
        print(
            f"Error: --to-trek {trek_id} → no joined sessions to fan out "
            "to (trek has no leader_session_id).",
            file=sys.stderr,
        )
        return None
    return recipients


def _handle_bus(root: Path, args: argparse.Namespace) -> int:
    """`beacon bus <send|listen|receive|ack|status|directory|budget>`.

    Bash↔Python role split (ms-73 e-1763):
      - **bus.mjs** (Node) is the *transport / persistence* layer. It owns
        the bridge poll loop that stamps session heartbeat, and is the
        only writer of bus events into the cloud bus collection.
      - **commands.py** (Python, via this dispatch) is the *operator
        surface*: enumerate (directory), enqueue an outbound event (send),
        block-read (listen / receive), advance the cursor (ack), and
        inspect (status). It is what humans + Skills call from the CLI.
      - **dispatch.py** (this file) is the *argv → env shim* that lets
        Windows pipx callers reach the same commands.py entry points
        without a bash dispatcher in front.
    The transport layer (bus.mjs) is not duplicated in Python — it runs
    out-of-process and is OS-independent.
    """
    if args.show_help or args.bus_cmd is None:
        print("Usage: beacon bus send      --channel <ch> [--payload '<json>'] "
              "[--sender <id>] [--to <recipient> ...] [--to-trek <trek-id>] "
              "[--delivery <mode>] [--in-reply-to <event_id>] "
              "[--action <name> ...] [--no-envelope] [--project <id>]")
        print("       beacon bus listen    [--recipient <id>] [--channel <ch>] "
              "[--interval <sec>] [--auto-ack] [--once] [--project <id>]")
        print("       beacon bus receive   [--recipient <id>] [--channel <ch>] "
              "[--timeout <sec>] [--auto-ack] [--project <id>]")
        print("       beacon bus ack       [--recipient <id>] "
              "--last-seen-at <iso8601> [--event <event_id>] [--project <id>]")
        print("       beacon bus status    <event_id> [--project <id>] [--json]")
        print("       beacon bus directory [--user <email>] [--machine <name>] "
              "[--agent <name>] [--live] [--healthy] [--since-min <N>] "
              "[--project <id>] [--json]")
        print("       beacon bus budget    grant --turns <N>  |  show  |  clear")
        print("")
        print("delivery: auto-execute | propose-to-ai (default) | notify-user-only")
        print("--to: cross-user DM recipient session id (Tier-1 envelope "
              "path). Repeatable: multiple --to flags fan out the same "
              "send to each recipient (ms-75 / e-1858).")
        print("--to-trek: expand to the trek's joined session(s). Equivalent "
              "to passing --to for each trek member (= broadcast within a "
              "Trek scope).")
        print("--action: repeat for each authorized action; comma-joined "
              "for envelope issuance (e-1290).")
        print("--no-envelope: opt-out of envelope issuance (legacy / debug).")
        print("--in-reply-to: mark as AI-authored reply. Requires `bus budget "
              "grant N` first.")
        print("--project: override the cwd's project_id (e-1151) for cross-"
              "project DM/directory.")
        return 0 if args.show_help else 2

    cmd = args.bus_cmd
    # Common: --project propagates to commands.py via BEACON_BUS_PROJECT_ID.
    project_id = getattr(args, "bus_project_id", "") or ""

    if cmd == "send":
        # ms-73 e-1763: comma-join repeated --action values so commands.py
        # receives the same shape as bin/beacon's BEACON_BUS_ACTION.
        actions = getattr(args, "bus_action", []) or []
        bus_action_csv = ",".join([a for a in actions if a])

        # ms-75 / e-1858: collect recipients from --to (repeatable) and
        # --to-trek (= expand to trek member session ids). The legacy
        # bash entry point passes a single string in
        # BEACON_BUS_RECIPIENT_SESSION; we keep that contract by looping
        # one send per recipient when multiple are supplied. Single
        # recipient stays a single subprocess invocation (= no behaviour
        # change for existing scripts).
        bus_to_raw = getattr(args, "bus_to", []) or []
        if isinstance(bus_to_raw, str):
            bus_to_list = [bus_to_raw] if bus_to_raw else []
        else:
            bus_to_list = [r for r in bus_to_raw if r]
        bus_to_trek = (getattr(args, "bus_to_trek", "") or "").strip()
        if bus_to_trek:
            extras = _expand_trek_recipients(root, bus_to_trek)
            if extras is None:
                # _expand_trek_recipients already printed an error.
                return 1
            bus_to_list.extend(extras)
        # Deduplicate while preserving order; treat the sender's own
        # session as harmless (= same-user broadcast within their own
        # treks). Self-filtering is up to the caller's policy.
        seen: set[str] = set()
        dedup_list: list[str] = []
        for r in bus_to_list:
            if r not in seen:
                seen.add(r)
                dedup_list.append(r)
        bus_to_list = dedup_list

        base_env: Dict[str, str] = {
            "BEACON_BUS_CHANNEL": args.channel or "",
            "BEACON_BUS_PAYLOAD": args.payload or "",
            "BEACON_BUS_SENDER": args.sender or "",
            "BEACON_BUS_DELIVERY": args.delivery or "",
            "BEACON_BUS_IN_REPLY_TO": args.in_reply_to or "",
            # ms-95 e-2441: pre-populate BEACON_BUS_RECIPIENT_SESSION as empty
            # so the no-recipient branch (= broadcast channels) still passes
            # the env var through, matching the bash dispatcher contract that
            # always exports it. Overridden per-recipient inside the loop.
            "BEACON_BUS_RECIPIENT_SESSION": "",
            "BEACON_BUS_ACTION": bus_action_csv,
            "BEACON_BUS_NO_ENVELOPE": "1" if getattr(args, "no_envelope", False) else "",
            "BEACON_JSON": "1" if args.json else "",
        }
        if project_id:
            base_env["BEACON_BUS_PROJECT_ID"] = project_id

        if not bus_to_list:
            # Preserve legacy behaviour: no --to / --to-trek means the
            # caller intentionally targeted a non-DM broadcast channel
            # (cmd_bus_send warns if channel == 'dm' anyway).
            return _run_commands_py(root, "bus_send", base_env)

        rc_final = 0
        for recipient in bus_to_list:
            env = dict(base_env)
            env["BEACON_BUS_RECIPIENT_SESSION"] = recipient
            rc = _run_commands_py(root, "bus_send", env)
            # Any failure surfaces in the exit code, but we keep fanning
            # out the rest so a single bad recipient doesn't drop the
            # entire broadcast.
            if rc != 0:
                rc_final = rc
        return rc_final

    if cmd == "listen":
        env = {
            "BEACON_BUS_RECIPIENT": args.recipient or "",
            "BEACON_BUS_CHANNEL": args.channel or "",
            "BEACON_BUS_INTERVAL": args.interval or "",
            "BEACON_BUS_AUTO_ACK": "1" if args.auto_ack else "",
            "BEACON_BUS_ONCE": "1" if args.once else "",
        }
        if project_id:
            env["BEACON_BUS_PROJECT_ID"] = project_id
        return _run_commands_py(root, "bus_listen", env)

    if cmd == "receive":
        env = {
            "BEACON_BUS_RECIPIENT": args.recipient or "",
            "BEACON_BUS_CHANNEL": args.channel or "",
            "BEACON_BUS_INTERVAL": args.interval or "",
            "BEACON_BUS_TIMEOUT": args.timeout or "",
            "BEACON_BUS_AUTO_ACK": "1" if args.auto_ack else "",
        }
        if project_id:
            env["BEACON_BUS_PROJECT_ID"] = project_id
        return _run_commands_py(root, "bus_receive", env)

    if cmd == "ack":
        # ms-73 e-1763: --event <id> for explicit single-event ack (e-1423).
        env = {
            "BEACON_BUS_RECIPIENT": args.recipient or "",
            "BEACON_BUS_LAST_SEEN_AT": args.last_seen_at or "",
            "BEACON_BUS_ACK_EVENT_ID": getattr(args, "bus_ack_event_id", "") or "",
            "BEACON_JSON": "1" if args.json else "",
        }
        if project_id:
            env["BEACON_BUS_PROJECT_ID"] = project_id
        return _run_commands_py(root, "bus_ack", env)

    if cmd == "status":
        env = {
            "BEACON_BUS_EVENT_ID": args.event_id or "",
            "BEACON_JSON": "1" if args.json else "",
        }
        if project_id:
            env["BEACON_BUS_PROJECT_ID"] = project_id
        return _run_commands_py(root, "bus_status", env)

    if cmd in ("directory", "dir"):
        env = {
            "BEACON_DIR_USER": args.user or "",
            "BEACON_DIR_MACHINE": args.machine or "",
            "BEACON_DIR_AGENT": args.agent or "",
            "BEACON_DIR_LIVE": "1" if args.live else "",
            "BEACON_DIR_SINCE_MIN": args.since_min or "",
            "BEACON_DIR_HEALTHY": "1" if getattr(args, "healthy", False) else "",
            "BEACON_JSON": "1" if args.json else "",
        }
        if project_id:
            env["BEACON_BUS_PROJECT_ID"] = project_id
        return _run_commands_py(root, "bus_directory", env)

    if cmd == "budget":
        bsub = getattr(args, "bus_budget_cmd", None)
        if bsub is None:
            print("Usage: beacon bus budget grant --turns <N> [--json]")
            print("       beacon bus budget show [--json]")
            print("       beacon bus budget clear")
            return 2
        if bsub == "grant":
            return _run_commands_py(root, "bus_budget_grant", {
                "BEACON_BUS_BUDGET_N": args.turns or "",
                "BEACON_JSON": "1" if args.json else "",
            })
        if bsub == "show":
            return _run_commands_py(root, "bus_budget_show", {
                "BEACON_JSON": "1" if args.json else "",
            })
        if bsub == "clear":
            return _run_commands_py(root, "bus_budget_clear", {})
        print(f"Unknown bus budget subcommand: {bsub}")
        return 2

    print(f"Unknown bus subcommand: {cmd}")
    return 2


def _handle_dm(root: Path, args: argparse.Namespace) -> int:
    """`beacon dm <respond|log>` (ms-70 / e-1716, e-1923).

    Two subverbs:
      * ``respond approve|deny <event_id>`` — receiver-side decision on
        a pending DM-action sidecar. Reaches POST /api/projects/{pid}/
        dm/approval/{event_id} via cmd_dm_respond. Order of approve|deny
        and event_id is flexible (both positional shapes accepted).
      * ``log [<project_id>] [--limit N]`` — audit-trail viewer in the
        terminal. Mirror of the Web UI Settings > Audit table; reaches
        GET /api/projects/{pid}/dm/approval/history.

    SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ")
    is enforced server-side: the CLI carries the human's Bearer token
    and the server stamps decision_by from the token's sub claim — neither
    `dm respond` argv nor `dm log` argv can spoof a different actor.
    """
    if args.show_help or getattr(args, "dm_cmd", None) is None:
        print("Usage: beacon dm respond approve <event_id> [--project <id>] [--json]")
        print("       beacon dm respond deny    <event_id> [--project <id>] [--json]")
        print("       beacon dm log    [<project_id>] [--limit N] [--project <id>] [--json]")
        print("")
        print("Decide a pending cross-user DM action envelope (respond), or")
        print("scroll the audit log of already-decided rows (log).")
        print("")
        print("respond: only the intended receiver can press approve / deny;")
        print("         the server enforces this via the Bearer token.")
        print("log:     read-only mirror of the Web UI Settings > Audit table.")
        return 0 if args.show_help else 2

    cmd = args.dm_cmd

    if cmd == "respond":
        # Parse the flexible positional pair: accept either
        # `respond approve <evt>` or `respond <evt> approve`. Empty / bad
        # input falls through to the handler's usage error.
        decision = ""
        event_id = ""
        for tok in getattr(args, "respond_args", []) or []:
            if tok in ("approve", "deny") and not decision:
                decision = tok
            elif not event_id:
                event_id = tok
        env: Dict[str, str] = {
            "BEACON_DM_DECISION": decision,
            "BEACON_DM_EVENT_ID": event_id,
            "BEACON_BUS_PROJECT_ID": getattr(args, "dm_project_id", "") or "",
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        }
        return _run_commands_py(root, "dm_respond", env)

    if cmd == "log":
        # `--project <id>` (override) wins over the positional project_id;
        # both feed _resolve_bus_project_id in commands.py (matches the
        # bash dispatch precedence).
        positional_pid = getattr(args, "project_id", "") or ""
        override_pid = getattr(args, "dm_project_id", "") or ""
        env = {
            "BEACON_DM_LOG_PROJECT_ID": positional_pid,
            "BEACON_DM_LOG_LIMIT": getattr(args, "limit", "") or "",
            "BEACON_BUS_PROJECT_ID": override_pid or positional_pid,
            "BEACON_JSON": "1" if getattr(args, "json", False) else "",
        }
        return _run_commands_py(root, "dm_log", env)

    print(f"Unknown dm subcommand: {cmd}")
    return 2


# ---------------------------------------------------------------------------
# Top-level entry — argv parse + dispatch
# ---------------------------------------------------------------------------


_HANDLERS: Dict[str, Callable[[Path, argparse.Namespace], int]] = {
    "init": _handle_init,
    "status": _handle_status,
    "summary": _handle_summary,
    "log": _handle_log,
    "save": _handle_save,
    "sync": _handle_sync,
    "task": _handle_task,
    "milestone": _handle_milestone,
    "ms": _handle_milestone,
    "doc": _handle_doc,
    "document": _handle_doc,
    "note": _handle_note,
    "trigger": _handle_trigger,
    "search": _handle_search,
    "cycle": _handle_cycle,
    "push": _handle_push,
    "deploy": _handle_deploy,
    "entry": _handle_entry,
    "operation": _handle_operation,
    "trek": _handle_trek,
    "project": _handle_project,
    "doctor": _handle_doctor,
    "skill": _handle_skill,
    "auth": _handle_auth,
    "cloud": _handle_cloud,
    "pr": _handle_pr,
    "issue": _handle_issue,
    "member": _handle_member,
    "session": _handle_session,
    "sessions": _handle_sessions,
    "profile": _handle_profile,
    "channel": _handle_channel,
    # ms-61 / e-2348: Win parity for /beacon-retro Skill (prepare/save/done).
    "retro": _handle_retro,
    "bus": _handle_bus,
    # ms-70 / e-1716 + e-1923: cross-user DM approval primitives (Win mirror
    # of bin/beacon's `dm)` case). `dm respond` + `dm log`.
    "dm": _handle_dm,
    "monitor": _handle_monitor,
    # ms-55 coordination signals (e-1735) — Win parity for the 6 verbs
    # in the 走る / 止まる両輪 surface.
    "stop": _handle_stop,
    "resume": _handle_resume,
    "rollback": _handle_rollback,
    "claim": _handle_claim,
    "stuck": _handle_stuck,
    "morning": _handle_morning,
}


def _print_top_help() -> None:
    print(
        f"beacon {__version__} — AI-driven milestone tracker (PowerShell-native)\n"
        "\n"
        "Day-1 commands (work without bash):\n"
        "  beacon --version                  Show version\n"
        "  beacon init [--name N --objective O --retro-day D --storage local|cloud]\n"
        "  beacon status [--json] [--ms <id>]\n"
        "  beacon summary \"text\"\n"
        "  beacon log [--ms id] [--summary text]\n"
        "  beacon task add \"desc\" [-m ms-id] [--priority P] [--motivation T] [--ac T]\n"
        "  beacon task done <entry-id>\n"
        "  beacon task list [-m ms-id]\n"
        "  beacon task update <entry-id> [--description T] [--status S] ...\n"
        "  beacon milestone add \"title\" [--priority P] [--objective O] [--ac A]\n"
        "  beacon milestone list | start <id> | done <id> | observe <id> | show <id>\n"
        "  beacon milestone graph [--json] | workspace <id> | workspace-cleanup <id>\n"
        "  beacon doc add \"title\" [--scope core|spec|memo|retro|report] [--ms id]\n"
        "  beacon doc list [--scope S] [--ms id]\n"
        "  beacon doc show <doc-id>\n"
        "  beacon note \"<text>\" | note list | note clear\n"
        "  beacon search \"query\" [--ms id] [--scope S]\n"
        "  beacon trigger fire|check|clear [name]\n"
        "  beacon retro [--prepare|--catch-up] [--since X] [--until Y]\n"
        "  beacon retro save --week YYYY-WNN [--content T | --stdin] [--json]\n"
        "  beacon retro done\n"
        "  beacon cycle status\n"
        "  beacon push record|list\n"
        "  beacon deploy record|list\n"
        "  beacon entry move <entry-id> [-t task-id | -m ms-id]\n"
        "  beacon project archive|unarchive\n"
        "  beacon doctor\n"
        "  beacon skill install [--force]\n"
        "  beacon session id\n"
        "  beacon channel install\n"
        "  beacon monitor context [--dry-run]\n"
        "\n"
        "Not yet available on bash-less systems (tracked under ms-44):\n"
        "  beacon setup, dashboard (tmux), beacon update, beacon pr review,\n"
        "  beacon cloud open/launch (tmux dashboard),\n"
        "  beacon operation/run/incident/member.\n"
    )


def dispatch(root: Optional[Path], argv: Sequence[str]) -> int:
    """Parse argv and dispatch to a Phase-1 handler.

    Returns the subprocess exit code (or a Python-side error code).
    """
    parser = build_parser()

    # Treat bare/empty invocation as help. The tmux dashboard launcher is
    # bash-only, so on a Windows shell there is no sensible default action.
    if not argv:
        _print_top_help()
        return 0

    # We want --version/--help to work even when commands.py is unreachable.
    if argv[0] in ("--version", "-V", "version"):
        print(f"beacon {__version__}")
        return 0
    if argv[0] in ("--help", "-h", "help"):
        _print_top_help()
        return 0

    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:  # argparse calls sys.exit() on parse errors
        return int(exc.code) if isinstance(exc.code, int) else 2

    if args.version:
        print(f"beacon {__version__}")
        return 0
    if args.help and not args.command:
        _print_top_help()
        return 0
    if not args.command:
        _print_top_help()
        return 0

    # ms-64 / e-1458: persist --profile into the env before any handler runs
    # so child processes (commands.py subprocess) inherit it.
    if getattr(args, "profile", ""):
        os.environ["BEACON_PROFILE"] = args.profile

    # e-1227 (ms-17): stash the cwd at invocation time *before* the
    # relocate below changes it. Handlers that need git HEAD from the
    # user's actual location (notably ``beacon log``, which runs inside
    # parallel sub-agent worktrees where .beacon/ lives in the parent
    # repo) read this via ``get_invocation_cwd()`` and pass it to
    # ``git -C`` so they don't accidentally capture the parent repo's
    # HEAD after the relocate.
    global _invocation_cwd
    _invocation_cwd = Path.cwd()

    # Locate the project root by walking up from CWD so beacon works from
    # any subdirectory (e-862). Must happen before the handler runs and
    # before commands.py is spawned (it inherits this cwd).
    _relocate_to_project_root(args.command)

    handler = _HANDLERS.get(args.command)
    if handler is None:
        print(f"Unknown command: {args.command}")
        _print_top_help()
        return 1

    if root is None:
        _eprint(
            "Error: beacon installation is incomplete — could not locate the "
            "`lib/` directory. Reinstall with `pipx reinstall beacon` or "
            "ensure the source checkout includes lib/commands.py."
        )
        return 2

    return handler(root, args)
