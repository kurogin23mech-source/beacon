#!/bin/bash
# PostToolUse / UserPromptSubmit hook — record a tool's use when the LEDGER says to.
#
# ms-180 e-7001: this file used to carry a hand-written `case` statement naming two
# Google-Drive MCP servers, and `exit 0` for every other tool — so "using a tool
# leaves a record" held for two tools and silently failed for all the rest. The
# decision now comes from the tool ledger (.beacon/tools.json), consulted in ONE
# place (tool_ledger.record_plan). There is deliberately no tool name in this file
# any more: adding a tool to the record set is `beacon tool add`, not an edit here.
#
# The path stays beacon-save-hook.sh because installed settings.json files point at
# it. It is now a shim: all behaviour lives in beacon-tool-record-hook.py, next to
# the other Python hooks that share bin/hook_bootstrap.py.
exec python3 "$(dirname "$0")/beacon-tool-record-hook.py" "$@"
