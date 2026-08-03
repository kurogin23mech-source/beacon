#!/usr/bin/env python3
"""Shared CLI helpers for the beacon command modules (ms-127 e-4316).

This module holds the leaf helpers that every ``cmd_*`` family module depends
on: project load/save, the session/commit-source resolvers, the changelog
appender, and the write-gate helpers. It was extracted verbatim from the head
of ``commands.py`` (the god-module split) so that the per-family ``cmd_<family>``
modules can ``from commands_shared import ...`` a single, stable dependency
without pulling in the whole 29K-line ``commands.py``.

Dependency direction (ms-127 SPEC 方針4 = 循環 import を構造で防ぐ):
  commands_shared  →  core / store / work_model   (downward, leaf domain modules)
  commands.py      →  commands_shared              (upward)
  cmd_<family>.py  →  commands_shared              (upward)
``commands_shared`` MUST NOT import ``commands`` or any ``cmd_<family>`` module,
so the dependency graph stays one-directional and no cycle can form.
"""

import os
import sys

from store import get_store
import core
import work_model  # ms-109 e-3559: 職種非依存の Target 正準ラベルアクセサ

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def _resolve_session_id() -> str:
    """Get the current beacon session_id, or "" if it can't be resolved.

    Returns "" on any failure so that commit/PR recording never fails just
    because session.py is misbehaving (corrupt session.json, missing .beacon/,
    test sandboxes that don't run the heartbeat). The empty string is
    the documented "no session" sentinel in core.log_commit / core.pr_add —
    those entries simply won't appear in session-log aggregation queries.

    ms-95 e-2419: bridge claim を優先する resolve_active_session_id を使う
    (= e-1331 / e-1460 で確立された "MCP bridge が listen 中の cwd では bridge
    の session_id を CLI sender にも採用する" 原則)。 これで beacon bus send で
    送った DM への reply (= recipient = parent sender) が bridge listen sid に
    一致し、 PE → LPS → PE の双方向 push が成立する。 旧実装は get_session_id を
    直叩きしていたため bridge claim を skip し、 CLI mint sid と bridge listen
    sid が乖離して reply silent failure を引き起こしていた。
    """
    try:
        import session as _session
        return _session.resolve_active_session_id()
    except Exception:
        return ""


def _resolve_commit_source() -> str:
    """Detect the source axis for a commit being recorded (ms-79 / e-1817).

    Returns one of:
      - ``"auto-op"`` when the commit is happening inside a Beacon
        Operation envelope auto-execute context (= ms-60). The detection
        keys off env vars set by the operation runner; specifically:
          * ``BEACON_OPERATION_ENVELOPE_ID``  (= active envelope token)
          * ``BEACON_OPERATION_AUTO_EXECUTE`` set to ``"1"``
      - ``""`` (empty) for the default human dialog case. The empty
        string is the documented "untagged = human" sentinel — older
        commits without the field stay as-is and continue to count as
        human in retro_query's source breakdown.

    Kept env-var-driven on purpose: the Operation runner can set the
    flag without log_commit needing to know about envelope internals,
    and tests can drive it deterministically by exporting one env var.
    """
    if os.environ.get("BEACON_OPERATION_AUTO_EXECUTE", "") == "1":
        return "auto-op"
    if os.environ.get("BEACON_OPERATION_ENVELOPE_ID", "").strip():
        return "auto-op"
    return ""


def _user_home():
    """Resolve the user home directory, honoring an explicit HOME override.

    os.path.expanduser('~') on Windows keys off USERPROFILE/HOMEDRIVE+HOMEPATH
    and ignores HOME, which breaks env-overridden contexts (tests, sandboxes)
    and any setup where HOME != USERPROFILE. Prefer HOME when it resolves to a
    real absolute directory (so a leftover msys-style '/c/...' value can't send
    writes to a bogus path); otherwise fall back to expanduser. (ms-44 e-844)
    """
    home = os.environ.get("HOME")
    if home and os.path.isabs(home) and os.path.isdir(home):
        return home
    return os.path.expanduser("~")


def load_project():
    store = get_store()
    data = store.load_project()
    core.validate_project(data)
    return data


def load_project_unsafe():
    """Load project data WITHOUT running validate_project.

    Reserved for recovery flows (`beacon doctor`, `beacon milestone purge`)
    that must keep working when the data already violates invariants —
    e.g. duplicate IDs from a hand-edit (Issue #14). All other code paths
    must continue to use load_project() so corruption is surfaced early.
    """
    store = get_store()
    return store.load_project()


def _local_date(iso_str: str) -> str:
    """Convert a UTC ISO8601 timestamp (e.g. '2026-05-24T23:30:00Z') to the
    operator's local YYYY-MM-DD. Empty/invalid input passes through (best-effort)."""
    import datetime as _dt
    if not iso_str:
        return ""
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str[:10]


def _append_changelog(op: dict) -> None:
    """Append an operation entry to .beacon/changelog.jsonl."""
    import json as _json
    import datetime as _dt
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    changelog_path = os.path.join(beacon_dir, "changelog.jsonl")
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **op,
    }
    try:
        with open(changelog_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # changelog is best-effort; never block operations


_ACKNOWLEDGED_REASON = "(acknowledged: no detailed reason given)"


def _require_reason_or_skip(verb: str) -> str:
    """Gate state-transition / destructive verbs on an unambiguous audit entry.

    ms-120 e-3906 (option B): the old design accepted ``--reason ""`` as a
    silent waiver, but an empty string is ambiguous at read time (deliberate
    waiver vs. an AI padding the flag to pass the gate) — and a warning never
    stops an AI that reads exit 0 as success. So the gate now admits exactly two
    unambiguous states, and rejects the ambiguous empty:

    - Non-empty ``BEACON_REASON`` (``--reason "..."``) → accept, return it.
    - ``BEACON_ACKNOWLEDGE=1`` (``--acknowledge``) → accept, return a sentinel
      recording a *deliberate* no-reason. The intent is explicit, not inferred
      from an empty string.
    - Neither, or an empty ``BEACON_REASON`` → refuse with exit 1 and a
      recoverable message (原則 3) naming both valid paths.

    Args:
        verb: Human-facing verb for the error message (e.g. ``"task done"``).

    Returns:
        The reason string, or the acknowledgment sentinel.
    """
    if os.environ.get("BEACON_ACKNOWLEDGE") == "1":
        return _ACKNOWLEDGED_REASON
    reason = os.environ.get("BEACON_REASON", "")
    if reason.strip():
        return reason
    print(
        f"Error: `{verb}` requires an audit entry. Pass --reason \"...\" to "
        f"record why, or --acknowledge to deliberately proceed without a "
        f"written reason. An empty --reason \"\" is no longer accepted "
        f"(it was ambiguous — use --acknowledge to waive on purpose).",
        file=sys.stderr,
    )
    sys.exit(1)


# ms-81 e-1916: forcing function — warn (don't block) when a write targets
# a milestone whose status doesn't authorise writes per the state-machine
# CORE doc DqIvAVzDprcq6hsq0AuF §1 + §6.
#
# Per the SPEC (warning-based, not block), this emits a stderr warning that
# names the offending status and the recommended remediation, and:
#   - on an interactive tty, prompts [y/N] to let the operator decide;
#   - on a non-interactive run (no tty — Skill / hook / dispatch path),
#     proceeds after logging the warning (= 努力義務, the operator can act
#     on the audit trail later);
#   - if BEACON_BYPASS_STATUS_GATE=1, skips the prompt entirely (= explicit
#     opt-out for bulk migrations / hook bypass scenarios).
_WRITE_AUTHORISED_STATUSES = {"in_progress", "active", "observing"}


def _check_ms_status_for_write(ms: dict, op_desc: str) -> bool:
    """Return True if the write may proceed (status authorised or operator
    consented to override); False only when an interactive operator declines.
    """
    status = ms.get("status", "todo")
    if status in _WRITE_AUTHORISED_STATUSES:
        return True

    if os.environ.get("BEACON_BYPASS_STATUS_GATE", "") == "1":
        return True

    title = work_model.target_label(ms)
    ms_id = ms.get("id", "")
    print(
        f"\n[ms-81 status gate] write to a {status} milestone\n"
        f"   target:      [{ms_id}] {title}\n"
        f"   status:      {status} (writes are discouraged — see CORE doc "
        f"DqIvAVzDprcq6hsq0AuF §1)\n"
        f"   operation:   {op_desc}\n"
        f"   suggestion:  transition to active via `beacon milestone start "
        f"{ms_id}` or to observing via `beacon milestone observe {ms_id} "
        f"--reason \"...\"` first.\n"
        f"   bypass:      set BEACON_BYPASS_STATUS_GATE=1 to silence this gate "
        f"(opt-out for hooks / bulk ops).",
        file=sys.stderr,
    )

    if not sys.stdin.isatty():
        # Non-interactive: proceed after logging the warning. The forcing
        # function is the visible warning + audit trail, not a block.
        print(
            f"   (non-interactive stdin — proceeding; the warning above is "
            f"the forcing function)",
            file=sys.stderr,
        )
        return True

    try:
        response = input("   Proceed anyway? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n   declined (no input)", file=sys.stderr)
        return False
    return response in ("y", "yes")


def save_project(data, op=None):
    core.validate_project(data)
    store = get_store()
    try:
        store.save_project(data)
    except RuntimeError as e:
        # Lost-update guard tripped (cloud mode only): the cloud changed since
        # we loaded it. Surface a clear message instead of a traceback so the
        # user knows to re-run rather than silently losing a concurrent edit.
        from store_api import ConflictError
        if isinstance(e, ConflictError):
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        raise
    if op:
        _append_changelog(op)


def save_project_unsafe(data, op=None):
    """Save project data WITHOUT validate_project.

    Reserved for the recovery flow where a single purge cannot make the
    project entirely clean (e.g. 3 duplicates of the same ms-id — the
    first purge leaves 2 dups, still invalid). Callers MUST run
    `beacon doctor` afterwards to confirm cleanup.
    """
    store = get_store()
    store.save_project(data)
    if op:
        _append_changelog(op)
