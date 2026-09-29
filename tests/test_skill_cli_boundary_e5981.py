"""Skill → CLI boundary guard, and proof that it actually goes red (ms-160 e-5981).

CORE doc ``architecture-tool-skill-separation`` §2 makes the CLI the single
abstraction boundary (Skill → CLI → local/API). Before e-5981, 21 verbs were
reached by Skills writing ``python3 commands.py <verb>`` with hand-written env
vars, and two of them (``sales_account_remove`` / ``sales_identity_show``) had
gone fully unreachable — no CLI verb, and no Skill calling them either.

These pin BOTH halves:

  * the tree is green now (the debt is actually paid off, not allowlisted);
  * injecting a violation turns it RED — the guard is not a no-op. A loose
    substring check would pass on prose that merely names the anti-pattern, so
    the discriminating cases are asserted explicitly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "_skill_cli_boundary_e5981", ROOT / "scripts" / "check-skill-cli-boundary.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_current_tree_has_no_boundary_jump():
    """Every Skill reaches its capability through the CLI."""
    assert _load().find_violations() == []


def test_allowlist_is_empty():
    """An allowlist is how this debt would quietly come back; keep it at zero.

    If a verb ever genuinely cannot be a CLI verb, the module docstring demands
    a written reason alongside the entry — so this assertion is the place that
    forces that conversation to happen.
    """
    assert _load().ALLOWED == {}


def _skill_tree(tmp_path: Path, body: str, rel: str = "skills/probe.md") -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return tmp_path


def test_guard_goes_red_on_an_injected_violation(tmp_path):
    """test-the-test: a real direct invocation must be reported, or the green
    result above proves nothing."""
    mod = _load()
    root = _skill_tree(
        tmp_path,
        'まず台帳を引く:\n\n```bash\n'
        'BEACON_SEND_LABEL="会社" python3 "$(beacon _lib-path)/commands.py" sales_account_add\n'
        '```\n',
    )
    found = mod.find_violations(root)
    assert [v for _, _, v in found] == ["sales_account_add"]
    assert found[0][0] == "skills/probe.md"


def test_guard_catches_every_skill_copy(tmp_path):
    """The 3 synchronized copies are all in scope — fixing only `skills/` while
    the shipped `shared/` and `plugins/` bundles keep the old call is exactly
    the drift this guards."""
    mod = _load()
    snippet = 'python3 "$(beacon _lib-path)/commands.py" watch_set\n'
    root = tmp_path
    for rel in ("skills/a.md",
                "shared/skills/a/SKILL.md",
                "plugins/beacon/skills/a/SKILL.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(snippet, encoding="utf-8")
    assert len(mod.find_violations(root)) == 3


def test_prose_naming_the_antipattern_does_not_trip_the_guard(tmp_path):
    """The repaired Skills carry a line that NAMES the forbidden form, using the
    literal placeholder `<cmd>`. A substring check would flag that line and make
    the guard unusable, so the extraction must require a real verb token."""
    mod = _load()
    root = _skill_tree(
        tmp_path,
        'Beacon への記録・参照はすべて `beacon <名詞> <動詞>` の CLI を通す。\n'
        '`python3 "$(beacon _lib-path)/commands.py" <cmd>` の直叩きは使わない。\n'
        '実装の在り処は `lib/commands.py` の `cmd_<verb>` ハンドラ。\n',
    )
    assert mod.find_violations(root) == []


def test_the_two_verbs_that_had_gone_unreachable_are_on_the_cli():
    """`sales_account_remove` / `sales_identity_show` were reachable by nothing.

    Assert the repair at the surface a user actually types, on BOTH frontends —
    a verb present in only one of them is the Windows drift ms-44 e-1171 froze.
    """
    bash = (ROOT / "bin" / "beacon").read_text()
    bash += (ROOT / "bin" / "lib" / "cmd_sales.sh").read_text()
    assert "sales_account_remove" in bash
    assert "sales_identity_show" in bash
    assert "send-account) shift 2; cmd_sales_send_account" in bash

    dispatch = (ROOT / "beacon_cli" / "dispatch.py").read_text()
    assert "sales_account_remove" in dispatch
    assert "sales_identity_show" in dispatch

    registry = (ROOT / "lib" / "commands.py").read_text()
    assert '"beacon sales send-account remove <label>"' in registry
    assert '"beacon sales identity show"' in registry
