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

Scope: the 3 Skill copies, plus the runtime guidance that lib/, bin/lib/ and
scripts/ print — an error message saying "run `python3 commands.py …`" teaches the
same bypass, at the moment the reader is most likely to follow it.

``ALLOWED`` is deliberately EMPTY. The whole debt was paid off in e-5981; a
non-empty allowlist would let the next one in quietly. If a verb genuinely has
no place on the CLI, say so here in prose with the reason — an entry without a
reason is the failure mode this guard exists to prevent.
"""

from __future__ import annotations

import ast
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

# Runtime guidance is a Skill surface too (ms-160 e-5981, 独立 AX レビュー A-1).
# An error message that tells the reader to run `python3 commands.py <verb>` teaches
# the boundary jump just as effectively as Skill prose does — and it teaches it at
# the exact moment the AI is stuck, so it is MORE likely to be obeyed. Two such
# strings in lib/sales_entities.py went stale the moment this CLI verb landed and
# kept instructing readers to bypass it; scanning only skills/*.md could not see
# them. Any file that can print guidance to a human or an AI is in scope.
GUIDANCE_GLOBS = (
    "lib/*.py",
    "bin/lib/*.sh",
    "scripts/*.py",
)

# Structural extraction, not a loose substring: match an actual invocation —
# `commands.py` (optionally closing a quote) followed by a verb token. Prose
# that merely NAMES the anti-pattern uses the literal placeholder `<cmd>`,
# which cannot match a verb token, so documentation does not self-trip.
_INVOKE = re.compile(r'commands\.py["\']?\s+([a-z][a-z0-9_]*)\b')

# Verbs exempt from the boundary, each with the reason it cannot be a CLI verb.
# Empty on purpose — see the module docstring.
ALLOWED: dict[str, str] = {}


# A guidance file legitimately mentions `commands.py` when it IS the dispatcher, or
# when a COMMENT names the anti-pattern (this guard's own repair instructions do
# exactly that). What must not exist is guidance *addressed to a reader* — an error
# message or usage line telling them to run it. So the two file kinds are read
# structurally rather than by line:
#
#   * Python  — parse with ast and inspect only string CONSTANTS (what can be
#               printed). Comments are not in the AST, so prose naming the
#               anti-pattern cannot false-positive, and a docstring that does is
#               caught deliberately (a docstring IS guidance to the next reader).
#   * Shell   — skip comment lines (`# …`); what remains is echo/usage text.
#
# A line-based regex cannot make this distinction: the first draft of this guard
# flagged its own explanatory comments, which would have made it unusable.
_RUNS_IT = re.compile(r'python3?[^\n]{0,80}commands\.py')


def _python_guidance_strings(path: Path):
    """Yield (lineno, text) for every string constant in a Python file.

    If the file does not parse, fall back to scanning its lines. Returning nothing
    would make an unparseable file a silent blind spot — the guard would go green
    precisely where it can see least, which is the failure mode this whole module
    exists to prevent.
    """
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        for lineno, line in enumerate(source.splitlines(), 1):
            if not line.lstrip().startswith("#"):
                yield lineno, line
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield getattr(node, "lineno", 0), node.value


def _shell_guidance_lines(path: Path):
    """Yield (lineno, text) for every non-comment line in a shell file."""
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield lineno, line


def find_violations(root: Path = ROOT):
    """Yield (relative path, line number, verb) for every boundary jump.

    Skill prose is checked line by line. Guidance files are checked only where the
    text is something a reader could be shown (a Python string constant, or a
    non-comment shell line) AND spells out a `python3 … commands.py <verb>`
    invocation — so a module that merely references commands.py in a comment does
    not trip.
    """
    out = []
    for glob in SKILL_GLOBS:
        for path in sorted(root.glob(glob)):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for verb in _INVOKE.findall(line):
                    if verb in ALLOWED:
                        continue
                    out.append((str(path.relative_to(root)), lineno, verb))

    for glob in GUIDANCE_GLOBS:
        for path in sorted(root.glob(glob)):
            if path.resolve() == Path(__file__).resolve():
                continue
            reader = (_python_guidance_strings if path.suffix == ".py"
                      else _shell_guidance_lines)
            for lineno, text in reader(path):
                if not _RUNS_IT.search(text):
                    continue
                for verb in _INVOKE.findall(text):
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
