"""Beacon Agent Identification (ms-51 / e-931).

This module provides the *actor identification* primitive used by the
branch-driven multi-user / multi-machine workflow introduced in ms-51.

Design (per SPEC 7QQZWMijguuZ66ZFEbIi):

* The "actor" of a beacon operation is the pair ``(machine, agent)``.

  * ``machine`` — host identity. Defaults to ``socket.gethostname()`` so
    that 1-person / 1-machine users get something meaningful for free.
  * ``agent`` — operator identity. Resolution order:

      1. ``.beacon/agent.json`` ``{"name": "..."}`` (committed-or-local
         override; lets a user say "this machine is `mac-claude`").
      2. ``socket.gethostname()`` fallback (same as ``machine``).

* When a sub-agent is spawned, the parent sets
  ``BEACON_AGENT_PARENT=<parent-name>`` in the child's environment.
  In that case the child's agent name becomes
  ``<parent-name>.dispatch-<n>``, where ``n`` is taken from
  ``BEACON_AGENT_CHILD_ID`` (caller-supplied) or auto-generated from
  the PID. This keeps sub-agent identifiers deterministic without
  any per-spawn configuration.

The module is intentionally side-effect-free at import time. Callers
should invoke :func:`get_actor` (or the lower-level helpers) when they
need the actor; they should *not* cache the result at module load time
because tests and dispatch contexts mutate the environment per-call.

Why a separate module: every other place in commands.py / core.py
already has its own concerns. This file is the one-and-only source of
truth for "who am I, beacon-wise?" so the ms-51 acceptance criteria
(actor consistency in branch claims, commit metadata, and assignees)
holds by construction — no duplicated lookups.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration paths
# ---------------------------------------------------------------------------

# Location of .beacon/agent.json relative to the project root. We deliberately
# resolve against the project root rather than CWD so the same agent identity
# applies regardless of which subdirectory the command is run from.
_AGENT_JSON_RELATIVE = Path(".beacon") / "agent.json"


def _agent_json_path() -> Path:
    """Return the absolute path to .beacon/agent.json under the current CWD.

    We use CWD (not BEACON_PROJECT_FILE's parent) because agent identity is
    per-checkout, not per-project-file. A developer with two clones of the
    same project on the same machine may still want different agent names
    per clone, and that maps naturally to "one agent.json per .beacon dir".
    """
    return Path.cwd() / _AGENT_JSON_RELATIVE


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_machine() -> str:
    """Return the machine identifier.

    Currently always ``socket.gethostname()`` — there is no override mechanism
    on purpose. Hostnames are stable per-machine, human-readable, and free
    (zero setup). If a future MS needs to override (e.g. cloud worker pools
    where hostname is ephemeral), add a ``machine`` field to
    ``.beacon/agent.json`` then.
    """
    return socket.gethostname() or "unknown"


def read_agent_config() -> dict:
    """Return the parsed ``.beacon/agent.json`` dict, or ``{}`` on any error.

    We swallow JSON / IO errors and return ``{}`` instead of raising because
    the file is *optional*. Half of the design value is "user does nothing
    and it still works" — a malformed agent.json should degrade gracefully
    to the hostname fallback, not crash the CLI.
    """
    path = _agent_json_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _base_agent_name() -> str:
    """Resolve the agent name *before* sub-agent suffixing.

    Order: agent.json's ``name`` field → hostname.
    """
    cfg = read_agent_config()
    name = cfg.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return get_machine()


def _child_suffix() -> Optional[str]:
    """Return the suffix to apply for a sub-agent, or ``None`` if not a child.

    The parent dispatcher is expected to set ``BEACON_AGENT_PARENT`` in the
    child's environment. When set:

    * If ``BEACON_AGENT_CHILD_ID`` is also set, that value is used as-is
      (e.g. ``"dispatch-1"``). This lets a dispatcher number its spawns.
    * Otherwise we synthesise ``dispatch-<pid>`` from the current process
      ID. PIDs are deterministic-per-process and avoid an additional
      sequencing layer; the dispatcher can override when it has a better
      sequence.
    """
    parent = os.environ.get("BEACON_AGENT_PARENT", "").strip()
    if not parent:
        return None
    child_id = os.environ.get("BEACON_AGENT_CHILD_ID", "").strip()
    if child_id:
        return child_id
    return f"dispatch-{os.getpid()}"


def get_agent() -> str:
    """Return the agent name, with sub-agent suffixing applied if applicable.

    Examples (per SPEC AC-6)::

        # agent.json says {"name": "mac-claude"}, no parent env
        > get_agent()
        'mac-claude'

        # parent dispatched with BEACON_AGENT_PARENT=mac-claude,
        # BEACON_AGENT_CHILD_ID=dispatch-1
        > get_agent()
        'mac-claude.dispatch-1'

        # no agent.json, parent env unset, hostname = "macbook.local"
        > get_agent()
        'macbook.local'
    """
    suffix = _child_suffix()
    if suffix is not None:
        # When a parent has spawned us, the parent's name takes precedence
        # over our own agent.json. Otherwise sub-agents on the same box
        # would all collapse to the box's agent name.
        parent = os.environ.get("BEACON_AGENT_PARENT", "").strip()
        return f"{parent}.{suffix}"
    return _base_agent_name()


def get_actor() -> dict:
    """Return the full actor identity dict: ``{"machine": ..., "agent": ...}``.

    This is the canonical helper. All callers that need to record "who did
    this" should call this function. Returning a dict (rather than a tuple
    or a packed string) keeps the contract self-describing and makes the
    JSON serialisation of beacon metadata trivial.
    """
    return {"machine": get_machine(), "agent": get_agent()}


__all__ = [
    "get_actor",
    "get_agent",
    "get_machine",
    "read_agent_config",
]
