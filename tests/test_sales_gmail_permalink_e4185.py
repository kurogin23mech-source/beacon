"""CLI-layer tests for e-4185 — sales_gmail_permalink tells its 3 "cannot build"
causes apart instead of collapsing them to one silent empty exit.

- empty BEACON_SEND_FROM = wiring bug  → exit 1 + stderr (skill stops/surfaces)
- empty Message-ID / non-rfc822 id     → exit 0 + stderr reason + empty stdout
- valid                                → URL on stdout, exit 0

Also: BEACON_RFC822_MSGID is the new name; BEACON_MSGID stays a back-compat alias.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402


def _clear(monkeypatch):
    for k in ("BEACON_SEND_FROM", "BEACON_RFC822_MSGID", "BEACON_MSGID"):
        monkeypatch.delenv(k, raising=False)


def test_empty_from_is_wiring_bug_exit1_with_stderr(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_RFC822_MSGID", "<abc@mail.example>")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_gmail_permalink()
    assert ei.value.code == 1
    out = capsys.readouterr()
    assert out.out.strip() == ""          # no URL on stdout
    assert "Error" in out.err and "$FROM" in out.err  # wiring bug surfaced


def test_empty_message_id_is_legit_nolink_exit0(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_FROM", "me@x.example")
    monkeypatch.setenv("BEACON_RFC822_MSGID", "")
    commands.cmd_sales_gmail_permalink()  # no SystemExit → exit 0
    out = capsys.readouterr()
    assert out.out.strip() == ""          # empty stdout = no --source-url
    assert "no-link" in out.err and "Message-ID が空" in out.err


def test_threadid_is_legit_nolink_exit0_with_recovery_hint(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_FROM", "me@x.example")
    monkeypatch.setenv("BEACON_RFC822_MSGID", "1899abcdef0123")  # thread-id, no '@'
    commands.cmd_sales_gmail_permalink()
    out = capsys.readouterr()
    assert out.out.strip() == ""
    assert "no-link" in out.err and "rfc822 Message-ID" in out.err  # recovery hint


def test_valid_prints_url_exit0(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_FROM", "sales@corp.example")
    monkeypatch.setenv("BEACON_RFC822_MSGID", "<abc.1@mail.example>")
    commands.cmd_sales_gmail_permalink()
    out = capsys.readouterr()
    assert out.out.strip() == ("https://mail.google.com/mail/u/sales@corp.example/"
                               "#search/rfc822msgid%3Aabc.1%40mail.example")


def test_legacy_msgid_alias_still_honoured(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_FROM", "sales@corp.example")
    monkeypatch.setenv("BEACON_MSGID", "<legacy@mail.example>")  # old name only
    commands.cmd_sales_gmail_permalink()
    out = capsys.readouterr()
    assert "rfc822msgid%3Alegacy%40mail.example" in out.out


def test_new_name_wins_over_alias(monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_FROM", "sales@corp.example")
    monkeypatch.setenv("BEACON_RFC822_MSGID", "<new@mail.example>")
    monkeypatch.setenv("BEACON_MSGID", "<old@mail.example>")
    commands.cmd_sales_gmail_permalink()
    out = capsys.readouterr()
    assert "new%40mail.example" in out.out and "old%40mail.example" not in out.out
