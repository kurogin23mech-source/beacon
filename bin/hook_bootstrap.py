"""Shared bootstrap for beacon's Python hook scripts (ms-169 e-6296).

The PreToolUse gate (``beacon-untrusted-tool-gate.py``), the PostToolUse vet hook
(``beacon-untrusted-turn-vet-hook.py``), and the bus-inbox hook
(``beacon-bus-inbox-hook.py``) each need the same three primitives before they can
do anything useful:

  * find the beacon project root (walk up for a ``.beacon/project.json`` marker),
  * read + parse the harness hook JSON off stdin, and
  * import ``lib/`` modules that live in a sibling directory of ``bin/``.

Those were copied verbatim into all three scripts. The copies had already begun
to drift — the inbox hook walked parents with a ``while`` loop while the gate and
vet hooks used ``(cur, *cur.parents)`` — and the lib-search convention
(``(here.parent / "lib", here.parent.parent / "lib")``) was re-spelled in five
separate ``_import_*`` helpers, so changing the layout rule meant editing one
place and silently leaving the rest behind (PR #738 maintainability finding).

This module is the ONE definition of all three. Each hook locates it via its OWN
directory — ``Path(__file__).resolve().parent`` — which is unambiguous in every
install layout (dev repo ``bin/``, or the individually-copied ``~/.local/bin``
form), so there is no lib-search convention to duplicate just to reach the
bootstrap. Everything past that point lives here.

Every function fails SAFE: a hook that cannot find its root / read its input /
import its lib must degrade to a silent no-op, never raise into the harness.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def find_beacon_root(start: "str | Path") -> "Path | None":
    """Walk up from ``start`` looking for a ``.beacon/project.json`` marker.

    Returns the project root ``Path`` or ``None`` when ``start`` is not inside a
    beacon project (the hooks treat ``None`` as "not our concern → no-op")."""
    try:
        cur = Path(start).resolve()
    except Exception:
        return None
    for cand in (cur, *cur.parents):
        try:
            if (cand / ".beacon" / "project.json").exists():
                return cand
        except Exception:
            continue
    return None


def read_hook_input() -> "dict | None":
    """Read stdin and parse the harness hook JSON.

    Returns the parsed ``dict`` (an empty stdin yields ``{}`` — a usable, empty
    payload), or ``None`` when the input could not be turned into a dict (read
    error, malformed JSON, or a non-object top-level value). Callers that must
    have a payload treat ``None`` as "return"; the lenient inbox hook coerces
    ``None`` to ``{}`` and proceeds."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _lib_dirs() -> tuple:
    """The candidate ``lib/`` directories relative to THIS file — the single
    definition of the lib-search convention. ``bin/`` and ``lib/`` are siblings in
    the dev repo (``here.parent / "lib"``); the second candidate covers a nested
    install layout (``here.parent.parent / "lib"``)."""
    here = Path(__file__).resolve().parent
    return (here.parent / "lib", here.parent.parent / "lib")


def import_lib(*names: str) -> "dict | None":
    """Import the named ``lib/`` modules from the sibling lib dir.

    Returns ``{name: module}`` only when EVERY requested module is present and
    imports cleanly; otherwise ``None`` (fail-safe: a hook that cannot import the
    libs it classifies with stays silent). The lib dir is put on ``sys.path`` once.
    This is the ONLY place the lib-search convention lives, so a layout change is a
    one-line edit here rather than five copies across three scripts."""
    if not names:
        return None
    for lib_dir in _lib_dirs():
        try:
            present = all((lib_dir / f"{n}.py").exists() for n in names)
        except Exception:
            present = False
        if not present:
            continue
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
        try:
            return {n: importlib.import_module(n) for n in names}
        except Exception:
            return None
    return None
