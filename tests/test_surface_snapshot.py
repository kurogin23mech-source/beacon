"""ms-119 / e-3987 — AX full-surface audit: a surface-snapshot collector.

AX 原典 wants to audit the whole command surface (help / representative error /
exit code), not only a PR diff — a diff misses defects in code that didn't change
this PR. Before this, `beacon review context --mode full-surface` was rejected as
a follow-up. Now it probes each command group with an unknown subcommand and
records the usage/error/exit surface (a silent no-op = exit 0 with no guidance is
exactly the AX defect the snapshot exposes).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import review_spine  # noqa: E402
import commands  # noqa: E402


# --- pure artifact shaping ---

def test_surface_snapshot_artifact_shape():
    probes = [{"cmd": "task", "exit_code": 0, "silent_no_op": True}]
    a = review_spine.surface_snapshot_artifact(probes, target_ref="full-surface")
    assert a["kind"] == "surface-snapshot"
    assert a["probes"] == probes
    assert a["ref"] == "full-surface"


def test_assemble_uses_surface_artifact_in_full_surface_mode():
    a = review_spine.surface_snapshot_artifact([{"cmd": "doc"}], target_ref="surface")
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX, origin_id="x", origin_content="y",
        diff_text="", mode="full-surface", target_ref="surface",
        known_judge_types={"ax"}, artifact=a,
    )
    assert b["mode"] == "full-surface"
    assert b["artifact"]["kind"] == "surface-snapshot"  # not "diff"


def test_diff_mode_still_defaults_to_diff_artifact():
    b = review_spine.assemble_review_context(
        review_spine.REVIEW_AX, origin_id="x", origin_content="y",
        diff_text="+z", mode="diff", target_ref="PR #1",
        known_judge_types={"ax"},
    )
    assert b["artifact"]["kind"] == "diff"
    assert b["artifact"]["content"] == "+z"


# --- the real collector (bounded to one command group so it's fast) ---

def test_collector_probes_command_surface_and_flags_silent_no_op():
    probes = commands._collect_surface_snapshot(commands_list=["target"])
    assert len(probes) == 1
    p = probes[0]
    assert p["cmd"] == "target"
    assert p["probe_argv"].endswith("__ax_surface_probe__")
    # a real probe records exit code + whether it was a silent no-op
    assert "exit_code" in p
    assert "silent_no_op" in p


def test_collector_empty_list_is_empty():
    assert commands._collect_surface_snapshot(commands_list=[]) == []


# --- CLI: full-surface is accepted and yields a surface-snapshot bundle ---
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMANDS = os.path.join(REPO, "lib", "commands.py")


def test_cli_full_surface_no_longer_rejected():
    env = dict(os.environ)
    env.update({"BEACON_REVIEW_TYPE": "ax", "BEACON_MODE": "full-surface"})
    p = subprocess.run([sys.executable, COMMANDS, "review_context"],
                       capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    assert p.returncode == 0, p.stderr
    bundle = json.loads(p.stdout)
    assert bundle["mode"] == "full-surface"
    assert bundle["artifact"]["kind"] == "surface-snapshot"
    assert len(bundle["artifact"]["probes"]) > 0
