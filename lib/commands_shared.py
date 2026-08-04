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

import json
import os
import sys
from typing import Optional

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
    """Return (user_id, email, session_id) for the trek creator.

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
