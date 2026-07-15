# Shelved: ms-110 PreToolUse DM guard hook (e-3444)

Shelved 2026-07-15 by decision on ms-110 (cross-user DM consent gate).

## Why shelved

ms-110 protects against cross-user DM misfire. The **authoritative** guard is
the server-side backstop (`server/app.py::post_bus_event`, e-3443): the server
rejects a cross-user DM lacking a `recipient_confirmed` claim with 403, on the
one path every client must traverse.

This directory held a **client-side** PreToolUse hook that denied the raw send
primitive before it ran and redirected the AI to `/beacon-dm-send`. It was a
UX nicety (fail earlier, clearer message), not a correctness layer. Wiring it
required editing the user's **global** `~/.claude/settings.json` (affects every
session), which is overkill for a single-user tool. Per the ms-110 design
review we kept the strong single gate (server) and shelved the hook.

## What's here

- `beacon-pretooluse-dm-guard.py` — the PreToolUse hook entrypoint
- `dm_send_guard.py` — the decision core (parse / carve-outs / one-time token)
- `test_dm_send_guard.py` — its unit + subprocess tests (27, all passed)

## Resurrecting (when a real multi-user / multi-client setting arrives)

1. Move `dm_send_guard.py` back to `lib/`, the hook to `bin/`, the test to
   `tests/` (fix the relative `../lib` / `../bin` paths back).
2. Register the hook in `~/.claude/settings.json` under `PreToolUse`:
   ```json
   "PreToolUse": [ { "hooks": [ { "type": "command",
     "command": "/ABS/PATH/bin/beacon-pretooluse-dm-guard.py", "timeout": 10 } ] } ]
   ```
3. The server backstop stays as the correctness guarantee regardless.
