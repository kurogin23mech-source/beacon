"""SHARED region purity + frontend-drift strict gate (ms-93 e-3203, ms-160 e-5808).

2026-07-10 Codex launch review flagged Web/Tauri frontend drift. This pins:
  1. The SHARED region must not call platform-specific fetch primitives
     (api()/invoke()) — trek scope approve/reject now route through dataSource.
  2. copy-setup-prompt (empty-state install button, wired via addEventListener)
     must be allowlisted so it is not treated as an orphan action.

ms-160 e-5808: the drift guard is now a hard CI stop (--strict, wired in
test.yml), which makes overall drift-green an enforceable invariant — asserted
directly below.

ms-185 (PR#840): the entries this file used to be about are gone. Two Trek
placeholder buttons were allowlisted in INTENTIONAL_NOOP_ACTIONS as
known-deferred debt; the buttons themselves were removed with the Trek screens,
and `check_exemptions_are_live` in the guard now **reddens on an exemption no
front-end emits any more**. So the assertion below is the opposite of what it
was: the names must NOT be in the allowlist. A third-round review caught that
leaving the old assertion here would make the guard and its sibling test print
contradictory instructions ("drop the entry" vs "it should be in the
allowlist") on the same failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-frontend-drift.py"


def _drift_run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO), capture_output=True, text=True,
    )


def _drift_output() -> str:
    proc = _drift_run()
    return proc.stdout + proc.stderr


def test_shared_region_has_no_api_or_invoke_purity_violation():
    out = _drift_output()
    assert "shared/purity" not in out, (
        f"SHARED region leaked a platform fetch primitive (api()/invoke()):\n{out}"
    )


def test_copy_setup_prompt_is_not_flagged_as_orphan():
    out = _drift_output()
    assert "copy-setup-prompt" not in out, (
        f"copy-setup-prompt should be allowlisted (WEB_ONLY + event-listener):\n{out}"
    )


def test_strict_gate_is_clean_on_current_tree():
    """ms-160 e-5808: the same gate CI runs (--strict, exit 1 on drift) must be
    green on the committed tree — otherwise the CI job would red-line every PR."""
    proc = _drift_run("--strict")
    assert proc.returncode == 0, (
        "frontend-drift --strict is not clean; CI would block all merges:\n"
        f"{proc.stdout}{proc.stderr}"
    )


# ms-185 / PR#840: ここに `trek-*-noop` の 2 名が免除リストに無いことを固定する試験が
# 在ったが落とした。同じ性質は `check_exemptions_are_live` が **固有名に依らず** 汎用に
# 赤にし (死んだ免除を足すと報告 / 生きた免除では報告しない)、4 周の独立レビューが毎回
# 実測で確認している。名前固定の側は、別の死んだ免除が足されても無反応で、何も足して
# いなかった。
