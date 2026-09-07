"""ms-169 e-6237 — the untrusted-turn state that bridges the receive hook to the
PreToolUse gate.

The A gate (最後の砦) must pause for human approval when the AI is about to call
a side-effect tool *while untrusted content is live in the turn*. But the two
sides run in SEPARATE processes:

  * the receive hook (``beacon-bus-inbox-hook.py`` on UserPromptSubmit) is where
    untrusted DM content enters the AI's context (ms-169 e-6235 framing), and
  * the gate (``beacon-untrusted-tool-gate.py`` on PreToolUse) fires just before
    each tool call.

A per-turn in-memory flag can't cross that gap, so the receive hook persists a
small state file that the gate reads. This module is the ONE place that state's
shape + lifetime live.

Lifetime (documented tradeoff — ms-169 方針1/2, refined by e-6280):
  * ARM: the receive hook arms ``pending`` the moment it injects untrusted
    content (records the triggering sources — event_id / sender / a short body
    preview — + a timestamp), keyed by the harness session id so one session's
    DM never gates another session in the same cwd. A DM *proven* to be from the
    same user (e-6238) is NOT armed (no injection risk — the sender authenticated
    as this human); arming stays for cross-user / unknown-identity DMs.
  * FIRE: while ``pending`` is live, the gate routes any side-effect tool call to
    human approval (read-only calls pass), surfacing the sources inline so the
    human judges the *context*, not the tool. Holds in every session — including
    an ``armed`` autonomous one, which is exactly when no human is watching.
  * VET (e-6280): when a human approves a side-effect *while pending* (proven by
    the PostToolUse vet hook: a side-effect tool actually executed in an armed
    turn ⇒ the human approved it), the session is marked ``vetted``. From then on
    this session does NOT re-arm — subsequent untrusted DMs no longer nag for
    per-tool approval (承認範囲 = セッション全体, a deliberate friction/safety
    tradeoff: B body-isolation + untrusted framing remain for later DMs). This is
    what turns「毎ツール確認地獄」into「リスク文脈ごとの1回承認」.
  * DISARM: a plain *human* UserPromptSubmit clears ``pending`` (the human retook
    the turn without approving) but PRESERVES ``vetted`` — so per-session trust
    survives a normal turn, while an un-approved DM re-gates the next new DM.

autonomous safety: in an ``armed`` autonomous session an「ask」has no human to
approve it, so the side-effect never executes ⇒ the vet hook never fires ⇒ the
gate stays up. Vetting is thus impossible without a real human decision.

Fail-safe on I/O: the gate must never brick a session because the state file is
unreadable. ``is_armed`` returns None (not armed) on any read error so a corrupt
file can't stop every tool call. e-6274 hardens the fail-OPEN edge of that: a
*missing* file is benign (pass), but a *corrupt* one (present-but-unreadable) is
suspicious — ``state_file_health`` lets the gate tilt side-effect tools toward
human confirmation rather than silently passing, without over-gating read-only
calls or the common absent-file case. The security property is still ultimately
carried by the fail-closed *classification* (unknown tool → side-effect). A
follow-up (Codex PreToolUse parity, e-6273) is blocked until Codex grows a
pre-execution hook; today only the Claude gate reads this state.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

try:  # work_base gives the canonical ISO timestamp; degrade gracefully.
    import work_base
except Exception:  # pragma: no cover - work_base always present in-repo
    work_base = None  # type: ignore[assignment]

# State file under the project's .beacon dir. A single file holding a dict keyed
# by session so concurrent sessions in one cwd don't cross-contaminate.
STATE_RELPATH = (".beacon", "untrusted-turn.json")

# Entries older than this (hours) are pruned on write, so dead sessions that
# never disarmed don't accumulate forever.
_STALE_HOURS = 24

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def _now_iso() -> str:
    if work_base is not None:
        try:
            return work_base.now_iso()
        except Exception:
            pass
    return ""


def session_key_from_hook_input(hook_input: dict) -> str:
    """Resolve the key from a harness hook input's ``session_id``.

    Claude Code passes the same ``session_id`` to every hook event in a session
    (UserPromptSubmit and PreToolUse alike). Sanitised to a safe token. Returns
    "" when absent. Prefer ``resolve_session_key`` when a project root is
    available (it also lets a plain CLI — ``beacon dm show`` — agree on the key).
    """
    if not isinstance(hook_input, dict):
        return ""
    raw = hook_input.get("session_id") or ""
    return _SAFE_KEY_RE.sub("", str(raw))[:128]


def resolve_session_key(root: "str | Path", hook_input: dict = None) -> str:
    """Resolve the untrusted-turn key that hook + gate + CLI all agree on.

    Prefers ``.beacon/session.json``'s ``session_id`` (readable by every process
    in the cwd — the two inbox hooks, the PreToolUse gate, AND the ``beacon dm
    show`` CLI that has no harness hook input), falling back to the harness
    ``hook_input.session_id``. Sanitised; "" when neither resolves.

    Keying on the on-disk beacon session id (not only the harness id) is what
    lets the CLI's explicit fetch (``beacon dm show``) arm the SAME turn the gate
    reads — the harness id never reaches a plain CLI invocation.
    """
    try:
        sess = json.loads(
            (Path(root) / ".beacon" / "session.json").read_text(encoding="utf-8"))
        sid = sess.get("session_id") if isinstance(sess, dict) else ""
    except Exception:
        sid = ""
    if not sid and isinstance(hook_input, dict):
        sid = hook_input.get("session_id") or ""
    return _SAFE_KEY_RE.sub("", str(sid or ""))[:128]


def _state_path(root: "str | Path") -> Path:
    return Path(root).joinpath(*STATE_RELPATH)


def _read_all(root: "str | Path") -> dict:
    """Read the whole state map. Fail-safe: any error → empty map."""
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def state_file_health(root: "str | Path") -> str:
    """Classify the on-disk state file as ``"absent"`` / ``"ok"`` / ``"corrupt"``.

    e-6274 hardening: ``is_armed`` fails safe (corrupt → None → not armed), which
    for a corrupt file means fail-OPEN toward a side-effect. That is a bounded but
    real gap. This lets the gate tell the two "not armed" causes apart:

      * ``absent`` — no file at all. The overwhelming common case (a session that
        never received untrusted content); genuinely not armed, must pass so the
        gate doesn't nag every tool of every session.
      * ``ok`` — file parsed to a dict. Trust ``is_armed``'s answer.
      * ``corrupt`` — file exists but is unreadable / not a JSON object (truncated
        write, tampering, disk error). We CANNOT prove the session isn't armed, so
        the gate tilts a side-effect toward human confirmation instead of silently
        passing. Read-only calls still pass (no over-gating).
    """
    path = _state_path(root)
    try:
        if not path.exists():
            return "absent"
    except Exception:
        return "corrupt"  # can't even stat it → treat as suspicious
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "corrupt"
    return "ok" if isinstance(data, dict) else "corrupt"


def _hours_between(a_iso: str, b_iso: str) -> float:
    """Best-effort hours between two ISO strings; 0.0 if unparseable."""
    from datetime import datetime
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
        return abs((b - a).total_seconds()) / 3600.0
    except Exception:
        return 0.0


def _entry_timestamp(v: dict) -> str:
    """The most-recent activity timestamp on a state entry, for staleness pruning.

    Reads the new-shape ``pending.armed_at`` / ``vetted_at`` and falls back to a
    legacy top-level ``armed_at`` so pre-e-6280 files prune too."""
    if not isinstance(v, dict):
        return ""
    pend = v.get("pending")
    pend_at = pend.get("armed_at", "") if isinstance(pend, dict) else ""
    return pend_at or v.get("vetted_at", "") or v.get("armed_at", "")


def _write_all(root: "str | Path", state: dict) -> None:
    """Write the state map, pruning stale entries. Best-effort (never raises)."""
    now = _now_iso()
    if now:
        pruned = {}
        for k, v in state.items():
            ts = _entry_timestamp(v)
            if ts and _hours_between(ts, now) > _STALE_HOURS:
                continue  # drop stale (incl. long-lived vetted sessions)
            pruned[k] = v
        state = pruned
    path = _state_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _normalize_sources(sources, event_ids) -> list:
    """Build a de-duplicated ``[{event_id, sender, preview}]`` list from either a
    rich ``sources`` list (e-6280) or a bare ``event_ids`` list (back-compat).
    Order preserved; first occurrence of each event_id wins."""
    out: list = []
    seen: set = set()
    for s in (sources or []):
        if not isinstance(s, dict):
            continue
        eid = str(s.get("event_id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append({
            "event_id": eid,
            "sender": str(s.get("sender") or ""),
            "preview": str(s.get("preview") or ""),
        })
    # back-compat(e-6280): the bare event_ids form (no sender/preview) is only
    # used by callers predating sources[] (currently tests + a possible legacy
    # state file). Remove this branch once no caller passes event_ids= and old
    # state files have aged out via _STALE_HOURS.
    for raw in (event_ids or []):
        eid = str(raw or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        out.append({"event_id": eid, "sender": "", "preview": ""})
    return out


def arm(root: "str | Path", session_key: str, *, event_ids=None, sources=None,
        at: str = "") -> bool:
    """Arm ``pending`` untrusted content for ``session_key``. Returns True if it
    armed, False on a no-op.

    ``sources`` (preferred, e-6280) is a list of ``{event_id, sender, preview}``
    the gate surfaces inline; ``event_ids`` is the back-compat bare form. New
    sources merge into any existing pending set (dedup by event_id).

    No-op (returns False) when ``session_key`` is blank OR the session is already
    ``vetted`` (e-6280 per-session trust: once a human approved an untrusted
    context this session, later DMs no longer re-arm)."""
    if not session_key:
        return False
    state = _read_all(root)
    entry = state.get(session_key)
    if isinstance(entry, dict) and entry.get("vetted"):
        return False  # per-session: already vetted → suppress further arming
    new_sources = _normalize_sources(sources, event_ids)
    prev_pending = entry.get("pending") if isinstance(entry, dict) else None
    prev_sources = prev_pending.get("sources") if isinstance(prev_pending, dict) else None
    merged = _normalize_sources(list(prev_sources or []) + new_sources, None)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["vetted"] = bool(entry.get("vetted", False))  # stays False here
    entry["pending"] = {"armed_at": at or _now_iso(), "sources": merged}
    state[session_key] = entry
    _write_all(root, state)
    return True


def record_asked(root: "str | Path", session_key: str, tool_name: str = "") -> None:
    """Record that the PreToolUse gate actually emitted an approval「ask」for a
    side-effect in this pending turn (ms-169 e-6280 review fix).

    This closes a hole both independent reviewers flagged: ``vet`` used to infer
    "the human approved" purely from "a side-effect executed while armed". But a
    side-effect can execute while armed WITHOUT the gate ever asking — the gate
    hook fail-safe-returned, timed out, wasn't installed, or the harness runs in
    an auto-accept permission mode. In those cases the old ``vet`` would silently
    mark the session vetted and never gate again. By having the gate stamp an
    ``asked_at`` marker when (and only when) it emits an「ask」, and having the vet
    hook require that marker, the approval inference becomes a measured fact
    instead of a cross-process timing assumption. No-op if not armed (nothing to
    stamp) or session already vetted."""
    if not session_key:
        return
    state = _read_all(root)
    entry = state.get(session_key)
    if not isinstance(entry, dict) or entry.get("vetted"):
        return
    pending = entry.get("pending")
    if not isinstance(pending, dict):
        return  # not armed → nothing to stamp
    pending = dict(pending)
    pending["asked_at"] = _now_iso()
    if tool_name:
        pending["asked_tool"] = str(tool_name)
    entry = dict(entry)
    entry["pending"] = pending
    state[session_key] = entry
    _write_all(root, state)


def vet(root: "str | Path", session_key: str) -> None:
    """Mark ``session_key`` vetted: a human approved a side-effect under untrusted
    content this session (e-6280). Clears ``pending`` and suppresses future
    arming for the rest of the session. Blank key is a no-op."""
    if not session_key:
        return
    state = _read_all(root)
    entry = state.get(session_key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["vetted"] = True
    entry["vetted_at"] = _now_iso()
    entry["pending"] = None
    state[session_key] = entry
    _write_all(root, state)


def is_vetted(root: "str | Path", session_key: str) -> bool:
    """True when this session already approved an untrusted context (e-6280).
    Fail-safe: unreadable / missing state → False."""
    if not session_key:
        return False
    entry = _read_all(root).get(session_key)
    return bool(isinstance(entry, dict) and entry.get("vetted"))


def _quarantine_corrupt(root: "str | Path") -> None:
    """Move a corrupt state file aside and write a fresh empty map, so a human
    turn (which calls ``disarm``) genuinely heals it (ms-169 e-6274 review fix).

    Before this, a corrupt file made ``disarm`` early-return without writing (the
    parsed map was empty, so there was 'nothing to delete'), so the corrupt-file
    gate reason's promise "一度人間プロンプトを送ると再生成されます" was false — the
    file stayed corrupt and every side-effect kept re-asking. We rename the bad
    file to ``untrusted-turn.json.corrupt-<ts>`` (evidence preserved, never rm)
    and write an empty valid map. Best-effort (never raises)."""
    if state_file_health(root) != "corrupt":
        return
    path = _state_path(root)
    stamp = _SAFE_KEY_RE.sub("", _now_iso()) or "backup"
    try:
        path.replace(path.with_suffix(path.suffix + f".corrupt-{stamp}"))
    except Exception:
        pass  # keep going — we still overwrite with an empty map below
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    except Exception:
        pass


def disarm(root: "str | Path", session_key: str) -> None:
    """Clear ``pending`` for ``session_key`` (human retook the turn WITHOUT
    approving) but PRESERVE a per-session ``vetted`` flag (e-6280). Drops the
    whole entry when it was never vetted (nothing worth keeping).

    A corrupt state file is healed here (e-6274 review fix): the human turn
    quarantines the bad file and writes a fresh empty map, matching the recovery
    the gate's corrupt-file reason promises."""
    if not session_key:
        return
    _quarantine_corrupt(root)
    state = _read_all(root)
    entry = state.get(session_key)
    if not isinstance(entry, dict):
        if session_key in state:
            del state[session_key]
            _write_all(root, state)
        return
    if entry.get("vetted"):
        entry = dict(entry)
        entry["pending"] = None
        state[session_key] = entry
    else:
        del state[session_key]
    _write_all(root, state)


def is_armed(root: "str | Path", session_key: str) -> "dict | None":
    """Return the pending-turn dict when ``session_key`` has UNVETTED untrusted
    content live, else None. The dict carries ``armed_at`` / ``sources``
    ([{event_id, sender, preview}]) and a back-compat ``event_ids`` list.

    Fail-safe: an unreadable / missing state file, or a vetted session, returns
    None (not armed) so a corrupt file can never brick tool calls."""
    if not session_key:
        return None
    entry = _read_all(root).get(session_key)
    if not isinstance(entry, dict) or entry.get("vetted"):
        return None
    pending = entry.get("pending")
    if not isinstance(pending, dict):
        return None
    sources = [s for s in (pending.get("sources") or []) if isinstance(s, dict)]
    # back-compat(e-6280): event_ids mirrors sources[].event_id for readers that
    # predate the sources[] shape (gate _source_lines fallback, older tests).
    # Remove once all readers consume sources[] directly.
    event_ids = [s.get("event_id") for s in sources if s.get("event_id")]
    return {
        "armed_at": pending.get("armed_at", ""),
        "sources": sources,
        "event_ids": event_ids,
        # ms-169 e-6280 review fix: present only if the gate actually asked. The
        # vet hook requires it, so a side-effect that ran WITHOUT the gate asking
        # (fail-safe / timeout / uninstalled / auto-accept) can't silently vet.
        "asked_at": pending.get("asked_at", ""),
    }


__all__ = [
    "STATE_RELPATH",
    "session_key_from_hook_input",
    "resolve_session_key",
    "arm",
    "record_asked",
    "vet",
    "is_vetted",
    "disarm",
    "is_armed",
    "state_file_health",
]
