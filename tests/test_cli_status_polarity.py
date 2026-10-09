"""`status` is read-only; state changes use named intent verbs (ms-120 e-3907).

AX 原則 5 (最小驚き) + 原則 1 (命名一貫性): `beacon status` reads, but
`beacon operation status <id> <state>` and `beacon acquisition status <id>
<state>` used to WRITE — the same word with reversed polarity. An AI that
learned "status = show" would silently mutate state. Now every lifecycle move is
a named verb (activate/close, start/observe/done) — the verb names the intent
and illegal transitions have no verb — while `status` stays read-only. The old
status-write form still works (backward compat) but warns and is unadvertised.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        json.dumps({
            "name": "t",
            "milestones": [],
            "acquisitions": [{"id": "acq-1", "title": "x", "status": "todo"}],
            "operations": [{"id": "op-1", "title": "y", "status": "todo"}],
        }),
        encoding="utf-8",
    )
    return tmp_path


def _run(proj, *args):
    return subprocess.run(
        [BASH, str(BIN), *args], cwd=proj, capture_output=True, text=True
    )


def _acq_status(proj):
    data = json.loads((proj / ".beacon" / "project.json").read_text("utf-8"))
    return data["acquisitions"][0]["status"]


@pytest.mark.parametrize("verb,expected,audit", [
    ("start", "in_progress", []),            # 非終端の前進 — 監査エントリは不要
    # ms-132 e-4507: observe 除去 (打ち切りは delete で表す)
    # ms-166 e-6893: done は終端遷移なので監査エントリを運ぶ
    ("done", "done", ["--reason", "施策をやり切った"]),
])
def test_acquisition_intent_verbs_move_state(proj, verb, expected, audit):
    r = _run(proj, "acquisition", verb, "acq-1", *audit)
    assert r.returncode == 0, r.stderr
    assert _acq_status(proj) == expected


def test_acquisition_done_refuses_without_an_audit_entry(proj):
    """ms-166 e-6893: 終端に倒すなら理由か明示的な省略が要る。

    以前 ``acquisition_set_status`` は reason を受ける口すら無く、施策の done は
    構造的に「なぜ終わったか」が空のまま記録されていた。
    """
    r = _run(proj, "acquisition", "done", "acq-1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "requires an audit entry" in r.stderr
    assert _acq_status(proj) != "done", "拒否したのに状態を書き換えてはいけない"


def test_acquisition_status_write_is_deprecated_but_works(proj):
    r = _run(proj, "acquisition", "status", "acq-1", "in_progress")
    assert r.returncode == 0
    assert "deprecated" in r.stderr.lower()
    assert "start|done" in r.stderr
    assert _acq_status(proj) == "in_progress"  # still performs the write (compat)


def test_operation_status_write_is_deprecated_but_works(proj):
    r = _run(proj, "operation", "status", "op-1", "in_progress")
    assert "deprecated" in r.stderr.lower()
    assert "activate|close" in r.stderr


def test_bare_status_is_still_read_only(proj):
    # `beacon status` must not mutate anything and must succeed as a read.
    before = (proj / ".beacon" / "project.json").read_text("utf-8")
    r = _run(proj, "status")
    assert r.returncode == 0
    after = (proj / ".beacon" / "project.json").read_text("utf-8")
    assert before == after  # a read changed nothing


def test_acquisition_help_advertises_verbs_not_status_write(proj):
    r = _run(proj, "acquisition", "--help")
    assert r.returncode == 0
    for verb in ("acquisition start", "acquisition done"):  # ms-132 e-4507: observe 除去
        assert verb in r.stdout
    # the write-status form is no longer advertised
    assert "acquisition status <acq-id> <status>" not in r.stdout
