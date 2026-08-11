#!/usr/bin/env python3
"""Shared CLI helpers for the beacon command modules (ms-127 e-4316).

This module holds the leaf helpers that every ``cmd_*`` family module depends
on. It grows as the god-module split proceeds; each promoted group is delimited
by a ``# --- ... ---`` / ``# ms-127 ...`` section comment. Currently:
  - project load/save, session/commit-source resolvers, changelog appender,
    write-gate helpers (e-4316);
  - cloud mode / config / API client / token / bus project-id resolution /
    persistence-poisoning (bus-origin) write defense / session-notes path
    (e-4317-foundation);
  - cross-family identity / cloud-project helpers — ``_project_id_for_ops`` /
    ``_read_credentials_for_identity`` / ``_resolve_creator_identity`` /
    ``_rename_local_project_json_for_cloud_cutover`` (e-4318-foundation).
Helpers were extracted verbatim from ``commands.py`` so the per-family
``cmd_<family>`` modules can ``from commands_shared import ...`` a single, stable
dependency without pulling in the whole ``commands.py``.

Dependency direction (ms-127 SPEC 方針4 = 循環 import を構造で防ぐ):
  commands_shared  →  core / store / work_model   (downward, leaf domain modules)
  commands.py      →  commands_shared              (upward)
  cmd_<family>.py  →  commands_shared              (upward)
``commands_shared`` MUST NOT import ``commands`` or any ``cmd_<family>`` module,
so the dependency graph stays one-directional and no cycle can form.
"""

import hashlib
import json
import os
import re
import sys
import time
from typing import Callable, Optional, Tuple

from store import get_store
import core
import work_model  # ms-109 e-3559: 職種非依存の Target 正準ラベルアクセサ
import occupation  # ms-108 e-3269: 職種 ⊃ target-class 包含ゲート (_gate_target_class)
import transition_approval as _ta  # ms-127 e-4849 (milestone split)

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


# ---------------------------------------------------------------------------

# ms-127 e-4317-foundation: cross-family shared helpers promoted from commands.py

#

# These are the leaf helpers every cmd_<family> module needs (cloud config /

# API client / token / cloud-mode detection / bus project-id resolution /

# persistence-poisoning defense / session-notes path).

# Moved verbatim so family modules can `from commands_shared import ...`

# instead of importing commands.py (which would form a cycle — SPEC 方針4).

# ---------------------------------------------------------------------------

# --- cloud mode / config / api url / token / api client ---

def _is_cloud_mode():
    """Check if we're in cloud mode (single source of truth: cloud.json existence).

    e-1861 (ms-61): The legacy ``config.json["mode"] == "cloud"`` dual-source
    check was removed because it created a silent drift window: a sub-agent
    could overwrite ``.beacon/config.json`` to ``{"mode": "local"}`` and the
    CLI would suddenly read the stale local ``project.json`` instead of cloud,
    causing apparent user data loss (2026-06-15 incident).

    Beacon is always invoked from Claude Code, which requires internet, so
    "local mode" has no production use case. ``.beacon/cloud.json`` existence
    is now the single, structurally protected source of truth. ``BEACON_CLOUD=1``
    still forces cloud for test harnesses that mock ``cloud.json`` indirectly.

    Any ``mode`` field still sitting in legacy ``config.json`` is ignored
    (graceful — we never error, we just stop reading it). ``beacon doctor``
    surfaces the legacy field as a non-fatal migration warning.
    """
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    return os.path.exists(cloud_path)

def _resolve_active_api_url() -> str:
    """Return the api_url of the active profile (ms-64 / e-1458).

    Replaces the previous ``os.environ.get("BEACON_API_URL", DEFAULT_API_URL)``
    + ``config.get("api_url", DEFAULT_API_URL)`` chain that was duplicated at
    11+ sites in this module. The profile resolver already implements the full
    precedence chain (env > cwd cloud.json > profile.json > default), so all
    sites now go through it and a single point of truth handles the precedence
    rules.

    The active profile is determined by (in order): ``--profile`` CLI arg
    (exported as ``BEACON_PROFILE`` by ``bin/beacon`` top-level), then
    ``BEACON_PROFILE`` env, then cwd ``.beacon/cloud.json`` ``profile`` field,
    then the ``default`` profile.
    """
    try:
        import profile as _profile  # type: ignore[import-not-found]
        return _profile.resolve_active_profile().api_url
    except Exception:
        # Best-effort fallback: legacy chain. Keeps CLI usable if profile.py
        # itself is unimportable for any reason (e.g. partial install).
        api_url = _resolve_active_api_url()
        config_path = _get_cloud_config_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    api_url = json.load(f).get("api_url", api_url)
            except Exception:
                pass
        return api_url

def _get_cloud_config_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "cloud.json")

def _extract_token(creds) -> str:
    """Extract bearer token from credentials (handles both object and dict forms)."""
    if isinstance(creds, dict):
        return creds.get("token", "") or creds.get("id_token", "")
    return (creds.id_token or creds.token) if creds else ""

def _get_api_client():
    """Create an ApiClient from cloud.json config and auth credentials.

    The ApiClient receives a TokenProvider callable instead of a static token,
    so tokens are refreshed on every API call. This prevents long-lived CLI
    sessions from failing after the initial token expires.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("No cloud.json found. Run 'beacon cloud upload-initial' first.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_url = _resolve_active_api_url()

    # Use a TokenProvider callable so each request picks up a fresh token.
    # load_credentials() refreshes OAuth tokens automatically; web_auth tokens
    # will be refreshed once _refresh_web_auth_token() support is wired in.
    def _token_provider() -> str:
        from auth import load_credentials as _load
        _creds = _load()
        return _extract_token(_creds) if _creds else ""

    from api_client import ApiClient
    return ApiClient(api_url, _token_provider), config

# --- bus project-id resolution ---

def _resolve_bus_project_id(config: dict) -> str:
    """Return the project_id the bus call should target.

    Resolution order (ms-54 e-1151):
      1. ``BEACON_BUS_PROJECT_ID`` env var — set by the dispatcher when
         the user passed ``--project <id>``. This lets a Beacon session
         post into / read from another project's bus (e.g. Mac Beacon
         session DMs a TrailNode session) without flipping cwd.
      2. ``config["project_id"]`` — the default, derived from
         ``.beacon/cloud.json`` of the current project.

    ms-97 / e-2694 dogfood fix: when an override is supplied via
    ``--project`` (= env var), pass it through
    :func:`lib.project_ref.resolve_project_ref` against the cloud
    project list so short names (= ``life-plan-simulator``) expand to
    full suffix'd ids (= ``life-plan-simulator-68c5df``). The session
    registry, DM fanout, and recipient lookups all key off the full
    id — short-name input silently misses every record. Best-effort:
    if the api client / list call is unavailable (= local-mode tests,
    offline), passthrough preserves the legacy behaviour.
    """
    override = os.environ.get("BEACON_BUS_PROJECT_ID", "").strip()
    if override:
        return _canonicalize_project_ref(override) or override
    return config.get("project_id", "")


def _canonicalize_project_ref(ref: str) -> str:
    """Best-effort short-name → full project_id expansion.

    Wraps :func:`lib.project_ref.resolve_project_ref` with a cloud-only
    lister (= ``api_client.list_projects``) and swallows transport
    errors so a network blip in the resolver never blocks the underlying
    send / fanout. Returns the resolved id, or the input unchanged when
    resolution can't run.
    """
    if not ref:
        return ref
    try:
        from project_ref import resolve_project_ref as _resolve
    except ImportError:
        from lib.project_ref import resolve_project_ref as _resolve
    lister = None
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()

            def _list() -> list:
                try:
                    return client.list_projects() or []
                except Exception:  # noqa: BLE001 — degrade to passthrough
                    return []

            lister = _list
        except Exception:  # noqa: BLE001
            lister = None
    try:
        return _resolve(ref, db_or_lister=lister)
    except ValueError:
        # Ambiguous — surface the input unchanged. The downstream call
        # (= /bus send, /trek scope add) will hit a clearer error path
        # (= "project not found" or rejection at the server) instead of
        # the resolver hijacking error reporting.
        return ref

# --- persistence-poisoning (bus-origin) write defense (ms-54 e-1293) ---

PERSISTENCE_POISONING_AUDIT_FILE = "persistence_poisoning_audit.jsonl"

_BUS_ORIGIN_REFUSAL_MESSAGE = (
    "Error: writes from bus-origin payloads are not allowed "
    "(persistence poisoning defense)"
)


def _is_bus_origin_input() -> bool:
    """Return True iff the current invocation is marked as bus-derived.

    Read from ``BEACON_BUS_ORIGIN``. Truthy values are ``"1"`` and ``"true"``
    (case-insensitive). Everything else is treated as not-set so a
    misconfigured caller fails closed (i.e. allows the write) only when the
    flag is absent — never on a typo'd truthy value.
    """
    raw = os.environ.get("BEACON_BUS_ORIGIN", "").strip().lower()
    return raw in ("1", "true", "yes")


def _persistence_poisoning_audit_path() -> str:
    """Local jsonl path for refused bus-origin persistence attempts."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, PERSISTENCE_POISONING_AUDIT_FILE)


def _record_persistence_poisoning_refusal(handler: str, details: dict) -> None:
    """Append a refusal audit record to the local jsonl audit log.

    Best-effort: any IO error is swallowed (we still refuse the write — the
    audit log is forensic context, not the gate). The record schema is
    intentionally small and human-readable so an operator can grep for
    ``handler=note_add`` to see the trail.
    """
    import datetime
    try:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "handler": handler,
            "verdict": "refused",
            "reason": "bus_origin_persistence_blocked",
            "session_id": _resolve_session_id(),
            "details": details,
        }
        path = _persistence_poisoning_audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Audit logging is best-effort; never let it mask the refusal itself.
        pass


def _refuse_if_bus_origin(handler: str, details: dict) -> bool:
    """Refuse the current persistence call if marked bus-origin.

    Returns True iff the call was refused (caller should ``sys.exit(1)``
    immediately). When True, an audit record is written and the refusal
    message is printed to stderr.

    Handlers (``cmd_note_add``, ``cmd_doc_add``, ``cmd_doc_update``,
    ``cmd_session_end``) MUST call this BEFORE any persistence side effect.
    """
    if not _is_bus_origin_input():
        return False
    _record_persistence_poisoning_refusal(handler, details)
    print(_BUS_ORIGIN_REFUSAL_MESSAGE, file=sys.stderr)
    return True

# --- session-notes path (shared by note family + session rescue) ---

def _get_notes_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "session_notes.jsonl")


# ---------------------------------------------------------------------------

# ms-127 e-4318-foundation: cross-family identity / cloud-project helpers

# promoted from commands.py so cmd_org (org+member family) and other callers

# (deploy / cloud / trek / bus) depend on commands_shared, not commands.py.

# ---------------------------------------------------------------------------

def _project_id_for_ops() -> str:
    """Return the project_id used by apply_operation for this CLI invocation.

    Local mode: project_id is irrelevant beyond changelog labeling, so we
    use the project's `name` field. Cloud mode: requires the cloud.json
    project_id which the API layer normally supplies — here we read
    .beacon/cloud.json if present, else fall back to project name.
    """
    try:
        data = load_project()
    except Exception:
        return ""
    project_file = get_project_file()
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    cloud_json = os.path.join(beacon_dir, "cloud.json")
    if os.path.exists(cloud_json):
        try:
            with open(cloud_json, "r", encoding="utf-8") as f:
                return json.load(f).get("project_id", "") or data.get("name", "")
        except (OSError, json.JSONDecodeError):
            pass
    return data.get("name", "")


def _read_credentials_for_identity() -> tuple[str, str]:
    """Read (user_id, email) from the active-profile credentials.json (ms-61 / e-2132).

    Returns ``("", "")`` if no credentials file exists, the file is malformed,
    or no usable fields are present. Never raises — this is a best-effort
    fallback for ``_resolve_creator_identity``; the caller still treats
    missing email as a hard error if env is also empty.

    Implementation:
      * Resolves the credentials path via ``profile.resolve_active_profile()``
        so cwd ``cloud.json.profile`` and ``BEACON_PROFILE`` are honored.
      * Falls back to legacy ``~/.beacon/credentials.json`` if the per-profile
        path doesn't exist yet (= pre-migration installs).
      * ``user_id`` is decoded from the JWT ``sub`` claim (= Google sub, e.g.
        Cognito/Cloud Identity uid). The token is split as ``bcli.<payload>.<sig>``
        or stdlib JWT ``<header>.<payload>.<sig>``; we read the middle segment.
      * ``email`` is read from the top-level ``email`` field directly.

    Why this exists:
      ms-84 dogfood (2026-06-19) で観測された病理: fork session で
      ``BEACON_USER_EMAIL`` 設定漏れ → ``beacon trek create / join / dm send``
      が hard error。 credentials.json には email がある (= login 済) のに
      env を要求するため、 fork 経路で identity 漏れる導線が温存されていた。
      本 helper で auto-read 経路を足し、 env override は最優先のまま
      (= test / CI / multi-account 用)。
    """
    import base64
    import json as _json
    try:
        import profile as _profile
        cred_path = _profile.resolve_active_profile().credentials_path
    except Exception:
        cred_path = None

    candidates = []
    if cred_path is not None:
        candidates.append(cred_path)
    # Legacy singleton location (= pre-profile installs). Honor BEACON_HOME
    # so tests / multi-account flows can isolate it the same way the profile
    # module does (= profile._beacon_home contract).
    beacon_home = os.environ.get("BEACON_HOME") or os.path.expanduser("~/.beacon")
    candidates.append(os.path.join(beacon_home, "credentials.json"))

    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue

        email = (data.get("email") or "").strip()
        token = (data.get("token") or "").strip()

        # Decode token payload to recover sub (= user_id). Two formats
        # supported:
        #   * ``bcli.<payload_b64>.<sig_hex>`` — Beacon CLI long-lived token
        #     (= 2 segments after the "bcli." prefix, payload is segment[0])
        #   * ``<header>.<payload>.<sig>`` — standard 3-segment JWT (= Cognito
        #     id_token, Google id_token; payload is segment[1])
        user_id = ""
        if token:
            tok = token
            is_bcli = False
            if tok.startswith("bcli."):
                tok = tok[5:]
                is_bcli = True
            segments = tok.split(".")
            payload_b64 = ""
            if is_bcli and len(segments) >= 1:
                payload_b64 = segments[0]
            elif len(segments) >= 2:
                payload_b64 = segments[1]
            if payload_b64:
                # Re-pad base64 (JWT strips '='); urlsafe alphabet.
                padding = "=" * (-len(payload_b64) % 4)
                try:
                    payload_bytes = base64.urlsafe_b64decode(
                        payload_b64 + padding
                    )
                    payload = _json.loads(payload_bytes.decode("utf-8"))
                    user_id = str(payload.get("sub") or "").strip()
                except Exception:
                    user_id = ""

        if email or user_id:
            return user_id, email
    return "", ""


def _resolve_creator_identity() -> tuple[str, str, str]:
    """Return (user_id, email, session_id) for the calling user.

    ms-127 e-4318: promoted to commands_shared because this is a cross-family
    identity resolver — the caller may be an org command (cmd_org_create /
    cmd_org_list), a trek command, or a bus command. (The name predates the
    promotion; it resolves the acting user's identity, not a trek-specific one.)

    Resolution order (ms-61 / e-2132):
      1. Env vars (``BEACON_USER_ID`` / ``BEACON_USER_EMAIL`` / ``BEACON_SESSION_ID``)
         — highest precedence so tests, CI, multi-account flows override freely.
      2. ``credentials.json`` of the active profile (= login 済セッションは
         自動継承)。 email と user_id のうち env で埋まらなかったものだけ
         credentials から補う。 env と credentials の値が共存している場合は
         **env が勝つ**。
      3. ``whoami`` for ``user_id`` only — final fallback so dev-mode runs
         without login still produce a non-empty user_id (= just the OS user).

    Email と session_id は credentials 経路でも埋まらなければ呼び出し側で
    hard error にする (= ``BEACON_USER_EMAIL`` 要求 など)。 fabricating
    silently は member/leader 記録を破壊するので構造的に避ける。
    """
    user_id = os.environ.get("BEACON_USER_ID", "").strip()
    email = os.environ.get("BEACON_USER_EMAIL", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()

    # ms-61 / e-2132 — credentials.json fallback for env-missing case.
    if not email or not user_id:
        cred_user_id, cred_email = _read_credentials_for_identity()
        if not email:
            email = cred_email
        if not user_id:
            user_id = cred_user_id

    if not user_id:
        try:
            import getpass
            user_id = getpass.getuser()
        except Exception:
            user_id = ""
    return user_id, email, session_id


def _rename_local_project_json_for_cloud_cutover(project_file: str) -> Optional[str]:
    """Rename ``.beacon/project.json`` to ``.before-cloud-YYYYMMDD`` (ms-84 Phase 3).

    Idempotent insurance for the local→cloud migration: after upload-initial
    succeeds the local file becomes the silent-drift source from the moment
    of cloud cut-over (any later cloud write will not propagate to disk).
    Renaming it to a dated suffix:

      - hides it from ``beacon-find-root`` style markers (cloud.json now
        carries that role; see ``bin/beacon-find-root``);
      - keeps a one-shot recovery copy on disk (= we never ``rm``);
      - encodes the cut-over date so multiple runs (re-uploads, sandbox
        tests) do not collide.

    No-op when the source file is missing (= already cut over or never
    existed for a from-scratch cloud project). Returns the destination
    path on rename, ``None`` otherwise. Failures are logged to stderr
    but do not raise — the cut-over should not be blocked by a stat /
    rename hiccup, the user can rename manually post-hoc.
    """
    if not os.path.exists(project_file):
        return None
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    base_dir = os.path.dirname(project_file) or ".beacon"
    base_name = os.path.basename(project_file)
    dest = os.path.join(base_dir, f"{base_name}.before-cloud-{stamp}")
    # If the destination already exists (= re-run on the same day), append
    # a short suffix to keep the rename idempotent without overwriting the
    # earlier recovery copy. Never delete an existing backup.
    if os.path.exists(dest):
        suffix = 1
        while os.path.exists(f"{dest}.{suffix}"):
            suffix += 1
        dest = f"{dest}.{suffix}"
    try:
        os.rename(project_file, dest)
    except OSError as exc:
        print(
            f"Warning: failed to rename {project_file} → {dest}: {exc}\n"
            f"  The cloud cut-over succeeded but the local cache is still on disk.\n"
            f"  Rename it manually to prevent drift confusion.",
            file=sys.stderr,
        )
        return None
    _append_changelog({
        "op": "cloud_cutover_rename",
        "from": project_file,
        "to": dest,
    })
    return dest


# ---------------------------------------------------------------------------
# ms-127 e-4319-foundation: cross-family untriaged-gate / dup-report / author
# helpers promoted from commands.py (used by task+entry family AND milestone /
# operation / sales). cmd_task depends on commands_shared, not commands.py.
# ---------------------------------------------------------------------------


_HUMAN_UNTRIAGED_REFUSED_MSG = (
    "Priority is required. '--untriaged' is a machine-only sentinel (issue "
    "import / review-derived / roadmap / dispatch); a human session cannot use "
    "it to defer the judgement. Choose one of: highest, high, medium, low, "
    "lowest (highest = 大目的への寄与が最大 / lowest = 最小). "
    "(If this IS an automated / AI caller running at a terminal, declare it "
    "with BEACON_SESSION_KIND=ai, or run non-interactively.)"
)


def _caller_is_human_for_untriaged() -> bool:
    """Is the --untriaged caller a human (who must be refused the bypass)?

    ms-126 philosophy fix (2026-07-29, independent review): the earlier gate
    refused the bypass only when a session opted into BEACON_SESSION_KIND=human.
    But nothing sets that flag by default, so an undeclared human at an
    interactive terminal — the exact actor the mandatory-priority forcing
    function targets — was classified as a machine and slipped straight through
    (default-OPEN hole). The --untriaged bypass is a *machine* capability (a
    machine legitimately can't judge priority); a human must pick a severity.

    So refuse the bypass by default for anything interactive, and require a
    machine to be *identifiable*:
      * explicit BEACON_SESSION_KIND=human        → human (refuse).
      * explicit non-human kind (ai / machine / …) → machine (allow; covers an
        AI that happens to run inside a PTY).
      * unset → infer from the terminal: an interactive stdin (a person typing)
        is a human (refuse); a non-interactive stdin (AI tool / pipe / CI) is a
        machine (allow).

    The polarity is deliberately opposite the ms-119 merge / approve bans (there
    an undeclared session is treated as AI to deny a privileged *human* action);
    here an undeclared *interactive* session is treated as human to deny a
    privileged *machine* bypass. That is why this does NOT reuse
    ``_session_kind_is_human`` (whose "unset = AI" default is correct for those
    bans but would re-open this hole).
    """
    kind = (os.environ.get("BEACON_SESSION_KIND", "") or "").strip().lower()
    if kind == "human":
        return True
    if kind:
        return False
    # Unset: a person typing at an interactive terminal has a tty on stdin;
    # an AI tool / pipe / CI does not. No tty (or an unreadable stdin) = not a
    # person typing = machine, so the bypass is allowed.
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _human_untriaged_bypass_refused() -> bool:
    """True when a human is trying to use the untriaged escape hatch.

    Returns True iff BOTH: the process asked for untriaged
    (BEACON_ALLOW_UNTRIAGED=1, set by the `--untriaged` flag or a manual export)
    AND the caller resolves to a human (see ``_caller_is_human_for_untriaged``:
    explicit BEACON_SESSION_KIND=human, or an undeclared *interactive* terminal).
    In that case the caller MUST be forced back onto the priority forcing
    function. Machine / AI sessions (non-interactive, or explicitly declared
    non-human) are unaffected.
    """
    allow_untriaged = os.environ.get("BEACON_ALLOW_UNTRIAGED", "") == "1"
    return allow_untriaged and _caller_is_human_for_untriaged()


def _print_residual_dups(dup_report: dict) -> None:
    """Shared tail for *_purge commands: report duplicates still present."""
    remaining: list[str] = []
    for category, dupes in dup_report.items():
        for did, n in dupes.items():
            remaining.append(f"{category[:-1]} '{did}' x{n}")
    print("  Note: residual duplicates remain — " + ", ".join(remaining))
    print("  Run `beacon doctor` to inspect and purge the next one.")


def _resolve_current_author(data: Optional[dict] = None) -> dict:
    """Return ``{"user_id", "email", "display_name"}`` for the current operator.

    ms-43 / e-2281 — mirror of the server-side ``_resolve_author`` contract
    (server/app.py L538) on the CLI side so MS / task / Operation creates
    initiated from the local CLI also stamp ``meta.author`` with the
    *human* identity rather than leaving the field absent and falling
    back to the ``"claude"`` literal in ``created_by``.

    Resolution order (= env > credentials.json > project members[]):

      1. ``BEACON_USER_ID`` / ``BEACON_USER_EMAIL`` / ``BEACON_DISPLAY_NAME``
         env vars take precedence — tests / CI / multi-account flows
         override freely without touching credentials.json.
      2. ``credentials.json`` of the active profile (= login 済セッションは
         自動継承) supplies user_id (= JWT sub claim) and email when env
         is empty. Reuses ``_read_credentials_for_identity`` so the JWT
         parsing path stays in one place.
      3. ``display_name`` is not in credentials.json. As a best-effort
         fallback, scan ``data["members"][]`` for a member whose id /
         user_id / email matches the caller and lift the member's
         ``display_name`` (or ``name``) field. Empty when no match.

    Empty fields are dropped via ``core._clean_author`` so the on-disk
    shape stays exactly ``{user_id, email, display_name}`` minus the
    empties. Unauthenticated / no-credentials local mode returns ``{}``
    — the caller passes ``author=None`` (or this empty dict, same effect)
    and the create proceeds without a ``meta.author`` field, which the
    Web UI then renders via the legacy ``created_by`` fallback.

    Best-effort: any exception in member lookup is swallowed so the
    create path never fails due to display_name resolution.
    """
    user_id = (os.environ.get("BEACON_USER_ID") or "").strip()
    email = (os.environ.get("BEACON_USER_EMAIL") or "").strip()
    display_name = (os.environ.get("BEACON_DISPLAY_NAME") or "").strip()

    # credentials.json fallback for env-missing case (ms-61 / e-2132 parity).
    if not email or not user_id:
        try:
            cred_user_id, cred_email = _read_credentials_for_identity()
            if not email:
                email = cred_email
            if not user_id:
                user_id = cred_user_id
        except Exception:
            pass

    # display_name fallback via project members[] (ms-78 e-1807 shape).
    if not display_name and isinstance(data, dict):
        try:
            members = data.get("members")
            if isinstance(members, list):
                for m in members:
                    if not isinstance(m, dict):
                        continue
                    m_uid = (m.get("user_id") or m.get("id") or "").strip()
                    m_email = (m.get("email") or "").strip()
                    if user_id and m_uid == user_id:
                        display_name = (m.get("display_name") or m.get("name")
                                        or "").strip()
                        break
                    if email and m_email and m_email == email:
                        display_name = (m.get("display_name") or m.get("name")
                                        or "").strip()
                        break
        except Exception:
            pass

    return core._clean_author({
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
    })


# ---------------------------------------------------------------------------
# ms-127 e-4798: review / spec / gate leaf helpers promoted from commands.py.
# Shared foundation (triggers dir, review-due nudge firing, target-class gate,
# session-kind, SPEC lookup) used by many command families. The operation
# family (cmd_operation.py) is the immediate new consumer; milestone / target
# handlers in commands.py also call them (via re-export). review_spine /
# datetime are imported locally inside the functions, matching this file's
# leaf-only module-import discipline.
# ---------------------------------------------------------------------------

def _get_triggers_dir():
    project_dir = os.path.dirname(get_project_file())
    return os.path.join(project_dir, "triggers")


def _spec_doc_for_target(target_id: str, kind: str) -> Optional[dict]:
    """Return the first spec-scoped document attached to a target, or None.

    Single source of truth for the "spec doc attached to a target" scan (ms-119 —
    maintainability finding §2): both ``_spec_exists_for_ms`` / ``_spec_exists_for_op``
    delegate here so the scan rule (scope=="spec" + milestone/operation field
    match, transport failure swallowed) lives in exactly one place.

    Milestones carry a ``milestone`` field on the doc; operations carry
    ``operation``. Best-effort: transport failure → None (StoreApi rounds cloud
    transport failure to []).
    """
    if not target_id:
        return None
    field = "milestone" if kind == "milestone" else "operation"
    try:
        docs = get_store().list_documents()
    except Exception:
        return None
    for doc in docs:
        if doc.get("scope") == "spec" and doc.get(field) == target_id:
            return doc
    return None


def _spec_exists_for_op(op_id: str) -> bool:
    """True if any spec-scoped document is attached to op_id (delegates to the
    single-source scan _spec_doc_for_target)."""
    return _spec_doc_for_target(op_id, "operation") is not None


def _fire_review_due_trigger(target_id: str, target_kind: str, old_state: str,
                             new_state: str, *, target_title: str = "",
                             has_spec: bool = False, gated: bool = False,
                             is_completion: "Optional[bool]" = None) -> None:
    """Fire a 'review-due' trigger for a target lifecycle transition
    (ms-119 / e-3911 — the review firing spine).

    Beacon owns the target lifecycle, so a phase transition / close is a
    trigger GitHub cannot emit. On a completion-claim transition this surfaces
    the bound review(s) (see review_spine.review_bindings_for_transition):
    the 目的達成 nudge (only when the transition bypassed the approval gate) and
    the 思想 advisory (only when the target has a SPEC 原典). Empty bindings =
    no file written (routine / reversible transitions fire nothing).

    ``is_completion`` (ms-119 / e-4087):
      * ``None`` (default) — a BUILT-IN milestone / operation transition: whether
        it is a completion claim is decided by the transition_approval truth
        table (review_bindings_for_transition).
      * ``True`` — a data-defined (descriptor) target reaching done / a terminal
        phase. Its KIND is a descriptor class name the built-in truth table does
        not know, but the 節目 is the same completion claim, so bind the reviews
        directly (review_bindings_for_completion).
      * ``False`` — a descriptor transition that is NOT a completion (early phase
        advance): fire nothing.

    Advisory only — never blocks the transition. The blocking mechanism for
    目的達成 is the approval entry from e-3912, not this trigger.
    """
    import review_spine
    if is_completion is None:
        bindings = review_spine.review_bindings_for_transition(
            target_kind, old_state, new_state, has_spec=has_spec, gated=gated)
    elif is_completion:
        bindings = review_spine.review_bindings_for_completion(
            has_spec=has_spec, gated=gated)
    else:
        bindings = []
    if not bindings:
        return
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    parts = []
    for b in bindings:
        if b["review"] == review_spine.REVIEW_ATTAINMENT:
            # ms-119 e-4005: the 目的達成 review auto-fires at the close 節目 and
            # points at the INDEPENDENT evidence generation (a context-zero judge
            # verifies the SPEC against real code); the human owns the verdict.
            if b.get("gated"):
                parts.append(
                    f"目的達成レビュー (target が目的を果たしたか、証拠は独立 judge・"
                    f"verdict は人間): {target_id} の完了は承認待ちです。"
                    f"`/beacon-review-run --type attainment --target {target_id}` で"
                    f"文脈ゼロの独立 judge に SPEC × 実コードを検証させ証拠を作り、"
                    f"人間が `beacon target approve` で確定してください。")
            else:
                parts.append(
                    f"目的達成レビュー (target が目的を果たしたか、証拠は独立 judge・"
                    f"verdict は人間): 完了主張がゲートを経ずに適用されました。"
                    f"`/beacon-review-run --type attainment --target {target_id}` で"
                    f"独立 judge に振り返り証拠を作らせ、次からは `beacon milestone done "
                    f"{target_id} --review` でゲート経由に。")
        elif b["review"] == review_spine.REVIEW_PHILOSOPHY:
            parts.append(
                f"思想レビュー (実装が原典 = SPEC / vision の精神通りか、助言・非 "
                f"blocking): `/beacon-review-run --type philosophy --origin-doc "
                f"<spec-doc-id>` で文脈ゼロの独立 judge に SPEC を渡し {target_id} の"
                f"実装 drift を確認してください。")
    import datetime
    trigger_data = {
        "name": f"review-due-{target_id}",
        "kind": "review-due",
        "target_id": target_id,
        "target_kind": target_kind,
        "old_state": old_state,
        "new_state": new_state,
        "bindings": [b["review"] for b in bindings],
        "gated": gated,
        "message": f"{target_id} \"{target_title}\" が {old_state} -> {new_state} "
                   f"(完了主張) に遷移しました。節目のレビュー: " + " / ".join(parts),
        "created_at": datetime.datetime.now().isoformat(),
    }
    trigger_path = os.path.join(triggers_dir, f"review-due-{target_id}.json")
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _session_kind_is_human() -> bool:
    """True when the calling session declares itself human-driven.

    Default (unset ``BEACON_SESSION_KIND``) is treated as AI for safety, the
    same convention as the PR merge ban (see ``_ai_session_merge_ban_active``).

    ⚠ Divergent twin: ``_caller_is_human_for_untriaged`` (near the top of this
    module) answers the same "is the caller human?" question with the OPPOSITE
    default for an undeclared session — there an undeclared *interactive*
    terminal is treated as human (to deny a privileged *machine* bypass), the
    reverse of this "unset = AI" default (which denies a privileged *human*
    action). Do NOT reuse this helper for the untriaged forcing-function gate,
    and if you change this default, re-check that twin — the two polarities are
    intentional and live far apart (ms-126 philosophy fix).
    """
    return (os.environ.get("BEACON_SESSION_KIND", "") or "").strip().lower() == "human"


def _ai_session_direct_completion_ban_active() -> bool:
    """ms-119 / e-4008 — refuse an AI session's gate-bypassing direct completion.

    The 目的達成 approval gate (e-3912) was *opt-in*: `beacon milestone done`
    (and `beacon operation close`) applied the completion immediately and only
    left an advisory nudge, so the blocking review was skippable by just not
    passing ``--review``. The independent attainment review flagged this
    (AC2 gap(a)): "構造発火・非迂回" cannot hold while the default completion
    path bypasses the gate.

    This makes the gate non-bypassable *for AI sessions*: a direct completion
    (no ``--review``) is refused unless an explicit human signal is present.
    Humans still own the straight-line path (they own the verdict); the AI must
    route through the gate (``--review`` → human ``beacon target approve``).

      * ``BEACON_TARGET_COMPLETE_USER_OVERRIDE=1`` — user explicit opt-in for a
        one-off straight completion.
      * ``BEACON_SESSION_KIND=human`` — non-AI session (straight terminal use).

    Returns True if the ban fires. The ``--review`` gated path never reaches
    this check (it is the sanctioned route), so it is unaffected.
    """
    if os.environ.get("BEACON_TARGET_COMPLETE_USER_OVERRIDE", "") == "1":
        return False
    return not _session_kind_is_human()


def _self_close_ban_refuse(target_id: str, action: str, retry_cmd: str) -> None:
    """Print the Scope-B anti-self-close refusal for a target-class that carries
    the lightweight structural gate (acquisition / descriptor, ms-142 T3 / e-5158)
    and exit non-zero. Single source for the two verbs so the "Paths forward" text
    cannot drift between them (T3 maintainability review §6). Names the concrete
    RETRY invocation so a context-zero agent can act directly from the error, not
    just learn which env var exists (T3 AX review, principle 3).

    ``action`` is the human phrase for what is refused ("marking X done" /
    "closing X"); ``retry_cmd`` is the original command to re-run under a signal.
    Callers guard with ``_ai_session_direct_completion_ban_active()`` before
    calling this (it always exits)."""
    print(
        f"Error: {action} directly from an AI session is refused "
        "(ms-142 T3 anti-self-close gate — completion needs a human signal).\n"
        "  Paths forward (= one of these):\n"
        f"    1. BEACON_TARGET_COMPLETE_USER_OVERRIDE=1 {retry_cmd}\n"
        "       (explicit one-off user opt-in for this completion)\n"
        "    2. BEACON_SESSION_KIND=human — declare the session human-driven, "
        "then re-run.",
        file=sys.stderr,
    )
    sys.exit(2)


def _gate_target_class(data: dict, kind: str) -> None:
    """Enforce profession ⊃ target-class containment before creating a target
    (ms-115 e-3785). Prints the guidance-rich block message and exits non-zero
    when the project's profession does not own ``kind`` — so a wrong-profession
    create fails structurally instead of producing an invisible ghost target."""
    try:
        occupation.assert_target_class_owned(data, kind)
    except occupation.TargetClassProfessionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# ms-127 e-4803: bus budget / recipient / identity leaf helpers promoted from
# commands.py. Shared foundation used by both the bus family (cmd_bus.py) and a
# few commands.py callers (_arm_for_trek / cmd_acquisition_attack_list_send /
# _check_recipient_live_health). The 6 identity/swap/budget-path helpers below
# are pulled in transitively by _resolve_recipient_live / _read_bus_budget /
# _write_bus_budget, so they must live here too (a commands_shared resident
# cannot reach back into cmd_bus without forming a cycle). importlib is a local
# import inside _resolve_recipient_live, matching the leaf-only discipline.
# ---------------------------------------------------------------------------

def _get_bus_budget_path() -> str:
    """Resolve .beacon/bus-budget.json under the current project root."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "bus-budget.json")


def _read_bus_budget() -> Optional[dict]:
    path = _get_bus_budget_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # Treat a corrupted budget file as "no budget granted" — the gate
        # is fail-closed: sends refused until a human re-grants.
        return None


def _write_bus_budget(data: dict) -> None:
    path = _get_bus_budget_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# ms-141 / e-4965: client-side recent-send guard for idempotent DM sends.
#
# The server only dedups on client_event_id when is_retry=true (app.py), so a
# naive re-run of `beacon bus send` (fresh key, is_retry=false) always creates a
# true duplicate. This guard makes an *accidental identical re-send* idempotent
# by DEFAULT without any server change: `beacon bus send` records a fingerprint
# (project + channel + recipient + payload) of each dm send in a small cwd-local
# log; a second send with the same fingerprint within a short window returns the
# first send's result verbatim instead of posting again. Scoped to channel="dm"
# because non-dm channels (operation-trigger / trek / heartbeat) legitimately
# repeat identical payloads. `--allow-duplicate` bypasses the guard for an
# intentional resend (AX: auto-correct the common accidental case, keep an
# explicit escape for the rare intentional one).
# ---------------------------------------------------------------------------
_BUS_DEDUP_WINDOW_DEFAULT = 90   # seconds; accidental re-runs are seconds apart
_BUS_SENT_LOG_MAX = 30           # keep only the most-recent N sends


def _get_bus_sent_log_path() -> str:
    """Resolve .beacon/bus-sent-log.json under the current project root.

    ``BEACON_BUS_SENT_LOG_PATH`` overrides the location (tests point it at a
    per-test tmp file so the guard never writes the real repo .beacon/ or
    cross-contaminates other tests)."""
    override = os.environ.get("BEACON_BUS_SENT_LOG_PATH", "").strip()
    if override:
        return override
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "bus-sent-log.json")


def _read_bus_sent_log() -> list:
    path = _get_bus_sent_log_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        # A corrupted log just means "no recent-send memory" — fail open so the
        # guard never blocks a legitimate send on a bad file.
        return []


def _write_bus_sent_log(entries: list) -> None:
    path = _get_bus_sent_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries[-_BUS_SENT_LOG_MAX:], f, ensure_ascii=False, indent=2)


def _bus_dedup_window_seconds() -> int:
    """Recent-send window in seconds. 0 or negative disables the guard."""
    raw = os.environ.get("BEACON_BUS_DEDUP_WINDOW_SEC", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            return _BUS_DEDUP_WINDOW_DEFAULT
    return _BUS_DEDUP_WINDOW_DEFAULT


def _bus_send_fingerprint(
    *, project_id: str, channel: str, recipient: str,
    recipient_user: str, payload_raw: str,
) -> str:
    """Stable fingerprint of a logical send (user-intent parts only, not the
    metadata the CLI stamps onto the payload afterwards)."""
    basis = "|".join([
        project_id or "", channel or "", recipient or "",
        recipient_user or "", payload_raw or "",
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _bus_find_recent_send(
    entries: list, fingerprint: str, window_sec: int, now: float,
) -> Optional[dict]:
    """Return the most-recent log entry matching ``fingerprint`` if it is within
    ``window_sec`` of ``now``, else None. Pure (now/window injected) so the
    staleness boundary is unit-testable. window_sec <= 0 disables the guard."""
    if window_sec <= 0 or not fingerprint:
        return None
    for e in reversed(entries):
        if e.get("fingerprint") != fingerprint:
            continue
        # The most-recent match decides: if it is stale, older ones are too.
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            return None
        return e if (now - ts) <= window_sec else None
    return None


def _bus_recent_send_record(fingerprint: str, event: dict) -> None:
    """Append a successful send to the recent-send log so a later identical
    re-send is recognised by ``_bus_find_recent_send``. Paired with the guard
    check in cmd_bus_send — keep the stored shape (fingerprint / ts / event /
    event_id) in sync with what the lookup reads. Best-effort: a log-write
    failure must never break a send that already landed on the server."""
    if not fingerprint:
        return
    try:
        log = _read_bus_sent_log()
        log.append({
            "fingerprint": fingerprint,
            "ts": time.time(),
            "event": event,
            "event_id": (event or {}).get("event_id", ""),
        })
        _write_bus_sent_log(log)
    except Exception:
        pass


def dm_sent_rows(events: list, my_sid: str, limit: int = 20) -> list:
    """ms-141 / e-4966: build the sender-side "DMs I sent" audit rows.

    Pure function (no IO) so the shaping + duplicate detection is unit-testable.
    ``events`` is the raw list from ``list_bus_events(channel="dm")``; keep only
    the ones this session sent (``sender_session_id == my_sid``), newest first,
    cap at ``limit``. Each row carries the receipt fields already on the event
    (created_at = sent, delivered_at, opened_at) plus a ``duplicate`` flag =
    the same (recipient, text) appears more than once in the shown set (the
    "did I send the same DM twice recently?" signal — the shown recent window
    stands in for "short time").
    """
    mine = [
        e for e in (events or [])
        if str((e or {}).get("sender_session_id") or "") == (my_sid or "")
    ]
    mine.sort(key=lambda e: str((e or {}).get("created_at") or ""), reverse=True)
    mine = mine[: max(int(limit), 0)] if limit else mine
    rows = []
    for e in mine:
        p = (e or {}).get("payload") or {}
        _rsid = p.get("recipient_session_id") or ""
        _ruid = p.get("recipient_user_id") or ""
        rows.append({
            "event_id": (e or {}).get("event_id", ""),
            # AX (PR #622): keep the id type unambiguous for a follow-up
            # `dm send --to` (session-scoped) vs `--to-user` (user-scoped) —
            # a caller must not have to guess which kind `recipient` is.
            "recipient_session_id": _rsid,
            "recipient_user_id": _ruid,
            "recipient": _rsid or _ruid or "",  # display convenience
            "text": p.get("text") or "",
            "created_at": (e or {}).get("created_at", ""),
            "delivered_at": (e or {}).get("delivered_at", ""),
            "opened_at": (e or {}).get("opened_at", ""),
            "duplicate": False,
        })
    # Flag same (recipient, text) appearing more than once in the shown set.
    seen: dict = {}
    for r in rows:
        seen[(r["recipient"], r["text"])] = seen.get(
            (r["recipient"], r["text"]), 0) + 1
    for r in rows:
        if seen.get((r["recipient"], r["text"]), 0) > 1:
            r["duplicate"] = True
    return rows


def _bus_auto_execute_channels(data: dict) -> list:
    """Read the allowlist from a project.json dict, with type guard.

    Anything other than a list of strings is treated as an empty list — the
    safe default. We coerce on read rather than on write so a hand-edited
    project.json with the wrong shape never silently lets auto-execute slip
    through; the user sees "no channels are armed" until they re-add via CLI.
    """
    raw = data.get("bus_auto_execute_channels")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def _mirror_auto_execute_channels_to_local(channels: list) -> None:
    """Write-through mirror of the auto-execute allowlist into local
    ``.beacon/project.json``.

    Why: in cloud mode, ``save_project`` PUTs to the cloud only — the local
    file is a stale snapshot from ``beacon init`` time and is never refreshed
    on writes (StoreApi.save_project doesn't mirror). The inbox hook
    (``bin/beacon-bus-inbox-hook.py``) reads ``bus_auto_execute_channels``
    from the local file directly (= cheap, no subprocess), so without this
    mirror the hook always sees an empty allowlist and degrades every
    opted-in ``operation-trigger`` event to ``propose-to-ai`` — silently
    breaking the autonomous loop even when the CLI shows the channel as
    allowed.

    Scoped to this single config field on purpose: a full local mirror is
    ms-36 territory (cloud-first cache rethink). This is a targeted patch
    that closes the autonomous-loop UX gap without changing wider semantics.

    Fail-soft: any error (cwd not a beacon project, file unwritable, etc.)
    is swallowed — the cloud-side write already succeeded, so the user-
    facing operation is still correct; only the inbox hook's local read
    stays stale, which is the pre-existing (broken) baseline.
    """
    try:
        project_file = os.environ.get("BEACON_PROJECT_FILE") or os.path.join(
            ".beacon", "project.json")
        if not os.path.exists(project_file):
            return
        with open(project_file, "r", encoding="utf-8") as f:
            local = json.load(f)
        local["bus_auto_execute_channels"] = list(channels)
        with open(project_file, "w", encoding="utf-8") as f:
            json.dump(local, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] local mirror of bus_auto_execute_channels failed "
            f"(cloud write succeeded; inbox hook may read stale value): "
            f"{type(exc).__name__}: {exc}\n"
        )


def _row_owner_identity(row: dict) -> Tuple[str, str]:
    """Extract (user_id, email) from a directory row, normalised to strings.

    Both fields are best-effort: the server stamps ``user_id`` at the top
    level of each session document (= ms-70 / e-1713 path); ``actor.email``
    arrives via ``stamp_session_actor_email`` (ms-54 / e-1349) on bridge
    boot. Either one alone is enough to recognise "same user" for the
    auto-swap decision; we accept both so e-2280 swap works across the
    full deployment surface even if one field is unset on legacy bridges.
    """
    uid = str(row.get("user_id") or "").strip()
    actor = row.get("actor") if isinstance(row.get("actor"), dict) else {}
    email = str(actor.get("email") or "").strip()
    return (uid, email)


def _identity_matches(a: Tuple[str, str], b: Tuple[str, str]) -> bool:
    """Same-user check: any non-empty field matches on both sides.

    Conservative: an empty pair on either side is never a match (so we
    never auto-swap into a row whose owner we cannot identify). Either
    user_id OR email matching is sufficient — this tolerates legacy
    bridges that only stamp one of the two.
    """
    a_uid, a_email = a
    b_uid, b_email = b
    if a_uid and b_uid and a_uid == b_uid:
        return True
    if a_email and b_email and a_email == b_email:
        return True
    return False


def _find_stale_recipient_identity(
    recipient: str, helpers
) -> Optional[Tuple[str, str]]:
    """Look up the (user_id, email) the stale recipient sid belonged to.

    Live+healthy directory excludes the stale sid (by definition — that's
    why we're here). We query the broader directory (no healthy filter,
    wider since_min) to recover the dead session's owner identity, so
    the swap candidate search has something to match against.

    Returns ``None`` if the sid can't be found in the broader directory
    either — that means we don't know who owned it (= stamp was lost
    or the sid was synthetic / never registered). In that case we fall
    through to the existing soft-warn path; auto-swap is unsafe without
    a confirmed owner identity.
    """
    try:
        rows = helpers.discover_and_aggregate(healthy=False, since_min=1440)
    except Exception:
        return None
    for row in rows:
        if row.get("session_id") == recipient:
            ident = _row_owner_identity(row)
            if ident[0] or ident[1]:
                return ident
            return None
    return None


def _find_swap_candidate(
    stale_recipient: str,
    healthy_rows: list,
    helpers,
) -> Optional[dict]:
    """Pick a same-user live+healthy session to swap the stale sid to.

    Rules (intentionally narrow to keep auto-swap conservative):
      * Stale recipient's owner identity must be recoverable (= we need
        a user_id or email to match on).
      * Among healthy rows, count rows that match that identity AND are
        not the stale sid itself.
      * Exactly 1 match → return it (= unambiguous swap).
      * 0 or 2+ matches → return None (fall through to soft warn; we
        will not guess between multiple live sessions of the same user).

    The 2+ case matters: the same user can hold multiple concurrent
    bclaude sessions on different worktrees / machines. Without further
    context (cwd, machine, agent) we can't tell which one the sender
    "meant", so we refuse to guess and let the sender re-pick.
    """
    stale_ident = _find_stale_recipient_identity(stale_recipient, helpers)
    if stale_ident is None:
        return None
    matches = []
    for row in healthy_rows:
        if row.get("session_id") == stale_recipient:
            continue
        if _identity_matches(_row_owner_identity(row), stale_ident):
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_recipient_email_via_self_sessions(recipient: str) -> str:
    """Return an email for ``recipient`` iff it is one of the caller's OWN sessions.

    e-3880: the live-directory path (``_resolve_recipient_live``) only surfaces
    ``actor.email`` when the recipient's session document happens to carry it —
    and that field is stamped separately (``stamp_session_actor_email``), so a
    session minted purely from a heartbeat (``machine=None`` etc.) can be live
    yet have a blank email. When that happens, a same-user cross-project reply
    (which the server dm_gate permits) gets misclassified as 外部宛 and held in
    armed mode — the exact non-determinism this fixes.

    The robust fallback: ``GET /api/me/sessions`` (``list_user_sessions``) returns
    ONLY the calling user's own sessions across all their projects (the server
    scopes it via ``db.list_projects(user_id=uid)``). So if the recipient sid is
    present in that set, the recipient is — by construction — the SAME user as
    the sender. We then return the sender's own login email as the recipient
    identity, guaranteeing ``_same_user`` fires even when the session doc's
    ``actor.email`` was never stamped. (If the found row does carry an
    ``actor.email`` we prefer that, but the sender email is a safe floor since
    membership already proves same-user.)

    Returns ``""`` when the sid is NOT one of the caller's own sessions (= a
    genuinely external recipient, or lookup unavailable) so the caller keeps the
    conservative 外部 default. Never raises — any failure degrades to ``""``.
    """
    if not recipient:
        return ""
    try:
        # BaseException, not Exception: _get_api_client does sys.exit(1) (=
        # SystemExit) when there's no login / cloud.json. A best-effort
        # identity resolver must degrade to "" there, never abort the send.
        client, _config = _get_api_client()
    except BaseException:
        return ""
    try:
        # since_minutes only matters with live_only; unfiltered we get every
        # session of the caller (across projects). 1 day keeps a just-stopped
        # sibling visible without live-filtering it out.
        my_sessions = client.list_user_sessions(since_minutes=1440)
    except Exception:
        return ""
    # e-3880 review fix (over-relaxation): /api/me/sessions returns EVERY session
    # in the caller's projects — including a *co-member's* session in a shared
    # project. Project co-membership is NOT same-user. So we must positively
    # confirm the matched row's owner ``user_id`` equals the caller's own uid;
    # only then is it safe to treat the recipient as same-user (and hand the
    # caller's own email to _same_user). A different user's row must NOT return
    # the caller's email — that would misclassify a cross-user DM as 内部 and let
    # it bypass the armed 外部宛 hold that ms-110 exists to enforce.
    try:
        caller_uid, caller_email, _ = _resolve_creator_identity()
    except Exception:
        caller_uid, caller_email = "", ""
    caller_uid = str(caller_uid or "").strip()
    caller_email = str(caller_email or "").strip()
    for s in my_sessions or []:
        if s.get("session_id") != recipient:
            continue
        row_uid = str(s.get("user_id") or "").strip()
        row_email = str((s.get("actor") or {}).get("email") or "").strip()
        if caller_uid and row_uid and row_uid == caller_uid:
            # Confirmed same user (by owner user_id). Prefer the row's stamped
            # email (authoritative); fall back to the caller's own email so the
            # same-user identity holds even when actor.email was never stamped.
            return row_email or caller_email or ""
        # Different / unconfirmable user: return their stamped email if present
        # (→ _same_user False → 外部宛), else "" (external default). Never the
        # caller's email here.
        return row_email
    return ""


def _make_notice(
    advise: Optional[Callable[[str], None]],
) -> Callable[[str], None]:
    """ms-140: return the sink for a non-fatal advisory. When ``advise`` is
    given (the --json path), advisories are collected by the caller (folded into
    the result's "notes"); when it is None, they print to stderr as before. Both
    ``bus send`` helpers route their proceed-anyway warnings through this so the
    "keep --json stdout pure" fallback lives in exactly one place (single source
    of truth). Hard-error paths do NOT use this — they sys.exit and print to
    stderr directly, since they never coexist with a success JSON."""
    return advise if advise is not None else (
        lambda m: print(m, file=sys.stderr))


def _resolve_recipient_live(
    recipient: str, channel: str,
    advise: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Liveness gate + soft auto-swap for stale session_id reuse.

    e-1402 established the soft-warn floor: any ``--to <sid>`` DM send
    triggers a live+healthy directory check; if the sid isn't present,
    emit a loud stderr warning naming the Skill as recommended fix and
    let the send proceed.

    e-2280 extends that with a structural recovery path: when the stale
    sid resolves to a known user, and that user has *exactly one* live
    healthy session in the directory, auto-swap to that sid (= the AI's
    new bclaude restart). The swap is loud (stderr notice) so it stays
    auditable, and conservative (only single-candidate swaps) so we
    never silently redirect cross-machine or cross-worktree.

    Returns ``(new_recipient, swap_notice, recipient_email)`` where
    ``swap_notice`` is ``None`` if no swap happened (= unchanged sid), or a
    short string describing the swap (= caller can log / display).
    ``recipient_email`` is the resolved recipient's ``actor.email`` when the
    live check found them (or the swap target), else ``""`` — e-3566 uses it
    so the qual gate can tell a same-user cross-project DM from a真の外部宛.

    Distinct from ``_validate_recipient_project`` (e-1362) which routes
    cross-project. This one is identity-grain (same user, new sid).

    Opt-outs:
      * ``BEACON_BUS_NO_LIVE_CHECK=1`` — bypass entirely (= retains
        e-1402 contract; CI / automation that handles its own liveness).
      * ``BEACON_BUS_NO_AUTO_SWAP=1`` — keep the live-check warning but
        never swap. Useful for scripts that want to detect stale sends
        explicitly without surprise redirection.
      * ``BEACON_BUS_REFUSE_STALE=1`` — hard-refuse (exit 1) when stale
        AND no swap candidate. Opt-in strictness for CI pipelines that
        treat dead-sid sends as a bug rather than a soft hint.
    """
    # ms-140: the stale-swap / not-live warnings below are non-fatal (the send
    # proceeds), so in --json mode they must be routable into the caller's
    # advisory sink (folded into the result "notes") instead of stderr — a note
    # on stderr merges ahead of the JSON on stdout (the Bash tool combines the
    # streams), corrupts the caller's parse, and drives a duplicate resend. The
    # hard-refuse path (BEACON_BUS_REFUSE_STALE) keeps printing to stderr since
    # it sys.exit's and never yields a success JSON.
    _notice = _make_notice(advise)
    if channel != "dm" or not recipient:
        return (recipient, None, "")
    if os.environ.get("BEACON_BUS_NO_LIVE_CHECK", "") == "1":
        # Liveness check opted out, but identity resolution is independent and
        # still needed for the qual gate's same-user recognition (e-3880).
        return (recipient, None, _resolve_recipient_email_via_self_sessions(recipient))

    try:
        import importlib
        helpers = importlib.import_module(
            "beacon_cli.skills_helpers.dm_discover"
        )
    except Exception:
        # No discovery module available (e.g. beacon_cli not on the CLI
        # subprocess's sys.path) — bypass the liveness check, but still resolve
        # identity via the api client so same-user recognition (the qual gate)
        # does not depend on the dm_discover helper importing (e-3880).
        return (recipient, None, _resolve_recipient_email_via_self_sessions(recipient))

    try:
        rows = helpers.discover_and_aggregate(healthy=True, since_min=10)
    except Exception:
        # Discovery raised (network / psutil / auth) — bypass the liveness
        # check, but still resolve identity: same-user recognition (the qual
        # gate) must not depend on local bridge discovery succeeding (e-3880).
        return (recipient, None, _resolve_recipient_email_via_self_sessions(recipient))

    for row in rows:
        if row.get("session_id") == recipient:
            # e-3566: surface the recipient's identity (actor.email) so the
            # qual gate can recognise a same-user cross-project DM and not
            # over-block it as 外部宛. This reuses the live-check we already ran.
            row_email = str((row.get("actor") or {}).get("email") or "")
            if not row_email:
                # e-3880: the live directory row can lack a stamped
                # actor.email (heartbeat-only session). Fall back to the
                # sid→user resolution so a same-user cross-project reply is
                # still recognised and not held in armed mode.
                row_email = _resolve_recipient_email_via_self_sessions(recipient)
            return (recipient, None, row_email)  # live + healthy, all good

    # Not in the live+healthy set. Try auto-swap before falling back to
    # the soft warning.
    swap_disabled = os.environ.get("BEACON_BUS_NO_AUTO_SWAP", "") == "1"
    candidate = None
    if not swap_disabled:
        candidate = _find_swap_candidate(recipient, rows, helpers)

    if candidate is not None:
        new_sid = str(candidate.get("session_id") or "")
        actor = candidate.get("actor") or {}
        ident_hint = (
            actor.get("email")
            or actor.get("machine")
            or actor.get("agent")
            or "(unknown owner)"
        )
        notice = (
            f"recipient {recipient[:24]}… is stale; auto-swapped to "
            f"{new_sid[:24]}… (owner={ident_hint}, single live match)"
        )
        _notice(f"⇄ {notice}")
        swap_email = str(actor.get("email") or "")
        if not swap_email:
            # e-3880: swap target's row can also lack a stamped email; resolve
            # via the caller's own sessions so same-user免除 still fires.
            swap_email = _resolve_recipient_email_via_self_sessions(new_sid)
        return (new_sid, notice, swap_email)

    # Stale + no swap candidate (= zero or multiple matches, or owner
    # identity not recoverable). Either soft-warn (default) or hard-
    # refuse if the strict opt-in is set.
    if os.environ.get("BEACON_BUS_REFUSE_STALE", "") == "1":
        print(
            f"Error: recipient session {recipient[:24]}… is not in the "
            f"live+healthy directory and BEACON_BUS_REFUSE_STALE=1 is set."
            f" Refusing to send into a dead or unreachable session. Use "
            f"`/beacon-dm-send` Skill to re-discover a live recipient, or"
            f" unset the env to fall back to soft-warn.",
            file=sys.stderr,
        )
        sys.exit(1)

    _notice(
        f"⚠ recipient session {recipient[:24]}… is not in the live+healthy "
        f"directory. The send will proceed, but the target may be dead or "
        f"unreachable. Consider `/beacon-dm-send` Skill which re-discovers "
        f"on every send. (Set BEACON_BUS_NO_LIVE_CHECK=1 to suppress, "
        f"BEACON_BUS_REFUSE_STALE=1 to hard-refuse instead.)"
    )
    # e-3880: the recipient may be a same-user session that simply isn't in the
    # live+healthy set right now (just stopped, poll stale). Still resolve the
    # identity via the caller's own sessions so a same-user cross-project reply
    # is not held in armed mode purely because the target wasn't "healthy".
    return (recipient, None, _resolve_recipient_email_via_self_sessions(recipient))


# ---------------------------------------------------------------------------
# ms-127 e-4809: retro-day / week / document / content-input leaf helpers
# promoted from commands.py. Shared foundation used by the retro family
# (cmd_retro.py) and by commands.py callers (_auto_fire_retro_trigger /
# cmd_search / cmd_doc_add / cmd_doc_update). DAY_NAMES is the constant
# _get_retro_day maps day abbreviations through.
# ---------------------------------------------------------------------------

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _get_retro_day():
    try:
        data = load_project()
        day_str = data.get("retro_day", "friday").lower()
        return DAY_NAMES.get(day_str, 4)
    except Exception:
        return 4


def _last_reviewed_week() -> Optional[str]:
    """Return the most recent ISO-week string a retro was reviewed for, or None.

    Reads the `.beacon/retro/.reviewed` marker that `beacon retro done` writes.
    Used by the persistent retro trigger to distinguish "already retro'd this
    week" from "retro is overdue for one or more past weeks".
    """
    project_dir = os.path.dirname(get_project_file())
    reviewed_path = os.path.join(project_dir, "retro", ".reviewed")
    try:
        with open(reviewed_path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (FileNotFoundError, IOError):
        return None


def _most_recent_retro_day_on_or_before(today, retro_day_idx: int):
    """Return the latest date <= today whose weekday() == retro_day_idx.

    If today *is* the retro day, returns today. Used to anchor the
    "current retro week" — every Friday (or configured day) starts a new
    retro slot that must be settled before it becomes "stale".
    """
    import datetime as _dt
    delta = (today.weekday() - retro_day_idx) % 7
    return today - _dt.timedelta(days=delta)


def _resolve_content_input(content: str) -> str:
    """Resolve a ``--content`` argument, treating ``"-"`` as stdin.

    PE dogfood 2026-06-10: ``beacon doc update --content -`` was interpreted
    literally and replaced a 130-line SPEC with the single character ``-``.
    kubectl / curl convention treats ``-`` as stdin; we follow the same rule
    and hard-reject the dangerous case where stdin is a tty.
    """
    if content == "-":
        if sys.stdin.isatty():
            print("Error: --content - は stdin からの読み込みを意味します", file=sys.stderr)
            print("       pipe で渡してください: cat file.md | beacon doc update <id>", file=sys.stderr)
            print("       または --content フラグを省略して stdin から渡してください", file=sys.stderr)
            sys.exit(1)
        return sys.stdin.read()
    if not content and not sys.stdin.isatty():
        return sys.stdin.read()
    return content


def _load_local_documents() -> list[dict]:
    """Read all .beacon/documents/*.md files and return them as dicts with
    frontmatter fields (title, scope, milestone, operation, content, etc.)
    promoted to top level. Used by cmd_search for local-mode document search."""
    docs: list[dict] = []
    project_file = get_project_file()
    docs_dir = os.path.join(os.path.dirname(project_file), "documents")
    if not os.path.isdir(docs_dir):
        return docs
    for fname in os.listdir(docs_dir):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(docs_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        # Parse YAML-ish frontmatter (--- delimited)
        meta: dict[str, str] = {}
        content = raw
        if raw.startswith("---\n"):
            try:
                _, fm, body = raw.split("---\n", 2)
                for line in fm.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip().strip('"').strip("'")
                content = body
            except ValueError:
                pass
        doc_id = meta.get("doc_id") or fname[:-3]
        docs.append({
            "doc_id": doc_id,
            "title": meta.get("title", fname[:-3]),
            "scope": meta.get("scope", "memo"),
            "milestone": meta.get("milestone", ""),
            "operation": meta.get("operation", ""),
            "content": content,
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        })
    return docs


# ---------------------------------------------------------------------------
# ms-127 e-4815: application-map applicability helpers promoted from commands.py.
# _application_map_applies decides whether the全貌マップ (application-map) drift /
# reconcile machinery applies to this project; _auto_fire_map_drift_trigger
# (commands.py) and the deploy family (cmd_deploy.py) both call it. It pulls in
# _project_profession_safe (profession lookup that never raises), so that helper
# is promoted alongside it (a commands_shared resident cannot reach back into
# cmd_deploy without a cycle).
# ---------------------------------------------------------------------------

def _project_profession_safe() -> str:
    """Best-effort read of the project's profession (e.g. ``dev`` / ``sales``),
    defaulting to ``dev``. Used to gate development-only surfaces such as the
    application-map (ms-109 e-3404) without hard-failing when the store is
    unavailable."""
    try:
        data = get_store().load_project()
    except Exception:
        return "dev"
    # ms-108 e-3701 (fable review B-6): one definition of "resolve profession"
    # lives in occupation.resolve_profession — reuse it rather than re-inlining
    # the ``(get("profession") or "dev").strip().lower()`` expression.
    return occupation.resolve_profession(data)


def _application_map_applies() -> bool:
    """True when the application-map (= 全機能の現在地索引) applies to this
    project. It is a development-instance surface: it maps code / CLI / Skill
    entry points, which a sales project does not own. So map seeding, the
    map-drift backstop, and the deploy-time reconcile prompt fire only for the
    development instance (ms-109 e-3404). Other occupations get neither the
    box at init nor the recurring nags."""
    return _project_profession_safe() == "dev"


# ---------------------------------------------------------------------------
# ms-127 e-4820: docs / frontmatter / project-id leaf helpers promoted from
# commands.py alongside the trek family split. _get_docs_dir / _parse_frontmatter
# / _read_local_doc / _current_project_id and the DEFAULT_SCOPE constant are
# shared foundation used by the trek family (cmd_trek.py) and by many commands.py
# callers (cmd_doc_* / _auto_fire_operation_triggers / account / table-doc). A
# commands_shared resident cannot reach back into cmd_trek without a cycle, so
# these live here. datetime is imported locally inside _read_local_doc.
# ---------------------------------------------------------------------------

def _current_project_id() -> str:
    """Return the current project's id (= what ``load_project()`` operates on).

    Used by trek-show / trek-timeline aggregation to decide which scope
    entries it can resolve locally (= same-project) and which are
    cross-project hints the caller must visit separately.

    Resolution order (ms-83 / e-2007 dogfood finding):
      1. ``data.id`` / ``data.project_id`` on the local project.json
         (= local mode and cloud-cached layouts that store the id inline)
      2. ``.beacon/cloud.json`` ``project_id`` (= cloud mode default —
         project.json is the cached document and does not embed the id)
      3. Empty string if neither path resolves
    """
    try:
        data = load_project()
    except Exception:
        data = {}
    pid = (data.get("id") or data.get("project_id") or "").strip()
    if pid:
        return pid
    try:
        cloud_path = _get_cloud_config_path()
        if os.path.exists(cloud_path):
            with open(cloud_path, "r", encoding="utf-8") as f:
                cloud_cfg = json.load(f)
            return (cloud_cfg.get("project_id") or "").strip()
    except Exception:
        pass
    return ""


DEFAULT_SCOPE = "memo"
VALID_SCOPES = ("core", "spec", "memo", "retro", "report")  # ms-127 e-4831 (doc scope vocab)


def _get_docs_dir():
    project_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(project_dir, "documents")


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text.

    Returns ``(metadata_dict, body_text)``. Block-list values (lines starting
    with ``- `` immediately after a key with empty value) are returned as
    ``list[str]``; everything else is ``str``. PE dogfood 2026-06-10:
    block-list ``approved_actions`` of the form ``- "op:op-2:check_run"`` were
    silently destroyed by the previous line-based parser which split each
    ``- "op:...`` row on its first colon.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta = {}
    lines = header.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            # Orphan list item without a preceding key — ignore.
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val:
            # Inline list form: ``key: [a, b]`` is parsed to ``list[str]`` so
            # that block- and inline-list values round-trip identically.
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if not inner:
                    meta[key] = []
                else:
                    meta[key] = [
                        s.strip().strip('"').strip("'")
                        for s in inner.split(",")
                        if s.strip()
                    ]
            else:
                meta[key] = val
            continue
        # Empty inline value → look ahead for a block-list.
        items: list[str] = []
        while i < len(lines):
            next_stripped = lines[i].strip()
            if not next_stripped:
                i += 1
                continue
            if next_stripped.startswith("- "):
                items.append(next_stripped[2:].strip().strip('"').strip("'"))
                i += 1
                continue
            break
        meta[key] = items if items else ""
    return meta, body


def _read_local_doc(fpath):
    """Read a local document file and return parsed metadata."""
    import datetime
    fname = os.path.basename(fpath)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, body = _parse_frontmatter(content)
    scope = meta.get("scope", DEFAULT_SCOPE)
    milestone = meta.get("milestone", "")
    # Find title from first heading in body
    first_line = ""
    for line in body.split("\n"):
        line = line.strip()
        if line:
            first_line = line
            break
    title = first_line.lstrip("# ") if first_line.startswith("#") else fname[:-3]
    stat = os.stat(fpath)
    updated = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
    operation = meta.get("operation", "")
    trek_id = meta.get("trek_id", "")  # ms-69 / e-1663
    result = {
        "doc_id": fname[:-3],
        "title": title,
        "scope": scope,
        "content": content,
        "updated_at": updated,
    }
    if milestone:
        result["milestone"] = milestone
    if operation:
        result["operation"] = operation
    if trek_id:
        result["trek_id"] = trek_id
    # ms-131 e-4494: surface the document format (``table`` vs default markdown)
    # so table-doc readers (CLI show / Web UI) can branch without re-parsing the
    # frontmatter. Additive — plain markdown docs omit it and read unchanged.
    if meta.get("format"):
        result["format"] = meta["format"]
    # Soft-delete fields surface so cmd_doc_list can filter without
    # re-parsing the frontmatter (ms-14 e-973).
    if meta.get("status"):
        result["status"] = meta["status"]
    for k in ("trashed_at", "trashed_by", "trash_reason"):
        if meta.get(k):
            result[k] = meta[k]
    return result



# ---------------------------------------------------------------------------
# ms-127 e-4831-foundation: doc family shared helpers (promoted from commands.py)
# frontmatter build/slug, link-target validation, table-doc model load/write/
# persist. Shared by cmd_doc.py (doc handlers) and the acquisition / profile /
# briefing callers remaining in commands.py.
# ---------------------------------------------------------------------------
def _doc_slug(title):
    """Generate a file-safe slug from a document title."""
    slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "untitled"

def _add_frontmatter(content, scope, milestone="", operation="", trek_id="",
                     drop_milestone=False, drop_operation=False, target="",
                     doc_format="", drop_target=False):
    """Prepend frontmatter to content, or update existing scope/milestone/operation/trek_id.

    List values are written as inline YAML arrays (``key: ["a", "b"]``) so
    they survive the line-based parser on the next round-trip — block-list
    items containing colons (e.g. ``op:op-2:check_run``) cannot be expressed
    safely in the line-based format and must be normalised.

    ``trek_id`` (ms-69 / e-1663) associates a doc with a cross-project trek;
    optional, defaults preserved on round-trip.

    ``target`` (ms-109 e-3754) is the canonical, target-class-agnostic doc
    linkage key (``acc-1`` / ``opp-3`` / ``ms-5`` …). When set it writes
    ``target: <id>`` and, for Targets that predate it (milestone / operation /
    trek), dual-writes the legacy key so existing readers keep working. New
    Target classes (account / opportunity) carry ``target`` only.

    ``drop_milestone`` / ``drop_operation`` (e-1859) explicitly remove the
    matching key from existing frontmatter. ``cmd_doc_update`` sets these
    when the user switches a doc from milestone scope to operation scope
    (or vice versa) so the rejected field doesn't linger and produce
    two-headed (= both milestone and operation set) frontmatter that
    silently misleads ``/beacon-operation-review`` discovery filters.
    """
    import work_model
    meta, body = _parse_frontmatter(content)
    meta["scope"] = scope
    # ms-131 e-4496: ``format`` distinguishes a table-doc from the default
    # markdown. Set it only when a non-empty format is passed, and never emit
    # ``format: markdown`` (the default is the absence of the key, so existing
    # markdown docs stay byte-identical on round-trip).
    if doc_format and doc_format != "markdown":
        meta["format"] = doc_format
    if drop_milestone:
        meta.pop("milestone", None)
    elif milestone:
        meta["milestone"] = milestone
    if drop_operation:
        meta.pop("operation", None)
    elif operation:
        meta["operation"] = operation
    if trek_id:
        meta["trek_id"] = trek_id
    if drop_target:
        # ms-131 e-4497: detach — remove the canonical target and any legacy
        # mirror so ``doc update <id> --target ""`` fully unlinks the doc.
        prior = meta.pop("target", "")
        prior_legacy = work_model.legacy_link_key_for(prior)
        if prior_legacy:
            meta.pop(prior_legacy, None)
    elif target:
        # ms-109 e-3754: canonical linkage + back-compat dual-write of the
        # legacy key (milestone / operation / trek_id) when the Target is one
        # of the classes that had one, so legacy readers/filters keep resolving.
        meta["target"] = target
        legacy_key = work_model.legacy_link_key_for(target)
        if legacy_key:
            meta[legacy_key] = target
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            quoted = ", ".join(f'"{item}"' for item in v)
            lines.append(f"{k}: [{quoted}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body

def _validate_link_target_exists(target: str) -> None:
    """Exit with a clear error if a doc's link ``target`` is a hard-validated
    class (sales account / opportunity / acquisition) whose id has no record.

    ms-134 (philosophy review 2026-08-02 #1): the existence knowledge — which
    kinds are hard-validated and which collections hold them — lives in the
    occupation dispatch layer (``occupation.is_valid_link_target``), NOT here. So
    this profession-SHARED (doc) path no longer branches on sales collections; it
    asks the occupation layer and only owns the CLI error/exit. No-op for dev /
    unknown targets (lenient round-trip preserved)."""
    if target and not occupation.is_valid_link_target(load_project(), target):
        kind = work_model.target_kind(target or "")
        # AX review 2026-08-02 #2: name the recovery path so a caller can find a
        # valid id instead of guessing. Keep the historical "{kind} not found:
        # {target}" phrasing (multiple tests assert that contiguous substring)
        # and APPEND the recovery hint. account/opportunity/acquisition each have
        # a `list` verb.
        print(f"Error: {kind} not found: {target}. "
              f"Run 'beacon {kind} list' to see valid IDs.", file=sys.stderr)
        sys.exit(1)

def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _split_frontmatter_raw(content: str) -> tuple[str, str]:
    """Split content into (raw_frontmatter_block, body), preserving the block
    verbatim. Returns ("", content) when there is no frontmatter. The raw block
    includes the closing ``---`` and its trailing newline, so
    ``raw + body == content`` for round-trip fidelity (we swap only the body)."""
    if not content.startswith("---"):
        return "", content
    end = content.find("\n---", 3)
    if end == -1:
        return "", content
    # Advance past the closing '---' line and any following blank line(s), the
    # same way _parse_frontmatter computes the body start.
    body_start = end + 4
    raw = content[:body_start]
    body = content[body_start:]
    stripped = body.lstrip("\n")
    raw += body[: len(body) - len(stripped)]
    return raw, stripped

def _load_table_model(doc_id: str):
    """Load a table-doc and return (content, title, model). Exits with a clear
    error when the doc is missing or is not a table-doc."""
    import table_doc
    existing = get_store().get_document(doc_id)
    if not existing:
        print(f"Document not found: {doc_id}", file=sys.stderr)
        sys.exit(3)
    content = existing.get("content", "")
    if not table_doc.is_table_content(content):
        print(f"Error: {doc_id} は table-doc ではありません (format: table のみ対応)",
              file=sys.stderr)
        sys.exit(1)
    try:
        model = table_doc.parse_table(content)
    except table_doc.TableDocError as exc:
        print(f"Error: table 構造が壊れています: {exc}", file=sys.stderr)
        sys.exit(1)
    return content, existing.get("title", doc_id), model

def _write_table_model(doc_id: str, title: str, old_content: str, model) -> None:
    """Re-serialize ``model`` into the doc, keeping the original frontmatter
    verbatim, and write it back through the cloud/local path."""
    import table_doc
    raw_fm, _ = _split_frontmatter_raw(old_content)
    new_body = table_doc.serialize_table_body(title, model)
    new_content = raw_fm + new_body if raw_fm else new_body
    if _is_cloud_mode():
        client, config = _get_api_client()
        client.update_document(config["project_id"], doc_id, title, new_content)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)

def _persist_table_doc(*, title, columns, scope, milestone="", operation="",
                       trek_id="", target="", doc_id=""):
    """Validate columns, build + link + persist a table-doc, record the dev-Target
    entry, and return ``(doc_id, model)``.

    The single create path shared by ``cmd_doc_table_create`` (env-driven) and
    ``cmd_acquisition_attach_list`` (param-driven) so neither has to mutate
    ``os.environ`` to reach the other — the delegation is an ordinary function
    call with an explicit signature (ms-132 e-4502 AX/保守性レビュー: env mutation
    as in-process argument passing broke local reasoning). Raises
    ``table_doc.TableDocError`` on an invalid column definition so callers keep
    their own error wording."""
    import table_doc
    import table_type
    import work_model
    table_type.validate_column_types(columns if isinstance(columns, list) else [])
    model = table_doc.new_table(columns)
    # ms-131 e-4497 — hard-validate a sales Target (account/opportunity/
    # acquisition) exists before linking, mirroring cmd_doc_add.
    if target:
        _validate_link_target_exists(target)
    if scope == "core":
        milestone = milestone or None
    body = table_doc.serialize_table_body(title, model)
    content = _add_frontmatter(body, scope, milestone or "", operation or "",
                               trek_id or "", target=target or "",
                               doc_format=table_doc.TABLE_FORMAT)
    if _is_cloud_mode():
        client, config = _get_api_client()
        if doc_id:
            result = client.update_document(config["project_id"], doc_id, title, content)
        else:
            result = client.create_document(config["project_id"], title, content)
        doc_id = result["doc_id"]
    else:
        docs_dir = _get_docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        if not doc_id:
            doc_id = _doc_slug(title)
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
    # ms-134 e-4720: record the table-doc create side effect through the
    # occupation layer, which dispatches by the Target's kind and no-ops when
    # there is no dev-era changelog to record onto (sales Target / trek). Replaces
    # a direct core.save_entry that required a milestone (bug e-4710). core docs
    # are project-wide; a table-doc with no explicit link records nothing (its
    # linkage lives in the frontmatter, surfaced via `doc list --target`).
    data = load_project()
    today = _now_iso()
    if scope != "core":
        link = target or operation or milestone or ""
        if link:
            rec = occupation.record_target_entry(
                data, link, description=f"table-doc create: {title} ({scope})",
                source="auto", date=today, revision_id=doc_id or "")
            if rec.get("recorded"):
                save_project(data)
    return doc_id, model


def _actor_str() -> str:
    """Best-effort machine/agent identity string for the audit trail."""
    try:
        import agent as _agent_for_actor
        act = _agent_for_actor.get_actor()
        m, a = act.get("machine", ""), act.get("agent", "")
        return f"{m}/{a}" if (m or a) else ""
    except Exception:
        return ""

def _sales_skill_nudge(what: str, skill: str, detail: str) -> None:
    """営業エンティティの user-facing verb を直叩きしたとき、対応する対話スキルへ soft に
    誘導する (e-3760)。**hard block はしない** (master=人間) ので nudge を stderr に出して
    実行はそのまま続ける。スキル自身の正規呼び出しは ``BEACON_SALES_SKILL_CALL=1`` を
    立てるので nudge は出さない (= dev の `beacon pr review`→`/review` 誘導と対称、ただし
    こちらは同じ verb を skill も使うため bypass token で自身の呼び出しを素通しにする)。
    stderr に出すので ``--json`` の stdout は汚さない。"""
    if os.environ.get("BEACON_SALES_SKILL_CALL") == "1":
        return
    print(f"💡 {what}は {skill} で対話的に進めると{detail}。"
          f"このまま直接続行します (master=人間)。", file=sys.stderr)


def _today_iso() -> str:
    """Today's date as ``YYYY-MM-DD`` (date-only). ms-132 e-4623: the single
    source for the date stamped into ``date``-typed table columns — those columns
    reject ``_now_iso()``'s time component, so a future policy change (timezone,
    format) has one place to edit rather than a slice expression copied per site
    (PR #559 保守性レビュー M1)."""
    return _now_iso()[:10]


def _parse_number(raw: str, flag: str):
    """Parse an optional numeric flag; empty → None, int-if-whole else float."""
    if not raw or not raw.strip():
        return None
    try:
        val = float(raw)
        return int(val) if val.is_integer() else val
    except ValueError:
        print(f"Error: {flag} must be a number, got {raw!r}", file=sys.stderr)
        sys.exit(1)

# ---------------------------------------------------------------------------
# ms-127 e-4849: promoted from commands.py during the milestone-family split.
# Used by BOTH the milestone handlers (cmd_milestone.py) and the target /
# transition / backlog handlers still in commands.py; lives here so both
# import it by bare name without a cmd_milestone<->commands cycle.
# ---------------------------------------------------------------------------
def _release_occupation_for_transition(data, target_id, *, reason):
    """ms-81 e-1918: phase transitions auto-release any active occupation on the
    target. Per the SPEC the release happens whether or not the session that claimed
    it is the same one calling the transition (= a done verb on someone else's claim
    is implicitly a takeover).

    ms-142 T7 (e-5162): generic over target-class — releases the live claim on ANY
    target (milestone / operation / release / descriptor) via ``core.release_occupation``,
    so a non-milestone transition frees its claim too. The append-only audit event
    (``worktree_sessions``) stays milestone-scoped (milestone-specific history, not
    the live-claim layer). Best-effort: a not-found target is a silent no-op."""
    sid = _resolve_session_id() or ""
    try:
        import agent as _agent_for_release
        actor = _agent_for_release.get_actor()
    except Exception:
        actor = {}
    try:
        _rec, released = core.release_occupation(data, target_id, reason=reason)
    except ValueError:
        return  # target not found (already gone) — release is best-effort
    if released and work_model.target_kind(target_id) == "milestone":
        core.milestone_record_occupation_event(
            data, ms_id=target_id, event_type="release",
            session_id=sid,
            machine=actor.get("machine", ""),
            agent=actor.get("agent", ""),
            reason=reason,
        )


def _claim_occupation_for_work(data, target_id):
    """Stamp a live occupation claim on ANY target when a session starts working it
    (ms-142 T7 e-5162) — the generic sibling of ``milestone start``'s claim, so the
    "someone is sitting here now" layer covers operation / release / descriptor
    targets, not just milestones (closing the silent double-work hole, 理想像 §5).

    Warns (never blocks — soft claim, SPEC §3-3) on a collision with ANOTHER
    session's existing claim. Only stamps when a real session id resolves: a
    session-less context (test sandbox / scripted scaffold) is a no-op, so it never
    perturbs a record's shape where there is no live session to claim on its behalf.
    A not-found target is a silent no-op."""
    sid = _resolve_session_id() or ""
    if not sid:
        return
    try:
        import agent as _agent_for_claim
        actor = _agent_for_claim.get_actor()
    except Exception:
        actor = {}
    try:
        _rec, previous = core.claim_occupation(
            data, target_id, session_id=sid,
            machine=actor.get("machine", ""), agent=actor.get("agent", ""))
    except ValueError:
        return  # target not found — best-effort
    if previous and previous.get("session_id") and \
            previous.get("session_id") != sid:
        prev_sid = previous.get("session_id", "?")
        prev_machine = previous.get("machine", "?")
        print(
            f"  [occupation] {target_id} は別セッション {prev_sid[:12]}... "
            f"({prev_machine}, claimed_at: {previous.get('claimed_at', '?')}) が "
            f"作業中でした。takeover で続行します — 相手がまだ動いているなら "
            f"beacon dm で調整してください (二重作業防止)。",
            file=sys.stderr,
        )

def _print_evidence_guidance(eid: str, target_id: str) -> None:
    """Print the 'attach independent review evidence' guidance for a pending approval
    (ms-119 / e-4205, #504 maint review — this text was copied across the completion
    route, review-request, and the approve error; rendered once here). Interpolates
    the concrete target-id and enumerates the verdict vocabulary from its single
    source (#504 AX review — no bare <id> / <v> placeholders)."""
    verdicts = "|".join(_ta.REVIEW_EVIDENCE_VERDICTS)
    print(f"  独立証拠 (= 承認の前提, ms-119/e-4205): "
          f"`beacon review context --type attainment --target {target_id}` で判定を"
          f"生成し、")
    print(f"    `beacon target attach-review-evidence {eid} --verdict <{verdicts}> "
          f"--summary <text>` で記録 (無ければ approve は --acknowledge-no-evidence を要求)")

def _spec_updated_at_for_target(target_id: str) -> Optional[str]:
    """The SPEC doc's ``updated_at`` for a target, or None (ms-119 / e-4597).

    Evidence for the task↔SPEC last-written-intent tie-breaker in the attainment
    disposition gate: reuses the single-source _spec_doc_for_target scan, inferring the
    kind from the id prefix (op- → operation, else milestone). Best-effort — no spec /
    transport failure → None (the tie-breaker is simply omitted, never wrong)."""
    if not target_id:
        return None
    kind = "operation" if str(target_id).startswith("op-") else "milestone"
    doc = _spec_doc_for_target(target_id, kind)
    if not doc:
        return None
    return doc.get("updated_at") or None

def _spec_exists_for_ms(ms_id: str) -> bool:
    """True if any spec-scoped document is attached to ms_id (delegates to the
    single-source scan _spec_doc_for_target)."""
    return _spec_doc_for_target(ms_id, "milestone") is not None

# ---------------------------------------------------------------------------
# ms-127 e-4856: promoted from commands.py during the pr-family split.
# The review-due trigger helpers (+ _REVIEW_DUE_SUFFIX) are shared by BOTH the
# pr handlers (cmd_pr.py) and the `beacon review` handlers still in commands.py;
# they live here so both import them by bare name without a cmd_pr<->commands
# cycle.
# ---------------------------------------------------------------------------
def _fire_review_due_for_pr(review_type: str, label: str, pr_number: str,
                            pr_title: str, pr_url: str) -> None:
    """Write a '<type>-review-due' trigger for a PR-bound review (ms-119).

    One trigger file per review type so each re-surfaces (via ``beacon trigger
    check`` / session-start) and clears independently. Advisory only — never
    blocks. Best-effort: a bad PR number / IO error is swallowed so recording a
    PR never fails over a trigger write.
    """
    if not pr_number:
        return
    try:
        triggers_dir = _get_triggers_dir()
        os.makedirs(triggers_dir, exist_ok=True)
        import datetime
        trigger_data = {
            "name": f"{review_type}-review-due-{pr_number}",
            "kind": f"{review_type}-review-due",
            "pr_number": pr_number,
            "pr_url": pr_url,
            "review": review_type,
            "message": (
                f"PR #{pr_number} \"{pr_title or pr_url}\" が作成されました "
                f"({label} の節目)。文脈ゼロの独立 judge に原典と差分を渡して "
                f"drift を確認してください: "
                f"`/beacon-review-run --type {review_type} --pr {pr_number}` "
                f"(または `beacon review context --type {review_type} --pr {pr_number}`)。"
            ),
            "created_at": datetime.datetime.now().isoformat(),
        }
        trigger_path = os.path.join(triggers_dir, f"{review_type}-review-due-{pr_number}.json")
        with open(trigger_path, "w", encoding="utf-8") as f:
            json.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")
    except OSError:
        return

def _clear_review_due_for_pr(review_type: str, pr_number: str) -> None:
    """Remove a '<type>-review-due' trigger once the PR closes / merges — the
    interface / code-change 節目 is resolved (ms-119). Best-effort."""
    if not pr_number:
        return
    try:
        path = os.path.join(_get_triggers_dir(), f"{review_type}-review-due-{pr_number}.json")
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        return

def _fire_pr_open_review_triggers(pr_number: str, pr_title: str, pr_url: str) -> None:
    """Fire a review-due trigger for EVERY judge-run review type that binds to the
    PR-open 節目 (ms-119 / e-4003 + maintainability).

    Data-driven: the set of PR-bound reviews is read from the review-type registry
    (descriptor ``fires_on == "pr-open"``), so adding a new PR-bound review type
    (drop a review-type.json with fires_on=pr-open) makes it auto-fire here with
    **no code change** — e-4009's data-driven registry, extended from *assembly*
    to *firing*. Today AX and maintainability both bind here; philosophy /
    attainment bind to target transitions instead (see _fire_review_due_trigger).
    """
    if not pr_number:
        return
    import review_spine
    for tid, desc in review_spine.judge_run_review_types().items():
        if desc.get("fires_on") != "pr-open":
            continue
        _fire_review_due_for_pr(tid, desc.get("label", tid), pr_number, pr_title, pr_url)


def _clear_pr_open_review_triggers(pr_number: str) -> None:
    """Clear all PR-open review-due triggers for a PR (ms-119). Mirror of
    _fire_pr_open_review_triggers over the same registry set. ms-127 e-4859:
    promoted here alongside its fire counterpart so the fire/clear pair shares a
    single canonical home (the pr close/merge handlers in cmd_pr.py and any
    review-family caller import it from here)."""
    if not pr_number:
        return
    import review_spine
    for tid, desc in review_spine.judge_run_review_types().items():
        if desc.get("fires_on") != "pr-open":
            continue
        _clear_review_due_for_pr(tid, pr_number)
    # ms-119 e-4060: PR resolved (merged/closed) → drop the reviewed done-marker
    # too, so the trigger dir does not accumulate stale markers.
    try:
        marker = _pr_open_reviewed_marker_path(pr_number)
        if os.path.exists(marker):
            os.remove(marker)
    except OSError:
        pass


_REVIEW_DUE_SUFFIX = "-review-due-"

def _pending_review_types_for_pr(pr_number: str) -> list:
    """Return the review types whose review-due trigger is still present for this
    PR — i.e. independent reviews (AX / maintainability) that fired at PR-open
    and have NOT been run/cleared yet. Empty when none outstanding. This is the
    single gate signal beacon pr approve/merge read (ms-119 e-4060)."""
    pr_number = str(pr_number or "").strip()
    if not pr_number:
        return []
    tail = f"{_REVIEW_DUE_SUFFIX}{pr_number}.json"
    out: list = []
    try:
        for fn in sorted(os.listdir(_get_triggers_dir())):
            if not fn.endswith(tail):
                continue
            rtype = fn[:-len(tail)]
            try:
                with open(os.path.join(_get_triggers_dir(), fn),
                          encoding="utf-8") as f:
                    rtype = json.load(f).get("review") or rtype
            except (OSError, ValueError):
                pass
            if rtype:
                out.append(rtype)
    except OSError:
        return []
    return out

def _pr_open_reviewed_marker_path(pr_number: str) -> str:
    return os.path.join(_get_triggers_dir(), f".reviewed-pr-{pr_number}")
