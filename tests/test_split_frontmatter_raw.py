"""Unit tests for commands._split_frontmatter_raw (ms-131, maintainability review
of PR #544).

The table-doc write-back (_write_table_model) swaps only the body and keeps the
frontmatter verbatim, so the frontmatter/body split must be lossless. The core
invariant: raw + body == content, with the closing ``---`` and any following
blank lines kept on the frontmatter side.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


CASES = [
    "---\nformat: table\n---\n# T\n\nbody\n",   # normal: blank line after ---
    "---\nscope: memo\n---\nbody-no-blank",       # no blank line after ---
    "---\na: 1\n---\n\n\n# T\n",                  # multiple blank lines after ---
    "no frontmatter at all\n",                     # no frontmatter
    "",                                             # empty
    "---\nunterminated frontmatter\n# still body",  # no closing ---
]


def test_roundtrip_is_lossless():
    for content in CASES:
        raw, body = commands._split_frontmatter_raw(content)
        assert raw + body == content, f"lossy split for {content!r}"


def test_no_frontmatter_returns_empty_raw():
    raw, body = commands._split_frontmatter_raw("plain body")
    assert raw == "" and body == "plain body"


def test_frontmatter_kept_on_raw_side():
    raw, body = commands._split_frontmatter_raw("---\nformat: table\n---\n# T\n\nx\n")
    assert raw.startswith("---") and raw.rstrip().endswith("---")
    assert body == "# T\n\nx\n"  # leading blank lines stay on the raw side


def test_blank_lines_after_fence_stay_with_frontmatter():
    raw, body = commands._split_frontmatter_raw("---\na: 1\n---\n\n\n# T\n")
    assert raw.endswith("\n\n\n")
    assert body == "# T\n"


def test_unterminated_frontmatter_is_all_body():
    raw, body = commands._split_frontmatter_raw("---\nno close\nbody")
    assert raw == "" and body == "---\nno close\nbody"
