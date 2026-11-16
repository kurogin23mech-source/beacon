"""The structural-removal notice must read the same on both CLI frontends.

ms-185 e-7079. `beacon trek` is gone, and both frontends tell the caller so:
`bin/beacon`'s `trek)` arm echoes the notice line by line, and
`beacon_cli/dispatch.py` holds it as one string in `_REMOVED_VERBS`. The text
was hand-copied between them, and a comment in dispatch.py asserted the two
match — with nothing enforcing it.

An independent maintainability review of PR #827 found that gap and named the
consequence precisely: the next edit to the notice (e-7070 removes
`GET /api/treks/{id}`, which makes the "Still reachable" list false) will be
made on whichever frontend the maintainer happens to have open, and the other
copy will keep advertising a dead endpoint. The comment claiming parity makes
it worse, because a reader trusts it and does not look.

So the claim is pinned here. This is a *text* comparison on purpose: the two
frontends already agree on verbs and flags (check-cli-help-drift.py) and on
flag→env mapping (check-cli-env-parity.py); what no guard compared was the
message body a human or an AI actually reads.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _bash_notice(verb: str) -> str:
    """Reassemble the notice the bash frontend prints for ``verb``."""
    text = (REPO / "bin" / "beacon").read_text(encoding="utf-8")
    arm = re.search(
        r"^    %s\)\n(.*?)^        ;;" % re.escape(verb),
        text, re.S | re.M)
    assert arm, f"no `{verb})` arm in bin/beacon"
    lines = []
    for raw in arm.group(1).splitlines():
        stripped = raw.strip()
        if not stripped.startswith("echo "):
            continue
        body = stripped[len("echo "):].strip()
        if body in ('""', "''"):
            lines.append("")
            continue
        assert body[0] == body[-1] and body[0] in "\"'", (
            f"unhandled echo form in `{verb})`: {raw!r}")
        lines.append(body[1:-1])
    assert lines, f"`{verb})` arm prints nothing"
    return "\n".join(lines)


def _python_notice(verb: str) -> str:
    from beacon_cli.dispatch import _REMOVED_VERBS  # noqa: E402
    assert verb in _REMOVED_VERBS, f"{verb} missing from _REMOVED_VERBS"
    return _REMOVED_VERBS[verb]


def test_trek_removal_notice_is_identical_on_both_frontends():
    bash = _bash_notice("trek")
    python = _python_notice("trek")
    assert bash == python, (
        "the two frontends print different removal notices.\n"
        "--- bin/beacon ---\n" + bash +
        "\n--- beacon_cli/dispatch.py ---\n" + python
    )


def test_the_parity_check_itself_fails_on_drift():
    """Test the test: a one-word change on either side must be detected.

    Without this, a bug in `_bash_notice` (e.g. returning "" because the arm
    shape changed) would make the comparison above pass vacuously — a green
    that means nothing, which this repo has paid for before.
    """
    bash = _bash_notice("trek")
    python = _python_notice("trek")
    assert bash and python, "both notices must be non-empty to compare at all"
    assert bash.replace("removed", "retired") != python, (
        "a changed word still compared equal — the comparison is not looking "
        "at the text")


def test_every_removed_verb_has_both_halves():
    """A verb announced in Python must have a bash arm, and vice versa."""
    from beacon_cli.dispatch import _REMOVED_VERBS  # noqa: E402
    for verb in _REMOVED_VERBS:
        assert _bash_notice(verb), f"{verb}: no bash arm for a removed verb"
