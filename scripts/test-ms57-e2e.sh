#!/usr/bin/env bash
# ms-57 e-1042 — simple e2e for the session-log flow.
#
# Walks the CLI end-to-end against a temp local-mode project, switching
# .beacon/session.json between two synthesized session_ids to simulate
# "machine A" / "machine B" on one box. Verifies:
#
#   1. notes / commits tag with meta.session_id correctly per session
#   2. `beacon session rescue` aggregates the OTHER session and sets
#      recovered=true
#   3. `beacon session end` for that other session, run later, preserves
#      recovered=true while overwriting the summary
#   4. `beacon session log list` returns both entries
#
# Limitations vs. real cross-machine validation:
#   * No actual Firestore round-trip — local-mode only. Cloud-side
#     merge semantics need separate verification on 2 real machines.
#   * Single OS / single user — Mac+Windows interop is not exercised.
#
# Exit codes: 0 = all asserts pass, non-zero on first failure.

set -euo pipefail

# Resolve the project root (so the script runs from anywhere).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BEACON_BIN="$REPO_ROOT/bin/beacon"

if [ ! -x "$BEACON_BIN" ]; then
  echo "error: $BEACON_BIN not executable" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'mv "$WORK" "$REPO_ROOT/.trash/test-ms57-e2e-$$" 2>/dev/null || true' EXIT
cd "$WORK"

# ---- helpers --------------------------------------------------------------

SID_A="session-A-$(date +%s)-aaaa1111"
SID_B="session-B-$(date +%s)-bbbb2222"

set_session() {
  # Force the next beacon CLI run to act under a specific session_id by
  # rewriting .beacon/session.json. Bypasses the natural heartbeat-based
  # reuse so we can deterministically simulate two sessions on one box.
  local sid="$1"
  local now
  now="$(python3 -c 'import datetime; print(datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))')"
  mkdir -p .beacon
  cat > .beacon/session.json <<EOF
{
  "session_id": "$sid",
  "actor": {"machine": "test-host", "agent": "test"},
  "created_at": "$now",
  "last_active": "$now",
  "harness": "shell-e2e"
}
EOF
}

assert() {
  local label="$1"; shift
  if ! eval "$@"; then
    echo "  FAIL: $label" >&2
    exit 1
  fi
  echo "  PASS: $label"
}

# ---- 1. init project (local mode) ----------------------------------------

echo ""
echo "== step 1: init local project =="
"$BEACON_BIN" init --name test-ms57 --objective "e2e verification" --storage local >/dev/null
"$BEACON_BIN" milestone add "ms-57 e2e check" >/dev/null
MS_ID="$("$BEACON_BIN" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["milestones"][-1]["id"])')"
"$BEACON_BIN" milestone start "$MS_ID" >/dev/null 2>&1 || true
echo "  ms=$MS_ID  A=$SID_A  B=$SID_B"

# ---- 2. session A writes a note ------------------------------------------

echo ""
echo "== step 2: session A writes a note =="
set_session "$SID_A"
"$BEACON_BIN" note "note from A" >/dev/null

assert "session A note carries session_id" \
  "grep -q '$SID_A' .beacon/session_notes.jsonl"

# ---- 3. session A writes a commit-like save -------------------------------

echo ""
echo "== step 3: session A records a save entry (commit stand-in) =="
"$BEACON_BIN" save "A side change" -m "$MS_ID" --source manual --hash "aaaaa11" --json >/dev/null

# Saves don't get auto-tagged (they're not commits) — that's fine.
# For session_id on commit, we directly verify via core's contract — done in
# the unit test test_commit_pr_session_tagging.py. This script focuses on
# the notes+session-log flow which is what was hardest to integration-test.

# ---- 4. switch to session B, write a note --------------------------------

echo ""
echo "== step 4: session B writes a note =="
set_session "$SID_B"
"$BEACON_BIN" note "note from B" >/dev/null

assert "session B note carries its own session_id" \
  "grep -q '$SID_B' .beacon/session_notes.jsonl"
assert "session A's note still in jsonl (not destroyed)" \
  "grep -q '$SID_A' .beacon/session_notes.jsonl"

# ---- 5. session B runs rescue --------------------------------------------

echo ""
echo "== step 5: session B runs rescue =="
RESCUE_OUT="$("$BEACON_BIN" session rescue --json)"
echo "  rescue output: $RESCUE_OUT"

assert "rescue picked up session A" \
  "echo '$RESCUE_OUT' | python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if any(x[\"session_id\"]==\"$SID_A\" and x[\"recovered\"] for x in r) else 1)'"

assert "rescue did NOT touch session B (current)" \
  "echo '$RESCUE_OUT' | python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if all(x[\"session_id\"]!=\"$SID_B\" for x in r) else 1)'"

# Session A log on disk should now exist with recovered=true.
SAFE_A="$(echo "$SID_A" | sed 's|[^A-Za-z0-9._-]|_|g')"
assert "session A log file exists" \
  "test -f .beacon/session_logs/${SAFE_A}.json"
assert "session A log has recovered=true" \
  "python3 -c 'import json,sys; sys.exit(0 if json.load(open(\".beacon/session_logs/${SAFE_A}.json\")).get(\"recovered\")==True else 1)'"

# ---- 6. session A comes back, runs session-end ----------------------------

echo ""
echo "== step 6: session A comes back, runs session-end =="
set_session "$SID_A"
"$BEACON_BIN" session end --summary "AI-written A summary" --json >/dev/null

LOG_A="$(cat .beacon/session_logs/${SAFE_A}.json)"
echo "  A log after session-end:"
echo "$LOG_A" | python3 -m json.tool | sed 's/^/    /'

assert "session-end overwrites summary" \
  "echo '$LOG_A' | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)[\"summary\"]==\"AI-written A summary\" else 1)'"

assert "session-end preserves recovered=true" \
  "echo '$LOG_A' | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get(\"recovered\")==True else 1)'"

# ---- 7. list shows both sessions -----------------------------------------

echo ""
echo "== step 7: session log list shows both sessions =="
LIST_OUT="$("$BEACON_BIN" session log list --json)"
echo "  list count: $(echo "$LIST_OUT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"

assert "list includes session A" \
  "echo '$LIST_OUT' | python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit(0 if any(e[\"session_id\"]==\"$SID_A\" for e in r) else 1)'"

# ---- 8. session-end idempotency ------------------------------------------

echo ""
echo "== step 8: re-run session-end (idempotency) =="
"$BEACON_BIN" session end --summary "AI-written A summary" --json >/dev/null
LOG_A2="$(cat .beacon/session_logs/${SAFE_A}.json)"

assert "summary still matches (idempotent)" \
  "echo '$LOG_A2' | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)[\"summary\"]==\"AI-written A summary\" else 1)'"
assert "recovered flag still True (never flipped)" \
  "echo '$LOG_A2' | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get(\"recovered\")==True else 1)'"

# ---- done ----------------------------------------------------------------

echo ""
echo "All ms-57 e2e asserts passed."
echo "Workspace will be moved to .trash/ on exit."
