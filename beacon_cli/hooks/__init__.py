"""Cross-platform Python translations of bin/*.sh hook scripts.

These modules are registered as console-script entry-points
(``beacon-hook-post-commit``, ``beacon-hook-postcompact``,
``beacon-hook-save``) so Claude Code can spawn them directly on Windows
where bash is unavailable. They are also importable as
``python -m beacon_cli.hooks.<name>`` for environments where the
pipx-managed PATH is not yet wired up.

Semantic equivalence with the bash counterparts is enforced by
``tests/test_post_commit_hook_py.py`` and
``tests/test_skill_install_windows.py``.
"""
