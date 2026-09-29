#!/usr/bin/env python3
"""Skill → CLI boundary guard (ms-160 e-5981).

CORE doc ``architecture-tool-skill-separation`` §2 makes the CLI the single
abstraction boundary: **Skill → CLI → (local | API)**. A Skill that instead
writes ``python3 "$(beacon _lib-path)/commands.py" <verb>`` with hand-written
environment variables jumps that boundary, and the jump is not cosmetic:

1. the capability never appears in ``beacon --help`` — neither a human nor an
   AI can discover it (two verbs, ``sales_account_remove`` and
   ``sales_identity_show``, had gone fully unreachable this way);
2. Windows/pipx users go through ``beacon_cli/dispatch.py`` and cannot reach
   it at all;
3. the environment variable NAMES get duplicated into Skill prose, so renaming
   them in ``lib/commands.py`` breaks the Skill silently; and
4. it escapes ``scripts/check-cli-help-drift.py`` entirely.

This guard fails when a Skill invokes ``commands.py`` directly. The intended
repair is to add the verb to the CLI (bash ``bin/lib/cmd_*.sh`` + Windows
``beacon_cli/dispatch.py`` + the ``_help_registry()`` in ``lib/commands.py`` +
the README table) and call *that* from the Skill.

``ALLOWED`` is deliberately EMPTY. The whole debt was paid off in e-5981; a
non-empty allowlist would let the next one in quietly. If a verb genuinely has
no place on the CLI, say so here in prose with the reason — an entry without a
reason is the failure mode this guard exists to prevent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The 3 synchronized Skill copies (repo source, shared bundle, plugin bundle).
SKILL_GLOBS = (
    "skills/*.md",
    "shared/skills/*/SKILL.md",
    "plugins/beacon/skills/*/SKILL.md",
)

# Structural extraction, not a loose substring: match an actual invocation —
# `commands.py` (optionally closing a quote) followed by a verb token. Prose
# that merely NAMES the anti-pattern uses the literal placeholder `<cmd>`,
# which cannot match a verb token, so documentation does not self-trip.
_INVOKE = re.compile(r'commands\.py["\']?\s+([a-z][a-z0-9_]*)\b')

# Verbs exempt from the boundary, each with the reason it cannot be a CLI verb.
# Empty on purpose — see the module docstring.
ALLOWED: dict[str, str] = {}


def find_violations(root: Path = ROOT):
    """Yield (relative path, line number, verb) for every boundary jump."""
    out = []
    for glob in SKILL_GLOBS:
        for path in sorted(root.glob(glob)):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for verb in _INVOKE.findall(line):
                    if verb in ALLOWED:
                        continue
                    out.append((str(path.relative_to(root)), lineno, verb))
    return out


def main() -> int:
    violations = find_violations()
    if not violations:
        print("[skill-cli-boundary] OK: no Skill invokes lib/commands.py "
              "directly; every capability goes through the CLI.")
        return 0

    print("[skill-cli-boundary] Skill が CLI 境界を飛ばして lib/commands.py を"
          "直叩きしています:\n", file=sys.stderr)
    for path, lineno, verb in violations:
        print(f"  - {path}:{lineno}  → {verb}", file=sys.stderr)
    print(
        "\n直し方: その動詞を CLI に載せてから、Skill はその CLI を呼ぶ。\n"
        "  1. bin/lib/cmd_<noun>.sh に旗を解析する関数を足し、bin/beacon から呼ぶ\n"
        "  2. beacon_cli/dispatch.py にも同じ形を足す (Windows/pipx 経路)\n"
        "  3. lib/commands.py の _help_registry() に載せる (help の唯一の真値源)\n"
        "  4. README の CLI 表に行を足す (scripts/check-cli-help-drift.py が検査)\n"
        "  5. Skill の呼び出しを `beacon <名詞> <動詞>` に書き換える\n"
        "\n出典: CORE doc `architecture-tool-skill-separation` §2 / ms-160 e-5981",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
