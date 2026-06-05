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

    sub = p.add_subparsers(dest="command", metavar="<command>")

    # ---- init ----
    p_init = sub.add_parser("init", help="Initialize .beacon/ in current dir", add_help=False)
    p_init.add_argument("--name")
    p_init.add_argument("--objective")
    p_init.add_argument("--retro-day", dest="retro_day")
    p_init.add_argument("--storage", default="local")
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
    p_task_add.add_argument("description", nargs="?", default="")
    p_task_add.add_argument("-m", "--ms", dest="ms_id", default="")
    p_task_add.add_argument("-t", "--type", dest="entry_type", default="task")
    p_task_add.add_argument("-d", "--detail", default="")
    p_task_add.add_argument("--from", dest="requested_by", default="")
    p_task_add.add_argument("--priority", default="")
    p_task_add.add_argument("--motivation", "--why", dest="motivation", default="")
    p_task_add.add_argument(
        "--acceptance-criteria", "--ac", dest="acceptance_criteria", default=""
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
    p_doc_add.add_argument("--json", action="store_true")
    p_doc_add.add_argument("--content", default="")
    p_doc_add.add_argument("--stdin", action="store_true")

    p_doc_list = doc_sub.add_parser("list", aliases=["ls"], add_help=False)
    p_doc_list.add_argument("--json", action="store_true")
    p_doc_list.add_argument("--scope", "-s", dest="scope", default="")
    p_doc_list.add_argument("--ms", dest="doc_ms", default="")
    p_doc_list.add_argument("--op", dest="doc_op", default="")

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
    p_doc_update.add_argument("--json", action="store_true")
    p_doc_update.add_argument("--stdin", action="store_true")

    p_doc_delete = doc_sub.add_parser("delete", aliases=["rm"], add_help=False)
    p_doc_delete.add_argument("doc_id", nargs="?", default="")
    p_doc_delete.add_argument("--json", action="store_true")

    # ---- note ----
    p_note = sub.add_parser("note", help="Session note operations", add_help=False)
    p_note.add_argument("text_or_sub", nargs="?", default="")
    p_note.add_argument("--context", default="")
    p_note.add_argument("--json", action="store_true")
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

    # ---- operation (purge only — full operation CRUD remains bash-only) ----
    p_op = sub.add_parser("operation", help="Operation operations (purge)", add_help=False)
    p_op.add_argument("--help", "-h", action="store_true", dest="show_help")
    op_sub = p_op.add_subparsers(dest="op_cmd", metavar="<subcmd>")
    p_op_purge = op_sub.add_parser("purge", add_help=False)
    p_op_purge.add_argument("op_id", nargs="?", default="")
    p_op_purge.add_argument("-r", "--reason", default="")
    p_op_purge.add_argument("--index", default="")
    p_op_purge.add_argument("--json", action="store_true")

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
    p_skill_install.add_argument("--settings-path", dest="settings_path", default="")

    # ---- auth login / logout / status (cloud OAuth) ----
    # `beacon auth login` opens a browser and signs in with Google so the
    # cloud project APIs (firestore / WS) become reachable. commands.py
    # delegates straight to the `auth` module — no env vars in play, the
    # interactive flow is owned by `auth.login()` itself.
    p_auth = sub.add_parser("auth", help="Cloud authentication", add_help=False)
    p_auth.add_argument("--help", "-h", action="store_true", dest="show_help")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", metavar="<subcmd>")
    auth_sub.add_parser("login", add_help=False)
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
    cloud_sub.add_parser("off", add_help=False)
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
    }
    rc = _run_commands_py(root, "init", env)
    if rc != 0:
        return rc

    if args.storage == "cloud":
        _run_commands_py(root, "cloud_push", {})
        try:
            Path(".beacon/config.json").write_text('{"mode": "cloud"}\n')
            print("Cloud mode enabled.")
        except OSError as exc:
            _eprint(f"Warning: could not write .beacon/config.json: {exc}")
    return 0


def _print_init_help() -> None:
    print(
        "Usage: beacon init [--name NAME] [--objective TEXT] "
        "[--retro-day mon|tue|...|sun|monday|...] [--storage local|cloud]"
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

    # Mirror bash: require git + read HEAD info via subprocess.
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--is-inside-work-tree"],
            stderr=subprocess.STDOUT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("Error: Not a git repository")
        return 1

    custom_summary = args.summary or args.message or ""
    target_ref = args.explicit_hash or "HEAD"
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", target_ref], text=True
        ).strip()
        commit_msg = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s", target_ref], text=True
        ).strip()
        commit_date = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%ci", target_ref], text=True
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
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", target_ref],
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
        env = {
            "BEACON_DESCRIPTION": args.description or "",
            "BEACON_MS_ID": args.ms_id or "",
            "BEACON_TYPE": args.entry_type or "task",
            "BEACON_DATE": _today(),
            "BEACON_DETAIL": args.detail or "",
            "BEACON_REQUESTED_BY": args.requested_by or "",
            "BEACON_PRIORITY": args.priority or "",
            "BEACON_MOTIVATION": args.motivation or "",
            "BEACON_ACCEPTANCE_CRITERIA": args.acceptance_criteria or "",
        }
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
                "Usage: beacon doc add \"title\" [--scope core|spec|memo] "
                "[--ms ms-id] [--op op-id] [--id slug] [--content text] [--json]"
            )
            return 1
        env = {
            "BEACON_TITLE": args.title,
            "BEACON_DOC_ID": args.doc_id or "",
            "BEACON_CONTENT": args.content or "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
            "BEACON_JSON": "1" if args.json else "",
        }
        return _run_commands_py(root, "doc_add", env)

    if cmd in ("list", "ls"):
        env = {
            "BEACON_JSON": "1" if args.json else "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
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
                "[--scope ...] [--ms ms-id] [--op op-id] [--content text] [--json]"
            )
            return 1
        env = {
            "BEACON_DOC_ID": args.doc_id,
            "BEACON_TITLE": args.title or "",
            "BEACON_CONTENT": args.content or "",
            "BEACON_SCOPE": args.scope or "",
            "BEACON_MS": args.doc_ms or "",
            "BEACON_OP": args.doc_op or "",
            "BEACON_JSON": "1" if args.json else "",
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
            "Usage: beacon note \"<text>\" [--context \"<label>\"]\n"
            "       beacon note list [--json]\n"
            "       beacon note clear"
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
            "Usage: beacon note \"<text>\" [--context \"<label>\"]\n"
            "       beacon note list [--json]\n"
            "       beacon note clear"
        )
        return 1
    env = {
        "BEACON_NOTE_TEXT": sub,
        "BEACON_NOTE_CONTEXT": args.context or "",
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
    """Operation dispatch — only `purge` is implemented natively (e-893).

    Full operation management (open/close/list/show/run/incident) is still
    bash-only; we expose just the duplicate-ID recovery path here because
    that is what must work when the project won't load and bash is absent.
    """
    if args.show_help or args.op_cmd is None:
        print("Usage: beacon operation purge <op-id> --reason <text> [--index <n>]")
        print("  (Full operation management is available via the bash CLI.)")
        return 0 if args.show_help else 2
    if args.op_cmd == "purge":
        return _purge_dispatch(
            root, "operation_purge", id_value=args.op_id, id_env="BEACON_OP_ID",
            reason=args.reason, index=args.index, json_flag=args.json,
            usage="beacon operation purge <op-id> --reason <text> [--index <n>] [--json]",
        )
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
        # gh_args is a REMAINDER list — bash forwards via printf %q. We
        # join with shell-safe quoting; commands.py:cmd_pr_create reads
        # BEACON_GH_ARGS as a pre-quoted single string.
        import shlex
        gh_args = " ".join(shlex.quote(a) for a in (args.gh_args or []))
        return _run_commands_py(
            root, "pr_create",
            {
                "BEACON_MS_ID": args.ms_id or "",
                "BEACON_INTENT": args.intent or "",
                "BEACON_GH_ARGS": gh_args,
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
            "Usage: beacon cloud [list|status|open <id>|join <id>|off|push|pull]\n"
            "  list                 List cloud projects\n"
            "  status               Show current cloud mode + project_id\n"
            "  open <project-id>    Bind cwd to a cloud project + open Web UI\n"
            "  join <project-id>    Bind cwd to a cloud project (no UI launch)\n"
            "  off                  Switch back to local mode (writes config.json)\n"
            "  push [-f|--force]    Upload local project (local mode → cloud + auto switch)\n"
            "  pull                 Sync cloud state into the local read-only cache"
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
        # Local Python-side write (no commands.py handler exists for `off` —
        # bash does it inline). Mirror the bash behaviour byte-for-byte.
        config_path = Path(".beacon/config.json")
        if not config_path.exists():
            print("No .beacon/config.json found.")
            return 0
        try:
            config_path.write_text('{"mode": "local"}\n', encoding="utf-8")
            print("Switched to local mode.")
            return 0
        except OSError as exc:
            _eprint(f"Error writing config.json: {exc}")
            return 1
    if cmd == "open":
        if not args.project_id:
            print("Usage: beacon cloud open <project-id>")
            return 1
        return _do_cloud_open(root, args.project_id, args.no_browser)
    if cmd == "push":
        # cmd_cloud_push reads BEACON_FORCE from env. It already enforces the
        # ms-24 cloud-mode block (refuses without --force) and ms-36 auto-
        # switch (config.json mode -> cloud after initial migration), so the
        # dispatch handler is intentionally thin.
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

    # 3. Write the cloud / config / project skeleton.
    Path(".beacon").mkdir(exist_ok=True)
    cloud_path.write_text(
        '{\n  "project_id": "' + project_id + '",\n'
        '  "api_url": "https://beacon-ai.dev"\n}\n',
        encoding="utf-8",
    )
    Path(".beacon/config.json").write_text(
        '{"mode": "cloud"}\n', encoding="utf-8"
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
    web_url = f"https://beacon-ai.dev/?project={project_id}"
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
    return _run_commands_py(root, f"auth_{args.auth_cmd}", {})


def _handle_skill(root: Path, args: argparse.Namespace) -> int:
    """`beacon skill install [--force] [--settings-path PATH]`.

    Delegates to ``commands.py skill_install`` after surfacing optional
    flags as ``BEACON_*`` env vars. The bash path uses
    ``python3 commands.py skill_install`` with no env, so the Python
    path stays env-superset-compatible.
    """
    if args.show_help or args.skill_cmd is None:
        print("Usage: beacon skill install [--force] [--settings-path PATH]")
        return 0 if args.show_help else 2
    if args.skill_cmd != "install":
        print(f"Unknown skill subcommand: {args.skill_cmd}")
        return 2
    env: Dict[str, str] = {}
    if getattr(args, "force", False):
        env["BEACON_FORCE"] = "1"
    settings_path = getattr(args, "settings_path", "") or ""
    if settings_path:
        env["BEACON_SETTINGS_PATH"] = settings_path
    return _run_commands_py(root, "skill_install", env)


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
    "project": _handle_project,
    "doctor": _handle_doctor,
    "skill": _handle_skill,
    "auth": _handle_auth,
    "cloud": _handle_cloud,
    "pr": _handle_pr,
    "issue": _handle_issue,
    "member": _handle_member,
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
        "  beacon doc add \"title\" [--scope core|spec|memo] [--ms id]\n"
        "  beacon doc list [--scope S] [--ms id]\n"
        "  beacon doc show <doc-id>\n"
        "  beacon note \"<text>\" | note list | note clear\n"
        "  beacon search \"query\" [--ms id] [--scope S]\n"
        "  beacon trigger fire|check|clear [name]\n"
        "  beacon cycle status\n"
        "  beacon push record|list\n"
        "  beacon deploy record|list\n"
        "  beacon entry move <entry-id> [-t task-id | -m ms-id]\n"
        "  beacon project archive|unarchive\n"
        "  beacon doctor\n"
        "  beacon skill install [--force]\n"
        "\n"
        "Not yet available on bash-less systems (tracked under ms-44):\n"
        "  beacon setup, dashboard (tmux), beacon update, beacon pr review,\n"
        "  beacon cloud open/launch (tmux dashboard), beacon retro (interactive),\n"
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
