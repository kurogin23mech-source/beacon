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


def derive_activity(declared_activity, *, head_subject="") -> str:
    """Return the session's activity (作業概要, 1 line), declared-or-derived.

    A declared activity wins; otherwise the last commit subject
    (``git.head_subject``) is the best available proxy for "what it is doing".
    Returns "" when neither is known (the ops面 renders a placeholder). The
    provenance is intentionally *not* returned here — unlike the working target,
    a wrong activity guess is low-stakes, and callers only need the text.
    """
    declared = _clean(declared_activity)
    if declared:
        return declared
    return _clean(head_subject)
