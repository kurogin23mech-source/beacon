"""Local file-based trek storage (ms-69 / e-1653).

Trek is a top-level cross-project entity, so we don't store it inside any
single ``project.json``. In **local mode** treks live as one JSON file per
trek under ``~/.beacon/treks/<trek_id>.json``. In **cloud mode** the CLI
talks to the server-side HTTP API (added in e-1656) which fans out to
``server/store_router`` (= firestore_client / dynamodb_client).

This module covers the local mode only. Cloud mode is intentionally
deferred so e-1653 can ship a self-contained CLI without a running server.

Test override: set ``BEACON_TREKS_DIR`` to point at a tmp dir.
"""
from __future__ import annotations

import json
import os
from typing import Iterable


def get_trek_dir() -> str:
    """Return the directory holding trek JSON files.

    Default: ``~/.beacon/treks/``. Tests override with ``BEACON_TREKS_DIR``.
    The directory is created lazily on first save.
    """
    custom = os.environ.get("BEACON_TREKS_DIR")
    if custom:
        return os.path.expanduser(custom)
    return os.path.expanduser("~/.beacon/treks")


def _path_for(trek_id: str) -> str:
    return os.path.join(get_trek_dir(), f"{trek_id}.json")


def load_trek(trek_id: str) -> dict | None:
    path = _path_for(trek_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_trek(trek: dict) -> None:
    """Persist a trek doc. ``trek["trek_id"]`` is the file key (authoritative)."""
    trek_id = trek.get("trek_id")
    if not trek_id:
        raise ValueError("trek doc missing trek_id")
    os.makedirs(get_trek_dir(), exist_ok=True)
    with open(_path_for(trek_id), "w", encoding="utf-8") as f:
        json.dump(trek, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_treks(*, actor_id: str | None = None,
               status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    """List treks, optionally filtered by visibility + status.

    Visibility (= actor_id) follows the same model as ``server.firestore_client``:
    ``None`` returns all (admin), set returns only treks where the actor is
    creator OR appears in the members list.
    """
    d = get_trek_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for fname in os.listdir(d):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # skip unreadable / malformed files (best-effort listing)
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        if actor_id:
            creator = (t.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id") for m in t.get("members", []) or []]
            if creator != actor_id and actor_id not in members:
                continue
        out.append(t)
    # Newest first (= matches server-side ordering)
    out.sort(key=lambda x: (x.get("created_at", ""), x.get("trek_id", "")),
             reverse=True)
    return out


def update_trek(trek_id: str, *, updates: dict) -> dict | None:
    """Read-modify-write helper. Returns the updated trek or None if missing."""
    trek = load_trek(trek_id)
    if trek is None:
        return None
    trek.update(updates)
    save_trek(trek)
    return trek
