#!/usr/bin/env python3
"""Beacon CLI commands - thin adapter over core.py logic."""

__version__ = "0.46.0"

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from typing import Optional

from store import get_store
import core

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def _resolve_session_id() -> str:
    """Get the current beacon session_id, or "" if it can't be resolved.

    Returns "" on any failure so that commit/PR recording never fails just
    because session.py is misbehaving (corrupt session.json, missing .beacon/,
    test sandboxes that don't run the heartbeat). The empty string is
    the documented "no session" sentinel in core.log_commit / core.pr_add —
    those entries simply won't appear in session-log aggregation queries.
    """
    try:
        import session as _session
        return _session.get_session_id()
    except Exception:
        return ""


def _resolve_commit_source() -> str:
    """Detect the source axis for a commit being recorded (ms-79 / e-1817).

    Returns one of:
      - ``"auto-op"`` when the commit is happening inside a Beacon
        Operation envelope auto-execute context (= ms-60). The detection
        keys off env vars set by the operation runner; specifically:
          * ``BEACON_OPERATION_ENVELOPE_ID``  (= active envelope token)
          * ``BEACON_OPERATION_AUTO_EXECUTE`` set to ``"1"``
      - ``""`` (empty) for the default human dialog case. The empty
        string is the documented "untagged = human" sentinel — older
        commits without the field stay as-is and continue to count as
        human in retro_query's source breakdown.

    Kept env-var-driven on purpose: the Operation runner can set the
    flag without log_commit needing to know about envelope internals,
    and tests can drive it deterministically by exporting one env var.
    """
    if os.environ.get("BEACON_OPERATION_AUTO_EXECUTE", "") == "1":
        return "auto-op"
    if os.environ.get("BEACON_OPERATION_ENVELOPE_ID", "").strip():
        return "auto-op"
    return ""


def _user_home():
    """Resolve the user home directory, honoring an explicit HOME override.

    os.path.expanduser('~') on Windows keys off USERPROFILE/HOMEDRIVE+HOMEPATH
    and ignores HOME, which breaks env-overridden contexts (tests, sandboxes)
    and any setup where HOME != USERPROFILE. Prefer HOME when it resolves to a
    real absolute directory (so a leftover msys-style '/c/...' value can't send
    writes to a bogus path); otherwise fall back to expanduser. (ms-44 e-844)
    """
    home = os.environ.get("HOME")
    if home and os.path.isabs(home) and os.path.isdir(home):
        return home
    return os.path.expanduser("~")


def load_project():
    store = get_store()
    data = store.load_project()
    core.validate_project(data)
    return data


def load_project_unsafe():
    """Load project data WITHOUT running validate_project.

    Reserved for recovery flows (`beacon doctor`, `beacon milestone purge`)
    that must keep working when the data already violates invariants —
    e.g. duplicate IDs from a hand-edit (Issue #14). All other code paths
    must continue to use load_project() so corruption is surfaced early.
    """
    store = get_store()
    return store.load_project()


def _local_date(iso_str: str) -> str:
    """Convert a UTC ISO8601 timestamp (e.g. '2026-05-24T23:30:00Z') to the
    operator's local YYYY-MM-DD. Empty/invalid input passes through (best-effort)."""
    import datetime as _dt
    if not iso_str:
        return ""
    try:
        s = iso_str.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str[:10]


def _append_changelog(op: dict) -> None:
    """Append an operation entry to .beacon/changelog.jsonl."""
    import json as _json
    import datetime as _dt
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    changelog_path = os.path.join(beacon_dir, "changelog.jsonl")
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **op,
    }
    try:
        with open(changelog_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # changelog is best-effort; never block operations


def _require_reason_or_skip(verb: str) -> str:
    """Gate state-transition verbs (done / observe / restore / etc.) on a
    written reason being present in the BEACON_REASON env var (e-976).

    Three cases:

    - ``BEACON_REASON`` not in environ → refuse with exit 1. The CLI dispatcher
      only sets this env var when ``--reason`` is passed, so a missing var
      means the operator did not pass the flag at all.
    - ``BEACON_REASON`` present but empty (``--reason ""``) → accept. The
      operator has explicitly waived the gate. Discouraged but allowed so the
      audit trail records a deliberate "no reason" rather than silently going
      back to the pre-gate behavior.
    - Any non-empty value → accept and return it unchanged.

    Args:
        verb: Human-facing verb for the error message (e.g. ``"task done"``).

    Returns:
        The reason string (possibly empty when explicitly waived).
    """
    if "BEACON_REASON" not in os.environ:
        print(
            f"Error: --reason is required for `{verb}`. "
            f"Pass --reason \"...\" to record why, or --reason \"\" to "
            f"acknowledge the action without a written reason "
            f"(discouraged — retro becomes harder).",
            file=sys.stderr,
        )
        sys.exit(1)
    return os.environ.get("BEACON_REASON", "")


# ms-81 e-1916: forcing function — warn (don't block) when a write targets
# a milestone whose status doesn't authorise writes per the state-machine
# CORE doc DqIvAVzDprcq6hsq0AuF §1 + §6.
#
# Per the SPEC (warning-based, not block), this emits a stderr warning that
# names the offending status and the recommended remediation, and:
#   - on an interactive tty, prompts [y/N] to let the operator decide;
#   - on a non-interactive run (no tty — Skill / hook / dispatch path),
#     proceeds after logging the warning (= 努力義務, the operator can act
#     on the audit trail later);
#   - if BEACON_BYPASS_STATUS_GATE=1, skips the prompt entirely (= explicit
#     opt-out for bulk migrations / hook bypass scenarios).
_WRITE_AUTHORISED_STATUSES = {"in_progress", "active", "observing"}


def _check_ms_status_for_write(ms: dict, op_desc: str) -> bool:
    """Return True if the write may proceed (status authorised or operator
    consented to override); False only when an interactive operator declines.
    """
    status = ms.get("status", "todo")
    if status in _WRITE_AUTHORISED_STATUSES:
        return True

    if os.environ.get("BEACON_BYPASS_STATUS_GATE", "") == "1":
        return True

    title = ms.get("title", "")
    ms_id = ms.get("id", "")
    print(
        f"\n[ms-81 status gate] write to a {status} milestone\n"
        f"   target:      [{ms_id}] {title}\n"
        f"   status:      {status} (writes are discouraged — see CORE doc "
        f"DqIvAVzDprcq6hsq0AuF §1)\n"
        f"   operation:   {op_desc}\n"
        f"   suggestion:  transition to active via `beacon milestone start "
        f"{ms_id}` or to observing via `beacon milestone observe {ms_id} "
        f"--reason \"...\"` first.\n"
        f"   bypass:      set BEACON_BYPASS_STATUS_GATE=1 to silence this gate "
        f"(opt-out for hooks / bulk ops).",
        file=sys.stderr,
    )

    if not sys.stdin.isatty():
        # Non-interactive: proceed after logging the warning. The forcing
        # function is the visible warning + audit trail, not a block.
        print(
            f"   (non-interactive stdin — proceeding; the warning above is "
            f"the forcing function)",
            file=sys.stderr,
        )
        return True

    try:
        response = input("   Proceed anyway? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n   declined (no input)", file=sys.stderr)
        return False
    return response in ("y", "yes")


def save_project(data, op=None):
    core.validate_project(data)
    store = get_store()
    try:
        store.save_project(data)
    except RuntimeError as e:
        # Lost-update guard tripped (cloud mode only): the cloud changed since
        # we loaded it. Surface a clear message instead of a traceback so the
        # user knows to re-run rather than silently losing a concurrent edit.
        from store_api import ConflictError
        if isinstance(e, ConflictError):
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        raise
    if op:
        _append_changelog(op)


def save_project_unsafe(data, op=None):
    """Save project data WITHOUT validate_project.

    Reserved for the recovery flow where a single purge cannot make the
    project entirely clean (e.g. 3 duplicates of the same ms-id — the
    first purge leaves 2 dups, still invalid). Callers MUST run
    `beacon doctor` afterwards to confirm cleanup.
    """
    store = get_store()
    store.save_project(data)
    if op:
        _append_changelog(op)


# ---------------------------------------------------------------------------
# Init (CLI-specific: file creation, hook installation)
# ---------------------------------------------------------------------------

CLAUDE_MD_BEACON_SECTION = """\

## Beacon Project Management

This project uses [Beacon](https://github.com/r-kida2/beacon) for milestone-driven progress tracking.
このプロジェクトは Beacon でマイルストーンベースの進捗管理を行っています。

### Rules / ルール

- **Never edit `.beacon/project.json` directly. Always use beacon CLI commands.**
  `.beacon/project.json` を直接編集しない。必ず beacon CLI を使うこと。
- Before starting work, check milestones (`beacon status`) and confirm which milestone the work targets.
  実装開始前にマイルストーンを確認し、どのマイルストーンに向かう作業かユーザーに確認すること。
- After committing, the PostToolUse hook will auto-trigger `/beacon-log` Skill for AI-evaluated progress recording.
  コミット後はPostToolUse hookが自動で `/beacon-log` Skillを起動し、AI評価付きで進捗を記録する。
- If 2+ commits address the same issue, suggest grouping them into a task.
  同じ課題に2回以上コミットが発生したら、タスクにまとめることを提案する。
- Update the project summary when direction changes: `beacon summary "text"`
  方向性が変わった時はサマリーを更新する。書くべきは経緯・判断・背景であり、進捗率やMS名ではない。
- When the user hints at ending the session, or before you suggest splitting/ending the session yourself, run `/beacon-session-end` Skill first.
  ユーザーがセッション終了を仄めかしたとき、または自分自身がセッション分割・終了を提案する前に、必ず `/beacon-session-end` Skill を実行する。
- When the user wants to implement multiple milestones in parallel ("parallel", "sub-agents", "dispatch", etc.), run `/beacon-dispatch` Skill. Do not call the Agent tool directly.
  ユーザーが複数MSの並列実装を求めた場合（「パラレル」「サブエージェント」「並列」等）、必ず `/beacon-dispatch` Skill を実行する。Agent toolを直接呼ばない。
- When the user asks to review a PR ("レビューして", "review this PR", etc.), or when `beacon trigger check` shows a PR review trigger, immediately invoke `/review`. Never call `beacon pr approve/reject` directly without running `/review` first.
  ユーザーがPRのレビューを依頼したとき、またはbeacon triggerにPRレビュー通知があるとき、必ず `/review` Skillを使う。`/review` を経ずに `beacon pr approve/reject` を直接呼ばない。
- When the user says "memo this", "remember this", "メモして", "覚えておいて", or when you find context that must survive compaction, use `/beacon-note` Skill (or `beacon note "text"`). Notes are cleared at session-end.
  ユーザーが「メモして」「覚えておいて」と言ったとき、またはコンパクション後に必要なコンテキストを見つけたときは `/beacon-note` Skill を使う。セッション終了時にクリアされる。

### Proactive Guidance / 自発的な提案

Act as a consultant, not just a status display. Use beacon data to proactively propose next steps:
ダッシュボード（状態を見せる）ではなくコンサルタント（解釈して提案する）として振る舞う。

- **No milestones yet**: Read the codebase and docs, then suggest a concrete first milestone.
  MSがゼロの場合: コードとドキュメントを読み、最初のマイルストーン候補を提案する。
- **After a milestone completes**: Propose what the next milestone should be.
  MS完了直後: 次のマイルストーンを提案する。
- **After adding a new milestone**: Proactively offer to create a SPEC document for it.
  MS追加直後: そのMSのSPECドキュメント作成を自発的に提案する。
- **Progress stalled** (no commits in a while): Acknowledge it and offer to break down the work.
  進捗が止まっている: 気づいて声をかけ、タスク分解を提案する。
- **After a retro**: Propose next-phase direction based on what was learned.
  振り返り後: 学びを踏まえた次フェーズの方向性を提案する。

Proposals should feel like "What if we tried X?" — not directives.
提案は指示ではなく「こういう方向はどうですか？」という姿勢で。

### CLI Quick Reference

| Command | Description |
|---------|-------------|
| `beacon status` | Show project status / ステータス表示 |
| `beacon milestone add "title"` | Add milestone / MS追加 |
| `beacon milestone start <id>` | Activate milestone / MS開始 |
| `beacon task add "desc" -m <ms-id>` | Add task / タスク追加 |
| `beacon task done <id>` | Complete task / タスク完了 |
| `beacon log "summary"` | Record commit (auto via hook) / コミット記録（hook経由で自動） |
| `beacon summary "text"` | Update summary / サマリー更新 |
| `beacon note "text"` | Add session note (ephemeral, cleared at session-end) / セッションメモ追加 |
| `beacon note list` | Show session notes / メモ一覧 |
| `beacon note clear` | Clear all session notes / メモ全削除 |

<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->
### Entry Writing Principle / エントリ記述原則

When writing task / spec / doc entries (`description` / `motivation` / `acceptance_criteria`), follow these 4 principles. Beacon's readers include non-developers — the principles apply to all forward-going writes.
タスク・SPEC・ドキュメントを書くとき、以下 4 原則を守る。Beacon の読み手には非開発者が含まれるため、新しく書く全エントリに適用する。

1. **Reader-first 1-line description / 読み手目線 1 行**: `description` は「何が嬉しいか」をユーザーの言葉で 1 行。実装手段は含めない。
2. **3-tier loanword policy / 横文字 3 段階**: 固有名詞 (`Firestore` / `MCP`) はそのまま、技術概念 (`allowlist` / `opt-in`) は初出時に「(= 許可リスト)」のような日本語注、一般概念 (configure / receiver / audit) は日本語化。
3. **ID references with context / ID 参照に文脈**: `e-XXXX` / `ms-XX` / `UC?` の初出には必ず「(何の話か)」を 1 行添える。click-through 前提にしない。
4. **No truncated sentences / 尻切れトンボ禁止**: 主語・述語・論理関係を省略しない。開発者は文脈で補えるが非開発者は補えない。

Full principle and examples: `beacon doc show F3ZkqT0pKS6JpR8dn70n` (CORE doc `entry-writing-principle`).
原則の全文と実例: `beacon doc show F3ZkqT0pKS6JpR8dn70n` (CORE doc `entry-writing-principle`)。
<!-- /BEACON_ENTRY_WRITING_PRINCIPLE -->
"""


def _append_claude_md():
    claude_md = "CLAUDE.md"
    marker = "## Beacon Project Management"
    if os.path.exists(claude_md):
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
        if marker in content:
            # Replace existing section with latest content
            idx = content.index(marker)
            prefix = content[:idx].rstrip('\n') + '\n\n'
            new_section = CLAUDE_MD_BEACON_SECTION.lstrip('\n')
            after = content[idx + len(marker):]
            next_h2 = after.find('\n## ')
            if next_h2 == -1:
                new_content = prefix + new_section
            else:
                new_content = prefix + new_section + after[next_h2:]
            if new_content != content:
                with open(claude_md, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated {claude_md} with beacon rules")
            return
    with open(claude_md, "a", encoding="utf-8") as f:
        f.write(CLAUDE_MD_BEACON_SECTION)
    print(f"Updated {claude_md} with beacon rules")


POST_COMMIT_HOOK = """\
#!/usr/bin/env bash
# Beacon: auto-log commits to the active milestone
# Skip in Claude Code — AI handles logging with milestone/task judgment
if [ -n "$BEACON_CLAUDE_CODE" ]; then
    exit 0
fi
if [ -f ".beacon/project.json" ] && command -v beacon &>/dev/null; then
    beacon log 2>/dev/null || true
fi
"""

BEACON_HOOK_MARKER = "# Beacon: auto-log commits"


def _install_git_hook():
    hook_dir = os.path.join(".git", "hooks")
    if not os.path.isdir(hook_dir):
        return
    hook_path = os.path.join(hook_dir, "post-commit")
    if os.path.exists(hook_path):
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()
        if BEACON_HOOK_MARKER in content:
            return
        with open(hook_path, "a", encoding="utf-8") as f:
            f.write("\n" + POST_COMMIT_HOOK)
    else:
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(POST_COMMIT_HOOK)
    os.chmod(hook_path, 0o755)
    print("Installed git post-commit hook")


def _find_hook(name):
    """Locate a hook script. Brew install places hooks alongside this file
    (libexec/); dev repo has them at <repo>/bin/."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo_bin = os.path.join(os.path.dirname(here), "bin", name)
    sibling = os.path.join(here, name)
    if os.path.exists(sibling):
        return sibling
    return repo_bin


CLAUDE_HOOK_SCRIPT = _find_hook("beacon-post-commit-hook.sh")
CLAUDE_SAVE_HOOK_SCRIPT = _find_hook("beacon-save-hook.sh")
CLAUDE_POSTCOMPACT_HOOK_SCRIPT = _find_hook("beacon-postcompact.sh")
# ms-44 e-854: Stop hook bash script (Mac/Linux). Python port lives at
# beacon_cli.hooks.context_monitor and is resolved via _resolve_hook_command
# when the user is on Windows or has the wheel installed.
CLAUDE_CONTEXT_MONITOR_HOOK_SCRIPT = _find_hook("context-usage-monitor.sh")


def _install_claude_hook():
    settings_path = os.path.join(_user_home(), ".claude", "settings.json")
    settings_dir = os.path.dirname(settings_path)
    os.makedirs(settings_dir, exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])

    # Install commit detection hook (matcher: Bash)
    commit_hook_exists = False
    for entry in post_tool_use:
        if entry.get("matcher") == "Bash":
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "beacon-post-commit-hook" in cmd or "beacon log --prepare" in cmd:
                    commit_hook_exists = True
                    break
    if not commit_hook_exists:
        post_tool_use.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                # ms-44 e-853: resolve cross-platform (entry-point / python -m)
                # so Windows never gets a non-executable .sh commit hook.
                "command": _resolve_hook_command("beacon-post-commit-hook.sh"),
                "timeout": 10,
                "statusMessage": "Beacon: checking commit...",
            }],
        })

    # Install MCP save hook (matcher: mcp__)
    save_hook_exists = False
    for entry in post_tool_use:
        if entry.get("matcher") == "mcp__":
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "beacon-save-hook" in cmd:
                    save_hook_exists = True
                    break
    if not save_hook_exists:
        post_tool_use.append({
            "matcher": "mcp__",
            "hooks": [{
                "type": "command",
                "command": _resolve_hook_command("beacon-save-hook.sh"),
                "timeout": 10,
                "statusMessage": "Beacon: checking MCP operation...",
            }],
        })

    # Install PostCompact hook (e-565): inject Tier-2 orientation after
    # transcript compaction so the AI re-fetches the source of truth rather
    # than trusting the (now blurry) summary's specific IDs / numbers.
    post_compact = hooks.setdefault("PostCompact", [])
    postcompact_hook_exists = False
    for entry in post_compact:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if (
                "beacon-postcompact" in cmd
                or "beacon-hook-postcompact" in cmd
                or "beacon_cli.hooks.postcompact" in cmd
            ):
                postcompact_hook_exists = True
                break
        if postcompact_hook_exists:
            break
    # ms-44 e-853: resolve cross-platform; the command may be a ``python -m``
    # form (not a path), so guard path-existence only for path commands.
    _pc_cmd = _resolve_hook_command("beacon-postcompact.sh")
    _pc_ok = bool(_pc_cmd) and (not _is_path_command(_pc_cmd) or os.path.exists(_pc_cmd))
    if not postcompact_hook_exists and _pc_ok:
        post_compact.append({
            "hooks": [{
                "type": "command",
                "command": _pc_cmd,
                "timeout": 10,
                "statusMessage": "Beacon: post-compaction orientation...",
            }],
        })

    # ms-44 e-854: register the Stop hook (context-usage threshold monitor)
    # so Mac/Linux + Win all get a Python or bash hook that actually fires.
    # OS branching is implicit via _resolve_hook_command + _hook_unusable_on_windows.
    stop_hooks = hooks.setdefault("Stop", [])
    stop_hook_exists = False
    for entry in stop_hooks:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if (
                "context-usage-monitor" in cmd
                or "beacon-hook-context-monitor" in cmd
                or "beacon_cli.hooks.context_monitor" in cmd
            ):
                stop_hook_exists = True
                break
        if stop_hook_exists:
            break
    _ctx_cmd = _resolve_hook_command("context-usage-monitor.sh")
    _ctx_ok = bool(_ctx_cmd) and (not _is_path_command(_ctx_cmd) or os.path.exists(_ctx_cmd))
    if not stop_hook_exists and _ctx_ok:
        stop_hooks.append({
            "hooks": [{
                "type": "command",
                "command": _ctx_cmd,
                "timeout": 10,
                "statusMessage": "Beacon: checking context-usage threshold...",
            }],
        })

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Installed Claude Code PostToolUse + PostCompact + Stop hooks")


def _install_skills():
    """Copy beacon skills to ~/.claude/skills/ for Claude Code integration."""
    skills_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
    )
    if not os.path.isdir(skills_src):
        return
    skills_dst = os.path.join(_user_home(), ".claude", "skills")
    os.makedirs(skills_dst, exist_ok=True)
    installed = []
    for fname in os.listdir(skills_src):
        if not fname.endswith(".md"):
            continue
        skill_name = fname[:-3]  # beacon-log.md -> beacon-log
        dst_dir = os.path.join(skills_dst, skill_name)
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(os.path.join(skills_src, fname), os.path.join(dst_dir, "SKILL.md"))
        installed.append(skill_name)
    if installed:
        print(f"Installed skills: {', '.join(sorted(installed))}")


def _maybe_prompt_initial_profile() -> Optional[str]:
    """ms-64 e-1633: When the user has auth-logged into >= 2 Beacon backends
    and has not already committed this cwd to a profile, ask once which one
    this project should target. Returns the chosen profile name (or None if
    the prompt should be skipped — caller falls back to silent default).

    Skip rules (all silent, no prompt):
      - --profile / BEACON_PROFILE explicitly set (= user already chose)
      - .beacon/cloud.json already has a `profile` field (= prior choice persisted)
      - len(logged_in_profiles) < 2 (= no ambiguity)
      - stdin is not a TTY (= hook / CI / pipe — don't block on input)
    """
    if os.environ.get("BEACON_PROFILE"):
        return None
    cloud_path = _get_cloud_config_path()
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
            if existing.get("profile"):
                return None
        except Exception:
            pass
    try:
        import profile as _profile  # type: ignore[import-not-found]
        candidates = _profile.list_logged_in_profiles()
    except Exception:
        return None
    if len(candidates) < 2:
        return None
    if not sys.stdin.isatty():
        return None
    try:
        return _profile.prompt_choose_profile(candidates)
    except Exception:
        return None


def _persist_initial_profile_choice(profile_name: str) -> None:
    """Write {"profile": <name>} to .beacon/cloud.json so that all subsequent
    commands in this cwd resolve to that profile (= cwd-based auto-switch)."""
    cloud_path = _get_cloud_config_path()
    os.makedirs(os.path.dirname(cloud_path) or ".", exist_ok=True)
    existing: dict = {}
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    existing["profile"] = profile_name
    with open(cloud_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _build_disclosure_policy_from_env() -> dict:
    """Resolve the disclosure_policy for a new project from env vars.

    ms-63 / e-1441: ``beacon init --sensitivity {high,low}`` (forwarded via
    ``BEACON_SENSITIVITY``) lets the user pick the project posture at
    create-time. Default is ``high`` per SPEC § 設計方針 2 — forgetting to
    configure must fail closed (= AI clams up) rather than fail open (= AI
    leaks). The OSS dogfood (Beacon itself) opts in to ``low`` explicitly.

    Recognised values: ``high`` or ``low``. Anything else (typo / future
    value) silently degrades to ``high`` so a misconfiguration cannot
    accidentally produce an open project.
    """
    raw = os.environ.get("BEACON_SENSITIVITY", "").strip().lower()
    sensitivity = "low" if raw == "low" else "high"
    # The semantics mirror server/envelope.normalize_disclosure_contract:
    # high → schema-only T5 + no free text; low → free mode + free text on.
    if sensitivity == "low":
        return {
            "sensitivity": "low",
            "t5_response_mode": "free",
            "t5_free_text": True,
        }
    return {
        "sensitivity": "high",
        "t5_response_mode": "schema-only",
        "t5_free_text": False,
    }


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    retro_day = os.environ.get("BEACON_RETRO_DAY", "monday")
    # ms-63 / e-1428 + e-1441: persist the project disclosure_policy at
    # init time so subsequent envelope mints (server/envelope.issue_envelope)
    # can snapshot the contract. Default is high (safe). The field lives
    # next to the existing top-level project fields so legacy readers that
    # don't know about it just ignore it (= forward-compat append-only).
    disclosure_policy = _build_disclosure_policy_from_env()
    data = {
        "name": name,
        "objective": objective,
        "milestones": [],
        "retro_day": retro_day,
        "disclosure_policy": disclosure_policy,
    }
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {pf}")
    # Visible feedback on the chosen posture (SPEC § acceptance 2 + 3):
    # default-high is silent-but-printed so the user notices, opt-in low
    # gets a single-line confirmation that the OSS-friendly mode is active.
    if disclosure_policy["sensitivity"] == "low":
        print("  disclosure_policy.sensitivity = low (open posture; free-text "
              "DM replies permitted)")
    else:
        print("  disclosure_policy.sensitivity = high (default-safe; T5 DM "
              "replies capped to schema)")

    chosen = _maybe_prompt_initial_profile()
    if chosen:
        _persist_initial_profile_choice(chosen)
        print(f"Profile pinned for this project: {chosen}")

    print("Next: beacon milestone add")


def cmd_common_setup():
    """Install Claude Code hooks, skills, and CLAUDE.md beacon section (idempotent)."""
    _append_claude_md()
    _install_git_hook()
    _install_claude_hook()
    _install_skills()
    print("Claude Code integration ready.")


def cmd_auth_check():
    """Exit 0 if authenticated, exit 1 if not."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("not_authenticated")
        sys.exit(1)
    email = getattr(creds, "email", "") or ""
    print(f"authenticated:{email}")
    sys.exit(0)


def cmd_cloud_check_project():
    """Exit 0 if project exists in cloud, exit 1 otherwise."""
    import sys as _sys
    project_id = _sys.argv[1] if len(_sys.argv) > 1 else os.environ.get("BEACON_CLOUD_PROJECT_ID", "")
    if not project_id:
        _sys.exit(1)
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        _sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    try:
        client.get_project(project_id)
        _sys.exit(0)
    except Exception:
        _sys.exit(1)


def cmd_cloud_join():
    """Join an existing cloud project (no .beacon/ required)."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    project_id = os.environ.get("BEACON_CLOUD_PROJECT_ID", "")
    if not project_id:
        print("Error: project ID required")
        sys.exit(1)

    api_url = _resolve_active_api_url()
    token = _extract_token(creds)

    from api_client import ApiClient
    client = ApiClient(api_url, token)

    try:
        data = client.get_project(project_id)
    except RuntimeError as e:
        if "404" in str(e):
            print(f"Project '{project_id}' not found in cloud.")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    core.validate_project(data)

    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    os.makedirs(beacon_dir, exist_ok=True)

    cloud_config_path = os.path.join(beacon_dir, "cloud.json")
    with open(cloud_config_path, "w", encoding="utf-8") as f:
        json.dump({"project_id": project_id, "api_url": api_url}, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # e-1861 (ms-61): No longer write `{"mode": "cloud"}` to config.json —
    # cloud.json existence is the sole source of truth. The previous dual
    # write created a silent-drift attack surface (= sub-agent overwriting
    # config.json could flip cloud → local without touching cloud.json).

    # Write project.json directly (LocalStore.save_project requires the file to exist)
    pf = get_project_file()
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Joined cloud project: {project_id}")
    print(f"Project: {data.get('name', 'unnamed')}")


# ---------------------------------------------------------------------------
# Milestone commands
# ---------------------------------------------------------------------------

def cmd_milestone_add():
    title = os.environ.get("BEACON_TITLE", "")
    target_date = os.environ.get("BEACON_TARGET_DATE", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    owner = os.environ.get("BEACON_OWNER", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    data = load_project()
    ms_id = core.milestone_add(data, title, target_date, description=description,
                               priority=priority, objective=objective,
                               acceptance_criteria=acceptance_criteria,
                               owner=owner, assignee=assignee)
    save_project(data)
    print(f"Added milestone {ms_id}: {title}")
    if owner or assignee:
        if owner:
            print(f"  owner: {owner}")
        if assignee:
            print(f"  assignee: {assignee}")

    # Promote SPEC creation: fire a trigger so session-start / next prompt
    # surfaces a warning. SPEC = requirements + decision trail (see CORE
    # doc `doc-classification`). This is a soft promotion (warning only),
    # never a hard block — see SPEC for ms-41.
    try:
        _fire_spec_needed_trigger(ms_id, title)
    except Exception:
        pass  # Trigger is best-effort; never block milestone creation.

    # Print an inline hint, but suppress it when the user is doing a bulk
    # add (e.g. /beacon-roadmap registering 5+ MSs in a row). Without this
    # suppression the hint repeats N times and adds noise (e-702).
    # Detection heuristic: if another milestone was added within the last
    # 60 seconds, treat this as bulk-add mode and skip the inline hint.
    # session-start / dispatch warnings still surface the SPEC promotion
    # for any MS still missing a SPEC, so users don't lose the reminder.
    if not _is_bulk_milestone_add(data, ms_id):
        print(
            f"\n  Hint: SPEC (要求書 / 判断軌跡) を作成すると、サブエージェントや retrospection が"
            f"機能しやすくなります。`/beacon-spec {ms_id}` を実行するか、後で session-start で"
            f"warning が出たときに作成してください。"
        )


def _is_bulk_milestone_add(data: dict, current_ms_id: str) -> bool:
    """Return True if another milestone was added within the last 60 seconds.

    Used by cmd_milestone_add to suppress the SPEC promotion hint during
    bulk-add operations (e.g. /beacon-roadmap registering 6 MSs in a row).
    The trigger system still records spec-needed for each MS, and
    session-start surfaces them — only the inline hint is suppressed.
    """
    import datetime as _dt
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        milestones = data.get("milestones", [])
        # Compare against the second-most-recent MS (the most recent is the
        # one we just added).
        prior = None
        for ms in reversed(milestones):
            if ms.get("id") == current_ms_id:
                continue
            prior = ms
            break
        if prior is None:
            return False
        prior_created = prior.get("created_at", "") or ""
        if not prior_created:
            return False
        # ISO format; strip trailing Z and parse
        prior_dt = _dt.datetime.fromisoformat(prior_created.replace("Z", "+00:00"))
        delta = (now - prior_dt).total_seconds()
        return delta < 60.0
    except Exception:
        return False


def cmd_milestone_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    ms_filter_str = os.environ.get("BEACON_MS_FILTER", "")
    data = load_project()

    milestones = data["milestones"]
    if not show_all:
        milestones = [ms for ms in milestones if ms.get("status") != "cancelled"]

    if ms_filter_str:
        ms_ids = [m.strip() for m in ms_filter_str.split(",") if m.strip()]
        all_ids = {ms["id"] for ms in data["milestones"]}
        for ms_id in ms_ids:
            if ms_id not in all_ids:
                print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
                sys.exit(1)
        milestones = [ms for ms in milestones if ms["id"] in ms_ids]

    if json_mode:
        output = {
            "name": data.get("name", ""),
            "summary": data.get("summary", ""),
            "milestones": [],
            "operations": [],
        }
        for ms in milestones:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            ms_item = {
                "id": ms["id"],
                "title": ms.get("title", ""),
                "status": ms.get("status", "todo"),
                "progress": ms.get("progress", 0),
                "target_date": ms.get("target_date", ""),
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
            }
            for f in ("priority", "objective", "acceptance_criteria", "description"):
                if ms.get(f):
                    ms_item[f] = ms[f]
            output["milestones"].append(ms_item)
        import datetime as _dt
        today_str = _dt.date.today().isoformat()
        for op in data.get("operations", []):
            if op.get("status") != "open":
                continue
            entries = op.get("entries", [])
            runs = [e for e in entries if e.get("type") == "run_record"]
            open_incidents = [e for e in entries if e.get("type") == "incident" and e.get("status") == "open"]
            recent_runs = []
            for r in runs[-3:]:
                recent_runs.append({"date": _local_date(r.get("date", "")), "status": r.get("status", "")})
            output["operations"].append({
                "id": op["id"],
                "title": op.get("title", ""),
                "status": op.get("status", "open"),
                "log_source": op.get("log_source", ""),
                "schedule": op.get("schedule", {}),
                "recent_runs": recent_runs,
                "open_incidents": open_incidents,
            })
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    for ms in milestones:
        icon = icons.get(ms["status"], "?")
        active = " \u25c0 ACTIVE" if ms["status"] == "in_progress" else ""
        progress = ms.get("progress", 0)
        print(f"  {icon} [{ms['id']}] {ms['title']} ({progress}%){active}")


def cmd_milestone_start():
    """Activate an MS, auto-create its workspace, and self-add as assignee.

    Layered behaviour (current):

      1. Self-assignee: the actor (``lib/agent.get_actor()``) is added to
         the MS's assignee list (no-op if already present).
      2. Auto-workspace (ms-65 e-1477, cwd-aware):
           - Main project root → create a worktree at
             ``.worktrees/<ms-branch>/`` checked out to ``ms-XX-<slug>``.
             The main cwd's HEAD is NOT touched. This avoids the
             2026-06-10 incident where two bclaude sessions in the same
             cwd silently shared a git HEAD (per CORE doc *MS 活性化 =
             workspace 確保の根本責務*, see ms-65 SPEC).
           - Inside an existing worktree (e.g. dispatch-spawned context,
             or the user manually cd'd into one) → keep the legacy
             in-place ``git checkout`` behaviour. Switching the worktree's
             own HEAD does not affect other sessions because each worktree
             has its own HEAD.
      3. Skip when not a git repo: dispatch contexts (tests, scaffolds)
         where there is no .git transparently fall back to "just flip
         status" so existing flows are unaffected.

    Suppression knobs:
      * ``BEACON_NO_BRANCH=1`` skips the workspace / branch step entirely
        (used by tests and by callers that manage their own branching).
      * ``BEACON_NO_ASSIGNEE=1`` skips the assignee auto-add.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    no_branch = os.environ.get("BEACON_NO_BRANCH", "") == "1"
    no_assignee = os.environ.get("BEACON_NO_ASSIGNEE", "") == "1"

    data = load_project()
    ms = core.milestone_start(data, ms_id)

    # ---- 1b. occupation claim (ms-81 e-1918) ----
    # Tied to milestone_start so status / assignee / occupation always lift
    # together. If a previous claim exists from another session, warn —
    # never block (= SPEC §3-3, 努力義務). Gated by BEACON_NO_BRANCH /
    # BEACON_NO_ASSIGNEE so test sandboxes and scripted scaffolds that
    # request a pure status flip don't trip session-resolution side effects.
    if not no_branch and not no_assignee:
        try:
            import agent as _agent_for_claim
            actor_for_claim = _agent_for_claim.get_actor()
        except Exception:
            actor_for_claim = {}
        sid_for_claim = _resolve_session_id() or ""
        _ms_claim, previous_claim = core.milestone_claim_occupation(
            data, ms_id,
            session_id=sid_for_claim,
            machine=actor_for_claim.get("machine", ""),
            agent=actor_for_claim.get("agent", ""),
        )
        if previous_claim and previous_claim.get("session_id") != sid_for_claim:
            prev_sid = previous_claim.get("session_id", "?")
            prev_machine = previous_claim.get("machine", "?")
            print(
                f"  [ms-81 occupation] previous claim by session "
                f"{prev_sid[:12]}... on {prev_machine} (claimed_at: "
                f"{previous_claim.get('claimed_at', '?')}). Proceeding with "
                f"takeover; if that session crashed, this is normal — if it is "
                f"still actively working, coordinate via beacon dm.",
                file=sys.stderr,
            )
            core.milestone_record_occupation_event(
                data, ms_id=ms_id, event_type="takeover",
                session_id=sid_for_claim,
                machine=actor_for_claim.get("machine", ""),
                agent=actor_for_claim.get("agent", ""),
                reason=f"superseded session {prev_sid[:12]}",
            )
        else:
            core.milestone_record_occupation_event(
                data, ms_id=ms_id, event_type="claim",
                session_id=sid_for_claim,
                machine=actor_for_claim.get("machine", ""),
                agent=actor_for_claim.get("agent", ""),
            )

    # ---- 1. assignee auto-add (lib/agent.py is the single source) ----
    actor_str = ""
    if not no_assignee:
        try:
            import agent as _agent
            actor = _agent.get_actor()
            # Wire-format: prefer the agent name (it's the unique writer
            # identifier — machine is implicit). If the machine should
            # ever be needed for disambiguation it's still in the commit
            # metadata (e-934). For the assignee badge a clean handle
            # reads better.
            actor_str = actor.get("agent", "") or actor.get("machine", "")
            if actor_str:
                _ms_unused, _added = core.milestone_assignee_add(
                    data, ms_id, actor_str
                )
        except Exception as e:  # pragma: no cover - defensive
            print(f"  warning: could not auto-add assignee: {e}", file=sys.stderr)

    # ---- 2. auto-workspace / auto-branch (cwd-aware + project-type-aware) ----
    # ms-81 e-1917: explicit non-git degrade. If the project isn't a git repo
    # (= research / writing project, scaffold, fresh dir), we skip the entire
    # worktree branch silently and only flip status + assignee. Per CORE doc
    # DqIvAVzDprcq6hsq0AuF §3-2 the physical-boundary mechanism degrades to
    # logical occupation only; for now occupation is recorded server-side
    # (handled in e-1918), so degrade simply means "no worktree".
    branch_name = ""
    branch_msg = ""
    workspace_path = ""
    non_git_skip = False
    if not no_branch:
        if not _is_git_project():
            non_git_skip = True
        else:
            try:
                import branch as _branch
                branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
                if _is_in_main_project_root():
                    import worktree as _worktree
                    workspace_path = os.path.join(".worktrees", branch_name)
                    try:
                        wt = _worktree.create_workspace(workspace_path, branch_name)
                        branch_msg = "worktree created" if wt["created"] else "worktree exists"
                    except _worktree.GitNotInstalledError:
                        # No git → fall through silently (status flip already done).
                        branch_name = ""
                        workspace_path = ""
                    except _worktree.WorktreeCreateError as exc:
                        print(f"  warning: could not create worktree: {exc}", file=sys.stderr)
                        branch_name = ""
                        workspace_path = ""
                else:
                    # Already inside a worktree: in-place checkout is safe
                    # because each worktree owns its own HEAD.
                    branch_msg = _ensure_on_branch(branch_name)
            except _NotAGitRepoError:
                # Quiet skip: scaffolds / tests / fresh projects without git
                # should still be able to start a milestone.
                branch_name = ""
            except Exception as e:  # pragma: no cover - defensive
                print(f"  warning: could not create/switch branch: {e}", file=sys.stderr)

    save_project(data)
    print(f"Activated: {ms['title']}")
    if actor_str and not no_assignee:
        print(f"  assignee: {actor_str}")
    if branch_name and branch_msg:
        print(f"  branch: {branch_name} ({branch_msg})")
    if workspace_path:
        print(f"  next: cd {workspace_path} && bclaude")
        print(f"        (新しいセッションをこの worktree で開いて作業してください — "
              f"同 cwd で並走すると別マイルストーンの作業を同じ branch に書く事故が起きるため)")
    if non_git_skip:
        # ms-81 e-1917: surface the project-type degrade so the user knows
        # the worktree step was intentionally skipped (= research / writing
        # project), not silently dropped.
        print("  workspace: non-git project, worktree step skipped "
              "(logical occupation only)")


def _is_git_project() -> bool:
    """Return True if the current project root looks like a git repository.

    ms-81 e-1917: explicit project-type detection used by ``milestone start``
    to decide whether to engage the worktree mechanism. We check the cheap
    common-case (``.git`` exists at the beacon root / cwd) before falling
    back to ``git rev-parse``, which would otherwise produce noisy stderr
    on plain directories.
    """
    # Cheapest path: a ``.git`` directory or file at cwd / beacon root.
    candidates = [os.getcwd()]
    beacon_root = os.environ.get("BEACON_ROOT", "")
    if beacon_root and beacon_root not in candidates:
        candidates.append(beacon_root)
    for root in candidates:
        if os.path.exists(os.path.join(root, ".git")):
            return True
    # Fall back to git rev-parse (covers the worktree case where ``.git`` is
    # a pointer file outside cwd).
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _is_in_main_project_root() -> bool:
    """Return True if the cwd is the main project root (not inside a worktree).

    Detection: ``git rev-parse --git-dir`` and ``--git-common-dir`` return
    the same path at the main checkout, but in a linked worktree the
    ``--git-dir`` points to ``<main>/.git/worktrees/<name>/`` while
    ``--git-common-dir`` still points to the shared ``<main>/.git``.

    Conservative default: on any failure (git missing, not a repo, parse
    error) return True. Treating "unknown environment" as "main root"
    keeps the cwd-aware path engaged so the worktree creation is at least
    attempted; if git is genuinely missing the worktree helper will raise
    GitNotInstalledError and the caller falls back gracefully.
    """
    try:
        gd = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=5,
        )
        cd = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return True
    if gd.returncode != 0 or cd.returncode != 0:
        return True
    return os.path.abspath(gd.stdout.strip()) == os.path.abspath(cd.stdout.strip())


class _NotAGitRepoError(RuntimeError):
    """Raised by :func:`_ensure_on_branch` when we are not inside a git repo.

    Distinct from generic ``RuntimeError`` so callers can swallow only this
    case (transparent skip) without hiding real git errors.
    """


def _ensure_on_branch(branch_name: str) -> str:
    """Make sure HEAD is on ``branch_name``; create it from HEAD if missing.

    Returns a short human-readable status string used by the CLI to tell
    the user what happened ("created", "switched", "already on it",
    "preserved", or empty). Raises :class:`_NotAGitRepoError` if the
    working directory is not inside a git repository.

    The function intentionally does NOT touch ``main`` (or any upstream)
    — it creates the new branch *from current HEAD*. Callers that want
    a fresh base from origin/main should run ``git fetch && git
    checkout main`` first; this helper is just the safe local step.
    """
    try:
        # First confirm we're in a git repo. `rev-parse --is-inside-work-tree`
        # exits 0 + "true" on success, non-zero otherwise.
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError) as e:
        raise _NotAGitRepoError("git not available") from e
    if check.returncode != 0 or check.stdout.strip() != "true":
        raise _NotAGitRepoError("not inside a git work tree")

    # Where are we now?
    cur = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True, text=True, timeout=5,
    )
    current = cur.stdout.strip() if cur.returncode == 0 else ""
    if current == branch_name:
        return "already on it"

    # Does the branch already exist locally?
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch_name}"],
        capture_output=True, text=True, timeout=5,
    )
    if exists.returncode == 0:
        # Switch to it; refuse if there are uncommitted changes (let git
        # handle the safety check — we don't want to overwrite work).
        sw = subprocess.run(
            ["git", "checkout", branch_name],
            capture_output=True, text=True, timeout=10,
        )
        if sw.returncode != 0:
            err = sw.stderr.strip() or "checkout failed"
            print(f"  warning: branch {branch_name} exists but checkout failed: {err}",
                  file=sys.stderr)
            return "preserved"
        return "switched"

    # Create it from current HEAD.
    create = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        capture_output=True, text=True, timeout=10,
    )
    if create.returncode != 0:
        err = create.stderr.strip() or "create failed"
        print(f"  warning: could not create {branch_name}: {err}", file=sys.stderr)
        return "preserved"
    return "created"


def _prompt_close_leftover_worktree(ms_id: str, transition: str) -> None:
    """ms-81 e-1919: surface leftover worktrees on phase transitions.

    When an MS moves to done / observing / waiting we check whether the
    branch-specific worktree directory still exists; a leftover worktree
    is the temptation other sessions could later (re-)check out and
    accidentally commit against. We prompt rather than block: interactive
    runs get a [y/N] auto-close; non-interactive runs get a one-line
    warning and proceed. Per SPEC §4 this is intentionally a forcing
    function, not enforcement.
    """
    try:
        data = load_project()
    except Exception:
        return
    ms = next((m for m in data.get("milestones", []) if m.get("id") == ms_id), None)
    if ms is None:
        return
    try:
        import branch as _branch
        branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
    except Exception:
        return
    workspace_path = os.path.join(".worktrees", branch_name)
    if not os.path.exists(workspace_path):
        return
    msg = (
        f"\n[ms-81 transition prompt] worktree still present at "
        f"{workspace_path} after {transition} of [{ms_id}].\n"
        f"   Leftover worktrees can be (re-)entered by other sessions; "
        f"cleanup keeps the audit trail tight."
    )
    print(msg, file=sys.stderr)
    if not sys.stdin.isatty():
        print(
            "   (non-interactive — leaving the worktree in place; clean up "
            "with `beacon milestone workspace-cleanup " + ms_id + "` when "
            "convenient.)",
            file=sys.stderr,
        )
        return
    try:
        choice = input("   Auto-close worktree? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    if choice in ("y", "yes"):
        # Defer to the existing workspace_cleanup command rather than
        # duplicating the git worktree remove machinery; it already
        # handles branch checks and idempotency.
        os.environ["BEACON_MS_ID"] = ms_id
        cmd_milestone_workspace_cleanup()
    else:
        print(
            f"   leaving worktree in place; run `beacon milestone "
            f"workspace-cleanup {ms_id}` to remove later.",
            file=sys.stderr,
        )


def _release_all_occupations_for_session(session_id: str) -> int:
    """ms-81 e-1918 (SPEC AC #15): release every MS this session is occupying.

    Called from session-end so a clean exit leaves the next session free
    to claim. Returns the number of releases performed (0 for sessions
    that weren't holding anything).
    """
    if not session_id:
        return 0
    data = load_project()
    try:
        import agent as _agent_for_se
        actor = _agent_for_se.get_actor()
    except Exception:
        actor = {}
    released = 0
    for ms in data.get("milestones", []):
        occ = ms.get("occupation")
        if occ and occ.get("session_id") == session_id:
            ms_id = ms["id"]
            core.milestone_release_occupation(data, ms_id, reason="session-end")
            core.milestone_record_occupation_event(
                data, ms_id=ms_id, event_type="release",
                session_id=session_id,
                machine=actor.get("machine", ""),
                agent=actor.get("agent", ""),
                reason="session-end",
            )
            released += 1
    if released:
        save_project(data, op={"op": "session_end_release", "count": released})
    return released


def _release_occupation_for_transition(data, ms_id, *, reason):
    """ms-81 e-1918: phase transitions auto-release any active occupation
    on the target MS. Per the SPEC the release happens whether or not the
    session that claimed it is the same one calling the transition (= a
    done verb on someone else's claim is implicitly a takeover).
    """
    sid = _resolve_session_id() or ""
    try:
        import agent as _agent_for_release
        actor = _agent_for_release.get_actor()
    except Exception:
        actor = {}
    _ms, released = core.milestone_release_occupation(data, ms_id, reason=reason)
    if released:
        core.milestone_record_occupation_event(
            data, ms_id=ms_id, event_type="release",
            session_id=sid,
            machine=actor.get("machine", ""),
            agent=actor.get("agent", ""),
            reason=reason,
        )


def cmd_milestone_done():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone done")
    data = load_project()
    ms = core.milestone_done(data, ms_id, reason=reason)
    _release_occupation_for_transition(data, ms_id, reason="done")
    save_project(data, op={"op": "milestone_done", "ms_id": ms_id, "reason": reason})
    _prompt_close_leftover_worktree(ms_id, "done")
    print(f"Completed: {ms['title']}")
    if reason:
        print(f"  Reason: {reason}")


def cmd_milestone_wait():
    """Transition a milestone to ``waiting`` status (ms-81 e-1915).

    Requires --reason (same gate as observe / done) so retro can reconstruct
    why work paused. The transition is rejected by core.milestone_wait if
    the source status is not active or observing.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone wait")
    if not ms_id:
        print("Usage: beacon milestone wait <ms-id> --reason <text>",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms = core.milestone_wait(data, ms_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _release_occupation_for_transition(data, ms_id, reason="wait")
    save_project(data, op={"op": "milestone_wait", "ms_id": ms_id,
                           "reason": reason})
    _prompt_close_leftover_worktree(ms_id, "wait")
    print(f"Waiting: [{ms['id']}] {ms['title']}")
    if reason:
        print(f"  Reason: {reason}")


def cmd_milestone_occupations():
    """List worktree_sessions / occupation log entries (ms-81 e-1921).

    Surfaces the audit trail recorded by milestone_record_occupation_event.
    Filtered with --ms <ms-id> to scope to one milestone; --json emits the
    raw shape for downstream tools (= Web UI audit tab in a later iteration
    can hit this endpoint via the standard project sync rather than a new
    subcollection plumbing).
    """
    ms_filter = os.environ.get("BEACON_MS_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    log = data.get("worktree_sessions", [])
    if ms_filter:
        log = [e for e in log if e.get("ms_id") == ms_filter]
    if json_mode:
        print(json.dumps(log, ensure_ascii=False))
        return
    if not log:
        print("(no occupation events recorded)")
        return
    for ev in log:
        ev_type = ev.get("event_type", "?")
        ev_ms = ev.get("ms_id", "?")
        ev_sid = (ev.get("session_id") or "?")[:14]
        ev_machine = ev.get("machine", "")
        ev_agent = ev.get("agent", "")
        ev_at = ev.get("at", "")
        ev_reason = ev.get("reason", "")
        actor_str = f"{ev_machine}/{ev_agent}" if ev_machine or ev_agent else "?"
        line = f"  {ev_at[:19]} [{ev_type:8}] {ev_ms:6} by {ev_sid}... ({actor_str})"
        if ev_reason:
            line += f"  — {ev_reason}"
        print(line)


def cmd_milestone_release():
    """Release the occupation claim on a milestone without changing status
    (ms-81 e-1918, SPEC AC #16).

    Use this when finishing a working session on an active MS so the next
    session can pick it up immediately. The MS stays in its current phase;
    only the per-session occupation marker is cleared.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    if not ms_id:
        print("Usage: beacon milestone release <ms-id>", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        _ms, released = core.milestone_release_occupation(
            data, ms_id, reason="manual"
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if released:
        sid = _resolve_session_id() or ""
        try:
            import agent as _agent_for_release
            actor = _agent_for_release.get_actor()
        except Exception:
            actor = {}
        core.milestone_record_occupation_event(
            data, ms_id=ms_id, event_type="release",
            session_id=sid,
            machine=actor.get("machine", ""),
            agent=actor.get("agent", ""),
            reason="manual",
        )
        save_project(data, op={"op": "milestone_release", "ms_id": ms_id})
        prev_sid = released.get("session_id", "?")
        print(
            f"Released: [{ms_id}] (was claimed by session "
            f"{prev_sid[:12] if prev_sid else '?'}...)"
        )
    else:
        print(f"Released: [{ms_id}] (was not occupied; no-op)")


def cmd_milestone_observe():
    """Transition a milestone to ``observing`` status with --reason gating
    (e-976).

    Previously `beacon milestone observe` dispatched to ``milestone_update``
    with ``BEACON_STATUS=observing`` and accepted an optional reason. With
    the gate, observing is now a first-class transition verb that must record
    why the work paused (so retro can reconstruct the decision). The reason
    is forwarded through ``core.milestone_update`` and ends up at
    ``meta.observing_reason``, identical to the previous path; only the gate
    is new.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = _require_reason_or_skip("milestone observe")
    if not ms_id:
        print("Usage: beacon milestone observe <ms-id> --reason <text>",
              file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms = core.milestone_update(data, ms_id, status="observing",
                                   reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _release_occupation_for_transition(data, ms_id, reason="observe")
    save_project(data, op={"op": "milestone_observe", "ms_id": ms_id,
                           "reason": reason})
    _prompt_close_leftover_worktree(ms_id, "observe")
    print(f"Observing: [{ms['id']}] {ms['title']}")
    if reason:
        print(f"  Reason: {reason}")


def cmd_milestone_join():
    """Add the current actor as an assignee on an MS (ms-51 / e-933).

    Mirror of the assignee-add step of ``ms start``, but without flipping
    status (the MS is presumed already active or otherwise in a healthy
    state). With ``--checkout`` (BEACON_CHECKOUT=1) the MS branch is also
    switched-to, with a fetch-from-origin fallback if the branch only
    exists on the remote.

    Per SPEC AC-3, duplicate joins are a warned no-op rather than a
    hard error — joining a board you're already on is a benign user
    intent ("am I on this?") and a hard error would be hostile.
    Per SPEC AC-4, done / cancelled milestones refuse the join; that
    rejection is raised by core.milestone_assignee_add.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    do_checkout = os.environ.get("BEACON_CHECKOUT", "") == "1"

    if not ms_id:
        print("Usage: beacon ms join <ms-id> [--checkout]", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # Find the MS up front so we can read its title (needed for slug + the
    # human-facing "joined X" message) and so we can fail fast with a
    # clearer error than "Milestone not found" from the deep helper.
    ms = next((m for m in data.get("milestones", []) if m.get("id") == ms_id), None)
    if ms is None:
        print(f"Error: Milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    # Resolve the actor name through the same lib/agent.py contract used
    # by ms start; keeps the two paths trivially consistent.
    import agent as _agent
    actor = _agent.get_actor()
    actor_str = actor.get("agent", "") or actor.get("machine", "")
    if not actor_str:
        print("Error: could not resolve agent identity (lib/agent.py)",
              file=sys.stderr)
        sys.exit(1)

    try:
        _ms, added = core.milestone_assignee_add(data, ms_id, actor_str)
    except ValueError as e:
        # done / cancelled MS or other validation failure — surface as 1.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    save_project(data)

    if added:
        print(f"Joined: [{ms_id}] {ms.get('title','')} as {actor_str}")
    else:
        print(f"Already on [{ms_id}] as {actor_str} (no-op)")

    # ---- optional --checkout ----
    if not do_checkout:
        return

    try:
        import branch as _branch
        branch_name = _branch.ms_branch_name(ms_id, ms.get("title", ""))
    except Exception as e:
        print(f"  warning: could not derive branch name: {e}", file=sys.stderr)
        return

    try:
        # 1. Try a local-only ensure first. If the branch is here we just
        #    switch; nothing else to do.
        msg = _ensure_on_branch(branch_name)
        print(f"  branch: {branch_name} ({msg})")
        return
    except _NotAGitRepoError:
        print("  warning: not in a git repo; --checkout skipped",
              file=sys.stderr)
        return
    except Exception:
        # Fall through to remote fetch attempt.
        pass

    # 2. Local create failed (most likely the branch only exists on origin).
    #    Try to fetch + checkout the remote branch.
    fetch = subprocess.run(
        ["git", "fetch", "origin", branch_name],
        capture_output=True, text=True, timeout=30,
    )
    if fetch.returncode != 0:
        print(f"  warning: fetch origin {branch_name} failed; "
              f"branch may not exist yet on remote", file=sys.stderr)
        return
    co = subprocess.run(
        ["git", "checkout", "-B", branch_name,
         f"origin/{branch_name}"],
        capture_output=True, text=True, timeout=10,
    )
    if co.returncode == 0:
        print(f"  branch: {branch_name} (tracked from origin)")
    else:
        print(f"  warning: checkout {branch_name} failed: "
              f"{co.stderr.strip()}", file=sys.stderr)


def cmd_milestone_show():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            if json_mode:
                output = {
                    "id": ms["id"],
                    "title": ms.get("title", ""),
                    "description": ms.get("description", ""),
                    "status": ms.get("status", "todo"),
                    "progress": ms.get("progress", 0),
                    "target_date": ms.get("target_date", ""),
                    "total_tasks": total_tasks,
                    "done_tasks": done_tasks,
                    "entries": core.entries_to_json(entries),
                }
                for f in ("priority", "objective", "acceptance_criteria"):
                    if ms.get(f):
                        output[f] = ms[f]
                print(json.dumps(output, ensure_ascii=False))
            else:
                icons = {"done": "\u25cf", "in_progress": "\u25d1", "todo": "\u25cb",
                         "waiting": "\u25cc", "in_review": "\u25d5", "cancelled": "\u2718"}
                icon = icons.get(ms["status"], "?")
                print(f"{icon} [{ms['id']}] {ms['title']}")
                if ms.get("description"):
                    print(f"  {ms['description']}")
                print(f"  Status: {ms['status']}  Progress: {ms.get('progress', 0)}%")
                print(f"  Target: {ms.get('target_date') or '-'}")
                print(f"  Tasks: {done_tasks}/{total_tasks} done")
            return

    print(f"Milestone not found: {ms_id}")
    sys.exit(1)


def cmd_milestone_update():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    status = os.environ.get("BEACON_STATUS", "")
    reason = os.environ.get("BEACON_REASON", "")

    # e-630: changing MS status or owner/assignee is a decision worth
    # recording. Require --reason so the changelog has a "why", not just
    # a "what". Other fields (title, description, etc.) are content edits
    # and don't need a reason — those are equivalent to documentation
    # updates and would just create noise if forced.
    decision_fields = bool(
        status
        or os.environ.get("BEACON_OWNER", "")
        or os.environ.get("BEACON_ASSIGNEE", "")
    )
    if decision_fields and not reason:
        print(
            "Error: --reason is required when changing status / owner / "
            "assignee. These are team-visible decisions and the decision "
            "trail must record the 'why' (CORE doc data-immutability-principle).",
            file=sys.stderr,
        )
        print(
            "  Example: beacon milestone update ms-42 --status observing "
            "--reason 'merge after ms-43 lands'",
            file=sys.stderr,
        )
        sys.exit(1)

    data = load_project()
    try:
        ms = core.milestone_update(
            data, ms_id,
            title=os.environ.get("BEACON_TITLE", ""),
            progress=os.environ.get("BEACON_PROGRESS", ""),
            target_date=os.environ.get("BEACON_TARGET_DATE", ""),
            status=status,
            description=os.environ.get("BEACON_DESCRIPTION", ""),
            reason=reason,
            priority=os.environ.get("BEACON_PRIORITY", ""),
            objective=os.environ.get("BEACON_OBJECTIVE", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            owner=os.environ.get("BEACON_OWNER", ""),
            assignee=os.environ.get("BEACON_ASSIGNEE", ""),
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    changelog_op = {"op": f"milestone_{status}", "ms_id": ms_id, "reason": reason} if status else None
    save_project(data, op=changelog_op)
    if json_mode:
        print(json.dumps({"id": ms["id"], "title": ms["title"],
                          "status": ms["status"], "progress": ms.get("progress", 0)},
                         ensure_ascii=False))
    else:
        print(f"Updated: [{ms['id']}] {ms['title']}")


def cmd_milestone_delete():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not reason:
        print("Error: --reason is required for milestone delete.")
        print("  Example: beacon milestone delete <ms-id> --reason \"スコープアウト: 別MSに統合\"")
        sys.exit(1)
    data = load_project()
    try:
        ms = core.milestone_delete(data, ms_id, reason=reason)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data, op={"op": "milestone_delete", "ms_id": ms_id, "reason": reason})
    if json_mode:
        print(json.dumps({"id": ms["id"], "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{ms['id']}] {ms['title']}")
        print(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# Cloud-mode purge helpers (e-1030)
#
# Local purge mutates `.beacon/project.json` directly via load_project_unsafe
# + save_project_unsafe. Cloud purge instead calls the server's `POST
# .../purge` endpoint, which enforces owner-only access. The pre-flight
# duplicate listing is kept symmetric with local UX so the operator sees the
# same "re-run with --index <n>" hint regardless of mode.
# ---------------------------------------------------------------------------

def _cloud_purge_403_message(role_word: str) -> str:
    """Translate a server 403 into a CLI-friendly error string."""
    return (
        f"Error: only the project owner can purge {role_word} "
        "(owner access required). Ask the project owner to run "
        "this command, or transfer ownership first."
    )


def _cloud_fetch_project_or_exit(client, project_id: str) -> dict:
    """Fetch the cloud project for pre-flight; exit 1 on error."""
    try:
        return client.get_project(project_id)
    except (RuntimeError, ConnectionError) as e:
        print(f"Error: cannot read cloud project: {e}", file=sys.stderr)
        sys.exit(1)


def _cloud_purge_dispatch(action_label: str, fn, *,
                          json_mode: bool, success_fmt) -> None:
    """Call a purge API method and render success / failure output.

    Args:
        action_label: short label for error messages ("milestone", etc).
        fn: zero-arg callable that performs the API request.
        json_mode: BEACON_JSON=1 toggle.
        success_fmt: callable(result) → list[str] of lines for human output.
    """
    try:
        result = fn()
    except RuntimeError as e:
        msg = str(e)
        if "403" in msg:
            print(_cloud_purge_403_message(action_label + "s"), file=sys.stderr)
            sys.exit(1)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        out = dict(result)
        out["purged"] = True
        print(json.dumps(out, ensure_ascii=False))
    else:
        for line in success_fmt(result):
            print(line)


def cmd_milestone_purge():
    """Hard-delete a milestone record (Issue #14 recovery path).

    Unlike `beacon milestone delete` (soft / status=cancelled), purge
    physically removes the record from the array. This exists only for
    data-corruption recovery and is not a substitute for soft delete.

    Loads via load_project_unsafe so it can recover from a project that
    already fails validation (the whole point of the command). The save
    path also bypasses validation when residual duplicates remain so
    the operator can purge them one at a time; once clean, validation
    passes naturally on the next normal load.

    Cloud mode (e-1030): routes through the server `POST .../purge` endpoint
    which enforces owner-only access. Editors get a clear 403 instead of
    silently mutating the local cache and pushing.
    """
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: ms-id is required.", file=sys.stderr)
        print(
            "  Usage: beacon milestone purge <ms-id> --reason \"...\" [--index <n>]",
            file=sys.stderr,
        )
        sys.exit(1)
    if not reason:
        print(
            "Error: --reason is required for milestone purge "
            "(audit trail per CORE doc data-immutability-principle).",
            file=sys.stderr,
        )
        print(
            "  Example: beacon milestone purge ms-13 "
            "--reason \"duplicate ID — Issue #14 recovery\" --index 2",
            file=sys.stderr,
        )
        sys.exit(1)
    index: Optional[int] = None
    if index_str:
        try:
            index = int(index_str)
        except ValueError:
            print(f"Error: --index must be an integer, got '{index_str}'.",
                  file=sys.stderr)
            sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.purge_milestone unifies the cloud + local
    # paths. The CLI no longer branches on ``_is_cloud_mode()``; the Store
    # implementation knows how to talk to its backend (cloud server enforces
    # owner-only access + post-purge validation; LocalStore does the local
    # file mutation + still_dirty bookkeeping). Pre-flight stays here as a
    # UX layer (= friendly duplicate display before delegating).
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_milestones(data, ms_id)
    if not matches:
        print(f"Milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(
            f"Milestone '{ms_id}' has {len(matches)} duplicate records. "
            "Re-run with --index <n>:",
            file=sys.stderr,
        )
        for i, m in enumerate(matches, 1):
            title = m.get("title", "(no title)")
            status = m.get("status", "?")
            print(f"  --index {i}  status={status}  title={title}",
                  file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_milestone(ms_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    # Local-mode changelog parity: the cloud server records its own audit
    # trail via operations.apply_operation, so we only append to
    # .beacon/changelog.jsonl when LocalStore did the mutation.
    if not store.is_cloud():
        _append_changelog({
            "op": "milestone_purge",
            "ms_id": ms_id,
            "index": index,
            "reason": reason,
            "purged_title": purged.get("title", ""),
        })

    if json_mode:
        out = {
            "id": purged["id"],
            "title": purged.get("title", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Purged: [{purged['id']}] {purged.get('title', '')}")
        print(f"  Reason: {reason}")
        if still_dirty:
            remaining: list[str] = []
            for category, dupes in dup_report.items():
                for did, n in dupes.items():
                    remaining.append(f"{category[:-1]} '{did}' x{n}")
            print("  Note: residual duplicates remain — "
                  + ", ".join(remaining))
            print("  Run `beacon doctor` to inspect and purge the next one.")


def _print_residual_dups(dup_report: dict) -> None:
    """Shared tail for *_purge commands: report duplicates still present."""
    remaining: list[str] = []
    for category, dupes in dup_report.items():
        for did, n in dupes.items():
            remaining.append(f"{category[:-1]} '{did}' x{n}")
    print("  Note: residual duplicates remain — " + ", ".join(remaining))
    print("  Run `beacon doctor` to inspect and purge the next one.")


def cmd_entry_purge():
    """Hard-delete an entry record (e-N) — duplicate-ID recovery (e-863).

    The entry-level analogue of cmd_milestone_purge. Loads via
    load_project_unsafe so it works on a project that fails validation, and
    falls back to an unsafe save while residual duplicates remain.

    Cloud mode (e-1030): routes through the server purge endpoint, which is
    owner-only.
    """
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry-id is required.", file=sys.stderr)
        print("  Usage: beacon entry purge <e-id> --reason \"...\" [--index <n>]",
              file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for entry purge "
              "(audit trail per CORE doc data-immutability-principle).",
              file=sys.stderr)
        sys.exit(1)
    index: Optional[int] = None
    if index_str:
        try:
            index = int(index_str)
        except ValueError:
            print(f"Error: --index must be an integer, got '{index_str}'.",
                  file=sys.stderr)
            sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.purge_entry unifies cloud + local paths.
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_entries(data, entry_id)
    if not matches:
        print(f"Entry not found: {entry_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(f"Entry '{entry_id}' has {len(matches)} duplicate records. "
              "Re-run with --index <n>:", file=sys.stderr)
        for i, e in enumerate(matches, 1):
            desc = e.get("description", "(no description)")
            etype = e.get("type", "?")
            print(f"  --index {i}  type={etype}  desc={desc[:60]}", file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_entry(entry_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    if not store.is_cloud():
        _append_changelog({
            "op": "entry_purge",
            "entry_id": entry_id,
            "index": index,
            "reason": reason,
            "purged_desc": purged.get("description", ""),
        })

    if json_mode:
        print(json.dumps({
            "id": purged.get("id", entry_id),
            "description": purged.get("description", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }, ensure_ascii=False))
    else:
        print(f"Purged entry: [{purged.get('id', entry_id)}] "
              f"{purged.get('description', '')[:80]}")
        print(f"  Reason: {reason}")
        if still_dirty:
            _print_residual_dups(dup_report)


def cmd_operation_purge():
    """Hard-delete an operation record (op-N) — duplicate-ID recovery (e-863).

    The operation-level analogue of cmd_milestone_purge.

    Cloud mode (e-1030): routes through the server purge endpoint, which is
    owner-only.
    """
    op_id = os.environ.get("BEACON_OP_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    index_str = os.environ.get("BEACON_INDEX", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not op_id:
        print("Error: op-id is required.", file=sys.stderr)
        print("  Usage: beacon operation purge <op-id> --reason \"...\" [--index <n>]",
              file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for operation purge "
              "(audit trail per CORE doc data-immutability-principle).",
              file=sys.stderr)
        sys.exit(1)
    index: Optional[int] = None
    if index_str:
        try:
            index = int(index_str)
        except ValueError:
            print(f"Error: --index must be an integer, got '{index_str}'.",
                  file=sys.stderr)
            sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.purge_operation unifies cloud + local.
    store = get_store()
    try:
        data = store.load_project()
    except (RuntimeError, ConnectionError) as e:
        print(f"Error loading project: {e}", file=sys.stderr)
        sys.exit(1)
    matches = core.find_operations(data, op_id)
    if not matches:
        print(f"Operation not found: {op_id}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1 and index is None:
        print(f"Operation '{op_id}' has {len(matches)} duplicate records. "
              "Re-run with --index <n>:", file=sys.stderr)
        for i, o in enumerate(matches, 1):
            title = o.get("title", "(no title)")
            status = o.get("status", "?")
            print(f"  --index {i}  status={status}  title={title}", file=sys.stderr)
        sys.exit(1)

    try:
        result = store.purge_operation(op_id, reason=reason, index=index)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    purged = result["purged"]
    still_dirty = result["still_dirty"]
    dup_report = result["dup_report"]

    if not store.is_cloud():
        _append_changelog({
            "op": "operation_purge",
            "op_id": op_id,
            "index": index,
            "reason": reason,
            "purged_title": purged.get("title", ""),
        })

    if json_mode:
        print(json.dumps({
            "id": purged.get("id", op_id),
            "title": purged.get("title", ""),
            "purged": True,
            "still_dirty": still_dirty,
        }, ensure_ascii=False))
    else:
        print(f"Purged operation: [{purged.get('id', op_id)}] {purged.get('title', '')}")
        print(f"  Reason: {reason}")
        if still_dirty:
            _print_residual_dups(dup_report)


# ---------------------------------------------------------------------------
# Log commands
# ---------------------------------------------------------------------------

def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    behavior = os.environ.get("BEACON_BEHAVIOR", "")
    resolves = os.environ.get("BEACON_RESOLVES", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-51 / e-934: attach actor (see cmd_log_finalize for the matching
    # detail). Done in both entry points because beacon log is exposed
    # directly to users (`beacon log "msg"`) and via the post-commit hook.
    try:
        import agent as _agent
        actor = _agent.get_actor()
    except Exception:
        actor = None

    session_id = _resolve_session_id()  # ms-57 / e-1062
    # ms-79 / e-1817 (UC3-F3): detect envelope context to tag auto-op
    # commits. The source label is stored on meta.source so retrospect /
    # retro can filter "AI 自律でやった commit だけ".
    source = _resolve_commit_source()

    # ms-79 / e-1816 (UC3-F2): fork.json の target_ms_id を最優先する。
    # 親が --ms を明示しているならそれが勝つ (= fork.json は hint であって
    # 命令ではない、cmd_log_prepare と同じセマンティクスを揃える)。
    if not ms_id:
        fork_target = _read_fork_target_ms_id()
        if fork_target:
            # fork target_ms_id が project.json にあるか軽くチェック。無ければ
            # 普通の auto-pick (find_target_milestone) に倒れる。
            data_check = load_project()
            if any(m.get("id") == fork_target for m in data_check.get("milestones", [])):
                ms_id = fork_target

    data = load_project()
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary, progress=progress,
        behavior=behavior, resolves=resolves, actor=actor,
        session_id=session_id, source=source,
    )
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "duplicate":
        print(f"Already logged: {commit_hash}")
    else:
        loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
        print(f"Logged {commit_hash} to {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")


def cmd_log_prepare():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")

    data = load_project()

    # ms-79 / e-1816 (UC3-F2): fork.json の target_ms_id を最優先する。
    # ms-67 で fork した子 worktree は明示意図 (= 親が「この MS をやれ」と
    # 指示した target_ms_id) を持つ。fork.json があれば、--ms 明示が無い
    # ときの候補選定の前にそれを優先採用する。これにより「fork 子で
    # commit したら親 MS に誤記録」 のドリフトを構造的に防ぐ。
    fork_target_ms_id = ""
    if not ms_id:
        fork_target_ms_id = _read_fork_target_ms_id()

    effective_ms_id = ms_id or fork_target_ms_id

    if effective_ms_id:
        for ms in data["milestones"]:
            if ms["id"] == effective_ms_id:
                targets = [ms]
                break
        else:
            # fork.json が指す ms_id がローカル project.json に無い場合
            # (= stale fork、活性化前 / 削除済 MS) は普通の active 候補に
            # fallback。explicit --ms ms_id が無効なときと違って
            # silent fallback でよい (= fork.json は hint であって命令ではない)。
            if fork_target_ms_id and not ms_id:
                targets = [ms for ms in data["milestones"]
                           if ms["status"] in ("todo", "in_progress", "observing")]
                if not targets:
                    print("No active milestone. Run: beacon milestone start <ms-id>")
                    sys.exit(1)
            else:
                print(f"Milestone not found: {effective_ms_id}")
                sys.exit(1)
    else:
        targets = [ms for ms in data["milestones"] if ms["status"] in ("todo", "in_progress", "observing")]
        if not targets:
            print("No active milestone. Run: beacon milestone start <ms-id>")
            sys.exit(1)

    output = {
        "commit": {"hash": commit_hash, "message": message, "date": date, "summary": summary_text},
        "current_summary": data.get("summary", ""),
    }
    # e-1816: 透明性のため fork.json 由来であることを payload に明示する。
    # /beacon-log Skill はこれを見て「fork 由来の active MS を採用しています」
    # と user に notice を出せる (= 暗黙の選定で誤解されるのを防ぐ)。
    if fork_target_ms_id and not ms_id:
        output["fork_target_ms_id"] = fork_target_ms_id

    if len(targets) == 1:
        output["milestone"] = core.milestone_prepare_info(targets[0])
    else:
        output["candidates"] = [core.milestone_prepare_info(ms) for ms in targets]

    print(json.dumps(output, ensure_ascii=False))


def _read_fork_target_ms_id() -> str:
    """Return the ``target_ms_id`` from ``.beacon/fork.json``, or "".

    Fork worktrees are created by /beacon-session-fork (ms-67) and carry
    a fork.json next to project.json that records the intent (= "this
    worktree exists to work on ms-X"). cmd_log_prepare consults this
    before falling back to the active-MS heuristic so commits made from
    a fork worktree route to the milestone the fork was created for —
    even if the parent repo has multiple active milestones (which would
    otherwise force a candidates / picker dialog the human did not
    intend in this child session).

    Returns "" when:
      - no fork.json exists (= regular worktree / main checkout)
      - fork.json is malformed (= treat as no fork hint, don't crash)
      - target_ms_id field is missing or empty

    Reads from ``$(dirname project.json)/fork.json`` so it stays
    consistent with how every other beacon helper resolves its
    ``.beacon/`` directory.
    """
    try:
        project_file = get_project_file()
        fork_path = os.path.join(os.path.dirname(project_file), "fork.json")
        if not os.path.exists(fork_path):
            return ""
        with open(fork_path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if not isinstance(rec, dict):
            return ""
        target = rec.get("target_ms_id")
        if isinstance(target, str) and target.strip():
            return target.strip()
        return ""
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def cmd_log_finalize():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    # e-1040: project-level summary writes are deprecated. We still read
    # BEACON_NEW_SUMMARY so we can warn the caller (typically the legacy
    # /beacon-log Skill or a stale script), but we no longer mutate
    # data["summary"]. Human narrative lives in the project-vision CORE
    # doc; session-scoped context lives in the session_logs subcollection.
    legacy_new_summary = os.environ.get("BEACON_NEW_SUMMARY", "")
    behavior = os.environ.get("BEACON_BEHAVIOR", "")
    resolves = os.environ.get("BEACON_RESOLVES", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-51 / e-934: attach actor metadata so multi-machine commits are
    # distinguishable downstream. lib.agent.get_actor is the single source
    # of truth for this (configured via .beacon/agent.json + env vars).
    try:
        import agent as _agent
        actor = _agent.get_actor()
    except Exception:
        actor = None

    session_id = _resolve_session_id()  # ms-57 / e-1062
    # ms-79 / e-1817 (UC3-F3): tag the commit's source axis (= human dialog
    # vs auto-op envelope execution). Retrospect / retro can then filter
    # "AI 自律 commit だけ見たい" type queries.
    source = _resolve_commit_source()

    data = load_project()
    # ms-81 e-1916: status gate. Resolve the target MS the same way log_commit
    # would internally, then surface the warning before mutating state.
    try:
        target_ms = core.find_target_milestone(data, ms_id)
    except ValueError:
        target_ms = None
    if target_ms is not None:
        if not _check_ms_status_for_write(
            target_ms, f"log commit {commit_hash[:7]}"
        ):
            sys.exit(1)
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary_text, progress=progress,
        behavior=behavior, resolves=resolves, actor=actor,
        session_id=session_id, source=source,
    )

    # e-1040 deprecation: don't write data["summary"] anymore. The legacy
    # path used to set it from BEACON_NEW_SUMMARY; that field is now
    # ignored at write time. Callers should switch to project-vision
    # CORE doc updates (human narrative) or rely on session_logs
    # subcollection (session context).
    summary_was_ignored = bool(legacy_new_summary)

    save_project(data)

    if json_mode:
        result["summary_updated"] = False  # always False post-e-1040
        if summary_was_ignored:
            result["summary_deprecated"] = True
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Already logged: {commit_hash} (updated progress)")
        else:
            loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
            print(f"Logged {commit_hash} → {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")
        if summary_was_ignored:
            # stderr so JSON consumers / pipes don't break; interactive
            # users still see it. Mirrors the pattern in cmd_summary.
            sys.stderr.write(
                "[deprecated] --summary was provided but is no longer written to "
                "the project. The project summary path was retired in e-1040 — "
                "use the `project-vision` CORE doc for human narrative and the "
                "session_logs subcollection for per-session context. This "
                "argument will be removed in a future release.\n"
            )


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def cmd_sync():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    entries = target.setdefault("entries", [])
    existing_hashes = set()
    for entry in entries:
        if entry.get("type") == "commit":
            h = entry.get("meta", {}).get("hash", "")
            if h:
                existing_hashes.add(h[:7])

    result = subprocess.run(
        ["git", "log", "--oneline", "-20", "--pretty=format:%h|%s|%ci"],
        capture_output=True, text=True,
    )

    added = 0
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        h, msg, date_str = parts
        if h not in existing_hashes:
            commit_date = date_str.split(" ")[0]
            entries.insert(0, {
                "id": core.next_entry_id(data),
                "type": "commit",
                "description": msg,
                "date": commit_date,
                "created_at": commit_date,
                "done_at": commit_date,
                "status": "done",
                "meta": {"hash": h, "message": msg},
            })
            added += 1

    if added:
        save_project(data)
        print(f"Synced {added} new commits to: {target['title']}")
    else:
        print("No new commits to sync.")


# ---------------------------------------------------------------------------
# Task commands
# ---------------------------------------------------------------------------

def cmd_task_add():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    entry_type = os.environ.get("BEACON_TYPE", "task")
    date = os.environ.get("BEACON_DATE", "")
    detail = os.environ.get("BEACON_DETAIL", "")
    requested_by = os.environ.get("BEACON_REQUESTED_BY", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    motivation = os.environ.get("BEACON_MOTIVATION", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")

    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    # ms-81 e-1919: re-open prompt for done MS. Adding a task to a done
    # milestone creates a zombie (= the e-1916 write gate then blocks any
    # commit/done against it, so it would stay in todo forever). Per SPEC
    # §5 the right move is to surface the choice: re-open into observing
    # (the natural recovery slot) or active, or abort. Interactive only;
    # in the non-interactive Skill / hook path we proceed with a warning
    # so the Skill report can flag the audit trail rather than block.
    if target.get("status") == "done":
        if sys.stdin.isatty():
            print(
                f"\n[ms-81 re-open prompt] [{target['id']}] {target['title']} "
                f"is done. Adding a task here will leave it stuck (the "
                f"write gate blocks commit / done on done milestones).",
                file=sys.stderr,
            )
            print(
                "   Options: (o) re-open as observing, (a) re-open as active, "
                "(s) skip prompt and add anyway, (n) abort",
                file=sys.stderr,
            )
            try:
                choice = input("   Choice [o/a/s/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "n"
            if choice == "o":
                core.milestone_update(
                    data, ms_id, status="observing",
                    reason="re-opened to add task",
                )
                print(f"  re-opened {ms_id} as observing", file=sys.stderr)
            elif choice == "a":
                core.milestone_start(data, ms_id)
                print(f"  re-opened {ms_id} as active", file=sys.stderr)
            elif choice in ("", "n"):
                print("  aborted (no task added)", file=sys.stderr)
                sys.exit(1)
            # "s" falls through and adds the task without changing status
        else:
            print(
                f"[ms-81 re-open warning] adding to done MS [{target['id']}] "
                f"{target['title']} — task will be stuck (write gate blocks "
                f"future commits / done). Re-open with `beacon milestone start "
                f"{ms_id}` or `beacon milestone observe {ms_id}` first.",
                file=sys.stderr,
            )

    eid = core.task_add(data, ms_id, description, entry_type=entry_type,
                        date=date, detail=detail, requested_by=requested_by,
                        priority=priority, motivation=motivation,
                        acceptance_criteria=acceptance_criteria)
    save_project(data)
    from_str = f" (from {requested_by})" if requested_by else ""
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}{from_str}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    reason = _require_reason_or_skip("task done")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = load_project()
    # ms-81 e-1916: status gate. Look up the entry's parent MS first so we
    # can warn if the MS isn't write-authorised. The PR-merge sub-branch
    # below also goes through the same gate.
    result = core.find_entry(data, entry_id)
    if result:
        parent_ms, _, entry, _ = result
        if not _check_ms_status_for_write(
            parent_ms, f"task done {entry_id}"
        ):
            sys.exit(1)
        if entry.get("type") == "pr":
            pr_status = entry.get("meta", {}).get("pr_status", "")
            if pr_status not in ("approved", "merged"):
                print(
                    f"Warning: PR [{entry_id}] has pr_status='{pr_status}'. "
                    "Use 'beacon pr merge' to merge explicitly, or 'beacon pr close' to close without merging.",
                    file=sys.stderr,
                )
            ms, entry = core.pr_merge(data, entry_id, date=today)
            print(f"Merged PR [{entry_id}]: {entry['description']}")
            core.update_progress(ms, progress)
            if progress:
                print(f"  Progress: {ms.get('progress', 0)}%")
            save_project(data)
            return
    ms, entry = core.task_done(data, entry_id, date=today, reason=reason)
    print(f"Done: [{entry_id}] {entry['description']}")
    if reason:
        print(f"  Reason: {reason}")
    core.update_progress(ms, progress)
    if progress:
        print(f"  Progress: {ms.get('progress', 0)}%")
    save_project(data, op={"op": "task_done", "entry_id": entry_id, "reason": reason})
    issue_number = entry.get("meta", {}).get("issue_number")
    if issue_number:
        print(f"  Linked Issue: #{issue_number} — close it with: gh issue close {issue_number}")


def cmd_task_list():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    show_all = os.environ.get("BEACON_ALL", "") == "1"
    type_filter = os.environ.get("BEACON_TYPE_FILTER", "")
    data = load_project()
    target = core.find_target_milestone(data, ms_id)

    entries = core.filter_cancelled(target.get("entries", []), show_all)

    if type_filter:
        entries = [e for e in entries if e.get("type") == type_filter]

    if json_mode:
        output = {
            "milestone_id": target["id"],
            "milestone_title": target.get("title", ""),
            "entries": core.entries_to_json(entries),
        }
        print(json.dumps(output, ensure_ascii=False))
        return

    if not entries:
        print(f"No entries in {target['title']}")
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "cancelled": "\u2718"}
    for entry in entries:
        icon = icons.get(entry.get("status", "todo"), "?")
        etype = entry.get("type", "?")
        print(f"  {icon} [{entry['id']}] ({etype}) {entry['description']}")


def cmd_task_show():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    result = core.find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    ms, _, entry, _ = result

    if json_mode:
        output = {"milestone_id": ms["id"], "milestone_title": ms.get("title", "")}
        entry_json = core.entries_to_json([entry])[0]
        output.update(entry_json)
        print(json.dumps(output, ensure_ascii=False))
        return

    icons = {"done": "\u25cf", "todo": "\u25cb", "in_progress": "\u25d1",
             "waiting": "\u25cc", "in_review": "\u25d5", "observing": "\u25d5",
             "cancelled": "\u2718"}
    icon = icons.get(entry.get("status", "todo"), "?")
    print(f"{icon} [{entry['id']}] {entry.get('description', '')}")
    print(f"  Milestone: [{ms['id']}] {ms['title']}")
    print(f"  Type: {entry.get('type', '?')}  Status: {entry.get('status', '?')}")
    priority = entry.get("meta", {}).get("priority", "")
    if priority:
        print(f"  Priority: {priority}")
    print(f"  Created: {entry.get('created_at', '-')}  Done: {entry.get('done_at', '-')}")
    if entry.get("motivation"):
        print(f"  Why: {entry['motivation']}")
    if entry.get("acceptance_criteria"):
        print(f"  Done when: {entry['acceptance_criteria']}")
    if entry.get("behavior"):
        print(f"  Behavior: {entry['behavior']}")
    # Show PR-specific fields
    if entry.get("type") == "pr":
        meta = entry.get("meta", {})
        print(f"  PR status: {meta.get('pr_status', '-')}  Review: {meta.get('review_status', '-')}")
        if meta.get("url"):
            print(f"  URL: {meta['url']}")
        if meta.get("intent"):
            print(f"  Intent: {meta['intent']}")
        if meta.get("review_rationale"):
            print(f"  Rationale: {meta['review_rationale']}")
        if meta.get("author"):
            print(f"  Author: {meta['author']}")
    detail = entry.get("detail", "")
    if detail:
        print(f"\n{detail}")


def cmd_task_detail():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    detail = os.environ.get("BEACON_DETAIL", "")
    data = load_project()
    result = core.find_entry(data, entry_id)
    if not result:
        print(f"Entry not found: {entry_id}")
        sys.exit(1)
    _, _, entry, _ = result
    if detail:
        entry["detail"] = detail
        save_project(data)
        print(f"Updated detail for [{entry_id}] {entry.get('description', '')}")
    else:
        print(entry.get("detail", "(no detail)"))


def cmd_task_update():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    ms_id = os.environ.get("BEACON_MS_ID", "")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = load_project()
    try:
        if ms_id:
            core.entry_move(data, entry_id, ms_id=ms_id)
        ms, entry = core.task_update(
            data, entry_id,
            description=os.environ.get("BEACON_DESCRIPTION", ""),
            status=os.environ.get("BEACON_STATUS", ""),
            detail=os.environ.get("BEACON_DETAIL", ""),
            date=today,
            motivation=os.environ.get("BEACON_MOTIVATION", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            behavior=os.environ.get("BEACON_BEHAVIOR", ""),
            priority=os.environ.get("BEACON_PRIORITY", ""),
        )
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps(core.entries_to_json([entry])[0], ensure_ascii=False))
    else:
        print(f"Updated: [{entry_id}] {entry.get('description', '')}")


def cmd_task_delete():
    print("Error: 'beacon task delete' is deprecated. Use 'beacon task cancel' instead.")
    print("  Physical deletion is not allowed — all cancellations are soft and traceable.")
    print("  Example: beacon task cancel <entry-id> --reason \"重複タスクのため\"")
    sys.exit(1)


def cmd_task_cancel():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    try:
        entry = core.task_delete(data, entry_id, reason=reason)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data, op={"op": "task_cancel", "entry_id": entry_id, "reason": reason})
    if json_mode:
        print(json.dumps({"id": entry_id, "status": "cancelled"}, ensure_ascii=False))
    else:
        print(f"Cancelled: [{entry_id}] {entry.get('description', '')}")
        if reason:
            print(f"  Reason: {reason}")


def cmd_entry_move():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    task_id = os.environ.get("BEACON_TASK_ID", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")

    if not entry_id or (not task_id and not ms_id):
        print("Usage: beacon entry move <entry-id> -t <task-id> | -m <ms-id>")
        sys.exit(1)

    data = load_project()
    try:
        core.entry_move(data, entry_id, task_id=task_id, ms_id=ms_id)
    except ValueError as e:
        print(str(e))
        sys.exit(1)
    save_project(data)
    if ms_id:
        print(f"Moved [{entry_id}] to {ms_id}")
    else:
        print(f"Moved [{entry_id}] under [{task_id}]")


# ---------------------------------------------------------------------------
# Persistence poisoning defense (ms-54 / e-1293)
#
# Defense in depth on top of the bus envelope verify layer (e-1155 Phase 1).
# Even though the envelope tier blocks an inbound DM from auto-executing a
# Skill, an AI that *reads* a DM and then interprets the content as a user
# request and calls ``note add`` / ``doc add`` / ``session log`` itself is a
# different attack surface — the envelope guard never sees that call. To
# close the gap, the handlers themselves refuse any write whose source is
# marked as bus-origin via ``BEACON_BUS_ORIGIN=1`` (or ``--bus-origin``).
#
# Producers in the bus path SHOULD set this flag when forwarding
# bus-derived content into a persistence handler. The current codebase has
# no such producer yet — the flag is in place as a structural defense for
# future code paths and as a tripwire if attack-surface code ever appears.
# Legitimate user / AI-internal flows do not pass the flag, so this never
# fires for ordinary use.
#
# Rejected attempts are logged to a local audit jsonl
# (``.beacon/persistence_poisoning_audit.jsonl``) so the attempt remains
# forensically visible even without cloud connectivity. The local file is
# the source of truth for this defense — by design we do NOT round-trip
# through the cloud audit collection (a bus-origin caller is, by
# definition, not trusted to write audit records remotely).
# ---------------------------------------------------------------------------

PERSISTENCE_POISONING_AUDIT_FILE = "persistence_poisoning_audit.jsonl"

_BUS_ORIGIN_REFUSAL_MESSAGE = (
    "Error: writes from bus-origin payloads are not allowed "
    "(persistence poisoning defense)"
)


def _is_bus_origin_input() -> bool:
    """Return True iff the current invocation is marked as bus-derived.

    Read from ``BEACON_BUS_ORIGIN``. Truthy values are ``"1"`` and ``"true"``
    (case-insensitive). Everything else is treated as not-set so a
    misconfigured caller fails closed (i.e. allows the write) only when the
    flag is absent — never on a typo'd truthy value.
    """
    raw = os.environ.get("BEACON_BUS_ORIGIN", "").strip().lower()
    return raw in ("1", "true", "yes")


def _persistence_poisoning_audit_path() -> str:
    """Local jsonl path for refused bus-origin persistence attempts."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, PERSISTENCE_POISONING_AUDIT_FILE)


def _record_persistence_poisoning_refusal(handler: str, details: dict) -> None:
    """Append a refusal audit record to the local jsonl audit log.

    Best-effort: any IO error is swallowed (we still refuse the write — the
    audit log is forensic context, not the gate). The record schema is
    intentionally small and human-readable so an operator can grep for
    ``handler=note_add`` to see the trail.
    """
    import datetime
    try:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "handler": handler,
            "verdict": "refused",
            "reason": "bus_origin_persistence_blocked",
            "session_id": _resolve_session_id(),
            "details": details,
        }
        path = _persistence_poisoning_audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Audit logging is best-effort; never let it mask the refusal itself.
        pass


def _refuse_if_bus_origin(handler: str, details: dict) -> bool:
    """Refuse the current persistence call if marked bus-origin.

    Returns True iff the call was refused (caller should ``sys.exit(1)``
    immediately). When True, an audit record is written and the refusal
    message is printed to stderr.

    Handlers (``cmd_note_add``, ``cmd_doc_add``, ``cmd_doc_update``,
    ``cmd_session_end``) MUST call this BEFORE any persistence side effect.
    """
    if not _is_bus_origin_input():
        return False
    _record_persistence_poisoning_refusal(handler, details)
    print(_BUS_ORIGIN_REFUSAL_MESSAGE, file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# Session Notes (ephemeral, cleared at session-end)
# ---------------------------------------------------------------------------

def _get_notes_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "session_notes.jsonl")


def _push_note_to_cloud(note: dict) -> None:
    """Push a session note to cloud API. Best-effort: silently ignores all errors."""
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id", "")
        api_url = _resolve_active_api_url()
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        def _token():
            from auth import load_credentials as _lc
            c = _lc()
            return _extract_token(c) if c else ""
        client = ApiClient(api_url, _token)
        client.add_note(project_id, note)
    except Exception:
        pass


def cmd_note_add():
    import datetime
    text = os.environ.get("BEACON_NOTE_TEXT", "")
    context = os.environ.get("BEACON_NOTE_CONTEXT", "")
    if not text:
        print("Error: note text required")
        sys.exit(1)
    # ms-54 / e-1293: persistence poisoning defense — refuse writes whose
    # source is a bus DM. See module-level "Persistence poisoning defense"
    # block for the threat model.
    if _refuse_if_bus_origin(
        "note_add",
        {"text_preview": text[:80], "context": context},
    ):
        sys.exit(1)
    note = {
        "ts": datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "text": text,
    }
    if context:
        note["context"] = context
    # ms-57 / e-1036: tag the note with the current session_id so session-end
    # / rescue can aggregate it (notes WHERE session_id == X). Forward-only —
    # past notes stay untagged. Empty session_id is the "no session" sentinel
    # and is omitted, mirroring the commit/PR tagging convention (e-1062).
    session_id = _resolve_session_id()
    if session_id:
        note["session_id"] = session_id
    path = _get_notes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")
    _push_note_to_cloud(note)
    print(f"Note: {text[:60]}{'...' if len(text) > 60 else ''}")


def cmd_note_list():
    path = _get_notes_path()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    notes = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        notes.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    if json_mode:
        print(json.dumps(notes, ensure_ascii=False))
        return
    if not notes:
        print("(メモなし)")
        return
    for n in notes:
        ctx = f" [{n['context']}]" if n.get("context") else ""
        print(f"  {n['ts'][:16]}{ctx}: {n['text']}")


def cmd_note_clear():
    path = _get_notes_path()
    if os.path.exists(path):
        import shutil
        shutil.move(path, path + ".bak")
    try:
        config_path = _get_cloud_config_path()
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            project_id = config.get("project_id", "")
            api_url = _resolve_active_api_url()
            if project_id:
                from auth import load_credentials
                creds = load_credentials()
                if creds:
                    from api_client import ApiClient
                    def _token():
                        from auth import load_credentials as _lc
                        c = _lc()
                        return _extract_token(c) if c else ""
                    ApiClient(api_url, _token).clear_notes(project_id)
    except Exception:
        pass
    print("Session notes cleared.")


# ---------------------------------------------------------------------------
# Session log (ms-57 / e-1038 + e-1039)
# ---------------------------------------------------------------------------

def _session_logs_dir() -> str:
    """Local cache dir for session log entries (mirrors the cloud subcollection).

    In cloud mode the authoritative store is Firestore; we still write a
    local copy so list/show work offline. In local-only mode this is the
    primary store.
    """
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "session_logs")


def _session_log_path(session_id: str) -> str:
    """Slug-safe path for a session log file. Doc-id-safe characters only."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "unknown"
    return os.path.join(_session_logs_dir(), f"{safe}.json")


def _read_local_session_log(session_id: str) -> Optional[dict]:
    path = _session_log_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_local_session_log(payload: dict) -> None:
    """Merge-write a session log entry to the local cache.

    We merge (read-modify-write) so a partial rescue payload doesn't wipe
    fields a richer session-end already produced — mirrors the server's
    merge=True semantics on the cache layer too.
    """
    sid = payload.get("session_id", "")
    if not sid:
        return
    path = _session_log_path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = _read_local_session_log(sid) or {}
    existing.update(payload)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _push_session_log_to_cloud(payload: dict) -> bool:
    """Best-effort upsert via Store.upsert_session_log (ms-84 Phase 2 e-2036).

    LocalStore returns False unconditionally (= cloud session log subcollection
    has no local analogue); StoreApi calls the API and swallows transport
    failures the same way the legacy inline cloud branch did. The session
    log primary truth is the local cache when cloud is unreachable — a later
    run will re-aggregate and resync.
    """
    sid = payload.get("session_id", "")
    if not sid:
        return False
    body = {k: v for k, v in payload.items()
            if k != "session_id" and v is not None}
    try:
        return get_store().upsert_session_log(sid, body)
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()
        return False


def _list_other_session_ids() -> list:
    """Return session_ids (other than the current one) that have entries
    associated with them in the project data or local notes.

    Used by rescue (e-1039). Walks project entries' meta.session_id and
    the local notes jsonl. In cloud mode we additionally fetch the cloud
    session registry so cross-machine orphans are discovered.
    """
    seen = set()
    try:
        import session as _session
        current = _session.get_session_id()
    except Exception:
        current = ""

    try:
        data = load_project()
    except Exception:
        data = {"milestones": []}

    def _walk(entries):
        for e in entries or []:
            sid = (e.get("meta") or {}).get("session_id")
            if sid and sid != current:
                seen.add(sid)
            _walk(e.get("entries", []))

    for ms in data.get("milestones", []) or []:
        _walk(ms.get("entries", []))

    notes_path = _get_notes_path()
    if os.path.exists(notes_path):
        try:
            with open(notes_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        note = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = note.get("session_id")
                    if sid and sid != current:
                        seen.add(sid)
        except OSError:
            pass

    # ms-84 Phase 2 (e-2036): Store.list_session_ids unifies cloud + local.
    # LocalStore returns []; StoreApi returns the API list (swallowing
    # transport failures), so the union below does not branch on backend.
    try:
        for sid in get_store().list_session_ids():
            if sid and sid != current:
                seen.add(sid)
    except BaseException:
        if os.environ.get("BEACON_DEBUG") == "1":
            import traceback as _tb
            _tb.print_exc()

    return sorted(seen)


def _aggregate_and_persist(session_id: str, *, recovered: bool,
                            summary_override: Optional[str] = None) -> dict:
    """Run the aggregation core, write the local cache, push to cloud.

    Returns the persisted payload. Single helper used by both cmd_session_end
    (recovered=False) and cmd_session_rescue (recovered=True) so the
    idempotency / merge semantics live in one place.
    """
    import session_log as _slog
    from pathlib import Path as _Path
    beacon_dir = _Path(os.path.dirname(get_project_file()) or ".beacon")

    data = load_project()
    existing_local = _read_local_session_log(session_id)

    # ms-84 Phase 2 (e-2036): fetch any persisted remote session log via
    # Store.get_session_log so the merge step is backend-uniform. LocalStore
    # returns None; StoreApi returns the persisted dict (or None on 404 /
    # transport failure). The cloud_client / cloud_pid pass-through to
    # aggregate_session is left as a separate slice because session_log.py
    # still talks to ApiClient directly via collect_cloud_notes; folding
    # that into the Store interface is a follow-up commit.
    store = get_store()
    remote = store.get_session_log(session_id)
    if remote and (not existing_local
                   or remote.get("last_aggregated_at", "")
                      >= existing_local.get("last_aggregated_at", "")):
        existing_local = remote

    cloud_client = None
    cloud_pid = ""
    if store.is_cloud():
        try:
            from auth import load_credentials
            if load_credentials() is not None:
                cloud_client, cfg = _get_api_client()
                cloud_pid = cfg.get("project_id", "")
        except BaseException:
            if os.environ.get("BEACON_DEBUG") == "1":
                import traceback as _tb
                _tb.print_exc()

    payload = _slog.aggregate_session(
        project_data=data,
        beacon_dir=beacon_dir,
        session_id=session_id,
        recovered=recovered,
        summary_override=summary_override,
        cloud_client=cloud_client,
        cloud_project_id=cloud_pid,
        existing_log=existing_local,
    )
    _write_local_session_log(payload)
    _push_session_log_to_cloud(payload)
    return payload


def cmd_session_end():
    """Finalize the current session: aggregate its entries into the session log.

    Idempotent per SPEC §3: running twice on the same session produces the
    same logical result. Callers (typically /beacon-session-end Skill) may
    supply an AI-generated summary via BEACON_SUMMARY; otherwise the
    mechanical fallback in session_log.aggregate_session is used.
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    summary_override = os.environ.get("BEACON_SUMMARY", "").strip() or None
    explicit_sid = os.environ.get("BEACON_SESSION_ID", "").strip()

    # ms-54 / e-1293: persistence poisoning defense. A bus-origin caller
    # MUST NOT be able to upsert a session log — a poisoned summary that
    # lands in the session_log subcollection survives into the next
    # /beacon-session-start context restore, which is the cross-session
    # infection vector this defense exists to block.
    if _refuse_if_bus_origin(
        "session_end",
        {
            "session_id": explicit_sid,
            "summary_preview": (summary_override or "")[:80],
        },
    ):
        sys.exit(1)
    try:
        import session as _session
        sid = explicit_sid or _session.get_session_id()
    except Exception:
        sid = explicit_sid
    if not sid:
        print("Error: no session id (no .beacon/session.json and BEACON_SESSION_ID unset)",
              file=sys.stderr)
        sys.exit(1)

    # ms-81 e-1918 (SPEC AC #15): release any MS occupations held by this
    # session before aggregating. Done here so the session_log includes the
    # release events; running it after persistence would race the cloud sync.
    _released_count = _release_all_occupations_for_session(sid)
    if _released_count:
        print(
            f"  released {_released_count} occupation claim(s) held by this "
            f"session (status unchanged)",
            file=sys.stderr,
        )

    payload = _aggregate_and_persist(sid, recovered=False,
                                      summary_override=summary_override)
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Session log upserted: {sid}")
        print(f"  notes: {len(payload.get('note_ids', []))}, "
              f"commits: {len(payload.get('commit_ids', []))}, "
              f"prs: {len(payload.get('pr_ids', []))}")


def cmd_session_rescue():
    """Aggregate all sessions other than the current one.

    Per SPEC §7 the rescue path does NOT gate on heartbeat freshness — the
    aggregation is idempotent, so even rescuing an alive session is safe:
    the alive side's session-end will overwrite us later with a richer
    summary. ``recovered=True`` is set on first creation so forensics can
    distinguish rescue-born entries.
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    sids = _list_other_session_ids()
    results = []
    for sid in sids:
        try:
            payload = _aggregate_and_persist(sid, recovered=True)
            results.append({"session_id": sid, "status": "ok",
                            "notes": len(payload.get("note_ids", [])),
                            "commits": len(payload.get("commit_ids", [])),
                            "prs": len(payload.get("pr_ids", [])),
                            "recovered": payload.get("recovered", False)})
        except BaseException as e:
            results.append({"session_id": sid, "status": "error", "error": str(e)})
    if json_mode:
        print(json.dumps(results, ensure_ascii=False))
    else:
        if not results:
            print("No other sessions to rescue.")
        else:
            for r in results:
                if r["status"] == "ok":
                    print(f"  {r['session_id']}: rescued "
                          f"(notes={r['notes']}, commits={r['commits']}, prs={r['prs']}, "
                          f"recovered={r['recovered']})")
                else:
                    print(f"  {r['session_id']}: error — {r.get('error','')}")


def cmd_session_log_list():
    """List recent session log entries. Used by /beacon-session-start."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    try:
        limit = int(os.environ.get("BEACON_LIMIT", "0") or 0)
    except ValueError:
        limit = 0

    # ms-84 Phase 2 (e-2036): Store.list_session_logs returns the cloud
    # rows in cloud mode or [] in local mode, so the fallback to walking
    # ``.beacon/session_logs/`` directly only fires when the Store could
    # not supply anything.
    entries: list[dict] = get_store().list_session_logs(limit=limit)
    if not entries:
        # Local cache fallback
        d = _session_logs_dir()
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.endswith(".json"):
                    try:
                        with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                            entries.append(json.load(f))
                    except (OSError, json.JSONDecodeError):
                        continue
            entries.sort(key=lambda e: e.get("last_aggregated_at", ""), reverse=True)
            if limit:
                entries = entries[:limit]

    if json_mode:
        print(json.dumps(entries, ensure_ascii=False))
        return
    if not entries:
        print("(no session logs)")
        return
    for e in entries:
        sid = e.get("session_id", "?")
        when = e.get("last_aggregated_at", "")
        summary = (e.get("summary") or "")[:120]
        print(f"  {sid} [{when}]: {summary}")


def cmd_session_log_show():
    """Show a single session log entry by session_id."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    sid = os.environ.get("BEACON_SESSION_ID", "").strip()
    if not sid:
        print("Error: session id required", file=sys.stderr)
        sys.exit(1)
    entry = _read_local_session_log(sid)
    # ms-84 Phase 2 (e-2036): when the local cache is empty, ask the Store
    # for any persisted remote copy. LocalStore returns None (= no cloud
    # registry to consult); StoreApi returns the fetched dict or None on
    # 404 / transport failure, so the caller only checks truthiness.
    if entry is None:
        entry = get_store().get_session_log(sid)
    if entry is None:
        print(f"Session log not found: {sid}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Session: {entry.get('session_id','')}")
        print(f"Aggregated: {entry.get('last_aggregated_at','')}")
        print(f"Summary: {entry.get('summary','')}")
        print(f"Notes: {len(entry.get('note_ids', []))}")
        print(f"Commits: {len(entry.get('commit_ids', []))}")
        print(f"PRs: {len(entry.get('pr_ids', []))}")
        if entry.get("recovered"):
            print("(recovered)")


def cmd_session_id():
    """Print the current session_id — pure getter, no heartbeat (ms-54 e-1319).

    Post Option C (PR #111 / commit 78048b6) the bridge poll loop owns
    mint+heartbeat (``last_active`` + ``last_poll_at``); CLI's role is
    *resolve only*. This command still mints once on first call so the
    bridge has an id to read from .beacon/session.json, but never bumps
    ``last_active`` or pushes to cloud — keeping the bridge as the single
    truth source for liveness.

    Used by channel/bus.mjs at startup (cold-start session-id discovery)
    and historically by /beacon-session-start Step 0b (now no-op post
    e-1319). ms-54 e-1150 / e-1152 / e-1319.
    """
    try:
        import session as _session
        sid = _session.get_session_id()
        if not sid:
            print("Error: failed to materialise session_id", file=sys.stderr)
            sys.exit(1)
        print(sid)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _resolve_current_session_id() -> str:
    """Return the local session_id for `beacon session focus / attention`.

    Prefers the bridge's claim (.beacon/bridge.json) when a bus.mjs is
    actively running here (ms-54 / e-1331 quick fix). Without that step,
    this CLI call would mint or adopt the env's session_id and silently
    target a different session document than the bridge writes its
    heartbeat into — leaving intent stamps invisible to the directory
    picker.

    Falls back to :func:`session.get_session_id` when no live bridge
    claim exists. Errors degrade to an empty string so the caller can
    surface a clean "could not resolve" message rather than a traceback.
    """
    try:
        import session as _session
        return _session.resolve_active_session_id() or ""
    except Exception:
        return ""


def cmd_session_focus():
    """Stamp the AI's free-form intent on the current session (ms-54 / e-1369).

    Three modes:
      * `beacon session focus "<text>"`  — set the intent text
      * `beacon session focus --clear`   — clear the intent text (sets "")
      * `beacon session focus --show`    — read the current intent

    The intent is the only narrative field on the session document; Layer
    0-3 (Identity / Where / What / Reach) are stamped by the bridge from
    pure machine observation. Intent lets the AI explain *why* in one line,
    which is what the directory picker shows to a sender deciding "who to
    DM".
    """
    show = os.environ.get("BEACON_SESSION_FOCUS_SHOW", "") == "1"
    clear = os.environ.get("BEACON_SESSION_FOCUS_CLEAR", "") == "1"
    text = os.environ.get("BEACON_SESSION_FOCUS_TEXT", "")
    json_out = os.environ.get("BEACON_JSON", "") == "1"

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    session_id = _resolve_current_session_id()
    if not session_id:
        print("Error: could not resolve current session_id "
              "(run `beacon session id` first)", file=sys.stderr)
        sys.exit(1)

    if show:
        s = client.get_session(project_id, session_id)
        intent = s.get("intent") or {}
        if json_out:
            print(json.dumps(intent, ensure_ascii=False))
        else:
            txt = intent.get("text") or "(no intent set)"
            attn = intent.get("attention_required")
            print(f"focus: {txt}")
            if attn is not None:
                print(f"  attention_required: {attn}")
        return

    if clear:
        result = client.upsert_session_intent(
            project_id, session_id, text="", attention_required=None,
        )
    else:
        if not text:
            print("Usage: beacon session focus \"<text>\" "
                  "| --clear | --show [--json]", file=sys.stderr)
            sys.exit(2)
        result = client.upsert_session_intent(
            project_id, session_id, text=text, attention_required=None,
        )

    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        intent = (result.get("intent") or {}).get("text") or "(cleared)"
        print(f"focus: {intent}")


def cmd_session_attention():
    """Raise / lower the attention_required flag on the current session.

    `beacon session attention --set true` signals "this session is waiting
    on a human decision" — the directory picker surfaces it prominently
    so a teammate sees "who needs me" without reading every intent text.
    Lowered with `--set false` once the human input lands.
    """
    set_val = os.environ.get("BEACON_SESSION_ATTENTION_SET", "")
    json_out = os.environ.get("BEACON_JSON", "") == "1"
    if set_val not in ("true", "false"):
        print("Usage: beacon session attention --set true|false [--json]",
              file=sys.stderr)
        sys.exit(2)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    session_id = _resolve_current_session_id()
    if not session_id:
        print("Error: could not resolve current session_id", file=sys.stderr)
        sys.exit(1)

    flag = (set_val == "true")
    result = client.upsert_session_intent(
        project_id, session_id, text=None, attention_required=flag,
    )
    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"attention_required: {flag}")


def cmd_session_fork():
    """Fork a sibling worktree for parallel work on a target milestone (ms-67 / e-1549).

    Creates ``.worktrees/<ms-id>-fork-<short-uuid>``, binds it to the same
    Beacon project by copying ``.beacon/cloud.json``, runs ``beacon channel
    install`` so the child worktree can host its own bclaude with a clean
    MCP server declaration, and records the parent ↔ child link in
    ``.beacon/fork.json``.

    The actual subprocess work lives in :func:`session.fork_workspace` so
    the CLI layer here only does arg parsing + lookups + reporting. Tests
    target the helper directly with a fake runner.
    """
    import subprocess as _sp

    ms_id = os.environ.get("BEACON_SESSION_FORK_MS_ID", "").strip()
    json_out = os.environ.get("BEACON_JSON", "") == "1"
    if not ms_id:
        print("Usage: beacon session fork <ms-id> [--json]", file=sys.stderr)
        sys.exit(2)

    # Resolve ms_title from the local project.json cache (load_project gives
    # the full project including milestones[]). Surfaces a clean error if
    # the ms doesn't exist rather than writing fork.json with a blank title.
    data = load_project()
    ms_title = ""
    for ms in data.get("milestones", []):
        if ms.get("id") == ms_id:
            ms_title = ms.get("title", "")
            break
    if not ms_title:
        print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    # Parent session info — used by the child's session-start to surface
    # "you were forked from sv-xxxx". Tolerate missing values: forking
    # outside a live bclaude session shouldn't be blocked just because
    # the parent id can't be resolved.
    import session as _session
    parent_session_id = _session.resolve_active_session_id() or ""

    parent_repo_path = os.getcwd()  # bin/beacon has already cd'd to project root
    # git rev-parse runs in the project root; if it fails fall back to empty
    proc = _sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False, cwd=parent_repo_path,
    )
    parent_branch = proc.stdout.strip() if proc.returncode == 0 else ""

    try:
        result = _session.fork_workspace(
            ms_id, ms_title,
            parent_session_id=parent_session_id,
            parent_branch=parent_branch,
            parent_repo_path=parent_repo_path,
        )
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_out:
        print(json.dumps(result, ensure_ascii=False))
    else:
        wt = result["worktree_path"]
        branch = result["branch"]
        channel_ok = result["fork_record"]["channel_install"]["ok"]
        print(f"Forked worktree: {wt}")
        print(f"  branch:        {branch}")
        print(f"  target_ms:     {ms_id} {ms_title}")
        print(f"  parent_branch: {parent_branch or '(unresolved)'}")
        print(f"  parent_sid:    {parent_session_id or '(unresolved)'}")
        if not channel_ok:
            print("  channel install failed — re-run `beacon channel install` "
                  "from the worktree once you cd into it")
        print("")
        print(f"  next: cd {wt} && bclaude")


def cmd_session_fork_list():
    """List active forked worktrees in this repo (ms-67 / e-1553).

    Walks ``git worktree list`` and, for each worktree, checks for
    ``.beacon/fork.json``. Only fork-created worktrees show up — this is
    explicitly *not* a generic worktree listing (use ``git worktree list``
    for that). Used by ``/beacon-session-merge-back`` (e-1552) as its
    picker source.
    """
    import session as _session

    json_out = os.environ.get("BEACON_JSON", "") == "1"
    repo_root = os.getcwd()  # bin/beacon already cd'd to project root
    forks = _session.list_forks(repo_root)

    if json_out:
        print(json.dumps(forks, ensure_ascii=False))
        return

    if not forks:
        print("No active forks")
        return

    for fk in forks:
        print(f"{fk['worktree_path']}")
        print(f"  target:        {fk['target_ms_id']} {fk['target_ms_title']}")
        print(f"  child_branch:  {fk['child_branch']}")
        print(f"  parent_sid:    {fk['parent_session_id'] or '(unknown)'}")
        print(f"  parent_branch: {fk['parent_branch'] or '(unknown)'}")
        print(f"  created:       {fk['created_at']}")
        print("")


def _resolve_channel_root() -> "Path | None":
    """Find the directory containing channel/bus.mjs across all install paths.

    Walks a fixed candidate list in priority order so dev clone, Homebrew, and
    pypi (pipx/pip) installs all locate the bundled channel assets without a
    hard-coded layout. Returns None if no candidate exists (caller surfaces a
    clean error). ms-54 e-1169.

    Candidates tried in order:
      1. ``$BEACON_CHANNEL_DIR`` (env override — used by tests / Win users
         who manually copied channel/ to a non-standard location).
      2. ``<commands.py>/../../channel``           — dev clone layout
         (lib/commands.py + channel/ siblings under <repo>).
      3. ``<commands.py>/../channel``              — Homebrew libexec layout
         (libexec/commands.py + libexec/channel/).
      4. ``<commands.py>/../../_bundled_channel``  — pypi wheel layout
         (beacon_cli/_bundled_lib/commands.py + beacon_cli/_bundled_channel/).
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    import os as _os
    override = _os.environ.get("BEACON_CHANNEL_DIR", "").strip()
    if override:
        cand = Path(override).resolve()
        if (cand / "bus.mjs").exists():
            return cand
    commands_path = Path(__file__).resolve()
    candidates = [
        commands_path.parent.parent / "channel",           # dev clone
        commands_path.parent / "channel",                  # Homebrew libexec
        commands_path.parent.parent / "_bundled_channel",  # pypi wheel
    ]
    for c in candidates:
        if (c / "bus.mjs").exists():
            return c
    return None


def _build_channel_server_entry(bus_path: "Path") -> dict:
    """Construct the `.mcp.json` `beacon-bus` server entry, OS-aware.

    Cross-platform pitfalls this handles (ms-54 e-1159):

    - **Windows ``node`` bare command**: Claude Code on Windows shells out
      via CreateProcess (not cmd.exe), so a bare ``"node"`` fails to spawn
      when node.exe is on PATH but not in CWD. We resolve the absolute
      node.exe path via ``shutil.which("node")`` and fall back to
      ``cmd.exe /c node`` so the shell does the PATH lookup. Mac/Linux
      keep the bare ``"node"`` form (shell does fork+exec lookup).
    - **`/tmp` log path**: ``/tmp`` doesn't exist on Windows; the file is
      silently never written and bus.mjs loses its diagnostic log. We use
      ``%TEMP%\\beacon-bus-channel.log`` on Windows (via ``tempfile.
      gettempdir()`` which resolves env vars cross-platform). Mac/Linux
      keep ``/tmp/beacon-bus-channel.log`` for compatibility with the
      existing dogfood debug pipeline.
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    import platform as _platform
    import shutil as _shutil
    import tempfile as _tempfile
    is_windows = _platform.system() == "Windows"

    if is_windows:
        node_abs = _shutil.which("node") or _shutil.which("node.exe")
        if node_abs:
            command = node_abs
            args = [str(bus_path)]
        else:
            # Fallback: have cmd.exe walk PATH. Works even if node was
            # installed after this .mcp.json was generated.
            command = "cmd.exe"
            args = ["/c", "node", str(bus_path)]
        log_path = str(Path(_tempfile.gettempdir()) / "beacon-bus-channel.log")
    else:
        command = "node"
        args = [str(bus_path)]
        # Keep /tmp on Unix so existing dogfood log paths (e.g.
        # `/tmp/beacon-bus-channel.log` referenced in memo docs) stay
        # stable; tempfile.gettempdir() would return /var/folders/... on
        # macOS which breaks the muscle-memory `tail -f /tmp/...` flow.
        log_path = "/tmp/beacon-bus-channel.log"

    return {
        "command": command,
        "args": args,
        "env": {
            "BEACON_CHANNEL_ALLOWLIST": "dm",
            "BEACON_BUS_LOG": log_path,
        },
    }


def _ensure_channel_node_modules(channel_root: "Path") -> bool:
    """Make sure channel/node_modules/ exists (run `npm install` if not).

    Most install paths leave node_modules to runtime first-use because:
      - dev clone: contributor never runs npm install
      - pypi wheel: shipping node_modules in a wheel is platform-fragile
      - Homebrew: depends_on "node" + brew's install step should generate it,
        but a partial install / brew formula bug can leave it missing
    Returns True if node_modules is present (existed or created); False if
    install failed. ms-54 e-1169 + e-1191.

    Windows PATHEXT semantics (e-1191): the npm CLI on Windows ships as
    ``npm.cmd`` (a batch shim around node), not ``npm.exe``. Python's
    ``subprocess`` without ``shell=True`` does NOT consult PATHEXT, so
    ``subprocess.call(["npm", ...])`` raises FileNotFoundError on Windows
    even when Node.js is installed and ``shutil.which("node")`` finds it.
    The fix: resolve the absolute path via ``shutil.which`` (which DOES
    consult PATHEXT) FIRST, then call subprocess with the resolved path.
    """
    import subprocess as _subprocess
    import shutil as _shutil
    import platform as _platform

    nm = channel_root / "node_modules"
    if nm.exists():
        return True
    pkg_json = channel_root / "package.json"
    if not pkg_json.exists():
        return False

    is_windows = _platform.system() == "Windows"
    # Try canonical names first, then Windows-specific extensions. shutil.which
    # consults PATHEXT so on Win it will pick up npm.cmd via the bare "npm"
    # form too, but listing the explicit extensions makes the contract clear.
    candidates = ["npm"]
    if is_windows:
        candidates += ["npm.cmd", "npm.exe", "npm.bat"]
    npm_path = None
    for cand in candidates:
        resolved = _shutil.which(cand)
        if resolved:
            npm_path = resolved
            break

    if not npm_path:
        print("Error: `npm` not found on PATH.", file=sys.stderr)
        if is_windows:
            print("Install Node.js on Windows: `winget install OpenJS.NodeJS.LTS`",
                  file=sys.stderr)
            print("  or download from https://nodejs.org/ (LTS Windows Installer).",
                  file=sys.stderr)
        elif _platform.system() == "Darwin":
            print("Install Node.js on macOS: `brew install node` (or via nvm).",
                  file=sys.stderr)
        else:
            print("Install Node.js: your package manager (e.g. `apt install nodejs npm`)",
                  file=sys.stderr)
            print("  or via nvm (https://github.com/nvm-sh/nvm).", file=sys.stderr)
        return False

    print(f"channel/node_modules not found — running `npm install` in {channel_root}",
          file=sys.stderr)
    print(f"  using npm at: {npm_path}", file=sys.stderr)
    try:
        rc = _subprocess.call([npm_path, "install", "--silent"], cwd=str(channel_root))
    except OSError as e:
        print(f"Error: failed to launch npm at {npm_path}: {e}", file=sys.stderr)
        return False
    if rc != 0:
        print(f"Error: `npm install` exited {rc}.", file=sys.stderr)
        return False
    return nm.exists()


# ---------------------------------------------------------------------------
# Channel lifecycle: opt-out / opt-in / uninstall / status (ms-54 e-1266)
# ---------------------------------------------------------------------------
#
# The DM ("beacon-bus") channel has four user states:
#   (a) never installed — fresh user
#   (b) installed and active
#   (c) installed but paused (MCP entry removed, node_modules retained)
#   (d) opted out (no install will succeed, env / project / global flag set)
#
# The four CLI verbs that move between these states are:
#   beacon channel install               (a|c|d-without-opt-out) → b
#   beacon channel uninstall             b → c (default) or a (with --purge-files)
#   beacon channel opt-out [--global|--project]   any → d
#   beacon channel opt-in  [--global|--project]   d → previous
#
# `beacon channel status` prints the current state and predicts whether the
# next auto-install attempt will run. The whole surface is designed so that
# install paths (this CLI, the e-1238 auto-install, the e-1167 bclaude
# wrapper) all share `_is_bus_opted_out()` as the single gate — if any of
# (env BEACON_NO_BUS=1, project flag, global flag) is set, install is
# refused and the user sees a single consistent explanation.

def _user_beacon_config_path() -> str:
    """Absolute path to the user-global beacon config (~/.beacon/config.json).

    Distinct from project-local .beacon/config.json. We mint the directory
    on demand so the first `beacon channel opt-out --global` call doesn't
    fail because ~/.beacon does not yet exist.
    """
    return os.path.join(_user_home(), ".beacon", "config.json")


def _read_user_beacon_config() -> dict:
    """Read ~/.beacon/config.json, returning {} if missing or unreadable.

    Unreadable / corrupt files surface as {} so opt-out checks don't crash
    install paths; the broken file is preserved on disk so the user can
    diagnose it manually.
    """
    p = _user_beacon_config_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_user_beacon_config(d: dict) -> None:
    """Atomically write ~/.beacon/config.json, minting parent dir as needed."""
    p = _user_beacon_config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _read_project_bus_disabled() -> bool:
    """True if the active project sets `bus.disabled` in project.json.

    Project-scoped opt-out lives under a top-level `bus` object:
        { "bus": {"disabled": true}, ... }
    This shape mirrors the global config schema so a user inspecting
    either file sees the same idiom. Missing project file → not opted out
    (consistent with how install requires .beacon/project.json anyway).
    """
    try:
        pf = get_project_file()
    except Exception:
        return False
    if not os.path.exists(pf):
        return False
    try:
        with open(pf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    bus = data.get("bus") or {}
    return bool(bus.get("disabled"))


def _write_project_bus_flag(disabled: bool) -> bool:
    """Set or clear the project-local `bus.disabled` flag.

    Returns True on write. False if no project file exists (caller decides
    whether to fail or treat as no-op). When `disabled=False`, the bus
    object is removed entirely if empty so project.json stays minimal.
    """
    try:
        pf = get_project_file()
    except Exception:
        return False
    if not os.path.exists(pf):
        return False
    try:
        with open(pf, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    bus = data.get("bus") or {}
    if disabled:
        bus["disabled"] = True
        data["bus"] = bus
    else:
        bus.pop("disabled", None)
        if not bus:
            data.pop("bus", None)
        else:
            data["bus"] = bus
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return True


def _bus_opt_out_status() -> dict:
    """Probe all three opt-out sources and report which (if any) is active.

    Returns {"env": bool, "project": bool, "global": bool, "any": bool}.
    The order matches the precedence we present to the user: env wins
    (transient, easy to flip), project next (scoped intent), global last
    (broad). `any` is the OR used by install gates.
    """
    env_flag = os.environ.get("BEACON_NO_BUS", "").strip() in ("1", "true", "yes", "on")
    project_flag = _read_project_bus_disabled()
    user_cfg = _read_user_beacon_config()
    global_flag = bool((user_cfg.get("bus") or {}).get("disabled"))
    return {
        "env": env_flag,
        "project": project_flag,
        "global": global_flag,
        "any": env_flag or project_flag or global_flag,
    }


def _is_bus_opted_out() -> "tuple[bool, str]":
    """Single gate every install path consults.

    Returns (is_opted_out, human_reason). The reason string is suitable
    for printing in CLI warnings / audit log lines without further
    formatting. Designed to be the only check sites need to make so the
    rule "install never silently overrides opt-out" holds globally.
    """
    s = _bus_opt_out_status()
    if not s["any"]:
        return False, ""
    sources = []
    if s["env"]:
        sources.append("env BEACON_NO_BUS=1")
    if s["project"]:
        sources.append("project (.beacon/project.json bus.disabled)")
    if s["global"]:
        sources.append("global (~/.beacon/config.json bus.disabled)")
    return True, "DM opt-out active: " + " + ".join(sources)


def cmd_channel_install():
    """Write .mcp.json with the Beacon bus Channel MCP server entry.

    Project-level opt-in for ms-54 channel/bus.mjs. Resolves the bundled
    bus.mjs absolute path via _resolve_channel_root() so dev clone /
    Homebrew / pypi installs all work. Refuses to overwrite an existing
    .mcp.json that already has unrelated mcpServers entries; merges
    instead when safe. ms-54 e-1152 / e-1169.
    """
    from pathlib import Path  # local import — pathlib not yet at module level
    cwd = Path.cwd()
    if not (cwd / ".beacon" / "project.json").exists():
        print("Error: no .beacon/project.json in this directory. Run `beacon init` first.",
              file=sys.stderr)
        sys.exit(1)

    # ms-54 e-1266: refuse to install if any opt-out source is set. This is
    # the single gate every install path consults (manual install, e-1238
    # auto-install, e-1167 bclaude wrapper). Refusing here — rather than
    # silently no-op'ing — gives the user feedback that opt-out is in force
    # and tells them how to lift it.
    opted_out, reason = _is_bus_opted_out()
    if opted_out:
        print(f"Refusing to install: {reason}", file=sys.stderr)
        print("Lift the opt-out first:", file=sys.stderr)
        print("  beacon channel opt-in [--project|--global]   "
              "(or unset BEACON_NO_BUS)", file=sys.stderr)
        print("Or inspect current state with:  beacon channel status",
              file=sys.stderr)
        sys.exit(1)

    channel_root = _resolve_channel_root()
    if channel_root is None:
        print("Error: channel/bus.mjs not found in any expected location.", file=sys.stderr)
        print("Looked for:", file=sys.stderr)
        print("  - $BEACON_CHANNEL_DIR (env override)", file=sys.stderr)
        print("  - <repo>/channel        (dev clone)", file=sys.stderr)
        print("  - <libexec>/channel     (Homebrew)", file=sys.stderr)
        print("  - <beacon_cli>/_bundled_channel  (pypi wheel)", file=sys.stderr)
        print("Reinstall Beacon (brew reinstall / pipx reinstall) or set "
              "BEACON_CHANNEL_DIR to point at your channel/ directory.",
              file=sys.stderr)
        sys.exit(1)
    bus_path = channel_root / "bus.mjs"

    # Ensure node_modules so the MCP server actually starts on first launch.
    # Best-effort: surface a warning if missing but still write .mcp.json so
    # the user sees the next-steps text and can fix node manually.
    if not _ensure_channel_node_modules(channel_root):
        print(f"Warning: channel/node_modules at {channel_root} is missing or "
              f"`npm install` failed.", file=sys.stderr)
        print("The beacon-bus MCP server will fail to start until this is "
              "resolved.", file=sys.stderr)

    server_entry = _build_channel_server_entry(bus_path)

    mcp_path = cwd / ".mcp.json"
    config: dict
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error: existing .mcp.json is not valid JSON ({e}).", file=sys.stderr)
            print("Resolve the file by hand, or remove it and re-run.", file=sys.stderr)
            sys.exit(1)
    else:
        config = {}

    mcp_servers = config.setdefault("mcpServers", {})
    existed = "beacon-bus" in mcp_servers
    mcp_servers["beacon-bus"] = server_entry

    with mcp_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"{'Updated' if existed else 'Created'} {mcp_path.relative_to(cwd)}")
    print(f"  beacon-bus server → {bus_path}")
    print()
    print("Next steps:")
    # ms-54 e-1167: bclaude wrapper is the recommended launcher because it
    # hides the long `--dangerously-load-development-channels` flag and
    # honors the opt-out gate. The raw flag form and shell alias stay as
    # fallbacks for users who don't have the wrapper on PATH yet.
    print("  1. Start Claude Code via the bundled wrapper (recommended):")
    print("     bclaude")
    print("     (forwards all args to `claude`; refuses the channel flag if "
          "DM opt-out is active)")
    print("  2. Or directly with the long flag (research preview):")
    print("     claude --dangerously-load-development-channels server:beacon-bus")
    print("  3. Or define a shell alias:")
    print("     alias bclaude='claude --dangerously-load-development-channels server:beacon-bus'")
    print()
    print("Channels are research preview. See:")
    print("  https://code.claude.com/docs/en/channels.md")
    print()
    print("To stop using DM later:")
    print("  beacon channel uninstall              # remove MCP entry, keep node_modules")
    print("  beacon channel uninstall --purge-files # also remove channel/node_modules")
    print("  beacon channel opt-out                # block all future auto-installs")
    print("  beacon channel status                 # check current state")


def cmd_channel_uninstall():
    """Remove the beacon-bus MCP entry; optionally also wipe node_modules.

    Modes (selected via env from bin/beacon):
      - default / --keep-files: only remove `beacon-bus` from .mcp.json.
        node_modules/ stays so a later `beacon channel install` is fast
        (this is the "pause" path).
      - --purge-files: remove the MCP entry AND delete channel/node_modules
        so the next install does a fresh `npm install`. Useful when the
        user wants the disk back or suspects a corrupt install.

    The MCP-entry removal mirrors install: if other mcpServers entries
    exist alongside beacon-bus, only beacon-bus is removed and the rest
    stay intact. If beacon-bus is the only entry, the mcpServers object
    is left empty (we don't delete .mcp.json itself — other tools may
    add their own entries later).

    Always prints what was actually removed so the user can audit.
    """
    from pathlib import Path
    cwd = Path.cwd()
    if not (cwd / ".beacon" / "project.json").exists():
        print("Error: no .beacon/project.json in this directory. Run from a "
              "project root.", file=sys.stderr)
        sys.exit(1)

    purge = os.environ.get("BEACON_CHANNEL_PURGE_FILES", "") == "1"
    removed_anything = False

    # ---- Phase 1: strip the MCP entry --------------------------------------
    mcp_path = cwd / ".mcp.json"
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: .mcp.json is not valid JSON ({e}).", file=sys.stderr)
            print("Resolve the file by hand, or remove it and re-run.",
                  file=sys.stderr)
            sys.exit(1)
        servers = config.get("mcpServers", {}) or {}
        if "beacon-bus" in servers:
            del servers["beacon-bus"]
            config["mcpServers"] = servers
            with mcp_path.open("w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"Removed beacon-bus from {mcp_path.relative_to(cwd)}")
            removed_anything = True
        else:
            print(f"(beacon-bus was not present in {mcp_path.relative_to(cwd)})")
    else:
        print("(no .mcp.json found — nothing to remove)")

    # ---- Phase 2: optionally wipe node_modules ----------------------------
    if purge:
        channel_root = _resolve_channel_root()
        if channel_root is None:
            print("Warning: channel/ root not located — skipping node_modules "
                  "purge.", file=sys.stderr)
        else:
            nm = channel_root / "node_modules"
            if nm.exists():
                # Move to .trash/ in the project root rather than rm -rf — this
                # follows the global "no rm" convention and lets the user
                # restore if --purge-files was a mistake.
                import time as _time
                trash_dir = cwd / ".trash"
                trash_dir.mkdir(exist_ok=True)
                stamp = _time.strftime("%Y%m%d-%H%M%S")
                target = trash_dir / f"channel-node_modules-{stamp}"
                try:
                    nm.rename(target)
                    print(f"Moved {nm} → {target.relative_to(cwd)}")
                    print("  (kept in .trash/ rather than deleted — "
                          "`mv` it back if you want it restored)")
                    removed_anything = True
                except OSError as e:
                    print(f"Warning: failed to move {nm}: {e}",
                          file=sys.stderr)
            else:
                print(f"(no node_modules at {nm} — nothing to purge)")

    if not removed_anything:
        print("Nothing to do — beacon-bus was already uninstalled.")

    # ---- Helpful follow-up text --------------------------------------------
    print()
    if purge:
        print("Re-install later with:  beacon channel install")
        print("  (will trigger a fresh `npm install`)")
    else:
        print("Re-install later with:  beacon channel install")
        print("  (fast — node_modules retained)")
    print("To block ALL future installs (incl. auto-install): "
          "beacon channel opt-out")


def cmd_channel_opt_out():
    """Set the persistent opt-out flag at project or global scope.

    Default scope is --project (set via env from bin/beacon). The flag
    blocks every install path: manual `beacon channel install`, the
    e-1238 auto-install in `beacon setup` / Skill flows, and the e-1167
    bclaude wrapper. Idempotent — setting twice is a no-op.

    Env BEACON_NO_BUS=1 is *also* honored (transient, per-shell) but
    needs no command; it is documented in `beacon channel status`.
    """
    scope = os.environ.get("BEACON_CHANNEL_SCOPE", "project")

    if scope == "global":
        cfg = _read_user_beacon_config()
        bus = cfg.get("bus") or {}
        already = bool(bus.get("disabled"))
        bus["disabled"] = True
        cfg["bus"] = bus
        _write_user_beacon_config(cfg)
        p = _user_beacon_config_path()
        if already:
            print(f"Global opt-out already set in {p}")
        else:
            print(f"Global opt-out written to {p}")
            print("  → DM auto-install will be skipped in every project.")
    else:
        # project scope (default)
        try:
            pf = get_project_file()
        except Exception:
            pf = ""
        if not pf or not os.path.exists(pf):
            print("Error: no .beacon/project.json in this directory. Run from "
                  "a project root, or use --global.", file=sys.stderr)
            sys.exit(1)
        already = _read_project_bus_disabled()
        if not _write_project_bus_flag(True):
            print("Error: failed to write project.json", file=sys.stderr)
            sys.exit(1)
        if already:
            print(f"Project opt-out already set in {pf}")
        else:
            print(f"Project opt-out written to {pf}")
            print("  → DM auto-install will be skipped in this project only.")

    print()
    print("Lift later with:  beacon channel opt-in "
          f"[--{scope}]")


def cmd_channel_opt_in():
    """Clear the opt-out flag at project or global scope (idempotent)."""
    scope = os.environ.get("BEACON_CHANNEL_SCOPE", "project")

    if scope == "global":
        cfg = _read_user_beacon_config()
        bus = cfg.get("bus") or {}
        had = bool(bus.get("disabled"))
        if had:
            bus.pop("disabled", None)
            if bus:
                cfg["bus"] = bus
            else:
                cfg.pop("bus", None)
            _write_user_beacon_config(cfg)
            print(f"Global opt-out cleared from {_user_beacon_config_path()}")
        else:
            print(f"Global opt-out was not set (nothing to clear).")
    else:
        try:
            pf = get_project_file()
        except Exception:
            pf = ""
        if not pf or not os.path.exists(pf):
            print("Error: no .beacon/project.json in this directory. Run from "
                  "a project root, or use --global.", file=sys.stderr)
            sys.exit(1)
        had = _read_project_bus_disabled()
        if had:
            _write_project_bus_flag(False)
            print(f"Project opt-out cleared from {pf}")
        else:
            print(f"Project opt-out was not set (nothing to clear).")

    # Surface remaining opt-out sources so the user understands why DM may
    # still be off even after this opt-in.
    status = _bus_opt_out_status()
    remaining = [k for k in ("env", "project", "global") if status[k]]
    if remaining:
        print()
        print("Note: opt-out is still active via: " + ", ".join(remaining))
        print("  (run `beacon channel status` to see details)")


def cmd_channel_status():
    """Print a 4-block summary of the DM channel lifecycle state.

    Blocks (in this order):
      1. Install state — is `beacon-bus` in .mcp.json?
      2. Files state — does channel/node_modules/ exist?
      3. Opt-out state — env / project / global, with source paths.
      4. Prediction — would the next auto-install run, and why?

    Read-only. Never modifies project.json, .mcp.json, or config.json.
    Designed so a user troubleshooting "why is DM not working?" gets the
    full picture in one screen.
    """
    from pathlib import Path
    cwd = Path.cwd()

    # ---- 1. Install state -------------------------------------------------
    mcp_path = cwd / ".mcp.json"
    mcp_has_entry = False
    if mcp_path.exists():
        try:
            with mcp_path.open("r", encoding="utf-8") as f:
                config = json.load(f) or {}
            mcp_has_entry = "beacon-bus" in (config.get("mcpServers") or {})
        except (OSError, json.JSONDecodeError):
            pass

    # ---- 2. Files state ---------------------------------------------------
    channel_root = _resolve_channel_root()
    nm_exists = False
    nm_path_str = "(channel/ root not located)"
    if channel_root is not None:
        nm = channel_root / "node_modules"
        nm_exists = nm.exists()
        nm_path_str = str(nm)

    # ---- 3. Opt-out state -------------------------------------------------
    status = _bus_opt_out_status()

    # ---- 4. Prediction ----------------------------------------------------
    if status["any"]:
        sources = []
        if status["env"]:
            sources.append("env BEACON_NO_BUS")
        if status["project"]:
            sources.append("project flag")
        if status["global"]:
            sources.append("global flag")
        prediction = "would be SKIPPED (opt-out via " + ", ".join(sources) + ")"
    elif mcp_has_entry:
        prediction = "would be a NO-OP (already installed, MCP entry present)"
    else:
        prediction = "would RUN (no opt-out, no MCP entry present)"

    # ---- Render ------------------------------------------------------------
    print("Beacon DM channel — current state")
    print("=" * 50)
    print()
    print(f"[1] Install state ({mcp_path.relative_to(cwd) if mcp_path.exists() else '.mcp.json'}):")
    if mcp_has_entry:
        print("    ✓ beacon-bus MCP entry present")
    elif mcp_path.exists():
        print("    × beacon-bus MCP entry missing (.mcp.json exists but no entry)")
    else:
        print("    × no .mcp.json (never installed in this project)")
    print()
    print(f"[2] Files state:")
    print(f"    path: {nm_path_str}")
    if nm_exists:
        print("    ✓ channel/node_modules/ present")
    else:
        print("    × channel/node_modules/ missing")
    print()
    print("[3] Opt-out state:")
    if not status["any"]:
        print("    (no opt-out set)")
    else:
        if status["env"]:
            print(f"    ✓ env BEACON_NO_BUS=1 (transient, this shell only)")
        if status["project"]:
            try:
                pf = get_project_file()
            except Exception:
                pf = ".beacon/project.json"
            print(f"    ✓ project flag in {pf}")
        if status["global"]:
            print(f"    ✓ global flag in {_user_beacon_config_path()}")
    print()
    print("[4] Next auto-install:")
    print(f"    → {prediction}")
    print()

    # Action hints based on current state.
    if status["any"]:
        print("To re-enable DM: beacon channel opt-in [--project|--global] "
              "(or unset BEACON_NO_BUS)")
    elif not mcp_has_entry:
        print("To install: beacon channel install")
    else:
        print("To uninstall: beacon channel uninstall [--purge-files]")
        print("To block future auto-installs: beacon channel opt-out")


# ---------------------------------------------------------------------------
# Member management (e-624)
# ---------------------------------------------------------------------------
#
# All write paths go through lib/operations.apply_operation so the ms-39
# lost-update protection covers concurrent member edits (two maintainers
# inviting different people at the same moment, etc.). Reads are direct
# load_project, which is consistent with task_list / milestone_list and
# does not need atomicity.

def _project_id_for_ops() -> str:
    """Return the project_id used by apply_operation for this CLI invocation.

    Local mode: project_id is irrelevant beyond changelog labeling, so we
    use the project's `name` field. Cloud mode: requires the cloud.json
    project_id which the API layer normally supplies — here we read
    .beacon/cloud.json if present, else fall back to project name.
    """
    try:
        data = load_project()
    except Exception:
        return ""
    project_file = get_project_file()
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    cloud_json = os.path.join(beacon_dir, "cloud.json")
    if os.path.exists(cloud_json):
        try:
            with open(cloud_json, "r", encoding="utf-8") as f:
                return json.load(f).get("project_id", "") or data.get("name", "")
        except (OSError, json.JSONDecodeError):
            pass
    return data.get("name", "")


# ---------------------------------------------------------------------------
# Trek (ms-69) — top-level cross-project collaboration area.
# Local mode only for now (storage = lib/trek_store, files under
# ~/.beacon/treks/). Cloud HTTP path lands in e-1656.
# ---------------------------------------------------------------------------

def _read_credentials_for_identity() -> tuple[str, str]:
    """Read (user_id, email) from the active-profile credentials.json (ms-61 / e-2132).

    Returns ``("", "")`` if no credentials file exists, the file is malformed,
    or no usable fields are present. Never raises — this is a best-effort
    fallback for ``_resolve_creator_identity``; the caller still treats
    missing email as a hard error if env is also empty.

    Implementation:
      * Resolves the credentials path via ``profile.resolve_active_profile()``
        so cwd ``cloud.json.profile`` and ``BEACON_PROFILE`` are honored.
      * Falls back to legacy ``~/.beacon/credentials.json`` if the per-profile
        path doesn't exist yet (= pre-migration installs).
      * ``user_id`` is decoded from the JWT ``sub`` claim (= Google sub, e.g.
        Cognito/Cloud Identity uid). The token is split as ``bcli.<payload>.<sig>``
        or stdlib JWT ``<header>.<payload>.<sig>``; we read the middle segment.
      * ``email`` is read from the top-level ``email`` field directly.

    Why this exists:
      ms-84 dogfood (2026-06-19) で観測された病理: fork session で
      ``BEACON_USER_EMAIL`` 設定漏れ → ``beacon trek create / join / dm send``
      が hard error。 credentials.json には email がある (= login 済) のに
      env を要求するため、 fork 経路で identity 漏れる導線が温存されていた。
      本 helper で auto-read 経路を足し、 env override は最優先のまま
      (= test / CI / multi-account 用)。
    """
    import base64
    import json as _json
    try:
        import profile as _profile
        cred_path = _profile.resolve_active_profile().credentials_path
    except Exception:
        cred_path = None

    candidates = []
    if cred_path is not None:
        candidates.append(cred_path)
    # Legacy singleton location (= pre-profile installs). Honor BEACON_HOME
    # so tests / multi-account flows can isolate it the same way the profile
    # module does (= profile._beacon_home contract).
    beacon_home = os.environ.get("BEACON_HOME") or os.path.expanduser("~/.beacon")
    candidates.append(os.path.join(beacon_home, "credentials.json"))

    for path in candidates:
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue

        email = (data.get("email") or "").strip()
        token = (data.get("token") or "").strip()

        # Decode token payload to recover sub (= user_id). Two formats
        # supported:
        #   * ``bcli.<payload_b64>.<sig_hex>`` — Beacon CLI long-lived token
        #     (= 2 segments after the "bcli." prefix, payload is segment[0])
        #   * ``<header>.<payload>.<sig>`` — standard 3-segment JWT (= Cognito
        #     id_token, Google id_token; payload is segment[1])
        user_id = ""
        if token:
            tok = token
            is_bcli = False
            if tok.startswith("bcli."):
                tok = tok[5:]
                is_bcli = True
            segments = tok.split(".")
            payload_b64 = ""
            if is_bcli and len(segments) >= 1:
                payload_b64 = segments[0]
            elif len(segments) >= 2:
                payload_b64 = segments[1]
            if payload_b64:
                # Re-pad base64 (JWT strips '='); urlsafe alphabet.
                padding = "=" * (-len(payload_b64) % 4)
                try:
                    payload_bytes = base64.urlsafe_b64decode(
                        payload_b64 + padding
                    )
                    payload = _json.loads(payload_bytes.decode("utf-8"))
                    user_id = str(payload.get("sub") or "").strip()
                except Exception:
                    user_id = ""

        if email or user_id:
            return user_id, email
    return "", ""


def _resolve_creator_identity() -> tuple[str, str, str]:
    """Return (user_id, email, session_id) for the trek creator.

    Resolution order (ms-61 / e-2132):
      1. Env vars (``BEACON_USER_ID`` / ``BEACON_USER_EMAIL`` / ``BEACON_SESSION_ID``)
         — highest precedence so tests, CI, multi-account flows override freely.
      2. ``credentials.json`` of the active profile (= login 済セッションは
         自動継承)。 email と user_id のうち env で埋まらなかったものだけ
         credentials から補う。 env と credentials の値が共存している場合は
         **env が勝つ**。
      3. ``whoami`` for ``user_id`` only — final fallback so dev-mode runs
         without login still produce a non-empty user_id (= just the OS user).

    Email と session_id は credentials 経路でも埋まらなければ呼び出し側で
    hard error にする (= ``BEACON_USER_EMAIL`` 要求 など)。 fabricating
    silently は member/leader 記録を破壊するので構造的に避ける。
    """
    user_id = os.environ.get("BEACON_USER_ID", "").strip()
    email = os.environ.get("BEACON_USER_EMAIL", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()

    # ms-61 / e-2132 — credentials.json fallback for env-missing case.
    if not email or not user_id:
        cred_user_id, cred_email = _read_credentials_for_identity()
        if not email:
            email = cred_email
        if not user_id:
            user_id = cred_user_id

    if not user_id:
        try:
            import getpass
            user_id = getpass.getuser()
        except Exception:
            user_id = ""
    return user_id, email, session_id


def cmd_trek_create():
    """Create a new trek (= top-level cross-project collaboration area).

    Reads from env:
      BEACON_TREK_TITLE       (required) trek title
      BEACON_TREK_TYPE        temporary | persistent (default persistent)
      BEACON_TREK_DESCRIPTION free-form description (optional)
      BEACON_USER_ID          creator user_id (fallback: whoami)
      BEACON_USER_EMAIL       creator email (required)
      BEACON_SESSION_ID       creator session_id (required, becomes leader)
      BEACON_JSON             "1" → emit json instead of human text
    """
    import trek
    import trek_store

    title = os.environ.get("BEACON_TREK_TITLE", "").strip()
    type_ = os.environ.get("BEACON_TREK_TYPE", "").strip() or "persistent"
    description = os.environ.get("BEACON_TREK_DESCRIPTION", "")
    # ms-75 / e-1865: optional acceptance criterion / completion marker for the
    # trek. Empty = "leader decides", non-empty = explicit signal that members
    # can match against to suggest archive.
    goal_state = os.environ.get("BEACON_TREK_GOAL_STATE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: trek title is required (--title or positional arg)",
              file=sys.stderr)
        sys.exit(1)

    user_id, email, session_id = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to create a trek "
            "(= recorded as creator/leader member email)",
            file=sys.stderr,
        )
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required to create a trek "
            "(= the session that creates becomes initial leader; SPEC 方針 9)",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud path: server resolves creator identity from auth token.
        # BEACON_USER_ID / BEACON_USER_EMAIL are ignored server-side (kept
        # for local-mode parity).
        try:
            client, _config = _get_api_client()
            new_doc = client.create_trek(
                title=title,
                creator_session_id=session_id,
                description=description,
                type_=type_,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            new_doc = trek.new_trek(
                title=title,
                creator_user_id=user_id,
                creator_email=email,
                creator_session_id=session_id,
                description=description,
                type_=type_,
                goal_state=goal_state,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(new_doc)

    if json_mode:
        print(json.dumps(new_doc, ensure_ascii=False))
    else:
        print(f"Created trek {new_doc['trek_id']} \"{new_doc['title']}\" "
              f"({new_doc['type']}, status={new_doc['status']})")
        print(f"  leader session: {new_doc['leader_session_id']}")
        print(f"  creator: {new_doc['creator_actor']['email']}")
        print("  next: `beacon trek plan` で scope を追加、"
              "`beacon trek invite` でメンバーを呼ぶ、"
              "`beacon trek start <id>` で active に")


def cmd_trek_list():
    """List treks. By default hides archived; --all includes them.

    Reads from env:
      BEACON_TREK_STATUS       optional status filter
      BEACON_TREK_INCLUDE_ARCHIVED  "1" → include archived
      BEACON_USER_ID           visibility filter (defaults to current user
                               so the listing is per-actor; pass --all to
                               disable from CLI)
      BEACON_TREK_ALL_ACTORS   "1" → disable actor filter (admin view)
      BEACON_JSON              "1" → emit json
    """
    import trek_store

    status_filter = os.environ.get("BEACON_TREK_STATUS", "").strip() or None
    include_archived = os.environ.get("BEACON_TREK_INCLUDE_ARCHIVED", "") == "1"
    all_actors = os.environ.get("BEACON_TREK_ALL_ACTORS", "") == "1"
    # ms-75 / e-1813: filter to treks the current user has actually joined
    # (= members[].joined_at non-empty for them). Pending invitations stay
    # out of the joined list so /beacon-session-start can display "current
    # treks" without mixing in invitations the user hasn't yet accepted.
    joined_only = os.environ.get("BEACON_TREK_JOINED_ONLY", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if all_actors:
        actor_id = None
        actor_email = ""
    else:
        user_id, actor_email, _ = _resolve_creator_identity()
        actor_id = user_id or None

    # ms-84 Phase 2: Store 経由で cloud / local を統一。 actor_id (= local-only
    # filter) と all_actors (= cloud admin view) はそれぞれ片方の backend が
    # ignore する設計。 cloud transport / 403 は RuntimeError として呼び出し
    # 側 (= ここ) で従来通り display する。
    try:
        treks = get_store().list_treks(
            actor_id=actor_id,
            status=status_filter or "",
            include_archived=include_archived,
            all_actors=all_actors,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if joined_only:
        # Walk members[] for an entry matching the caller (user_id or email)
        # whose ``joined_at`` is non-empty. This is the only structurally
        # reliable join check — bare visibility (= creator/member presence)
        # would include treks the user was invited to but never accepted.
        def _is_joined(t: dict) -> bool:
            for m in t.get("members") or []:
                if (actor_id and m.get("user_id") == actor_id) \
                        or (actor_email and m.get("email") == actor_email):
                    if m.get("joined_at"):
                        return True
            return False
        treks = [t for t in treks if _is_joined(t)]

    if json_mode:
        print(json.dumps(treks, ensure_ascii=False, indent=2))
        return

    if not treks:
        if joined_only:
            print("(no joined treks — `beacon trek join <id>` で招待を承諾)")
        elif actor_id:
            print(f"(no treks visible to {actor_id} — try --all で全件)")
        else:
            print("(no treks yet — `beacon trek create \"title\"` で最初の trek を起票)")
        return

    print(f"Treks ({len(treks)}):")
    for t in treks:
        status_icon = {
            "planning": "○",
            "active": "●",
            "archived": "□",
        }.get(t.get("status", ""), "?")
        halt_marker = " [halted]" if t.get("halt") else ""
        member_count = len(t.get("members") or [])
        scope_count = len(t.get("scope") or [])
        print(f"  {status_icon} {t['trek_id']:14s} {t['title'][:55]}"
              f" — {t.get('type', '?')}/{t.get('status', '?')}"
              f"{halt_marker}, {member_count}m/{scope_count}s")


def _current_project_id() -> str:
    """Return the current project's id (= what ``load_project()`` operates on).

    Used by trek-show / trek-timeline aggregation to decide which scope
    entries it can resolve locally (= same-project) and which are
    cross-project hints the caller must visit separately.

    Resolution order (ms-83 / e-2007 dogfood finding):
      1. ``data.id`` / ``data.project_id`` on the local project.json
         (= local mode and cloud-cached layouts that store the id inline)
      2. ``.beacon/cloud.json`` ``project_id`` (= cloud mode default —
         project.json is the cached document and does not embed the id)
      3. Empty string if neither path resolves
    """
    try:
        data = load_project()
    except Exception:
        data = {}
    pid = (data.get("id") or data.get("project_id") or "").strip()
    if pid:
        return pid
    try:
        cloud_path = _get_cloud_config_path()
        if os.path.exists(cloud_path):
            with open(cloud_path, "r", encoding="utf-8") as f:
                cloud_cfg = json.load(f)
            return (cloud_cfg.get("project_id") or "").strip()
    except Exception:
        pass
    return ""


def _scope_matches_entry(scope: list[dict], project_id: str,
                         entry: dict, ms_id: str) -> bool:
    """Return True if ``entry`` (in milestone ``ms_id``) is in the trek scope.

    A scope row matches when its ``project`` matches AND any narrowing key
    (milestone / task) matches. Project-wide scope (= no narrowing) is a
    catch-all that includes every milestone/task in the project.
    """
    if not scope or not project_id:
        return False
    eid = entry.get("id", "")
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        # Project-wide scope row → always matches.
        narrow_keys = [k for k in ("milestone", "task", "operation")
                       if row.get(k)]
        if not narrow_keys:
            return True
        if row.get("milestone") and row.get("milestone") == ms_id:
            return True
        if row.get("task") and row.get("task") == eid:
            return True
        # operation narrowing doesn't apply to milestone entries; let the
        # operation-walk pick it up instead.
    return False


def _scope_matches_operation(scope: list[dict], project_id: str,
                             op_id: str) -> bool:
    if not scope or not project_id:
        return False
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        if not any(row.get(k) for k in ("milestone", "task", "operation")):
            return True  # project-wide
        if row.get("operation") and row.get("operation") == op_id:
            return True
    return False


def _scope_matches_milestone(scope: list[dict], project_id: str,
                             ms_id: str) -> bool:
    if not scope or not project_id:
        return False
    for row in scope:
        if (row.get("project") or "") != project_id:
            continue
        if not any(row.get(k) for k in ("milestone", "task", "operation")):
            return True
        if row.get("milestone") and row.get("milestone") == ms_id:
            return True
    return False


def _collect_trek_local_aggregation(trek_doc: dict) -> dict:
    """Walk the current project and collect items in this trek's scope.

    Returns a dict shaped as::

        {
          "project_id": "<pid or empty>",
          "tasks_todo": [{ms_id, ms_title, id, description, status, priority}],
          "tasks_done_recent": [...],
          "commits_recent": [{ms_id, hash, summary, date}],
          "docs": [{doc_id, title, scope, milestone, trek_id}],
          "cross_project_scope": [{project, milestone/task/operation}],
        }

    Cross-project scope rows are surfaced as hints so the CLI prompts the
    user to cd into the other project; only the current project's items
    are walked here (= e-1864 CLI-side aggregation lives at the project
    grain, no remote fetch).
    """
    scope = trek_doc.get("scope") or []
    pid = _current_project_id()
    out: dict = {
        "project_id": pid,
        "tasks_todo": [],
        "tasks_done_recent": [],
        "commits_recent": [],
        "docs": [],
        "cross_project_scope": [s for s in scope
                                if (s.get("project") or "") != pid],
    }
    if not pid:
        return out
    try:
        data = load_project()
    except Exception:
        return out

    done_recent: list[dict] = []
    commits: list[dict] = []
    for ms in data.get("milestones", []) or []:
        ms_id = ms.get("id", "")
        ms_title = ms.get("title", "")
        for entry in ms.get("entries", []) or []:
            etype = entry.get("type", "")
            if etype == "commit":
                if _scope_matches_milestone(scope, pid, ms_id) or \
                   _scope_matches_entry(scope, pid, entry, ms_id):
                    commits.append({
                        "ms_id": ms_id,
                        "hash": (entry.get("meta") or {}).get("hash", ""),
                        "summary": entry.get("description", ""),
                        "date": entry.get("created_at", ""),
                    })
                continue
            if not _scope_matches_entry(scope, pid, entry, ms_id):
                continue
            row = {
                "ms_id": ms_id,
                "ms_title": ms_title,
                "id": entry.get("id", ""),
                "description": entry.get("description", ""),
                "status": entry.get("status", ""),
                "priority": (entry.get("meta") or {}).get("priority", ""),
                "type": etype,
            }
            if entry.get("status") == "todo":
                out["tasks_todo"].append(row)
            elif entry.get("status") == "done":
                done_recent.append(row | {"done_at": entry.get("done_at", "")})

    # Operations matched at trek scope (= ms-75 4.4 UC7-F4 ハイブリッド入口、
    # ここでは entries は別途扱わず、Operation 自体の状況を要約)
    ops_in_scope = []
    for op in data.get("operations", []) or []:
        op_id = op.get("id", "")
        if _scope_matches_operation(scope, pid, op_id):
            ops_in_scope.append({
                "id": op_id,
                "title": op.get("title", ""),
                "status": op.get("status", ""),
            })
    out["operations"] = ops_in_scope

    # Sort recent commits / done tasks newest-first, cap to 5 each so the
    # default view stays readable. --detail / --all in the caller can lift
    # the cap if we want a full expansion (= e-1864 AC 2).
    commits.sort(key=lambda c: c.get("date", ""), reverse=True)
    done_recent.sort(key=lambda r: r.get("done_at", ""), reverse=True)
    out["commits_recent"] = commits[:5]
    out["tasks_done_recent"] = done_recent[:5]

    # Forward doc lookup (= e-1866): docs tagged with this trek_id in the
    # current project's docs/ directory. Cross-project doc lookup lives
    # behind /api/treks/{tid}/documents (cloud-mode).
    docs: list[dict] = []
    docs_dir = _get_docs_dir()
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                doc = _read_local_doc(os.path.join(docs_dir, fname))
            except (OSError, UnicodeDecodeError):
                continue
            if doc.get("trek_id") != trek_doc.get("trek_id"):
                continue
            docs.append({
                "doc_id": doc.get("doc_id", ""),
                "title": doc.get("title", ""),
                "scope": doc.get("scope", ""),
                "milestone": doc.get("milestone", ""),
                "updated_at": doc.get("updated_at", ""),
            })
    out["docs"] = docs
    return out


def _goal_state_status(trek_doc: dict, agg: dict) -> str:
    """Return a 1-line readable status of the trek's goal_state field.

    The criterion itself is free-form text, so we cannot programmatically
    score completion. Instead we surface a simple counter ("3 todo / 7
    done") that members can match against the text to decide whether to
    suggest archive. This is intentionally lightweight — the SPEC explicitly
    rejects "completion enforcement" as overdesign.
    """
    goal = (trek_doc.get("goal_state") or "").strip()
    if not goal:
        return ""
    todo = len(agg.get("tasks_todo") or [])
    done = len(agg.get("tasks_done_recent") or [])
    return (
        f"goal:        {goal}\n"
        f"  progress (current project): {todo} todo / {done} recently-done"
        + (" — consider `beacon trek archive` if criterion is met."
           if todo == 0 and done > 0 else "")
    )


def cmd_trek_show():
    """Show a single trek by id with task / commit / doc aggregation.

    Reads from env:
      BEACON_TREK_ID  required
      BEACON_JSON     "1" → emit json (= raw trek doc + ``aggregation`` key)
      BEACON_ALL      "1" → uncap recent lists (= --detail equivalent)

    ms-75 / e-1864: human output now surfaces the trek's scoped tasks /
    commits / docs from the *current* project so a CLI-driven workflow can
    see Trek-wide progress without bouncing to the Web UI. Cross-project
    scope rows are surfaced as hints (= the user must cd into the other
    project) — the CLI intentionally does not fan out into other
    project.json files to keep aggregation cheap and unambiguous.
    """
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    expand = os.environ.get("BEACON_ALL", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    # ms-84 Phase 2: Store 経由で cloud / local を統一。 Store.get_trek は
    # ValueError on unknown / RuntimeError on transport の error contract を
    # 両 backend で共有しているため、 CLI 側はバックエンドを意識せず
    # 同じ except 分岐で扱える。
    try:
        t = get_store().get_trek(trek_id)
    except ValueError:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Build local aggregation regardless of cloud/local — the current
    # project view is always available.
    agg = _collect_trek_local_aggregation(t)

    if json_mode:
        # Emit the trek doc as-is, plus an ``aggregation`` key. Existing
        # consumers (= /beacon-trek-execute Step 1) that only read top-level
        # fields are unaffected; new consumers can opt into the aggregation
        # block by name (= forward-compatible).
        out = dict(t)
        out["aggregation"] = agg
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    halt_marker = " [HALTED]" if t.get("halt") else ""
    print(f"Trek {t['trek_id']} — {t['title']}{halt_marker}")
    print(f"  type:        {t.get('type')}")
    print(f"  status:      {t.get('status')}")
    print(f"  created:     {t.get('created_at', '')[:19]}")
    if t.get("archived_at"):
        print(f"  archived:    {t.get('archived_at', '')[:19]}")
    creator = t.get("creator_actor") or {}
    print(f"  creator:     {creator.get('email')} (user_id={creator.get('user_id')})")
    print(f"  leader sess: {t.get('leader_session_id')}")
    if t.get("description"):
        print(f"  description: {t['description']}")
    goal_line = _goal_state_status(t, agg)
    if goal_line:
        # _goal_state_status returns 1-2 lines, prefix each with two spaces
        # so it visually aligns with the other show fields.
        for line in goal_line.split("\n"):
            print(f"  {line}" if not line.startswith("  ") else line)
    members = t.get("members") or []
    print(f"  members ({len(members)}):")
    for m in members:
        joined = "joined" if m.get("joined_at") else "invited"
        print(f"    - {m.get('email')} [{m.get('role')}] ({joined})")
    scope = t.get("scope") or []
    print(f"  scope ({len(scope)}):")
    for s in scope:
        # ms-75 / e-1864 AC 6: surface bind grain (project / project:task=eXXX /
        # project:ms=msXX / project:op=opXX) explicitly so members understand
        # whether the trek covers a whole project or a narrow item.
        narrow = [(k, v) for k, v in s.items() if k != "project"]
        if narrow:
            ref_str = ", ".join(f"{k}={v}" for k, v in narrow)
            print(f"    - {s.get('project')}:{ref_str}")
        else:
            print(f"    - {s.get('project')} (project-wide)")
    if t.get("halt"):
        h = t["halt"]
        print(f"  halt: at={h.get('issued_at')} by={h.get('issued_by_session_id')}"
              + (f" reason={h.get('reason')}" if h.get("reason") else ""))

    # ms-75 / e-1864: aggregation sections. Cross-project scope rows can't
    # be expanded locally, so we surface them as hints rather than silently
    # omitting items the user expects to see.
    if agg.get("cross_project_scope"):
        print()
        print(f"  cross-project scope (not aggregated locally; cd into the "
              f"other project for detail):")
        for s in agg["cross_project_scope"]:
            narrow = [(k, v) for k, v in s.items() if k != "project"]
            if narrow:
                ref_str = ", ".join(f"{k}={v}" for k, v in narrow)
                print(f"    - {s.get('project')}:{ref_str}")
            else:
                print(f"    - {s.get('project')} (project-wide)")

    todos = agg.get("tasks_todo") or []
    if todos:
        print()
        print(f"  open tasks in scope ({len(todos)}):")
        shown = todos if expand else todos[:10]
        for row in shown:
            pri = f" [{row['priority']}]" if row.get("priority") else ""
            print(f"    ○ [{row['id']}]{pri} {row['description'][:80]}")
        if not expand and len(todos) > len(shown):
            print(f"    … {len(todos) - len(shown)} more (pass --all to "
                  f"expand)")

    done_recent = agg.get("tasks_done_recent") or []
    if done_recent:
        print()
        print(f"  recently done in scope ({len(done_recent)} shown):")
        for row in done_recent:
            print(f"    ● [{row['id']}] {row['description'][:80]}")

    ops_in_scope = agg.get("operations") or []
    if ops_in_scope:
        print()
        print(f"  Operations in scope ({len(ops_in_scope)}):")
        for op in ops_in_scope:
            print(f"    - [{op['id']}] {op['title'][:60]} ({op['status']})")

    commits = agg.get("commits_recent") or []
    if commits:
        print()
        print(f"  recent commits in scope ({len(commits)} shown):")
        for c in commits:
            short = (c.get("hash") or "")[:8]
            print(f"    {short:8s} {c['summary'][:80]}")

    docs = agg.get("docs") or []
    if docs:
        print()
        print(f"  trek docs ({len(docs)}):")
        scope_icons = {"core": "*", "spec": "+", "memo": "-",
                       "retro": "~", "report": "!"}
        for d in docs:
            icon = scope_icons.get(d.get("scope") or "memo", "?")
            print(f"    {icon} [{d.get('scope', 'memo')}] "
                  f"{d['doc_id']}: {d['title'][:60]}")


def cmd_trek_timeline():
    """Show a chronological timeline of trek-scoped events (ms-75 / e-1867).

    Combines (a) trek lifecycle events (= created / status / halt / scope
    changes / members) reconstructed from the trek doc's timestamps,
    (b) scope-matching commits and task done events from the current
    project, (c) trek-scoped doc additions, and (d) Trek-scope DM events
    if a local bus_log file is available. Cross-project events live behind
    the Tauri / Web UI (ms-72) and are surfaced here only as a one-line
    hint pointing to the trek_id.

    Reads from env:
      BEACON_TREK_ID  required
      BEACON_JSON     "1" → emit json (list of events, newest first)
      BEACON_LIMIT    integer (default 50) — cap event count
    """
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    try:
        limit = int(os.environ.get("BEACON_LIMIT", "50") or "50")
    except ValueError:
        limit = 50

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    # ms-84 Phase 2 (e-2036): Store.get_trek unifies the cloud / local
    # branch. ValueError on not-found, RuntimeError on auth / transport
    # propagates as the original cloud branch behavior.
    try:
        t = get_store().get_trek(trek_id)
    except ValueError:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    events: list[dict] = []

    # (a) trek lifecycle.
    if t.get("created_at"):
        events.append({"at": t["created_at"], "kind": "trek_created",
                       "summary": f"trek {trek_id} created"})
    if t.get("archived_at"):
        events.append({"at": t["archived_at"], "kind": "trek_archived",
                       "summary": "trek archived"})
    for m in t.get("members") or []:
        if m.get("invited_at"):
            events.append({
                "at": m["invited_at"], "kind": "member_invited",
                "summary": f"invited {m.get('email')} [{m.get('role')}]",
            })
        if m.get("joined_at"):
            events.append({
                "at": m["joined_at"], "kind": "member_joined",
                "summary": f"joined {m.get('email')} [{m.get('role')}]",
            })
    halt = t.get("halt") or {}
    if halt.get("issued_at"):
        reason = halt.get("reason") or "(no reason)"
        events.append({
            "at": halt["issued_at"], "kind": "halt_engaged",
            "summary": f"halt engaged: {reason}",
        })

    # (b) scoped commits + done tasks from current project.
    pid = _current_project_id()
    if pid:
        try:
            data = load_project()
        except Exception:
            data = {}
        scope = t.get("scope") or []
        for ms in data.get("milestones", []) or []:
            ms_id = ms.get("id", "")
            for entry in ms.get("entries", []) or []:
                etype = entry.get("type", "")
                if etype == "commit":
                    if not (_scope_matches_milestone(scope, pid, ms_id)
                            or _scope_matches_entry(scope, pid, entry, ms_id)):
                        continue
                    events.append({
                        "at": entry.get("created_at", ""),
                        "kind": "commit",
                        "summary": (
                            f"[{ms_id}] {entry.get('description', '')[:80]}"
                        ),
                    })
                    continue
                if not _scope_matches_entry(scope, pid, entry, ms_id):
                    continue
                if entry.get("done_at"):
                    events.append({
                        "at": entry["done_at"],
                        "kind": "task_done",
                        "summary": (
                            f"[{entry.get('id', '')}] "
                            f"{entry.get('description', '')[:80]}"
                        ),
                    })

    # (c) trek-scoped docs.
    docs_dir = _get_docs_dir()
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                doc = _read_local_doc(os.path.join(docs_dir, fname))
            except (OSError, UnicodeDecodeError):
                continue
            if doc.get("trek_id") != trek_id:
                continue
            events.append({
                "at": doc.get("updated_at", ""),
                "kind": "doc",
                "summary": (
                    f"[{doc.get('scope', 'memo')}] {doc.get('doc_id', '')}: "
                    f"{doc.get('title', '')[:70]}"
                ),
            })

    # Newest first; cap.
    events.sort(key=lambda e: e.get("at", ""), reverse=True)
    events = events[:max(limit, 0)] if limit > 0 else events

    if json_mode:
        print(json.dumps(events, ensure_ascii=False))
        return

    if not events:
        print(f"Trek {trek_id} timeline: (no events found in current "
              f"project; cross-project events are visible via the Web / "
              f"Tauri UI)")
        return

    print(f"Trek {trek_id} timeline ({len(events)} events shown, newest "
          f"first):")
    kind_icons = {
        "trek_created": "*", "trek_archived": "!", "member_invited": "+",
        "member_joined": "+", "halt_engaged": "!", "commit": ">",
        "task_done": "●", "doc": "-",
    }
    for ev in events:
        when = (ev.get("at") or "")[:19]
        icon = kind_icons.get(ev.get("kind", ""), "?")
        print(f"  {when}  {icon} [{ev.get('kind')}] {ev.get('summary', '')}")


def _trek_transition(trek_id: str, to_status: str):
    """Helper: validate + apply a state transition. Returns the updated trek.

    Local mode only. Cloud mode dispatches via the dedicated start/archive
    endpoints on the server (= caller branches before invoking this).
    """
    import trek
    import trek_store

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    cur = t.get("status", "")
    try:
        trek.validate_transition(cur, to_status)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    updates = {
        "status": to_status,
        "updated_at": trek.utcnow_iso(),
    }
    if to_status == "archived":
        updates["archived_at"] = updates["updated_at"]
    return trek_store.update_trek(trek_id, updates=updates)


def cmd_trek_start():
    """Transition trek planning → active."""
    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.start_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = _trek_transition(trek_id, "active")
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Started trek {t['trek_id']} (status: active)")


def cmd_trek_archive():
    """Transition trek → archived (terminal)."""
    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.archive_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = _trek_transition(trek_id, "archived")
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Archived trek {t['trek_id']} — recorded for posterity, "
              "再開したい時は新 trek 起票")


def cmd_trek_invite():
    """Invite a user (by email) to a trek (= add to members[] with joined_at='').

    Env:
      BEACON_TREK_ID       (required) trek to invite into
      BEACON_TREK_ACTOR    (required) invitee's email
      BEACON_TREK_NOTIFY   "1" → also send a live DM (= e-1662 で実装、
                           現在は acknowledged but no-op)
      BEACON_USER_EMAIL    inviter's email (= invited_by, defaults to whoami)
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    actor_email = os.environ.get("BEACON_TREK_ACTOR", "").strip()
    notify = os.environ.get("BEACON_TREK_NOTIFY", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not actor_email:
        print("Error: --actor <email> is required", file=sys.stderr)
        sys.exit(1)

    inviter_user_id, _, _ = _resolve_creator_identity()

    if _is_cloud_mode():
        # Cloud path: server resolves the invitee's user_id via
        # find_user_by_email + records inviter from auth token.
        try:
            client, _config = _get_api_client()
            t = client.invite_trek_member(trek_id, actor_email)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        # Local mode identity: user_id = email (cloud mode resolves properly
        # via auth in e-1656). When the invitee later runs `beacon trek join`,
        # their BEACON_USER_ID must match — easiest path is to also use email
        # there.
        invitee_user_id = actor_email
        try:
            trek.add_invitation(
                t, user_id=invitee_user_id, email=actor_email,
                invited_by_user_id=inviter_user_id,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        member_count = len(t.get("members") or [])
        print(f"Invited {actor_email} to trek {trek_id} "
              f"(members: {member_count})")
        if notify:
            # e-1662 で bus DM 経路を実装予定。現在は --notify を受け付ける
            # だけで、DM は飛ばない (= invitee が trigger check / session-start
            # で気付く流れ default)。
            print("  (--notify is acknowledged but live DM is not implemented "
                  "yet — invitee will see the invitation via trigger check; "
                  "live DM lands in e-1662)")


# ms-75 / e-2047 — Trek auto-arm constants. The 3 channels covered here
# match CHANNEL_TO_SKILL in channel/bus-autonomous-content.mjs (= e-2069)
# so the bus.mjs side has a Skill mapping for every channel we mark as
# auto-execute. Default budget = 20 outbound turns gives the executor
# roughly two cadence cycles' worth of room before requiring a re-grant,
# which is the SPEC-pinned starting envelope per AC 1.
TREK_AUTO_ARM_CHANNELS = (
    "trek-progress-check",
    "trek-trigger",
    "trek-task-review",
)
TREK_AUTO_ARM_DEFAULT_BUDGET = 20


def _arm_for_trek(trek_id: str) -> dict:
    """Run the 3 auto-arm actions for a freshly-joined Trek.

    ms-75 / e-2047 AC 1 — pre-armed Trek participation: a session that
    joins a Trek should be ready to act on scope-internal DMs without
    requiring the user to remember to add channels + grant budget + start
    the /beacon-bus-armed Skill. This helper performs the first two
    structurally (= file writes) and surfaces a hint for the third (=
    Skill invocation belongs to the AI side, not CLI).

    Idempotency:
      * channel allowlist add is idempotent — re-running for the same
        trek is a no-op for channels already present.
      * budget set is unconditional (= refresh to default). Re-joining a
        trek effectively re-arms; this matches the user intent (= "I am
        rejoining this work, give me a fresh runway").

    Returns a dict with the actions taken so callers can render an audit
    summary or emit JSON without re-reading the files.
    """
    import datetime
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    added: list[str] = []
    for ch in TREK_AUTO_ARM_CHANNELS:
        if ch not in channels:
            channels.append(ch)
            added.append(ch)
    if added:
        data["bus_auto_execute_channels"] = channels
        save_project(data, op={
            "op": "trek_auto_arm",
            "trek_id": trek_id,
            "channels_added": added,
        })
        _mirror_auto_execute_channels_to_local(channels)
    budget_data = {
        "total": TREK_AUTO_ARM_DEFAULT_BUDGET,
        "used": 0,
        "granted_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "channels": [],
        "trek_id": trek_id,  # audit marker (= auto-arm source)
    }
    _write_bus_budget(budget_data)
    return {
        "trek_id": trek_id,
        "channels": list(channels),
        "channels_added": added,
        "budget_turns": TREK_AUTO_ARM_DEFAULT_BUDGET,
    }


# ms-88 / e-2090 — Trek 参加 per-session 明示同意 gate。
# Trek 参加 = scope 内 DM blanket 自動承認 + autonomous loop 入場の合算で
# turn 制限なく AI を動かす権限委譲。 typed-ack を求めることで user が
# consequence を理解せず参加する構造的危険を塞ぐ。
TREK_JOIN_CONSENT_PHRASE = "I UNDERSTAND"


def _trek_join_consent_gate(trek_id: str, email: str, *, json_mode: bool) -> None:
    """Gate ``beacon trek join`` on per-session 明示同意 (ms-88 / e-2090).

    Behavior:
    - TTY stdin → prompt for the literal phrase ``I UNDERSTAND``. Mismatch =
      abort with exit 1 and a one-line explanation. Case-sensitive (= a
      thoughtful enough action to require precise typing, not a reflexive y).
    - Non-TTY stdin (= bot / CI / Skill pipe) → refuse and instruct caller
      to use ``--i-understand-the-implications`` flag explicitly. We never
      auto-accept on non-TTY because that path is exactly the silent bypass
      this gate exists to close.
    - ``json_mode`` is irrelevant to the gate itself but affects the abort
      payload (= keep stderr human-readable either way; the gate is a UX
      checkpoint, not a JSON API).
    """
    explanation = (
        f"\n⚠ Trek 参加 = AI を turn 制限なく走らせる権限委譲です\n"
        f"  trek_id: {trek_id}\n"
        f"  joining as: {email}\n"
        f"\n"
        f"  参加すると次の挙動が unlock されます:\n"
        f"  - scope 内 DM が blanket 自動承認 (= ms-70 / e-1854) で配信される\n"
        f"  - trek-progress-check 等の channel が autonomous execution に乗る\n"
        f"  - server-side scheduler が定期的に「次やって」 と push してくる\n"
        f"  - 不可逆 action (= deploy / release / external write) は user_review に\n"
        f"    forward されますが、 それ以外は AI 判断で実行されます\n"
        f"\n"
        f"  撤回したい場合: `beacon trek leave {trek_id}` で同等の手順で抜けられます。\n"
    )

    if not sys.stdin.isatty():
        # Non-TTY (= bot / CI / Skill pipe) は typed prompt を取れない。
        # silent auto-accept は本 gate の趣旨に反するので明示 flag を要求。
        sys.stderr.write(explanation)
        sys.stderr.write(
            "\n  非 TTY 経由のため typed-ack を取れません。 自動化 / bot 経路で\n"
            "  参加する場合は `--i-understand-the-implications` を明示的に渡してください。\n"
        )
        sys.exit(1)

    sys.stderr.write(explanation)
    sys.stderr.write(
        f"\n  意味を理解したうえで参加する場合は次のフレーズを正確に入力してください\n"
        f"  (case-sensitive、 余分な空白なし):\n"
        f"    {TREK_JOIN_CONSENT_PHRASE}\n"
        f"\n  > "
    )
    sys.stderr.flush()
    try:
        typed = input("").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n  aborted by user — Trek 参加を中止しました。\n")
        sys.exit(1)
    if typed != TREK_JOIN_CONSENT_PHRASE:
        sys.stderr.write(
            f"\n  入力 {typed!r} が期待値と一致しません — Trek 参加を中止しました。\n"
            f"  必要なら再度 `beacon trek join {trek_id}` から実行してください。\n"
        )
        sys.exit(1)
    # consent OK; fall through


def cmd_trek_join():
    """Accept an invitation (= set member.joined_at = now for the current user).

    ms-75 / e-2047 — by default also auto-arms the session for the joined
    Trek: adds trek-progress-check / trek-trigger / trek-task-review to
    bus_auto_execute_channels (idempotent), sets the outbound-send budget
    to 20 turns, and prints a hint to start the /beacon-bus-armed Skill.
    Opt-out: ``BEACON_TREK_NO_ARM=1`` (mapped from ``--no-arm`` at the
    bash / dispatch.py shim).

    Env:
      BEACON_TREK_ID     (required)
      BEACON_USER_EMAIL  (required, matched against the invitee's email)
      BEACON_USER_ID     inviter's recorded user_id (fallback: whoami).
                         Used as the local-mode identity match.
      BEACON_SESSION_ID  (informational, recorded if leader_session_id is empty)
      BEACON_TREK_NO_ARM "1" → skip auto-arm post-join
      BEACON_JSON        "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    no_arm = os.environ.get("BEACON_TREK_NO_ARM", "") == "1"
    consent_ack = os.environ.get("BEACON_TREK_CONSENT_ACK", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to join a trek",
            file=sys.stderr,
        )
        sys.exit(1)

    # ms-88 / e-2090 — per-session 明示同意 gate。 Trek 参加 = scope 内 DM blanket
    # 自動承認 (= ms-70 / e-1854) + autonomous loop 入場の合算で turn 制限なく
    # AI を動かす権限委譲。 user が consequence を理解せず参加する構造的危険を
    # 構造的に塞ぐため、 typed-ack または明示 flag を要求。
    # bypass 経路: --i-understand-the-implications flag (dispatch.py で
    # BEACON_TREK_CONSENT_ACK=1 にマップされる)、 または BEACON_TREK_CONSENT_ACK=1
    # 環境変数 (= テスト fixture / 自動化用)。
    if not consent_ack:
        _trek_join_consent_gate(trek_id, email, json_mode=json_mode)

    if _is_cloud_mode():
        # Cloud path: server identifies the joiner from auth token (no
        # email lookup needed). Non-invited callers get 403.
        try:
            client, _config = _get_api_client()
            t = client.join_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        # Local mode identity match: prefer email lookup first (= simplest path
        # for the invitee who is told "look for an invitation to email X"); fall
        # back to user_id lookup. Use the looked-up member's user_id for the
        # actual accept call.
        member = trek.find_member_by_email(t, email)
        if member is None:
            member = trek.find_member(t, user_id)
        if member is None:
            print(
                f"Error: no invitation found for {email} (user_id={user_id}) "
                f"in trek {trek_id} — owner must `beacon trek invite` first",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            trek.accept_invitation(t, user_id=member["user_id"])
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    arm_summary: Optional[dict] = None
    if not no_arm:
        try:
            arm_summary = _arm_for_trek(trek_id)
        except Exception as exc:
            # Best-effort: join succeeded; arm is a UX enhancement. Surface
            # the failure but do not unwind the join (= the user can re-run
            # the arm steps manually if needed).
            print(
                f"[warning] join succeeded but auto-arm failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    if json_mode:
        # Keep the trek doc as the top-level shape so existing consumers
        # (= tests / Skill bodies parsing the trek doc directly) keep
        # working. The arm summary rides on a meta key (= `_arm`, the
        # underscore prefix signals "extra context, not part of the trek
        # schema").
        out = dict(t)
        if arm_summary is not None:
            out["_arm"] = arm_summary
        elif no_arm:
            out["_arm"] = {"skipped": True, "reason": "--no-arm"}
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Joined trek {trek_id} as {email}")
        if arm_summary is not None:
            added = arm_summary.get("channels_added") or []
            if added:
                print(
                    f"  auto-arm: added {len(added)} channel(s) to "
                    f"bus_auto_execute_channels: {', '.join(added)}"
                )
            else:
                print(
                    "  auto-arm: bus_auto_execute_channels already covers "
                    f"trek channels ({', '.join(TREK_AUTO_ARM_CHANNELS)})"
                )
            print(
                f"  auto-arm: budget granted "
                f"{arm_summary['budget_turns']} outbound sends "
                f"(refresh: `beacon bus budget grant --turns N`)"
            )
            print(
                "  next step: start the autonomous loop in this session via "
                "`/beacon-bus-armed` Skill so trek-progress-check events "
                "wake the executor without a user prompt."
            )
            print(
                "  opt-out: re-run with `--no-arm` to skip auto-arm "
                "(= only join, no channel / budget changes)."
            )
        elif no_arm:
            print(
                "  auto-arm skipped (--no-arm). "
                "Run `beacon bus auto-execute add --channel trek-progress-check` "
                "and `beacon bus budget grant --turns 20` manually to arm later."
            )


def cmd_trek_stop():
    """Engage the Andon cord (= set the trek's halt field).

    Env:
      BEACON_TREK_ID    (required)
      BEACON_TREK_REASON optional human-readable reason
      BEACON_SESSION_ID (required, recorded as issued_by_session_id)
      BEACON_JSON       "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    reason = os.environ.get("BEACON_TREK_REASON", "")
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required (= recorded as the session "
            "that pulled the Andon cord)",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.set_trek_halt(
                trek_id, issued_by_session_id=session_id, reason=reason,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            trek.set_halt(t, issued_by_session_id=session_id, reason=reason)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        suffix = f" — {reason}" if reason else ""
        print(f"STOP signal raised on trek {trek_id} by {session_id}{suffix}")
        print("  All participating sessions will halt their autonomous work.")
        print("  Resume with: beacon trek resume " + trek_id)


def cmd_trek_resume():
    """Clear the halt signal. Idempotent.

    Env:
      BEACON_TREK_ID  (required)
      BEACON_JSON     "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        # Server idempotently clears halt; we cannot know "was it halted"
        # from the post-clear response, so the message just confirms the
        # outcome.
        try:
            client, _config = _get_api_client()
            t = client.clear_trek_halt(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Resumed trek {trek_id} — sessions can continue work")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    had_halt = bool(t.get("halt"))
    trek.clear_halt(t)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        if had_halt:
            print(f"Resumed trek {trek_id} — sessions can continue work")
        else:
            print(f"Trek {trek_id} was not halted (no-op)")


def cmd_trek_pulse_ack():
    """Self-report /beacon-trek-pulse Skill invocation (ms-88 / e-2106).

    Layer 2 (= observability) of the 3-layer trek autonomy harness. The
    Skill calls this as Step 3 (after picking a choice) so the server has
    ground truth that the Skill actually fired in response to a scheduler
    tick. Without this self-report, the server can only infer compliance
    from indirect signals (= commit / DM activity), and dogfood (=
    tk-40b0b27c) showed those signals are insufficient.

    Env:
      BEACON_TREK_ID            (required)
      BEACON_SESSION_ID         (required) — keyed in pulse_acks[]
      BEACON_TREK_PICKED_CHOICE optional — 'terminal' / 'continue' /
                                'dm-leader' / 'no-op' / ''
      BEACON_TREK_NOTE          optional short context (= 200 char cap)
      BEACON_JSON               "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    picked_choice = os.environ.get("BEACON_TREK_PICKED_CHOICE", "").strip()
    note = os.environ.get("BEACON_TREK_NOTE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required (= recorded as the "
            "pulse-ack source)",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            entry = client.pulse_ack_trek(
                trek_id, session_id=session_id,
                picked_choice=picked_choice, note=note,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(entry, ensure_ascii=False))
        else:
            total = entry.get("total_acks", 0)
            last = entry.get("last_pulse_ack_at", "")
            choice = entry.get("last_picked_choice", "")
            print(
                f"pulse-ack recorded: trek={trek_id} session={session_id} "
                f"total_acks={total} last_choice={choice or '(unset)'} "
                f"last_at={last}"
            )
        return

    # Local mode: directly mutate trek_doc.
    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    try:
        trek.record_pulse_ack(
            t, session_id=session_id,
            picked_choice=picked_choice, note=note,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)
    entry = (t.get("pulse_acks") or {}).get(session_id) or {}
    if json_mode:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        total = entry.get("total_acks", 0)
        last = entry.get("last_pulse_ack_at", "")
        choice = entry.get("last_picked_choice", "")
        print(
            f"pulse-ack recorded: trek={trek_id} session={session_id} "
            f"total_acks={total} last_choice={choice or '(unset)'} "
            f"last_at={last}"
        )


def cmd_trek_take_over():
    """Take over leader_session_id for a fresh session of the same user (ms-88 / e-2089).

    Why this exists:
    - ``leader_session_id`` is bound to whatever session called
      ``beacon trek create`` (or the last ``transfer-leader`` target).
    - When that session dies (= Mac restart / terminal closed / fresh bclaude
      from clean env), the leader_session_id is **stale-but-non-null**:
      scheduler still fan-outs to it, DM gates still treat it as leader,
      but there's no live bclaude on the other side.
    - ``transfer-leader`` can't recover this because it requires the
      *current* leader session to authorize the swap — the dead session
      can't do that.
    - ``take-over`` is the fresh-session recovery path: any joined member
      with ``role == 'leader'`` (= user-grain check) can claim the
      ``leader_session_id`` for their **current** session in one call.

    This is *not* the same as ``join`` (= acceptance of invitation, member
    creation) or ``transfer-leader`` (= consensual handoff between live
    sessions). It is specifically for "the durable leader role is mine
    (user-grain), the live binding pointed at a dead session — bind it to
    me now".

    Env:
      BEACON_TREK_ID       (required)
      BEACON_SESSION_ID    (required) — becomes the new leader_session_id
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    session_id = os.environ.get("BEACON_SESSION_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not session_id:
        print(
            "Error: BEACON_SESSION_ID is required "
            "(= the calling session becomes the new leader_session_id)",
            file=sys.stderr,
        )
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.take_over_trek(trek_id, session_id=session_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)

        # Local-mode authorization: caller must be a *joined leader* member
        # of this trek (user-grain). Email lookup first (= simpler UX), then
        # user_id. The point of take-over is that the calling user already
        # holds the durable leader role — we only re-bind the live session
        # pointer to a fresh session_id under that user.
        member = trek.find_member_by_email(t, email) if email else None
        if member is None:
            member = trek.find_member(t, user_id)
        if member is None:
            print(
                f"Error: you are not a member of trek {trek_id} "
                f"(user_id={user_id!r}, email={email!r}). take-over only "
                f"works for joined leader members.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not member.get("joined_at"):
            print(
                f"Error: you have not joined trek {trek_id} yet "
                f"(invitation pending). Run `beacon trek join` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        if member.get("role") != "leader":
            print(
                f"Error: only leader members can take over a trek "
                f"(your role: {member.get('role')!r}). Use "
                f"`beacon trek transfer-leader` from the current leader's "
                f"session if you need to be promoted first.",
                file=sys.stderr,
            )
            sys.exit(1)

        prior_leader = t.get("leader_session_id") or ""
        if prior_leader == session_id:
            # Already bound — idempotent. Print a friendly notice and exit
            # 0 so chained scripts don't trip on a re-run.
            if json_mode:
                print(json.dumps(t, ensure_ascii=False))
            else:
                print(
                    f"Leader of trek {trek_id} already bound to this session "
                    f"({session_id}); no-op."
                )
            return

        trek.transfer_leader(t, target_session_id=session_id)
        trek_store.save_trek(t)
        if json_mode:
            out = dict(t)
            out["_take_over"] = {
                "prior_leader_session_id": prior_leader,
                "new_leader_session_id": session_id,
            }
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(
                f"Took over leader of trek {trek_id}: "
                f"{prior_leader or '(unset)'} → {session_id}"
            )
        return

    # Cloud mode — server already validated leader role; just print result.
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Took over leader of trek {trek_id}: "
            f"new leader_session_id = {session_id}"
        )


def cmd_trek_transfer_leader():
    """Hand off leader_session_id to another session.

    Env:
      BEACON_TREK_ID       (required)
      BEACON_TREK_TO       (required) target session_id
      BEACON_JSON          "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    target = os.environ.get("BEACON_TREK_TO", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not target:
        print("Error: --to <session_id> is required", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud mode requires caller's session as `from_session_id` for the
        # session-grain check. We use BEACON_SESSION_ID (the calling CLI's
        # session). Server also enforces user-grain leader role.
        from_session = os.environ.get("BEACON_SESSION_ID", "").strip()
        if not from_session:
            print(
                "Error: BEACON_SESSION_ID is required in cloud mode "
                "(= the caller's session; server checks it equals the "
                "current leader_session_id)",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            client, _config = _get_api_client()
            t = client.transfer_trek_leader(
                trek_id, from_session_id=from_session, to_session_id=target,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Transferred leader of trek {trek_id}: "
                  f"{from_session} → {target}")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)

    prior_leader = t.get("leader_session_id")
    try:
        trek.transfer_leader(t, target_session_id=target)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Transferred leader of trek {trek_id}: "
              f"{prior_leader} → {target}")


def cmd_trek_task_state():
    """Stamp Trek-internal task state (ms-75 / e-2048 + ms-88 / e-2107).

    Env:
      BEACON_TREK_ID         (required)
      BEACON_TREK_TASK_ID    (required, the entry id e-XXXX)
      BEACON_TREK_STATE      (required, one of todo/working/leader_review/
                              user_review/done; legacy `waiting-review`
                              auto-migrates to leader_review)
      BEACON_TREK_NOTE       (optional)
      BEACON_JSON            "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    task_id = os.environ.get("BEACON_TREK_TASK_ID", "").strip()
    state = os.environ.get("BEACON_TREK_STATE", "").strip()
    note = os.environ.get("BEACON_TREK_NOTE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not task_id:
        print("Error: task_id is required (e.g. e-2034)", file=sys.stderr)
        sys.exit(1)
    if not state:
        print(
            "Error: state is required (one of todo/working/leader_review/"
            "user_review/done; legacy `waiting-review` も accepted、 ms-88 e-2107)",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        trek.validate_task_state(state)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = client.set_trek_task_state(
                trek_id, task_id=task_id, state=state, note=note,
            )
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(
                f"Stamped trek {trek_id} task {task_id} state → {state}"
            )
            if state in trek.TERMINAL_TASK_STATES:
                print(
                    "  Leader has been notified via trek-task-review DM "
                    "(= /beacon-trek-review surface)."
                )
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)
    caller_sid = os.environ.get("BEACON_SESSION_ID", "")
    try:
        trek.set_task_state(
            t,
            task_id=task_id,
            state=state,
            updated_by_session_id=caller_sid,
            note=note,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)
    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(
            f"Stamped trek {trek_id} task {task_id} state → {state} "
            "(local mode; review notification skipped — no bus path)"
        )


def cmd_trek_plan():
    """Edit a trek's scope (= what work items the trek is concerned with).

    Env (exactly one of add/remove must be set):
      BEACON_TREK_ID            (required)
      BEACON_TREK_SCOPE_ADD     "<project>[:<ref>]" — append a scope entry
      BEACON_TREK_SCOPE_REMOVE  "<project>[:<ref>]" — remove a scope entry
      BEACON_JSON               "1" → json output

    ``ref`` prefix dispatches: ``ms-...`` → milestone, ``op-...`` → operation,
    ``e-...`` → task; omitted = project-wide scope.
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    add_arg = os.environ.get("BEACON_TREK_SCOPE_ADD", "").strip()
    remove_arg = os.environ.get("BEACON_TREK_SCOPE_REMOVE", "").strip()
    # ms-75 / e-1865: optional goal_state setter. Empty string clears it.
    # ``BEACON_TREK_GOAL_STATE_SET`` distinguishes "user passed --goal-state ''"
    # (= clear) from "user did not pass --goal-state at all" (= preserve).
    goal_state_arg = os.environ.get("BEACON_TREK_GOAL_STATE", "")
    goal_state_explicit = (
        os.environ.get("BEACON_TREK_GOAL_STATE_SET", "") == "1"
    )
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)
    if not add_arg and not remove_arg and not goal_state_explicit:
        print(
            "Error: --add-scope <ref>, --remove-scope <ref>, or --goal-state "
            "<text> is required",
            file=sys.stderr,
        )
        sys.exit(1)
    if add_arg and remove_arg:
        print(
            "Error: pass --add-scope or --remove-scope, not both in one call",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse the scope arg with the shared helper so cloud and local agree on
    # the entry shape. We do this BEFORE branching so syntax errors surface
    # the same way regardless of mode. goal_state-only calls skip parsing.
    entry: dict | None = None
    if add_arg or remove_arg:
        try:
            entry = trek.parse_scope_arg(add_arg or remove_arg)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    if _is_cloud_mode():
        try:
            client, _config = _get_api_client()
            t = None
            if entry is not None:
                kwargs = {
                    "project": entry["project"],
                    "milestone": entry.get("milestone", ""),
                    "operation": entry.get("operation", ""),
                    "task": entry.get("task", ""),
                }
                if add_arg:
                    t = client.add_trek_scope(trek_id, **kwargs)
                else:
                    t = client.remove_trek_scope(trek_id, **kwargs)
            if goal_state_explicit:
                # ms-75 / e-1865: goal_state setter via cloud API. The server
                # endpoint is wired in a follow-up (cloud-mode parity will
                # land alongside the rest of e-1865 server work); in the
                # meantime cloud users get a graceful warning rather than a
                # silent no-op.
                if hasattr(client, "set_trek_goal_state"):
                    t = client.set_trek_goal_state(
                        trek_id, goal_state=goal_state_arg,
                    )
                else:
                    print(
                        "warn: --goal-state is local-mode only until the "
                        "server endpoint lands (e-1865 cloud parity). "
                        "Set it locally first; the cloud copy will be "
                        "updated by the next sync.",
                        file=sys.stderr,
                    )
                    if t is None:
                        # Nothing to print downstream — bail without error so
                        # the user sees the warning and can re-plan.
                        sys.exit(0)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        t = trek_store.load_trek(trek_id)
        if t is None:
            print(f"Error: trek {trek_id} not found", file=sys.stderr)
            sys.exit(1)
        try:
            if entry is not None:
                if add_arg:
                    trek.add_scope_entry(t, entry=entry)
                else:
                    trek.remove_scope_entry(t, entry=entry)
            if goal_state_explicit:
                trek.set_goal_state(t, goal_state=goal_state_arg)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        if add_arg or remove_arg:
            verb = "Added" if add_arg else "Removed"
            ref_display = (add_arg or remove_arg)
            print(f"{verb} scope {ref_display} on trek {trek_id} "
                  f"(scope: {len(t.get('scope') or [])} items)")
        if goal_state_explicit:
            new_val = (goal_state_arg or "").strip()
            if new_val:
                print(f"goal_state set on trek {trek_id}: \"{new_val}\"")
            else:
                print(f"goal_state cleared on trek {trek_id}")


def cmd_trek_leave():
    """Leave a trek (= remove self from members[]).

    Env:
      BEACON_TREK_ID    (required)
      BEACON_USER_EMAIL (required, matched against the member's email)
      BEACON_USER_ID    fallback
      BEACON_JSON       "1" → json output
    """
    import trek
    import trek_store

    trek_id = os.environ.get("BEACON_TREK_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not trek_id:
        print("Error: trek_id is required", file=sys.stderr)
        sys.exit(1)

    user_id, email, _ = _resolve_creator_identity()
    if not email:
        print(
            "Error: BEACON_USER_EMAIL is required to leave a trek",
            file=sys.stderr,
        )
        sys.exit(1)

    if _is_cloud_mode():
        # Cloud path: server identifies the leaver from auth token; rejects
        # leader-removal / last-member with 400.
        try:
            client, _config = _get_api_client()
            t = client.leave_trek(trek_id)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if json_mode:
            print(json.dumps(t, ensure_ascii=False))
        else:
            print(f"Left trek {trek_id} ({email})")
        return

    t = trek_store.load_trek(trek_id)
    if t is None:
        print(f"Error: trek {trek_id} not found", file=sys.stderr)
        sys.exit(1)

    member = trek.find_member_by_email(t, email) or trek.find_member(t, user_id)
    if member is None:
        print(
            f"Error: {email} (user_id={user_id}) is not a member of trek "
            f"{trek_id}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        trek.remove_member(t, user_id=member["user_id"])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    trek_store.save_trek(t)

    if json_mode:
        print(json.dumps(t, ensure_ascii=False))
    else:
        print(f"Left trek {trek_id} ({email})")


def cmd_member_add():
    """Add a member to the project."""
    import operations  # lazy import to avoid circular at module load

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    name = os.environ.get("BEACON_MEMBER_NAME", "")
    email = os.environ.get("BEACON_MEMBER_EMAIL", "")
    role = os.environ.get("BEACON_MEMBER_ROLE", "") or "contributor"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id:
        print("Error: member id is required", file=sys.stderr)
        print("Usage: beacon member add <id> [--name N] [--email E] [--role owner|maintainer|contributor|viewer]",
              file=sys.stderr)
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        new_member = core.member_add(data, member_id, name=name, email=email, role=role)
        return data, new_member

    try:
        new_member = operations.apply_operation(
            project_id, op,
            op_name="member.add",
            reason=f"add member {member_id} as {role}",
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(new_member, ensure_ascii=False))
    else:
        print(f"Added member {new_member['id']} ({new_member['role']})")
        print("  note: this adds a Beacon project member. GitHub repo")
        print("  collaborator access is separate — set via `gh repo edit --add-collaborator <user>`.")


def cmd_member_list():
    """List members of the project.

    ms-78 e-1807: prefer display_name (= the human-friendly label set during
    invite accept) over the raw id / email when present. Local-mode members
    use the id-based schema; cloud members use the user_id-based schema —
    the display logic accepts both.
    """
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    members = core.members_list(data)
    if json_mode:
        print(json.dumps(members, ensure_ascii=False, indent=2))
        return
    if not members:
        print("(no members — `beacon member add <id>` to add the first one)")
        return
    print(f"Members ({len(members)}):")
    # Pretty-print: align role column for readability. Use display_name (or
    # name fallback) as the primary label; email goes in the parens.
    def label(m):
        return (m.get("display_name") or m.get("name") or
                m.get("id") or m.get("user_id") or m.get("email") or "?")
    width = max(len(str(label(m))) for m in members) + 2
    for m in members:
        role = m.get("role", "?")
        lbl = label(m)
        email = m.get("email", "")
        extras = []
        if email and email != lbl:
            extras.append(email)
        extras_str = f"  ({', '.join(extras)})" if extras else ""
        print(f"  {str(lbl):<{width}} {role:<11}{extras_str}")


def cmd_member_remove():
    """Remove a member from the project."""
    import operations

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id:
        print("Error: member id is required", file=sys.stderr)
        sys.exit(1)
    if not reason:
        # e-630: state-changing operations require an audit reason.
        print(
            "Error: --reason is required for member remove "
            "(audit trail per CORE doc data-immutability-principle)",
            file=sys.stderr,
        )
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        removed = core.member_remove(data, member_id, reason=reason)
        return data, removed

    try:
        removed = operations.apply_operation(
            project_id, op,
            op_name="member.remove",
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(removed, ensure_ascii=False))
    else:
        print(f"Removed member {removed['id']}")


def cmd_member_role():
    """Change a member's role."""
    import operations

    member_id = (os.environ.get("BEACON_MEMBER_ID", "") or "").strip()
    role = (os.environ.get("BEACON_MEMBER_ROLE", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not member_id or not role:
        print("Error: member id and role are required", file=sys.stderr)
        print("Usage: beacon member role <id> <owner|maintainer|contributor|viewer>",
              file=sys.stderr)
        sys.exit(1)

    project_id = _project_id_for_ops()

    def op(data):
        updated = core.member_set_role(data, member_id, role)
        return data, updated

    try:
        updated = operations.apply_operation(
            project_id, op,
            op_name="member.role",
            reason=f"set role of {member_id} to {role}",
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(updated, ensure_ascii=False))
    else:
        print(f"Set {updated['id']} role to {updated['role']}")


# ---------------------------------------------------------------------------
# Member invitations (ms-78 e-1805) — token-based invite flow.
# These thin commands call the server REST API (= no local-mode equivalent;
# invitations always go through the cloud project doc). The Skill / CLI / Web
# trinity stays symmetric: the same UC11 invitation can be issued, listed,
# and cancelled from any of the three surfaces.
# ---------------------------------------------------------------------------

def _resolve_cloud_project_id() -> tuple[str, "object", "object"]:
    """Resolve the active cloud project_id + an authenticated ApiClient.

    Returns (project_id, client, creds). Raises SystemExit on failure with a
    user-friendly message. Used by `member invite / invitation list / cancel /
    join / whoami` to share one auth-resolve path.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    # Read cloud project_id from .beacon/cloud.json (= the same file
    # `beacon cloud join` writes).
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    if not os.path.exists(cloud_path):
        print(
            "No cloud project bound to this cwd.\n"
            "Run `beacon cloud join <project-id>` or `beacon cloud setup` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with open(cloud_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        pid = cfg.get("project_id", "")
    except Exception as e:
        print(f"Error reading {cloud_path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not pid:
        print(f"cloud.json has no project_id", file=sys.stderr)
        sys.exit(1)
    return pid, client, creds


def cmd_member_invite():
    """Issue an invite URL for a Beacon project member (ms-78 e-1805)."""
    email = (os.environ.get("BEACON_MEMBER_EMAIL", "") or "").strip()
    role = (os.environ.get("BEACON_MEMBER_ROLE", "") or "viewer").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not email:
        print("Error: email is required", file=sys.stderr)
        print("Usage: beacon member invite <email> [--role viewer|editor]",
              file=sys.stderr)
        sys.exit(1)
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.post(
            f"/api/projects/{pid}/invitations",
            {"email": email, "role": role},
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(resp, ensure_ascii=False))
        return
    url = resp.get("url", "")
    exp = (resp.get("expires_at") or "")[:10]
    print(f"Invite URL for {email} ({role}):")
    print(f"  {url}")
    print(f"  expires: {exp}")
    print()
    print("  Send this URL to the invitee. It can only be used once.")
    print("  note: this adds a Beacon project member only. GitHub repo")
    print("  collaborator access is separate — set via")
    print("  `gh repo edit --add-collaborator <user>`.")


def cmd_member_invitation_list():
    """List pending invitations for the current cloud project (ms-78 e-1805)."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.get(f"/api/projects/{pid}/invitations")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    items = resp.get("invitations", [])
    if json_mode:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("(no pending invitations)")
        return
    print(f"Pending invitations ({len(items)}):")
    for inv in items:
        exp = (inv.get("expires_at") or "")[:10]
        print(f"  {inv.get('id', '?'):<12} {inv.get('email', ''):<32} "
              f"{inv.get('role', ''):<8} expires {exp}")


def cmd_member_invitation_cancel():
    """Cancel a pending invitation by id (ms-78 e-1805)."""
    invitation_id = (os.environ.get("BEACON_INVITATION_ID", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not invitation_id:
        print("Error: invitation id required", file=sys.stderr)
        print("Usage: beacon member invitation cancel <id>", file=sys.stderr)
        sys.exit(1)
    pid, client, _ = _resolve_cloud_project_id()
    try:
        resp = client.delete(f"/api/projects/{pid}/invitations/{invitation_id}")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if json_mode:
        print(json.dumps(resp, ensure_ascii=False))
        return
    inv = resp.get("invitation") or {}
    print(f"Cancelled invitation {invitation_id} ({inv.get('email', '')}).")


def cmd_member_join():
    """Accept an invite token and bind this cwd to the joined project (ms-78 e-1805).

    Server-side: consumes the invite + adds caller to project members[].
    Client-side: writes .beacon/cloud.json + .beacon/project.json so subsequent
    Beacon commands operate against the joined project.
    """
    token = (os.environ.get("BEACON_INVITE_TOKEN", "") or "").strip()
    display_name = (os.environ.get("BEACON_DISPLAY_NAME", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not token:
        print("Error: --token is required", file=sys.stderr)
        print("Usage: beacon member join --token <token> [--display-name <name>]",
              file=sys.stderr)
        sys.exit(1)
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login first.", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    try:
        resp = client.post(
            f"/api/invitations/{token}/accept",
            {"display_name": display_name},
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    pid = resp.get("project_id", "")
    role = resp.get("role", "")
    if not pid:
        print(f"Server returned no project_id: {resp}", file=sys.stderr)
        sys.exit(1)
    # Bind this cwd to the joined project by writing cloud.json + project.json,
    # mirroring `beacon cloud join`.
    try:
        data = client.get_project(pid)
    except RuntimeError as e:
        print(f"Joined project {pid}, but failed to fetch project doc: {e}",
              file=sys.stderr)
        sys.exit(1)
    core.validate_project(data)
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    os.makedirs(beacon_dir, exist_ok=True)
    cloud_config_path = os.path.join(beacon_dir, "cloud.json")
    with open(cloud_config_path, "w", encoding="utf-8") as f:
        json.dump({"project_id": pid, "api_url": api_url}, f,
                  indent=2, ensure_ascii=False)
        f.write("\n")
    pf = get_project_file()
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if json_mode:
        print(json.dumps({
            "status": "joined",
            "project_id": pid,
            "project_name": data.get("name", ""),
            "role": role,
            "display_name": display_name,
        }, ensure_ascii=False))
        return
    print(f"Joined project {data.get('name', pid)} ({pid}) as {role}.")
    if display_name:
        print(f"  display name: {display_name}")
    print()
    print("Next steps:")
    print("  - Run `beacon channel install` in your work directory so other")
    print("    sessions can DM you.")
    print("  - In Claude Code, invoke `/beacon-onboard` to load project context.")
    print("  note: GitHub repo collaborator access is separate — ask the")
    print("  inviter if you need write access to the repo.")


def cmd_member_whoami():
    """Show the calling user's identity + role on the current cloud project (e-1805)."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login", file=sys.stderr)
        sys.exit(1)
    api_url = _resolve_active_api_url()
    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))
    # Best-effort: look at .beacon/cloud.json to scope role lookup.
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    pid = ""
    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                pid = (json.load(f) or {}).get("project_id", "")
        except Exception:
            pid = ""
    email = ""
    sub = ""
    if isinstance(creds, dict):
        # Token payload may not include email; rely on auth claims if present.
        email = creds.get("email", "") or creds.get("user_email", "")
        sub = creds.get("sub", "") or creds.get("user_id", "")
    # Fallback: ask the server for the caller's projects + role list.
    role = ""
    project_name = ""
    if pid:
        try:
            resp = client.get(f"/api/projects/{pid}/members")
            owner_email = resp.get("owner_email", "")
            if email and owner_email == email:
                role = "owner"
            else:
                for m in resp.get("members", []) or []:
                    if (m.get("email") or "") == email:
                        role = m.get("role", "")
                        break
            # Also try to recover the project name for nicer display
            try:
                proj = client.get_project(pid)
                project_name = proj.get("name", "")
            except Exception:
                pass
        except RuntimeError:
            pass
    out = {
        "email": email,
        "user_id": sub,
        "project_id": pid,
        "project_name": project_name,
        "role": role,
    }
    if json_mode:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"email:        {email or '(unknown — token has no email claim)'}")
    if sub:
        print(f"user_id:      {sub}")
    if pid:
        print(f"project_id:   {pid}")
        if project_name:
            print(f"project_name: {project_name}")
        print(f"role:         {role or '(not a member)'}")
    else:
        print("(no cloud project bound to this cwd — run `beacon cloud join <id>`)")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def cmd_summary():
    # ms-57 / e-1040 deprecation completed: `beacon summary "text"` writes
    # are now a no-op. Reads still work for legacy CLI surfaces that
    # haven't migrated to project-vision / session_logs yet — they get
    # whatever was last written before the soft-deprecation ended.
    # Cross-session hand-off → `beacon session log`. Human narrative →
    # project-vision CORE doc.
    text = os.environ.get("BEACON_SUMMARY_TEXT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    if text:
        # No write. Surface the deprecation so the caller knows their
        # input was ignored, but keep JSON callers quiet (mirrors the
        # pattern in cmd_log_finalize).
        if not json_mode and not os.environ.get("BEACON_SUPPRESS_DEPRECATION"):
            sys.stderr.write(
                "[deprecated] `beacon summary <text>` is no longer a write "
                "(ms-57 / e-1040 completed). Cross-session hand-off → "
                "`beacon session log`; human narrative → `project-vision` "
                "CORE doc. Your input was IGNORED. This command will be "
                "removed in a future release.\n"
            )
        if json_mode:
            print(json.dumps(
                {"summary": data.get("summary", ""),
                 "write_ignored": True,
                 "deprecated_since": "e-1040"},
                ensure_ascii=False,
            ))
        else:
            print("Summary write ignored (deprecated; see stderr).")
    elif json_mode:
        print(json.dumps({"summary": data.get("summary", "")}, ensure_ascii=False))
    else:
        print(data.get("summary", "(未設定)"))


# ---------------------------------------------------------------------------
# Save (ms-16)
# ---------------------------------------------------------------------------

def cmd_save():
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    source = os.environ.get("BEACON_SOURCE", "")
    url = os.environ.get("BEACON_URL", "")
    revision_id = os.environ.get("BEACON_REVISION_ID", "")
    hash_val = os.environ.get("BEACON_HASH", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    date = os.environ.get("BEACON_DATE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not source:
        print("Error: --source is required", file=sys.stderr)
        sys.exit(1)
    if not description:
        print("Error: description is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    result = core.save_entry(data, ms_id=ms_id, description=description,
                             source=source, date=date, url=url,
                             revision_id=revision_id, hash=hash_val,
                             progress=progress)
    save_project(data)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Duplicate save skipped (source={source}, ms={result['milestone']})")
        else:
            print(f"Saved [{result['entry_id']}] to {result['milestone']}: {description}")


# ---------------------------------------------------------------------------
# Milestone depends / workspace / graph (ms-17)
# ---------------------------------------------------------------------------

def cmd_milestone_depends():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    depends_on_str = os.environ.get("BEACON_DEPENDS_ON", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    if clear:
        ms = core.milestone_depends(data, ms_id, [])
    else:
        if not depends_on_str:
            print("Error: --on is required (or use --clear)", file=sys.stderr)
            sys.exit(1)
        deps = [d.strip() for d in depends_on_str.split(",") if d.strip()]
        ms = core.milestone_depends(data, ms_id, deps)
    save_project(data)

    if json_mode:
        print(json.dumps({"id": ms["id"], "depends_on": ms.get("depends_on", [])}, ensure_ascii=False))
    else:
        deps = ms.get("depends_on", [])
        if deps:
            print(f"{ms_id} depends on: {', '.join(deps)}")
        else:
            print(f"{ms_id}: dependencies cleared")


def cmd_milestone_workspace():
    # OSS: git worktree lifecycle core
    # Human Executor notification (beacon trigger fire) is handled below and is closed-source.
    #
    # ms-81 e-1917: this verb is being demoted to a deprecated alias of
    # `milestone start`. The common "create worktree + flip status" path
    # belongs on `start` so status / assignee / worktree always lift
    # together. Two legacy sub-modes survive here for back-compat:
    #   - `--clear`: legacy workspace-field clear (kept; pure data op)
    #   - `--dir <path>` (BEACON_NO_GIT=1): legacy explicit-path mode that
    #     bypasses git worktree creation entirely (kept; some scripts use it)
    # The default no-arg path emits a deprecation warning and delegates to
    # `cmd_milestone_start` so callers stop accumulating drift.
    import subprocess
    from datetime import datetime, timezone

    ms_id = os.environ.get("BEACON_MS_ID", "")
    workspace = os.environ.get("BEACON_WORKSPACE", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    executor = os.environ.get("BEACON_EXECUTOR", "")  # ai | human | human:<user>
    no_git = os.environ.get("BEACON_NO_GIT", "") == "1"  # skip git worktree add (for --dir mode)

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # ms-81 e-1917: deprecation alias. If neither legacy sub-mode is in play
    # (= no --clear, no --dir/no-git), the caller is using the default
    # worktree-create path which has moved to `milestone start`.
    if not clear and not no_git:
        print(
            "[ms-81 deprecation] `beacon milestone workspace` is being "
            "absorbed by `beacon milestone start` so that status / assignee "
            "/ worktree always activate together. Forwarding to "
            "`milestone start` now; please update callers to use "
            "`beacon milestone start " + ms_id + "` directly.",
            file=sys.stderr,
        )
        cmd_milestone_start()
        return

    if clear:
        # Legacy --clear: remove the old workspace field only
        ms = core.milestone_workspace(data, ms_id, "")
        save_project(data)
        if json_mode:
            print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
        else:
            print(f"{ms_id}: workspace cleared")
        return

    if workspace and no_git:
        # Legacy --dir mode: set workspace path without git worktree
        ms = core.milestone_workspace(data, ms_id, workspace)
        save_project(data)
        if json_mode:
            print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
        else:
            print(f"{ms_id} workspace: {workspace}")
        return

    # OSS: git worktree add + milestone field update
    # Determine worktree path and branch
    workspace_branch = f"{ms_id}/work"
    workspace_path = os.path.join(".worktrees", ms_id)

    # ms-65 e-1476: worktree creation now lives in lib/worktree.py so the
    # upcoming cwd-aware ``beacon milestone start`` (e-1477) can share the
    # same retry-on-existing-branch behaviour. The shape of the legacy CLI
    # output (stderr line on idempotent reuse, exit 1 on failure) is
    # preserved so existing dispatch flows don't see a change.
    import worktree as _worktree
    try:
        wt = _worktree.create_workspace(workspace_path, workspace_branch)
    except _worktree.GitNotInstalledError:
        print("Error: git not found in PATH", file=sys.stderr)
        sys.exit(1)
    except _worktree.WorktreeCreateError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if not wt["created"]:
        print(f"Worktree already exists at {workspace_path}", file=sys.stderr)

    assigned_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    effective_executor = executor or "ai"

    ms = core.milestone_worktree_set(
        data, ms_id,
        workspace_branch=workspace_branch,
        workspace_path=workspace_path,
        executor=effective_executor,
        executor_assigned_at=assigned_at,
    )
    # Also update legacy workspace field for backward compatibility
    ms = core.milestone_workspace(data, ms_id, workspace_path)
    save_project(data)

    # Closed-source: Human Executor notification via beacon trigger
    if effective_executor.startswith("human"):
        user_part = effective_executor[len("human:"):] if ":" in effective_executor else ""
        user_label = f" ({user_part})" if user_part else ""
        msg = (
            f"ブランチ {workspace_branch} をチェックアウトして作業開始: "
            f"git checkout {workspace_branch}"
        )
        try:
            subprocess.run(
                ["beacon", "trigger", "fire", f"{ms_id}-workspace", msg],
                capture_output=True, text=True
            )
        except FileNotFoundError:
            pass  # beacon not in PATH; skip trigger

    if json_mode:
        print(json.dumps({
            "id": ms_id,
            "workspace_branch": workspace_branch,
            "workspace_path": workspace_path,
            "executor": effective_executor,
            "executor_assigned_at": assigned_at,
        }, ensure_ascii=False))
    else:
        print(f"{ms_id} worktree: {workspace_path}  branch: {workspace_branch}  executor: {effective_executor}")


def cmd_milestone_workspace_cleanup():
    # OSS: git merge + git worktree remove lifecycle cleanup
    import subprocess

    ms_id = os.environ.get("BEACON_MS_ID", "")
    merge_to = os.environ.get("BEACON_MERGE_TO", "")  # target branch for merge
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    ms = next((m for m in data["milestones"] if m["id"] == ms_id), None)
    if ms is None:
        print(f"Error: milestone not found: {ms_id}", file=sys.stderr)
        sys.exit(1)

    workspace_path = ms.get("workspace_path") or ms.get("workspace")
    if not workspace_path:
        print(f"Error: {ms_id} has no workspace_path set", file=sys.stderr)
        sys.exit(1)

    abs_workspace_path = os.path.abspath(workspace_path)
    workspace_branch = ms.get("workspace_branch", "")

    errors = []

    # Step 1: git merge (OSS: git worktree lifecycle)
    if merge_to:
        # Merge workspace branch into the specified branch
        if workspace_branch:
            merge_result = subprocess.run(
                ["git", "merge", workspace_branch],
                capture_output=True, text=True,
                cwd=os.path.abspath(".")  # run from project root
            )
        else:
            merge_result = subprocess.run(
                ["git", "merge", abs_workspace_path],
                capture_output=True, text=True
            )
        if merge_result.returncode != 0:
            errors.append(f"git merge failed: {merge_result.stderr.strip()}")

    # Step 2: git worktree remove (OSS: git worktree lifecycle)
    if os.path.exists(abs_workspace_path):
        rm_result = subprocess.run(
            ["git", "worktree", "remove", abs_workspace_path, "--force"],
            capture_output=True, text=True
        )
        if rm_result.returncode != 0:
            errors.append(f"git worktree remove failed: {rm_result.stderr.strip()}")

    # Step 3: clear milestone worktree fields
    core.milestone_worktree_clear(data, ms_id)
    # Also clear legacy workspace field
    core.milestone_workspace(data, ms_id, "")
    save_project(data)

    if json_mode:
        print(json.dumps({
            "id": ms_id,
            "cleaned_up": True,
            "errors": errors,
        }, ensure_ascii=False))
    else:
        if errors:
            for e in errors:
                print(f"Warning: {e}", file=sys.stderr)
        print(f"{ms_id}: workspace cleaned up (path: {workspace_path})")


def cmd_milestone_graph():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    graph = core.milestone_graph(data)

    if json_mode:
        print(json.dumps(graph, ensure_ascii=False))
    else:
        for wave_info in graph["waves"]:
            wave_num = wave_info["wave"]
            cycle_marker = " [CYCLE]" if wave_info.get("cycle") else ""
            ms_ids = wave_info["milestones"]
            # Build display lines
            lines = []
            for ms_id in ms_ids:
                node = next((n for n in graph["nodes"] if n["id"] == ms_id), None)
                if node:
                    deps = node.get("depends_on", [])
                    dep_str = f" <- {', '.join(deps)}" if deps else ""
                    status_icon = {"done": "●", "in_progress": "◐", "todo": "○",
                                   "waiting": "◌", "observing": "◔"}.get(node["status"], "?")
                    lines.append(f"  {status_icon} {ms_id} {node['title']} ({node['progress']}%){dep_str}")
            print(f"Wave {wave_num}{cycle_marker}:")
            for line in lines:
                print(line)


# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------

def cmd_retro_prepare():
    """Prepare the JSON payload that /beacon-retro Skill renders into a
    weekly markdown.

    The historical core path (= ``core.collect_retro_entries``) is kept
    as the "per-MS narrative grouping" layer — it walks each milestone's
    entry tree recursively in date order, which is exactly what the Skill
    wants for the "今週の取り組み" section.

    ms-79 / e-1836 additions:

      - ``source_breakdown`` (source 別件数): a top-level facet showing
        how many of the week's entries came from human dialog vs auto-op
        (= envelope auto-execute) vs DM. Generated via the unified
        ``retro_query`` base so the same human/auto-op/dm tagging used
        by /beacon-retrospect is reused here without duplication.
      - ``catch_up`` block: when multiple retro slots are unreviewed
        (= the retro trigger reports ``overdue_slots`` length > 1), this
        block surfaces the overdue weeks list so the Skill can offer a
        catch-up batch path (e-1837 / UC5-F2). The flag
        ``BEACON_RETRO_CATCH_UP=1`` enables the listing; without it the
        block is omitted so existing Skill output is unchanged.
    """
    since = os.environ.get("BEACON_SINCE", "")
    until = os.environ.get("BEACON_UNTIL", "")
    catch_up_mode = os.environ.get("BEACON_RETRO_CATCH_UP", "") == "1"
    data = load_project()

    weekly_milestones = []
    for ms in data.get("milestones", []):
        ms_entries = core.collect_retro_entries(ms.get("entries", []), since, until)
        if ms_entries:
            weekly_milestones.append({
                "id": ms["id"],
                "title": ms.get("title", ""),
                "status": ms.get("status", ""),
                "progress": ms.get("progress", 0),
                "entries": ms_entries,
            })

    # Include deploy records that fall within the period
    weekly_deploys = []
    for dep in data.get("deployments", []):
        dep_date = (dep.get("date") or "")[:10]
        if (not since or dep_date >= since) and (not until or dep_date <= until):
            weekly_deploys.append({
                "id": dep["id"],
                "type": dep.get("type", ""),
                "date": dep.get("date", "")[:10],
                "milestones": dep.get("milestones", []),
                "newly_completed_ms": dep.get("newly_completed_ms", []),
                "description": dep.get("description", ""),
            })

    # ms-79 / e-1836: source breakdown via the unified retro_query base.
    # Counts how many of the week's history events were human vs auto-op
    # vs DM. Silent-fail-tolerant so the retro prepare path never breaks
    # on a malformed entry — we just omit the breakdown in that case.
    source_breakdown: dict[str, int] = {}
    try:
        import retro_query as _rq  # noqa: PLC0415
        documents = _load_local_documents()
        rq_result = _rq.retro_query(
            data,
            documents,
            from_date=since,
            to_date=until,
            limit=10_000,
        )
        source_breakdown = (rq_result.get("facets") or {}).get("source") or {}
    except Exception:
        pass

    output: dict = {
        "project": data.get("name", ""),
        "period": {"since": since, "until": until},
        "summary": data.get("summary", ""),
        "milestones": weekly_milestones,
        "deploys": weekly_deploys,
        "source_breakdown": source_breakdown,
    }

    # ms-79 / e-1837 (UC5-F2): catch-up batch info.
    # Read the retro trigger payload (= written by _auto_fire_retro_trigger)
    # to discover whether more than one slot is overdue. The trigger file
    # is the canonical record so we don't recompute the slot list here.
    if catch_up_mode:
        catch_up_block = _retro_catch_up_block()
        if catch_up_block:
            output["catch_up"] = catch_up_block

    print(json.dumps(output, ensure_ascii=False))


def _retro_catch_up_block() -> Optional[dict]:
    """Return the catch-up payload built from the persistent retro trigger.

    Shape::

        {
          "overdue_slots": ["2026-W23", "2026-W24", "2026-W25"],
          "count": 3,
          "since_first_overdue": "2026-06-01",
        }

    Returns ``None`` if there is no retro trigger (= nothing to catch up
    on) or only a single overdue slot (= the regular retro flow already
    covers it).
    """
    project_dir = os.path.dirname(get_project_file())
    trigger_path = os.path.join(project_dir, "triggers", "retro.json")
    if not os.path.exists(trigger_path):
        return None
    try:
        with open(trigger_path, "r", encoding="utf-8") as f:
            trig = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    overdue = trig.get("overdue_slots") or []
    if not isinstance(overdue, list) or len(overdue) < 2:
        return None
    # Compute the Monday of the earliest overdue slot for since-display.
    import datetime as _dt
    since_first = ""
    try:
        first_slot = overdue[0]  # YYYY-WNN
        year_str, wk_str = first_slot.split("-W")
        year = int(year_str); wk = int(wk_str)
        jan4 = _dt.date(year, 1, 4)
        week1_mon = jan4 - _dt.timedelta(days=jan4.weekday())
        first_mon = week1_mon + _dt.timedelta(weeks=wk - 1)
        since_first = first_mon.strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        pass
    return {
        "overdue_slots": overdue,
        "count": len(overdue),
        "since_first_overdue": since_first,
    }


def cmd_retro_default_since():
    """Print the recommended default `--since` date for `beacon retro`.

    ms-43 e-570: when the user delays a retro past the configured retro_day,
    the previous default ("this Monday") covers too short a window and misses
    the actual unreviewed period. Logic:

      1. Read `.reviewed` for the most recent reviewed ISO week (if any).
      2. If found, return the Monday of the **next** ISO week after that.
      3. Otherwise fall back to the most recent retro_day on or before today,
         minus 6 days (= the start of the slot being reviewed).
      4. Final fallback: this Monday.

    Output is a YYYY-MM-DD line; empty on error (the shell wrapper has its
    own date-arithmetic fallback so older installs still function).
    """
    import datetime
    try:
        today = datetime.date.today()
        # 1. Try the .reviewed marker first.
        reviewed = _last_reviewed_week()
        if reviewed:
            # ISO week format: YYYY-WNN. The Monday of the NEXT week begins
            # the period we still owe a retro for.
            try:
                year_str, wk_str = reviewed.split("-W")
                year = int(year_str); wk = int(wk_str)
                # ISO week's Monday: use isocalendar inverse.
                # week N's Monday = first ISO date with isocalendar() == (year, wk, 1)
                jan4 = datetime.date(year, 1, 4)  # Always in ISO week 1
                week1_mon = jan4 - datetime.timedelta(days=jan4.weekday())
                last_reviewed_mon = week1_mon + datetime.timedelta(weeks=wk - 1)
                next_mon = last_reviewed_mon + datetime.timedelta(days=7)
                # Don't let it go past today.
                if next_mon <= today:
                    print(next_mon.strftime("%Y-%m-%d"))
                    return
            except (ValueError, IndexError):
                pass  # malformed marker, fall through

        # 2. No marker: anchor on the most recent retro_day.
        retro_day = _get_retro_day()
        anchor = _most_recent_retro_day_on_or_before(today, retro_day)
        # Cover the week ending on the anchor day.
        since = anchor - datetime.timedelta(days=6)
        # But not past today (defensive).
        if since > today:
            since = today
        print(since.strftime("%Y-%m-%d"))
    except Exception:
        # Empty output → shell wrapper falls back to its own date math.
        pass


def cmd_retro_save():
    """Persist a retro markdown document for a given ISO week.

    Cloud mode: pushes to the cloud retros subcollection (the source of truth
    for the Web UI Reviews tab). Local mode: writes `.beacon/retro/{week}.md`.

    /beacon-retro Skill MUST call this instead of writing the file directly.
    The legacy Write-tool path orphaned retros in cloud mode because the only
    push path was the initial `beacon cloud push` migration.
    """
    week = os.environ.get("BEACON_RETRO_WEEK", "")
    content = os.environ.get("BEACON_CONTENT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not week:
        print("Error: --week required (e.g. 2026-W23)")
        sys.exit(1)

    import re
    if not re.match(r"^\d{4}-W\d{2}$", week):
        print(f"Error: week must be in YYYY-WNN format (got {week!r})")
        sys.exit(1)

    content = _resolve_content_input(content)

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.save_retro(config["project_id"], week, content)
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
        location = f"cloud:projects/{config['project_id']}/retros/{week}"
    else:
        project_dir = os.path.dirname(get_project_file())
        retro_dir = os.path.join(project_dir, "retro")
        os.makedirs(retro_dir, exist_ok=True)
        fpath = os.path.join(retro_dir, f"{week}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        location = fpath

    if json_mode:
        print(json.dumps({"week": week, "location": location}, ensure_ascii=False))
    else:
        print(f"Saved retro: {week} -> {location}")


def cmd_retro_done():
    import datetime
    today = datetime.date.today()
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"

    project_dir = os.path.dirname(get_project_file())
    retro_dir = os.path.join(project_dir, "retro")
    os.makedirs(retro_dir, exist_ok=True)
    reviewed_path = os.path.join(retro_dir, ".reviewed")
    with open(reviewed_path, "w", encoding="utf-8") as f:
        f.write(current_week + "\n")

    triggers_dir = os.path.join(project_dir, "triggers")
    retro_trigger = os.path.join(triggers_dir, "retro.json")
    if os.path.exists(retro_trigger):
        os.remove(retro_trigger)

    print(f"Retro reviewed: {current_week}")


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

def _get_triggers_dir():
    project_dir = os.path.dirname(get_project_file())
    return os.path.join(project_dir, "triggers")


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def _get_retro_day():
    try:
        data = load_project()
        day_str = data.get("retro_day", "friday").lower()
        return DAY_NAMES.get(day_str, 4)
    except Exception:
        return 4


def _last_reviewed_week() -> Optional[str]:
    """Return the most recent ISO-week string a retro was reviewed for, or None.

    Reads the `.beacon/retro/.reviewed` marker that `beacon retro done` writes.
    Used by the persistent retro trigger to distinguish "already retro'd this
    week" from "retro is overdue for one or more past weeks".
    """
    project_dir = os.path.dirname(get_project_file())
    reviewed_path = os.path.join(project_dir, "retro", ".reviewed")
    try:
        with open(reviewed_path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (FileNotFoundError, IOError):
        return None


def _iso_week_string(date) -> str:
    """`YYYY-WNN` formatted ISO week for a `datetime.date`."""
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02d}"


def _most_recent_retro_day_on_or_before(today, retro_day_idx: int):
    """Return the latest date <= today whose weekday() == retro_day_idx.

    If today *is* the retro day, returns today. Used to anchor the
    "current retro week" — every Friday (or configured day) starts a new
    retro slot that must be settled before it becomes "stale".
    """
    import datetime as _dt
    delta = (today.weekday() - retro_day_idx) % 7
    return today - _dt.timedelta(days=delta)


def _auto_fire_retro_trigger():
    """Fire (or refresh) the retro trigger until the user actually retros.

    Per e-575 / SPEC ms-40 §B-1: the retro trigger MUST persist across days
    until the corresponding week is reviewed. Earlier behavior fired only on
    the configured retro day and `_cleanup_stale_triggers` deleted it the
    next morning — meaning a busy user who deferred retro to Monday saw the
    reminder vanish.

    New behavior:
      - Compute the most recent retro-day date on or before today
      - Identify the ISO-week of that retro day as the "current retro slot"
      - If `.reviewed` says we already retro'd that week or later, do nothing
      - Otherwise fire/refresh the trigger with a message that:
          - Names the unreviewed week (or weeks, if multiple slots accumulated)
          - Persists across days until `beacon retro done` writes a fresh
            `.reviewed` value
    """
    import datetime
    today = datetime.date.today()
    retro_day = _get_retro_day()

    # The retro slot for "this period" anchors on the most recent retro_day.
    # Before the first retro_day has occurred for the current week, we don't
    # have a slot yet (the week's data isn't ready to review). Skip silently.
    anchor = _most_recent_retro_day_on_or_before(today, retro_day)
    if anchor > today:
        return
    current_slot = _iso_week_string(anchor)

    last_reviewed = _last_reviewed_week()
    # If we've already reviewed this slot (or a later one), no trigger needed.
    if last_reviewed and last_reviewed >= current_slot:
        return

    # Determine whether multiple unreviewed slots have piled up. We count
    # weeks between (last_reviewed exclusive, current_slot inclusive). For
    # cosmetic purposes only — the trigger fires once either way.
    slots_overdue: list[str] = [current_slot]
    if last_reviewed:
        # Walk back week-by-week from current_slot until last_reviewed.
        cursor = anchor
        while True:
            cursor = cursor - datetime.timedelta(days=7)
            slot = _iso_week_string(cursor)
            if slot <= last_reviewed:
                break
            slots_overdue.insert(0, slot)
            # Safety bound: stop after 12 weeks to avoid runaway messages.
            if len(slots_overdue) >= 12:
                break

    project_dir = os.path.dirname(get_project_file())
    triggers_dir = os.path.join(project_dir, "triggers")
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, "retro.json")

    # Build the message — singular for one slot, multi for catchup.
    if len(slots_overdue) == 1:
        message = (
            f"今週の振り返りがまだです（{current_slot}）。"
            "/beacon-retro で開始しますか？"
        )
    else:
        weeks_str = ", ".join(slots_overdue)
        message = (
            f"振り返り未完の週が {len(slots_overdue)} 件あります（{weeks_str}）。"
            "/beacon-retro で順に消化できます。"
        )

    # Persist: rewrite the trigger every check so the message reflects the
    # current accumulation. created_at stays as the original first-fire date
    # if the trigger already exists, so users see "this has been pending
    # since YYYY-MM-DD".
    created_at = today.isoformat()
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing.get("created_at"), str):
                created_at = existing["created_at"]
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            # #21 follow-up: legacy retro trigger from pre-fix builds is cp932
            # — skip and let the new write below replace it with UTF-8.
            pass

    trigger_data = {
        "name": "retro",
        "kind": "retro-due",
        "current_slot": current_slot,
        "overdue_slots": slots_overdue,
        "message": message,
        "created_at": created_at,
        "refreshed_at": today.isoformat(),
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _fire_spec_needed_trigger(ms_id: str, ms_title: str) -> None:
    """Promote SPEC creation for a newly-added milestone.

    Writes a trigger file that session-start / dispatch will surface as a
    warning. This is a *soft* promotion: never blocks. The trigger is
    cleared when a SPEC doc is added for the MS (see _spec_exists_for_ms
    in trigger_check).
    """
    if not ms_id:
        return
    # If SPEC already exists for this MS, no need to fire.
    if _spec_exists_for_ms(ms_id):
        return
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, f"spec-needed-{ms_id}.json")
    if os.path.exists(trigger_path):
        return
    import datetime
    trigger_data = {
        "name": f"spec-needed-{ms_id}",
        "kind": "spec-needed",
        "ms_id": ms_id,
        "ms_title": ms_title,
        "message": f"{ms_id} \"{ms_title}\" に SPEC (要求書/判断軌跡) がありません。"
                   f"`/beacon-spec {ms_id}` で作成すると、サブエージェントや retrospection が機能しやすくなります。",
        "created_at": datetime.datetime.now().isoformat(),
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


# release-due trigger thresholds (ms-52 e-958, SPEC 設計方針 2).
# feat-primary + fix-supplementary surfaces both "feature accumulation"
# and "fix accumulation" rhythms; see CORE doc rMlHx9n0LYFJ2kWIQELi.
_RELEASE_DUE_FEAT_THRESHOLD = 3
_RELEASE_DUE_FIX_THRESHOLD = 5


def _release_due_trigger_path() -> str:
    return os.path.join(_get_triggers_dir(), "release-due.json")


def _clear_release_due_trigger_if_exists() -> None:
    path = _release_due_trigger_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _auto_fire_release_due_trigger() -> None:
    """Surface a 'release-due' trigger when feat / fix commits since the
    last v* tag cross the threshold defined in CORE doc rMlHx9n0LYFJ2kWIQELi.

    Trigger lifecycle:
      - If repo has no v* tag yet, skip (no baseline to compare against).
      - If commits since tag fall below threshold (e.g. after squash),
        clear any stale trigger and skip.
      - If a trigger already exists for the same since_tag, keep its
        created_at (so users see "this has been pending since YYYY-MM-DD")
        but refresh counts / next_version_hint / refreshed_at.
      - If a trigger exists but since_tag != current tag (a release happened),
        clear it so the rewrite below uses today's created_at.

    Failures (no git / no version_rules / IOError) degrade silently so
    trigger check stays usable even outside a git repo.
    """
    try:
        import version_rules
    except Exception:
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."

    try:
        current_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not current_tag:
        # No baseline tag → no signal to compute. Don't fire (would be noisy
        # for fresh repos that haven't shipped anything yet).
        return

    rev_range = f"{current_tag}..HEAD"
    try:
        result = subprocess.run(
            ["git", "log", rev_range, "--pretty=format:%H%x00%s%x00%b%x1e"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
    except (FileNotFoundError, OSError):
        return
    if result.returncode != 0:
        return
    log_output = result.stdout

    commits = []
    for entry in log_output.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00")
        if len(parts) >= 2:
            subject = parts[1]
            body = parts[2] if len(parts) > 2 else ""
            full_message = subject + ("\n\n" + body if body else "")
            commits.append({"hash": parts[0][:7], "message": full_message})

    if not commits:
        _clear_release_due_trigger_if_exists()
        return

    try:
        info = version_rules.propose_next_version(
            commits, axis="push", repo_path=repo_root
        )
    except Exception:
        return
    counts = info.get("counts", {})
    feat = int(counts.get("feat", 0) or 0)
    fix = int(counts.get("fix", 0) or 0)

    above_threshold = (
        feat >= _RELEASE_DUE_FEAT_THRESHOLD
        or fix >= _RELEASE_DUE_FIX_THRESHOLD
    )
    if not above_threshold:
        _clear_release_due_trigger_if_exists()
        return

    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = _release_due_trigger_path()

    existing = None
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            existing = None

    import datetime
    now_iso = datetime.datetime.now().isoformat()
    # Preserve created_at only if the existing trigger references the same
    # baseline tag (otherwise a release happened and the next "since" date
    # should reset).
    if existing and existing.get("since_tag") == current_tag:
        created_at = existing.get("created_at", now_iso)
    else:
        created_at = now_iso

    next_version = info.get("next") or ""
    summary_parts = []
    if feat:
        summary_parts.append(f"feat {feat} 件")
    if fix:
        summary_parts.append(f"fix {fix} 件")
    summary = " / ".join(summary_parts) if summary_parts else f"{len(commits)} 件"

    hint_path = "`gh workflow run release.yml -f dry_run=true` または `/beacon-push`"
    message = (
        f"前回 release {current_tag} 以降 {summary} が積み上がっています "
        f"(合計 {len(commits)} commits)。次バージョン候補: {next_version}。"
        f"{hint_path} で release.yml 経路の起動を検討してください "
        f"(CORE doc rMlHx9n0LYFJ2kWIQELi: 5 配信チャネル整合の原則)。"
    )

    trigger_data = {
        "name": "release-due",
        "kind": "release-due",
        "since_tag": current_tag,
        "next_version_hint": next_version,
        "feat_count": feat,
        "fix_count": fix,
        "commit_count": len(commits),
        "message": message,
        "created_at": created_at,
        "refreshed_at": now_iso,
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _count_commits_between_tags(repo_root: str, prev_tag: str, tag: str) -> int:
    """Count commits between `prev_tag..tag`. If prev_tag is empty, count
    commits up to and including `tag`. Returns 0 on any git failure."""
    try:
        if prev_tag:
            rev_range = f"{prev_tag}..{tag}"
        else:
            rev_range = tag
        result = subprocess.run(
            ["git", "log", rev_range, "--oneline"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
        if result.returncode != 0:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])
    except (FileNotFoundError, OSError):
        return 0


def _previous_v_tag(repo_root: str, tag: str) -> str:
    """Return the v* tag immediately preceding `tag`, or '' if none."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=v:refname"],
            capture_output=True, text=True, cwd=repo_root, check=False,
        )
        if result.returncode != 0:
            return ""
        prev = ""
        for line in result.stdout.splitlines():
            t = line.strip()
            if not t:
                continue
            if t == tag:
                return prev
            prev = t
        return prev  # tag not found — return last seen as best effort
    except (FileNotFoundError, OSError):
        return ""


def _auto_fire_release_marker_trigger() -> None:
    """Surface a 'release-<version>' trigger for the latest v* tag if not
    already present locally (ms-52 e-952).

    Rationale: release.yml runs in GitHub Actions and fires
    `beacon trigger fire release-X.Y.Z` against the *ephemeral CI runner*
    filesystem, so the maintainer's local cloud-mode session never sees
    the trigger. Instead we derive it from git state (which IS synced via
    `git pull`): if the latest v* tag has no matching local trigger file,
    create one with the same payload release.py would have written
    (commit count + /discord-post hint).

    Lifecycle: this fires only for the *latest* tag. Older missed releases
    require explicit `beacon trigger fire release-vX.Y.Z "<message>"` (AC 3
    listed this catchup as optional). The trigger file persists until the
    user explicitly clears it (the standard release-marker contract).

    Silent failures: any git / version_rules error -> no-op.
    """
    try:
        import version_rules
    except Exception:
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."

    try:
        latest_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not latest_tag:
        return

    # Use the bare version (without leading 'v') in the trigger name to match
    # the convention scripts/release.py established (release-0.8.0, not
    # release-v0.8.0). Trigger payload keeps the full tag for clarity.
    version_str = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    trigger_name = f"release-{version_str}"

    triggers_dir = _get_triggers_dir()
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        return  # Already fired (locally or via this auto-derivation earlier).

    prev_tag = _previous_v_tag(repo_root, latest_tag)
    commit_count = _count_commits_between_tags(repo_root, prev_tag, latest_tag)

    os.makedirs(triggers_dir, exist_ok=True)
    import datetime
    message = (
        f"beacon {latest_tag} released ({commit_count} commits). "
        f"Use /discord-post to share."
    )
    trigger_data = {
        "name": trigger_name,
        "kind": "release-marker",
        "tag": latest_tag,
        "previous_tag": prev_tag,
        "commit_count": commit_count,
        "message": message,
        "created_at": datetime.datetime.now().isoformat(),
        "source": "auto-from-tag",
    }
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _spec_exists_for_ms(ms_id: str) -> bool:
    """Return True if any spec-scoped document is attached to ms_id.

    ms-84 Phase 2: ``_is_cloud_mode()`` 分岐を Store 経由に統一。 LocalStore /
    StoreApi の list_documents() がどちらも scope / milestone を含む同形
    dict 列を返すため、 CLI 側 (= ここ) は backend を意識せず post-filter
    だけで判定できる。 cloud transport 失敗は StoreApi 側で [] に丸める
    best-effort 契約 (= 既存挙動と等価)。
    """
    if not ms_id:
        return False
    try:
        docs = get_store().list_documents()
    except Exception:
        return False
    for doc in docs:
        if doc.get("scope") == "spec" and doc.get("milestone") == ms_id:
            return True
    return False


def _cleanup_spec_needed_triggers() -> None:
    """Remove spec-needed triggers for milestones that now have SPEC docs,
    or for milestones that no longer exist (cancelled/deleted)."""
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return
    try:
        data = load_project()
    except Exception:
        return
    valid_ms_ids = {ms.get("id") for ms in data.get("milestones", [])
                    if ms.get("status") not in ("cancelled",)}
    for fname in os.listdir(triggers_dir):
        if not fname.startswith("spec-needed-"):
            continue
        if not fname.endswith(".json"):
            continue
        ms_id = fname[len("spec-needed-"):-len(".json")]
        # Cleared if MS gone, or SPEC now exists for it
        if ms_id not in valid_ms_ids or _spec_exists_for_ms(ms_id):
            try:
                os.remove(os.path.join(triggers_dir, fname))
            except OSError:
                pass


def _cleanup_stale_triggers():
    import datetime
    today = datetime.date.today()
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return
    # Clean up stale spec-needed triggers (MS gone or SPEC now exists)
    _cleanup_spec_needed_triggers()
    # NOTE (e-575): Do NOT auto-delete retro.json by age. The retro trigger now
    # persists across days until `beacon retro done` (cmd_retro_done) explicitly
    # removes it. _auto_fire_retro_trigger refreshes the message daily so the
    # "overdue weeks" list stays accurate; deletion solely on age would defeat
    # the persistence requirement of UC5-J5'.
    # Clean up stale operation_check triggers
    for fname in os.listdir(triggers_dir):
        if not fname.startswith("operation_check_") or not fname.endswith(".json"):
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                trigger = json.load(f)
            created = datetime.date.fromisoformat(trigger["created_at"][:10])
            if today > created:
                os.remove(fpath)
        except (json.JSONDecodeError, KeyError, ValueError, IOError):
            pass

    # ms-80 e-1829: release-marker triggers accumulate forever otherwise (e.g.
    # 28 markers from v0.8.0 〜 v0.38.1 observed in session-start). Keep only
    # the *latest* tag's marker (= matches current `version_rules.get_current_tag`),
    # delete older ones. The current marker is preserved for the "did I post
    # to Discord?" reminder (= release-due / release-marker pair).
    _cleanup_old_release_marker_triggers()


def _cleanup_old_release_marker_triggers():
    """Sweep release-marker triggers, keeping only the one matching the
    current git tag. Older markers accumulate on every release fire and
    have no signal value once a newer marker exists.

    Silent failures: any git / version_rules / IO error -> no-op.
    """
    try:
        import version_rules
    except Exception:
        return
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        return

    project_dir = os.path.dirname(get_project_file())
    repo_root = os.path.dirname(project_dir) or "."
    try:
        latest_tag = version_rules.get_current_tag(prefix="v", repo_path=repo_root)
    except Exception:
        return
    if not latest_tag:
        return
    version_str = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
    keep_name = f"release-{version_str}"

    for fname in os.listdir(triggers_dir):
        if not fname.startswith("release-") or not fname.endswith(".json"):
            continue
        # Skip release-due (= not a release-marker, different trigger kind)
        if fname == "release-due.json":
            continue
        # Keep the trigger for the current tag
        if fname == f"{keep_name}.json":
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                trigger = json.load(f)
            if trigger.get("kind") != "release-marker":
                continue
            os.remove(fpath)
        except (json.JSONDecodeError, IOError, OSError, ValueError):
            pass


def _auto_fire_operation_triggers():
    import datetime
    today = datetime.date.today()
    day_abbr = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][today.weekday()]
    today_str = today.isoformat()
    try:
        data = load_project()
    except Exception:
        return
    triggers_dir = _get_triggers_dir()
    for op in data.get("operations", []):
        if op.get("status") != "open":
            continue
        op_id = op["id"]
        days = op.get("schedule", {}).get("days", [])
        if day_abbr not in days:
            continue
        has_today_run = any(
            e.get("type") == "run_record" and e.get("date", "").startswith(today_str)
            for e in op.get("entries", [])
        )
        if has_today_run:
            continue
        trigger_name = f"operation_check_{op_id}"
        trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
        if os.path.exists(trigger_path):
            continue
        log_source = op.get("log_source", op_id)
        # Find linked SPEC doc for this operation
        spec_ref = ""
        spec_doc_id = ""
        try:
            docs_dir = _get_docs_dir()
            if os.path.isdir(docs_dir):
                for fname in sorted(os.listdir(docs_dir)):
                    if not fname.endswith(".md"):
                        continue
                    doc = _read_local_doc(os.path.join(docs_dir, fname))
                    if doc.get("operation") == op_id and doc.get("scope") == "spec":
                        spec_doc_id = doc["doc_id"]
                        spec_ref = f" (doc: {spec_doc_id} 参照)"
                        break
        except Exception:
            pass
        os.makedirs(triggers_dir, exist_ok=True)
        trigger_data = {
            "name": trigger_name,
            "message": f"{op_id} ({log_source}) のバッチ確認が必要です{spec_ref}。/beacon-operation-review で記録してください。",
            "created_at": today_str,
        }
        with open(trigger_path, "w", encoding="utf-8") as f:
            json.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")
        # ms-60 / e-1340: also mirror onto the bus so AI sessions subscribed
        # via the inbox hook can autonomously run `/beacon-operation-execute`.
        # Channel "operation-trigger" + delivery="auto-execute" is the autonomous
        # path; the inbox hook downgrades to propose-to-ai unless the channel
        # is in bus_auto_execute_channels (opt-in).
        _push_operation_trigger_to_bus(op_id, log_source, trigger_data, spec_doc_id)


def cmd_trigger_fire():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    trigger_message = os.environ.get("BEACON_TRIGGER_MESSAGE", "")
    # ms-75 / e-1870: Trek-aware trigger. When a trek-id is supplied
    # (= via `beacon trigger fire --trek <id> <name> [msg]`), the trigger
    # rides a dedicated ``trek-trigger`` channel on the bus with
    # ``delivery=auto-execute`` so opted-in sessions can run
    # ``/beacon-trek-execute`` autonomously — twin of the
    # ``operation-trigger`` path. Without --trek the legacy
    # ``trigger`` channel + ``propose-to-ai`` delivery is unchanged.
    trek_id = os.environ.get("BEACON_TRIGGER_TREK_ID", "").strip()
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        return
    import datetime
    trigger_data = {
        "name": trigger_name,
        "message": trigger_message,
        "created_at": datetime.datetime.now().isoformat(),
    }
    # Persist trek_id on the on-disk trigger so `beacon trigger check`
    # readers (= session-start, dispatch) can detect a trek-scoped
    # trigger without re-querying the bus event.
    if trek_id:
        trigger_data["trek_id"] = trek_id
    with open(trigger_path, "w", encoding="utf-8") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")
    # ms-54 / e-1136: dogfood the bus by also posting the trigger as a bus
    # event. Every cloud-connected session subscribing through the inbox hook
    # will see it as a propose-to-ai event and the AI can decide what to do.
    #
    # The post is best-effort: a network failure, missing creds, or local-mode
    # project must NOT prevent the trigger file from being written. Triggers
    # are the existing single source of truth; the bus event is a propagation
    # layer on top. _push_trigger_to_bus / _push_trek_trigger_to_bus swallow
    # every error path to stderr.
    if trek_id:
        _push_trek_trigger_to_bus(trek_id, trigger_data)
    else:
        _push_trigger_to_bus(trigger_data)


def _push_trigger_to_bus(trigger_data: dict) -> None:
    """Mirror a fired trigger onto the cloud bus as a propose-to-ai event.

    No-ops for local-mode projects (no cloud.json), missing credentials, or
    any cloud failure. The user-facing behavior of `beacon trigger fire`
    is unchanged on the failure path — bus is purely additive.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials  # local import — heavy module
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))
        # Channel "trigger" is the convention for system-fired bus events,
        # distinct from "session-dm" used for agent-to-agent chat. Receivers
        # can filter on it for triage UI, or treat it like any inbox event.
        # sender_session_id is empty to mark a system-originated event so the
        # inbox hook can render "from=system" cleanly.
        client.post_bus_event(
            project_id, "trigger",
            sender_session_id="",
            payload={
                "trigger_name": trigger_data.get("name", ""),
                "message": trigger_data.get("message", ""),
                "created_at": trigger_data.get("created_at", ""),
            },
            delivery="propose-to-ai",
        )
    except Exception as exc:
        # stderr keeps the failure visible in BEACON_HOOK_DEBUG=1 traces
        # without polluting normal CLI output.
        sys.stderr.write(
            f"[beacon] trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


def _resolve_operation_trigger_recipient(op_id: str) -> str:
    """Resolve the unicast recipient_session_id for an operation-trigger event.

    ms-76 / e-1860 / e-1604: operation-trigger default = unicast to a single
    claimer / owner session. Broadcast (= empty recipient → fan out to every
    live session in the project) is the LEGACY behaviour and is retained
    only as fallback when no claimer is registered. The CORE doc
    QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier framework)
    section "構造的禁止" makes default broadcast a禁止帯; this resolver
    is the structural enforcement point.

    Resolution order (first hit wins):
      1. ``meta.claimer_session_id`` on the Operation (= explicit
         claim-based registration, e-1604). A session calls
         ``beacon operation claim <op-id>`` to register itself as the
         sole receiver; subsequent triggers route only here.
      2. ``meta.open_by`` on the Operation (= the session that opened
         the Operation, treated as default owner when no explicit claim).
      3. Empty string (= legacy broadcast). Best-effort fallback for
         pre-ms-76 projects that have no owner/claimer recorded.

    The ``BEACON_OPERATION_TRIGGER_BROADCAST=1`` env flag opts back into
    legacy broadcast explicitly (= for SPECs that legitimately want all
    sessions notified, e.g. a release announcement trigger). This is the
    "explicit opt-in pattern" from ms-76 SPEC EuLwGrAawmMzeKYsxkrd
    設計方針 7.
    """
    # Explicit broadcast opt-in (= rare, only when SPEC declares it).
    if os.environ.get("BEACON_OPERATION_TRIGGER_BROADCAST", "") == "1":
        return ""
    try:
        data = load_project()
    except Exception:
        return ""
    for op in data.get("operations", []):
        if op.get("id") != op_id:
            continue
        meta = op.get("meta", {}) or {}
        claimer = (meta.get("claimer_session_id") or "").strip()
        if claimer:
            return claimer
        owner = (meta.get("open_by") or "").strip()
        if owner:
            return owner
        break
    return ""


def _push_operation_trigger_to_bus(op_id: str, log_source: str,
                                   trigger_data: dict,
                                   spec_doc_id: str = "") -> None:
    """Mirror an operation-check trigger onto the bus (ms-60 / e-1340).

    Distinct from ``_push_trigger_to_bus`` because operation triggers carry
    structured fields the autonomous Skill needs (op_id, spec_doc_id), and
    they ride a dedicated channel so the auto-execute allowlist can opt in
    to operation autonomy without arming every other trigger source.

    Channel ``"operation-trigger"`` + delivery ``"auto-execute"`` means: a
    project that has opted in (``beacon bus auto-execute add --channel
    operation-trigger``) lets the inbox hook run ``/beacon-operation-execute``
    without human review. Without opt-in, the event is downgraded to
    ``propose-to-ai`` and surfaces in the AI inbox for human review.

    The post carries a T2 Operation-scope envelope so the server-side verify
    pipeline (e-1155) preserves the auto-execute delivery. Without it the
    server treats the post as legacy (effective_tier=T5) and degrades
    delivery to notify-user-only, which never injects into AI context —
    silently breaking the autonomous loop (e-1393).

    ms-76 / e-1860 / e-1604: payload now carries ``recipient_session_id``
    by default (= unicast to the registered claimer/owner). Legacy broadcast
    is retained only as fallback when no claimer is registered, or when
    ``BEACON_OPERATION_TRIGGER_BROADCAST=1`` is set for SPECs that
    explicitly opt into broadcast.

    Best-effort — failures don't break the local trigger file write.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))

        # e-1393: mint a T2 Operation-scope envelope so the server's verify
        # pipeline keeps `delivery="auto-execute"` instead of degrading to
        # `notify-user-only`. Without an envelope the post is treated as
        # legacy (effective_tier=T5) and `decide_delivery` caps auto-execute
        # at notify-user-only, which the inbox hook never injects — breaking
        # the autonomous loop silently. T2 is the right tier because the
        # trigger fire is scoped to a single op_id and is initiated by the
        # autofire system (not a human typing → not T1).
        envelope_obj = None
        try:
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T2",
                actions_authorized=["operation.trigger.fire"],
                scope=f"op:{op_id}",
                data_class="free",
            )
        except Exception as env_exc:
            sys.stderr.write(
                f"[beacon] operation-trigger envelope mint failed "
                f"({type(env_exc).__name__}: {env_exc}); posting without "
                "envelope — delivery will be degraded to notify-user-only.\n"
            )

        # ms-76 / e-1860 / e-1604: stamp the unicast recipient. Empty string
        # falls through to legacy broadcast (= fan out to every session)
        # only when no claimer/owner is registered and broadcast was not
        # explicitly opted into. The server's /bus/unread filter
        # (server/app.py _bus_event_addressed_to) honours the field for
        # all channels except dm; for operation-trigger an empty recipient
        # behaves as legacy fan-out so back-compat is preserved.
        recipient_session_id = _resolve_operation_trigger_recipient(op_id)
        payload = {
            "op_id": op_id,
            "log_source": log_source,
            "spec_doc_id": spec_doc_id,
            "trigger_name": trigger_data.get("name", ""),
            "message": trigger_data.get("message", ""),
            "created_at": trigger_data.get("created_at", ""),
        }
        if recipient_session_id:
            payload["recipient_session_id"] = recipient_session_id
        client.post_bus_event(
            project_id, "operation-trigger",
            sender_session_id="",
            payload=payload,
            delivery="auto-execute",
            envelope=envelope_obj,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] operation-trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


def _push_trek_trigger_to_bus(trek_id: str, trigger_data: dict) -> None:
    """Mirror a trek-scoped trigger onto the bus (ms-75 / e-1870).

    Twin of ``_push_operation_trigger_to_bus`` for Trek scope. Posts on the
    ``trek-trigger`` channel with ``delivery=auto-execute`` and a T2 Trek-
    scope envelope. Sessions opted in via
    ``beacon bus auto-execute add --channel trek-trigger`` see the event
    routed by the inbox hook into a structured "TREK ACTION" block that
    launches ``/beacon-trek-execute <trek-id>`` without a confirmation
    prompt. Without opt-in the event is downgraded to ``propose-to-ai``
    (= safe fallback, user reviews before launching).

    Why a dedicated channel:
    - operation-trigger is scoped to one ``op_id`` (= a single periodic
      check). Trek-trigger is scoped to a ``trek_id`` (= an entire
      workspace of MS / task / Operation). Sharing one channel would force
      every receiver to inspect the payload before they can decide whether
      to act.
    - Opt-in is per-channel. A project that auto-executes Operations
      may still want to keep Trek autonomy gated behind manual review
      until it has dogfooded the trek-execute Skill once.

    Best-effort: failures don't break the local trigger file write.
    """
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id")
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        api_url = _resolve_active_api_url()
        client = ApiClient(api_url, _extract_token(creds))

        # Mint a T2 Trek-scope envelope so the server's verify pipeline
        # keeps ``delivery=auto-execute`` instead of degrading it to
        # ``notify-user-only`` (same gate as e-1393 for operations).
        # ``scope=trek:<trek-id>`` mirrors the ``op:<op-id>`` convention
        # used by operation-trigger so the audit log carries the trek id
        # in a uniform shape.
        envelope_obj = None
        try:
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T2",
                actions_authorized=["trek.trigger.fire"],
                scope=f"trek:{trek_id}",
                data_class="free",
            )
        except Exception as env_exc:
            sys.stderr.write(
                f"[beacon] trek-trigger envelope mint failed "
                f"({type(env_exc).__name__}: {env_exc}); posting without "
                "envelope — delivery will be degraded to notify-user-only.\n"
            )

        client.post_bus_event(
            project_id, "trek-trigger",
            sender_session_id="",
            payload={
                "trek_id": trek_id,
                "trigger_name": trigger_data.get("name", ""),
                "message": trigger_data.get("message", ""),
                "created_at": trigger_data.get("created_at", ""),
            },
            delivery="auto-execute",
            envelope=envelope_obj,
        )
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] trek-trigger bus mirror failed silently: "
            f"{type(exc).__name__}: {exc}\n"
        )


def cmd_trigger_check():
    _auto_fire_retro_trigger()
    _auto_fire_operation_triggers()
    _auto_fire_release_due_trigger()
    _auto_fire_release_marker_trigger()
    _cleanup_stale_triggers()
    triggers_dir = _get_triggers_dir()
    if not os.path.isdir(triggers_dir):
        print("[]")
        return
    triggers = []
    for fname in sorted(os.listdir(triggers_dir)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(triggers_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                triggers.append(json.load(f))
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as exc:
            # #21 follow-up: legacy trigger files written by pre-encoding-fix
            # builds on Windows are persisted in cp932 and fail UTF-8 decode.
            # We silently skip so `beacon trigger check` keeps working; the
            # bad file stays on disk until the user clears it explicitly
            # (or the firing code overwrites it with valid UTF-8).
            sys.stderr.write(
                f"[beacon] skipping malformed trigger file "
                f"{os.path.basename(fpath)}: {type(exc).__name__}\n"
            )
    print(json.dumps(triggers, ensure_ascii=False))


def cmd_trigger_clear():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    if not trigger_name:
        print("Error: trigger name required")
        sys.exit(1)
    triggers_dir = _get_triggers_dir()
    trigger_path = os.path.join(triggers_dir, f"{trigger_name}.json")
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
        print(f"Cleared trigger: {trigger_name}")
    else:
        print(f"No trigger: {trigger_name}")


# ---------------------------------------------------------------------------
# Document commands
# ---------------------------------------------------------------------------

VALID_SCOPES = ("core", "spec", "memo", "retro", "report")
DEFAULT_SCOPE = "memo"


def _get_docs_dir():
    project_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(project_dir, "documents")


def _doc_slug(title):
    """Generate a file-safe slug from a document title."""
    slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug or "untitled"


def _is_cloud_mode():
    """Check if we're in cloud mode (single source of truth: cloud.json existence).

    e-1861 (ms-61): The legacy ``config.json["mode"] == "cloud"`` dual-source
    check was removed because it created a silent drift window: a sub-agent
    could overwrite ``.beacon/config.json`` to ``{"mode": "local"}`` and the
    CLI would suddenly read the stale local ``project.json`` instead of cloud,
    causing apparent user data loss (2026-06-15 incident).

    Beacon is always invoked from Claude Code, which requires internet, so
    "local mode" has no production use case. ``.beacon/cloud.json`` existence
    is now the single, structurally protected source of truth. ``BEACON_CLOUD=1``
    still forces cloud for test harnesses that mock ``cloud.json`` indirectly.

    Any ``mode`` field still sitting in legacy ``config.json`` is ignored
    (graceful — we never error, we just stop reading it). ``beacon doctor``
    surfaces the legacy field as a non-fatal migration warning.
    """
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    cloud_path = os.path.join(beacon_dir, "cloud.json")
    return os.path.exists(cloud_path)


def _resolve_content_input(content: str) -> str:
    """Resolve a ``--content`` argument, treating ``"-"`` as stdin.

    PE dogfood 2026-06-10: ``beacon doc update --content -`` was interpreted
    literally and replaced a 130-line SPEC with the single character ``-``.
    kubectl / curl convention treats ``-`` as stdin; we follow the same rule
    and hard-reject the dangerous case where stdin is a tty.
    """
    if content == "-":
        if sys.stdin.isatty():
            print("Error: --content - は stdin からの読み込みを意味します", file=sys.stderr)
            print("       pipe で渡してください: cat file.md | beacon doc update <id>", file=sys.stderr)
            print("       または --content フラグを省略して stdin から渡してください", file=sys.stderr)
            sys.exit(1)
        return sys.stdin.read()
    if not content and not sys.stdin.isatty():
        return sys.stdin.read()
    return content


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text.

    Returns ``(metadata_dict, body_text)``. Block-list values (lines starting
    with ``- `` immediately after a key with empty value) are returned as
    ``list[str]``; everything else is ``str``. PE dogfood 2026-06-10:
    block-list ``approved_actions`` of the form ``- "op:op-2:check_run"`` were
    silently destroyed by the previous line-based parser which split each
    ``- "op:...`` row on its first colon.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta = {}
    lines = header.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            # Orphan list item without a preceding key — ignore.
            continue
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val:
            # Inline list form: ``key: [a, b]`` is parsed to ``list[str]`` so
            # that block- and inline-list values round-trip identically.
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if not inner:
                    meta[key] = []
                else:
                    meta[key] = [
                        s.strip().strip('"').strip("'")
                        for s in inner.split(",")
                        if s.strip()
                    ]
            else:
                meta[key] = val
            continue
        # Empty inline value → look ahead for a block-list.
        items: list[str] = []
        while i < len(lines):
            next_stripped = lines[i].strip()
            if not next_stripped:
                i += 1
                continue
            if next_stripped.startswith("- "):
                items.append(next_stripped[2:].strip().strip('"').strip("'"))
                i += 1
                continue
            break
        meta[key] = items if items else ""
    return meta, body


def _add_frontmatter(content, scope, milestone="", operation="", trek_id="",
                     drop_milestone=False, drop_operation=False):
    """Prepend frontmatter to content, or update existing scope/milestone/operation/trek_id.

    List values are written as inline YAML arrays (``key: ["a", "b"]``) so
    they survive the line-based parser on the next round-trip — block-list
    items containing colons (e.g. ``op:op-2:check_run``) cannot be expressed
    safely in the line-based format and must be normalised.

    ``trek_id`` (ms-69 / e-1663) associates a doc with a cross-project trek;
    optional, defaults preserved on round-trip.

    ``drop_milestone`` / ``drop_operation`` (e-1859) explicitly remove the
    matching key from existing frontmatter. ``cmd_doc_update`` sets these
    when the user switches a doc from milestone scope to operation scope
    (or vice versa) so the rejected field doesn't linger and produce
    two-headed (= both milestone and operation set) frontmatter that
    silently misleads ``/beacon-operation-review`` discovery filters.
    """
    meta, body = _parse_frontmatter(content)
    meta["scope"] = scope
    if drop_milestone:
        meta.pop("milestone", None)
    elif milestone:
        meta["milestone"] = milestone
    if drop_operation:
        meta.pop("operation", None)
    elif operation:
        meta["operation"] = operation
    if trek_id:
        meta["trek_id"] = trek_id
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            quoted = ", ".join(f'"{item}"' for item in v)
            lines.append(f"{k}: [{quoted}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body


def _read_local_doc(fpath):
    """Read a local document file and return parsed metadata."""
    import datetime
    fname = os.path.basename(fpath)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, body = _parse_frontmatter(content)
    scope = meta.get("scope", DEFAULT_SCOPE)
    milestone = meta.get("milestone", "")
    # Find title from first heading in body
    first_line = ""
    for line in body.split("\n"):
        line = line.strip()
        if line:
            first_line = line
            break
    title = first_line.lstrip("# ") if first_line.startswith("#") else fname[:-3]
    stat = os.stat(fpath)
    updated = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
    operation = meta.get("operation", "")
    trek_id = meta.get("trek_id", "")  # ms-69 / e-1663
    result = {
        "doc_id": fname[:-3],
        "title": title,
        "scope": scope,
        "content": content,
        "updated_at": updated,
    }
    if milestone:
        result["milestone"] = milestone
    if operation:
        result["operation"] = operation
    if trek_id:
        result["trek_id"] = trek_id
    # Soft-delete fields surface so cmd_doc_list can filter without
    # re-parsing the frontmatter (ms-14 e-973).
    if meta.get("status"):
        result["status"] = meta["status"]
    for k in ("trashed_at", "trashed_by", "trash_reason"):
        if meta.get(k):
            result[k] = meta[k]
    return result


def cmd_doc_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    ms_filter = os.environ.get("BEACON_MS", "")
    op_filter = os.environ.get("BEACON_OP", "")
    # ms-75 / e-1866: ``--trek <trek-id>`` filter — surfaces all docs
    # whose frontmatter is tagged with the given trek_id. The trek_id
    # frontmatter field has existed since ms-69 / e-1663; this filter
    # makes it usable from the CLI (= mirrors what the server-side
    # /api/treks/{tid}/documents lookup does for the Web UI).
    trek_filter = os.environ.get("BEACON_TREK_ID", "")
    # Trashed docs are hidden by default — pass --include-trashed to see
    # them in line with active ones (ms-14 e-973).
    include_trashed = os.environ.get("BEACON_INCLUDE_TRASHED", "") == "1"

    # ms-84 Phase 2: Store 経由で local / cloud を統一。 LocalStore.list_documents
    # は frontmatter 解析済の同形 dict 列を返すため、 ここでは soft-delete filter
    # と post-filter (scope / ms / op / trek) を一括で適用するだけ。
    docs = get_store().list_documents()
    if not include_trashed:
        docs = [d for d in docs if d.get("status") != "cancelled"]

    if scope_filter:
        docs = [d for d in docs if d.get("scope") == scope_filter]
    if ms_filter:
        docs = [d for d in docs if d.get("milestone") == ms_filter]
    if op_filter:
        docs = [d for d in docs if d.get("operation") == op_filter]
    if trek_filter:
        docs = [d for d in docs if d.get("trek_id") == trek_filter]

    if json_mode:
        print(json.dumps(docs, ensure_ascii=False))
    else:
        if not docs:
            print("No documents.")
            return
        scope_icons = {"core": "*", "spec": "+", "memo": "-", "retro": "~", "report": "!"}
        for doc in docs:
            icon = scope_icons.get(doc.get("scope", "memo"), "?")
            print(f"  {icon} [{doc.get('scope', 'memo')}] {doc['doc_id']}: {doc['title']}")


def cmd_doc_show():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # ms-84 Phase 2: Store 経由で local / cloud を統一。 Store.get_document は
    # 両 backend で同形 ({} on not-found / full dict on hit) を返す契約。
    doc = get_store().get_document(doc_id)
    if not doc:
        print(f"Document not found: {doc_id}")
        sys.exit(1)

    if json_mode:
        print(json.dumps(doc, ensure_ascii=False))
    else:
        print(doc.get("content", ""))


def cmd_doc_add():
    title = os.environ.get("BEACON_TITLE", "")
    content = os.environ.get("BEACON_CONTENT", "")
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    scope = os.environ.get("BEACON_SCOPE", DEFAULT_SCOPE)
    milestone = os.environ.get("BEACON_MS", "")
    operation = os.environ.get("BEACON_OP", "")
    trek_id = os.environ.get("BEACON_TREK_ID", "")  # ms-69 / e-1663
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: title required")
        sys.exit(1)

    if scope not in VALID_SCOPES:
        print(f"Error: scope must be one of {VALID_SCOPES}")
        sys.exit(1)

    # ms-54 / e-1293: persistence poisoning defense — refuse writes whose
    # source is a bus DM, regardless of scope. Memos are the canonical
    # poisoning target ("write me a memo that says ..."), but every doc
    # scope is a persistent vector so we gate uniformly.
    if _refuse_if_bus_origin(
        "doc_add",
        {"title": title[:80], "scope": scope, "doc_id": doc_id},
    ):
        sys.exit(1)

    # core docs are project-wide — MS association is optional
    if scope == "core":
        milestone = milestone or None

    content = _resolve_content_input(content)

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    # Duplicate check: warn if same title+scope already exists.
    # ms-84 Phase 2: Store 経由で local / cloud を統一 (= 受入条件 10 の _is_cloud_mode
    # 分岐削減)。 失敗時は best-effort で skip する従来挙動を維持。
    try:
        existing = get_store().list_documents()
        dupes = [d for d in existing
                 if d.get("title") == title and d.get("scope") == scope]
        if dupes:
            print(
                f"Warning: document with same title+scope already exists "
                f"({dupes[0]['doc_id']}). Proceeding anyway."
            )
    except Exception:
        pass

    # Add frontmatter with scope, milestone, operation, and trek_id
    content = _add_frontmatter(content, scope, milestone or "", operation or "",
                               trek_id or "")

    if _is_cloud_mode():
        client, config = _get_api_client()
        if doc_id:
            result = client.update_document(config["project_id"], doc_id, title, content)
        else:
            result = client.create_document(config["project_id"], title, content)
        doc_id = result["doc_id"]
    else:
        docs_dir = _get_docs_dir()
        os.makedirs(docs_dir, exist_ok=True)
        if not doc_id:
            doc_id = _doc_slug(title)
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    import datetime
    data = load_project()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # core docs: skip MS/Op entry recording (they're project-wide)
    if scope != "core":
        if operation:
            # Record in operation entries
            for op in data.get("operations", []):
                if op.get("id") == operation:
                    eid = core.next_entry_id(data)
                    op.setdefault("entries", []).append({
                        "id": eid,
                        "type": "save",
                        "description": f"doc add: {title} ({scope})",
                        "status": "done",
                        "created_at": today,
                        "done_at": today,
                        "meta": {"revision_id": doc_id, "source": "auto"},
                    })
                    break
        else:
            core.save_entry(data, ms_id=milestone, description=f"doc add: {title} ({scope})",
                            source="auto", date=today, revision_id=doc_id,
                            url=None, hash=None, progress=None)
        save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Saved: {doc_id} [{scope}] ({title})")


def cmd_doc_update():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    content = os.environ.get("BEACON_CONTENT", "")
    title = os.environ.get("BEACON_TITLE", "")
    scope = os.environ.get("BEACON_SCOPE", "")
    milestone = os.environ.get("BEACON_MS", "")
    trek_id = os.environ.get("BEACON_TREK_ID", "")  # ms-69 / e-1663
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # e-1859: the bin/beacon wrapper sets BEACON_{MS,OP}_SET=1 whenever the
    # user typed --ms / --op (even with an empty value). This lets us treat
    # `--ms ms-1` and "did not pass --ms" differently — without it, op-scoped
    # docs silently keep their `operation:` frontmatter while gaining a
    # `milestone:` field, producing two-headed scope rows that the
    # /beacon-operation-review discovery filter can't reason about.
    ms_explicit = os.environ.get("BEACON_MS_SET", "") == "1"
    op_explicit = os.environ.get("BEACON_OP_SET", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # ms-54 / e-1293: persistence poisoning defense. Updating a doc with
    # bus-derived content is the same poisoning vector as creating one.
    if _refuse_if_bus_origin(
        "doc_update",
        {"doc_id": doc_id, "title": title[:80], "scope": scope},
    ):
        sys.exit(1)

    content = _resolve_content_input(content)

    # Fetch existing document to merge fields.
    # ms-84 Phase 2: Store 経由で local / cloud を統一。 Store.get_document は
    # 両 backend で同形 ({} on not-found / full dict on hit) を返す契約。
    existing = get_store().get_document(doc_id)
    if not existing:
        print(f"Document not found: {doc_id}")
        sys.exit(1)

    operation = os.environ.get("BEACON_OP", "")
    # Use existing values as defaults
    if not title:
        title = existing.get("title", "")
    if not scope:
        scope = existing.get("scope", DEFAULT_SCOPE)

    # e-1859: scope (= milestone vs operation binding) is treated as mutually
    # exclusive. The three input modes:
    #   1. User passed --ms <id>  → switch to milestone scope; drop operation.
    #   2. User passed --op <id>  → switch to operation scope; drop milestone.
    #   3. User passed neither    → preserve whichever the doc already had.
    # If a user passes BOTH --ms and --op in one call, that is a programmer
    # error; we honor both literally (= same behavior as before this fix) and
    # leave the duplicated frontmatter visible so the mistake is loud, not
    # silent.
    if ms_explicit and not op_explicit:
        # Mode 1: user wants this doc on a milestone. Drop any prior op.
        operation = ""
    elif op_explicit and not ms_explicit:
        # Mode 2: user wants this doc on an operation. Drop any prior ms.
        milestone = ""
    else:
        # Mode 3 (neither flag) or both flags: preserve whatever wasn't passed.
        if not milestone:
            milestone = existing.get("milestone", "")
        if not operation:
            operation = existing.get("operation", "")

    if not trek_id:
        trek_id = existing.get("trek_id", "")
    if not content:
        content = existing.get("content", "")

    # Rebuild with frontmatter. e-1859: _add_frontmatter is called with an
    # explicit "scope wipe" pass so the field we are dropping (= operation
    # under Mode 1, milestone under Mode 2) is removed from the existing
    # frontmatter dict instead of being left behind alongside the new field.
    content = _add_frontmatter(
        content, scope, milestone, operation, trek_id,
        drop_milestone=(op_explicit and not ms_explicit),
        drop_operation=(ms_explicit and not op_explicit),
    )

    # Write path still branches per backend (Phase 3 で Store.save_document
    # 化予定)。 read だけ Phase 2 で Store 経由化したので、 ここで client /
    # docs_dir を遅延 resolve する。
    if _is_cloud_mode():
        client, config = _get_api_client()
        client.update_document(config["project_id"], doc_id, title, content)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    import datetime
    data = load_project()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # e-1859: mirror cmd_doc_add's scope-aware entry recording so an
    # op-scoped doc update lands in op.entries (not the milestone log).
    # core scope is project-wide and skips entry recording entirely.
    if scope == "core":
        pass
    elif operation:
        for op in data.get("operations", []):
            if op.get("id") == operation:
                eid = core.next_entry_id(data)
                op.setdefault("entries", []).append({
                    "id": eid,
                    "type": "save",
                    "description": f"doc update: {title} ({scope})",
                    "status": "done",
                    "created_at": today,
                    "done_at": today,
                    "meta": {"revision_id": doc_id, "source": "auto"},
                })
                break
    else:
        core.save_entry(data, ms_id=milestone,
                        description=f"doc update: {title} ({scope})",
                        source="auto", date=today, revision_id=doc_id,
                        url=None, hash=None, progress=None)
    save_project(data)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "title": title, "scope": scope}, ensure_ascii=False))
    else:
        print(f"Updated: {doc_id} [{scope}] ({title})")


def cmd_doc_history():
    """Show revision history of a document."""
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    if not doc_id:
        print("Error: doc ID required")
        sys.exit(1)
    client, config = _get_api_client()
    project_id = config["project_id"]
    try:
        revs = client.get(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}/revisions")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    if not revs:
        print(f"No revisions found for '{doc_id}'")
        return
    for r in revs:
        print(f"  rev-{r['rev']}  {r['ts'][:10]}  {r.get('saved_by', '?')}")


def _rewrite_doc_frontmatter(fpath: str, *,
                              updates: Optional[dict] = None,
                              removes: Optional[list] = None) -> None:
    """Rewrite a doc's frontmatter in place.

    Reads the on-disk content, parses frontmatter via the existing
    parser, applies ``updates`` (key → str value) and ``removes`` (list
    of keys to drop), and writes back. Body is preserved exactly. Used
    by the doc soft-delete path (ms-14 e-973) so we don't bypass the
    canonical frontmatter shape.
    """
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, body = _parse_frontmatter(content)
    if removes:
        for k in removes:
            meta.pop(k, None)
    if updates:
        meta.update({k: str(v) for k, v in updates.items()})
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            quoted = ", ".join(f'"{item}"' for item in v)
            lines.append(f"{k}: [{quoted}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    new_content = "\n".join(lines) + body
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(new_content)


def cmd_doc_restore():
    """Restore a document to a historical revision.

    Requires ``--rev N`` (BEACON_REV). Trash-restore was removed; cancelled
    docs are reactivated via ``beacon doc update`` or equivalent edits.
    """
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    rev = os.environ.get("BEACON_REV", "")
    if not doc_id:
        print("Error: doc_id required", file=sys.stderr)
        sys.exit(1)
    if not rev:
        print("Usage: beacon doc restore <doc-id> --rev <N>", file=sys.stderr)
        sys.exit(1)
    _doc_restore_revision(doc_id, rev)


def _doc_restore_revision(doc_id: str, rev: str) -> None:
    """Restore a doc to a historical revision. Pre-e-973 behaviour."""
    client, config = _get_api_client()
    project_id = config["project_id"]
    try:
        rev_data = client.get(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}/revisions/{rev}")
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    try:
        client.put_document(project_id, doc_id, rev_data["title"], rev_data["content"])
    except RuntimeError as e:
        print(f"Error restoring: {e}")
        sys.exit(1)
    print(f"Restored '{doc_id}' to rev-{rev}")


def cmd_doc_delete():
    """Soft-delete a document.

    Both modes set ``status: cancelled`` and stamp ``trashed_at`` /
    ``trashed_by`` / optional ``trash_reason``. Local rewrites the file
    frontmatter (ms-14 e-973); cloud calls the server's soft-delete
    endpoint and stores the same fields on the Firestore document
    (ms-14 e-991). Restore is a status flip in both modes — no version
    control undelete required.
    """
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.delete_document(config["project_id"], doc_id, reason=reason)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
        if json_mode:
            print(json.dumps(
                {"doc_id": doc_id, "status": "cancelled"},
                ensure_ascii=False,
            ))
        else:
            print(f"Trashed: {doc_id}")
            if reason:
                print(f"  Reason: {reason}")
        return

    docs_dir = _get_docs_dir()
    fpath = os.path.join(docs_dir, f"{doc_id}.md")
    if not os.path.exists(fpath):
        print(f"Document not found: {doc_id}")
        sys.exit(1)

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, _ = _parse_frontmatter(content)
    if meta.get("status") == "cancelled":
        print(f"Document {doc_id} is already in trash.")
        sys.exit(1)

    import core as _core
    now_iso = _core._now_iso()
    actor = _core._get_actor()
    updates = {"status": "cancelled", "trashed_at": now_iso, "trashed_by": actor}
    if reason:
        updates["trash_reason"] = reason
    # Drop any restored_* meta from a prior trash → restore cycle so the
    # only audit fields present reflect the current trash event.
    _rewrite_doc_frontmatter(
        fpath,
        updates=updates,
        removes=["restored_at", "restored_by", "restore_reason"],
    )
    _append_changelog({"op": "doc_delete", "doc_id": doc_id, "reason": reason})

    if json_mode:
        print(json.dumps(
            {"doc_id": doc_id, "status": "cancelled", "trashed_at": now_iso},
            ensure_ascii=False,
        ))
    else:
        print(f"Trashed: {doc_id}")
        if reason:
            print(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# Cloud commands
# ---------------------------------------------------------------------------

DEFAULT_API_URL = "https://beacon-ai.dev"


def _resolve_active_api_url() -> str:
    """Return the api_url of the active profile (ms-64 / e-1458).

    Replaces the previous ``os.environ.get("BEACON_API_URL", DEFAULT_API_URL)``
    + ``config.get("api_url", DEFAULT_API_URL)`` chain that was duplicated at
    11+ sites in this module. The profile resolver already implements the full
    precedence chain (env > cwd cloud.json > profile.json > default), so all
    sites now go through it and a single point of truth handles the precedence
    rules.

    The active profile is determined by (in order): ``--profile`` CLI arg
    (exported as ``BEACON_PROFILE`` by ``bin/beacon`` top-level), then
    ``BEACON_PROFILE`` env, then cwd ``.beacon/cloud.json`` ``profile`` field,
    then the ``default`` profile.
    """
    try:
        import profile as _profile  # type: ignore[import-not-found]
        return _profile.resolve_active_profile().api_url
    except Exception:
        # Best-effort fallback: legacy chain. Keeps CLI usable if profile.py
        # itself is unimportable for any reason (e.g. partial install).
        api_url = _resolve_active_api_url()
        config_path = _get_cloud_config_path()
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    api_url = json.load(f).get("api_url", api_url)
            except Exception:
                pass
        return api_url


def _get_cloud_config_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "cloud.json")


def _ensure_cloud_config():
    config_path = _get_cloud_config_path()
    existing: dict = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
        # Fast path: fully materialized cloud.json (= post-first-push state).
        if existing.get("project_id"):
            return existing
        # Partial cloud.json (e.g. init wrote {"profile": <name>} only) falls
        # through to the materialization step below, preserving existing fields.

    data = load_project()
    name = data.get("name", "project")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    import hashlib
    h = hashlib.md5(os.path.abspath(get_project_file()).encode()).hexdigest()[:6]
    project_id = f"{slug}-{h}"

    # ms-64 e-1633: if profile wasn't pinned at init time (= first push without
    # a prior `beacon init` having materialized a profile field), prompt once
    # when the user has multi-account auth state. Skip silently if the choice
    # was already made (existing.profile), explicitly set (env/--profile), or
    # the env can't take a prompt (non-TTY).
    profile_name = existing.get("profile")
    if not profile_name:
        chosen = _maybe_prompt_initial_profile()
        if chosen:
            profile_name = chosen
            # Persist immediately so _resolve_active_api_url below sees it
            # via the cwd cloud.json.profile precedence rule (lib/profile._resolve_api_url).
            _persist_initial_profile_choice(chosen)
        else:
            try:
                import profile as _profile  # type: ignore[import-not-found]
                profile_name = _profile.resolve_active_profile().name
            except Exception:
                profile_name = "default"

    api_url = _resolve_active_api_url()
    config = {
        "project_id": project_id,
        "api_url": api_url,
        "profile": profile_name,
    }
    # Preserve any unknown fields a partial cloud.json may have already carried.
    for k, v in existing.items():
        config.setdefault(k, v)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {config_path} (project_id: {project_id}, profile: {profile_name})")
    return config



def _extract_token(creds) -> str:
    """Extract bearer token from credentials (handles both object and dict forms)."""
    if isinstance(creds, dict):
        return creds.get("token", "") or creds.get("id_token", "")
    return (creds.id_token or creds.token) if creds else ""

def _get_api_client():
    """Create an ApiClient from cloud.json config and auth credentials.

    The ApiClient receives a TokenProvider callable instead of a static token,
    so tokens are refreshed on every API call. This prevents long-lived CLI
    sessions from failing after the initial token expires.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("No cloud.json found. Run 'beacon cloud push' first.")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    api_url = _resolve_active_api_url()

    # Use a TokenProvider callable so each request picks up a fresh token.
    # load_credentials() refreshes OAuth tokens automatically; web_auth tokens
    # will be refreshed once _refresh_web_auth_token() support is wired in.
    def _token_provider() -> str:
        from auth import load_credentials as _load
        _creds = _load()
        return _extract_token(_creds) if _creds else ""

    from api_client import ApiClient
    return ApiClient(api_url, _token_provider), config


def cmd_doc_image_upload():
    """ms-43: SPEC / memo / retro 本文に貼る画像を 1 枚アップロードする。

    ローカルファイルパスを受け取って Beacon API にアップロードし、本文に
    貼り付け可能な markdown img tag (= ``![filename](url)``) を stdout に
    返す。AI / 人間ともに ``beacon doc image-upload <path>`` を叩いて URL
    を得てから ``beacon doc update <doc-id>`` で本文に取り込む使い方を
    想定。

    クラウドモード必須 (= 画像 hosting に GCS bucket を使う、ローカル
    モードでは UI render 経路が無い)。AWS S3 対応は別 task で拡張する。
    """
    local_path = os.environ.get("BEACON_DOC_IMAGE_PATH", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not local_path:
        print("Error: image file path required (set BEACON_DOC_IMAGE_PATH or pass path as argument)")
        sys.exit(1)
    if not os.path.exists(local_path):
        print(f"Error: file not found: {local_path}")
        sys.exit(1)

    # ms-84 Phase 2: 直接 _is_cloud_mode 呼び出しを Store 経由に統一 (= 受入条件
    # 10 の direct-call 削減)。 image upload は cloud-only operation のため、
    # ここでは mode guard として is_cloud() を確認するだけ。
    if not get_store().is_cloud():
        print("Error: image upload requires cloud mode (run 'beacon cloud push' first)")
        sys.exit(1)

    client, config = _get_api_client()
    try:
        result = client.upload_document_image(config["project_id"], local_path)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except ConnectionError as e:
        print(f"Network error: {e}")
        sys.exit(1)

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result.get("markdown", ""))
        # ファイル sizeと URL も補助情報として stderr に出す (= stdout を
        # markdown 単独に保つことで、`beacon doc image-upload x.png` の出力を
        # そのまま doc 本文へ append できる)。
        size_kb = result.get("size", 0) / 1024
        sys.stderr.write(
            f"Uploaded: {result.get('content_type', '?')}, "
            f"{size_kb:.1f} KiB\n"
            f"URL: {result.get('url', '')}\n"
        )


def cmd_cloud_list():
    """List cloud projects via API."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    # cloud list may be called before cloud.json exists; the profile resolver
    # already honors the env > cwd cloud.json > profile.json > default chain,
    # so we just use it directly here (e-1458).
    api_url = _resolve_active_api_url()

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    try:
        projects = client.list_projects()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if json_mode:
        print(json.dumps(projects, ensure_ascii=False))
    else:
        if not projects:
            print("No cloud projects found.")
            print("Run 'beacon cloud push' to upload a project.")
            return
        for i, p in enumerate(projects, 1):
            print(f"  {i}. {p['project_id']}: {p['name']}")
            if p.get('objective'):
                print(f"     {p['objective'][:60]}")


def cmd_cloud_push():
    force = os.environ.get("BEACON_FORCE", "") == "1"

    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    config = _ensure_cloud_config()
    project_id = config["project_id"]
    api_url = _resolve_active_api_url()

    # In cloud mode, CLI writes go directly to cloud — local project.json is stale.
    # Pushing it would overwrite cloud state and cause data loss.
    if _is_cloud_mode():
        if not force:
            print("Error: already in cloud mode.")
            print("")
            print("  In cloud mode, all CLI changes go directly to the cloud.")
            print("  Pushing the local project.json (which may be stale) would")
            print("  overwrite cloud state and cause data loss.")
            print("")
            print("  To sync cloud state to local:  beacon cloud pull")
            print("  To force-push local state:     beacon cloud push --force")
            sys.exit(1)
        print("Warning: --force specified. Overwriting cloud project data with local file.")
        print("  documents and retros will NOT be pushed (they are managed in cloud).")

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    data = local.load_project()
    core.validate_project(data)

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    # Preserve cloud-only fields (deployments, releases) that are written directly
    # to Firestore and never synced back to local project.json.
    try:
        remote = client.get_project(project_id)
        for field in ("deployments", "releases"):
            if remote.get(field):
                data.setdefault(field, remote[field])
    except RuntimeError:
        pass  # new project or unreachable — proceed with local data only

    try:
        client.put_project(project_id, data)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    if force:
        _append_changelog({"op": "cloud_push_force", "project_id": project_id, "warning": "stale_local"})
    print(f"Pushed to cloud: projects/{project_id}")

    # Push local documents and retros only on the initial push (local → cloud).
    # In cloud mode they are managed via the API; pushing local files would
    # silently overwrite any edits made through the Web UI or CLI.
    if not _is_cloud_mode():
        docs_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "documents")
        if os.path.isdir(docs_dir):
            import glob
            md_files = glob.glob(os.path.join(docs_dir, "*.md"))
            for fpath in md_files:
                doc_info = _read_local_doc(fpath)
                try:
                    client.put_document(
                        project_id, doc_info["doc_id"],
                        doc_info["title"], doc_info["content"],
                        doc_info.get("scope"),
                    )
                    print(f"  doc: {doc_info['doc_id']} ({doc_info.get('scope', 'memo')})")
                except RuntimeError as e:
                    print(f"  doc error [{doc_info['doc_id']}]: {e}")

        retros_dir = os.path.join(os.path.dirname(get_project_file()) or ".beacon", "retro")
        if os.path.isdir(retros_dir):
            import glob
            retro_files = glob.glob(os.path.join(retros_dir, "*.md"))
            for fpath in retro_files:
                week = os.path.basename(fpath)[:-3]  # e.g. "2026-W19"
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                try:
                    client.save_retro(project_id, week, content)
                    print(f"  retro: {week}")
                except RuntimeError as e:
                    print(f"  retro error [{week}]: {e}")

    # e-1861 (ms-61): cloud.json existence already marks us as cloud-mode
    # (written above during the push setup), so the legacy config.json
    # ``{"mode": "cloud"}`` write was retired. Single source of truth = cloud.json.
    print("Switched to cloud mode.")


def cmd_cloud_pull():
    client, config = _get_api_client()
    project_id = config["project_id"]

    try:
        data = client.get_project(project_id)
    except RuntimeError as e:
        if "404" in str(e):
            print(f"Project '{project_id}' not found in cloud.")
            print("Run 'beacon cloud push' to upload first.")
        else:
            print(f"Error: {e}")
        sys.exit(1)

    core.validate_project(data)

    from store_local import LocalStore
    local = LocalStore(get_project_file())
    local.save_project(data)
    print(f"Pulled from cloud: projects/{project_id}")


def cmd_cloud_status():
    config_path = _get_cloud_config_path()
    if not os.path.exists(config_path):
        print("Cloud: not configured")
        print("Run 'beacon cloud push' to set up.")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    from auth import load_credentials
    creds = load_credentials()
    logged_in = creds is not None

    print(f"Cloud: {config['project_id']}")
    print(f"API: {_resolve_active_api_url()}")
    print(f"Auth: {'logged in' if logged_in else 'not logged in'}")


# ---------------------------------------------------------------------------
# PR commands (ms-15)
# ---------------------------------------------------------------------------

def _fetch_gh_pr_info(url: str) -> dict:
    """Fetch PR title, body, and commits from GitHub via gh CLI. Returns {} on failure."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", url, "--json", "title,body,commits"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return {}


def cmd_pr_add():
    import datetime
    url = os.environ.get("BEACON_URL", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    author = os.environ.get("BEACON_AUTHOR", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    date = os.environ.get("BEACON_DATE", "") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not url:
        print("Error: GitHub URL required", file=sys.stderr)
        sys.exit(1)

    # Fetch PR title, body, and commits from GitHub (before intent prompt so body can prefill)
    gh_info = _fetch_gh_pr_info(url)
    title = gh_info.get("title", "")
    pr_body = gh_info.get("body", "") or ""
    commits = gh_info.get("commits", [])
    if gh_info and not title:
        print("Warning: could not fetch PR title from GitHub", file=sys.stderr)

    if not intent:
        try:
            if pr_body.strip():
                # Show PR body as prefill hint so user can accept or edit
                print(f"PR body (prefill):\n  {pr_body.strip()[:300]}")
                prefill_hint = f" [{pr_body.strip()[:120]}]" if len(pr_body.strip()) <= 120 else ""
                raw = input(f"Intent (why was this PR created?){prefill_hint}: ").strip()
                intent = raw if raw else pr_body.strip()
            else:
                intent = input("Intent (why was this PR created?): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = pr_body.strip() if pr_body.strip() else ""

    data = load_project()
    # ms-81 e-1916: status gate. Surface the warning before adding a PR
    # entry to a non-write-authorised MS so the operator can re-target
    # rather than discover the issue later in retro.
    try:
        target_ms = core.find_target_milestone(data, ms_id)
    except ValueError:
        target_ms = None
    if target_ms is not None:
        if not _check_ms_status_for_write(
            target_ms, f"pr add {url}"
        ):
            sys.exit(1)
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=url, author=author,
                          intent=intent, date=date, title=title, commits=commits,
                          session_id=_resolve_session_id())  # ms-57 / e-1062
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    # ms-80 e-1821: 同一 MS に並列で open PR が他にもあれば claim 競合の可能性
    # を author に知らせる (= 警告のみ、block しない)。
    conflicts = _detect_pr_claim_conflict(data, ms_id, eid)

    if json_mode:
        out = {"entry_id": eid, "url": url, "title": title, "intent": intent,
               "commits": len(commits)}
        if conflicts:
            out["claim_conflicts"] = conflicts
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(f"Added PR [{eid}]: {title or url}")
        if commits:
            print(f"  Commits: {len(commits)} linked")
        if intent:
            print(f"  Intent: {intent}")
        if conflicts:
            print(f"")
            print(f"⚠ 同じ {ms_id} に並列で open な PR が {len(conflicts)} 件あります (= claim 競合の可能性):")
            for c in conflicts[:5]:
                author_str = f" by {c['author']}" if c.get("author") else ""
                intent_str = f" — {c['intent'][:60]}" if c.get("intent") else ""
                print(f"  [{c['eid']}] {c['title'] or c['url']}{author_str}{intent_str}")
            if len(conflicts) > 5:
                print(f"  (... and {len(conflicts) - 5} more)")
            print(f"  推奨: `beacon claim` で作業範囲を調整、または各 PR の intent を見直して重複を解消してください。")


def _detect_pr_claim_conflict(data: dict, ms_id: str, new_eid: str) -> list:
    """Return open PR entries under ms_id that may conflict with the newly
    added PR (= new_eid). Excludes the new entry itself.

    A "conflict" here means another PR is also in_review against the same MS.
    Author and reviewer should be aware to either coordinate via beacon claim
    (= ms-55 claim primitives) or split the MS into smaller pieces. The detection
    is structural (= same ms_id + status=in_review), not semantic — actual
    overlap is judged by the humans.
    """
    if not ms_id:
        return []
    conflicts = []
    for ms in data.get("milestones", []):
        if ms.get("id") != ms_id:
            continue
        for e in ms.get("entries", []):
            if e.get("type") != "pr":
                continue
            if e.get("id") == new_eid:
                continue
            if e.get("status") not in ("in_review", "open"):
                continue
            meta = e.get("meta") or {}
            if meta.get("pr_status") not in ("in_review", "open", None):
                continue
            conflicts.append({
                "eid": e.get("id"),
                "title": e.get("description", ""),
                "url": meta.get("url", ""),
                "author": meta.get("author", ""),
                "intent": meta.get("intent", ""),
                "date": e.get("date", ""),
            })
        break
    return conflicts


def cmd_pr_show():
    """Show a PR record's full detail (intent / commits / review history).

    Used by /review to pull intent before judging code changes (e-608).
    Resolution rules for the input identifier:
      - `e-NNN`         → entry id direct match
      - `<int>`         → PR number; looks up the matching entry
      - `https://…/pull/<n>` → URL match
    """
    ident = (os.environ.get("BEACON_PR_IDENT", "") or "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ident:
        print("Error: PR identifier required (e-id, PR number, or URL)", file=sys.stderr)
        sys.exit(1)

    data = load_project()

    # Find candidate PR entries across all milestones
    found = None
    found_ms_id = ""
    for ms in data.get("milestones", []):
        if not isinstance(ms, dict):
            continue
        for ent in ms.get("entries", []) or []:
            if not isinstance(ent, dict) or ent.get("type") != "pr":
                continue
            meta = ent.get("meta") or {}
            # Match by entry id
            if ent.get("id") == ident:
                found, found_ms_id = ent, ms.get("id", "")
                break
            # Match by PR number (int or numeric string)
            try:
                if ident.isdigit() and int(ident) == meta.get("pr_number"):
                    found, found_ms_id = ent, ms.get("id", "")
                    break
            except (ValueError, AttributeError):
                pass
            # Match by URL
            if meta.get("url") and (meta["url"] == ident or meta["url"].rstrip("/") == ident.rstrip("/")):
                found, found_ms_id = ent, ms.get("id", "")
                break
        if found:
            break

    if not found:
        print(f"Error: no PR entry matches '{ident}'", file=sys.stderr)
        sys.exit(1)

    meta = found.get("meta") or {}
    payload = {
        "entry_id": found.get("id"),
        "ms_id": found_ms_id,
        "description": found.get("description"),
        "status": found.get("status"),
        "url": meta.get("url"),
        "pr_number": meta.get("pr_number"),
        "author": meta.get("author"),
        "intent": meta.get("intent") or "",
        "pr_status": meta.get("pr_status"),
        "review_status": meta.get("review_status"),
        "review_rationale": meta.get("review_rationale"),
        # e-609: review back-and-forth visibility — full transition history
        # so the timeline can render "pending → changes_requested → pending → approved"
        "review_history": meta.get("review_history") or [],
        "commits": [
            {
                "id": c.get("id"),
                "hash": (c.get("meta") or {}).get("hash"),
                "message": c.get("description"),
            }
            for c in (found.get("entries") or [])
            if isinstance(c, dict) and c.get("type") == "commit"
        ],
    }

    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(f"PR {payload['entry_id']} (ms: {found_ms_id})")
    if payload["url"]:
        print(f"  URL:    {payload['url']}")
    print(f"  Title:  {payload['description']}")
    print(f"  Status: {payload['status']} / review: {payload['review_status']}")
    if payload["intent"]:
        print(f"  Intent: {payload['intent']}")
    else:
        print(f"  Intent: (none recorded — /review cannot do intent-vs-impl check)")
    if payload["review_rationale"]:
        print(f"  Rationale: {payload['review_rationale']}")
    if payload["review_history"]:
        # e-609: surface review back-and-forth count + the transition trail
        roundtrips = sum(
            1 for i, h in enumerate(payload["review_history"][1:], 1)
            if h["status"] == "pending"
        )
        print(f"  Review history ({len(payload['review_history'])} transitions, "
              f"{roundtrips} round-trip(s)):")
        for h in payload["review_history"]:
            at = h.get("at", "")[:19]  # trim to "YYYY-MM-DDTHH:MM:SS"
            tail = f" — {h['rationale'][:60]}" if h.get("rationale") else ""
            print(f"    {at}  {h.get('status', '?'):<19}{tail}")
    if payload["commits"]:
        print(f"  Commits ({len(payload['commits'])}):")
        for c in payload["commits"]:
            print(f"    {c['hash']}  {c['message']}")


def cmd_pr_close():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_close(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "closed"}, ensure_ascii=False))
    else:
        print(f"Closed PR [{entry_id}]: {entry.get('description', '')}")


def cmd_pr_approve():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("承認の根拠・受け入れたトレードオフは？ (Rationale for approval): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for approve. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_approve(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "approved",
                          "review_rationale": rationale}, ensure_ascii=False))
    else:
        print(f"Approved PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")


def cmd_pr_reject():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("却下の理由・懸念点は？ (Rationale for rejection): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for reject. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_reject(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "rejected",
                          "review_rationale": rationale}, ensure_ascii=False))
    else:
        print(f"Rejected PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Rationale: {rationale}")


def cmd_pr_request_review():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        ms, entry = core.pr_request_review(data, entry_id)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "in_review"}, ensure_ascii=False))
    else:
        print(f"In review: [{entry_id}]: {entry.get('description', '')}")


def cmd_pr_request_changes():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    rationale = os.environ.get("BEACON_RATIONALE", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)

    if not rationale:
        try:
            rationale = input("修正要求の理由・具体的な懸念点は？ (Reason for requesting changes): ").strip()
        except (EOFError, KeyboardInterrupt):
            pass

    if not rationale:
        print("Error: rationale is required for request-changes. Decision trail must be complete.", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        ms, entry = core.pr_request_changes(data, entry_id, rationale=rationale)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": entry_id, "review_status": "changes_requested"}, ensure_ascii=False))
    else:
        print(f"Changes requested on PR [{entry_id}]: {entry.get('description', '')}")
        if rationale:
            print(f"  Reason: {rationale}")




def cmd_pr_merge():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not entry_id:
        print("Error: entry ID required", file=sys.stderr)
        sys.exit(1)
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = load_project()
    try:
        ms, entry = core.pr_merge(data, entry_id, date=today)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)
    if json_mode:
        print(json.dumps({"entry_id": entry_id, "pr_status": "merged"}, ensure_ascii=False))
    else:
        print(f"Merged PR [{entry_id}]: {entry.get('description', '')}")


def _infer_pr_ms_id(data: dict) -> tuple[str, str]:
    """Infer the most likely target milestone ID for a PR being created.

    Returns (ms_id, reason). ms_id is "" when nothing matched confidently.
    Priority (highest first):
      1. Current branch name matches `ms-<n>` → use it if that MS exists.
      2. Last 5 commit messages contain `ms-<n>` → use it.
      3. Last 5 commit messages contain `(e-<n>)` → look up which MS owns that task.
      4. Exactly one milestone has status == "in_progress" → use it.
      5. Otherwise: return "" and let the caller prompt.

    This is best-effort and read-only. The caller MUST surface the `reason`
    to the user (no silent guessing).
    """
    if not isinstance(data, dict):
        return "", ""

    ms_ids = {m.get("id") for m in data.get("milestones", []) if isinstance(m, dict)}

    # 1) Branch name
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        branch = ""
    if branch:
        m = re.search(r"ms-(\d+)", branch)
        if m:
            cand = f"ms-{m.group(1)}"
            if cand in ms_ids:
                return cand, f"branch name `{branch}` → {cand}"

    # 2) Recent commit messages for ms-N
    try:
        log = subprocess.run(
            ["git", "log", "--pretty=%s", "-5"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        log = ""
    for line in log.splitlines():
        m = re.search(r"ms-(\d+)", line)
        if m:
            cand = f"ms-{m.group(1)}"
            if cand in ms_ids:
                return cand, f"recent commit `{line[:60]}` → {cand}"

    # 3) Recent commits for e-N → reverse-lookup owning MS
    for line in log.splitlines():
        m = re.search(r"\be-(\d+)\b", line)
        if m:
            eid = f"e-{m.group(1)}"
            for ms in data.get("milestones", []):
                if not isinstance(ms, dict):
                    continue
                for ent in ms.get("entries", []) or []:
                    if isinstance(ent, dict) and ent.get("id") == eid:
                        return ms.get("id", ""), f"task {eid} in commit `{line[:60]}` → {ms.get('id')}"

    # 4) Single in-progress milestone
    active = [m for m in data.get("milestones", [])
              if isinstance(m, dict) and m.get("status") == "in_progress"]
    if len(active) == 1:
        return active[0].get("id", ""), f"sole active milestone ({active[0].get('id')})"

    return "", ""


def cmd_pr_create():
    """Wrapper for gh pr create that auto-records the PR in beacon."""
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    gh_args = os.environ.get("BEACON_GH_ARGS", "")

    # e-607: auto-infer ms_id when -m was omitted.
    if not ms_id:
        try:
            _data_for_infer = load_project()
        except Exception:
            _data_for_infer = {}
        inferred, reason = _infer_pr_ms_id(_data_for_infer)
        if inferred:
            ms_id = inferred
            print(f"Beacon: inferred -m {ms_id} ({reason})", file=sys.stderr)
        else:
            # We DO NOT abort — gh pr create can still run, but the PR record
            # will not be linked to any MS. Warn the caller so the user knows.
            print(
                "Beacon: -m was not given and no MS could be inferred from "
                "branch/commits/active state. PR record will be unattached.",
                file=sys.stderr,
            )

    # Run gh pr create and capture the URL from stdout
    cmd = ["gh", "pr", "create"]
    if gh_args:
        import shlex
        cmd += shlex.split(gh_args)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)

    # Extract PR URL from gh output (last line that looks like a URL)
    pr_url = ""
    for line in reversed(result.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("https://github.com/") and "/pull/" in line:
            pr_url = line
            break

    if not pr_url:
        print("Warning: could not detect PR URL from gh output", file=sys.stderr)
        return

    if not intent:
        try:
            intent = input(f"Intent for beacon PR record (or Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            intent = ""

    gh_info = _fetch_gh_pr_info(pr_url)
    title = gh_info.get("title", "")
    commits = gh_info.get("commits", [])

    date = __import__("datetime").date.today().isoformat()
    data = load_project()
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=pr_url, intent=intent, date=date,
                          title=title, commits=commits,
                          session_id=_resolve_session_id())  # ms-57 / e-1062
    except ValueError as e:
        print(f"Warning: beacon pr record failed: {e}", file=sys.stderr)
        return
    save_project(data)
    print(f"Beacon: PR recorded [{eid}]: {title or pr_url}")
    if commits:
        print(f"  Commits: {len(commits)} linked")


# ---------------------------------------------------------------------------
# GitHub Issue import (ms-28)
# ---------------------------------------------------------------------------

def _gh_issue_fetch(number: int) -> dict:
    """Fetch a single GitHub issue via gh CLI. Returns dict or raises."""
    import subprocess as _sp
    result = _sp.run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body,url,labels,state"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh issue view {number} failed")
    return json.loads(result.stdout)


def _gh_issues_list(state: str = "open") -> list:
    """List GitHub issues via gh CLI. Returns list of dicts."""
    import subprocess as _sp
    result = _sp.run(
        ["gh", "issue", "list", "--state", state, "--limit", "200",
         "--json", "number,title,body,url,labels,state"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh issue list failed")
    return json.loads(result.stdout)


def cmd_issue_import():
    """Import a GitHub Issue as a beacon task."""
    number_str = os.environ.get("BEACON_ISSUE_NUMBER", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not number_str:
        print("Error: issue number required. Usage: beacon issue import <number>", file=sys.stderr)
        sys.exit(1)
    try:
        number = int(number_str)
    except ValueError:
        print(f"Error: invalid issue number: {number_str}", file=sys.stderr)
        sys.exit(1)

    try:
        issue = _gh_issue_fetch(number)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    imported = core._find_imported_issue_numbers(data)
    if number in imported:
        print(f"Already imported: Issue #{number}")
        sys.exit(0)

    if issue.get("state") == "CLOSED":
        print(f"Warning: Issue #{number} is already closed on GitHub")

    eid = core.issue_import(
        data, ms_id=ms_id, number=number,
        url=issue.get("url", ""),
        title=issue.get("title", ""),
        body=issue.get("body", ""),
    )
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": eid, "issue_number": number,
                          "title": issue.get("title", "")}, ensure_ascii=False))
    else:
        print(f"Imported Issue #{number} → [{eid}]: {issue.get('title', '')}")


def cmd_issue_list():
    """List open GitHub issues not yet imported into beacon."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        issues = _gh_issues_list("open")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    imported = core._find_imported_issue_numbers(data)
    unimported = [i for i in issues if i["number"] not in imported]

    if json_mode:
        print(json.dumps(unimported, ensure_ascii=False))
    else:
        if not unimported:
            print("All open GitHub Issues are already imported.")
        else:
            print(f"Unimported open Issues ({len(unimported)}):")
            for issue in unimported:
                print(f"  #{issue['number']}: {issue['title']}")


def cmd_issue_sync():
    """Import all open GitHub issues not yet in beacon."""
    ms_id = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        issues = _gh_issues_list("open")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    imported = core._find_imported_issue_numbers(data)

    added = []
    for issue in issues:
        number = issue["number"]
        if number in imported:
            continue
        eid = core.issue_import(
            data, ms_id=ms_id, number=number,
            url=issue.get("url", ""),
            title=issue.get("title", ""),
            body=issue.get("body", ""),
        )
        added.append({"entry_id": eid, "issue_number": number, "title": issue.get("title", "")})

    if added:
        save_project(data)

    if json_mode:
        print(json.dumps({"imported": added, "already_imported": len(imported)}, ensure_ascii=False))
    else:
        if not added:
            print("No new issues to import.")
        else:
            print(f"Imported {len(added)} issue(s):")
            for item in added:
                print(f"  #{item['issue_number']} → [{item['entry_id']}]: {item['title']}")


# ---------------------------------------------------------------------------
# Skill install
# ---------------------------------------------------------------------------

def _resolve_skills_src() -> str:
    """Locate the skills source directory.

    Resolution order (first existing wins):
      1. ``<beacon_root>/skills/`` — source / editable / brew layouts.
         beacon_root = directory two parents up from commands.py.
      2. ``<beacon_cli>/_bundled_skills/`` — wheel/pipx layout where this
         file is at ``<site-packages>/beacon_cli/_bundled_lib/commands.py``
         and skills were remapped via ``setuptools.package-dir`` into
         ``<site-packages>/beacon_cli/_bundled_skills/``.

    Returns empty string when neither exists so the caller can produce a
    targeted error.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    # 1) source layout: <repo>/skills/
    candidate = os.path.join(os.path.dirname(here), "skills")
    if os.path.isdir(candidate):
        return candidate
    # 2) wheel layout: <site-packages>/beacon_cli/_bundled_skills/
    candidate = os.path.join(here, "..", "_bundled_skills")
    candidate = os.path.normpath(candidate)
    if os.path.isdir(candidate):
        return candidate
    return ""


def _hook_unusable_on_windows(cmd: str) -> bool:
    """True if ``cmd`` is a bare ``.sh`` script while running on Windows.

    Claude Code's hook runner cannot execute a ``.sh`` on Windows (there is no
    shell association that yields the expected stdout JSON), so such a hook
    silently no-ops and ``/beacon-log`` never fires. Used to (a) keep
    ``_resolve_hook_command`` from resolving to a ``.sh`` on Windows and
    (b) let ``beacon doctor`` flag an already-broken config. (ms-44 e-853)
    """
    return os.name == "nt" and cmd.strip().lower().endswith(".sh")


def _bash_safe(path: str) -> str:
    """Normalize a path for use inside a Claude Code hook command (e-1043).

    Claude Code runs hook commands through bash (``/usr/bin/bash``) even on
    Windows. A backslash absolute path like
    ``C:\\Users\\me\\.local\\bin\\beacon-hook-post-commit.EXE`` has its
    backslashes eaten as escapes by bash, yielding
    ``C:Usersme.localbin...: command not found`` — so the hook silently fails
    on every Bash tool call. Forward slashes with the drive letter
    (``C:/Users/...``) are interpreted correctly by bash/msys, so rewrite them
    on Windows. POSIX paths are returned unchanged.
    """
    if os.name == "nt" and path:
        return path.replace("\\", "/")
    return path


def _resolve_hook_command(hook_basename: str) -> str:
    """Return a cross-platform command string for the named hook.

    Resolution order:
      1. ``shutil.which("beacon-hook-<name>")`` — the console-script entry-point
         installed by pipx / setuptools. Absolute path so Claude Code can
         spawn it without PATH dependence.
      2. Module-level ``CLAUDE_*_HOOK_SCRIPT`` constant when it points at an
         existing file. This preserves the bash-installed source/brew layout
         AND honors test-time ``monkeypatch.setattr(commands, "CLAUDE_…", …)``.
      3. ``<beacon_root>/bin/<hook_basename>`` — source / brew layout where
         the bash script lives next to the bin/beacon launcher.
      4. ``python -m beacon_cli.hooks.<name>`` — last-resort fallback that
         works as long as the Python interpreter that ran setup can still
         import the module. Used inside CI / wheel install when entry-points
         haven't been added to PATH yet (rare).

    ``hook_basename`` is the bash filename (e.g. ``beacon-post-commit-hook.sh``);
    we map it to the matching entry-point / module name.
    """
    # bash filename → (entry-point name, module name, module constant name)
    mapping = {
        "beacon-post-commit-hook.sh": (
            "beacon-hook-post-commit",
            "beacon_cli.hooks.post_commit",
            "CLAUDE_HOOK_SCRIPT",
        ),
        "beacon-postcompact.sh": (
            "beacon-hook-postcompact",
            "beacon_cli.hooks.postcompact",
            "CLAUDE_POSTCOMPACT_HOOK_SCRIPT",
        ),
        "beacon-save-hook.sh": (
            "beacon-hook-save",
            "beacon_cli.hooks.save_hook",
            "CLAUDE_SAVE_HOOK_SCRIPT",
        ),
        # ms-44 e-854: Stop hook (context-usage threshold monitor). Python
        # port lives at beacon_cli.hooks.context_monitor; bash stays at
        # bin/context-usage-monitor.sh for Mac/Linux source / brew users.
        "context-usage-monitor.sh": (
            "beacon-hook-context-monitor",
            "beacon_cli.hooks.context_monitor",
            "CLAUDE_CONTEXT_MONITOR_HOOK_SCRIPT",
        ),
    }
    entry_name, module_name, const_name = mapping.get(
        hook_basename, ("", "", "")
    )

    # NOTE: every path-returning branch below goes through _bash_safe() so the
    # command written into settings.json is bash-safe on Windows (e-1043).
    if entry_name:
        resolved = shutil.which(entry_name)
        if resolved:
            # e-1170: write the bare entry-point name (not the absolute
            # path) so the hook command survives `beacon` upgrades that
            # relocate the binary (e.g. pipx → pip --user, ~/.local/bin →
            # AppData/Roaming/Python/.../Scripts). Claude Code re-resolves
            # via PATH at hook fire time. We still call shutil.which here
            # to *validate* the entry-point exists at install time — if it
            # doesn't, we fall through to the bash / module fallback.
            return entry_name

    # Honor the module-level constant (set at import time, but tests
    # routinely monkeypatch them — keeping this lookup means existing
    # test_install_hooks.py contracts continue to hold).
    if const_name:
        const_val = globals().get(const_name, "")
        if const_val and os.path.exists(const_val) and not _hook_unusable_on_windows(const_val):
            return _bash_safe(const_val)

    # Source / brew: bash next to the launcher.
    bash_path = _find_hook(hook_basename)
    if bash_path and os.path.exists(bash_path) and not _hook_unusable_on_windows(bash_path):
        return _bash_safe(bash_path)

    # Final fallback: invoke the module via the current interpreter.
    if module_name:
        return f"{_bash_safe(sys.executable)} -m {module_name}"
    return ""


def cmd_skill_install():
    """Install beacon Claude Code Skills into ~/.claude/skills/, update CLAUDE.md, and configure hooks."""
    import shutil
    _append_claude_md()

    skills_src = _resolve_skills_src()
    if not skills_src:
        print("Error: skills directory not found (looked in source layout and wheel _bundled_skills).")
        sys.exit(1)
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Destination: ~/.claude/skills/
    home = _user_home()
    claude_skills = os.path.join(home, ".claude", "skills")
    os.makedirs(claude_skills, exist_ok=True)

    # Separate skills from companion files.
    # Companion convention: filename starts with `_` and is NOT installed as a
    # standalone Skill. Instead it's placed in the related Skill's directory
    # as a doc that the Skill can Read at runtime.
    # Filename: _<skill-name>-<companion-suffix>.md
    #   The <skill-name> portion is matched against existing skill names
    #   (longest match wins). The <companion-suffix> becomes the destination
    #   filename inside that Skill's directory.
    skill_files = []
    companion_files = []
    for src_file in sorted(os.listdir(skills_src)):
        if not src_file.endswith(".md"):
            continue
        if src_file.startswith("_"):
            companion_files.append(src_file)
        else:
            skill_files.append(src_file)

    skill_names = [f[:-3] for f in skill_files]

    installed = []
    for src_file in skill_files:
        skill_name = src_file[:-3]
        dest_dir = os.path.join(claude_skills, skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "skill.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        installed.append(skill_name)

    companion_installed = []
    for src_file in companion_files:
        base = src_file[1:-3]  # strip leading _ and trailing .md
        # Find longest matching skill name as the owning skill
        match = ""
        for s in skill_names:
            if (base == s or base.startswith(s + "-")) and len(s) > len(match):
                match = s
        if not match:
            print(f"  ! Companion file {src_file} doesn't match any installed skill (skipping)")
            continue
        # Companion suffix: everything after `<skill-name>-`
        suffix = base[len(match) + 1:] if len(base) > len(match) else "companion"
        if not suffix:
            suffix = "companion"
        dest_dir = os.path.join(claude_skills, match)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, f"{suffix}.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        companion_installed.append(f"{match}/{suffix}.md")

    if installed:
        print(f"Installed {len(installed)} Skills to {claude_skills}:")
        for name in installed:
            print(f"  /{name}")
    else:
        print("No skills found to install.")
    if companion_installed:
        print(f"Installed {len(companion_installed)} companion file(s):")
        for path in companion_installed:
            print(f"  {path}")

    # Configure Claude Code PostToolUse hooks.
    # ms-44 e-777: cross-platform — _resolve_hook_command returns a console-
    # script entry-point path (pipx install) or the bash .sh path (source /
    # brew layout) or a `python -m beacon_cli.hooks.<name>` fallback.
    hook_script = _resolve_hook_command("beacon-post-commit-hook.sh")
    settings_path = (
        os.environ.get("BEACON_SETTINGS_PATH", "")
        or os.path.join(home, ".claude", "settings.json")
    )
    _install_claude_hooks(hook_script, settings_path)

    # ms-46 e-725 follow-up: install git pre-commit hook for beacon dev clones.
    # Only acts when running inside the beacon source repo (has .git/ AND
    # scripts/hooks/pre-commit). Brew-installed users don't have a .git/ here
    # so this is a no-op for them.
    _install_dev_precommit_hook(beacon_root)


def _install_dev_precommit_hook(beacon_root: str) -> None:
    """Install scripts/hooks/pre-commit as a symlink to .git/hooks/pre-commit.

    Idempotent. Safe to call from any environment — does nothing if not a git clone.
    Backs up any existing non-symlink pre-commit hook to .git/hooks/pre-commit.bak.
    """
    git_dir = os.path.join(beacon_root, ".git")
    if not os.path.isdir(git_dir):
        return  # brew install / tarball, no .git
    src_rel = os.path.join("scripts", "hooks", "pre-commit")
    src_abs = os.path.join(beacon_root, src_rel)
    if not os.path.isfile(src_abs):
        return  # source not present (e.g., very old checkout)

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-commit")
    # Compute relative path from .git/hooks/ back to scripts/hooks/pre-commit
    # (.git/hooks/ → .git/ → repo root → scripts/hooks/pre-commit)
    link_target = os.path.join("..", "..", src_rel)

    # If already symlinked correctly, nothing to do.
    if os.path.islink(target):
        try:
            current = os.readlink(target)
            if current == link_target:
                return  # already correct
        except OSError:
            pass

    # Back up existing non-symlink hook so we don't clobber custom logic.
    if os.path.exists(target) and not os.path.islink(target):
        import time
        backup = target + f".bak.{int(time.time())}"
        try:
            os.rename(target, backup)
            print(f"  [hook] backed up existing pre-commit hook → {os.path.basename(backup)}")
        except OSError as e:
            print(f"  [hook] WARN: couldn't back up existing pre-commit ({e}), skipping")
            return
    elif os.path.islink(target):
        # Stale symlink (pointing elsewhere) — remove
        try:
            os.unlink(target)
        except OSError:
            return

    # Make sure source is executable (chmod 0o755). No-op on Windows where
    # file modes aren't used in the Unix sense; safe to ignore failures.
    try:
        os.chmod(src_abs, 0o755)
    except OSError:
        pass

    # Try symlink first (POSIX, atomic, tracks source updates automatically).
    # On Windows without Developer Mode / admin, symlink() raises OSError —
    # fall back to a plain file copy so the hook still fires. The copy
    # version becomes stale if the source is updated later; print a hint so
    # the user knows to re-run `beacon skill install` after pulling.
    try:
        os.symlink(link_target, target)
        print(f"  [hook] installed pre-commit hook (symlink → {src_rel})")
        return
    except OSError as e:
        symlink_err = e

    try:
        import shutil as _shutil
        _shutil.copyfile(src_abs, target)
        try:
            os.chmod(target, 0o755)
        except OSError:
            pass
        print(
            f"  [hook] installed pre-commit hook (copy → {src_rel}; "
            f"re-run 'beacon skill install' after updating the hook source)"
        )
    except OSError as copy_err:
        print(
            f"  [hook] WARN: couldn't install pre-commit hook — "
            f"symlink: {symlink_err}; copy: {copy_err}"
        )


# ---------------------------------------------------------------------------
# Self-update (`beacon update`)
# ---------------------------------------------------------------------------

def _detect_install_method() -> dict:
    """Detect how beacon was installed.

    Returns a dict:
      {
        "method": "brew" | "git" | "unknown",
        "prefix": str | None,           # brew prefix or git repo root
        "current_version": str,         # beacon --version
      }

    "brew": realpath of the running bin/beacon lives under `brew --prefix`.
    "git":  beacon root has a `.git/` directory (developer / manual clone install).
    "unknown": none of the above.
    """
    info = {"method": "unknown", "prefix": None, "current_version": __version__}

    # Resolve the running beacon root (lib/commands.py is at <root>/lib/commands.py)
    beacon_root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # 1) Homebrew detection
    try:
        result = subprocess.run(
            ["brew", "--prefix", "beacon"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            brew_prefix = result.stdout.strip()
            if brew_prefix:
                brew_real = os.path.realpath(brew_prefix)
                # beacon root may be the brew prefix itself, or sit under Cellar/...
                if beacon_root == brew_real or beacon_root.startswith(brew_real + os.sep):
                    info["method"] = "brew"
                    info["prefix"] = brew_prefix
                    return info
                # Cellar path: brew_prefix is a symlink to Cellar/beacon/<v>/...
                # If beacon_root is under any HOMEBREW Cellar, treat as brew.
                if "/Cellar/beacon/" in beacon_root:
                    info["method"] = "brew"
                    info["prefix"] = brew_prefix
                    return info
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 2) Git checkout detection
    if os.path.isdir(os.path.join(beacon_root, ".git")):
        info["method"] = "git"
        info["prefix"] = beacon_root
        return info

    return info


def cmd_update():
    """Self-update beacon: `brew upgrade beacon` (or git pull) then `beacon skill install`.

    Flags (via environment):
      BEACON_UPDATE_CHECK=1     Dry-run; report what would happen, no changes.
      BEACON_UPDATE_SKILL_ONLY=1  Skip brew/git step, only refresh skills.
      BEACON_UPDATE_YES=1       Skip interactive confirmation.

    Exit codes:
      0  success (or no-op when already up to date)
      1  failure (network / brew / git error)
    """
    check_only = os.environ.get("BEACON_UPDATE_CHECK", "") == "1"
    skill_only = os.environ.get("BEACON_UPDATE_SKILL_ONLY", "") == "1"
    auto_yes = os.environ.get("BEACON_UPDATE_YES", "") == "1"

    info = _detect_install_method()
    current = info["current_version"]
    method = info["method"]

    print(f"Current: beacon {current}  (install method: {method})")

    if skill_only:
        print("→ Skipping CLI upgrade (--skill-only); refreshing Claude Code Skills only.")
        if check_only:
            print("[dry-run] would run: beacon skill install")
            sys.exit(0)
        cmd_skill_install()
        return

    if method == "brew":
        # 1. Probe latest available version (best-effort).
        latest = None
        try:
            r = subprocess.run(
                ["brew", "info", "--json=v2", "beacon"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                try:
                    payload = json.loads(r.stdout)
                    formulae = payload.get("formulae", [])
                    if formulae:
                        latest = formulae[0].get("versions", {}).get("stable")
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        if latest:
            print(f"Latest:  beacon {latest}")
            if latest == current:
                print("✓ Already up to date.")
                if not check_only:
                    print("→ Re-installing Skills to ensure they match the running CLI.")
                    cmd_skill_install()
                sys.exit(0)
        else:
            print("Latest:  (could not determine — proceeding anyway)")

        if check_only:
            print("[dry-run] would run:")
            print("  brew update")
            print("  brew upgrade beacon")
            print("  beacon skill install")
            sys.exit(0)

        if not auto_yes:
            try:
                ans = input("Proceed with upgrade? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans and ans not in ("y", "yes", ""):
                print("Aborted.")
                sys.exit(0)

        # 2. brew update (best-effort; do not abort on network blip)
        print("→ brew update ...")
        r = subprocess.run(["brew", "update"])
        if r.returncode != 0:
            print("  (brew update failed; continuing to upgrade anyway)")

        # 3. brew upgrade beacon
        print("→ brew upgrade beacon ...")
        r = subprocess.run(["brew", "upgrade", "beacon"])
        if r.returncode != 0:
            # Detect the common "already up to date" case via re-check
            after = subprocess.run(
                ["beacon", "--version"], capture_output=True, text=True
            )
            if after.returncode == 0 and current in after.stdout:
                print("  No upgrade available (already at latest).")
            else:
                print("  brew upgrade failed.")
                sys.exit(1)

        # 4. Reinstall skills (the new CLI may have new/changed Skills)
        print("→ beacon skill install ...")
        # IMPORTANT: invoke the freshly-installed CLI, not this in-memory module.
        r = subprocess.run(["beacon", "skill", "install"])
        if r.returncode != 0:
            print("  Skill install failed.")
            sys.exit(1)

        print("✓ Update complete.")
        return

    if method == "git":
        prefix = info["prefix"]
        print(f"Detected developer / git install at: {prefix}")
        print("→ This install is not managed by Homebrew. Recommended manual steps:")
        print(f"    git -C {prefix} pull --ff-only")
        print(f"    beacon skill install")
        if check_only:
            print("[dry-run] no changes applied.")
            sys.exit(0)

        if not auto_yes:
            try:
                ans = input("Attempt automatic `git pull --ff-only` + skill install? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans and ans not in ("y", "yes", ""):
                print("Aborted. Run the commands above manually when ready.")
                sys.exit(0)

        r = subprocess.run(["git", "-C", prefix, "pull", "--ff-only"])
        if r.returncode != 0:
            print("  git pull failed (uncommitted changes or non-fast-forward). Resolve manually.")
            sys.exit(1)

        r = subprocess.run(["beacon", "skill", "install"])
        if r.returncode != 0:
            print("  Skill install failed.")
            sys.exit(1)
        print("✓ Update complete.")
        return

    # Unknown install method
    print("⚠ Could not determine how beacon was installed.")
    print("  If installed via Homebrew, ensure `brew` is on PATH.")
    print("  Otherwise, re-run the installer for your setup.")
    print(f"  You can refresh Skills only with: beacon update --skill-only")
    sys.exit(1)


def _is_path_command(cmd: str) -> bool:
    """Return True when ``cmd`` looks like a single executable path.

    ms-44 e-777: ``_install_claude_hooks`` originally received a bash .sh
    path and gated registration on ``os.path.exists``. The cross-platform
    refactor may pass back a multi-token command like
    ``/usr/bin/python3 -m beacon_cli.hooks.post_commit`` — that's not a
    file path, so we skip the existence check for those.

    ms-44 e-1170: distinguish absolute / relative paths (which have
    separators or a leading ``./``) from bare entry-point names (which
    are PATH-resolved by the shell, not file-existence-checked). After
    e-1170 we write the bare ``beacon-hook-post-commit`` to settings.json
    rather than the absolute path so the hook survives install relocation.
    """
    if not cmd or " " in cmd.strip():
        return False
    s = cmd.strip()
    # Path-like if it contains separators or starts with relative-path prefix.
    has_sep = "/" in s or "\\" in s
    has_dot_prefix = s.startswith(("./", ".\\"))
    return has_sep or has_dot_prefix


def _install_claude_hooks(hook_script: str, settings_path: str) -> None:
    """Add beacon PostToolUse + PostCompact hooks to Claude Code settings.json.

    ms-43 e-672: previously this only registered the PostToolUse commit hook,
    so users who installed via `beacon skill install` ended up missing the
    PostCompact orientation hook (registered by the legacy `_install_claude_hook`
    code path used by `beacon init`). The two install paths must produce the
    same end state.

    ms-44 e-777: ``hook_script`` may now be a console-script absolute path
    (pipx install) or a ``python -m ...`` fallback command, not just a .sh.
    We only run the disk-existence guard for single-token commands.
    """
    if not hook_script:
        print("Warning: could not resolve a PostToolUse hook command.")
        return
    if _is_path_command(hook_script) and not os.path.exists(hook_script):
        print(f"Warning: hook script not found at {hook_script}")
        return

    # Load existing settings
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, IOError, UnicodeDecodeError):
            # #21 follow-up: tolerate cp932 settings.json from legacy builds.
            pass

    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])

    # Dedup by hook *identity*: remove stale entries pointing to old install
    # paths (e.g. previous brew Cellar versions that no longer exist after
    # upgrade) or the obsolete bash .sh path after a pipx-style upgrade to the
    # Python entry-point.
    #
    # Identity rules:
    #   - Same basename (e.g. ``beacon-post-commit-hook.sh``) and different
    #     full path → stale.
    #   - Same "kind" — either references ``beacon-post-commit-hook`` /
    #     ``beacon-hook-post-commit`` / ``beacon_cli.hooks.post_commit`` —
    #     and different full command → stale.
    hook_basename = (
        os.path.basename(hook_script) if _is_path_command(hook_script) else hook_script
    )
    identity_substrings = (
        "beacon-post-commit-hook",
        "beacon-hook-post-commit",
        "beacon_cli.hooks.post_commit",
    )
    removed_stale = False
    cleaned_post = []
    for entry in post_tool_use:
        new_hooks_in_entry = []
        for h in entry.get("hooks", []):
            existing = h.get("command", "")
            same_basename = (
                _is_path_command(existing)
                and os.path.basename(existing) == hook_basename
            )
            same_kind = any(s in existing for s in identity_substrings)
            stale = (same_basename or same_kind) and existing != hook_script
            if stale:
                removed_stale = True
                continue  # drop stale
            new_hooks_in_entry.append(h)
        entry["hooks"] = new_hooks_in_entry
        if new_hooks_in_entry:
            cleaned_post.append(entry)
    post_tool_use[:] = cleaned_post

    # Check exact-path presence
    already_present = any(
        h.get("command", "") == hook_script
        for entry in post_tool_use
        for h in entry.get("hooks", [])
    )

    posttooluse_dirty = False
    if not already_present:
        # Add beacon PostToolUse hook (commit + deploy detection)
        post_tool_use.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": hook_script,
                "timeout": 10,
                "statusMessage": "Beacon: checking for commit or deploy..."
            }]
        })
        posttooluse_dirty = True

    # ms-43 e-672 / ms-44 e-777: register the PostCompact hook with the
    # cross-platform resolver so wheel installs (no bash .sh on disk) still
    # land on the Python entry-point.
    postcompact_dirty = False
    postcompact_cmd = _resolve_hook_command("beacon-postcompact.sh")
    pc_ok = bool(postcompact_cmd) and (
        not _is_path_command(postcompact_cmd) or os.path.exists(postcompact_cmd)
    )
    if pc_ok:
        post_compact = hooks.setdefault("PostCompact", [])
        pc_identity = (
            "beacon-postcompact",
            "beacon-hook-postcompact",
            "beacon_cli.hooks.postcompact",
        )
        # Drop stale PostCompact entries (same kind, different command) so an
        # old backslash path is rewritten with the freshly-resolved one. The
        # PostToolUse branch above already did this; PostCompact previously only
        # checked presence-by-kind and skipped, leaving the bad path in place
        # on re-doctor (e-1043 migration gap).
        cleaned_pc = []
        for entry in post_compact:
            kept = []
            for h in entry.get("hooks", []):
                existing = h.get("command", "")
                same_kind = any(s in existing for s in pc_identity)
                if same_kind and existing != postcompact_cmd:
                    removed_stale = True
                    continue  # drop stale
                kept.append(h)
            entry["hooks"] = kept
            if kept:
                cleaned_pc.append(entry)
        post_compact[:] = cleaned_pc

        already_pc = any(
            h.get("command", "") == postcompact_cmd
            for entry in post_compact
            for h in entry.get("hooks", [])
        )
        if not already_pc:
            post_compact.append({
                "hooks": [{
                    "type": "command",
                    "command": postcompact_cmd,
                    "timeout": 10,
                    "statusMessage": "Beacon: post-compaction orientation...",
                }],
            })
            postcompact_dirty = True

    # ms-44 e-854: register the Stop hook (context-usage threshold monitor)
    # via _resolve_hook_command so Mac/Linux source/brew users land on the
    # bash .sh, while Windows / wheel users land on the Python entry-point
    # (or `python -m beacon_cli.hooks.context_monitor` fallback). Critically,
    # _hook_unusable_on_windows() inside the resolver prevents a .sh path
    # from being written into Win settings.json — that was the e-854 bug.
    stop_hook_dirty = False
    stop_cmd = _resolve_hook_command("context-usage-monitor.sh")
    stop_ok = bool(stop_cmd) and (
        not _is_path_command(stop_cmd) or os.path.exists(stop_cmd)
    )
    if stop_ok:
        stop_hooks = hooks.setdefault("Stop", [])
        stop_identity = (
            "context-usage-monitor",   # bash basename, Mac/Linux
            "beacon-hook-context-monitor",  # console-script entry-point (pipx)
            "beacon_cli.hooks.context_monitor",  # python -m fallback
        )
        cleaned_stop = []
        for entry in stop_hooks:
            kept = []
            for h in entry.get("hooks", []):
                existing = h.get("command", "")
                same_kind = any(s in existing for s in stop_identity)
                if same_kind and existing != stop_cmd:
                    removed_stale = True
                    continue  # drop stale
                kept.append(h)
            entry["hooks"] = kept
            if kept:
                cleaned_stop.append(entry)
        stop_hooks[:] = cleaned_stop

        already_stop = any(
            h.get("command", "") == stop_cmd
            for entry in stop_hooks
            for h in entry.get("hooks", [])
        )
        if not already_stop:
            stop_hooks.append({
                "hooks": [{
                    "type": "command",
                    "command": stop_cmd,
                    "timeout": 10,
                    "statusMessage": "Beacon: checking context-usage threshold...",
                }],
            })
            stop_hook_dirty = True

    if removed_stale or posttooluse_dirty or postcompact_dirty or stop_hook_dirty:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        if removed_stale and not (posttooluse_dirty or postcompact_dirty or stop_hook_dirty):
            print(f"Hooks: cleaned stale {hook_basename} entries; current path active")
        else:
            parts = []
            if posttooluse_dirty:
                parts.append("PostToolUse")
            if postcompact_dirty:
                parts.append("PostCompact")
            if stop_hook_dirty:
                parts.append("Stop")
            print(f"Hooks: registered {' + '.join(parts) or 'nothing new'} in {settings_path}")
    else:
        print("Hooks: already configured in ~/.claude/settings.json")


def _next_deploy_id(data: dict, date_str: str) -> str:
    """Generate next deploy ID like deploy-20260517-1."""
    prefix = f"deploy-{date_str.replace('-', '')[:8]}"
    nums = []
    for d in data.get("deployments", []):
        if d["id"].startswith(prefix + "-"):
            try:
                nums.append(int(d["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def _next_release_id(data: dict, date_str: str) -> str:
    prefix = f"release-{date_str.replace('-', '')[:8]}"
    nums = []
    for r in data.get("releases", []):
        if r["id"].startswith(prefix + "-"):
            try:
                nums.append(int(r["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def cmd_deploy_record():
    """Record a deployment entry (major or minor) based on recent commits."""
    import subprocess as _sp
    mode = os.environ.get("BEACON_MODE", "")          # "prepare" or "finalize" or ""
    revision = os.environ.get("BEACON_REVISION", "")
    semver = os.environ.get("BEACON_SEMVER", "")
    # version (e-1274): git tag attached to this deploy (e.g. v0.22.0). Recorded
    # in the entry for cross-referencing with releases. Defaults to --semver
    # when --version not supplied so existing release flows backfill cleanly
    # without a CLI change.
    version = os.environ.get("BEACON_VERSION", "") or semver
    description = os.environ.get("BEACON_DESCRIPTION", "")
    deploy_hash = os.environ.get("BEACON_HASH", "")   # override: specify deployed commit
    deploy_date = os.environ.get("BEACON_DATE", "")   # override: specify deploy datetime
    insert_before = os.environ.get("BEACON_INSERT_BEFORE", "")  # insert before this deploy-id
    type_override = os.environ.get("BEACON_TYPE", "")  # override: "major" or "minor"
    environment = os.environ.get("BEACON_ENVIRONMENT", "prod")
    # ms-80 e-1831: backend (= GCP Cloud Run / AWS / TrailNode / custom) を deploy 記録に保存し
    # backend 別に追えるように。env 未指定 + cloud profile があれば profile name を fallback。
    backend = os.environ.get("BEACON_BACKEND", "").strip()
    if not backend:
        # Active profile を引いて backend を auto-detect (= 例: profile=aws-ga → backend="aws-ga")
        try:
            import profile as _profile
            backend = _profile.resolve_active_profile().name or ""
        except Exception:
            backend = ""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    now = deploy_date or core._now_iso()
    today = now[:10]

    # Resolve the target hash (short form)
    if deploy_hash:
        try:
            deploy_hash = _sp.check_output(
                ["git", "rev-parse", "--short", deploy_hash],
                stderr=_sp.DEVNULL, text=True
            ).strip()
        except Exception:
            pass

    # For retroactive inserts, find the previous deploy in insertion order
    deployments = data.get("deployments", [])
    if insert_before:
        idx = next((i for i, d in enumerate(deployments) if d["id"] == insert_before), len(deployments))
        prev_hash = deployments[idx - 1]["git_hash"] if idx > 0 else ""
        after_hash = deploy_hash or (deployments[idx]["git_hash"] if idx < len(deployments) else "HEAD")
    else:
        prev_hash = deployments[-1]["git_hash"] if deployments else ""
        after_hash = deploy_hash or "HEAD"

    # Collect commits in the range prev_hash..after_hash
    try:
        if prev_hash:
            log_out = _sp.check_output(
                ["git", "log", f"{prev_hash}..{after_hash}", "--format=%H %s"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
        else:
            log_out = _sp.check_output(
                ["git", "log", after_hash, "--format=%H %s", "-50"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
    except Exception:
        log_out = ""

    new_commits = []
    for line in log_out.splitlines():
        if line.strip():
            parts = line.split(" ", 1)
            new_commits.append({"hash": parts[0][:7], "message": parts[1] if len(parts) > 1 else ""})

    head_hash = deploy_hash or (new_commits[0]["hash"] if new_commits else _sp.check_output(
        ["git", "rev-parse", "--short", "HEAD"], text=True).strip())

    # Map commit hashes to milestones via beacon entries
    commit_hashes = [c["hash"] for c in new_commits]
    ms_status: dict[str, str] = {ms["id"]: ms.get("status", "") for ms in data.get("milestones", [])}

    # MSes that already appeared in previous deploys → they are patched, not newly completed
    previously_deployed: set[str] = set()
    for d in deployments:
        previously_deployed.update(d.get("newly_completed_ms", []))
        previously_deployed.update(d.get("milestones", []))  # legacy records

    # Find which MSes are touched by these commits, build per-MS commit lists
    newly_completed: set[str] = set()
    patch_ms: set[str] = set()
    milestone_commits: dict[str, list[str]] = {}  # ms_id -> [commit_hashes]

    for ms in data.get("milestones", []):
        ms_id = ms["id"]
        matched: list[str] = []

        def _scan(entries, _matched=matched, _ms_id=ms_id):
            for e in entries:
                if e.get("type") == "commit":
                    h = (e.get("meta") or {}).get("hash", "")
                    if h:
                        for c in commit_hashes:
                            if (h.startswith(c) or c.startswith(h)) and c not in _matched:
                                _matched.append(c)
                                status = ms_status.get(_ms_id, "")
                                if status in ("done", "observing") and _ms_id not in previously_deployed:
                                    newly_completed.add(_ms_id)
                                else:
                                    patch_ms.add(_ms_id)
                for child in e.get("entries", []):
                    _scan([child], _matched, _ms_id)
        _scan(ms.get("entries", []))

        if matched:
            milestone_commits[ms_id] = matched

    # Commits not associated with any milestone
    assigned_hashes = {c for cs in milestone_commits.values() for c in cs}
    unassigned_commits = [c for c in commit_hashes if c not in assigned_hashes]

    # Determine type (allow manual override)
    deploy_type = type_override if type_override in ("major", "minor") else ("major" if newly_completed else "minor")
    affected_ms = sorted(newly_completed if newly_completed else patch_ms)

    # --- Prepare mode: return context JSON for AI description generation ---
    if mode == "prepare":
        def _ms_context(ms_id):
            ms = next((m for m in data.get("milestones", []) if m["id"] == ms_id), {})
            entries = []
            def _collect(es):
                for e in es:
                    if e.get("type") == "commit" and len(entries) < 5:
                        h = (e.get("meta") or {}).get("hash", "")
                        if h and any(h.startswith(c) or c.startswith(h) for c in commit_hashes):
                            entries.append({"id": e.get("id",""), "description": e.get("description",""), "hash": h})
                    for child in e.get("entries", []):
                        _collect([child])
            _collect(ms.get("entries", []))
            return {"id": ms_id, "title": ms.get("title", ms_id), "commit_entries": entries}

        payload = {
            "deploy_type": "major" if newly_completed else "minor",
            "new_commits": new_commits[:20],
            "newly_completed_ms": [_ms_context(mid) for mid in sorted(newly_completed)],
            "patch_ms": [_ms_context(mid) for mid in sorted(patch_ms)],
            "unassigned_commits": unassigned_commits,
            "last_deploy": {"id": deployments[-1]["id"], "date": deployments[-1].get("date","")} if deployments else None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    # Auto-generate description (fallback if not AI-provided)
    if not description:
        ms_titles = []
        for ms in data.get("milestones", []):
            if ms["id"] in affected_ms:
                ms_titles.append(ms.get("title", ms["id"]))
        description = "・".join(ms_titles) if ms_titles else "deploy"

    deploy_id = _next_deploy_id(data, today)

    # Find links_to for minor: find the most recent major deploys that touch the same MSes
    links_to = []
    if deploy_type == "minor":
        for d in reversed(deployments):
            if d.get("type") == "major":
                if any(m in d.get("milestones", []) for m in affected_ms):
                    links_to.append(d["id"])
            if len(links_to) >= 3:
                break

    deploy_entry = {
        "id": deploy_id,
        "type": deploy_type,
        "date": now,
        "git_hash": head_hash,
        "environment": environment,
        "milestones": affected_ms,
        "newly_completed_ms": sorted(newly_completed),
        "patch_ms": sorted(patch_ms),
        "milestone_commits": milestone_commits,
        "unassigned_commits": unassigned_commits,
        "commit_hashes": commit_hashes,
        "description": description,
        "linked_release": None,
    }
    if revision:
        deploy_entry["cloud_run_revision"] = revision
    if version:
        # e-1274: top-level version tag (e.g. v0.22.0). Surfaces in Releases tab
        # without needing to dereference linked_release → releases[*].semver.
        deploy_entry["version"] = version
    if backend:
        # ms-80 e-1831: backend 名 (= 例 "default" / "aws-ga" / "trailnode")。multi-backend
        # 運用で「どの backend に何が反映されたか」を deploy 別に追えるように。
        deploy_entry["backend"] = backend
    if links_to:
        deploy_entry["links_to"] = links_to

    # Handle semver: create a Release entry and link
    release_entry = None
    if semver:
        release_id = _next_release_id(data, today)
        release_entry = {
            "id": release_id,
            "date": now,
            "semver": semver,
            "deploy_ids": [deploy_id],
            "description": description,
        }
        # e-1274: also stash the raw tag (e.g. v0.22.0). semver is the bare
        # number; version preserves the "v" prefix when the workflow passed it.
        if version and version != semver:
            release_entry["version"] = version
        deploy_entry["linked_release"] = release_id
        data.setdefault("releases", []).append(release_entry)
        # Create git tag
        try:
            _sp.run(["git", "tag", semver], check=True, capture_output=True)
            if not json_mode:
                print(f"Tagged: {semver}")
        except _sp.CalledProcessError:
            if not json_mode:
                print(f"Warning: git tag {semver} already exists or failed")

    dep_list = data.setdefault("deployments", [])
    if insert_before:
        idx = next((i for i, d in enumerate(dep_list) if d["id"] == insert_before), len(dep_list))
        dep_list.insert(idx, deploy_entry)
    else:
        dep_list.append(deploy_entry)
    save_project(data)

    if json_mode:
        out = {"deploy": deploy_entry}
        if release_entry:
            out["release"] = release_entry
        print(json.dumps(out, ensure_ascii=False))
    else:
        icon = "◉" if deploy_type == "major" else "○"
        ms_str = " ".join(f"[{m}]" for m in affected_ms) or "(no MS detected)"
        ver_str = f" {version}" if version and not semver else ""
        backend_str = f" <backend={backend}>" if backend else ""
        print(f"{icon} {deploy_id}{ver_str} [{deploy_type}]{backend_str} {ms_str}")
        print(f"  {description}")
        if semver:
            print(f"  Release: {release_entry['id']} ({semver})")
        if links_to:
            print(f"  Patches: {', '.join(links_to)}")


def cmd_deploy_list():
    """List deployment records, optionally filtered by environment or backend."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    env_filter = os.environ.get("BEACON_ENVIRONMENT", "")
    # ms-80 e-1831: backend 別 filter (= 例 --backend aws-ga で AWS だけ列挙)
    backend_filter = os.environ.get("BEACON_BACKEND", "").strip()
    data = load_project()
    deployments = data.get("deployments", [])
    releases = {r["id"]: r for r in data.get("releases", [])}

    if env_filter:
        deployments = [d for d in deployments if d.get("environment", "prod") == env_filter]
    if backend_filter:
        deployments = [d for d in deployments if d.get("backend", "") == backend_filter]

    if json_mode:
        print(json.dumps({"deployments": deployments, "releases": list(releases.values())},
                         ensure_ascii=False))
        return

    if not deployments:
        msg = f"No deployments recorded for env='{env_filter}'." if env_filter else "No deployments recorded yet."
        print(msg)
        return

    for d in reversed(deployments):
        icon = "◉" if d.get("type") == "major" else "○"
        rel = releases.get(d.get("linked_release", ""))
        semver_str = f" {rel['semver']}" if rel and rel.get("semver") else ""
        ms_str = " ".join(d.get("milestones", [])) or "-"
        env_str = f" [{d.get('environment', 'prod')}]" if d.get("environment") not in (None, "prod") else ""
        backend_str = f" <{d['backend']}>" if d.get("backend") else ""
        print(f"{icon} {d['id']}{semver_str}  {d['date'][:10]}  [{d.get('type','')}]{env_str}{backend_str}  {ms_str}")
        print(f"   {d.get('description', '')}")
        if d.get("links_to"):
            print(f"   patches: {', '.join(d['links_to'])}")


def cmd_deploy_delete():
    """Deprecated: physical deletion of deploy records is not allowed."""
    print("Error: 'beacon deploy delete' is deprecated.")
    print("  Deploy records are immutable facts — they cannot be physically deleted.")
    print("  To mark a record as invalid: beacon deploy void <id> --reason \"...\"")
    sys.exit(1)


def cmd_deploy_rollback():
    """Roll back to the deployment that ran *before* the most recent one (e-581).

    Plan:
      1. Find the latest non-voided deploy in `data["deployments"]`.
      2. Find the deploy immediately before it (same `service` if recorded).
      3. Print the `gcloud run services update-traffic ... --to-revisions ...`
         command the user should run.
      4. Optionally execute it (when --execute is passed).
      5. Mark the rolled-back deploy as voided with reason="rolled back to
         <prev-id>" so the timeline preserves the decision.

    This is an audit-first design: we never silently execute gcloud. The
    user must opt in with `--execute`, or read the command and run it
    themselves. Reason matches CORE doc `data-immutability-principle`:
    void carries a reason; the reason is what makes the timeline auditable.
    """
    reason = os.environ.get("BEACON_REASON", "")
    execute = os.environ.get("BEACON_EXECUTE", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    service_override = os.environ.get("BEACON_SERVICE", "")

    if not reason:
        print("Error: --reason is required for deploy rollback.", file=sys.stderr)
        print("  Example: beacon deploy rollback --reason \"high error rate on prod\"",
              file=sys.stderr)
        sys.exit(1)

    data = load_project()
    deployments = data.get("deployments", []) or []
    # Sort by date desc; void records may exist but should still be present.
    active = [d for d in deployments if not d.get("voided")]
    if not active:
        print("Error: no active deployments to roll back.", file=sys.stderr)
        sys.exit(1)
    active.sort(key=lambda d: d.get("deployed_at") or d.get("date", ""), reverse=True)

    current = active[0]
    service = service_override or current.get("service", "")
    previous = None
    for d in active[1:]:
        # Match by service when possible; fall back to "most recent earlier" otherwise.
        if not service or d.get("service") == service:
            previous = d
            break

    if not previous:
        print(
            "Error: cannot determine rollback target — only one active deploy "
            "exists for this service. Roll forward instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    prev_rev = previous.get("revision", "") or previous.get("meta", {}).get("revision", "")
    if not prev_rev:
        print(
            "Error: previous deploy has no `revision` recorded — cannot construct "
            "the gcloud update-traffic command. Add it with "
            f"`beacon deploy update {previous['id']} --revision <rev>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    region = current.get("region", "") or os.environ.get("BEACON_REGION", "asia-northeast1")
    gcloud_cmd = [
        "gcloud", "run", "services", "update-traffic", service,
        f"--to-revisions={prev_rev}=100",
        f"--region={region}",
    ]
    cmd_str = " ".join(gcloud_cmd)

    print(f"Rolling back {service}:")
    print(f"  current  → {current['id']}  rev={current.get('revision', '?')}")
    print(f"  rollback → {previous['id']}  rev={prev_rev}")
    print(f"  command  → {cmd_str}")
    print()

    if execute:
        print("=> executing gcloud command...")
        r = subprocess.run(gcloud_cmd)
        if r.returncode != 0:
            print("Error: gcloud command failed; deploy record NOT voided.", file=sys.stderr)
            sys.exit(r.returncode)
    else:
        print(
            "Not executed. Run the command above, then re-invoke with --execute "
            "to mark the deploy as voided automatically."
        )
        if not json_mode:
            sys.exit(0)

    # Void the rolled-back deploy so the project timeline reflects the
    # decision. We reuse cmd_deploy_void's core function via apply_operation.
    try:
        import operations
        project_id = _project_id_for_ops()

        full_reason = f"rolled back to {previous['id']} ({prev_rev}): {reason}"

        def op(d):
            voided = core.deploy_void(d, current["id"], reason=full_reason)
            return d, voided

        voided = operations.apply_operation(
            project_id, op, op_name="deploy.rollback", reason=full_reason,
        )
    except Exception as e:
        print(f"Warning: failed to void deploy record: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps({
            "voided_deploy": current["id"],
            "rollback_target": previous["id"],
            "rollback_revision": prev_rev,
            "executed": execute,
        }, ensure_ascii=False))
    else:
        print(f"✓ Voided {current['id']} with reason: {full_reason}")


def cmd_deploy_void():
    """Mark a deployment record as voided (immutable, never physically deleted)."""
    deploy_id = os.environ.get("BEACON_DEPLOY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not deploy_id:
        print("Error: deploy ID required", file=sys.stderr)
        sys.exit(1)
    if not reason:
        print("Error: --reason is required for deploy void.", file=sys.stderr)
        print("  Example: beacon deploy void <id> --reason \"誤ったハッシュで記録\"", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    try:
        dep = core.deploy_void(data, deploy_id, reason=reason)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    save_project(data, op={"op": "deploy_void", "deploy_id": dep["id"], "reason": reason})
    if json_mode:
        print(json.dumps({"id": dep["id"], "voided": True}, ensure_ascii=False))
    else:
        print(f"Voided: {dep['id']}")
        print(f"  Reason: {reason}")


def _next_push_id(data: dict, date_str: str) -> str:
    """Generate next push ID like push-20260519-1."""
    prefix = f"push-{date_str.replace('-', '')[:8]}"
    nums = []
    for p in data.get("pushes", []):
        if p["id"].startswith(prefix + "-"):
            try:
                nums.append(int(p["id"][len(prefix) + 1:]))
            except ValueError:
                pass
    n = (max(nums) + 1) if nums else 1
    return f"{prefix}-{n}"


def cmd_push_record():
    """Record a push (code release) entry based on commits since last push."""
    import subprocess as _sp
    mode = os.environ.get("BEACON_MODE", "")          # "prepare" or "finalize" or ""
    from_hash = os.environ.get("BEACON_FROM", "")
    to_hash = os.environ.get("BEACON_TO", "")
    branch = os.environ.get("BEACON_BRANCH", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    ms_id = os.environ.get("BEACON_MS", "")
    # version (e-1274): git tag attached to this push (e.g. v0.22.0). Stored
    # alongside branch / commits so later reviews can cross-reference releases
    # without timestamp guessing.
    version = os.environ.get("BEACON_VERSION", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    now = core._now_iso()
    today = now[:10]

    # Resolve branch
    if not branch:
        try:
            branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                      stderr=_sp.DEVNULL, text=True).strip()
        except Exception:
            branch = "main"

    # Resolve to_hash
    if not to_hash:
        try:
            to_hash = _sp.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       stderr=_sp.DEVNULL, text=True).strip()
        except Exception:
            to_hash = "HEAD"

    # Resolve from_hash: last recorded push's to_hash, or first commit
    pushes = data.get("pushes", [])
    if not from_hash:
        from_hash = pushes[-1]["to_hash"] if pushes else ""

    # Collect commits in range
    try:
        if from_hash:
            log_out = _sp.check_output(
                ["git", "log", f"{from_hash}..{to_hash}", "--format=%H %s"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
        else:
            log_out = _sp.check_output(
                ["git", "log", to_hash, "--format=%H %s", "-50"],
                stderr=_sp.DEVNULL, text=True
            ).strip()
    except Exception:
        log_out = ""

    commits = []
    for line in log_out.splitlines():
        if line.strip():
            parts = line.split(" ", 1)
            commits.append({"hash": parts[0][:7], "message": parts[1] if len(parts) > 1 else ""})

    # Resolve active ms_id if not given
    if not ms_id:
        for ms in data.get("milestones", []):
            if ms.get("status") == "in_progress":
                ms_id = ms["id"]
                break

    # Pushed by: git user
    try:
        pushed_by = _sp.check_output(["git", "config", "user.name"],
                                     stderr=_sp.DEVNULL, text=True).strip()
    except Exception:
        pushed_by = ""

    # --- Prepare mode: return context JSON for AI summary generation ---
    if mode == "prepare":
        last_push = pushes[-1] if pushes else None
        payload = {
            "branch": branch,
            "from_hash": from_hash,
            "to_hash": to_hash,
            "commits": commits[:30],
            "ms_id": ms_id,
            "last_push": {"id": last_push["id"], "date": last_push.get("pushed_at", "")} if last_push else None,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    # Auto-generate description if not AI-provided
    if not description and commits:
        description = commits[0]["message"] if len(commits) == 1 else f"{len(commits)} commits pushed"

    push_id = _next_push_id(data, today)
    push_entry = {
        "id": push_id,
        "branch": branch,
        "from_hash": from_hash,
        "to_hash": to_hash,
        "commit_count": len(commits),
        "commits": commits,
        "summary": description,
        "pushed_by": pushed_by,
        "pushed_at": now,
        "ms_id": ms_id or None,
    }
    if version:
        # e-1274: top-level version tag for cross-referencing with releases.
        push_entry["version"] = version

    data.setdefault("pushes", []).append(push_entry)
    save_project(data)

    if json_mode:
        print(json.dumps({"push": push_entry}, ensure_ascii=False))
    else:
        ver_str = f"  {version}" if version else ""
        print(f"↑ {push_id}{ver_str}  {branch}  {from_hash or '(initial)'}..{to_hash}  ({len(commits)} commits)")
        if description:
            print(f"  {description}")


def cmd_push_list():
    """List push records."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    pushes = data.get("pushes", [])

    if json_mode:
        print(json.dumps({"pushes": pushes}, ensure_ascii=False))
        return

    if not pushes:
        print("No push records.")
        return
    for p in reversed(pushes):
        date = p.get("pushed_at", "")[:16].replace("T", " ")
        print(f"↑ {p['id']}  {p['branch']}  {p.get('pushed_by', '')}  {date}  ({p.get('commit_count', 0)} commits)")
        if p.get("summary"):
            print(f"  {p['summary']}")


def cmd_project_archive():
    """Archive the current project (sets archived: true in project.json)."""
    data = load_project()
    if data.get("archived"):
        print("Project is already archived.")
        return
    data["archived"] = True
    save_project(data)
    print(f"Archived: [{data.get('name', '')}]")


def cmd_project_unarchive():
    """Unarchive the current project."""
    data = load_project()
    if not data.get("archived"):
        print("Project is not archived.")
        return
    data["archived"] = False
    save_project(data)
    print(f"Unarchived: [{data.get('name', '')}]")


# ---------------------------------------------------------------------------
# Project export / import (ms-14 e-828): full-snapshot backup
# ---------------------------------------------------------------------------
#
# Local-mode export reads the .beacon/ tree directly. Cloud-mode export
# fetches via the API so the snapshot reflects authoritative state, not the
# read-only local cache (CORE doc: cloud project.json is a cache). Both
# write the same ZIP layout so a round-trip is possible.
#
# ZIP layout (schema_version=1):
#   manifest.json   — required, describes source / version / entry counts
#   project.json    — authoritative project state
#   documents/<doc_id>.md (zero or more)
#   changelog.jsonl (optional)
#   retro/<file>.md (zero or more)
#   config.json     (optional, project-level)
#
# Import is local-mode-only in this iteration: extract → write to a fresh
# .beacon/ directory. Cloud import (push to API) is a follow-up.

_BACKUP_SCHEMA_VERSION = 1


def cmd_project_export():
    """Pack the current project into a backup ZIP (ms-14 e-828).

    Env-driven CLI surface (matches the rest of the dispatcher):
      BEACON_OUTPUT   path to the ZIP to write (required)
      BEACON_BACKUP   "1" to flag this as a full backup snapshot
                     (currently the only mode; accepted for forward-compat
                     with --backup CLI flag)
      BEACON_JSON     "1" to emit a JSON receipt instead of human text
    """
    import zipfile
    output = os.environ.get("BEACON_OUTPUT", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    if not output:
        print("Error: --output <path> is required.", file=sys.stderr)
        print(
            "  Example: beacon project export --backup -o ~/Backups/myproj.zip",
            file=sys.stderr,
        )
        sys.exit(1)

    # Refuse to silently overwrite an existing file — backups should be
    # append-only from the operator's POV.
    if os.path.exists(output):
        print(
            f"Error: output already exists: {output}\n"
            "  Choose a different path or remove the file first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Collect contents based on mode.
    if _is_cloud_mode():
        snapshot = _collect_export_cloud()
    else:
        snapshot = _collect_export_local()

    # Build manifest (entry_counts gives a quick integrity hash without
    # parsing the whole project).
    project_data = snapshot["project"]
    ms_list = project_data.get("milestones", []) or []
    op_list = project_data.get("operations", []) or []

    def _count_entries(ms):
        total = 0
        for e in ms.get("entries", []) or []:
            total += 1
            for nested in e.get("entries", []) or []:
                total += 1
        return total

    entry_counts = {
        "milestones": len(ms_list),
        "operations": len(op_list),
        "top_level_entries": sum(_count_entries(ms) for ms in ms_list),
        "documents": len(snapshot["documents"]),
        "changelog_lines": snapshot["changelog_lines"],
        "retro_files": len(snapshot["retros"]),
    }

    import datetime
    manifest = {
        "schema_version": _BACKUP_SCHEMA_VERSION,
        "export_ts": datetime.datetime.now().isoformat(),
        "project_name": project_data.get("name", ""),
        "project_id": snapshot.get("project_id", ""),
        "beacon_version": __version__,
        "source_mode": "cloud" if get_store().is_cloud() else "local",
        "entry_counts": entry_counts,
    }

    # Stream into ZIP. zipfile is in stdlib so no extra deps; DEFLATED
    # keeps the size small for text-heavy beacon dumps.
    written_paths: list[str] = []
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            written_paths.append("manifest.json")

            zf.writestr(
                "project.json",
                json.dumps(project_data, ensure_ascii=False, indent=2) + "\n",
            )
            written_paths.append("project.json")

            for doc_id, doc_body in snapshot["documents"].items():
                # doc_id is a Firestore-style ID or local slug; both are
                # filesystem-safe (alphanumeric / hyphen).
                zf.writestr(f"documents/{doc_id}.md", doc_body)
                written_paths.append(f"documents/{doc_id}.md")

            if snapshot["changelog"] is not None:
                zf.writestr("changelog.jsonl", snapshot["changelog"])
                written_paths.append("changelog.jsonl")

            for retro_name, retro_body in snapshot["retros"].items():
                zf.writestr(f"retro/{retro_name}", retro_body)
                written_paths.append(f"retro/{retro_name}")

            if snapshot["config"] is not None:
                zf.writestr("config.json", snapshot["config"])
                written_paths.append("config.json")
    except (OSError, zipfile.BadZipFile) as e:
        print(f"Error writing ZIP: {e}", file=sys.stderr)
        # Best-effort cleanup so a half-written backup isn't mistaken for a
        # complete one.
        if os.path.exists(output):
            try:
                os.remove(output)
            except OSError:
                pass
        sys.exit(1)

    size_bytes = os.path.getsize(output)
    if json_mode:
        print(json.dumps({
            "output": output,
            "size_bytes": size_bytes,
            "manifest": manifest,
            "entries_written": len(written_paths),
        }, ensure_ascii=False))
    else:
        print(f"Exported [{manifest['project_name']}] -> {output}")
        print(f"  size: {size_bytes} bytes ({len(written_paths)} files)")
        print(f"  milestones: {entry_counts['milestones']}, "
              f"docs: {entry_counts['documents']}, "
              f"changelog: {entry_counts['changelog_lines']} lines")


def _collect_export_local() -> dict:
    """Gather export contents from the local .beacon/ tree."""
    data = load_project()
    project_dir = os.path.dirname(get_project_file())

    documents: dict[str, str] = {}
    docs_dir = os.path.join(project_dir, "documents")
    if os.path.isdir(docs_dir):
        for fname in sorted(os.listdir(docs_dir)):
            if not fname.endswith(".md"):
                continue
            try:
                with open(os.path.join(docs_dir, fname), "r", encoding="utf-8") as f:
                    documents[fname[:-3]] = f.read()
            except (OSError, UnicodeDecodeError):
                # Skip unreadable docs (cp932 cruft, etc.) — don't fail the
                # whole export. The manifest count reflects what we got.
                continue

    changelog: Optional[str] = None
    changelog_lines = 0
    changelog_path = os.path.join(project_dir, "changelog.jsonl")
    if os.path.isfile(changelog_path):
        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                changelog = f.read()
            changelog_lines = sum(
                1 for line in changelog.splitlines() if line.strip()
            )
        except (OSError, UnicodeDecodeError):
            changelog = None

    retros: dict[str, str] = {}
    retro_dir = os.path.join(project_dir, "retro")
    if os.path.isdir(retro_dir):
        for fname in sorted(os.listdir(retro_dir)):
            if fname.startswith("."):
                continue
            fpath = os.path.join(retro_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    retros[fname] = f.read()
            except (OSError, UnicodeDecodeError):
                continue

    config_body: Optional[str] = None
    config_path = os.path.join(project_dir, "config.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_body = f.read()
        except (OSError, UnicodeDecodeError):
            config_body = None

    return {
        "project": data,
        "project_id": data.get("id", "") or data.get("name", ""),
        "documents": documents,
        "changelog": changelog,
        "changelog_lines": changelog_lines,
        "retros": retros,
        "config": config_body,
    }


def _collect_export_cloud() -> dict:
    """Gather export contents via the Beacon API (authoritative)."""
    client, config = _get_api_client()
    project_id = config["project_id"]

    project_data = client.get_project(project_id)

    documents: dict[str, str] = {}
    try:
        doc_index = client.list_documents(project_id)
    except Exception:
        doc_index = []
    for entry in doc_index:
        doc_id = entry.get("doc_id") or entry.get("id") or ""
        if not doc_id:
            continue
        try:
            full = client.get_document(project_id, doc_id)
        except Exception:
            continue
        # Reconstruct the .md body with frontmatter the same way
        # `beacon doc show` does, so a local import sees identical files.
        body = _reconstruct_doc_markdown(full)
        documents[doc_id] = body

    # Changelog and retros are not yet exposed by the API (e-825 covers
    # changelog persistence). Snapshot what we can from the local cache
    # if present — better than nothing for now, and the manifest records
    # source_mode=cloud so future imports can recognize the gap.
    project_dir = os.path.dirname(get_project_file())
    changelog: Optional[str] = None
    changelog_lines = 0
    changelog_path = os.path.join(project_dir, "changelog.jsonl")
    if os.path.isfile(changelog_path):
        try:
            with open(changelog_path, "r", encoding="utf-8") as f:
                changelog = f.read()
            changelog_lines = sum(
                1 for line in changelog.splitlines() if line.strip()
            )
        except (OSError, UnicodeDecodeError):
            changelog = None

    retros: dict[str, str] = {}
    retro_dir = os.path.join(project_dir, "retro")
    if os.path.isdir(retro_dir):
        for fname in sorted(os.listdir(retro_dir)):
            if fname.startswith("."):
                continue
            fpath = os.path.join(retro_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    retros[fname] = f.read()
            except (OSError, UnicodeDecodeError):
                continue

    return {
        "project": project_data,
        "project_id": project_id,
        "documents": documents,
        "changelog": changelog,
        "changelog_lines": changelog_lines,
        "retros": retros,
        "config": None,
    }


def _reconstruct_doc_markdown(full: dict) -> str:
    """Rebuild the on-disk .md body (frontmatter + content) for an API doc."""
    frontmatter_keys = ("scope", "milestone", "operation", "title")
    fm_lines = ["---"]
    for k in frontmatter_keys:
        v = full.get(k)
        if v not in (None, ""):
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    content = full.get("content", "")
    if content and not content.startswith("\n"):
        content = "\n" + content
    return "\n".join(fm_lines) + content


def cmd_project_import():
    """Extract a backup ZIP into a target .beacon/ directory (local mode).

    Env-driven CLI surface:
      BEACON_INPUT    path to the ZIP to read (required)
      BEACON_TARGET   directory to create / write into (required).
                     The .beacon/ subdir is created here; refusal if it
                     already exists with content unless --force.
      BEACON_FORCE    "1" to allow overwriting an existing .beacon/
                     (destructive — operator confirmation surface).
      BEACON_JSON     "1" to emit a JSON receipt.
    """
    import zipfile
    inp = os.environ.get("BEACON_INPUT", "").strip()
    target = os.environ.get("BEACON_TARGET", "").strip()
    force = os.environ.get("BEACON_FORCE", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not inp or not target:
        print(
            "Error: --input <zip> and --target <dir> are both required.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not os.path.isfile(inp):
        print(f"Error: backup not found: {inp}", file=sys.stderr)
        sys.exit(1)

    target = os.path.abspath(target)
    target_beacon = os.path.join(target, ".beacon")
    if os.path.exists(target_beacon) and not force:
        print(
            f"Error: {target_beacon} already exists.\n"
            "  Use --force to overwrite (destructive).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with zipfile.ZipFile(inp, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names or "project.json" not in names:
                print(
                    "Error: not a valid backup ZIP "
                    "(missing manifest.json or project.json).",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                print(f"Error: malformed manifest.json: {e}", file=sys.stderr)
                sys.exit(1)
            if manifest.get("schema_version") != _BACKUP_SCHEMA_VERSION:
                print(
                    f"Error: backup schema_version={manifest.get('schema_version')} "
                    f"is not supported by this beacon (expects {_BACKUP_SCHEMA_VERSION}).",
                    file=sys.stderr,
                )
                sys.exit(1)

            os.makedirs(target_beacon, exist_ok=True)
            written = 0
            for name in names:
                if name == "manifest.json":
                    continue  # don't litter the destination; we keep it
                              # alongside as .manifest.json for trace
                # Refuse any path attempting to escape the target via
                # absolute paths or ".." segments.
                norm = os.path.normpath(name)
                if norm.startswith(("..", "/")) or os.path.isabs(norm):
                    print(f"Error: refusing unsafe path in backup: {name}",
                          file=sys.stderr)
                    sys.exit(1)
                dest = os.path.join(target_beacon, norm)
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as out:
                    out.write(src.read())
                written += 1

            # Drop the manifest beside the data as a trace marker —
            # future operators / tooling can see how the dir was created.
            manifest_marker = os.path.join(target_beacon, ".import_manifest.json")
            with open(manifest_marker, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

    except zipfile.BadZipFile as e:
        print(f"Error: corrupt backup ZIP: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error writing target: {e}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps({
            "target": target_beacon,
            "files_written": written,
            "manifest": manifest,
        }, ensure_ascii=False))
    else:
        print(f"Imported [{manifest.get('project_name', '?')}] -> {target_beacon}")
        print(f"  files: {written}, exported at {manifest.get('export_ts', '?')}")
        print(f"  source: {manifest.get('source_mode', '?')}, "
              f"beacon {manifest.get('beacon_version', '?')}")


def cmd_cycle_status():
    """Emit a per-cycle activation snapshot for the current project.

    Output (JSON mode is default for AI consumers):

      {
        "push":     {"active": true,  "last_action_date": "2026-05-10T00:00:00Z"},
        "deploy":   {"active": false, "last_action_date": null},
        "retro":    {"active": false, "last_action_date": null},
        "operation":{"active": true,  "last_action_date": "..."},
        "release":  {"active": false, "last_action_date": null}
      }

    Skill consumers (/beacon-log Step 7, /beacon-push Step 2.5, etc.) call
    this once and branch their rhythm-suggestion logic on the result. Centra-
    lizing it here means every Skill agrees on activation semantics — see
    CORE doc `Cdg2zJtrOajm1q8adMa1` and SPEC `LqXEEbgsH712Z78KBELP`.

    Text mode is for human inspection and not load-bearing.
    """
    import cycle as cycle_mod  # local import: optional dependency outside of Skill use

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    # ms-84 Phase 2: Store.list_documents() で local / cloud を統一。
    # 失敗時は空 list に degrade する best-effort 契約 (= push / deploy /
    # operation 系の cycle 判定は document に依存しないので、 ここで空でも
    # snapshot は正しく作れる)。
    try:
        documents = get_store().list_documents() or []
    except Exception:
        documents = []

    snapshot = cycle_mod.cycle_status_snapshot(data, documents=documents)

    if json_mode:
        print(json.dumps(snapshot, ensure_ascii=False))
        return

    # Human-readable mode. Used for spot-checking during development; Skills
    # always use --json.
    print("Cycle status:")
    for c, info in snapshot.items():
        flag = "active" if info["active"] else "inactive"
        last = info["last_action_date"] or "—"
        print(f"  {c:<10s} {flag:<10s} last: {last}")


def cmd_search():
    """Unified search across all Beacon entities (CORE doc 検索基盤の原則 / SPEC 3ne57ccZegYQXDQA03op).

    Delegates the actual work to ``lib/search.search_project`` for the
    canonical (= pre-ms-79) path, or to ``lib/retro_query.retro_query``
    when any ms-79 extension flag is present (source / actor / claim /
    include_*). retro_query wraps search_project and adds the post-filters
    plus extension entity merges (= bus archive / session_logs / Trek);
    see SPEC ms-79 §3 ‘設計の柱 6’ (基盤統合).
    """
    import search as _search  # noqa: PLC0415

    query = os.environ.get("BEACON_QUERY", "")
    ms_filter = os.environ.get("BEACON_MS_ID", "")
    op_filter = os.environ.get("BEACON_OPERATION_ID", "")
    id_filter = os.environ.get("BEACON_ENTRY_ID", "")
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    assignee = os.environ.get("BEACON_ASSIGNEE", "")
    owner = os.environ.get("BEACON_OWNER", "")
    from_date = os.environ.get("BEACON_FROM", "")
    to_date = os.environ.get("BEACON_TO", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    # ms-79 extension knobs (= /beacon-retrospect Skill が opt-in で渡す).
    # 空のときは search_project の旧経路で動かして back-compat を保つ。
    source_list_raw = os.environ.get("BEACON_SOURCE", "").strip()
    actor_filter = os.environ.get("BEACON_ACTOR", "").strip()
    claimant_filter = os.environ.get("BEACON_CLAIMANT", "").strip()
    include_bus_dm = os.environ.get("BEACON_INCLUDE_BUS_DM", "") == "1"
    include_session_logs = os.environ.get("BEACON_INCLUDE_SESSION_LOGS", "") == "1"
    include_trek = os.environ.get("BEACON_INCLUDE_TREK", "") == "1"

    def _parse_list(env_key: str) -> list[str]:
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    type_list = _parse_list("BEACON_TYPE")
    status_list = _parse_list("BEACON_STATUS")
    priority_list = _parse_list("BEACON_PRIORITY")

    try:
        limit = int(os.environ.get("BEACON_LIMIT", "50"))
    except ValueError:
        limit = 50
    try:
        offset = int(os.environ.get("BEACON_OFFSET", "0"))
    except ValueError:
        offset = 0

    # Without any filters the search defaults to "give me the 50 most-recent
    # entities". This matches the SPEC: "all params omitted = ダッシュボード
    # 最近の動き相当". So an empty query is no longer an error.

    data = load_project()
    documents = _load_local_documents()

    source_list = [s.strip() for s in source_list_raw.split(",") if s.strip()] if source_list_raw else None
    use_retro_query = bool(
        source_list or actor_filter or claimant_filter
        or include_bus_dm or include_session_logs or include_trek
    )

    if use_retro_query:
        import retro_query as _rq  # noqa: PLC0415
        session_logs = _load_session_logs() if include_session_logs else None
        bus_archive = _load_bus_archive() if include_bus_dm else None
        trek_summaries = _load_trek_summaries() if include_trek else None
        result = _rq.retro_query(
            data,
            documents,
            q=query,
            type=type_list or None,
            status=status_list or None,
            priority=priority_list or None,
            scope=scope_filter,
            ms=ms_filter,
            op=op_filter,
            id=id_filter,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
            source=source_list,
            actor=actor_filter,
            claimant=claimant_filter,
            include_bus_dm=include_bus_dm,
            include_session_logs=include_session_logs,
            include_trek=include_trek,
            session_logs=session_logs,
            bus_archive=bus_archive,
            trek_summaries=trek_summaries,
        )
    else:
        result = _search.search_project(
            data,
            documents,
            q=query,
            type=type_list or None,
            status=status_list or None,
            priority=priority_list or None,
            scope=scope_filter,
            ms=ms_filter,
            op=op_filter,
            id=id_filter,
            assignee=assignee,
            owner=owner,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
        return

    results = result["results"]
    total = result["total"]

    if not results:
        if query:
            print(f"No results for: {query}")
        else:
            print("No results.")
        return

    type_icons = {
        "task": "□", "commit": "○", "pr": "PR", "milestone": "MS",
        "save": "→", "document": "📄", "push": "↑", "deploy": "▲",
        "operation": "⚙", "operation_task": "·", "run": "▷", "incident": "⚠",
    }
    header = f"{total} result(s)"
    if query:
        header += f" for: {query}"
    if total > len(results):
        header += f"  (showing {len(results)} from offset {result['offset']})"
    print(header)
    for r in results:
        icon = type_icons.get(r.get("entity_type", ""), "?")
        status_note = f" [{r['status']}]" if r.get("status") else ""
        date_part = (r.get("created_at") or "")[:10]
        title = r.get("title") or r.get("snippet", "")[:80]
        print(f"  {icon} [{r['id']}] {title[:80]}{status_note}")
        scope_or_ms = r.get("scope") or r.get("ms_id") or r.get("op_id") or ""
        if scope_or_ms or date_part:
            print(f"       └─ {scope_or_ms}  {date_part}")
        if r.get("snippet") and r["snippet"] != title:
            snip = r["snippet"][:120]
            print(f"       └─ {snip}")


def _load_local_documents() -> list[dict]:
    """Read all .beacon/documents/*.md files and return them as dicts with
    frontmatter fields (title, scope, milestone, operation, content, etc.)
    promoted to top level. Used by cmd_search for local-mode document search."""
    docs: list[dict] = []
    project_file = get_project_file()
    docs_dir = os.path.join(os.path.dirname(project_file), "documents")
    if not os.path.isdir(docs_dir):
        return docs
    for fname in os.listdir(docs_dir):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(docs_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        # Parse YAML-ish frontmatter (--- delimited)
        meta: dict[str, str] = {}
        content = raw
        if raw.startswith("---\n"):
            try:
                _, fm, body = raw.split("---\n", 2)
                for line in fm.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip().strip('"').strip("'")
                content = body
            except ValueError:
                pass
        doc_id = meta.get("doc_id") or fname[:-3]
        docs.append({
            "doc_id": doc_id,
            "title": meta.get("title", fname[:-3]),
            "scope": meta.get("scope", "memo"),
            "milestone": meta.get("milestone", ""),
            "operation": meta.get("operation", ""),
            "content": content,
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        })
    return docs


def _load_session_logs() -> list[dict]:
    """Return session_log entries for the active project (ms-79 / e-1835).

    Cloud mode: pulls ``/api/projects/{id}/session_logs`` (= the
    aggregated session summaries written by ``beacon session end`` and
    ``beacon session rescue``).

    Local mode: walks ``.beacon/session_logs/*.json`` if present and
    returns each as a dict. The local layout is the same shape the cloud
    endpoint serves, so retro_query needs no mode-aware logic.

    Returns ``[]`` on any failure — retro_query treats this as "no
    session log source available" and silently skips the merge. This
    keeps /beacon-retrospect usable on installs that never enabled the
    session_log subcollection.
    """
    try:
        # ms-84 Phase 2: Store.list_session_logs() で cloud / local 二経路を
        # 単一の呼び出しに寄せる。 StoreApi 側が transport 失敗を [] に丸める
        # best-effort 契約を持つので、 cloud auth glitch も同じ try/except で
        # 拾える。 LocalStore は no-op で [] を返す設計 (= Protocol docstring
        # 参照) のため、 local モード時は下の directory walk fallback が拾う。
        rows = get_store().list_session_logs() or []
        if isinstance(rows, dict):
            rows = rows.get("session_logs") or rows.get("items") or []
        if isinstance(rows, list) and rows:
            return rows
        # Local mode (or empty cloud): walk .beacon/session_logs/*.json
        project_dir = os.path.dirname(get_project_file())
        sl_dir = os.path.join(project_dir, "session_logs")
        if not os.path.isdir(sl_dir):
            return []
        out: list[dict] = []
        for fname in os.listdir(sl_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(sl_dir, fname), "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict):
                    out.append(rec)
            except (OSError, json.JSONDecodeError):
                continue
        return out
    except Exception:
        return []


def _load_bus_archive() -> list[dict]:
    """Return DM / bus event archive entries (ms-79 / e-1832 / UC10-F1).

    Reads ``.beacon/bus_archive/*.json`` if the install has captured
    DM history (= ms-54 envelope archive). Returns ``[]`` if nothing is
    persisted — retro_query then silently skips the merge.

    Per SPEC ms-79 §8 ‘やらないこと’: we do not change the archive
    schema, we only **read** it. Whatever shape ms-54 writes is what
    we surface, with the renderer in lib/retro_query tolerating missing
    fields.
    """
    try:
        project_dir = os.path.dirname(get_project_file())
        ba_dir = os.path.join(project_dir, "bus_archive")
        if not os.path.isdir(ba_dir):
            return []
        out: list[dict] = []
        for fname in os.listdir(ba_dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(ba_dir, fname), "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict):
                    out.append(rec)
                elif isinstance(rec, list):
                    # support an archive-as-list layout if any install uses it
                    out.extend(r for r in rec if isinstance(r, dict))
            except (OSError, json.JSONDecodeError):
                continue
        return out
    except Exception:
        return []


def _load_trek_summaries() -> list[dict]:
    """Return Trek summary entries (ms-79 / e-1835 / UC10-F4).

    Cloud mode: walks each trek's summaries via the API client (= ms-69
    Trek records carry their own summary list).

    Local mode: walks ``.beacon/treks/*/summaries/*.json`` if present.

    Returns ``[]`` on any failure — retro_query handles the absence
    gracefully.
    """
    out: list[dict] = []
    try:
        # Local mode walk (also useful as a cache when cloud sync is on).
        project_dir = os.path.dirname(get_project_file())
        treks_dir = os.path.join(project_dir, "treks")
        if os.path.isdir(treks_dir):
            for trek_name in os.listdir(treks_dir):
                summaries_dir = os.path.join(treks_dir, trek_name, "summaries")
                if not os.path.isdir(summaries_dir):
                    continue
                for fname in os.listdir(summaries_dir):
                    if not fname.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(summaries_dir, fname),
                                  "r", encoding="utf-8") as f:
                            rec = json.load(f)
                        if isinstance(rec, dict):
                            rec.setdefault("trek_id", trek_name)
                            out.append(rec)
                    except (OSError, json.JSONDecodeError):
                        continue
    except Exception:
        pass
    return out


def _find_all_on_path(name: str) -> list[str]:
    """Return every executable named ``name`` on PATH (across all PATH dirs).

    Unlike ``shutil.which`` which returns only the first hit, this walks
    every PATH directory and collects all matches. On Windows it also
    consults ``PATHEXT`` to find ``.exe`` / ``.cmd`` / ``.bat`` variants.

    Used by ``cmd_doctor`` to surface install-location shadowing (ms-44
    e-1170): e.g. an old ``~/.local/bin/beacon`` 0.11.1 silently shadowing
    a newer ``AppData/.../Scripts/beacon.exe`` 0.19.0.
    """
    path_env = os.environ.get("PATH", "")
    if not path_env:
        return []
    dirs = path_env.split(os.pathsep)
    if os.name == "nt":
        pathext_raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        exts = [""] + [e.lower() for e in pathext_raw.split(os.pathsep) if e]
    else:
        exts = [""]
    found: list[str] = []
    seen: set[str] = set()
    for d in dirs:
        d = d.strip()
        if not d:
            continue
        for ext in exts:
            cand = os.path.join(d, name + ext)
            if cand in seen:
                continue
            seen.add(cand)
            try:
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    found.append(cand)
                    break  # one hit per directory is enough
            except OSError:
                continue
    return found


def _shell_dispatched_subcommands():
    """Parse ``bin/beacon`` for top-level and 1-level-nested case branches.

    Returns set of word-tuples of subcommand labels. Some subcommands
    (e.g., ``status`` / ``log`` / ``milestone add``) are dispatched
    shell-side in ``bin/beacon`` rather than via the Python ``cmd_*``
    table, so the doctor drift check has to read both surfaces.

    State machine: track the current 4-space subgroup label, then group
    12-space sub-cases under it. ``milestone|ms)`` adds both forms.
    """
    beacon_bin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin", "beacon"
    )
    if not os.path.isfile(beacon_bin):
        return set()
    import re as _re
    top_pat = _re.compile(r"^    ([a-z][a-zA-Z0-9|_-]*)\)")
    sub_pat = _re.compile(r"^            ([a-z][a-zA-Z0-9|_-]*)\)")
    cases = set()
    cur_top_alts = []
    try:
        with open(beacon_bin, "r", encoding="utf-8") as fh:
            for line in fh:
                m = top_pat.match(line)
                if m:
                    alts = m.group(1).split("|")
                    for alt in alts:
                        cases.add((alt,))
                    cur_top_alts = alts
                    continue
                m = sub_pat.match(line)
                if m and cur_top_alts:
                    label = m.group(1)
                    for sub_alt in label.split("|"):
                        for top_alt in cur_top_alts:
                            cases.add((top_alt, sub_alt))
    except OSError:
        return set()
    return cases


def _canonical_subcommand_set():
    """Word-tuples for every known `beacon <subcmd>` invocation.

    Combines three sources for full coverage:
    - ``cmd_*`` functions in this module (Python dispatch table)
    - Top-level and nested case branches in ``bin/beacon`` (shell dispatch
      — ``status`` / ``log`` etc. are not ``cmd_*`` but plain shell)
    - Hardcoded entries for lambda-mapped dispatch keys
      (``version`` / ``auth_login`` / ``auth_logout`` / ``auth_status``)

    Adding a new ``cmd_foo`` function or a new ``foo)`` case in
    ``bin/beacon`` automatically makes ``beacon foo`` valid for the Skill
    drift check — no manual list update required. This is the ms-61 /
    e-1570 single-source-of-truth principle.
    """
    keys = set()
    for name, obj in list(globals().items()):
        if name.startswith("cmd_") and callable(obj):
            keys.add(name[4:])  # strip "cmd_"
    keys.update(["version", "auth_login", "auth_logout", "auth_status"])
    result = {tuple(k.split("_")) for k in keys}
    result.update(_shell_dispatched_subcommands())
    return result


def _doctor_check_skill_cli_drift(home):
    """Detect Skill markdown that references unknown beacon subcommands.

    Walks ``~/.claude/skills/<name>/*.md`` and extracts every ``beacon X
    [Y [Z]]`` invocation (regex: leading lowercase words after the literal
    "beacon"). For each, longest-prefix-matches the word sequence against
    the canonical set. Unmatched leads -> drift.

    Catches the class of bug behind e-1361 / e-1362 / the retired
    ``beacon summary`` write path: CLI evolved but dependent Skill markdown
    still calls the old command name.

    Returns a list of warning strings (one per drifting Skill). Empty list
    means no drift or no skills directory installed.
    """
    import re as _re
    skills_dir = os.path.join(home, ".claude", "skills")
    if not os.path.isdir(skills_dir):
        return []  # CI / new user — silent skip per ms-61 e-1570 AC#5

    canonical = _canonical_subcommand_set()
    # Match `beacon` followed by 1+ consecutive lowercase command words.
    # Stops at flags (-foo), placeholders (<id>), pipes, uppercase, digits.
    pattern = _re.compile(
        r"\bbeacon\s+([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)*)"
    )

    drift_by_skill = {}  # skill_name -> list[(lineno, phrase)]
    for skill_name in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, skill_name)
        if not os.path.isdir(skill_dir):
            continue
        for fn in sorted(os.listdir(skill_dir)):
            if not fn.endswith(".md"):
                continue
            fpath = os.path.join(skill_dir, fn)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            seen_in_file = set()  # dedupe per file
            in_fence = False  # fenced code block state across lines
            for lineno, line in enumerate(lines, start=1):
                # Toggle fenced state on ``` or ~~~ delimiter lines.
                stripped = line.lstrip()
                if stripped.startswith("```") or stripped.startswith("~~~"):
                    in_fence = not in_fence
                    continue
                for m in pattern.finditer(line):
                    # Require command context: in a fenced code block,
                    # or wrapped in inline backticks. Prose like
                    # "beacon repo root" is then ignored.
                    if not in_fence:
                        prefix = line[:m.start()]
                        if prefix.count("`") % 2 == 0:
                            continue
                    raw_words = m.group(1).split()
                    # Normalize: hyphenated commands act as underscored
                    # (e.g., "workspace-cleanup" -> ["workspace", "cleanup"]).
                    # cmd_milestone_workspace_cleanup -> ("milestone",
                    # "workspace", "cleanup") in canonical, so the Skill
                    # form "beacon milestone workspace-cleanup" must
                    # normalize to the same tuple.
                    words = []
                    for w in raw_words:
                        words.extend(w.split("-"))
                    matched = False
                    for i in range(len(words), 0, -1):
                        if tuple(words[:i]) in canonical:
                            matched = True
                            break
                    if matched:
                        continue
                    phrase = f"beacon {m.group(1)}"
                    key = (phrase,)
                    if key in seen_in_file:
                        continue
                    seen_in_file.add(key)
                    drift_by_skill.setdefault(skill_name, []).append(
                        (lineno, phrase)
                    )

    if not drift_by_skill:
        return []

    warnings = []
    for skill_name, drifts in sorted(drift_by_skill.items()):
        sample = drifts[:3]
        more = len(drifts) - len(sample)
        sample_lines = "\n".join(
            f"       L{ln}: {phrase}" for ln, phrase in sample
        )
        more_line = f"\n       (+{more} more unique reference(s))" if more > 0 else ""
        warnings.append(
            f"WARN [skill-cli-drift] Skill `{skill_name}` references unknown beacon command(s):\n"
            f"{sample_lines}{more_line}\n"
            "       The leading word(s) after `beacon` don't match any known\n"
            "       subcommand (cmd_* function or lambda dispatch entry).\n"
            "       Either the CLI dropped that command, or the Skill markdown\n"
            "       has a typo / out-of-date example. Opt-out: set\n"
            "       BEACON_DOCTOR_SKIP_SKILL_DRIFT=1."
        )
    return warnings


def _doctor_check_project_staleness():
    """Detect when local .beacon/project.json is out of sync with cloud.

    Compares local milestone / operation counts against the cloud copy.
    Catches the ms-61 P3 case observed in fork worktrees on 2026-06-12:
    a session sees ms-1..ms-22 locally while cloud already holds ms-43
    onwards because ``beacon cloud pull`` was never run from this cwd.
    The CLI surface ``beacon status`` happily returned the stale local
    snapshot, hiding the cloud's authoritative state.

    Cloud project has no top-level ``updated_at`` field on the response,
    so we compare counts (= a proxy that captures the common drift case:
    another session / Web UI added milestones or operations). Same counts
    but altered state (= same N items, but renames / status changes)
    are not detected by this check; the milestone count drift is the
    high-value attack surface.

    Returns a list of warning strings. Empty list = in sync, or skipped.

    Skip cases:
    - .beacon/project.json absent (= no project in cwd)
    - .beacon/cloud.json absent (= local mode)
    - Local mtime < 5 min (= obviously fresh, no network call needed)
    - BEACON_DOCTOR_SKIP_CLOUD_SYNC=1
    - Network / auth error fetching cloud (best-effort; doctor must
      stay usable offline).
    """
    if os.environ.get("BEACON_DOCTOR_SKIP_CLOUD_SYNC") == "1":
        return []

    proj_path = os.path.join(".beacon", "project.json")
    cloud_cfg = os.path.join(".beacon", "cloud.json")
    if not os.path.exists(proj_path) or not os.path.exists(cloud_cfg):
        return []  # not a cloud-mode beacon project from this cwd

    import time as _time
    try:
        mtime = os.stat(proj_path).st_mtime
    except OSError:
        return []
    age_sec = _time.time() - mtime

    # AC#3 honor: threshold for "very old" warn even when counts match.
    threshold_sec = int(os.environ.get("BEACON_DOCTOR_STALENESS_SEC", "1800"))

    # Avoid network call when local activity is recent (= obviously fresh).
    # 300s = 5 min, far below threshold_sec default 30 min.
    if age_sec < 300:
        return []

    try:
        client, config = _get_api_client()
        cloud_data = client.get_project(config["project_id"])
    except Exception:
        return []  # offline / auth issue — doctor stays usable

    try:
        with open(proj_path, "r", encoding="utf-8") as f:
            local_data = json.load(f)
    except Exception:
        return []

    cloud_ms = len(cloud_data.get("milestones", []))
    local_ms = len(local_data.get("milestones", []))
    cloud_op = len(cloud_data.get("operations", []))
    local_op = len(local_data.get("operations", []))

    diffs = []
    if cloud_ms != local_ms:
        diffs.append(f"milestones: local={local_ms}, cloud={cloud_ms}")
    if cloud_op != local_op:
        diffs.append(f"operations: local={local_op}, cloud={cloud_op}")

    if diffs:
        return [
            "WARN [project-stale] .beacon/project.json differs from cloud:\n"
            + "\n".join(f"       {d}" for d in diffs)
            + f"\n       (local mtime: {int(age_sec / 60)} min ago)\n"
            "       Run: beacon cloud pull   (to refresh local cache)\n"
            "       Opt-out: BEACON_DOCTOR_SKIP_CLOUD_SYNC=1"
        ]

    # Counts match but file is very old — soft warn so external writes
    # (another session, Web UI) are noticed even when counts coincide.
    if age_sec > threshold_sec:
        return [
            f"WARN [project-stale] .beacon/project.json was last touched "
            f"{int(age_sec / 60)} minutes ago (threshold {int(threshold_sec / 60)} min).\n"
            "       Counts match cloud, but external state changes\n"
            "       (renames / status shifts) may have happened. Run\n"
            "       `beacon cloud pull` if uncertain.\n"
            "       Threshold override: BEACON_DOCTOR_STALENESS_SEC=<seconds>"
        ]

    return []


def _doctor_check_claude_md_principle_marker():
    """Detect when CLAUDE.md lacks the entry-writing-principle marker.

    ms-68 / e-1640: the principle (= `entry-writing-principle` CORE doc) must
    live in CLAUDE.md so every session / every tool call has it in context.
    If the marker `<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->` is missing, the
    section was either never installed (legacy projects predating ms-68) or
    silently dropped by a manual edit. Warning-level only — never block.

    Returns a list of warning strings. Empty list = OK, or skip (no CLAUDE.md
    in cwd, which is not a beacon-managed project).
    """
    claude_md = "CLAUDE.md"
    marker = "<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->"

    # No CLAUDE.md = not a beacon-managed project from this cwd, skip silently.
    if not os.path.exists(claude_md):
        return []

    try:
        with open(claude_md, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []

    if marker in content:
        return []

    return [
        "WARN [entry-writing-principle] CLAUDE.md lacks the entry writing principle section.\n"
        f"       Missing marker: {marker}\n"
        "       Beacon's task/spec/doc readers include non-developers — the principle\n"
        "       (reader-first 1-line / loanword tiers / ID context / no truncated sentences)\n"
        "       must live in CLAUDE.md so it is in context for every session.\n"
        "       Run: beacon common-setup   (re-installs CLAUDE.md beacon section)"
    ]


def _doctor_check_ms81_state_machine():
    """ms-81 e-1921: surface the four state-machine warnings named in SPEC AC #9.

    Each is warning-level only; the state machine is a forcing function,
    not enforcement. Returns a list of warning strings.

    Checks:
      A. active or observing MS missing assignee — work is happening but
         nobody's name is on it; audit trail loses provenance.
      B. done MS still has a worktree directory at `.worktrees/<branch>/`
         — another session could check it out and accidentally commit
         against a closed branch.
      C. occupation field present but `session_id` empty — a half-written
         claim that nothing can release; usually a sign of a crash that
         left the field stale.
      D. waiting MS has commits/PRs being attached after it was paused —
         we approximate this by checking whether the most recent commit
         entry's `created_at` is *after* `meta.waiting_at`.
    """
    warnings: list[str] = []
    try:
        data = load_project()
    except Exception:
        return warnings

    try:
        import branch as _branch
    except Exception:
        _branch = None

    cwd_root = os.getcwd()
    a_hits: list[str] = []
    b_hits: list[str] = []
    c_hits: list[str] = []
    d_hits: list[str] = []

    for ms in data.get("milestones", []):
        ms_id = ms.get("id", "")
        title = ms.get("title", "")
        status = ms.get("status", "")
        # A: active / observing without assignee
        if status in ("in_progress", "active", "observing"):
            assignee = ms.get("assignee", "")
            if not assignee or (
                isinstance(assignee, list) and not any(a.strip() for a in assignee)
            ):
                a_hits.append(f"{ms_id} ({status}, {title[:60]})")
        # B: done MS with leftover worktree
        if status == "done" and _branch is not None:
            try:
                branch_name = _branch.ms_branch_name(ms_id, title)
                if os.path.exists(os.path.join(cwd_root, ".worktrees", branch_name)):
                    b_hits.append(f"{ms_id} (worktree: .worktrees/{branch_name})")
            except Exception:
                pass
        # C: occupation field present but stale shape (no session_id)
        occ = ms.get("occupation")
        if occ and not occ.get("session_id"):
            c_hits.append(f"{ms_id} (occupation present without session_id)")
        # D: waiting MS with commits attached after the wait transition
        if status == "waiting":
            waiting_at = ms.get("meta", {}).get("waiting_at", "")
            if waiting_at:
                for e in ms.get("entries", []):
                    if e.get("type") in ("commit", "pr"):
                        created = e.get("created_at", "") or e.get("date", "")
                        if created and created > waiting_at:
                            d_hits.append(
                                f"{ms_id} ({title[:50]}) — {e.get('type')} "
                                f"[{e.get('id')}] attached after waiting_at"
                            )
                            break

    def _fmt(label, code, hits, suggestion):
        if not hits:
            return None
        return (
            f"WARN [{code}] {label}:\n"
            + "\n".join(f"       - {h}" for h in hits[:8])
            + (f"\n       (+{len(hits) - 8} more)" if len(hits) > 8 else "")
            + f"\n       {suggestion}"
        )

    for w in (
        _fmt(
            "Active/observing milestones without an assignee",
            "ms81-assignee-missing",
            a_hits,
            "Run: beacon milestone update <ms-id> --assignee <name> "
            "(or `beacon milestone join <ms-id>` to self-add).",
        ),
        _fmt(
            "Done milestones with a leftover worktree directory",
            "ms81-leftover-worktree",
            b_hits,
            "Run: beacon milestone workspace-cleanup <ms-id> to remove the "
            "worktree; leftover dirs are a takeover-by-mistake hazard.",
        ),
        _fmt(
            "Occupation field stuck without a session_id",
            "ms81-stale-occupation",
            c_hits,
            "Run: beacon milestone release <ms-id> to clear the half-written "
            "occupation marker.",
        ),
        _fmt(
            "Waiting milestones with commits/PRs attached after wait_at",
            "ms81-waiting-write",
            d_hits,
            "Re-activate with `beacon milestone start <ms-id>` before adding "
            "more commit / PR entries — the write gate (e-1916) catches new "
            "writes but does not retroactively clean up past attachments.",
        ),
    ):
        if w:
            warnings.append(w)
    return warnings


def cmd_doctor():
    """Lightweight environment health check for Beacon.

    Checks (in order):
      1. beacon binary is on PATH
      2. ~/.claude/settings.json has PostToolUse hook configured
      3. Required Skills are installed in ~/.claude/skills/
      4. Token expiry (JWT decode — no network required)
      5. .beacon/cloud.json exists and has api_url set (if project is cloud-mode)
      6. Repo skills/ content matches installed ~/.claude/skills/ content
         (ms-10 e-722; only when running from a beacon source checkout).
      7. Duplicate IDs (Issue #14).
      8. Skill markdown references known beacon subcommands (ms-61 / e-1570).
      9. .beacon/project.json staleness vs cloud (ms-61 / e-1571).

    Only prints warnings for problems found. Exits 0 if all checks pass,
    exits 1 if at least one warning was emitted.
    """
    import shutil as _shutil
    import time as _time

    home = _user_home()
    warnings: list[str] = []

    # ------------------------------------------------------------------ #
    # 1. beacon on PATH (+ e-1170 version shadowing detect)
    # ------------------------------------------------------------------ #
    beacon_paths = _find_all_on_path("beacon")
    if not beacon_paths:
        warnings.append(
            "WARN [PATH] `beacon` not found on PATH.\n"
            "       Add the beacon bin/ directory to your PATH, or use\n"
            "       the full path to the beacon script."
        )
    elif len(beacon_paths) > 1:
        # e-1170: multiple beacon binaries on PATH = potential version
        # shadowing. Today's Win user case: stale ~/.local/bin 0.11.1
        # shadowing AppData/.../Scripts 0.19.0, so `beacon --version`
        # returned the old value silently and post-install confusion was
        # massive. Surface the shadow chain with versions for each.
        import subprocess as _subprocess
        version_lines = []
        for bp in beacon_paths:
            try:
                v = _subprocess.run(
                    [bp, "--version"], capture_output=True, text=True, timeout=3
                ).stdout.strip() or "?"
            except Exception:
                v = "?"
            version_lines.append(f"         {bp}  →  {v}")
        primary = beacon_paths[0]
        warnings.append(
            f"WARN [PATH] Multiple `beacon` binaries on PATH ({len(beacon_paths)} found):\n"
            + "\n".join(version_lines)
            + f"\n       Primary (will be used): {primary}\n"
            "       This is usually fine, but if `beacon --version` looks stale\n"
            "       relative to your last upgrade, the entry earlier on PATH is\n"
            "       shadowing a newer install. Remove or reorder PATH to put the\n"
            "       intended install first."
        )

    # ------------------------------------------------------------------ #
    # 2. PostToolUse hook in ~/.claude/settings.json
    # ------------------------------------------------------------------ #
    settings_path = os.path.join(home, ".claude", "settings.json")
    hook_ok = False
    hook_broken_cmd = ""  # ms-44 e-853: beacon hook present but not runnable here
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as _f:
                _settings = json.load(_f)
            _post_tool = _settings.get("hooks", {}).get("PostToolUse", [])
            for _entry in _post_tool:
                for _h in _entry.get("hooks", []):
                    _cmd = _h.get("command", "")
                    if "beacon" not in _cmd:
                        continue
                    # A beacon hook is configured — but is it runnable on this
                    # OS? A .sh on Windows (or a path that no longer exists)
                    # silently no-ops, so /beacon-log never fires. (e-853)
                    if _hook_unusable_on_windows(_cmd) or (
                        _is_path_command(_cmd) and not os.path.exists(_cmd)
                    ):
                        hook_broken_cmd = _cmd
                        continue
                    hook_ok = True
                    break
                if hook_ok:
                    break
        except Exception:
            pass
    if not hook_ok and hook_broken_cmd:
        warnings.append(
            "WARN [hooks] PostToolUse hook is configured but not executable on this OS:\n"
            f"       {hook_broken_cmd}\n"
            "       (a .sh hook does not run on Windows; commit -> /beacon-log will not fire.)\n"
            "       Run: beacon skill install   (re-points it to the cross-platform hook)"
        )
    elif not hook_ok:
        warnings.append(
            "WARN [hooks] PostToolUse hook not configured in ~/.claude/settings.json.\n"
            "       Run: beacon skill install"
        )

    # ------------------------------------------------------------------ #
    # 3. Required Skills installed
    # ------------------------------------------------------------------ #
    required_skills = ["beacon-log", "beacon-session-start", "beacon-session-end"]
    claude_skills = os.path.join(home, ".claude", "skills")
    for _skill in required_skills:
        _skill_dir = os.path.join(claude_skills, _skill)
        if not os.path.isdir(_skill_dir):
            warnings.append(
                f"WARN [skills] Skill `{_skill}` not installed.\n"
                "       Run: beacon skill install"
            )

    # ------------------------------------------------------------------ #
    # 4. Token expiry (no network — JWT decode only)
    # ------------------------------------------------------------------ #
    from auth import _credentials_path, _decode_jwt_expiry
    _cred_path = _credentials_path()
    if _cred_path.exists():
        try:
            with open(_cred_path, "r", encoding="utf-8") as _f:
                _creds = json.load(_f)
            _token = _creds.get("token") or _creds.get("id_token") or ""
            if _token:
                _exp = _creds.get("token_expiry") or _decode_jwt_expiry(_token)
                if _exp:
                    _now = int(_time.time())
                    _remaining = _exp - _now
                    if _remaining < 0:
                        warnings.append(
                            "WARN [token] Credentials have expired.\n"
                            "       Run: beacon auth login"
                        )
                    elif _remaining < 300:  # less than 5 minutes
                        warnings.append(
                            f"WARN [token] Credentials expire in {_remaining}s (< 5 min).\n"
                            "       Run: beacon auth login"
                        )
        except Exception:
            pass
    else:
        warnings.append(
            "WARN [token] Not logged in (no credentials file found).\n"
            "       Run: beacon auth login"
        )

    # ------------------------------------------------------------------ #
    # 5. cloud.json present and valid (only when in a beacon project dir)
    # ------------------------------------------------------------------ #
    # e-1861 (ms-61): cloud.json existence is the sole source of truth for
    # cloud mode. config.json's ``mode`` field was retired because it could
    # be silently overwritten by a sub-agent to flip cloud → local without
    # touching cloud.json (2026-06-15 incident). doctor now:
    #   (a) validates cloud.json shape directly when present, and
    #   (b) surfaces any legacy ``mode`` field still in config.json as a
    #       gentle migration warning (non-fatal, ignored by the runtime).
    cloud_json_path = os.path.join(".beacon", "cloud.json")
    config_json_path = os.path.join(".beacon", "config.json")
    if os.path.exists(cloud_json_path):
        try:
            with open(cloud_json_path, "r", encoding="utf-8") as _f:
                _cloud = json.load(_f)
            if not _cloud.get("api_url"):
                warnings.append(
                    "WARN [cloud.json] api_url is not set in .beacon/cloud.json.\n"
                    "       Run: beacon cloud push"
                )
        except Exception:
            warnings.append(
                "WARN [cloud.json] .beacon/cloud.json is unreadable.\n"
                "       Run: beacon cloud push"
            )
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path, "r", encoding="utf-8") as _f:
                _config = json.load(_f)
            if isinstance(_config, dict) and "mode" in _config:
                warnings.append(
                    "WARN [legacy-mode-field] .beacon/config.json still contains a `mode` field.\n"
                    "       This field is ignored as of e-1861 (ms-61) — cloud.json existence\n"
                    "       is now the sole source of truth. The field is harmless but can be\n"
                    "       removed manually for cleanliness."
                )
        except Exception:
            pass  # config.json unreadable — not a fatal error

    # ------------------------------------------------------------------ #
    # 6. Duplicate IDs (Issue #14)
    # ------------------------------------------------------------------ #
    # We use load_project_unsafe so doctor can still surface the very
    # problem strict load refuses to load. If we can't load at all (no
    # project, network error in cloud mode, etc.), we silently skip this
    # check — doctor is best-effort for environment health, not a
    # project linter.
    try:
        _data = load_project_unsafe()
        dup_report = core.find_duplicate_ids(_data)
        for category_label, key in (
            ("milestone", "milestones"),
            ("entry", "entries"),
            ("operation", "operations"),
        ):
            for did, n in dup_report.get(key, {}).items():
                if category_label == "milestone":
                    fix_cmd = f"beacon milestone purge {did} --reason \"...\" --index <n>"
                elif category_label == "entry":
                    fix_cmd = f"beacon entry purge {did} --reason \"...\" --index <n>"
                elif category_label == "operation":
                    fix_cmd = f"beacon operation purge {did} --reason \"...\" --index <n>"
                else:
                    fix_cmd = f"contact maintainers (corrupt {category_label})"
                warnings.append(
                    f"WARN [dup-id] Duplicate {category_label} ID '{did}' "
                    f"appears {n} times (Issue #14).\n"
                    f"       Recovery: {fix_cmd}"
                )
    except Exception:
        pass  # Project not loadable from this CWD — skip dup check.

    # ------------------------------------------------------------------ #
    # 7. Repo skills/ vs installed ~/.claude/skills/ drift (ms-10 e-722)
    # ------------------------------------------------------------------ #
    # This check only runs when we can find the drift script — i.e. running
    # from a beacon source checkout. brew / pipx users don't have scripts/
    # in their install tree and would get nothing meaningful from the check.
    _beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _drift_script = os.path.join(_beacon_root, "scripts", "check-skill-drift.py")
    if os.path.isfile(_drift_script):
        try:
            _r = subprocess.run(
                ["python3", _drift_script, "--json"],
                capture_output=True, text=True, timeout=10,
            )
            _report = json.loads(_r.stdout) if _r.stdout.strip() else {"ok": True, "drift": []}
            if not _report.get("ok", True):
                _drift = _report.get("drift", [])
                _names = [d.get("name", "?") for d in _drift]
                _summary = ", ".join(_names[:5])
                if len(_names) > 5:
                    _summary += f", +{len(_names) - 5} more"
                warnings.append(
                    "WARN [skills-drift] repo skills/ and ~/.claude/skills/ differ.\n"
                    f"       Affected: {_summary}\n"
                    "       Run: beacon skill install"
                )
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            # Drift check is best-effort; never let it block doctor.
            pass

    # ------------------------------------------------------------------ #
    # 8. Skill ↔ CLI command drift (ms-61 / e-1570)
    # ------------------------------------------------------------------ #
    # Walk each ~/.claude/skills/<name>/*.md and verify that any
    # `beacon X [Y ...]` invocation hits a known subcommand. Catches the
    # case where a CLI command was renamed / removed but dependent Skills
    # weren't touched (e.g., the retired `beacon summary` write path that
    # /beacon-session-end was still calling pre-e-1364).
    if os.environ.get("BEACON_DOCTOR_SKIP_SKILL_DRIFT") != "1":
        warnings.extend(_doctor_check_skill_cli_drift(home))

    # ------------------------------------------------------------------ #
    # 9. .beacon/project.json staleness vs cloud (ms-61 / e-1571)
    # ------------------------------------------------------------------ #
    # Detect when local project cache is out of sync with cloud — the
    # exact P3 case observed on 2026-06-12 in this fork worktree, where
    # `beacon status` returned 22 milestones while cloud held 67 because
    # `beacon cloud pull` had never run from this cwd. Best-effort: skips
    # on local mode, recent local writes (<5 min), and network errors.
    warnings.extend(_doctor_check_project_staleness())

    # ------------------------------------------------------------------ #
    # 10. CLAUDE.md entry-writing-principle marker (ms-68 / e-1640)
    # ------------------------------------------------------------------ #
    # Detect when CLAUDE.md is missing the marker that anchors the
    # entry-writing-principle section. Without it, the principle drops
    # out of every-session context and AI silently breaks reader-first /
    # loanword / ID-context / no-truncation rules. Warning-level only.
    if os.environ.get("BEACON_DOCTOR_SKIP_PRINCIPLE_MARKER") != "1":
        warnings.extend(_doctor_check_claude_md_principle_marker())

    # ------------------------------------------------------------------ #
    # 11. ms-81 state-machine warnings (e-1921)
    # ------------------------------------------------------------------ #
    # Surface the four SPEC §9 conditions where the state-machine and
    # occupation model is being violated — warning-level only because the
    # whole MS is designed as a forcing function, not a wall.
    if os.environ.get("BEACON_DOCTOR_SKIP_MS81") != "1":
        warnings.extend(_doctor_check_ms81_state_machine())

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    if warnings:
        for w in warnings:
            print(w)
        sys.exit(1)
    else:
        print("OK: all checks passed.")
        sys.exit(0)


def cmd_help_json():
    """Output beacon CLI command reference as machine-readable JSON."""
    commands = [
        {"command": "beacon init", "flags": [], "description": "Initialize .beacon/ in current directory"},
        {"command": "beacon setup", "flags": [], "description": "First-time setup wizard (auth + hooks + project)"},
        {"command": "beacon status", "flags": ["--json", "--ms <id>"], "description": "Show current status"},
        {"command": "beacon milestone add", "flags": [], "description": "Add a new milestone (interactive)"},
        {"command": "beacon milestone list", "flags": ["--json"], "description": "List milestones"},
        {"command": "beacon milestone start <id>", "flags": ["--no-branch", "--no-assignee"], "description": "Activate milestone + auto-create ms-XX-<slug> branch + self-add as assignee"},
        {"command": "beacon milestone join <id>", "flags": ["--checkout"], "description": "Add self as assignee on a milestone (and optionally switch to its branch)"},
        {"command": "beacon milestone close <id>", "flags": [], "description": "Close milestone"},
        {"command": "beacon milestone observe <id>", "flags": [], "description": "Set milestone to observing"},
        {"command": "beacon milestone wait <id>", "flags": ["--reason"], "description": "Pause an active/observing milestone (ms-81)"},
        {"command": "beacon milestone release <id>", "flags": [], "description": "Release occupation claim without changing status (ms-81)"},
        {"command": "beacon milestone occupations", "flags": ["--ms <id>", "--json"], "description": "List occupation event log (ms-81)"},
        {"command": "beacon milestone rename <id> <title>", "flags": [], "description": "Rename a milestone"},
        {"command": "beacon milestone depends <id> --on <id>", "flags": [], "description": "Declare milestone dependency"},
        {"command": "beacon milestone purge <id> --reason <text>", "flags": ["--index <n>", "--json"], "description": "Hard-delete a milestone record (recovery for duplicate-ID corruption; Issue #14)"},
        {"command": "beacon entry purge <e-id> --reason <text>", "flags": ["--index <n>", "--json"], "description": "Hard-delete an entry record (recovery for duplicate-ID corruption; e-863)"},
        {"command": "beacon operation purge <op-id> --reason <text>", "flags": ["--index <n>", "--json"], "description": "Hard-delete an operation record (recovery for duplicate-ID corruption; e-863)"},
        {"command": "beacon operation approve <op-id> --spec <doc-id>", "flags": ["--expires-at YYYY-MM-DD", "--ttl-seconds N", "--json"], "description": "Mint T2 envelope from SPEC doc's approved_actions list (ms-60 / e-1339)"},
        {"command": "beacon operation revoke <op-id>", "flags": ["--envelope-id ENV", "--reason TEXT", "--json"], "description": "Invalidate the active T2 envelope (auto-picks active if --envelope-id omitted)"},
        {"command": "beacon operation envelope verify <op-id> <action>", "flags": ["--json"], "description": "AI self-check: is <action> permitted by the active envelope's approved_actions? (ms-60 / e-1340)"},
        {"command": "beacon milestone graph", "flags": ["--json"], "description": "Show dependency graph"},
        {"command": "beacon task add <desc>", "flags": ["-m <ms-id>"], "description": "Add a task to a milestone"},
        {"command": "beacon task done <entry-id>", "flags": [], "description": "Mark task as done"},
        {"command": "beacon task list", "flags": ["--json", "--ms <id>"], "description": "List tasks"},
        {"command": "beacon task update <entry-id>", "flags": ["--ms <ms-id>", "--description <text>", "--status <s>", "--detail <text>", "--motivation <text>", "--acceptance-criteria <text>", "--behavior <text>", "--priority <p>"], "description": "Update task fields (description / status / detail / motivation / acceptance_criteria / behavior / priority) or move to another milestone"},
        {"command": "beacon log [message]", "flags": ["--prepare", "--finalize", "-m <ms-id>", "--progress <n>", "--summary <text>"], "description": "Record HEAD commit to active milestone"},
        {"command": "beacon save <desc>", "flags": ["-m <ms-id>", "--hash <hash>", "--source manual", "--json"], "description": "Save a freeform entry to a milestone"},
        {"command": "beacon sync", "flags": [], "description": "Auto-sync recent git commits to active milestone"},
        {"command": "beacon summary <text>", "flags": [], "description": "Update project summary"},
        {"command": "beacon doc add", "flags": ["--scope <core|spec|memo>", "--ms <id>", "--title <title>", "--content <text>", "--stdin"], "description": "Add a document"},
        {"command": "beacon doc list", "flags": ["--json", "--scope <scope>", "--ms <id>"], "description": "List documents"},
        {"command": "beacon doc show <doc-id>", "flags": [], "description": "Show document content"},
        {"command": "beacon doc update <doc-id>", "flags": ["--content <text>", "--stdin"], "description": "Update document content"},
        {"command": "beacon doc image-upload <local-file>", "flags": ["--json"], "description": "Upload image, get markdown img tag"},
        {"command": "beacon pr add", "flags": ["-m <ms-id>", "--url <url>", "--intent <text>"], "description": "Record a PR entry"},
        {"command": "beacon pr approve <entry-id>", "flags": [], "description": "Approve a PR"},
        {"command": "beacon pr reject <entry-id>", "flags": [], "description": "Reject a PR"},
        {"command": "beacon pr merge <entry-id>", "flags": [], "description": "Mark PR as merged"},
        {"command": "beacon retro", "flags": [], "description": "Start weekly retrospective (interactive)"},
        {"command": "beacon trigger check", "flags": [], "description": "Check pending triggers (JSON array)"},
        {"command": "beacon cloud list", "flags": [], "description": "List cloud projects"},
        {"command": "beacon cloud upload-initial", "flags": ["--force"], "description": "Initial bootstrap upload to a new cloud project (e-1862); alias of legacy 'push'"},
        {"command": "beacon cloud force-pull", "flags": [], "description": "Emergency overwrite local from cloud (e-1862); alias of legacy 'pull'"},
        {"command": "beacon cloud push", "flags": ["--force"], "description": "Legacy alias for upload-initial (deprecated, kept for backward compat)"},
        {"command": "beacon cloud pull", "flags": [], "description": "Legacy alias for force-pull (deprecated, kept for backward compat)"},
        {"command": "beacon cloud join <id>", "flags": [], "description": "Join an existing cloud project"},
        {"command": "beacon auth login", "flags": [], "description": "Sign in with Google"},
        {"command": "beacon auth logout", "flags": [], "description": "Remove cached credentials"},
        {"command": "beacon auth status", "flags": [], "description": "Show login status"},
        {"command": "beacon skill install", "flags": [], "description": "Install Claude Code Skills to ~/.claude/skills/"},
        {"command": "beacon monitor context", "flags": ["--dry-run"], "description": "Stop hook: context-usage threshold monitor (e-854); --dry-run skips note/state writes"},
        # ms-69 e-1652+: Trek = cross-project / cross-session collaboration area
        {"command": "beacon trek create <title>", "flags": ["--type temporary|persistent", "--description <text>", "--goal-state <criterion>", "--json"], "description": "Create a trek (cross-project協奏作業領域); caller becomes leader (ms-69). --goal-state は ms-75/e-1865 完了マーカー"},
        {"command": "beacon trek list", "flags": ["--status <s>", "--include-archived", "--all-actors", "--joined", "--json"], "description": "List treks visible to the caller. --joined で自分が join 済の trek だけ"},
        {"command": "beacon trek show <trek-id>", "flags": ["--all", "--json"], "description": "Show trek detail + 集約ビュー (task / commit / doc, ms-75/e-1864). --all で cap 解除"},
        {"command": "beacon trek timeline <trek-id>", "flags": ["--limit N", "--json"], "description": "Trek の lifecycle / commit / task done / doc を時系列で参照 (ms-75/e-1867)"},
        {"command": "beacon trek start <trek-id>", "flags": ["--json"], "description": "Transition trek planning → active"},
        {"command": "beacon trek archive <trek-id>", "flags": ["--json"], "description": "Archive trek (= terminal); restart by creating a new one"},
        {"command": "beacon trek invite <trek-id> --actor <email>", "flags": ["--notify", "--json"], "description": "Invite a user (by email) into the trek"},
        {"command": "beacon trek join <trek-id>", "flags": ["--json"], "description": "Accept own invitation"},
        {"command": "beacon trek leave <trek-id>", "flags": ["--json"], "description": "Remove self from the trek (leader must transfer first)"},
        {"command": "beacon trek plan <trek-id>", "flags": ["--add-scope <project:ref>", "--remove-scope <project:ref>", "--goal-state <criterion>", "--json"], "description": "Edit trek scope or goal_state (ms-75/e-1865)"},
        {"command": "beacon trek stop <trek-id>", "flags": ["--reason <text>", "--json"], "description": "Pull the Andon cord (= halt signal, sessions pause)"},
        {"command": "beacon trek resume <trek-id>", "flags": ["--json"], "description": "Clear the halt signal"},
        {"command": "beacon trek transfer-leader <trek-id> --to <session-id>", "flags": ["--json"], "description": "Hand off leader_session_id to another session"},
        {"command": "beacon trek take-over <trek-id>", "flags": ["--json"], "description": "Fresh session (= same user, leader role) を新 leader_session_id に bind し直す (= dead session 引き継ぎ、ms-88 e-2089)"},
        # ms-54 e-1266: DM channel lifecycle commands (install / uninstall / opt-out / opt-in / status)
        {"command": "beacon channel install", "flags": [], "description": "Install beacon-bus MCP entry into .mcp.json (DM channel for multi-session messaging)"},
        {"command": "beacon channel uninstall", "flags": ["--purge-files", "--keep-files"], "description": "Remove beacon-bus MCP entry; --purge-files also wipes channel/node_modules (moved to .trash/)"},
        {"command": "beacon channel opt-out", "flags": ["--project", "--global"], "description": "Block all install / auto-install attempts (persistent flag)"},
        {"command": "beacon channel opt-in", "flags": ["--project", "--global"], "description": "Lift the opt-out flag at project or global scope"},
        {"command": "beacon channel status", "flags": [], "description": "Show install / files / opt-out / next-action state in one screen"},
        # ms-55 e-1736: coordination signals (= 走る / 止まる両輪).
        # SPEC `bnzTXhu6KYIMfVE2Ivy2` for the design; landed in
        # e-1646 (stop) / e-1647 (rollback) / e-1648 (claim) /
        # e-1649 (stuck) / e-1650 (morning).
        {"command": "beacon stop scoped <target>", "flags": ["--kind ms|task|session", "--reason-kind <k>", "--reason <text>", "--json"], "description": "Broadcast a STOP signal at a single MS / task / session (Andon cord — anyone can halt)"},
        {"command": "beacon stop global", "flags": ["--reason-kind <k>", "--reason <text>", "--json"], "description": "Broadcast STOP across every active autonomous session (everything-stops fallback)"},
        {"command": "beacon stop status", "flags": ["--json"], "description": "Show the latest stop / resume state from the stop-signal channel"},
        {"command": "beacon resume scoped <target>", "flags": ["--kind ms|task|session", "--reason <text>", "--json"], "description": "Clear a scoped STOP, allowing the targeted session(s) to resume work"},
        {"command": "beacon resume global", "flags": ["--reason <text>", "--json"], "description": "Clear a global STOP across every autonomous session"},
        {"command": "beacon rollback", "flags": ["--commits N", "--reason <text>", "--dry-run", "--no-record", "--json"], "description": "Undo working tree (git stash) + N local commits (--soft reset); push past upstream → report-only with compensation proposals"},
        {"command": "beacon claim request <kind>:<id>", "flags": ["--intent <text>", "--json"], "description": "Announce intent to take a target (ms/task/operation/trek/free); other sessions can respond"},
        {"command": "beacon claim respond <claim-id>", "flags": ["--accept|--reject", "--reason <text>", "--json"], "description": "Respond to another session's claim request"},
        {"command": "beacon claim post <kind>:<id>", "flags": ["--intent <text>", "--json"], "description": "Post-hoc record that this session already started on the target (no request/response dance)"},
        {"command": "beacon claim handoff <claim-id> --to <session>", "flags": ["--reason <text>", "--json"], "description": "Transfer an active claim to a different session"},
        {"command": "beacon claim release <claim-id>", "flags": ["--outcome completed|abandoned", "--reason <text>", "--json"], "description": "Release a claim (outcome surfaces in `beacon morning` as 完了 or skip)"},
        {"command": "beacon claim list", "flags": ["--json"], "description": "List active claims from local `.beacon/active_claims.json` (= restart restore path)"},
        {"command": "beacon stuck check", "flags": ["--telemetry-file <path>", "--idle-min N", "--json"], "description": "Detect sessions idle past --idle-min; emit STUCK stop signals so morning briefing surfaces 介入要望"},
        {"command": "beacon morning", "flags": ["--since-hours N", "--events-file <path>", "--no-doc", "--json"], "description": "4-bucket digest of recent autonomous activity (完了 / 停止 / skip / 介入要望); auto-saves as scope=report doc"},
        {"command": "beacon help", "flags": ["--json"], "description": "Show help (--json for machine-readable output)"},
    ]
    print(json.dumps({"version": __version__, "commands": commands}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Operation / Run / Incident commands
# ---------------------------------------------------------------------------

def cmd_operation_open():
    title = os.environ.get("BEACON_OPERATION_TITLE", "")
    schedule = os.environ.get("BEACON_OPERATION_SCHEDULE", "weekdays")
    log_source = os.environ.get("BEACON_OPERATION_LOG_SOURCE", "")
    status = os.environ.get("BEACON_OPERATION_STATUS", "open")
    activation_hint = os.environ.get("BEACON_ACTIVATION_HINT", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    acceptance_criteria = os.environ.get("BEACON_ACCEPTANCE_CRITERIA", "")
    priority = os.environ.get("BEACON_PRIORITY", "")
    if not title:
        print("Error: operation title required")
        sys.exit(1)
    data = load_project()
    try:
        data, op = core.operation_open(
            data, title, schedule=schedule, log_source=log_source,
            status=status, activation_hint=activation_hint,
            objective=objective, acceptance_criteria=acceptance_criteria,
            priority=priority,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_open", "op_id": op["id"], "title": title})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(op, ensure_ascii=False))
    else:
        print(f"Operation {op['status']}: {op['id']} \"{op['title']}\" [{op['schedule']['frequency']}]")


def cmd_operation_set_status():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    status = os.environ.get("BEACON_OPERATION_STATUS", "")
    if not op_id or not status:
        print("Error: operation id and status required")
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_set_status(data, op_id, status)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_status", "op_id": op_id, "status": status})
    print(f"Operation {status}: {op_id} \"{op.get('title','')}\"")


def cmd_operation_update():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    data = load_project()
    try:
        op = core.operation_update(
            data, op_id,
            title=os.environ.get("BEACON_TITLE", ""),
            schedule=os.environ.get("BEACON_OPERATION_SCHEDULE", ""),
            activation_hint=os.environ.get("BEACON_ACTIVATION_HINT", ""),
            objective=os.environ.get("BEACON_OBJECTIVE", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
            priority=os.environ.get("BEACON_PRIORITY", ""),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_update", "op_id": op_id})
    print(f"Operation updated: {op_id} \"{op.get('title','')}\"")


def cmd_operation_task_add():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    description = os.environ.get("BEACON_DESCRIPTION", "")
    if not op_id or not description:
        print("Error: --op and description required")
        sys.exit(1)
    data = load_project()
    try:
        op, entry = core.operation_task_add(
            data, op_id, description,
            priority=os.environ.get("BEACON_PRIORITY", ""),
            motivation=os.environ.get("BEACON_MOTIVATION", ""),
            acceptance_criteria=os.environ.get("BEACON_ACCEPTANCE_CRITERIA", ""),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_task_add", "op_id": op_id, "entry_id": entry["id"]})
    print(f"Added operation_task [{entry['id']}] to {op_id}: {description}")


def cmd_operation_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    if not entry_id:
        print("Error: entry id required")
        sys.exit(1)
    if not reason:
        print("Error: --reason is required. Record why this task is done.", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    try:
        entry = core.operation_task_done(data, entry_id, reason=reason)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "operation_task_done", "entry_id": entry_id, "reason": reason})
    print(f"Done: [{entry_id}] {entry.get('description','')}\n  Reason: {reason}")


def cmd_operation_task_list():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: --op required")
        sys.exit(1)
    data = load_project()
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            tasks = [e for e in op.get("entries", []) if e.get("type") == "operation_task"]
            if os.environ.get("BEACON_JSON"):
                print(json.dumps(tasks, ensure_ascii=False))
                return
            if not tasks:
                print(f"No operation_tasks in {op_id}")
                return
            for t in tasks:
                icon = "●" if t.get("status") == "done" else "○"
                pri = f" [{t['meta']['priority']}]" if t.get("meta", {}).get("priority") else ""
                print(f"  {icon} [{t['id']}]{pri} {t.get('description','')}")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


def cmd_operation_close():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    data = load_project()
    op = core.operation_close(data, op_id)
    save_project(data, op={"type": "operation_close", "op_id": op_id})
    print(f"Operation closed: {op_id} \"{op.get('title', '')}\"")


def cmd_operation_list():
    data = load_project()
    ops = data.get("operations", [])
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(ops, ensure_ascii=False))
        return
    if not ops:
        print("No operations.")
        return
    for op in ops:
        status_icon = "◐" if op.get("status") == "open" else "●"
        entries = op.get("entries", [])
        runs = [e for e in entries if e.get("type") == "run_record"]
        incidents = [e for e in entries if e.get("type") == "incident" and e.get("status") == "open"]
        last_run = f" last: {_local_date(runs[-1]['date'])} {runs[-1]['status']}" if runs else ""
        incident_info = f" ⚠ {len(incidents)} incident(s)" if incidents else ""
        print(f"{status_icon} {op['id']} \"{op.get('title', '')}\" [{op.get('schedule', {}).get('frequency', '')}]{last_run}{incident_info}")


def _fetch_active_operation_envelope(op_id: str):
    """Cloud-mode helper: fetch the active T2 envelope for ``op_id`` if any.

    Returns ``None`` in local mode, on auth issues, or if the server is not
    reachable. Used by ``cmd_operation_show`` for the envelope section and
    by ``cmd_operation_revoke`` to default the envelope id to the active one.
    """
    if not _is_cloud_mode():
        return None
    try:
        client, config = _get_api_client()
        records = client.list_operation_envelopes(
            config["project_id"], op_id, status="active"
        )
    except Exception:
        return None
    return records[0] if records else None


def cmd_operation_show():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: operation id required")
        sys.exit(1)
    data = load_project()
    json_mode = bool(os.environ.get("BEACON_JSON"))
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            # Augment with envelope record (cloud mode only).
            active_env = _fetch_active_operation_envelope(op_id)
            if json_mode:
                payload = dict(op)
                if active_env is not None:
                    payload["active_envelope"] = active_env
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"{op['id']} \"{op.get('title', '')}\" [{op.get('status', '')}]")
                if active_env:
                    env = active_env.get("envelope", {})
                    created = active_env.get("created_at", "")[:10]
                    expires = env.get("expires_at", "")[:10]
                    print(f"  Active envelope: {active_env.get('envelope_id', '')[:12]}…  "
                          f"(issued {created} by {active_env.get('created_by', '')})")
                    actions = active_env.get("approved_actions", [])
                    if actions:
                        print(f"    Approved actions:")
                        for a in actions:
                            print(f"      - {a}")
                    if expires:
                        print(f"    Expires: {expires}  (revoke to invalidate)")
                else:
                    print(f"  Envelope: none active  "
                          f"(autonomous execution disabled — run "
                          f"`beacon operation approve {op_id} --spec <doc-id>`)")
                for e in op.get("entries", []):
                    if e.get("type") == "run_record":
                        icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(e.get("status", ""), "?")
                        print(f"  {icon} {e['date'][:10]} {e.get('batch', '')} — {e.get('description', '')}")
                    elif e.get("type") == "incident":
                        icon = "⚠" if e.get("status") == "open" else "✓"
                        print(f"  {icon} [{e['id']}] {e.get('title', '')} [{e.get('status', '')}]")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


def cmd_operation_approve():
    """Mint a T2 envelope from a SPEC doc's ``approved_actions`` (ms-60 / e-1339).

    Cloud-mode only. Local mode is rejected with a clear message — local
    project.json doesn't have anywhere to put a server-signed envelope
    record (the security model needs a server signing key).
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    spec_doc_id = os.environ.get("BEACON_SPEC_DOC_ID", "")
    ttl_seconds_str = os.environ.get("BEACON_TTL_SECONDS", "")
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(1)
    if not spec_doc_id:
        print("Error: --spec <doc-id> required (the SPEC doc whose "
              "approved_actions to authorize)", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        print("Error: operation approve requires cloud mode "
              "(envelope signing needs a server key). Run "
              "'beacon cloud push' first.", file=sys.stderr)
        sys.exit(1)

    ttl_seconds = None
    if ttl_seconds_str:
        try:
            ttl_seconds = int(ttl_seconds_str)
            if ttl_seconds <= 0:
                raise ValueError
        except ValueError:
            print(f"Error: --ttl-seconds must be a positive integer "
                  f"(got {ttl_seconds_str!r})", file=sys.stderr)
            sys.exit(1)

    client, config = _get_api_client()
    try:
        record = client.operation_approve(
            config["project_id"], op_id,
            spec_doc_id=spec_doc_id, ttl_seconds=ttl_seconds,
        )
    except Exception as exc:
        print(f"Error: approve failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
        return
    env_id = record.get("envelope_id", "")
    env = record.get("envelope", {})
    actions = record.get("approved_actions", [])
    print(f"Approved: {op_id}")
    print(f"  envelope: {env_id}")
    print(f"  spec doc: {record.get('spec_doc_id', '')}")
    print(f"  issuer:   {record.get('created_by', '')}")
    print(f"  expires:  {env.get('expires_at', '')[:10]}")
    print(f"  approved actions ({len(actions)}):")
    for a in actions:
        print(f"    - {a}")


def cmd_operation_envelope_verify():
    """Check whether a requested action is permitted by the active envelope.

    ms-60 / e-1340 — the AI self-check primitive. Called by the
    ``/beacon-operation-execute`` Skill before running each action.

    Output (json mode):
      {"op_id": ..., "action": ..., "envelope_id": ...|null,
       "active": bool, "permitted": bool,
       "approved_actions": [...], "reason": "..."}

    Exit codes:
      0 — permitted (active envelope + action matches approved_actions)
      1 — not permitted (no active envelope, or action outside scope)
      2 — usage / cloud error

    Designed to be easy to call from a Skill: ``beacon operation envelope
    verify op-X "extract:profile:user-1" --json`` returns a one-shot decision.
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    action = os.environ.get("BEACON_ACTION", "")
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(2)
    if not action:
        print("Error: action required (e.g. 'extract:profile:user-1')",
              file=sys.stderr)
        sys.exit(2)
    if not _is_cloud_mode():
        print("Error: envelope verify requires cloud mode.", file=sys.stderr)
        sys.exit(2)

    # Reuse server/approved_actions matcher as single source of truth.
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "server")
    )
    try:
        import approved_actions as aa
    except ImportError as exc:
        print(f"Error: cannot load approved_actions module: {exc}",
              file=sys.stderr)
        sys.exit(2)

    try:
        client, config = _get_api_client()
        records = client.list_operation_envelopes(
            config["project_id"], op_id, status="active"
        )
    except Exception as exc:
        print(f"Error: cannot fetch envelope: {exc}", file=sys.stderr)
        sys.exit(2)

    active = records[0] if records else None
    if active is None:
        result = {
            "op_id": op_id, "action": action,
            "envelope_id": None, "active": False, "permitted": False,
            "approved_actions": [],
            "reason": "no active envelope — operation not approved",
        }
    else:
        approved = active.get("approved_actions", [])
        permitted = aa.matches(approved, action)
        result = {
            "op_id": op_id, "action": action,
            "envelope_id": active.get("envelope_id"),
            "active": True, "permitted": permitted,
            "approved_actions": approved,
            "reason": (
                "action matches approved_actions"
                if permitted
                else "action outside approved_actions — escalate or refuse"
            ),
        }

    if json_mode:
        print(json.dumps(result, ensure_ascii=False))
    else:
        flag = "✓ permitted" if result["permitted"] else "✗ not permitted"
        print(f"{flag}: {op_id} action={action!r}")
        print(f"  reason: {result['reason']}")
        if result["envelope_id"]:
            print(f"  envelope: {result['envelope_id']}")
        if result["approved_actions"]:
            print(f"  approved actions ({len(result['approved_actions'])}):")
            for a in result["approved_actions"]:
                print(f"    - {a}")

    sys.exit(0 if result["permitted"] else 1)


def cmd_operation_revoke():
    """Mark the active envelope on ``op_id`` as revoked (ms-60 / e-1339).

    Without ``--envelope-id``, the current active envelope is revoked. If
    no active envelope exists, this is an error so the caller doesn't
    silently no-op.
    """
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    envelope_id = os.environ.get("BEACON_ENVELOPE_ID", "")
    reason = os.environ.get("BEACON_REASON", "") or "manual revoke"
    json_mode = bool(os.environ.get("BEACON_JSON"))

    if not op_id:
        print("Error: operation id required", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        print("Error: operation revoke requires cloud mode.", file=sys.stderr)
        sys.exit(1)

    client, config = _get_api_client()
    if not envelope_id:
        active = _fetch_active_operation_envelope(op_id)
        if not active:
            print(f"Error: no active envelope for {op_id}. "
                  f"Pass --envelope-id <id> to revoke a specific one, "
                  f"or check `beacon operation show {op_id}`.",
                  file=sys.stderr)
            sys.exit(1)
        envelope_id = active.get("envelope_id", "")

    try:
        record = client.operation_revoke(
            config["project_id"], op_id, envelope_id, reason=reason,
        )
    except Exception as exc:
        print(f"Error: revoke failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if json_mode:
        print(json.dumps(record, ensure_ascii=False))
        return
    print(f"Revoked: {op_id} envelope {envelope_id}")
    print(f"  reason: {record.get('revoke_reason', reason)}")
    print(f"  revoked_at: {record.get('revoked_at', '')[:19]}")


def cmd_run_record():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    batch = os.environ.get("BEACON_RUN_BATCH", "")
    status = os.environ.get("BEACON_RUN_STATUS", "")
    description = os.environ.get("BEACON_RUN_DESC", "")
    if not op_id or not batch or not status:
        print("Error: -o <op-id>, --batch <name>, --status ok|warning|error required")
        sys.exit(1)
    data = load_project()
    op, entry = core.run_record_add(data, op_id, batch=batch, status=status, description=description)
    save_project(data, op={"type": "run_record", "op_id": op_id, "entry_id": entry["id"], "status": status})
    # Clear the operation_check trigger for this op
    trigger_path = os.path.join(_get_triggers_dir(), f"operation_check_{op_id}.json")
    if os.path.exists(trigger_path):
        os.remove(trigger_path)
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(status, "?")
        print(f"Run recorded: {op_id} / {batch} {icon} {status}")
        if description:
            print(f"  {description}")


def cmd_run_list():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    if not op_id:
        print("Error: -o <op-id> required")
        sys.exit(1)
    data = load_project()
    for op in data.get("operations", []):
        if op.get("id") == op_id:
            runs = [e for e in op.get("entries", []) if e.get("type") == "run_record"]
            if os.environ.get("BEACON_JSON"):
                print(json.dumps(runs, ensure_ascii=False))
            else:
                for e in runs:
                    icon = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(e.get("status", ""), "?")
                    print(f"{icon} {_local_date(e['date'])} {e.get('batch', '')} — {e.get('description', '')}")
            return
    print(f"Operation not found: {op_id}")
    sys.exit(1)


def cmd_incident_open():
    op_id = os.environ.get("BEACON_OPERATION_ID", "")
    title = os.environ.get("BEACON_INCIDENT_TITLE", "")
    description = os.environ.get("BEACON_INCIDENT_DESC", "")
    priority = os.environ.get("BEACON_INCIDENT_PRIORITY", "")
    if not op_id or not title:
        print("Error: -o <op-id> and incident title required")
        sys.exit(1)
    data = load_project()
    op, entry = core.incident_open(data, op_id, title=title, description=description, priority=priority)
    save_project(data, op={"type": "incident_open", "op_id": op_id, "entry_id": entry["id"], "title": title})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Incident opened: {entry['id']} \"{title}\" in {op_id}")


def cmd_incident_close():
    incident_id = os.environ.get("BEACON_INCIDENT_ID", "")
    resolution = os.environ.get("BEACON_INCIDENT_RESOLUTION", "")
    if not incident_id:
        print("Error: incident entry id required")
        sys.exit(1)
    data = load_project()
    container, entry = core.incident_close(data, incident_id, resolution=resolution)
    save_project(data, op={"type": "incident_close", "entry_id": incident_id, "resolution": resolution})
    if os.environ.get("BEACON_JSON"):
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"Incident resolved: {incident_id} \"{entry.get('title', '')}\"")
        if resolution:
            print(f"  Resolution: {resolution}")


def cmd_incident_escalate():
    incident_id = os.environ.get("BEACON_INCIDENT_ID", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    if not incident_id or not ms_id:
        print("Error: incident id and -m <ms-id> required")
        sys.exit(1)
    data = load_project()
    op, incident, task = core.incident_escalate(data, incident_id, ms_id)
    save_project(data, op={"type": "incident_escalate", "entry_id": incident_id, "ms_id": ms_id, "task_id": task["id"]})
    print(f"Incident escalated: {incident_id} → {ms_id} as task {task['id']}")
    print(f"  {task['description']}")


def cmd_incident_list():
    """List incidents.

    Filters:
      -o <op-id>          only this Operation
      --status <status>   open / closed (default: all)
      --include-closed    shorthand for --status closed but additive
      --json              machine-readable output

    Used by:
      - /beacon-operation-review Step 6.5 (open Incident close 誘導)
      - /beacon-retrospect (past Incident history retrospection, UC10-O6 / e-619)
      - Direct CLI inspection
    """
    op_filter = os.environ.get("BEACON_OPERATION_ID", "")
    status_filter = os.environ.get("BEACON_INCIDENT_STATUS", "")
    include_closed = os.environ.get("BEACON_INCLUDE_CLOSED", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    results = []
    for op in data.get("operations", []):
        if op_filter and op.get("id") != op_filter:
            continue
        for entry in op.get("entries", []):
            if entry.get("type") != "incident":
                continue
            entry_status = entry.get("status", "open")
            # status_filter wins; if not specified, default is to show open only
            # unless --include-closed is set. Treat "closed" / "resolved" /
            # "cancelled" all as "not open" for the include_closed shorthand
            # since Beacon's incident states are not fully standardized yet.
            if status_filter:
                if status_filter != entry_status:
                    continue
            elif not include_closed:
                if entry_status != "open":
                    continue
            results.append({
                "id": entry.get("id"),
                "op_id": op.get("id"),
                "op_title": op.get("title", ""),
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
                "status": entry_status,
                "priority": entry.get("priority", ""),
                "created_at": entry.get("created_at", ""),
                "closed_at": entry.get("closed_at", ""),
                "resolution": entry.get("resolution", ""),
            })

    # Most recent first
    results.sort(key=lambda r: r.get("created_at", ""), reverse=True)

    if json_mode:
        print(json.dumps(results, ensure_ascii=False))
        return

    if not results:
        if op_filter:
            print(f"No incidents found for {op_filter}.")
        else:
            print("No incidents found.")
        return

    for r in results:
        status_icon = "✓" if r["status"] == "closed" else "⚠"
        date_part = (r.get("closed_at") or r.get("created_at") or "")[:10]
        print(f"{status_icon} [{r['id']}] {r['title']}")
        print(f"  op: {r['op_id']} \"{r['op_title']}\" / {date_part} / status: {r['status']}")
        if r["status"] == "closed" and r.get("resolution"):
            print(f"  resolution: {r['resolution'][:120]}")
        elif r.get("description"):
            print(f"  desc: {r['description'][:120]}")


# ---------------------------------------------------------------------------
# Bus (ms-54 / e-999 rendezvous client + e-1135 delivery field)
# ---------------------------------------------------------------------------
# `beacon bus send / listen / receive / ack` give a CLI surface on top of the
# /bus + /bus/unread + /bus/cursors API. They are the rendezvous primitive a
# Claude Code session uses to pause until a peer posts (via harness Monitor or
# ScheduleWakeup). The CLI keeps the daemon-side simple: events are streamed
# as JSON lines to stdout, and Claude Code's Monitor tool turns each line into
# a notification.

def _bus_resolve_recipient(default_fallback: str = "") -> str:
    """Pick the recipient_id for read-side bus calls.

    Resolution order: CLI flag (BEACON_BUS_RECIPIENT) → current session_id →
    explicit fallback. Hard-error if all three are empty so a typo doesn't
    silently read events for an empty-string recipient that lives next to
    every other empty-string recipient.
    """
    rid = os.environ.get("BEACON_BUS_RECIPIENT", "").strip()
    if rid:
        return rid
    sid = _resolve_session_id()
    if sid:
        return sid
    if default_fallback:
        return default_fallback
    print(
        "bus: no recipient_id. Pass --recipient <id> or run inside a session.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Bus budget (ms-54 / e-1000) — outbound send-rate gate.
# ---------------------------------------------------------------------------
# The autonomous-DM scenario is "agent gets woken by a Monitor, sees a DM,
# composes a reply, sends, and goes back to sleep — repeat indefinitely".
# Without a structural gate that loop runs until token exhaustion or a runaway
# back-and-forth between two agents. The budget is the gate: a granted
# allowance of N outbound sends, consumed one per `beacon bus send`. When the
# count hits 0, `bus send` refuses with a non-zero exit until a human reissues
# `bus budget grant`.
#
# Storage is intentionally a local file (.beacon/bus-budget.json) rather than
# a Firestore field. Reasons:
#   * the gate is per-machine: if Mac's budget runs out, Windows shouldn't be
#     dragged into the same halt.
#   * a local file lets the harness check/decrement atomically without a
#     cloud round-trip on the hot path (every send would otherwise pay 100ms+).
#   * tamper resistance is a feature of *humans* re-granting the budget, not
#     of the file's storage — see CORE doc data-immutability-principle.
#
# Schema:
#   {"total": N, "used": M, "granted_at": "<ISO>", "channels": ["<ch>", ...]}
# `channels` is reserved for a future per-channel gate; today it's stored as
# an empty list and the budget gates all outbound sends.

def _get_bus_budget_path() -> str:
    """Resolve .beacon/bus-budget.json under the current project root."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "bus-budget.json")


def _read_bus_budget() -> Optional[dict]:
    path = _get_bus_budget_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # Treat a corrupted budget file as "no budget granted" — the gate
        # is fail-closed: sends refused until a human re-grants.
        return None


def _write_bus_budget(data: dict) -> None:
    path = _get_bus_budget_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _bus_budget_consume_one() -> tuple[bool, dict]:
    """Decrement the budget for a single outbound send.

    Returns (allowed, budget_after). When ``allowed`` is False the caller
    must NOT issue the send. ``budget_after`` is the post-decrement state,
    or the unchanged refuse-state when the budget is exhausted/missing.
    """
    b = _read_bus_budget()
    if b is None:
        # No budget file at all == autonomous mode never armed → allow
        # sends (the gate only applies when armed). Manual one-off DMs from
        # the CLI should not require granting first.
        return True, {"total": 0, "used": 0, "armed": False}
    total = int(b.get("total", 0))
    used = int(b.get("used", 0))
    if total <= 0:
        return True, {**b, "armed": False}
    if used >= total:
        return False, {**b, "armed": True}
    b["used"] = used + 1
    _write_bus_budget(b)
    return True, {**b, "armed": True}


def _record_bus_budget_trek_bypass(trek_id: str) -> None:
    """Bump the per-Trek bypass counter on the budget file (ms-75 / e-2044).

    The counter is informational — it surfaces in ``bus budget show`` so a
    leader can see "this session sent N DMs that bypassed the budget gate
    because they were Trek-internal". It does not gate or rate-limit; the
    structural rationale is that Trek scope IS the gate (= pre-approved
    blanket consent), and a counter gives observability without re-adding
    a soft cap that the SPEC explicitly rejected.

    Best-effort write: budget file absent / unreadable / unwritable all
    silently no-op (= the bypass already happened, audit is the bonus).
    """
    if not trek_id:
        return
    try:
        b = _read_bus_budget() or {
            "total": 0, "used": 0,
            "channels": [], "armed": False,
        }
        bypassed = dict(b.get("trek_bypassed") or {})
        bypassed[trek_id] = int(bypassed.get(trek_id, 0)) + 1
        b["trek_bypassed"] = bypassed
        _write_bus_budget(b)
    except Exception:
        return


def _is_trek_internal_send(recipient_sid: str) -> tuple[bool, str]:
    """Decide whether the current --in-reply-to send is Trek-internal.

    Returns ``(True, trek_id)`` iff both the caller and ``recipient_sid``
    are joined members of the same active Trek; otherwise ``(False, "")``.

    Detection material (= mirror of server-side
    ``dm_gate.should_gate_dm_action`` shared_trek_member rule):
      * caller's user_id from the cloud identity (auth.json / credentials.json)
      * recipient's user_id resolved from the project session registry
      * caller's joined active treks; intersect ``members[]`` with the
        recipient_user_id

    Best-effort: any exception or missing material returns ``(False, "")``
    so the regular budget gate stays in force. The bypass MUST NOT fire
    on a misconfigured send — that would be a silent budget relaxation.
    """
    if not recipient_sid:
        return False, ""
    if not _is_cloud_mode():
        # Local mode uses ~/.beacon/treks/; recipient session lookup
        # requires a cloud-side directory listing. Keep local mode strict
        # (= no bypass), the safer side of the SPEC.
        return False, ""
    try:
        my_user_id, _, _ = _resolve_creator_identity()
        if not my_user_id:
            return False, ""
        client, config = _get_api_client()
        project_id = _resolve_bus_project_id(config)
        # Resolve recipient session → user.
        recipient_user_id = ""
        try:
            sessions = client.list_sessions(project_id) or []
        except Exception:
            return False, ""
        for s in sessions:
            if s.get("session_id") == recipient_sid:
                actor = s.get("actor") or {}
                recipient_user_id = actor.get("user_id") or ""
                break
        if not recipient_user_id or recipient_user_id == my_user_id:
            return False, ""
        # Walk my joined active treks.
        try:
            my_treks = client.list_treks() or []
        except Exception:
            return False, ""
        for trek in my_treks:
            if trek.get("status") != "active":
                continue
            # Caller must be a joined member of this trek (= joined_at non
            # empty). Pure invitation does not grant Trek scope yet.
            am_member = False
            for m in trek.get("members") or []:
                if m.get("user_id") == my_user_id and m.get("joined_at"):
                    am_member = True
                    break
            if not am_member:
                continue
            for m in trek.get("members") or []:
                if m.get("user_id") == recipient_user_id and m.get("joined_at"):
                    return True, str(trek.get("trek_id") or "")
        return False, ""
    except Exception:
        return False, ""


def cmd_bus_budget_grant():
    """Set or refresh the outbound-send budget for autonomous mode.

    ms-76 / e-1852 structural禁止帯 (= 構造的禁止): budget grant requires a
    human (T1 envelope-equivalent) signal — Operation auto-execute (T2) is
    NOT allowed to self-escalate. The whole point of the budget is to cap
    autonomous-loop runaway. If T2 Operations could re-grant the budget,
    an AI inside a long-running Operation could write a "grant N more
    turns" Operation, schedule itself, and bypass the cap silently. We
    block the path at the CLI entry: if the process is running under
    BEACON_OPERATION_AUTO_EXECUTE=1 (= the Operation runner's marker),
    refuse with a non-zero exit and a message pointing the human to run
    the grant interactively.

    See CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope tier
    framework) 「構造的禁止」 section. Mirrors the explicit T1-only
    guarantee in ms-76 SPEC EuLwGrAawmMzeKYsxkrd 設計方針 8.
    """
    if os.environ.get("BEACON_OPERATION_AUTO_EXECUTE", "") == "1" or \
       os.environ.get("BEACON_OPERATION_ENVELOPE_ID", "").strip():
        print(
            "Error: bus budget grant is T1-only (= human-signature required).\n"
            "  This process is running under an Operation auto-execute "
            "context (T2 envelope); structural禁止帯 forbids AI self-escalation.\n"
            "  See CORE doc QvyVwRU8otQEn5iMfP36 (= AI 自律 action の envelope "
            "tier framework). Run `beacon bus budget grant --turns N` "
            "interactively (= outside the Operation runner) to refresh.",
            file=sys.stderr,
        )
        sys.exit(1)
    import datetime
    raw = os.environ.get("BEACON_BUS_BUDGET_N", "").strip()
    try:
        total = int(raw)
    except ValueError:
        print(f"Error: --turns must be an integer (got: {raw!r})",
              file=sys.stderr)
        sys.exit(1)
    if total <= 0:
        print("Error: --turns must be > 0 (use `bus budget clear` to revoke)",
              file=sys.stderr)
        sys.exit(1)
    data = {
        "total": total,
        "used": 0,
        "granted_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "channels": [],
    }
    _write_bus_budget(data)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"Budget granted: {total} outbound sends "
              f"(armed). Use `bus budget show` to inspect.")


def cmd_bus_budget_show():
    b = _read_bus_budget()
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(b or {"armed": False}, ensure_ascii=False))
        return
    if b is None:
        print("Budget: not granted (autonomous mode disabled).")
        return
    total = int(b.get("total", 0))
    used = int(b.get("used", 0))
    remaining = max(total - used, 0)
    state = "exhausted (re-grant required)" if total > 0 and used >= total else "armed"
    print(f"Budget: {used}/{total} used  →  {remaining} remaining  ({state})")
    if b.get("granted_at"):
        print(f"  granted_at: {b['granted_at']}")
    # ms-75 / e-2044 — Trek-internal bypass audit. The counter is purely
    # informational; bypassed sends did not consume from total/used. Surface
    # it here so the leader can see "the executor has been chatty within the
    # trek but did not draw down the autonomous-loop guardrail".
    bypassed = b.get("trek_bypassed") or {}
    if bypassed:
        total_bypassed = sum(int(v) for v in bypassed.values())
        print(
            f"  Trek-internal bypassed: {total_bypassed} send(s) "
            f"(did NOT count against budget — ms-75 / e-2044)"
        )
        for trek_id, n in sorted(bypassed.items()):
            print(f"    {trek_id}: {n}")


def cmd_bus_budget_clear():
    """Revoke the budget — disables autonomous outbound sends until re-granted."""
    path = _get_bus_budget_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f"Error removing budget file: {e}", file=sys.stderr)
            sys.exit(1)
    print("Budget cleared.")


# ---------------------------------------------------------------------------
# Bus auto-execute allowlist (ms-54 / e-1145) — receiver-side opt-in for the
# delivery=auto-execute mode.
# ---------------------------------------------------------------------------
# Without this allowlist, any sender can post a bus event with
# delivery=auto-execute and the receiver-side daemon has no structural way to
# refuse. The safety fallback in beacon-bus-inbox-hook.py treats
# auto-execute as propose-to-ai today, but the long-term answer the user wants
# is "default OFF, channel-level opt-in":
#
#   * project.json carries `bus_auto_execute_channels: [str, ...]`. Missing or
#     empty list ⇒ NO channel may auto-execute. Explicit, ordered, audit-able.
#   * The inbox hook reads the list and downgrades any auto-execute event whose
#     channel is not in the allowlist to propose-to-ai, while annotating the
#     context inject so the human sees "this was downgraded for safety".
#   * Adding a channel to the allowlist is itself a deliberate CLI step. We do
#     NOT auto-create the field on every save — the absence of the field is
#     the safe default; presence is the opt-in.
#
# The field lives in project.json (NOT a separate file) so it's covered by the
# v2 meta document migration (e-1209) and synced cross-machine with the rest of
# the project state.

def _bus_auto_execute_channels(data: dict) -> list:
    """Read the allowlist from a project.json dict, with type guard.

    Anything other than a list of strings is treated as an empty list — the
    safe default. We coerce on read rather than on write so a hand-edited
    project.json with the wrong shape never silently lets auto-execute slip
    through; the user sees "no channels are armed" until they re-add via CLI.
    """
    raw = data.get("bus_auto_execute_channels")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def _mirror_auto_execute_channels_to_local(channels: list) -> None:
    """Write-through mirror of the auto-execute allowlist into local
    ``.beacon/project.json``.

    Why: in cloud mode, ``save_project`` PUTs to the cloud only — the local
    file is a stale snapshot from ``beacon init`` time and is never refreshed
    on writes (StoreApi.save_project doesn't mirror). The inbox hook
    (``bin/beacon-bus-inbox-hook.py``) reads ``bus_auto_execute_channels``
    from the local file directly (= cheap, no subprocess), so without this
    mirror the hook always sees an empty allowlist and degrades every
    opted-in ``operation-trigger`` event to ``propose-to-ai`` — silently
    breaking the autonomous loop even when the CLI shows the channel as
    allowed.

    Scoped to this single config field on purpose: a full local mirror is
    ms-36 territory (cloud-first cache rethink). This is a targeted patch
    that closes the autonomous-loop UX gap without changing wider semantics.

    Fail-soft: any error (cwd not a beacon project, file unwritable, etc.)
    is swallowed — the cloud-side write already succeeded, so the user-
    facing operation is still correct; only the inbox hook's local read
    stays stale, which is the pre-existing (broken) baseline.
    """
    try:
        project_file = os.environ.get("BEACON_PROJECT_FILE") or os.path.join(
            ".beacon", "project.json")
        if not os.path.exists(project_file):
            return
        with open(project_file, "r", encoding="utf-8") as f:
            local = json.load(f)
        local["bus_auto_execute_channels"] = list(channels)
        with open(project_file, "w", encoding="utf-8") as f:
            json.dump(local, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        sys.stderr.write(
            f"[beacon] local mirror of bus_auto_execute_channels failed "
            f"(cloud write succeeded; inbox hook may read stale value): "
            f"{type(exc).__name__}: {exc}\n"
        )


def cmd_bus_auto_execute_list():
    """Show the channels allowed to auto-execute incoming bus events.

    Empty list (or field absent) ⇒ NO channel is permitted. This is the
    secure default and the CLI prints it explicitly so the user is never
    confused by silence."""
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels}, ensure_ascii=False))
        return
    if not channels:
        print("Bus auto-execute allowlist: (empty) — every auto-execute event "
              "will be downgraded to propose-to-ai.")
        return
    print("Bus auto-execute allowlist:")
    for c in channels:
        print(f"  - {c}")


def cmd_bus_auto_execute_add():
    """Add a channel to the auto-execute allowlist.

    Idempotent: re-adding a channel that's already present is a no-op (no
    duplicate, no error). The list keeps insertion order so audit logs read
    naturally."""
    channel = os.environ.get("BEACON_BUS_AUTO_EXEC_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if channel in channels:
        if os.environ.get("BEACON_JSON", "") == "1":
            print(json.dumps({"channels": channels, "added": False},
                              ensure_ascii=False))
        else:
            print(f"Channel already in allowlist: {channel}")
        return
    channels.append(channel)
    data["bus_auto_execute_channels"] = channels
    save_project(data, op={
        "op": "bus_auto_execute_add",
        "channel": channel,
    })
    _mirror_auto_execute_channels_to_local(channels)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels, "added": True},
                          ensure_ascii=False))
    else:
        print(f"Added to bus auto-execute allowlist: {channel}")


def cmd_bus_auto_execute_remove():
    """Remove a channel from the auto-execute allowlist.

    Removing a channel that isn't present is also idempotent — the desired
    end state (channel not allowed) is reached either way."""
    channel = os.environ.get("BEACON_BUS_AUTO_EXEC_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    data = load_project()
    channels = _bus_auto_execute_channels(data)
    if channel not in channels:
        if os.environ.get("BEACON_JSON", "") == "1":
            print(json.dumps({"channels": channels, "removed": False},
                              ensure_ascii=False))
        else:
            print(f"Channel not in allowlist (no-op): {channel}")
        return
    channels = [c for c in channels if c != channel]
    data["bus_auto_execute_channels"] = channels
    save_project(data, op={
        "op": "bus_auto_execute_remove",
        "channel": channel,
    })
    _mirror_auto_execute_channels_to_local(channels)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps({"channels": channels, "removed": True},
                          ensure_ascii=False))
    else:
        print(f"Removed from bus auto-execute allowlist: {channel}")


def cmd_bus_send():
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    if not channel:
        print("Error: --channel <name> required", file=sys.stderr)
        sys.exit(1)
    payload_raw = os.environ.get("BEACON_BUS_PAYLOAD", "")
    payload: dict = {}
    if payload_raw:
        try:
            parsed = json.loads(payload_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --payload must be JSON ({e})", file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --payload must be a JSON object", file=sys.stderr)
            sys.exit(1)
        payload = parsed
    sender = os.environ.get("BEACON_BUS_SENDER", "").strip() or _resolve_session_id()
    delivery = os.environ.get("BEACON_BUS_DELIVERY", "").strip() or "propose-to-ai"
    in_reply_to = os.environ.get("BEACON_BUS_IN_REPLY_TO", "").strip()
    # e-1209: --to <session_id> stamps payload.recipient_session_id so the
    # server-side filter in /bus/unread can route the event to a single
    # recipient. Without this, `dm`-channel events fan out to every session
    # in the project (the historical bug). MCP reply tool (channel/bus.mjs)
    # already stamps this field; aligning the CLI sender closes the gap.
    #
    # The CLI does NOT require --to for non-DM channels — broadcast remains
    # the default semantics there. For DM the server drops unaddressed events
    # rather than broadcasting, so a `dm` send without --to is effectively
    # a no-op (drops everywhere). We surface a warning rather than hard-error
    # so existing scripts that rely on legacy broadcast behavior get loud
    # feedback instead of silent message loss.
    recipient = os.environ.get("BEACON_BUS_RECIPIENT_SESSION", "").strip()
    if recipient:
        # Caller-supplied --to overrides any payload.recipient_session_id
        # set by --payload; the flag is the unambiguous source of truth.
        payload = {**payload, "recipient_session_id": recipient}
    elif channel == "dm" and not payload.get("recipient_session_id"):
        print(
            "Warning: sending to channel 'dm' without --to <session_id>. "
            "After e-1209 the server drops unaddressed DM events rather than "
            "broadcasting them — pass --to or use a different channel for "
            "broadcasts.",
            file=sys.stderr,
        )

    # e-1000: budget gate.
    #
    # The gate applies ONLY when `--in-reply-to` is set, i.e. this send is an
    # AI-authored reply to a specific incoming event. The inbox hook
    # instructs the AI to always include the flag when replying, so any
    # autonomous loop necessarily passes through the gate. Manual one-off
    # CLI sends (no --in-reply-to) bypass the gate — the human typing the
    # command IS the approval.
    #
    # When the gate applies:
    #   * no budget granted at all → REFUSE. Default state is "AI cannot
    #     auto-reply"; the human must explicitly `bus budget grant N` to
    #     authorize N auto-replies. This matches the user's safety stance:
    #     "until a turn limit is explicitly set, replies require human
    #     approval."
    #   * budget granted but exhausted → REFUSE. Same path as above; the
    #     human must re-grant.
    #   * budget granted and remaining > 0 → decrement, then send. The
    #     decrement happens *before* the cloud call so a network failure
    #     can't smuggle an extra send past the gate.
    budget: dict = {"armed": False}
    if in_reply_to:
        # ms-75 / e-2044: Trek-internal sends bypass the budget gate. Trek
        # is an opt-in pre-approval scope (= 缶詰の作業部屋), so the budget
        # cap (= runaway-autonomy guardrail) is structurally redundant for
        # member-to-member DMs within an active Trek both sides have
        # joined. server/dm_gate.should_gate_dm_action already short-
        # circuits the receiver-side cross-user gate for shared_trek_member
        # pairs (ms-70 / e-1854); this client-side check brings the budget
        # layer into parity so a Trek doesn't deadlock on budget exhaustion
        # while leader/executor DMs keep flowing under blanket consent.
        # The check is best-effort: any error path (no auth, network blip,
        # unknown recipient) falls through to the regular budget gate so we
        # never silently relax enforcement on a misconfigured send.
        trek_bypass, bypass_trek_id = _is_trek_internal_send(recipient)
        if trek_bypass:
            budget = {
                "armed": True,
                "trek_bypass": True,
                "trek_id": bypass_trek_id,
            }
            _record_bus_budget_trek_bypass(bypass_trek_id)
        else:
            b = _read_bus_budget()
            if b is None:
                print(
                    "Error: this is a reply (--in-reply-to set) but no auto-reply "
                    "budget is granted. Default state requires human approval per "
                    "reply. Run `beacon bus budget grant <N>` to authorize N "
                    "auto-replies, or omit --in-reply-to for a manual send.",
                    file=sys.stderr,
                )
                sys.exit(1)
            allowed, budget = _bus_budget_consume_one()
            if not allowed:
                total = int(budget.get("total", 0))
                used = int(budget.get("used", 0))
                print(
                    f"Error: auto-reply budget exhausted ({used}/{total} used). "
                    "Run `beacon bus budget grant <N>` to re-grant before "
                    "sending again.",
                    file=sys.stderr,
                )
                sys.exit(1)

    if in_reply_to:
        # Thread the reply by stamping the parent event_id on the payload.
        # Server-side schema treats payload as opaque; this is a convention
        # the inbox hook can present back to the AI on subsequent rounds.
        payload = {**payload, "in_reply_to": in_reply_to}

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override
    # e-1340 follow-up (= structural defense against the 2026-06-09 silent
    # cross-project misroute incident): if --to points at a session that
    # polls a different project than `project_id`, auto-route to that
    # project. Loud stderr notice keeps the override auditable.
    project_id = _validate_recipient_project(recipient, project_id, channel)
    # e-1402: liveness gate on the sid itself (independent of project
    # routing). Catches stale session_id reuse when the Skill is bypassed.
    _check_recipient_live_health(recipient, channel)

    # e-1290: envelope-by-default for CLI sends.
    #
    # Tier selection rule: `beacon bus send` is invoked by a human typing a
    # command in their shell, so every send carries an implicit human
    # signature → tier T1. `--action <name>` (repeatable, comma-joined by the
    # bash wrapper) populates actions_authorized; with no flag the list is
    # empty (T1 with no specific delegations is still a valid envelope and
    # unlocks `propose-to-ai` delivery on the receive side).
    #
    # Fallback strategy:
    #   * `--no-envelope` (BEACON_BUS_NO_ENVELOPE=1) skips issuance entirely.
    #     Useful for debugging the server's legacy / backward-compat path.
    #   * Issuance returns HTTP 404 (older server without /bus/envelope/issue)
    #     → log + fall back to legacy POST. This keeps newer clients deployable
    #     against unmigrated servers.
    #   * Issuance returns HTTP 400 (server rejected our request — e.g. a
    #     high-risk action under a tier the server forbids, or a wildcard)
    #     → log + fall back. The user still gets their message through; the
    #     receiver simply won't auto-execute, which is the safe default.
    #   * Transport / 5xx errors also fall through silently — bus must not
    #     break for the user just because the envelope path misbehaves.
    envelope_obj: dict | None = None
    requested_action: str | None = None
    no_envelope = os.environ.get("BEACON_BUS_NO_ENVELOPE", "") == "1"
    if not no_envelope:
        actions_raw = os.environ.get("BEACON_BUS_ACTION", "").strip()
        actions_authorized = [
            a.strip() for a in actions_raw.split(",") if a.strip()
        ] if actions_raw else []
        # When the user passes a single --action we forward it as the
        # message's requested_action too; the envelope's actions_authorized
        # is the *capability grant* (what the receiver may auto-run), and
        # requested_action is the *request* (what this message is asking
        # for). For T1 these line up; for the wider model they need not.
        if len(actions_authorized) == 1:
            requested_action = actions_authorized[0]
        try:
            envelope_obj = client.issue_bus_envelope(
                project_id,
                tier="T1",
                actions_authorized=actions_authorized,
                data_class="free",
            )
        except RuntimeError as e:
            # api_client wraps HTTPError as `RuntimeError("API error CODE: ...")`.
            # 404 = legacy server, 400 = rejected payload — both fall back to
            # the legacy POST path. Any other shape (transport, 5xx) also
            # falls through; we log but don't surface so the user's send
            # still lands.
            msg = str(e)
            if "API error 404" in msg or "API error 400" in msg:
                print(
                    f"Note: envelope issuance unavailable ({msg.split(':', 1)[0]});"
                    " falling back to legacy bus path.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Note: envelope issuance error, falling back to legacy"
                    f" bus path: {msg}",
                    file=sys.stderr,
                )
            envelope_obj = None
            requested_action = None
        except (ConnectionError, OSError) as e:
            print(
                f"Note: envelope issuance network error, falling back to"
                f" legacy bus path: {e}",
                file=sys.stderr,
            )
            envelope_obj = None
            requested_action = None

    event = client.post_bus_event(
        project_id, channel,
        sender_session_id=sender,
        payload=payload,
        delivery=delivery,
        envelope=envelope_obj,
        requested_action=requested_action,
    )
    if os.environ.get("BEACON_JSON", "") == "1":
        # Augment the event JSON with the post-decrement budget so scripted
        # callers can decide whether to keep the autonomous loop running.
        out = dict(event)
        if budget.get("armed"):
            out["_budget"] = {
                "total": int(budget.get("total", 0)),
                "used": int(budget.get("used", 0)),
                "remaining": max(int(budget.get("total", 0))
                                  - int(budget.get("used", 0)), 0),
            }
        print(json.dumps(out, ensure_ascii=False))
        return
    line = (
        f"Sent: [{event.get('event_id', '?')}] {channel} "
        f"from={sender or '(anonymous)'} delivery={event.get('delivery', delivery)}"
    )
    if budget.get("armed"):
        total = int(budget.get("total", 0))
        used = int(budget.get("used", 0))
        remaining = max(total - used, 0)
        line += f"  (budget: {used}/{total}, {remaining} remaining)"
    print(line)


def _fetch_pending_dm_lookup(client, project_id: str) -> dict:
    """Fetch the project-scoped pending sidecar map for inline banner emission.

    Used by ``cmd_bus_listen`` / ``cmd_bus_receive`` (ms-70 / e-1715) to
    decide whether to print a banner before each event. Returns
    ``{event_id: row}`` (= via ``lib/dm_pending.build_pending_lookup``).

    All failures (= cloud unconfigured, endpoint absent on a server
    older than e-1714, transient network blip, auth missing) collapse
    to an empty dict — the CLI's existing event-stream output keeps
    flowing untouched. This is the explicit AC requirement: sidecar
    lookup failure MUST NOT break legacy listen / receive behaviour.

    We deliberately do NOT pass ``receiver_user_id`` because resolving
    the local user_id at this point would require touching the auth
    layer (= ``.beacon/cloud.json`` / ``~/.beacon/auth.json``) which
    has multiple resolution orders. The endpoint is membership-gated,
    so the empty receiver_user_id case returns all project-scoped
    pending rows; the banner only fires for events that actually appear
    in *this* recipient's unread feed, which is the natural filter.
    """
    try:
        from dm_pending import build_pending_lookup
    except Exception:
        return {}
    try:
        rows = client.get(
            f"/api/projects/{project_id}/dm/pending"
            f"?receiver_user_id=&limit=200"
        )
    except Exception:
        # Silent: this includes 404 (older server), 401 (no token),
        # 5xx (transient), and OSError (no network).
        return {}
    if not isinstance(rows, list):
        return {}
    try:
        return build_pending_lookup(rows)
    except Exception:
        return {}


def _print_event_with_banner(ev: dict, pending_lookup: dict) -> None:
    """Print a single bus event JSON line, optionally preceded by the
    pending-DM banner.

    ms-70 / e-1715: if ``ev['event_id']`` is in ``pending_lookup`` (= the
    dispatcher gate set ``approval_status="pending"`` for this envelope),
    emit a structurally recognizable banner line first so the AI session
    consuming the stream stops and asks the user before acting on the
    envelope's actions_authorized.

    Banner formatting / contract lives in ``lib/dm_pending.py``. This
    function is intentionally a thin wrapper so the legacy "just print
    JSON" behaviour stays one line away in a diff.
    """
    if pending_lookup:
        eid = ev.get("event_id")
        if eid and eid in pending_lookup:
            try:
                from dm_pending import format_inline_dm_banner
                row = pending_lookup[eid]
                banner = format_inline_dm_banner(
                    eid,
                    sender_user_id=row.get("sender_user_id", ""),
                    created_at=row.get("created_at", ""),
                )
                print(banner, flush=True)
            except Exception:
                # If formatting fails for any reason, fall back to the
                # plain JSON line. The AC says "do not break legacy
                # output"; missing banner is worse than missing JSON.
                pass
    print(json.dumps(ev, ensure_ascii=False), flush=True)


def cmd_bus_listen():
    """Long-poll /bus/unread and stream each event as one JSON line on stdout.

    Designed for Claude Code's Monitor tool: each stdout line becomes one
    notification. Without explicit acknowledgement (``--auto-ack``) events
    stay readable so a crashing consumer can replay; with auto-ack, the
    cursor advances to the last event of each batch after print.

    The loop ends only on SIGINT or when the optional ``--once`` mode has
    delivered a batch. There is no implicit timeout — callers wanting one
    should use ``beacon bus receive --timeout`` instead.

    ms-70 / e-1715: before printing each event, look up its sidecar
    ``approval_status``. If pending, emit an inline banner so the AI
    session reading the stream stops and asks the user before acting.
    Sidecar lookup failures (= older server / no auth / network) silently
    fall back to the legacy "just JSON" output — the banner is added on
    top of the existing contract, not in place of it.
    """
    import time
    recipient = _bus_resolve_recipient()
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    auto_ack = os.environ.get("BEACON_BUS_AUTO_ACK", "") == "1"
    once = os.environ.get("BEACON_BUS_ONCE", "") == "1"
    interval = float(os.environ.get("BEACON_BUS_INTERVAL", "2") or "2")
    if interval < 0.25:
        interval = 0.25  # don't hammer the server

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    try:
        while True:
            events = client.list_unread_bus_events(
                project_id, recipient, channel=channel,
            )
            if events:
                # e-1715: refresh sidecar lookup once per batch so a
                # newly-approved row in the middle of a poll loop stops
                # banner emission on the very next poll. Per-event
                # lookups would be more accurate but multiply HTTP cost
                # by N — batch granularity is the right tradeoff.
                pending_lookup = _fetch_pending_dm_lookup(client, project_id)
                for ev in events:
                    _print_event_with_banner(ev, pending_lookup)
                if auto_ack:
                    last_ts = events[-1].get("created_at", "")
                    if last_ts:
                        client.advance_bus_cursor(project_id, recipient, last_ts)
                if once:
                    return
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def cmd_bus_receive():
    """Block until a single batch of events arrives (or ``--timeout`` elapses).

    ms-70 / e-1715: same inline pending-DM banner injection as
    ``cmd_bus_listen``. ``beacon bus receive`` is the one-shot variant
    used by ``beacon-bus-inbox-hook.py`` and ad-hoc scripts; treating
    its output identically keeps the AI gate contract (= banner-then-event)
    uniform across the two CLI entry points.
    """
    import time
    recipient = _bus_resolve_recipient()
    channel = os.environ.get("BEACON_BUS_CHANNEL", "").strip()
    auto_ack = os.environ.get("BEACON_BUS_AUTO_ACK", "") == "1"
    timeout_raw = os.environ.get("BEACON_BUS_TIMEOUT", "0")
    try:
        timeout = float(timeout_raw or "0")
    except ValueError:
        timeout = 0.0
    interval = float(os.environ.get("BEACON_BUS_INTERVAL", "2") or "2")
    if interval < 0.25:
        interval = 0.25

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    started = time.monotonic()
    while True:
        events = client.list_unread_bus_events(
            project_id, recipient, channel=channel,
        )
        if events:
            pending_lookup = _fetch_pending_dm_lookup(client, project_id)
            for ev in events:
                _print_event_with_banner(ev, pending_lookup)
            if auto_ack:
                last_ts = events[-1].get("created_at", "")
                if last_ts:
                    client.advance_bus_cursor(project_id, recipient, last_ts)
            return
        if timeout > 0 and (time.monotonic() - started) >= timeout:
            # Exit 2 distinguishes "timeout, no events" from "error" (1) and
            # "got events" (0) so a calling script can branch on the outcome.
            sys.exit(2)
        time.sleep(interval)


def cmd_bus_ack():
    """Advance the recipient's cursor explicitly. Forward-only — older values
    are silent no-ops on the server side.

    Two input modes (exactly one required):
      * ``--last-seen-at <iso8601>``  Pass the cursor target directly.
      * ``--event <event_id>``        Look up the event server-side and
        advance the cursor past its ``created_at``. Used by
        ``/beacon-operation-execute`` as a forcing function (e-1423, ms-54
        Bug 4 第 2 層): Skill completion structurally clears the triggering
        event from the next inbox-hook inject, so the same op-X event is
        never replayed even if the session_id rotates (= Bug 5 root cause).
    """
    recipient = _bus_resolve_recipient()
    last_seen_at = os.environ.get("BEACON_BUS_LAST_SEEN_AT", "").strip()
    event_id = os.environ.get("BEACON_BUS_ACK_EVENT_ID", "").strip()

    if last_seen_at and event_id:
        print("Error: pass either --last-seen-at or --event, not both",
              file=sys.stderr)
        sys.exit(1)
    if not last_seen_at and not event_id:
        print("Error: --last-seen-at <iso8601> or --event <event_id> required",
              file=sys.stderr)
        sys.exit(1)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # e-1151: --project override

    if event_id:
        # Resolve created_at from the server. The event row carries the
        # canonical timestamp, so the Skill never has to format ISO8601
        # itself (= fewer ways to get the ack wrong).
        try:
            event = client.get_bus_event(project_id, event_id)
        except Exception as e:
            msg = str(e)
            if "404" in msg:
                print(f"Error: bus event {event_id!r} not found in project "
                      f"{project_id!r}", file=sys.stderr)
                sys.exit(1)
            print(f"Error: {msg}", file=sys.stderr)
            sys.exit(1)
        last_seen_at = (event or {}).get("created_at", "").strip()
        if not last_seen_at:
            print(f"Error: bus event {event_id!r} has no created_at "
                  f"(cannot derive cursor target)", file=sys.stderr)
            sys.exit(1)

    result = client.advance_bus_cursor(project_id, recipient, last_seen_at)
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Cursor: {recipient} → {result.get('last_seen_at', '(unchanged)')}")


def cmd_bus_status():
    """Render the 3-stage receipt view for a single bus event (ms-54 / e-1348).

    `sent` is always present (the row exists ⇒ the server stamped created_at).
    `delivered` and `opened` are set by channel/bus.mjs receipts and may be
    absent — the renderer marks them as "(not yet)" so the sender can localize
    exactly where a DM stalled:

      * sent ✓ / delivered ✗ / opened ✗ → bridge never polled / event filtered
        before /unread returned it (e.g. cursor already past it)
      * sent ✓ / delivered ✓ / opened ✗ → bridge fetched but the event got
        dropped at the filter chain (channel allowlist, self-sent, mis-addressed)
      * sent ✓ / delivered ✓ / opened ✓ → harness saw the channel push; the AI
        has structurally seen the content

    JSON mode emits the raw event dict — the same shape callers get from
    GET /api/projects/{id}/bus/{event_id} — so scripts can pivot on the
    receipt fields directly.
    """
    event_id = os.environ.get("BEACON_BUS_EVENT_ID", "").strip()
    if not event_id:
        print("Usage: beacon bus status <event_id> [--project <id>] [--json]",
              file=sys.stderr)
        sys.exit(2)
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    try:
        event = client.get_bus_event(project_id, event_id)
    except Exception as e:
        # api_client raises a plain Exception with the HTTP status message.
        # 404 is the common case (typo / event GC'd); surface it as a single
        # line rather than a Python traceback.
        msg = str(e)
        if "404" in msg:
            print(f"Error: bus event {event_id!r} not found in project "
                  f"{project_id!r}", file=sys.stderr)
            sys.exit(1)
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(event, ensure_ascii=False))
        return

    # Human view. Stage rows align so a quick eye-scan over multiple events
    # locks onto the missing checkmark.
    sent_at = event.get("created_at", "")
    delivered_at = event.get("delivered_at", "")
    delivered_by = event.get("delivered_by", "")
    opened_at = event.get("opened_at", "")
    opened_by = event.get("opened_by", "")

    def _row(mark, label, ts, by=""):
        ts_str = ts if ts else "(not yet)"
        by_str = f"  by {by}" if by else ""
        return f"  {mark} {label:<10} {ts_str}{by_str}"

    channel = event.get("channel", "")
    delivery = event.get("delivery", "")
    sender = event.get("sender_session_id", "")
    payload = event.get("payload", {})

    print(f"event: {event_id}")
    print(f"  channel: {channel}  delivery: {delivery}")
    print(f"  sender:  {sender}")
    try:
        print(f"  payload: {json.dumps(payload, ensure_ascii=False)}")
    except Exception:
        print(f"  payload: {payload!r}")
    print("  receipt:")
    print(_row("✓", "sent", sent_at))
    print(_row("✓" if delivered_at else "✗", "delivered", delivered_at,
               delivered_by))
    print(_row("✓" if opened_at else "✗", "opened", opened_at, opened_by))


def cmd_dm_respond():
    """Receiver-side decision CLI for a pending DM-action envelope (e-1716).

    Usage shape (= what the user types in their terminal):
      beacon dm respond approve <event_id>
      beacon dm respond deny    <event_id>

    SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ"):
    this primitive is reached by a human typing the command, never by an
    autonomous AI loop. The server stamps ``decision_by`` from the Bearer
    token's ``sub`` claim — the CLI cannot forge a different actor.

    Args flow through env vars set by ``bin/beacon`` dispatch:
      * ``BEACON_DM_DECISION``  = "approve" | "deny"
      * ``BEACON_DM_EVENT_ID``  = sidecar event_id (= parent bus_event id)
      * ``BEACON_BUS_PROJECT_ID`` = optional --project override (same
        semantics as bus subcmd)
      * ``BEACON_JSON`` = "1" emits the resulting 7-field sidecar row as
        JSON instead of a human summary

    Exit codes:
      * 0 — decision accepted (or idempotent no-op for "same user resubmits
        same decision")
      * 1 — server reported a structural error (404 unknown event_id, 403
        not your envelope, 409 already-decided / auto)
      * 2 — bad CLI usage (missing args)
    """
    decision = os.environ.get("BEACON_DM_DECISION", "").strip().lower()
    event_id = os.environ.get("BEACON_DM_EVENT_ID", "").strip()

    if decision not in ("approve", "deny"):
        print(
            "Usage: beacon dm respond approve <event_id>\n"
            "       beacon dm respond deny    <event_id>\n"
            "  Decide a pending cross-user DM action envelope. Only the\n"
            "  intended receiver (= the addressee on the sidecar) can press\n"
            "  approve/deny; the server enforces this via the Bearer token.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not event_id:
        print(
            "Error: <event_id> required.\n"
            "  Example: beacon dm respond approve evt-abc12345",
            file=sys.stderr,
        )
        sys.exit(2)

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)  # respects --project override

    try:
        row = client.respond_dm_approval(
            project_id, event_id, decision=decision
        )
    except Exception as e:
        # api_client raises RuntimeError("API error <code>: <detail>") on
        # HTTP errors. Surface a single human line — not a stacktrace —
        # because the human is staring at this terminal.
        msg = str(e)
        if "404" in msg:
            print(
                f"Error: no pending approval for event_id={event_id!r} "
                f"in project {project_id!r}.\n"
                "  Either the envelope was already auto-allowed (legacy "
                "path), the event_id is wrong, or the sidecar has not\n"
                "  landed yet.",
                file=sys.stderr,
            )
        elif "403" in msg:
            print(
                f"Error: event_id={event_id!r} is addressed to a different "
                "user; only the intended receiver can decide it.\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        elif "409" in msg:
            print(
                f"Error: event_id={event_id!r} is already in a terminal "
                "state (approved / denied / auto).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(row, ensure_ascii=False))
        return

    # Human-readable summary. Verb + 7-field row in compact form.
    verb_past = "approved" if decision == "approve" else "denied"
    print(f"{verb_past}: event_id={row.get('event_id', event_id)}")
    print(f"  status:      {row.get('approval_status', '')}")
    print(f"  decision_by: {row.get('decision_by', '')}")
    print(f"  decision_at: {row.get('decision_at', '')}")
    print(f"  sender:      {row.get('sender_user_id', '')}")
    print(f"  receiver:    {row.get('receiver_user_id', '')}")


def cmd_dm_log():
    """Audit-history CLI for decided DM-action sidecars (ms-70 / e-1923).

    Usage shape (= what the user types in their terminal):
      beacon dm log [<project_id>] [--limit N] [--json] [--project <id>]

    This is the CLI mirror of the Web UI Settings > Audit table that
    landed in e-1718 (commit 4c5052f). Both surfaces read the same
    server endpoint ``GET /api/projects/{pid}/dm/approval/history`` and
    show the same 6 fields (status / sender / receiver / decision_at /
    decision_by / event_id) — the Web UI as a styled table, the CLI as
    a fixed-column ASCII view sized for a terminal.

    SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ"):
    this view is read-only by design. The server filters out ``pending``
    and ``auto`` rows so a future contributor cannot wire an
    approve / deny button onto this output.

    Args flow through env vars set by ``bin/beacon`` / ``dispatch.py``:
      * ``BEACON_DM_LOG_PROJECT_ID`` = optional positional project_id
        (= the cloud project to read history from; defaults to the
        cwd's project)
      * ``BEACON_DM_LOG_LIMIT`` = optional row limit (defaults to 50,
        server caps at 500)
      * ``BEACON_BUS_PROJECT_ID`` = ``--project <id>`` override (same
        semantics as the bus / dm respond subcmds)
      * ``BEACON_JSON`` = "1" emits the raw rows list as JSON instead
        of the 6-column ASCII table

    Exit codes:
      * 0 — rows printed (zero rows is success, surfaces an empty-state
        line so a tail of "no decisions yet" is distinguishable from a
        broken pipe)
      * 1 — server reported a structural error (membership 403, 404, etc.)
      * 2 — bad CLI usage (currently unreachable: all flags have defaults)
    """
    # Positional project_id (if any) wins over the cwd default but loses
    # to an explicit --project override (= same precedence as bus subcmds).
    positional_pid = os.environ.get("BEACON_DM_LOG_PROJECT_ID", "").strip()
    limit_raw = os.environ.get("BEACON_DM_LOG_LIMIT", "").strip()
    try:
        limit = int(limit_raw) if limit_raw else 50
    except ValueError:
        print(
            f"Error: --limit must be an integer, got {limit_raw!r}",
            file=sys.stderr,
        )
        sys.exit(2)
    if limit <= 0:
        limit = 50

    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    if not project_id and positional_pid:
        project_id = positional_pid
    elif positional_pid and not os.environ.get("BEACON_BUS_PROJECT_ID", "").strip():
        # Positional arg supplied without --project override: positional wins
        # over cwd default. (= matches the help-banner shape.)
        project_id = positional_pid
    if not project_id:
        print(
            "Error: no project_id resolved.\n"
            "  Run from a beacon project cwd, pass `beacon dm log <project_id>`,\n"
            "  or set --project <id>.",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        rows = client.list_dm_approval_history(project_id, limit=limit)
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            print(
                f"Error: not a member of project {project_id!r} "
                "(audit history is membership-gated).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        elif "404" in msg:
            print(
                f"Error: project {project_id!r} not found "
                "(check the project_id).\n"
                f"  ({msg})",
                file=sys.stderr,
            )
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(rows or [], ensure_ascii=False))
        return

    if not rows:
        # Match the Web UI's empty-state copy (renderDmApprovalHistoryRows)
        # so the audit narrative reads the same across both surfaces.
        print("承認履歴はまだありません (= 過去に approve / deny された DM action がない)。")
        return

    # 6-column ASCII table. Widths chosen for typical 100-col terminals:
    # status is short (approved / denied), decision_at is fixed-ish ISO8601,
    # the event_id / user_id columns get truncated with an ellipsis so a
    # too-long Google-OAuth-style sub doesn't wreck the layout. Order
    # matches the AC: event_id / sender / receiver / status / decision_at /
    # decision_by.
    def _trunc(s, n):
        s = "" if s is None else str(s)
        if len(s) <= n:
            return s
        # 3-char ellipsis fits inside the column width; using a single
        # ASCII period × 3 keeps copy-paste safe (no multibyte width math).
        return s[: max(0, n - 3)] + "..."

    # Column widths (= total ~98 chars + 5 separator spaces = ~103). Tweak
    # here, not in renderer logic, so a future column-width A/B test stays
    # readable.
    W_EVT, W_SEN, W_REC, W_STA, W_DEC_AT, W_DEC_BY = 18, 22, 22, 9, 20, 22
    header = (
        f"{'event_id':<{W_EVT}}  "
        f"{'sender':<{W_SEN}}  "
        f"{'receiver':<{W_REC}}  "
        f"{'status':<{W_STA}}  "
        f"{'decision_at':<{W_DEC_AT}}  "
        f"{'decision_by':<{W_DEC_BY}}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)
    for r in rows:
        evt = _trunc(r.get("event_id", ""), W_EVT)
        sen = _trunc(r.get("sender_user_id", ""), W_SEN)
        rec = _trunc(r.get("receiver_user_id", ""), W_REC)
        sta = _trunc(r.get("approval_status", ""), W_STA)
        dat = _trunc(r.get("decision_at", ""), W_DEC_AT)
        dby = _trunc(r.get("decision_by", ""), W_DEC_BY)
        print(
            f"{evt:<{W_EVT}}  "
            f"{sen:<{W_SEN}}  "
            f"{rec:<{W_REC}}  "
            f"{sta:<{W_STA}}  "
            f"{dat:<{W_DEC_AT}}  "
            f"{dby:<{W_DEC_BY}}"
        )
    print(sep)
    print(f"{len(rows)} row(s).")


def _resolve_bus_project_id(config: dict) -> str:
    """Return the project_id the bus call should target.

    Resolution order (ms-54 e-1151):
      1. ``BEACON_BUS_PROJECT_ID`` env var — set by the dispatcher when
         the user passed ``--project <id>``. This lets a Beacon session
         post into / read from another project's bus (e.g. Mac Beacon
         session DMs a TrailNode session) without flipping cwd.
      2. ``config["project_id"]`` — the default, derived from
         ``.beacon/cloud.json`` of the current project.
    """
    override = os.environ.get("BEACON_BUS_PROJECT_ID", "").strip()
    if override:
        return override
    return config.get("project_id", "")


def _validate_recipient_project(
    recipient: str, target_project_id: str, channel: str,
):
    """Cross-project pre-flight for ``beacon bus send --to <recipient>``.

    Today's dogfood (2026-06-09) surfaced a silent-misroute pattern: the
    sender's CLI posts to its cwd's project bus, but the recipient is
    polling a different project. The event is stored but never delivered.
    Without this check, the only signal is the absence of a ``delivered``
    receipt — a negative signal humans (and AI) routinely miss.

    Resolution policy:
      * Non-DM channels skip (broadcast semantics, no fixed recipient).
      * Empty recipient skips (already warned about elsewhere).
      * If the recipient is found in the target project's local directory
        — pass through, no change.
      * If found in a *different* local project — auto-route to that
        project. Loud stderr warning so the routing decision is auditable.
      * If not found in any local project — soft warn (might be a remote
        bridge we can't see locally) but don't refuse.

    Returns the project_id to actually use. May differ from
    ``target_project_id`` when auto-routing.

    Opt-outs (in this order):
      * ``BEACON_BUS_SKIP_TO_CHECK=1`` — bypass entirely (CI / scripts
        that have already validated routing).
      * ``BEACON_BUS_NO_AUTO_ROUTE=1`` — keep the check, but refuse to
        auto-route on mismatch (emits a hard error so the script breaks
        instead of silently switching project).
    """
    if channel != "dm" or not recipient:
        return target_project_id
    if os.environ.get("BEACON_BUS_SKIP_TO_CHECK", "") == "1":
        return target_project_id

    # Import lazily so a missing dm_discover module (older install) just
    # bypasses the check rather than breaking `bus send`.
    try:
        # Ensure beacon_cli is importable; pip / dev-clone layouts vary.
        import importlib
        helpers = importlib.import_module(
            "beacon_cli.skills_helpers.dm_discover"
        )
    except Exception:
        return target_project_id

    try:
        rows = helpers.discover_and_aggregate(healthy=False, since_min=10)
    except Exception:
        # Discovery itself failed (psutil missing / network hiccup). Fall
        # back to no-op rather than refusing the send — defensive guard
        # mustn't become a footgun for unrelated errors.
        return target_project_id

    same_project: dict | None = None
    other_project: dict | None = None
    for row in rows:
        if row.get("session_id") != recipient:
            continue
        if row.get("project_id") == target_project_id:
            same_project = row
            break
        if other_project is None:
            other_project = row

    if same_project is not None:
        return target_project_id

    if other_project is not None:
        other_pid = other_project.get("project_id", "?")
        if os.environ.get("BEACON_BUS_NO_AUTO_ROUTE", "") == "1":
            print(
                f"Error: recipient session {recipient!r} polls project "
                f"{other_pid!r}, not the target {target_project_id!r}. "
                f"Pass --project {other_pid} explicitly, or unset "
                f"BEACON_BUS_NO_AUTO_ROUTE to let the send auto-route.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"⚠ recipient session {recipient[:24]}… polls project "
            f"{other_pid!r}, not the target {target_project_id!r}. "
            f"Auto-routing this DM via --project {other_pid}. "
            f"(Set BEACON_BUS_SKIP_TO_CHECK=1 to bypass this check, "
            f"or BEACON_BUS_NO_AUTO_ROUTE=1 to make the mismatch a "
            f"hard error.)",
            file=sys.stderr,
        )
        return other_pid

    # Not found in any locally-visible bridge. Could be a remote session
    # (different machine) — we can't disprove that here. Soft warn only.
    print(
        f"⚠ recipient session {recipient[:24]}… is not visible in any "
        f"locally-running bridge. Send proceeding against project "
        f"{target_project_id!r} as-is; verify the receipt afterwards "
        f"with `beacon bus status <event_id>`.",
        file=sys.stderr,
    )
    return target_project_id


def _check_recipient_live_health(recipient: str, channel: str) -> None:
    """Defense-in-depth gate (e-1402) against stale session_id reuse.

    2026-06-10 LPS dogfood incident: an AI in another project bypassed the
    /beacon-dm-send Skill (which forces a fresh `dm_discover` lookup per
    send) and reused a session_id from conversation memory. The target had
    shutdown 1 hour earlier; the DM landed in a dead session. The Skill's
    safety net was structural but only effective when the Skill was used.

    This helper closes that hole at the CLI surface: any `--to <sid>` send
    on the dm channel triggers a live+healthy directory check. If the sid
    isn't currently held by a polling bridge, emit a loud stderr warning
    naming the Skill as the recommended remediation. Soft-warn only — the
    send proceeds, since CLI immediacy is the legitimate use case (CI
    scripts, automated tooling, debugging). Hard-refuse is an opt-in for
    future strictness.

    Distinct from ``_validate_recipient_project`` (e-1362) which asks
    *which project does this sid poll?* — that's about cross-project
    routing. This one asks *is this sid alive right now?* — that's about
    session lifecycle. Same directory backend, different semantics, kept
    as separate helpers per the "thin single-purpose" convention.

    Opt-out:
      * ``BEACON_BUS_NO_LIVE_CHECK=1`` — bypass entirely (CI / automated
        scripts that handle their own liveness verification).
    """
    if channel != "dm" or not recipient:
        return
    if os.environ.get("BEACON_BUS_NO_LIVE_CHECK", "") == "1":
        return

    try:
        import importlib
        helpers = importlib.import_module(
            "beacon_cli.skills_helpers.dm_discover"
        )
    except Exception:
        # No discovery module available — bypass silently. Mirrors the
        # defensive posture of _validate_recipient_project; a missing
        # helper shouldn't break the send.
        return

    try:
        rows = helpers.discover_and_aggregate(healthy=True, since_min=10)
    except Exception:
        # Discovery raised (network / psutil / auth) — bypass rather than
        # turning a transient failure into a CLI footgun.
        return

    for row in rows:
        if row.get("session_id") == recipient:
            return  # live + healthy, all good

    # Not in the live+healthy set. The session might be down (= dead) or
    # simply not visible to this machine's bridge directory. Either way,
    # the sender should know that the send is best-effort.
    print(
        f"⚠ recipient session {recipient[:24]}… is not in the live+healthy "
        f"directory. The send will proceed, but the target may be dead or "
        f"unreachable. Consider `/beacon-dm-send` Skill which re-discovers "
        f"on every send. (Set BEACON_BUS_NO_LIVE_CHECK=1 to suppress.)",
        file=sys.stderr,
    )


def cmd_bus_directory():
    """Look up live sessions for DM target selection (ms-54 / e-1134 / e-1151).

    Wraps GET /sessions with the directory-query filters. The output is
    intentionally human-pickable (one line per session showing session_id +
    actor identity + last_active) — a sender reads this, picks a session_id,
    and passes it as the recipient for `bus send`. JSON mode is for scripts
    that want to auto-route (e.g. "send to every live agent of user X").

    ``--project <id>`` (env ``BEACON_BUS_PROJECT_ID``) lets the caller list
    sessions of a different project. The auth + API URL still come from
    the current project's cloud.json; only the project_id in the API call
    changes.

    ``--healthy`` (env ``BEACON_DIR_HEALTHY``, ms-54 e-1318): only return
    sessions whose bridge poll loop is actively pumping events into the AI
    inbox right now. Opt-in so existing callers' wire shape is unchanged.
    The JSON output ALWAYS includes the ``poll_health`` block per row (even
    without --healthy) so scripts can decide their own threshold.
    """
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    sessions = client.list_sessions(
        project_id,
        user_id=os.environ.get("BEACON_DIR_USER", "").strip(),
        machine=os.environ.get("BEACON_DIR_MACHINE", "").strip(),
        agent=os.environ.get("BEACON_DIR_AGENT", "").strip(),
        live_only=os.environ.get("BEACON_DIR_LIVE", "") == "1",
        since_minutes=int(os.environ.get("BEACON_DIR_SINCE_MIN", "5") or "5"),
        healthy_only=os.environ.get("BEACON_DIR_HEALTHY", "") == "1",
    )
    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(sessions, ensure_ascii=False))
        return
    if not sessions:
        print("(no matching sessions)")
        return
    for s in sessions:
        actor = s.get("actor") or {}
        sid = s.get("session_id", "?")
        email = actor.get("email", "")
        machine = actor.get("machine", "")
        agent = actor.get("agent", "")
        last = (s.get("last_active") or "")[:19]
        ident = " / ".join(p for p in (email, machine, agent) if p) or "(anon)"

        # e-1318: surface poll_health in the human picker so users can
        # tell at a glance "this session's bridge is actually polling"
        # vs. "the heartbeat code path ran but the bridge is dead".
        ph = s.get("poll_health") or {}
        healthy = ph.get("healthy")
        age = ph.get("age_seconds")
        shutdown = ph.get("shutdown")
        if shutdown:
            health_tag = "  health=shutdown"
        elif healthy is True:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=ok({age_str})"
        elif healthy is False:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=stale({age_str})"
        else:
            health_tag = "  health=unknown"
        print(f"  {sid}  {ident}  last_active={last}{health_tag}")


def cmd_profile_list():
    """List Beacon profiles found under ~/.beacon/profiles/ (ms-64 / e-1461).

    Each profile is a directory containing at least a credentials.json (created
    by ``beacon auth login --profile <name>``) and optionally a profile.json
    with ``api_url`` + ``backend_type`` overrides. The active profile is
    starred so the user can confirm which one would be picked by the current
    --profile / BEACON_PROFILE / cwd .beacon/cloud.json combination.

    Output respects ``BEACON_JSON=1`` for scripting; otherwise prints a
    human-readable list.
    """
    import profile as _profile  # local import: heavy module pulled lazily

    try:
        names = _profile.list_profiles()
    except Exception as exc:
        print(f"Error listing profiles: {exc}")
        sys.exit(1)

    # Resolve active profile name (does not require credentials to exist).
    try:
        active = _profile.resolve_profile_name()
    except Exception:
        active = ""

    if os.environ.get("BEACON_JSON", "") == "1":
        rows = []
        for name in names:
            try:
                p = _profile.load_profile(name)
                rows.append({
                    "name": name,
                    "api_url": p.api_url,
                    "credentials_exists": p.credentials_exist(),
                    "active": name == active,
                })
            except Exception:
                rows.append({"name": name, "active": name == active})
        print(json.dumps(rows, ensure_ascii=False))
        return

    if not names:
        print("(no profiles found)")
        print("Tip: `beacon auth login` will create the 'default' profile.")
        return

    for name in names:
        marker = "*" if name == active else " "
        try:
            p = _profile.load_profile(name)
            creds = "✓" if p.credentials_exist() else " "
            print(f" {marker} {creds} {name:<20} {p.api_url}")
        except Exception:
            print(f" {marker}   {name:<20} (could not load)")


# ---------------------------------------------------------------------------
# Stop signal CLI (ms-55 e-1646)
# ---------------------------------------------------------------------------
#
# `beacon stop` is the user-facing entry point for the halt protocol
# defined in lib/stop_signal.py. It rides on the existing bus event
# transport — the stop event is just another bus event on the
# `stop-signal` channel. The CLI's job is to:
#
#   1. Resolve the issuer's session_id (so the receipt is auditable).
#   2. Build a validated payload via stop_signal.build_stop_payload.
#   3. Post the event via the same api_client used by bus_send.
#
# Authorization mirrors SPEC §2: anyone can broadcast. We do NOT gate
# on envelope tier here — the human typing `beacon stop` is the human
# approval, and AI sessions that auto-emit a stop pass through the
# normal autonomous-action accounting. The halt itself is enforced on
# the receive side (the inbox hook for AI sessions, future Tauri UI for
# human-attended sessions).

def _stop_post_event(*, payload: dict, channel: str = None) -> dict:
    """Common transport for stop/resume events.

    Wraps the same client.post_bus_event call used by cmd_bus_send,
    but without the envelope-issue path: stop signals are explicitly
    not gated by tier (SPEC §2), so a T0 / no-envelope post is the
    correct wire form. This also makes the CLI usable from sessions
    that can't reach the envelope-issue endpoint (= offline, legacy
    server, etc.) — stopping a runaway must not fail because the
    auxiliary endpoint is down.
    """
    from stop_signal import STOP_CHANNEL  # local import to avoid cycle
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    sender = payload.get("issued_by_session_id", "")
    event = client.post_bus_event(
        project_id, channel or STOP_CHANNEL,
        sender_session_id=sender,
        payload=payload,
        delivery="propose-to-ai",
        envelope=None,
        requested_action=None,
    )
    return event


def cmd_stop_scoped():
    """Broadcast a scoped stop signal.

    Env:
      BEACON_STOP_TARGET_KIND   "ms" | "task" | "session" (required)
      BEACON_STOP_TARGET_ID     id of the target (required)
      BEACON_STOP_REASON        free text (optional)
      BEACON_STOP_REASON_KIND   one of stop_signal.REASON_KINDS (optional)
      BEACON_STOP_MACHINE_REASON  optional JSON-encoded dict
      BEACON_JSON               "1" → json output
    """
    import stop_signal as _stop

    target_kind = os.environ.get("BEACON_STOP_TARGET_KIND", "").strip()
    target_id = os.environ.get("BEACON_STOP_TARGET_ID", "").strip()
    reason = os.environ.get("BEACON_STOP_REASON", "")
    reason_kind = os.environ.get("BEACON_STOP_REASON_KIND", "").strip() or "manual"
    machine_reason_raw = os.environ.get("BEACON_STOP_MACHINE_REASON", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not target_kind or not target_id:
        print(
            "Error: scoped stop requires --target <kind>:<id> "
            "(kind = ms|task|session)",
            file=sys.stderr,
        )
        sys.exit(1)

    machine_reason = None
    if machine_reason_raw:
        try:
            parsed = json.loads(machine_reason_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --machine-reason must be valid JSON ({e})",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --machine-reason must be a JSON object",
                  file=sys.stderr)
            sys.exit(1)
        machine_reason = parsed

    sender = _resolve_session_id()
    if not sender:
        print(
            "Error: cannot resolve current session_id (run `beacon session id` "
            "to mint one, or set BEACON_BUS_SENDER explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        payload = _stop.build_stop_payload(
            scope=_stop.SCOPE_SCOPED,
            issued_by_session_id=sender,
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
            reason_kind=reason_kind,
            machine_reason=machine_reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    suffix = f" — {reason}" if reason else ""
    print(
        f"STOP signal (scoped) raised on {target_kind}:{target_id} "
        f"by {sender}{suffix}"
    )
    print(f"  event_id: {event.get('event_id', '?')}")
    print("  All matching sessions will halt after the current tool call.")
    print(f"  Resume with: beacon resume scoped --target {target_kind}:{target_id}")


def cmd_stop_global():
    """Broadcast a global stop signal (= halt every active autonomous
    session in the project).

    Env:
      BEACON_STOP_REASON        free text (recommended)
      BEACON_STOP_REASON_KIND   one of stop_signal.REASON_KINDS (optional)
      BEACON_STOP_MACHINE_REASON  optional JSON-encoded dict
      BEACON_JSON               "1" → json output
    """
    import stop_signal as _stop

    reason = os.environ.get("BEACON_STOP_REASON", "")
    reason_kind = os.environ.get("BEACON_STOP_REASON_KIND", "").strip() or "manual"
    machine_reason_raw = os.environ.get("BEACON_STOP_MACHINE_REASON", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    machine_reason = None
    if machine_reason_raw:
        try:
            parsed = json.loads(machine_reason_raw)
        except json.JSONDecodeError as e:
            print(f"Error: --machine-reason must be valid JSON ({e})",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(parsed, dict):
            print("Error: --machine-reason must be a JSON object",
                  file=sys.stderr)
            sys.exit(1)
        machine_reason = parsed

    sender = _resolve_session_id()
    if not sender:
        print(
            "Error: cannot resolve current session_id "
            "(set BEACON_BUS_SENDER explicitly)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        payload = _stop.build_stop_payload(
            scope=_stop.SCOPE_GLOBAL,
            issued_by_session_id=sender,
            reason=reason,
            reason_kind=reason_kind,
            machine_reason=machine_reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    suffix = f" — {reason}" if reason else ""
    print(f"STOP signal (GLOBAL) raised by {sender}{suffix}")
    print(f"  event_id: {event.get('event_id', '?')}")
    print("  All active autonomous sessions will halt after the current tool call.")
    print("  Resume with: beacon resume global")


def cmd_stop_status():
    """List currently active stop signals.

    Reads recent events on the stop-signal channel via list_unread_bus_events
    (with a synthetic empty recipient so the server returns the full
    channel history rather than a per-recipient cursor view). This avoids
    consuming any real recipient's cursor — `stop status` is observational
    and must not race with receivers.

    Env:
      BEACON_JSON               "1" → json output
      BEACON_STOP_SINCE_HOURS   limit window (default 24)
    """
    import stop_signal as _stop

    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        client, config = _get_api_client()
    except Exception as e:
        print(f"Error: cannot reach bus ({e})", file=sys.stderr)
        sys.exit(1)
    project_id = _resolve_bus_project_id(config)

    # Pull the recent stop-signal channel history. We use a synthetic
    # recipient so we don't advance any real session's cursor; the
    # server returns the channel slice (most backends honor that).
    try:
        events = client.list_unread_bus_events(
            project_id, "_stop_status_observer",
            channel=_stop.STOP_CHANNEL,
            limit=500,
        )
    except TypeError:
        # Older api_client signature without `limit`. The default page
        # size is typically generous enough for a stop-status snapshot.
        events = client.list_unread_bus_events(
            project_id, "_stop_status_observer",
            channel=_stop.STOP_CHANNEL,
        )
    except Exception as e:
        print(f"Error: cannot list stop-signal events ({e})", file=sys.stderr)
        sys.exit(1)

    actives = _stop.latest_active_stops(events or [])

    if json_mode:
        # Strip raw_event for compactness — callers who need the full event
        # can call `beacon bus receive --channel stop-signal --once`.
        out = []
        for rec in actives:
            r = {k: v for k, v in rec.items() if k != "raw_event"}
            out.append(r)
        print(json.dumps(out, ensure_ascii=False))
        return

    if not actives:
        print("No active stop signals.")
        return

    print(f"Active stop signals ({len(actives)}):")
    for rec in actives:
        scope = rec["scope"]
        target = rec.get("target") or {}
        if scope == "global":
            tag = "GLOBAL"
        else:
            tag = f"{target.get('kind', '?')}:{target.get('id', '?')}"
        line = (
            f"  ⚠ {tag}  reason_kind={rec.get('reason_kind', '?')}  "
            f"by={rec.get('issued_by_session_id', '?')}  "
            f"at={rec.get('issued_at', '?')}"
        )
        print(line)
        if rec.get("reason"):
            print(f"      reason: {rec['reason']}")


def cmd_resume_scoped():
    """Broadcast a resume (= clear stop) for a scoped target.

    Env mirrors cmd_stop_scoped (without machine_reason).
    """
    import stop_signal as _stop

    target_kind = os.environ.get("BEACON_STOP_TARGET_KIND", "").strip()
    target_id = os.environ.get("BEACON_STOP_TARGET_ID", "").strip()
    reason = os.environ.get("BEACON_STOP_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not target_kind or not target_id:
        print(
            "Error: scoped resume requires --target <kind>:<id>",
            file=sys.stderr,
        )
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _stop.build_resume_payload(
            scope=_stop.SCOPE_SCOPED,
            issued_by_session_id=sender,
            target_kind=target_kind,
            target_id=target_id,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(
        f"RESUME signal (scoped) raised on {target_kind}:{target_id} "
        f"by {sender}"
    )
    print(f"  event_id: {event.get('event_id', '?')}")


# ---------------------------------------------------------------------------
# Rollback CLI (ms-55 e-1647)
# ---------------------------------------------------------------------------
#
# `beacon rollback` is the SPEC §4 "safe boundary" surface: it undoes
# work that lives entirely inside the local repo (working tree edits,
# un-pushed commits) automatically, and refuses to touch anything past
# the upstream branch. Anything past upstream becomes a "compensation
# proposal" in the report — concrete next-step text like "open a
# revert PR" rather than silent destructive action.

def _rollback_record_history(plan, result, reason: str) -> dict:
    """Record a `beacon rollback` execution as a save entry on the active MS.

    ms-55 e-1727: after `beacon rollback` mutates the working tree (=
    non-dry-run, no fatal errors), persist a structured trail so
    `beacon search "rollback"` surfaces "when did we roll back, what
    did we touch, what compensation was proposed". Without this trail
    the runaway story is lost — stash refs survive but project history
    has no pointer to them.

    The entry uses source="rollback" so the search query is single-keyword,
    and the description packs reason + commit hashes + working-tree count +
    compensation hints. Returns the {status, entry_id, milestone} dict
    from save_entry, or {"status": "error", "error": ...} on failure.
    Failures are non-fatal — the rollback already succeeded.
    """
    try:
        import operations as _ops  # noqa: PLC0415
    except Exception as e:
        return {"status": "error", "error": f"import operations: {e}"}

    state = plan.state
    head_hash = state.head_hash or "?"
    branch = state.branch or "(detached HEAD)"
    upstream = state.upstream_branch or "(no upstream)"

    parts: list[str] = []
    parts.append(f"rollback on {branch} (HEAD~={head_hash}, upstream={upstream})")
    if reason:
        parts.append(f"reason: {reason}")
    if result.stashed:
        parts.append(
            f"stashed {len(state.working_tree_files)} working-tree path(s) → "
            f"{result.stash_ref or 'stash@{0}'}"
        )
    if result.reset_commits > 0:
        parts.append(f"reset {result.reset_commits} local commit(s) (--soft)")
    if not result.stashed and result.reset_commits == 0:
        parts.append("(no-op: clean tree, no local commits)")
    if plan.pushed_warning:
        parts.append("⚠ rollback request extended past upstream — report-only")
    for opt in plan.compensation_options:
        parts.append(f"compensation: {opt}")

    description = "; ".join(parts)

    def op(d):
        return d, core.save_entry(
            d,
            ms_id="",  # auto-target the active MS
            description=description,
            source="rollback",
            date="",
            hash=head_hash,
        )

    try:
        project_id = _project_id_for_ops()
        return _ops.apply_operation(
            project_id, op,
            op_name="rollback.record",
            reason=reason or "rollback record",
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}


def cmd_rollback():
    """Inspect local git state and roll back the safe portion.

    Env:
      BEACON_ROLLBACK_COMMITS    int (default 0 = auto)
      BEACON_ROLLBACK_REASON     free text, recorded into stash msg + report
      BEACON_ROLLBACK_DRY_RUN    "1" → show plan, don't mutate
      BEACON_ROLLBACK_CWD        override cwd (mainly for tests)
      BEACON_ROLLBACK_NO_RECORD  "1" → skip the history save entry (= e-1727
                                 escape hatch for tests / no-op rollbacks)
      BEACON_JSON                "1" → json output (plan + result)
    """
    import rollback as _rb

    commits_raw = os.environ.get("BEACON_ROLLBACK_COMMITS", "0").strip()
    try:
        commits = int(commits_raw)
    except ValueError:
        print(
            f"Error: --commits must be an integer (got {commits_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    reason = os.environ.get("BEACON_ROLLBACK_REASON", "")
    dry_run = os.environ.get("BEACON_ROLLBACK_DRY_RUN", "") == "1"
    cwd = os.environ.get("BEACON_ROLLBACK_CWD", "").strip() or None
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    plan, result, report = _rb.rollback(
        cwd=cwd,
        commits=commits,
        reason=reason,
        dry_run=dry_run,
    )

    # ms-55 e-1727: also record on the non-error JSON path. We compute
    # the record first so it appears in the JSON payload.
    record_info: Optional[dict] = None
    if (
        not dry_run
        and result is not None
        and os.environ.get("BEACON_ROLLBACK_NO_RECORD", "") != "1"
        and (result.stashed or result.reset_commits > 0
             or plan.compensation_options)
    ):
        record_info = _rollback_record_history(plan, result, reason)

    if json_mode:
        out = {
            "plan": {
                "stash_working_tree": plan.stash_working_tree,
                "reset_commits": plan.reset_commits,
                "pushed_warning": plan.pushed_warning,
                "compensation_options": list(plan.compensation_options),
                "reason": plan.reason,
                "requested_commits": plan.requested_commits,
                "state": {
                    "head_hash": plan.state.head_hash,
                    "branch": plan.state.branch,
                    "upstream_branch": plan.state.upstream_branch,
                    "local_commits_ahead": plan.state.local_commits_ahead,
                    "working_tree_dirty": plan.state.working_tree_dirty,
                    "working_tree_files": list(plan.state.working_tree_files),
                },
            },
            "dry_run": dry_run,
        }
        if result is not None:
            out["result"] = {
                "stashed": result.stashed,
                "stash_ref": result.stash_ref,
                "reset_commits": result.reset_commits,
                "errors": list(result.errors),
            }
        if record_info is not None:
            out["record"] = record_info
        print(json.dumps(out, ensure_ascii=False))
        if result is not None and result.errors:
            sys.exit(1)
        return

    print(report, end="")
    if dry_run:
        print("(dry run — nothing executed. Re-run without --dry-run to apply.)")
        return

    if result is None:
        # Defensive: rollback() should always return a result when
        # dry_run=False. Falling through means something inside the
        # helper changed shape; surface so it gets caught early.
        print("Error: rollback executor returned no result.", file=sys.stderr)
        sys.exit(1)

    summary_bits = []
    if result.stashed:
        summary_bits.append(f"stashed ({result.stash_ref or 'stash@{0}'})")
    if result.reset_commits > 0:
        summary_bits.append(f"reset {result.reset_commits} commit(s)")
    if not summary_bits:
        summary_bits.append("nothing to do")
    print(f"Executed: {', '.join(summary_bits)}")

    # ms-55 e-1727: surface the history record outcome. The save itself
    # was already attempted in the shared pre-print block above; here we
    # just report what happened. Non-fatal — the rollback already
    # succeeded; the trail being broken is annoying, not catastrophic.
    if record_info is not None:
        if record_info.get("status") == "saved":
            print(
                f"Recorded: {record_info.get('entry_id', '?')} → "
                f"{record_info.get('milestone', '?')}"
            )
        elif record_info.get("status") == "duplicate":
            print(f"Recorded: (duplicate) → {record_info.get('milestone', '?')}")
        elif record_info.get("status") == "error":
            print(
                f"Warning: could not record rollback history: "
                f"{record_info.get('error', 'unknown')}",
                file=sys.stderr,
            )

    if result.errors:
        print("", file=sys.stderr)
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Claim CLI (ms-55 e-1648)
# ---------------------------------------------------------------------------
#
# `beacon claim` is the user-facing surface for the claim primitives
# defined in lib/claims.py. Three issuance verbs share most of the
# argument shape (`--target`, `--intent`), so the implementation
# factors the common path through a private helper.

def _claims_register_provider() -> None:
    """Wire lib/claims.py's cloud provider hook to this CLI's transport
    (ms-55 e-1730).

    Called lazily by each claim command so import-time of lib/claims.py
    stays side-effect-free. Re-registers on every call (cheap, idempotent)
    — handy if the user switches cloud mode mid-session.

    The provider closure returns ``(client, project_id)`` when cloud
    mode is active, or ``None`` to fall back to local. Errors during
    resolution map to ``None`` so the worker keeps working.
    """
    import claims as _claims

    def _provider():
        try:
            if not _is_cloud_mode():
                return None
            client, config = _get_api_client()
        except Exception:
            return None
        project_id = (config or {}).get("project_id") or ""
        if not project_id:
            return None
        return client, project_id

    _claims.register_cloud_provider(_provider)


def _claim_parse_target_env() -> tuple[str, str]:
    """Pull --target kind:id from env vars set by the bash dispatcher."""
    tk = os.environ.get("BEACON_CLAIM_TARGET_KIND", "").strip()
    ti = os.environ.get("BEACON_CLAIM_TARGET_ID", "").strip()
    if not tk or not ti:
        print(
            "Error: --target <kind>:<id> is required "
            "(kind = ms|task|operation|trek|free)",
            file=sys.stderr,
        )
        sys.exit(1)
    return tk, ti


def _claim_post_event(payload: dict) -> dict:
    """Common transport for claim / response / release events.

    Same shape as _stop_post_event: bypass the envelope-issue path
    because claim signals are coordination, not tier-gated capability
    grants — broadcasting "I'm picking up this task" should not require
    the auxiliary endpoint to be up.
    """
    import claims as _claims
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    sender = payload.get("from_session_id", "")
    event = client.post_bus_event(
        project_id, _claims.CLAIM_CHANNEL,
        sender_session_id=sender,
        payload=payload,
        delivery="propose-to-ai",
        envelope=None,
        requested_action=None,
    )
    return event


def _claim_issue(claim_kind: str):
    """Shared body for `beacon claim request|handoff|post`."""
    import claims as _claims

    tk, ti = _claim_parse_target_env()
    intent = os.environ.get("BEACON_CLAIM_INTENT", "")
    to_sid = os.environ.get("BEACON_CLAIM_TO", "").strip()
    expires_at = os.environ.get("BEACON_CLAIM_EXPIRES_AT", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_claim_payload(
            claim_kind=claim_kind,
            from_session_id=sender,
            target_kind=tk,
            target_id=ti,
            intent=intent,
            to_session_id=to_sid,
            expires_at=expires_at,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    # Persist the issuer's view of the claim. Cloud-mode rounds through
    # the server subcollection (= multi-machine view) + the local cache
    # mirror; local-mode just writes the cache (ms-55 e-1730).
    _claims_register_provider()
    try:
        _claims.record_claim(payload)
    except (OSError, ValueError) as e:
        # Don't fail the send if persistence is broken — the bus event
        # is the canonical record, this store is a derived view.
        print(f"Note: could not persist claim: {e}",
              file=sys.stderr)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return

    kind_label = {
        "request": "REQUEST (recipient must accept)",
        "handoff": "HANDOFF (recipient must accept)",
        "claim": "CLAIM (broadcast, first-publisher-wins)",
    }.get(claim_kind, claim_kind.upper())
    print(f"{kind_label} on {tk}:{ti} by {sender}")
    print(f"  claim_id: {payload['claim_id']}")
    print(f"  event_id: {event.get('event_id', '?')}")
    if to_sid:
        print(f"  recipient: {to_sid}")
    if intent:
        print(f"  intent: {intent}")
    print(f"  Release with: beacon claim release {payload['claim_id']}")


def cmd_claim_request():
    """beacon claim request --target <k>:<id> --to <sid> [--intent ...]"""
    _claim_issue("request")


def cmd_claim_handoff():
    """beacon claim handoff --target <k>:<id> --to <sid> [--intent ...]"""
    _claim_issue("handoff")


def cmd_claim_post():
    """beacon claim post --target <k>:<id> [--intent ...]

    Broadcast a claim (= "I'm taking X"). First-publisher-wins; no
    recipient consent needed.
    """
    _claim_issue("claim")


def cmd_claim_respond():
    """beacon claim respond <claim_id> --accept|--decline [--reason ...]"""
    import claims as _claims

    claim_id = os.environ.get("BEACON_CLAIM_ID", "").strip()
    decision = os.environ.get("BEACON_CLAIM_DECISION", "").strip()
    reason = os.environ.get("BEACON_CLAIM_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not claim_id:
        print("Error: claim_id is required", file=sys.stderr)
        sys.exit(1)
    if decision not in ("accept", "decline"):
        print("Error: pass --accept or --decline", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_response_payload(
            claim_id=claim_id,
            decision=decision,
            from_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    label = "ACCEPTED" if decision == "accept" else "DECLINED"
    print(f"{label} claim {claim_id} by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


def cmd_claim_release():
    """beacon claim release <claim_id> [--outcome completed|abandoned] [--reason ...]"""
    import claims as _claims

    claim_id = os.environ.get("BEACON_CLAIM_ID", "").strip()
    outcome = os.environ.get("BEACON_CLAIM_OUTCOME", "completed").strip() or "completed"
    reason = os.environ.get("BEACON_CLAIM_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not claim_id:
        print("Error: claim_id is required", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_release_payload(
            claim_id=claim_id,
            outcome=outcome,
            from_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    # Drop the local cache entry + cloud-mode subcollection record
    # (ms-55 e-1730). Same fall-back rule as the issue path.
    _claims_register_provider()
    try:
        _claims.release_claim(claim_id)
    except OSError as e:
        print(f"Note: could not update claim cache: {e}",
              file=sys.stderr)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(f"RELEASED claim {claim_id} ({outcome}) by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


def _morning_save_briefing_doc(briefing_text: str, briefing) -> dict:
    """Save the morning briefing as a `scope=report` doc (ms-55 e-1733).

    Lets the user re-read past briefings via Web UI Documents tab (or
    `beacon doc show`) without having to scroll terminal history. The
    daily cadence makes each briefing a small artifact worth preserving.

    Title is ISO-prefixed for chronological listing. Body is the rendered
    text wrapped in a code fence + a one-line meta header so the doc
    stands alone as a search target.

    Returns {"status": "saved", "doc_id": ..., "title": ...} or
    {"status": "error", "error": ...}. Failures are non-fatal — the
    terminal briefing already printed.
    """
    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415
    import datetime as _datetime  # noqa: PLC0415

    now = _dt.now(_tz.utc)
    iso_min = now.strftime("%Y-%m-%dT%H:%M")
    title = f"morning briefing {iso_min}Z"

    counts = briefing.counts or {}
    summary_bits = []
    for b in ("completed", "halted", "skipped", "needs_attention"):
        summary_bits.append(f"{b}={counts.get(b, 0)}")
    meta_line = (
        f"window: {briefing.since or '(start)'} → {briefing.until or '(now)'}  "
        f"counts: {' / '.join(summary_bits)}"
    )

    body = (
        f"# morning briefing — {iso_min}Z\n\n"
        f"{meta_line}\n\n"
        f"```\n{briefing_text.rstrip()}\n```\n"
    )

    scope = "report"

    try:
        # Frontmatter aligns with cmd_doc_add so Web UI / list filters
        # recognise the scope = report tag.
        content = _add_frontmatter(body, scope, "", "", "")
    except Exception as e:
        return {"status": "error", "error": f"frontmatter: {e}"}

    today_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if _is_cloud_mode():
            client, config = _get_api_client()
            result = client.create_document(
                config["project_id"], title, content,
            )
            doc_id = result["doc_id"]
        else:
            docs_dir = _get_docs_dir()
            os.makedirs(docs_dir, exist_ok=True)
            # Slug + minute resolution → collision-free across the day
            # while still distinguishable. Adds an "-Z" suffix to
            # mirror the title's UTC marker.
            slug = _doc_slug(f"morning-briefing-{iso_min}-Z")
            doc_id = slug
            fpath = os.path.join(docs_dir, f"{doc_id}.md")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        return {"status": "error", "error": f"persist: {e}"}

    # Record an MS entry pointing at the saved doc — same shape as
    # cmd_doc_add (= source=auto, revision_id=doc_id) so the briefing
    # appears in the active MS timeline. Failures are non-fatal.
    try:
        import operations as _ops  # noqa: PLC0415

        def op(d):
            return d, core.save_entry(
                d, ms_id="",
                description=f"doc add: {title} ({scope})",
                source="auto", date=today_iso,
                revision_id=doc_id,
            )
        project_id = _project_id_for_ops()
        _ops.apply_operation(
            project_id, op,
            op_name="morning.briefing_doc",
            reason="morning briefing snapshot",
        )
    except Exception as e:
        # The doc itself was written — we just couldn't link it from
        # the MS timeline. Surface as warning only.
        return {
            "status": "saved",
            "doc_id": doc_id,
            "title": title,
            "warning": f"could not link to MS timeline: {e}",
        }

    return {"status": "saved", "doc_id": doc_id, "title": title}


def cmd_morning():
    """beacon morning (ms-55 e-1650 / e-1733): 4-category summary of recent
    autonomous activity, plus auto-save as a report doc.

    Reads events from the bus (channels: stop-signal + claim-signal)
    over a window (default = last 12 hours) and buckets them as:

      ✓ 完了 (Completed):   claim releases with outcome=completed
      ⚠ 停止 (Halted):      stop signals other than STUCK
      ✗ Skip:               claim releases with outcome=abandoned
      ⏱ 介入要望 (Needs attention): STUCK signals (= idle timeout)

    The briefing is also persisted as a `scope=report` doc so the user
    can re-read past briefings via Web UI Documents tab. Skip the save
    with BEACON_MORNING_NO_DOC=1 (= --no-doc).

    Env:
      BEACON_MORNING_SINCE_HOURS  default 12
      BEACON_MORNING_EVENTS_FILE  optional path to a JSON array of
                                  events (= testing / replay path)
      BEACON_MORNING_NO_DOC       "1" → skip the report doc save (e-1733)
      BEACON_JSON                 "1" → JSON output
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import morning as _morning

    since_hours_raw = os.environ.get("BEACON_MORNING_SINCE_HOURS", "12")
    events_file = os.environ.get("BEACON_MORNING_EVENTS_FILE", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        since_hours = float(since_hours_raw)
    except ValueError:
        print(
            f"Error: --since-hours must be a number (got {since_hours_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    if since_hours <= 0:
        print("Error: --since-hours must be > 0", file=sys.stderr)
        sys.exit(1)

    now = _dt.now(_tz.utc)
    since = now - _td(hours=since_hours)

    events: list[dict] = []
    if events_file:
        try:
            with open(events_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Error: could not read events file: {e}",
                  file=sys.stderr)
            sys.exit(1)
        if not isinstance(data, list):
            print("Error: events file must be a JSON array",
                  file=sys.stderr)
            sys.exit(1)
        events = data
    else:
        # Pull recent stop + claim channel events from the bus. Use a
        # synthetic observer recipient so we don't advance anyone's
        # cursor (= same trick as cmd_stop_status).
        try:
            client, config = _get_api_client()
            project_id = _resolve_bus_project_id(config)
        except Exception as e:
            print(
                f"Error: cannot reach bus to gather events ({e}).\n"
                "If running offline, pass --events-file <path> with a "
                "JSON dump of events.",
                file=sys.stderr,
            )
            sys.exit(1)
        for channel in ("stop-signal", "claim-signal"):
            try:
                batch = client.list_unread_bus_events(
                    project_id, "_morning_observer",
                    channel=channel, limit=500,
                )
            except TypeError:
                batch = client.list_unread_bus_events(
                    project_id, "_morning_observer", channel=channel,
                )
            except Exception as e:
                print(
                    f"Note: could not fetch {channel} events ({e}); "
                    "continuing with partial data.",
                    file=sys.stderr,
                )
                continue
            if batch:
                events.extend(batch)

    briefing = _morning.build_briefing(events, since=since, until=now)
    briefing_text = _morning.render_briefing(briefing)

    # ms-55 e-1733: save the briefing as a `scope=report` doc so the
    # user can re-read past briefings from the Web UI Documents tab
    # (or via `beacon doc show`). Opt out with --no-doc.
    record_info: Optional[dict] = None
    if os.environ.get("BEACON_MORNING_NO_DOC", "") != "1":
        record_info = _morning_save_briefing_doc(briefing_text, briefing)

    if json_mode:
        out = {
            "since": briefing.since,
            "until": briefing.until,
            "counts": briefing.counts,
            "entries": [
                {
                    "bucket": e.bucket,
                    "title": e.title,
                    "detail": e.detail,
                    "at": e.at,
                    "source": e.source,
                    "ref": e.ref,
                }
                for e in briefing.entries
            ],
        }
        if record_info is not None:
            out["doc"] = record_info
        print(json.dumps(out, ensure_ascii=False))
        return

    print(briefing_text, end="")
    if record_info is not None:
        if record_info.get("status") == "saved":
            warn = record_info.get("warning")
            line = (
                f"\nSaved briefing as doc: {record_info.get('doc_id', '?')} "
                f"({record_info.get('title', '?')})"
            )
            print(line)
            if warn:
                print(f"  warning: {warn}", file=sys.stderr)
        elif record_info.get("status") == "error":
            print(
                f"\nWarning: could not save briefing doc: "
                f"{record_info.get('error', 'unknown')}",
                file=sys.stderr,
            )


def cmd_stuck_check():
    """beacon stuck check (ms-55 e-1649): scan session telemetry, emit
    STUCK signals for sessions idle past the timeout.

    Input modes (exactly one):
      * BEACON_STUCK_TELEMETRY_FILE — path to a JSON file with a list
        of telemetry records. Each record needs at least session_id +
        last_active; ms_id / task_id / ignore are optional.
      * BEACON_STUCK_TELEMETRY_INLINE — JSON string with the same shape.

    The JSON shape:
      [
        {"session_id": "sv-A", "last_active": "2026-06-15T11:00:00Z",
         "ms_id": "ms-55", "task_id": "e-1646"},
        {"session_id": "sv-B", "last_active": "2026-06-15T11:55:00Z"},
        ...
      ]

    Other env:
      BEACON_STUCK_TIMEOUT_MINUTES   default 30
      BEACON_STUCK_DRY_RUN          "1" → identify stuck sessions but
                                    don't emit signals
      BEACON_JSON                   "1" → JSON output (list of records
                                    {session_id, last_active, payload?, event?})
    """
    from datetime import datetime as _dt, timezone as _tz
    import stuck_detect as _stuck

    inline = os.environ.get("BEACON_STUCK_TELEMETRY_INLINE", "").strip()
    file_path = os.environ.get("BEACON_STUCK_TELEMETRY_FILE", "").strip()
    timeout_raw = os.environ.get("BEACON_STUCK_TIMEOUT_MINUTES",
                                 str(_stuck.DEFAULT_TIMEOUT_MINUTES))
    dry_run = os.environ.get("BEACON_STUCK_DRY_RUN", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    try:
        timeout_minutes = int(timeout_raw)
    except ValueError:
        print(
            f"Error: --timeout-minutes must be an integer "
            f"(got {timeout_raw!r})",
            file=sys.stderr,
        )
        sys.exit(1)
    if timeout_minutes <= 0:
        print("Error: --timeout-minutes must be > 0", file=sys.stderr)
        sys.exit(1)

    raw = ""
    if inline and file_path:
        print(
            "Error: pass either --telemetry-inline or --telemetry-file, "
            "not both",
            file=sys.stderr,
        )
        sys.exit(1)
    if inline:
        raw = inline
    elif file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            print(f"Error: could not read telemetry file: {e}",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print(
            "Error: provide --telemetry-inline '<json>' "
            "or --telemetry-file <path>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: telemetry payload is not valid JSON ({e})",
              file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, list):
        print("Error: telemetry payload must be a JSON array",
              file=sys.stderr)
        sys.exit(1)

    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        rows.append(_stuck.SessionTelemetry(
            session_id=row.get("session_id", "") or "",
            last_active=row.get("last_active", "") or "",
            ms_id=row.get("ms_id", "") or "",
            task_id=row.get("task_id", "") or "",
            ignore=bool(row.get("ignore", False)),
        ))

    now = _dt.now(_tz.utc)
    try:
        stuck_rows = _stuck.find_stuck_sessions(
            rows, now=now, timeout_minutes=timeout_minutes,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id() or "_stuck_detector"

    results = []
    for t in stuck_rows:
        payload = _stuck.build_stuck_signal(
            t, issued_by_session_id=sender, now=now,
            timeout_minutes=timeout_minutes,
        )
        entry = {
            "session_id": t.session_id,
            "last_active": t.last_active,
            "ms_id": t.ms_id,
            "task_id": t.task_id,
            "payload": payload,
        }
        if not dry_run:
            try:
                ev = _stop_post_event(payload=payload)
                entry["event"] = ev
            except Exception as e:
                # Don't let a single send failure prevent the rest of
                # the batch from being reported — partial coverage is
                # still useful for the morning briefing.
                entry["error"] = str(e)
        results.append(entry)

    if json_mode:
        print(json.dumps({
            "dry_run": dry_run,
            "timeout_minutes": timeout_minutes,
            "stuck": results,
        }, ensure_ascii=False))
        return

    if not results:
        print(
            f"No stuck sessions (timeout = {timeout_minutes} min, "
            f"scanned {len(rows)} session(s))."
        )
        return

    print(f"Stuck sessions detected ({len(results)}):")
    for r in results:
        suffix = "  (dry-run, no signal emitted)" if dry_run else ""
        print(
            f"  ⚠ {r['session_id']}  last_active={r['last_active']}  "
            f"ms={r['ms_id'] or '-'}  task={r['task_id'] or '-'}{suffix}"
        )
        if "event" in r:
            print(f"      → STUCK signal posted: {r['event'].get('event_id', '?')}")
        if "error" in r:
            print(f"      → error: {r['error']}", file=sys.stderr)


def cmd_claim_list():
    """beacon claim list [--mine] [--target <k>:<id>] [--json]

    Lists claims this session has issued + still has cached locally.

    For a project-wide view ("what's everyone holding right now?"),
    use `beacon bus receive --channel claim-signal` (= live stream) or
    the future `beacon claim status` command that reduces the channel
    history server-side. The local list is the right surface for
    "what was I in the middle of when this session restarted?".
    """
    import claims as _claims

    mine_flag = os.environ.get("BEACON_CLAIM_MINE", "") == "1"
    tk = os.environ.get("BEACON_CLAIM_TARGET_KIND", "").strip() or None
    ti = os.environ.get("BEACON_CLAIM_TARGET_ID", "").strip() or None
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    mine = _resolve_session_id() if mine_flag else None
    if mine_flag and not mine:
        print("Error: --mine requires a resolvable session_id",
              file=sys.stderr)
        sys.exit(1)

    # ms-55 e-1730: cloud-mode pulls from the server subcollection (=
    # multi-machine view), local-mode falls back to the cached file.
    _claims_register_provider()
    out = _claims.list_claims(
        mine=mine, target_kind=tk, target_id=ti,
    )

    if json_mode:
        print(json.dumps(out, ensure_ascii=False))
        return

    if not out:
        print("No active claims in the local cache.")
        return

    print(f"Local active claims ({len(out)}):")
    for rec in out:
        target = rec.get("target") or {}
        tag = f"{target.get('kind', '?')}:{target.get('id', '?')}"
        intent = rec.get("intent") or ""
        intent_suffix = f"  intent: {intent}" if intent else ""
        print(
            f"  [{rec.get('claim_kind', '?')}] {tag}  "
            f"id={rec.get('claim_id', '?')}  "
            f"by={rec.get('from_session_id', '?')}  "
            f"at={rec.get('issued_at', '?')}"
        )
        if intent_suffix:
            print(f"   {intent_suffix.strip()}")


def cmd_resume_global():
    """Broadcast a global resume."""
    import stop_signal as _stop

    reason = os.environ.get("BEACON_STOP_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _stop.build_resume_payload(
            scope=_stop.SCOPE_GLOBAL,
            issued_by_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _stop_post_event(payload=payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(f"RESUME signal (GLOBAL) raised by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


def cmd_sessions_list():
    """Cross-project session directory (ms-54 / e-1587).

    Unlike ``bus directory`` which is cwd-scoped to the current project, this
    command lists the calling user's live sessions across *all* their projects.
    Use it when the picker needs to see DM recipients in projects you are not
    currently cd'd into, or to diagnose heartbeat-stop incidents
    (e.g. e-1579-style auth-fail-cascade) without cd-ing through each candidate.

    Bootstraps the API client cwd-independently via the profile resolver
    (ms-64 / e-1458). Auth credentials come from the active profile too
    (e-1457). The resolver already honors the env > cwd cloud.json >
    profile.json > default precedence chain, so no extra cloud.json read
    is needed.
    """
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    api_url = _resolve_active_api_url()

    from api_client import ApiClient
    client = ApiClient(api_url, _extract_token(creds))

    sessions = client.list_user_sessions(
        live_only=os.environ.get("BEACON_SESSIONS_LIVE", "") == "1",
        since_minutes=int(os.environ.get("BEACON_SESSIONS_SINCE_MIN", "5") or "5"),
        healthy_only=os.environ.get("BEACON_SESSIONS_HEALTHY", "") == "1",
        machine=os.environ.get("BEACON_SESSIONS_MACHINE", "").strip(),
        agent=os.environ.get("BEACON_SESSIONS_AGENT", "").strip(),
    )

    if os.environ.get("BEACON_JSON", "") == "1":
        print(json.dumps(sessions, ensure_ascii=False))
        return

    if not sessions:
        print("(no matching sessions across your projects)")
        return

    for s in sessions:
        actor = s.get("actor") or {}
        sid = s.get("session_id", "?")
        email = actor.get("email", "")
        machine_s = actor.get("machine", "")
        agent_s = actor.get("agent", "")
        last = (s.get("last_active") or "")[:19]
        ident = " / ".join(p for p in (email, machine_s, agent_s) if p) or "(anon)"
        pid = s.get("project_id", "?")
        pname = s.get("project_name", "") or pid

        ph = s.get("poll_health") or {}
        healthy = ph.get("healthy")
        age = ph.get("age_seconds")
        shutdown = ph.get("shutdown")
        if shutdown:
            health_tag = "  health=shutdown"
        elif healthy is True:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=ok({age_str})"
        elif healthy is False:
            age_str = f"{int(age)}s" if isinstance(age, (int, float)) else "?"
            health_tag = f"  health=stale({age_str})"
        else:
            health_tag = "  health=unknown"
        print(f"  [{pname}]  {sid}  {ident}  last_active={last}{health_tag}")


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    commands = {
        "init": cmd_init,
        "milestone_add": cmd_milestone_add,
        "milestone_list": cmd_milestone_list,
        "milestone_start": cmd_milestone_start,
        "milestone_done": cmd_milestone_done,
        "milestone_observe": cmd_milestone_observe,
        "milestone_wait": cmd_milestone_wait,
        "milestone_release": cmd_milestone_release,
        "milestone_occupations": cmd_milestone_occupations,
        "milestone_join": cmd_milestone_join,
        "milestone_show": cmd_milestone_show,
        "milestone_update": cmd_milestone_update,
        "milestone_delete": cmd_milestone_delete,
        "milestone_purge": cmd_milestone_purge,
        "entry_purge": cmd_entry_purge,
        "operation_purge": cmd_operation_purge,
        "log": cmd_log,
        "log_prepare": cmd_log_prepare,
        "log_finalize": cmd_log_finalize,
        "sync": cmd_sync,
        "task_add": cmd_task_add,
        "task_done": cmd_task_done,
        "task_list": cmd_task_list,
        "task_show": cmd_task_show,
        "task_detail": cmd_task_detail,
        "task_update": cmd_task_update,
        "task_delete": cmd_task_delete,
        "task_cancel": cmd_task_cancel,
        "entry_move": cmd_entry_move,
        "summary": cmd_summary,
        "save": cmd_save,
        "milestone_depends": cmd_milestone_depends,
        "milestone_workspace": cmd_milestone_workspace,
        "milestone_workspace_cleanup": cmd_milestone_workspace_cleanup,
        "milestone_graph": cmd_milestone_graph,
        "retro_prepare": cmd_retro_prepare,
        "retro_save": cmd_retro_save,
        "retro_done": cmd_retro_done,
        "retro_default_since": cmd_retro_default_since,
        "trigger_fire": cmd_trigger_fire,
        "trigger_check": cmd_trigger_check,
        "trigger_clear": cmd_trigger_clear,
        "doc_list": cmd_doc_list,
        "doc_show": cmd_doc_show,
        "doc_add": cmd_doc_add,
        "doc_update": cmd_doc_update,
        "doc_delete": cmd_doc_delete,
        "doc_history": cmd_doc_history,
        "doc_restore": cmd_doc_restore,
        "doc_image_upload": cmd_doc_image_upload,
        "cloud_list": cmd_cloud_list,
        "cloud_push": cmd_cloud_push,
        "cloud_pull": cmd_cloud_pull,
        "cloud_status": cmd_cloud_status,
        "cloud_check_project": cmd_cloud_check_project,
        "cloud_join": cmd_cloud_join,
        "common_setup": cmd_common_setup,
        "auth_check": cmd_auth_check,
        "auth_login": lambda: __import__("auth").login(),
        "auth_logout": lambda: __import__("auth").logout(),
        "auth_status": lambda: __import__("auth").status(),
        "skill_install": cmd_skill_install,
        "update": cmd_update,
        "search": cmd_search,
        "cycle_status": cmd_cycle_status,
        "project_archive": cmd_project_archive,
        "deploy_record": cmd_deploy_record,
        "deploy_list": cmd_deploy_list,
        "deploy_delete": cmd_deploy_delete,
        "deploy_void": cmd_deploy_void,
        "deploy_rollback": cmd_deploy_rollback,
        "push_record": cmd_push_record,
        "push_list": cmd_push_list,
        "project_unarchive": cmd_project_unarchive,
        "project_export": cmd_project_export,
        "project_import": cmd_project_import,
        "pr_add": cmd_pr_add,
        "pr_close": cmd_pr_close,
        "pr_approve": cmd_pr_approve,
        "pr_reject": cmd_pr_reject,
        "pr_create": cmd_pr_create,
        "pr_show": cmd_pr_show,
        "pr_request_review": cmd_pr_request_review,
        "pr_request_changes": cmd_pr_request_changes,
        "pr_merge": cmd_pr_merge,
        "issue_import": cmd_issue_import,
        "issue_list": cmd_issue_list,
        "issue_sync": cmd_issue_sync,
        "operation_open": cmd_operation_open,
        "operation_close": cmd_operation_close,
        "operation_set_status": cmd_operation_set_status,
        "operation_update": cmd_operation_update,
        "operation_task_add": cmd_operation_task_add,
        "operation_task_done": cmd_operation_task_done,
        "operation_task_list": cmd_operation_task_list,
        "operation_list": cmd_operation_list,
        "operation_show": cmd_operation_show,
        "operation_approve": cmd_operation_approve,
        "operation_revoke": cmd_operation_revoke,
        "operation_envelope_verify": cmd_operation_envelope_verify,
        "run_record": cmd_run_record,
        "run_list": cmd_run_list,
        "incident_open": cmd_incident_open,
        "incident_close": cmd_incident_close,
        "incident_escalate": cmd_incident_escalate,
        "incident_list": cmd_incident_list,
        "note_add": cmd_note_add,
        "note_list": cmd_note_list,
        "note_clear": cmd_note_clear,
        "bus_send": cmd_bus_send,
        "bus_listen": cmd_bus_listen,
        "bus_receive": cmd_bus_receive,
        "bus_ack": cmd_bus_ack,
        "bus_status": cmd_bus_status,
        "bus_directory": cmd_bus_directory,
        # ms-70 / e-1716: receiver-side decision primitive for pending DM
        # actions. Reached by `beacon dm respond approve|deny <event_id>`.
        "dm_respond": cmd_dm_respond,
        # ms-70 / e-1923 (e-1718 AC 4): audit history view in the terminal.
        # Mirror of the Web UI Settings > Audit table; reaches the same
        # server endpoint and renders 6 columns of decided sidecar rows.
        "dm_log": cmd_dm_log,
        # ms-55 e-1646: stop / resume signal CLI. Anyone can broadcast
        # (Andon cord principle, SPEC §2). The events ride on the
        # existing bus on channel `stop-signal`.
        "stop_scoped": cmd_stop_scoped,
        "stop_global": cmd_stop_global,
        "stop_status": cmd_stop_status,
        "resume_scoped": cmd_resume_scoped,
        "resume_global": cmd_resume_global,
        # ms-55 e-1647: rollback boundary CLI. Auto-undoes working tree +
        # un-pushed commits; refuses to touch pushed/merged/deployed
        # state (those produce report + compensation proposals).
        "rollback": cmd_rollback,
        # ms-55 e-1648: claim primitives. 3 kinds (request/handoff/claim);
        # request + handoff need recipient consent, claim is first-publisher
        # -wins broadcast. Local persistence for session restart recovery.
        "claim_request": cmd_claim_request,
        "claim_handoff": cmd_claim_handoff,
        "claim_post": cmd_claim_post,
        "claim_respond": cmd_claim_respond,
        "claim_release": cmd_claim_release,
        "claim_list": cmd_claim_list,
        # ms-55 e-1649: STUCK detector. Idle-timeout based emission of
        # stop signals with reason_kind="stuck", same protocol as e-1646.
        "stuck_check": cmd_stuck_check,
        # ms-55 e-1650: morning briefing. 4-bucket digest of recent
        # autonomous activity for the human to read with coffee.
        "morning": cmd_morning,
        "sessions_list": cmd_sessions_list,
        "profile_list": cmd_profile_list,
        "bus_budget_grant": cmd_bus_budget_grant,
        "bus_budget_show": cmd_bus_budget_show,
        "bus_budget_clear": cmd_bus_budget_clear,
        "bus_auto_execute_list": cmd_bus_auto_execute_list,
        "bus_auto_execute_add": cmd_bus_auto_execute_add,
        "bus_auto_execute_remove": cmd_bus_auto_execute_remove,
        "session_end": cmd_session_end,
        "session_rescue": cmd_session_rescue,
        "session_log_list": cmd_session_log_list,
        "session_log_show": cmd_session_log_show,
        "session_id": cmd_session_id,
        "session_focus": cmd_session_focus,
        "session_attention": cmd_session_attention,
        "session_fork": cmd_session_fork,
        "session_fork_list": cmd_session_fork_list,
        "channel_install": cmd_channel_install,
        "channel_uninstall": cmd_channel_uninstall,
        "channel_opt_out": cmd_channel_opt_out,
        "channel_opt_in": cmd_channel_opt_in,
        "channel_status": cmd_channel_status,
        "member_add": cmd_member_add,
        "member_list": cmd_member_list,
        "member_remove": cmd_member_remove,
        "member_role": cmd_member_role,
        # ms-78 e-1805: token-based invite flow + self-introspection.
        "member_invite": cmd_member_invite,
        "member_invitation_list": cmd_member_invitation_list,
        "member_invitation_cancel": cmd_member_invitation_cancel,
        "member_join": cmd_member_join,
        "member_whoami": cmd_member_whoami,
        "trek_create": cmd_trek_create,
        "trek_list": cmd_trek_list,
        "trek_show": cmd_trek_show,
        "trek_start": cmd_trek_start,
        "trek_archive": cmd_trek_archive,
        "trek_invite": cmd_trek_invite,
        "trek_join": cmd_trek_join,
        "trek_leave": cmd_trek_leave,
        "trek_plan": cmd_trek_plan,
        "trek_task_state": cmd_trek_task_state,
        "trek_stop": cmd_trek_stop,
        "trek_resume": cmd_trek_resume,
        "trek_pulse_ack": cmd_trek_pulse_ack,
        "trek_take_over": cmd_trek_take_over,
        "trek_transfer_leader": cmd_trek_transfer_leader,
        "trek_timeline": cmd_trek_timeline,
        "version": lambda: print(f"beacon {__version__}"),
        "help_json": cmd_help_json,
        "doctor": cmd_doctor,
    }
    fn = commands.get(cmd)
    # ms-54 e-1319: the CLI-side heartbeat (formerly bumped here, ms-57 e-1035)
    # has been retired. Post Option C (PR #111 / commit 78048b6) the bridge
    # poll loop is the truth source for both ``last_active`` (proof of life)
    # and ``last_poll_at`` (proof of receive-capability). A CLI-side write
    # created ambiguity — a session could "heartbeat" while the bridge poll
    # was dead, leaving DMs accumulating server-side unread. Resolve stays
    # in the CLI (`beacon session id` pure getter); mint+heartbeat = bridge;
    # lifecycle close = `beacon session end`.
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
