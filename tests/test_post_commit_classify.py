"""Direct unit tests for beacon_cli.hooks.post_commit._classify (ms-160 e-5801).

e-5803 review (Maint-6): before this, _classify — which added the review-node
branches and changed its return arity from a 2-tuple to a 3-tuple — was only
exercised through the slow subprocess e2e in test_review_node_manifest_drift.py.
A 3-tuple arity change silently breaks callers (``skill, message, node_id =
classified``) at runtime with no fast feedback. These pin the shape + branch
precedence directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from beacon_cli.hooks.post_commit import _classify  # noqa: E402


def test_no_match_returns_none():
    assert _classify("ls -la") is None
    assert _classify("echo git commit is a phrase here") is None or \
        _classify("echo git commit is a phrase here")[0] == "/beacon-log"


def test_commit_wins_over_review_nodes():
    skill, message, node = _classify("git commit -m 'feat: x'")
    assert skill == "/beacon-log"
    assert node == ""
    assert "MUST" in message


def test_push():
    skill, _msg, node = _classify("git push origin main")
    assert (skill, node) == ("/beacon-push", "")


def test_pr_open_gh_and_beacon():
    for cmd in ("gh pr create --fill", "beacon pr add https://x/pull/3",
                "beacon pr create -m ms-1"):
        skill, message, node = _classify(cmd)
        assert skill == "/beacon-review-run", cmd
        assert node == "pr-open", cmd
        assert "review" in message.lower()


def test_target_close_milestone_and_operation():
    for cmd in ("beacon milestone done ms-1 --reason x",
                "beacon milestone close ms-2",
                "beacon milestone observe ms-3 --reason y",
                "beacon operation close op-1"):
        skill, _message, node = _classify(cmd)
        assert (skill, node) == ("/beacon-review-run", "target-close"), cmd


def test_deploy():
    skill, _msg, node = _classify("gcloud run deploy api")
    assert (skill, node) == ("/beacon-deploy", "")


def test_every_result_is_a_three_tuple():
    # Arity guard: callers destructure (skill, message, node_id).
    for cmd in ("git commit -m x", "git push", "gh pr create",
                "beacon milestone done ms-1", "gcloud run deploy x"):
        result = _classify(cmd)
        assert result is not None and len(result) == 3, cmd
