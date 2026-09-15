"""ms-159 / e-6290 — derive a session's *working target* and *activity* when it
has not declared them (the fallback half of the D slice's hybrid truth source).

The 統合オペレーションUI needs each session row to answer "which root target
(= project) ＞ which target (= milestone / task / opportunity) is this session
working on, and what is it doing right now?". A session that declares this is
authoritative (it knows its own focus). But most existing sessions never
declare it, so we DERIVE a best-effort answer from signals the bridge already
stamps: ``git.branch`` + ``cwd`` + ``.beacon/fork.json`` → the working target,
``git.head_subject`` → the activity (作業概要 = 1-line summary of what it is
doing).

The asymmetry mirrors ``lib/bus_liveness.derive_state`` (方針2): the
DECLARATION is正 (authoritative), and only when it is absent do we fall back to
the derived guess — never the reverse. This means a bare session shows a decent
answer immediately (from its branch), and a declaring session shows a precise
one, on the same row shape.

Pure functions only. The caller passes already-read inputs (``fork_json``
parsed, ``branch`` / ``cwd`` / ``head_subject`` strings), so the contract is
pinned by unit tests without a bus or filesystem — the same流儀 as
``derive_state`` and ``lib/attention``.
"""
from __future__ import annotations

import re
from typing import Optional

import bus_liveness

# States whose "activity" (作業概要) is the WAIT/STOP detail — what the session is
# waiting for or blocked on — NOT the git head subject (ms-159 / e-6484). For a
# session that is `awaiting_human` / `blocked`, its last commit subject would
# misrepresent it as "doing" that commit; the human wants "レビュー待ち / 許可待ち /
# 相手の返信待ち". When no detail is known we return EMPTY rather than falling back
# to the head subject — 判定できない待機理由は素直に空にする (ms-173 SPEC 方針2 =
# データはでっち上げず消費側は空欄で正しく描画する)。
_WAIT_DETAIL_STATES = frozenset({
    bus_liveness.STATE_AWAITING_HUMAN,
    bus_liveness.STATE_BLOCKED,
})

# A milestone id embedded in a branch or worktree name, e.g.
# "ms-159-fork-361e58", "ms-159", "ms-159-backoffice-stub". First match wins.
_MS_ID_RE = re.compile(r"(ms-\d+)", re.IGNORECASE)

# Provenance of a derived answer, most→least authoritative. Surfaced on the row
# so the ops面 can show whether a target is a real declaration or a heuristic
# guess (a branch-parsed guess must not look as trustworthy as a declaration).
SOURCE_DECLARED = "declared"
SOURCE_FORK_JSON = "fork_json"
SOURCE_BRANCH = "branch"
SOURCE_CWD = "cwd"
SOURCE_FOCUS = "focus"
SOURCE_NONE = "none"


def _clean(s) -> str:
    """Best-effort string coercion + strip; falsy / non-str → ""."""
    if not s:
        return ""
    try:
        return str(s).strip()
    except Exception:
        return ""


def _ms_id_in(text) -> str:
    """Return the first ``ms-<n>`` found in ``text`` (lowercased), or ""."""
    t = _clean(text)
    if not t:
        return ""
    m = _MS_ID_RE.search(t)
    return m.group(1).lower() if m else ""


def _basename(pathish) -> str:
    """Last non-empty path segment of ``pathish`` (posix or windows), or ""."""
    p = _clean(pathish).replace("\\", "/").rstrip("/")
    if not p:
        return ""
    return p.rsplit("/", 1)[-1]


def _valid_target(t) -> bool:
    """A target dict is usable iff it is a dict carrying a non-empty id."""
    return isinstance(t, dict) and bool(_clean(t.get("id")))


def _target(kind, id_, title, source) -> dict:
    return {
        "kind": kind,
        "id": _clean(id_),
        "title": _clean(title),
        "source": source,
    }


def _root_from(fork_json, cwd, project) -> Optional[dict]:
    """Best-effort root target (= the project the session works in).

    Priority: an explicit ``project`` hint (the row's own project, passed by the
    server) → the fork's parent repo basename (``.beacon/fork.json``) → the cwd
    basename. Returns ``None`` if nothing is knowable.
    """
    if isinstance(project, dict) and _clean(project.get("id") or project.get("label")):
        return {
            "kind": project.get("kind") or "project",
            "id": _clean(project.get("id")),
            "label": _clean(project.get("label") or project.get("id")),
        }
    if isinstance(fork_json, dict):
        parent_repo = _basename(fork_json.get("parent_repo_path"))
        if parent_repo:
            return {"kind": "project", "id": "", "label": parent_repo}
    base = _basename(cwd)
    if base:
        return {"kind": "project", "id": "", "label": base}
    return None


def derive_working_target(
    declared,
    *,
    branch="",
    cwd="",
    fork_json=None,
    focus_milestone=None,
    project=None,
) -> dict:
    """Return ``{"root", "target", "source"}`` for a session's working target.

    ``declared`` (the session's own working_target dict) wins when present and
    parseable (carries a target with a non-empty id). Otherwise derive, most→
    least authoritative:

    1. ``fork_json.target_ms_id`` — a fork was stood up *for* this milestone, so
       its declared intent is strong (SOURCE_FORK_JSON).
    2. an ``ms-<n>`` in ``branch`` (SOURCE_BRANCH) then in ``cwd`` (SOURCE_CWD)
       — the worktree naming convention encodes the target.
    3. ``focus_milestone`` — the project's active MS. LOWEST priority: SPEC 方針2
       warns it is *not* session-specific (a fork on a different MS would show
       the wrong one), so it only fills in when nothing better is known.

    ``root`` is the project the session works in (see ``_root_from``); it rides
    along regardless of how ``target`` was resolved. ``source`` records the
    provenance so the ops面 can distinguish a declaration from a guess.
    """
    root = _root_from(fork_json, cwd, project)

    # 1. Declaration is authoritative when parseable.
    if isinstance(declared, dict):
        dt = declared.get("target")
        if _valid_target(dt):
            return {
                "root": declared.get("root") or root,
                "target": {
                    "kind": _clean(dt.get("kind")) or "milestone",
                    "id": _clean(dt.get("id")),
                    "title": _clean(dt.get("title")),
                    "source": SOURCE_DECLARED,
                },
                "source": SOURCE_DECLARED,
            }

    # 2. fork.json target (a fork's reason for existing).
    if isinstance(fork_json, dict):
        fid = _clean(fork_json.get("target_ms_id"))
        if fid:
            return {
                "root": root,
                "target": _target(
                    "milestone", fid, fork_json.get("target_ms_title"),
                    SOURCE_FORK_JSON),
                "source": SOURCE_FORK_JSON,
            }

    # 3. ms-id encoded in the branch, then the cwd.
    bid = _ms_id_in(branch)
    if bid:
        return {"root": root,
                "target": _target("milestone", bid, "", SOURCE_BRANCH),
                "source": SOURCE_BRANCH}
    cid = _ms_id_in(cwd)
    if cid:
        return {"root": root,
                "target": _target("milestone", cid, "", SOURCE_CWD),
                "source": SOURCE_CWD}

    # 4. Project active MS (weakest — not session-specific).
    if isinstance(focus_milestone, dict) and _clean(focus_milestone.get("id")):
        return {
            "root": root,
            "target": _target(
                "milestone", focus_milestone.get("id"),
                focus_milestone.get("title"), SOURCE_FOCUS),
            "source": SOURCE_FOCUS,
        }

    # 5. Nothing knowable — still return the root (may be None) so the row shape
    # is stable and the caller never has to null-check the dict itself.
    return {"root": root, "target": None, "source": SOURCE_NONE}


def derive_activity(declared_activity, *, head_subject="", state="",
                    state_detail="") -> str:
    """Return the session's activity (作業概要, 1 line), declared-or-derived.

    Precedence:

    1. An explicit ``declared_activity`` (the session's own self-report) always
       wins — it knows best what it is doing / waiting for.
    2. Otherwise the answer is STATE-AWARE (ms-159 / e-6484):
       * ``awaiting_human`` / ``blocked`` (``_WAIT_DETAIL_STATES``) → the
         ``state_detail`` (what it is waiting for / blocked on). When that is
         empty, return EMPTY — never the git head subject, which would
         misrepresent a stalled session as actively working on its last commit
         (判定できない待機理由は素直に空、ms-173 方針2 = no fabrication).
       * every other state (``running`` / ``idle`` / … / unknown) → the last
         commit subject (``git.head_subject``), the best proxy for "what it is
         doing".

    ``state_detail`` is named to match the directory row field of the same name
    (#749 review, maintainability): the wait detail lives on the row as
    ``row["state_detail"]`` (the field ``lib/attention`` reads too), so aligning
    the parameter name removes the wait_detail↔state_detail rename step at the
    call site and the positional-arg confusion an AI hits when it reads the row
    schema and calls this by shape.

    ``state``/``state_detail`` default to "" so callers that don't know the state
    (e.g. the server projection, older call sites) keep the pre-e-6484 behaviour
    exactly: declared-or-head_subject. The provenance is intentionally not
    returned — unlike the working target, a wrong activity guess is low-stakes.
    """
    declared = _clean(declared_activity)
    if declared:
        return declared
    if _clean(state) in _WAIT_DETAIL_STATES:
        return _clean(state_detail)
    return _clean(head_subject)


# ---------------------------------------------------------------------------
# Row enrichment (ms-159 / e-6399) — turn a server directory row into the
# derive-function inputs, and attach the results.
#
# The pure derive functions above take already-extracted signals. But the CLI
# fetch path (cmd_attention / cmd_bus_directory) gets whole directory rows from
# the server, which never carry ``working_target``/``activity`` (the server
# stamp is not deployed, and the derive fallback was never wired — e-6399). The
# fallback signals the bridge DOES stamp on every row are ``git.branch`` +
# ``cwd`` + ``focus.milestone``, and ``git.head_subject`` for the activity. This
# maps a row → those inputs and attaches the derived answer, so the roster shows
# a real target/activity instead of an empty ``(no target)/—`` shell.
#
# ``fork_json`` is intentionally NOT sourced here: a row describes a *remote*
# session, whose ``.beacon/fork.json`` lives on that session's machine and is
# not readable from here. If a fork session declares its working_target (the
# server-stamp path, when it lands), that declaration rides in ``row[
# "working_target"]`` and still wins inside ``derive_working_target``.
# ---------------------------------------------------------------------------

def working_target_for_row(row) -> dict:
    """Derive ``{"root", "target", "source"}`` for a server directory row.

    Declaration/existing-authoritative: a ``row["working_target"]`` that already
    carries content (a ``root`` or a ``target``) is the answer already — the
    server stamped it, or a session declared it — so it is returned VERBATIM and
    never clobbered by a guess (even a declared root with no specific target must
    survive). Only when it is absent or an empty shell do we derive from the
    signals the bridge stamps: ``git.branch`` → ``cwd`` → the project's active MS
    (``focus.milestone``), in that priority. ``root`` on the derived path is the
    row's own project (``project_id`` / ``project_name``)."""
    row = row if isinstance(row, dict) else {}
    existing = row.get("working_target")
    if isinstance(existing, dict) and (existing.get("root") or existing.get("target")):
        return existing
    git = row.get("git") if isinstance(row.get("git"), dict) else {}
    focus = row.get("focus") if isinstance(row.get("focus"), dict) else {}
    project = {
        "kind": "project",
        "id": _clean(row.get("project_id")),
        "label": _clean(row.get("project_name")) or _clean(row.get("project_id")),
    }
    return derive_working_target(
        None,
        branch=git.get("branch") or "",
        cwd=row.get("cwd") or "",
        fork_json=None,
        focus_milestone=focus.get("milestone"),
        project=project,
    )


def activity_for_row(row) -> str:
    """Derive the 1-line activity for a server directory row.

    A declared ``row["activity"]`` wins; otherwise it is state-aware (e-6484):
    an ``awaiting_human`` / ``blocked`` row shows its wait detail
    (``row["state_detail"]`` — the same field ``lib/attention`` reads) and is
    EMPTY when none is known, while any other state falls back to the row's
    ``git.head_subject``.

    NOTE (e-6488 wiring status, #750 AX3): the CARRY path for ``state_detail`` IS
    wired — beacon-state-hook.py writes it to the marker, the bridge piggybacks it
    on the heartbeat, and ``SessionUpsert`` stores it, so a deployed server exposes
    it on the row (via storage pass-through, not the liveness projection —
    ``server/app.py::_stamp_session_liveness`` deliberately does NOT stamp
    ``state_detail``; it only derives ``state``/``state_since``). What remains is a
    DEPLOY gap, not a code gap: the production server predates these fields, so
    until it ships, live rows carry no ``state_detail`` (and no ``state``), and the
    ``awaiting_human`` / ``blocked`` branch resolves to EMPTY. That empty is the
    intended, correct behaviour (no fabrication) — NOT a bug. A future editor
    seeing "waiting rows show blank activity" pre-deploy should NOT patch this;
    post-deploy the value flows through unchanged."""
    row = row if isinstance(row, dict) else {}
    git = row.get("git") if isinstance(row.get("git"), dict) else {}
    return derive_activity(
        row.get("activity"),
        head_subject=git.get("head_subject") or "",
        state=row.get("state") or "",
        state_detail=row.get("state_detail") or "",
    )


def enrich_row(row):
    """Return a shallow copy of ``row`` with ``working_target`` / ``activity``
    filled in from the derive fallback (declaration still wins inside the derive
    fns). Non-dict rows pass through unchanged. Non-mutating so callers can pass
    server rows without surprising aliasing."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    out["working_target"] = working_target_for_row(row)
    out["activity"] = activity_for_row(row)
    return out


def enrich_rows(rows) -> list:
    """``enrich_row`` over a list of directory rows (``None`` → ``[]``)."""
    return [enrich_row(r) for r in (rows or [])]
