# shellcheck shell=bash
# beacon CLI — master family (1 function)
# ms-127 e-4867: sourced by bin/beacon (noun-family god-module split).
#
# SOURCE-ONLY — do NOT execute directly; bin/beacon `source`s this file.
# No shebang on purpose: this is an include, not a standalone program.
# Pure function definitions only — no top-level execution.
#
# requires-fn: ensure_project
# requires-var: COMMANDS_PY
#   Defined in bin/beacon (the dispatcher) before this file is sourced;
#   bash resolves them at call time (late binding). Verified by
#   scripts/check-cli-help-drift.py (collect_requires_drift).

# ms-111 e-4399: master-sync outbox を CLI 操作を待たず定期経路から drain する。
# `beacon master-sync drain` を session-start helper 等の操作非依存経路から呼び、
# rename 後に別操作が無くても未配送の master-sync を再発行して at-least-once に寄せる。
cmd_master_sync() {
    ensure_project
    case "${1:-}" in
        drain) python3 "$COMMANDS_PY" master_sync_drain ;;
        *)     echo "Usage: beacon master-sync drain" ;;
    esac
}
