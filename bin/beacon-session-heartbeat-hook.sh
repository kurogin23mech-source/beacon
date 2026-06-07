#!/bin/bash
# PostToolUse hook: refresh beacon session heartbeat after any Bash invocation.
#
# Problem (ms-54 e-1189):
#   `beacon session id` (which calls session.update_last_active() and bumps
#   last_active in .beacon/session.json + cloud sessions/ subcollection) is
#   only invoked once per session via the /beacon-session-start Skill's
#   Step 0b. Long-running Claude Code sessions that haven't run an explicit
#   beacon command drift out of the bus directory because their last_active
#   stays stale, so `beacon bus directory --since-min 30 --live` drops them
#   even though the session is alive.
#
# Solution (design choice A — minimal, fire-and-forget):
#   On every PostToolUse Bash event we kick `beacon session id` in the
#   background. Since lib/commands.py already heartbeats from the main
#   dispatcher (ms-57 e-1035), any beacon CLI use auto-refreshes; this hook
#   covers the "AI is alive but only running git / ls / etc." case.
#
#   - fire-and-forget: backgrounded so it never blocks the tool result.
#   - fail-silent: stdout/stderr discarded; non-zero exits do not surface.
#   - recursion-safe: if the Bash command itself was `beacon session id`
#     (or any other `beacon` invocation) the dispatcher has already done
#     the heartbeat, so we skip to avoid noisy double-fires.
#   - project-scoped: walks up from the tool's cwd to find the nearest
#     .beacon/project.json. Without one we exit silently.
#
# Acceptance criteria coverage (e-1189):
#   (1) heartbeat fires periodically while session is active (= every tool
#       call, which for a working AI is well under the 30 min directory
#       window).
#   (2) mechanism: PostToolUse Bash matcher (no daemon, no orphan risk).
#   (3) verified by tests/test_session_heartbeat_hook.py.
#   (4) cleanup: nothing to clean up — no persistent process.
#   (5) existing tests untouched (separate script + separate hook entry).

INPUT=$(cat /dev/stdin)

# Malformed stdin -> silent exit. Match the conservative posture of
# beacon-post-commit-hook.sh so a malformed payload never breaks the chain.
if ! printf '%s' "$INPUT" | jq -e . >/dev/null 2>&1; then
  exit 0
fi

CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')
TOOL_CWD=$(echo "$INPUT" | jq -r '.tool_input.cwd // empty')

# Recursion guard. The main beacon dispatcher already bumps the heartbeat
# (lib/commands.py ms-57 e-1035), so any `beacon <subcmd>` invocation has
# already done what this hook would do. Skipping avoids:
#   - duplicate cloud writes within the debounce window
#   - confusing hook-debug traces ("session id" firing on its own command)
# Use word-boundary-ish matching so e.g. `git log "beacon"` (commit message
# containing the word) does not accidentally trigger the skip.
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]/])beacon([[:space:]]|$)'; then
  exit 0
fi

# Resolve which directory the tool actually ran in.
if [ -n "$TOOL_CWD" ]; then
  TOOL_CWD="${TOOL_CWD/#\~/$HOME}"
  COMMAND_CWD="$TOOL_CWD"
else
  COMMAND_CWD="$(pwd)"
fi

# Walk up from COMMAND_CWD to find the nearest .beacon/project.json. Without
# one there is no session to refresh.
find_beacon_root() {
  local dir="$1"
  while [ "$dir" != "/" ] && [ -n "$dir" ]; do
    if [ -f "$dir/.beacon/project.json" ]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

BEACON_ROOT=$(find_beacon_root "$COMMAND_CWD")
if [ -z "$BEACON_ROOT" ]; then
  exit 0
fi

# Locate a beacon binary. Prefer the one on PATH (matches user shell), fall
# back to the repo's bin/beacon for dev clones / test runs where PATH may
# not include /opt/homebrew/bin.
BEACON_BIN=$(command -v beacon 2>/dev/null)
if [ -z "$BEACON_BIN" ]; then
  if [ -x "$BEACON_ROOT/bin/beacon" ]; then
    BEACON_BIN="$BEACON_ROOT/bin/beacon"
  else
    # No beacon binary reachable. Silent exit — this is the "user has the
    # project checked out but no CLI installed" edge case.
    exit 0
  fi
fi

# Fire-and-forget. Backgrounded subshell, all output discarded, detached
# via `disown` (best-effort — `disown` is bash-builtin and may not exist
# in /bin/sh, hence the shebang is /bin/bash and the trailing redirect is
# the load-bearing piece).
( "$BEACON_BIN" session id >/dev/null 2>&1 ) &
disown 2>/dev/null || true

exit 0
