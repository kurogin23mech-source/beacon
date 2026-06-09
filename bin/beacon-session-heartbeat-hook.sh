#!/bin/bash
# PostToolUse hook: NO-OP since ms-54 e-1319.
#
# History
# -------
# Originally (ms-54 e-1189) this hook fire-and-forget'd `beacon session id`
# in the background after every Bash tool call, so
# lib/session.update_last_active() would bump .beacon/session.json.last_active
# (and push to the cloud sessions/ subcollection) even on sessions that
# weren't running explicit beacon CLI calls. The goal was to keep long-running
# Claude Code sessions visible in `beacon bus directory --since-min 30 --live`.
#
# Why it's no-op now (e-1319)
# ---------------------------
# Post Option C (PR #111 / commit 78048b6) the bridge's own poll loop is the
# truth source for liveness: every poll iteration stamps both ``last_active``
# (proof the session is alive) and ``last_poll_at`` (proof the session can
# actually receive DMs right now). A parallel CLI write created ambiguity —
# a session could heartbeat ``last_active`` while its bridge poll loop was
# dead, so the directory advertised a session as receive-capable when it
# wasn't, and DMs accumulated server-side unread (dogfood incident that
# motivated this cleanup).
#
# Why the file is kept
# --------------------
# Users may already have this script wired into their PostToolUse hook list
# in ~/.claude/settings.json. Deleting the script would cause every Bash
# tool call to spam a "command not found" warning. Replacing it with an
# explicit silent no-op gives users a smooth migration: existing settings
# keep working, and the next `beacon channel install` (or a manual edit)
# can remove the hook entry entirely.
#
# Reading the hook input still happens (and is discarded) so any harness
# that monitors stdin consumption sees the same shape it always did.
cat /dev/stdin >/dev/null 2>&1
exit 0
