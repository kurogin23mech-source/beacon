"""Tests for the MS status write gate (ms-81 e-1916).

Pins the warning-based forcing function: writes to a milestone in
todo / waiting / done / cancelled status surface a stderr warning and,
in non-interactive runs (the path that matters for the Skill / hook /
dispatch surface), proceed after logging the warning rather than block.

The interactive [y/N] path is sketched but tested only through the
``BEACON_BYPASS_STATUS_GATE`` opt-out — driving an actual ``input()``
inside a unit test would require either a tty fixture or monkeypatching
``sys.stdin.isatty`` + ``builtins.input``, which is overkill for pinning
the warning shape.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands


def _make_ms(status):
    return {"id": "ms-X", "title": "Sample", "status": status}


class TestStatusGateAuthorised:
    @pytest.mark.parametrize(
        "status", ["in_progress", "active", "observing"]
    )
    def test_authorised_status_passes_silently(self, status, capsys):
        ms = _make_ms(status)
        assert commands._check_ms_status_for_write(ms, "test op") is True
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""


class TestStatusGateUnauthorised:
    @pytest.mark.parametrize(
        "status", ["todo", "waiting", "done", "cancelled"]
    )
    def test_unauthorised_warns_and_proceeds_non_interactive(
        self, status, capsys, monkeypatch
    ):
        # Force the "no tty" branch — this is the path actually taken when
        # /beacon-log / Skill / hook call into the CLI.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        ms = _make_ms(status)
        assert commands._check_ms_status_for_write(ms, "log commit abc1234") is True
        err = capsys.readouterr().err
        assert "[ms-81 status gate]" in err
        assert status in err
        assert "log commit abc1234" in err
        assert "non-interactive stdin" in err

    @pytest.mark.parametrize(
        "status", ["todo", "waiting", "done", "cancelled"]
    )
    def test_bypass_env_skips_warning(self, status, monkeypatch, capsys):
        monkeypatch.setenv("BEACON_BYPASS_STATUS_GATE", "1")
        ms = _make_ms(status)
        assert commands._check_ms_status_for_write(ms, "op") is True
        assert capsys.readouterr().err == ""

    def test_warning_includes_remediation_hints(self, capsys, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        ms = _make_ms("done")
        commands._check_ms_status_for_write(ms, "task done e-999")
        err = capsys.readouterr().err
        assert "beacon milestone start ms-X" in err
        assert "beacon milestone observe ms-X" in err
        assert "BEACON_BYPASS_STATUS_GATE=1" in err


class TestStatusGateInteractive:
    def test_interactive_yes_proceeds(self, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        ms = _make_ms("done")
        assert commands._check_ms_status_for_write(ms, "op") is True

    def test_interactive_no_declines(self, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        ms = _make_ms("done")
        assert commands._check_ms_status_for_write(ms, "op") is False

    def test_interactive_eof_declines(self, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

        def _raise_eof(_prompt):
            raise EOFError()

        monkeypatch.setattr("builtins.input", _raise_eof)
        ms = _make_ms("done")
        assert commands._check_ms_status_for_write(ms, "op") is False
