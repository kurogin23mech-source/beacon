"""Regression tests for orphaned ``beacon trigger check`` processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "channel" / "managed-process.mjs"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group regression")
def test_timeout_kills_descendant_process(tmp_path: Path):
    pid_file = tmp_path / "descendant.pid"
    script = """
      import { runCommandWithTimeout } from %s;
      try {
        await runCommandWithTimeout('/bin/sh', [
          '-c',
          'sleep 30 & echo $! > "$1"; wait',
          'sh',
          %s,
        ], { timeout: 100 });
        process.exit(2);
      } catch (error) {
        process.stdout.write(JSON.stringify({ code: error.code }));
      }
    """ % (json.dumps(RUNNER.as_uri()), json.dumps(str(pid_file)))

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(result.stdout) == {"code": "ETIMEDOUT"}
    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant process {descendant_pid} survived timeout")


def test_scheduler_uses_managed_process_runner():
    source = (ROOT / "channel" / "bus.mjs").read_text(encoding="utf-8")
    assert "runCommandWithTimeout(beaconBin, ['trigger', 'check']" in source
    assert "execSync('beacon trigger check'" not in source


def test_scheduler_backs_off_after_repeated_failures():
    source = (ROOT / "channel" / "bus.mjs").read_text(encoding="utf-8")
    assert "SCHEDULER_MAX_BACKOFF_MS" in source
    assert "schedulerFailureCount += 1" in source
    assert "2 ** schedulerFailureCount" in source
    assert "schedulerFailureCount = 0" in source
