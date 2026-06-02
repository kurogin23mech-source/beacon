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
    p_task_done.add_argument("-r", "--reason", default="")

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

    p_ms_done = ms_sub.add_parser("done", aliases=["close"], add_help=False)
    p_ms_done.add_argument("ms_id", nargs="?", default="")
    p_ms_done.add_argument("-r", "--reason", default="")

    p_ms_observe = ms_sub.add_parser("observe", add_help=False)
    p_ms_observe.add_argument("ms_id", nargs="?", default="")
    p_ms_observe.add_argument("-r", "--reason", default="")

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
            print("Usage: beacon task done <entry-id> [-p <progress>] [--reason <text>]")
            return 1
        env = {
            "BEACON_ENTRY_ID": args.entry_id,
            "BEACON_PROGRESS": args.progress or "",
            "BEACON_REASON": args.reason or "",
        }
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
            "[add|list|start|done|close|observe|show|update] [options]"
        )
        return 0 if args.show_help else 2
    if (rc := _ensure_project()) is not None:
        return rc

    cmd = args.ms_cmd
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
            print("Usage: beacon milestone start <ms-id>")
            return 1
        return _run_commands_py(root, "milestone_start", {"BEACON_MS_ID": args.ms_id})

    if cmd in ("done", "close"):
        if not args.ms_id:
            print("Usage: beacon milestone done <ms-id> [--reason <text>]")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_REASON": args.reason or "",
        }
        return _run_commands_py(root, "milestone_done", env)

    if cmd == "observe":
        if not args.ms_id:
            print("Usage: beacon milestone observe <ms-id> [--reason <text>]")
            return 1
        env = {
            "BEACON_MS_ID": args.ms_id,
            "BEACON_STATUS": "observing",
            "BEACON_REASON": args.reason or "",
        }
        return _run_commands_py(root, "milestone_update", env)

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
        return 0 if args.show_help else 2
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
    "project": _handle_project,
    "doctor": _handle_doctor,
    "skill": _handle_skill,
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
