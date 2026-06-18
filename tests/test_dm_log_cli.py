"""Audit-history CLI primitive (ms-70 / e-1923, closing e-1718 AC 4).

These tests pin the **structural** behaviour of ``beacon dm log``:

(a) api_client.list_dm_approval_history calls
    ``GET /api/projects/{pid}/dm/approval/history?limit=N`` — the same
    endpoint the Web UI Settings > Audit table uses (e-1718 commit
    4c5052f). Endpoint shape is contract; if it ever moves, both the
    Web UI and this CLI need to follow in lockstep.

(b) cmd_dm_log renders the 6-column ASCII table when rows are present.
    Columns: event_id / sender / receiver / status / decision_at /
    decision_by. Each row's values appear in the output. The table is
    bookended by a separator line (= same skeleton as `beacon bus
    status`).

(c) cmd_dm_log emits raw JSON when ``BEACON_JSON=1`` is set. The shape
    matches the rows returned from the api_client (= passthrough).

(d) Empty-state: zero rows surfaces a localized line ("承認履歴はまだ
    ありません") instead of an empty table. Matches the Web UI's empty
    copy so the audit narrative reads the same across surfaces.

(e) Defensive refusals: server 403 (= caller is not a project member)
    and 404 (= project_id not found) become single-line human errors,
    not Python stacktraces. Exit code 1 in both cases.

(f) ``--limit`` is forwarded as an int. Non-integer input triggers
    exit 2 with a usage banner.

The api_client path test stubs the HTTP layer so no real network call
runs. The cmd_dm_log paths stub the api_client + project resolver so
the dispatcher is exercised end-to-end without HTTP roundtrips.
"""

from __future__ import annotations

import json
import os
import sys
from typing import List

import pytest


# ---------------------------------------------------------------------------
# Path setup — mirror tests/test_dm_respond_cli.py so server/ and lib/
# are importable from the suite without site-packages drift.
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REPO_ROOT, "server"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))


PID = "proj-x"
EVT_A = "evt-aaaaaaaa"
EVT_B = "evt-bbbbbbbb"
USER_RECEIVER = "user-bob"
USER_SENDER = "user-alice"


# ===========================================================================
# (a) api_client endpoint shape (= contract pin with the Web UI).
# ===========================================================================


def _make_stub_client_capture(rows):
    """Return an ApiClient with `_request` replaced by a recorder.

    The recorder appends (method, path, body) to ``calls`` and returns
    the supplied ``rows`` on the GET path so we can assert URL shape
    without standing up a real HTTP server.
    """
    sys.modules.pop("api_client", None)
    import api_client as ac

    client = ac.ApiClient("https://example.test", token="stub-tok")
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        return rows

    client._request = fake_request  # type: ignore[assignment]
    return client, calls


def test_list_dm_approval_history_hits_canonical_endpoint():
    """``GET /api/projects/{pid}/dm/approval/history?limit=N`` with default 50."""
    client, calls = _make_stub_client_capture(rows=[])
    out = client.list_dm_approval_history(PID, limit=50)

    assert out == []
    assert calls == [
        ("GET", f"/api/projects/{PID}/dm/approval/history?limit=50", None),
    ]


def test_list_dm_approval_history_forwards_custom_limit():
    """Custom ``limit=N`` arrives as a query-string int (server caps at 500)."""
    client, calls = _make_stub_client_capture(rows=[])
    client.list_dm_approval_history(PID, limit=200)

    assert calls == [
        ("GET", f"/api/projects/{PID}/dm/approval/history?limit=200", None),
    ]


def test_list_dm_approval_history_url_escapes_project_id():
    """Project IDs with shell-unsafe chars must be URL-encoded in the path."""
    client, calls = _make_stub_client_capture(rows=[])
    client.list_dm_approval_history("proj/with spaces&", limit=10)

    assert len(calls) == 1
    method, path, _ = calls[0]
    assert method == "GET"
    # urllib.parse.quote turns `/` into `%2F`, space into `%20`, `&` into `%26`.
    assert "/api/projects/proj%2Fwith%20spaces%26/dm/approval/history?limit=10" == path


# ===========================================================================
# CLI argv-parsing tests — cmd_dm_log + api_client stub.
# ===========================================================================


class _StubApiClient:
    """Records every call and returns canned rows or raises on demand."""
    def __init__(self, rows=None, raise_msg=None):
        self.calls: List[tuple] = []
        self._rows = rows or []
        self._raise_msg = raise_msg

    def list_dm_approval_history(self, project_id, *, limit=50):
        self.calls.append((project_id, limit))
        if self._raise_msg:
            raise RuntimeError(self._raise_msg)
        return self._rows


@pytest.fixture
def stub_commands(monkeypatch):
    """Load lib/commands.py with a stubbed _get_api_client + project resolver."""
    sys.modules.pop("commands", None)
    import commands as cm

    # Default resolver returns PID; tests that need the override path can
    # re-set this with a different value to verify --project plumbing.
    monkeypatch.setattr(cm, "_resolve_bus_project_id", lambda config: PID)

    state = {"client": None}

    def fake_get_api_client():
        return state["client"], {"project_id": PID}

    monkeypatch.setattr(cm, "_get_api_client", fake_get_api_client)
    yield cm, state
    sys.modules.pop("commands", None)


def _set_log_env(monkeypatch, *, project_id="", limit="", json_mode="",
                 project_override=""):
    monkeypatch.setenv("BEACON_DM_LOG_PROJECT_ID", project_id)
    monkeypatch.setenv("BEACON_DM_LOG_LIMIT", limit)
    monkeypatch.setenv("BEACON_JSON", json_mode)
    monkeypatch.setenv("BEACON_BUS_PROJECT_ID", project_override)


def _approved_row(*, event_id=EVT_A, decision_by=USER_RECEIVER,
                  approval_status="approved",
                  decision_at="2026-06-17T10:00:00.000000Z"):
    return {
        "event_id": event_id,
        "approval_status": approval_status,
        "decision_by": decision_by,
        "decision_at": decision_at,
        "sender_user_id": USER_SENDER,
        "receiver_user_id": USER_RECEIVER,
        "created_at": "2026-06-17T09:00:00.000000Z",
    }


# ---------------------------------------------------------------------------
# (b) Row-rendering path: 6 columns, each row's values present in output.
# ---------------------------------------------------------------------------


def test_cli_renders_six_column_table_for_rows(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    rows = [
        _approved_row(event_id=EVT_A, approval_status="approved"),
        _approved_row(event_id=EVT_B, approval_status="denied",
                      decision_at="2026-06-17T11:00:00.000000Z"),
    ]
    state["client"] = _StubApiClient(rows=rows)
    _set_log_env(monkeypatch)

    cm.cmd_dm_log()

    out = capsys.readouterr().out
    # Header columns appear (order: event_id / sender / receiver / status /
    # decision_at / decision_by — matches AC 2).
    assert "event_id" in out
    assert "sender" in out
    assert "receiver" in out
    assert "status" in out
    assert "decision_at" in out
    assert "decision_by" in out
    # Each row's distinguishing values appear in the rendered table.
    assert EVT_A in out
    assert EVT_B in out
    assert "approved" in out
    assert "denied" in out
    assert USER_SENDER in out
    assert USER_RECEIVER in out
    # Row-count footer surfaces so the user can confirm completeness.
    assert "2 row(s)" in out
    # api_client was called with the resolved project_id + default limit.
    assert state["client"].calls == [(PID, 50)]


# ---------------------------------------------------------------------------
# (c) JSON mode: raw passthrough.
# ---------------------------------------------------------------------------


def test_cli_json_mode_emits_raw_rows(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    rows = [_approved_row(), _approved_row(event_id=EVT_B,
                                            approval_status="denied")]
    state["client"] = _StubApiClient(rows=rows)
    _set_log_env(monkeypatch, json_mode="1")

    cm.cmd_dm_log()

    out = capsys.readouterr().out.strip()
    # Single JSON document containing the rows list as-is.
    decoded = json.loads(out)
    assert decoded == rows


def test_cli_json_mode_emits_empty_array_when_no_rows(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient(rows=[])
    _set_log_env(monkeypatch, json_mode="1")

    cm.cmd_dm_log()

    out = capsys.readouterr().out.strip()
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# (d) Empty-state: human surface matches the Web UI copy.
# ---------------------------------------------------------------------------


def test_cli_empty_history_prints_localized_empty_state(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient(rows=[])
    _set_log_env(monkeypatch)

    cm.cmd_dm_log()

    out = capsys.readouterr().out
    # Same Japanese empty-copy the Web UI's renderDmApprovalHistoryRows uses
    # so the audit narrative reads the same across both surfaces.
    assert "承認履歴はまだありません" in out
    # No row separator line (= empty-state path uses a single sentence).
    assert "event_id" not in out


# ---------------------------------------------------------------------------
# (e) Defensive refusals: 403 / 404 become single-line human errors.
# ---------------------------------------------------------------------------


def test_cli_surfaces_403_as_clean_error(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(
        raise_msg="API error 403: not a member of project",
    )
    _set_log_env(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_log()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "membership-gated" in err or "not a member" in err
    # Importantly, no Python stacktrace bleed.
    assert "Traceback" not in err


def test_cli_surfaces_404_as_clean_error(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(
        raise_msg="API error 404: project not found",
    )
    _set_log_env(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_log()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# (f) --limit forwarding + bad-input handling.
# ---------------------------------------------------------------------------


def test_cli_forwards_custom_limit_to_api_client(
    monkeypatch, stub_commands, capsys
):
    cm, state = stub_commands
    state["client"] = _StubApiClient(rows=[])
    _set_log_env(monkeypatch, limit="150")

    cm.cmd_dm_log()

    assert state["client"].calls == [(PID, 150)]


def test_cli_bad_limit_exits_with_usage(monkeypatch, stub_commands, capsys):
    cm, state = stub_commands
    state["client"] = _StubApiClient(rows=[])
    _set_log_env(monkeypatch, limit="not-an-int")

    with pytest.raises(SystemExit) as exc:
        cm.cmd_dm_log()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--limit must be an integer" in err
    # api_client must not have been touched on a usage error.
    assert state["client"].calls == []


def test_cli_zero_or_negative_limit_falls_back_to_default(
    monkeypatch, stub_commands, capsys
):
    """Defensive: a 0 / negative limit slips through int() but the
    handler still calls the server with the safe default (50) rather
    than risking an unbounded scan."""
    cm, state = stub_commands
    state["client"] = _StubApiClient(rows=[])
    _set_log_env(monkeypatch, limit="0")

    cm.cmd_dm_log()

    assert state["client"].calls == [(PID, 50)]
