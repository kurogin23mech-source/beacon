#!/usr/bin/env python3
"""beacon-untrusted-turn-vet-hook — the ms-169 e-6280 "one approval per context".

A PostToolUse hook, twin of the PreToolUse gate (beacon-untrusted-tool-gate.py).
It upgrades the A gate from「毎ツール確認」to「リスク文脈ごとの1回承認」: once a
human has approved a side-effect *while the turn was armed with untrusted DM
content*, this session is marked ``vetted`` and stops re-gating for the rest of
the session.

The fire condition (all three must hold):
  * ``tool_effect.is_side_effect`` — the tool that just ran changes state / sends
    outward (read-only completions are ignored), AND
  * ``untrusted_turn.is_armed`` — the session was in an UNVETTED untrusted turn
    *at completion time*, AND
  * that armed state carries an ``asked_at`` marker — the PreToolUse gate actually
    emitted an「ask」for a side-effect this turn (stamped by ``record_asked``).

Why all three: a side-effect that ran while NOT armed is ordinary work, and
vetting it would wrongly suppress the gate for a *later* real injection. And a
side-effect that ran while armed but WITHOUT a real ask — the gate hook
fail-safe-returned, timed out, wasn't installed, or the harness runs an
auto-accept permission mode — is NOT proof of human approval either. Requiring
the ``asked_at`` marker turns "the human approved" from a cross-process timing
assumption into a measured fact (independent AX + maintainability review
consensus). In an autonomous session no human resolves the「ask」, so the tool
never runs — the vet never happens and the gate stays up.

Fail-safe: any error, unreadable state, or missing lib means "do nothing" (the
session stays in whatever state it was — the gate is unaffected). This hook only
ever RELAXES gating and only after a proven human approval, so a silent no-op is
always the safe direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Shared hook bootstrap lives next to this script (bin/hook_bootstrap.py); reach
# it via THIS file's own directory so there is no lib-search convention to
# duplicate just to import it (ms-169 e-6296). It owns beacon-root discovery,
# stdin parsing, and the lib-import convention for all three hook scripts.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_bootstrap as hb  # noqa: E402


def main() -> None:
    hook_input = hb.read_hook_input()
    if hook_input is None:
        return

    # Only meaningful on PostToolUse; be lenient if the field is absent.
    event = hook_input.get("hook_event_name", "PostToolUse")
    if event and event != "PostToolUse":
        return

    libs = hb.import_lib("untrusted_turn", "tool_effect")
    if libs is None:
        return  # fail-safe: can't classify → do nothing
    ut, te = libs["untrusted_turn"], libs["tool_effect"]

    try:
        tool_name = hook_input.get("tool_name") or ""
        tool_input = hook_input.get("tool_input")
        if not te.is_side_effect(tool_name, tool_input):
            return  # read-only completion → never vets
        cwd = Path(hook_input.get("cwd") or ".")
        root = hb.find_beacon_root(cwd)
        if root is None:
            return  # not a beacon project
        session_key = ut.resolve_session_key(root, hook_input)
        state = ut.is_armed(root, session_key)
        if state is None:
            return  # side-effect ran while NOT armed → ordinary work, don't vet
        # Require proof the gate actually asked (asked_at). A side-effect that ran
        # while armed but WITHOUT a real「ask」(gate fail-safe / timeout / not
        # installed / auto-accept permission mode) must NOT be read as human
        # approval — else it would silently vet the session and drop the gate for
        # every later DM. This turns the old cross-process timing inference into a
        # measured fact (ms-169 e-6280 review fix, AX + maintainability consensus).
        if not ut.was_asked(state):
            return  # armed side-effect, but the gate never asked → do not vet
        # A side-effect executed while armed AND the gate asked ⇒ the human
        # approved it. Clear THIS untrusted context (per-context, e-6280): the
        # rest of this context passes, but a later new untrusted DM re-arms.
        ut.vet(root, session_key)
    except Exception:
        # Never brick the harness on a hook bug — fail safe (state unchanged).
        return


if __name__ == "__main__":
    main()
