"""Frontmatter parser / writer regression tests.

PE dogfood 2026-06-10 incident:

* Bug 1 (`--content -` literal): `beacon doc update --content -` replaced a
  130-line SPEC with the single character ``-``. Verified here via
  ``_resolve_content_input``.

* Bug 2 (block-list with colons in values): YAML frontmatter of the form ::

      approved_actions:
        - "op:op-2:check_run"
        - "op:op-2:run_record"

  was destroyed by the line-based parser, which split each ``- "op:...`` line
  on its first colon. Verified here via ``_parse_frontmatter`` /
  ``_add_frontmatter`` round-trip.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


# -----------------------------------------------------------------------------
# Bug 1: --content "-" must read stdin, never write the literal dash
# -----------------------------------------------------------------------------


def test_resolve_content_dash_reads_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello from stdin\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert commands._resolve_content_input("-") == "hello from stdin\n"


def test_resolve_content_dash_with_tty_rejects(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        commands._resolve_content_input("-")
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "stdin" in err


def test_resolve_content_empty_with_pipe_reads_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped body\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert commands._resolve_content_input("") == "piped body\n"


def test_resolve_content_literal_passthrough(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert commands._resolve_content_input("plain text body") == "plain text body"


# -----------------------------------------------------------------------------
# Bug 2: block-list values containing colons must survive round-trip
# -----------------------------------------------------------------------------


BLOCK_LIST_FRONTMATTER = (
    "---\n"
    "scope: spec\n"
    "operation: op-2\n"
    "approved_actions:\n"
    '  - "op:op-2:check_run"\n'
    '  - "op:op-2:run_record"\n'
    '  - "op:op-2:incident_open"\n'
    "---\n"
    "\n"
    "# SPEC body\n"
)


def test_parse_block_list_with_colons_preserves_items():
    meta, body = commands._parse_frontmatter(BLOCK_LIST_FRONTMATTER)
    assert meta["scope"] == "spec"
    assert meta["operation"] == "op-2"
    assert meta["approved_actions"] == [
        "op:op-2:check_run",
        "op:op-2:run_record",
        "op:op-2:incident_open",
    ]
    assert body.startswith("# SPEC body")


def test_roundtrip_normalises_block_list_to_inline():
    """The first round-trip rewrites the block-list as an inline JSON array.

    Inline form survives line-based re-parsing; block form does not without
    block-list lookahead. Normalising on write keeps every later round-trip
    lossless even if a downstream tool re-reads with a simpler parser.
    """
    rewritten = commands._add_frontmatter(BLOCK_LIST_FRONTMATTER, scope="spec")
    assert (
        'approved_actions: ["op:op-2:check_run", "op:op-2:run_record", '
        '"op:op-2:incident_open"]' in rewritten
    )

    # Re-parsing the rewritten text must yield the same list (lossless).
    meta2, _ = commands._parse_frontmatter(rewritten)
    assert meta2["approved_actions"] == [
        "op:op-2:check_run",
        "op:op-2:run_record",
        "op:op-2:incident_open",
    ]


def test_inline_jsonish_value_parsed_as_list():
    """Inline ``[a, b]`` form parses to ``list[str]`` for parity with block form.

    This makes round-trip idempotent regardless of which YAML shape the author
    used originally.
    """
    text = (
        "---\n"
        "scope: spec\n"
        'approved_actions: ["a:b:c", "d:e"]\n'
        "---\n"
        "body\n"
    )
    meta, _ = commands._parse_frontmatter(text)
    assert meta["approved_actions"] == ["a:b:c", "d:e"]


def test_plain_scalar_frontmatter_unchanged():
    text = "---\nscope: memo\nmilestone: ms-55\n---\nbody\n"
    meta, body = commands._parse_frontmatter(text)
    assert meta == {"scope": "memo", "milestone": "ms-55"}
    assert body == "body\n"

    rewritten = commands._add_frontmatter(text, scope="memo", milestone="ms-55")
    meta2, body2 = commands._parse_frontmatter(rewritten)
    assert meta2 == {"scope": "memo", "milestone": "ms-55"}
    assert body2 == "body\n"


def test_block_list_with_unquoted_items():
    text = (
        "---\n"
        "scope: spec\n"
        "approved_actions:\n"
        "  - extract:profile:*\n"
        "  - task done:e-*\n"
        "---\n"
        "body\n"
    )
    meta, _ = commands._parse_frontmatter(text)
    assert meta["approved_actions"] == ["extract:profile:*", "task done:e-*"]
