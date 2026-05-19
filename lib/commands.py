#!/usr/bin/env python3
"""Beacon CLI commands - thin adapter over core.py logic."""

__version__ = "0.1.0"

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from store import get_store
import core

# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def get_project_file():
    return os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")


def load_project():
    store = get_store()
    data = store.load_project()
    core.validate_project(data)
    return data


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


def save_project(data, op=None):
    core.validate_project(data)
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


CLAUDE_HOOK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "beacon-post-commit-hook.sh"
)

CLAUDE_SAVE_HOOK_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "beacon-save-hook.sh"
)


def _install_claude_hook():
    settings_path = os.path.expanduser("~/.claude/settings.json")
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
                "command": CLAUDE_HOOK_SCRIPT,
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
                "command": CLAUDE_SAVE_HOOK_SCRIPT,
                "timeout": 10,
                "statusMessage": "Beacon: checking MCP operation...",
            }],
        })

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("Installed Claude Code PostToolUse hooks")


def _install_skills():
    """Copy beacon skills to ~/.claude/skills/ for Claude Code integration."""
    skills_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "skills",
    )
    if not os.path.isdir(skills_src):
        return
    skills_dst = os.path.expanduser("~/.claude/skills")
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


def cmd_init():
    name = os.environ.get("BEACON_NAME", "")
    objective = os.environ.get("BEACON_OBJECTIVE", "")
    pf = get_project_file()
    os.makedirs(os.path.dirname(pf), exist_ok=True)
    retro_day = os.environ.get("BEACON_RETRO_DAY", "monday")
    data = {"name": name, "objective": objective, "milestones": [], "retro_day": retro_day}
    with open(pf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {pf}")
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

    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
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

    mode_config_path = os.path.join(beacon_dir, "config.json")
    with open(mode_config_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "cloud"}, f, indent=2, ensure_ascii=False)
        f.write("\n")

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
    data = load_project()
    ms_id = core.milestone_add(data, title, target_date, description=description)
    save_project(data)
    print(f"Added milestone {ms_id}: {title}")


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
        }
        for ms in milestones:
            entries = ms.get("entries", [])
            total_tasks, done_tasks = core.count_task_status(entries)
            output["milestones"].append({
                "id": ms["id"],
                "title": ms.get("title", ""),
                "status": ms.get("status", "todo"),
                "progress": ms.get("progress", 0),
                "target_date": ms.get("target_date", ""),
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
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
    ms_id = os.environ.get("BEACON_MS_ID", "")
    data = load_project()
    ms = core.milestone_start(data, ms_id)
    save_project(data)
    print(f"Activated: {ms['title']}")


def cmd_milestone_done():
    ms_id = os.environ.get("BEACON_MS_ID", "")
    reason = os.environ.get("BEACON_REASON", "")
    data = load_project()
    ms = core.milestone_done(data, ms_id, reason=reason)
    save_project(data, op={"op": "milestone_done", "ms_id": ms_id, "reason": reason})
    msg = f"Completed: {ms['title']}"
    if reason:
        msg += f"\n  Reason: {reason}"
    print(msg)


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
# Log commands
# ---------------------------------------------------------------------------

def cmd_log():
    summary = os.environ.get("BEACON_SUMMARY", "")
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary, progress=progress,
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

    if ms_id:
        for ms in data["milestones"]:
            if ms["id"] == ms_id:
                targets = [ms]
                break
        else:
            print(f"Milestone not found: {ms_id}")
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

    if len(targets) == 1:
        output["milestone"] = core.milestone_prepare_info(targets[0])
    else:
        output["candidates"] = [core.milestone_prepare_info(ms) for ms in targets]

    print(json.dumps(output, ensure_ascii=False))


def cmd_log_finalize():
    commit_hash = os.environ.get("BEACON_HASH", "")
    message = os.environ.get("BEACON_MESSAGE", "")
    date = os.environ.get("BEACON_DATE", "")
    ms_id = os.environ.get("BEACON_MS_ID", "")
    summary_text = os.environ.get("BEACON_SUMMARY", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    new_summary = os.environ.get("BEACON_NEW_SUMMARY", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()
    result = core.log_commit(
        data, ms_id=ms_id, commit_hash=commit_hash,
        message=message, date=date, summary=summary_text, progress=progress,
    )

    if new_summary:
        data["summary"] = new_summary

    save_project(data)

    if json_mode:
        result["summary_updated"] = bool(new_summary)
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["status"] == "duplicate":
            print(f"Already logged: {commit_hash} (updated progress/summary)")
        else:
            loc = f"[{result['matched_task']}]" if "matched_task" in result else result["milestone_title"]
            print(f"Logged {commit_hash} → {loc}")
        if progress:
            print(f"  Progress: {result['progress']}%")
        if new_summary:
            print(f"  Summary updated.")


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

    data = load_project()
    target = core.find_target_milestone(data, ms_id)
    eid = core.task_add(data, ms_id, description, entry_type=entry_type,
                        date=date, detail=detail, requested_by=requested_by)
    save_project(data)
    from_str = f" (from {requested_by})" if requested_by else ""
    print(f"Added {entry_type} [{eid}] to {target['title']}: {description}{from_str}")


def cmd_task_done():
    entry_id = os.environ.get("BEACON_ENTRY_ID", "")
    progress = os.environ.get("BEACON_PROGRESS", "")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = load_project()
    # PR entries: auto-forward to pr_merge, but warn if status is unexpected
    result = core.find_entry(data, entry_id)
    if result:
        _, _, entry, _ = result
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
    reason = os.environ.get("BEACON_REASON", "")
    ms, entry = core.task_done(data, entry_id, date=today, reason=reason)
    print(f"Done: [{entry_id}] {entry['description']}")
    if reason:
        print(f"  Reason: {reason}")
    core.update_progress(ms, progress)
    if progress:
        print(f"  Progress: {ms.get('progress', 0)}%")
    save_project(data, op={"op": "task_done", "entry_id": entry_id, "reason": reason})


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
    print(f"  Created: {entry.get('created_at', '-')}  Done: {entry.get('done_at', '-')}")
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
# Summary
# ---------------------------------------------------------------------------

def cmd_summary():
    text = os.environ.get("BEACON_SUMMARY_TEXT", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    if text:
        data["summary"] = text
        save_project(data)
        print(f"Summary updated.")
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
    ms_id = os.environ.get("BEACON_MS_ID", "")
    workspace = os.environ.get("BEACON_WORKSPACE", "")
    clear = os.environ.get("BEACON_CLEAR", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not ms_id:
        print("Error: milestone ID is required", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    if clear:
        ms = core.milestone_workspace(data, ms_id, "")
    else:
        if not workspace:
            print("Error: --dir is required (or use --clear)", file=sys.stderr)
            sys.exit(1)
        ms = core.milestone_workspace(data, ms_id, workspace)
    save_project(data)

    if json_mode:
        print(json.dumps({"id": ms["id"], "workspace": ms.get("workspace")}, ensure_ascii=False))
    else:
        ws = ms.get("workspace")
        if ws:
            print(f"{ms_id} workspace: {ws}")
        else:
            print(f"{ms_id}: workspace cleared")


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
    since = os.environ.get("BEACON_SINCE", "")
    until = os.environ.get("BEACON_UNTIL", "")
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

    output = {
        "project": data.get("name", ""),
        "period": {"since": since, "until": until},
        "summary": data.get("summary", ""),
        "milestones": weekly_milestones,
        "deploys": weekly_deploys,
    }
    print(json.dumps(output, ensure_ascii=False))


def cmd_retro_done():
    import datetime
    today = datetime.date.today()
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"

    project_dir = os.path.dirname(get_project_file())
    retro_dir = os.path.join(project_dir, "retro")
    os.makedirs(retro_dir, exist_ok=True)
    reviewed_path = os.path.join(retro_dir, ".reviewed")
    with open(reviewed_path, "w") as f:
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


def _auto_fire_retro_trigger():
    import datetime
    today = datetime.date.today()
    retro_day = _get_retro_day()
    if today.weekday() != retro_day:
        return
    year, week, _ = today.isocalendar()
    current_week = f"{year}-W{week:02d}"
    project_dir = os.path.dirname(get_project_file())
    reviewed_path = os.path.join(project_dir, "retro", ".reviewed")
    try:
        with open(reviewed_path, "r") as f:
            if f.read().strip() >= current_week:
                return
    except (FileNotFoundError, IOError):
        pass
    triggers_dir = os.path.join(project_dir, "triggers")
    trigger_path = os.path.join(triggers_dir, "retro.json")
    if os.path.exists(trigger_path):
        return
    os.makedirs(triggers_dir, exist_ok=True)
    trigger_data = {
        "name": "retro",
        "message": f"今週の振り返りがまだです（{current_week}）。/beacon-retro で開始しますか？",
        "created_at": today.isoformat(),
    }
    with open(trigger_path, "w") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def _cleanup_stale_triggers():
    import datetime
    today = datetime.date.today()
    triggers_dir = _get_triggers_dir()
    retro_path = os.path.join(triggers_dir, "retro.json")
    if not os.path.exists(retro_path):
        return
    try:
        with open(retro_path, "r") as f:
            trigger = json.load(f)
        created = datetime.date.fromisoformat(trigger["created_at"][:10])
        if today > created:
            os.remove(retro_path)
    except (json.JSONDecodeError, KeyError, ValueError, IOError):
        pass


def cmd_trigger_fire():
    trigger_name = os.environ.get("BEACON_TRIGGER_NAME", "")
    trigger_message = os.environ.get("BEACON_TRIGGER_MESSAGE", "")
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
    with open(trigger_path, "w") as f:
        json.dump(trigger_data, f, ensure_ascii=False)
        f.write("\n")


def cmd_trigger_check():
    _auto_fire_retro_trigger()
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
            with open(fpath, "r") as f:
                triggers.append(json.load(f))
        except (json.JSONDecodeError, IOError):
            pass
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

VALID_SCOPES = ("core", "spec", "memo")
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
    """Check if we're in cloud mode (has cloud.json + config mode=cloud)."""
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    config_path = os.path.join(beacon_dir, "config.json")
    if os.environ.get("BEACON_CLOUD") == "1":
        return True
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("mode") == "cloud"
    return False


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text.

    Returns (metadata_dict, body_text).
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta = {}
    for line in header.split("\n"):
        line = line.strip()
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta, body


def _add_frontmatter(content, scope, milestone=""):
    """Prepend frontmatter to content, or update existing scope/milestone."""
    meta, body = _parse_frontmatter(content)
    meta["scope"] = scope
    if milestone:
        meta["milestone"] = milestone
    elif "milestone" not in meta:
        pass  # keep existing or absent
    lines = ["---"]
    for k, v in meta.items():
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
    result = {
        "doc_id": fname[:-3],
        "title": title,
        "scope": scope,
        "content": content,
        "updated_at": updated,
    }
    if milestone:
        result["milestone"] = milestone
    return result


def cmd_doc_list():
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    scope_filter = os.environ.get("BEACON_SCOPE", "")
    ms_filter = os.environ.get("BEACON_MS", "")

    if _is_cloud_mode():
        client, config = _get_api_client()
        docs = client.list_documents(config["project_id"])
    else:
        docs_dir = _get_docs_dir()
        docs = []
        if os.path.isdir(docs_dir):
            for fname in sorted(os.listdir(docs_dir)):
                if not fname.endswith(".md"):
                    continue
                doc = _read_local_doc(os.path.join(docs_dir, fname))
                entry = {
                    "doc_id": doc["doc_id"],
                    "title": doc["title"],
                    "scope": doc["scope"],
                    "updated_at": doc["updated_at"],
                }
                if doc.get("milestone"):
                    entry["milestone"] = doc["milestone"]
                docs.append(entry)

    if scope_filter:
        docs = [d for d in docs if d.get("scope") == scope_filter]
    if ms_filter:
        docs = [d for d in docs if d.get("milestone") == ms_filter]

    if json_mode:
        print(json.dumps(docs, ensure_ascii=False))
    else:
        if not docs:
            print("No documents.")
            return
        scope_icons = {"core": "*", "spec": "+", "memo": "-"}
        for doc in docs:
            icon = scope_icons.get(doc.get("scope", "memo"), "?")
            print(f"  {icon} [{doc.get('scope', 'memo')}] {doc['doc_id']}: {doc['title']}")


def cmd_doc_show():
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        doc = client.get_document(config["project_id"], doc_id)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        doc = _read_local_doc(fpath)

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
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not title:
        print("Error: title required")
        sys.exit(1)

    if scope not in VALID_SCOPES:
        print(f"Error: scope must be one of {VALID_SCOPES}")
        sys.exit(1)

    # core docs are project-wide — MS association is optional
    if scope == "core":
        milestone = milestone or None

    # Read content from stdin if not provided via env
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()

    if not content:
        print("Error: content required (pass via BEACON_CONTENT or stdin)")
        sys.exit(1)

    # Duplicate check: warn if same title+scope already exists
    if _is_cloud_mode():
        try:
            client, config = _get_api_client()
            existing = client.list_documents(config["project_id"])
            dupes = [d for d in existing if d.get("title") == title and d.get("scope") == scope]
            if dupes:
                print(f"Warning: document with same title+scope already exists ({dupes[0]['doc_id']}). Proceeding anyway.")
        except Exception:
            pass

    # Add frontmatter with scope and milestone
    content = _add_frontmatter(content, scope, milestone or "")

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
    # core docs: skip MS entry recording (they're project-wide, not MS-specific)
    if scope != "core":
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
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    # Read content from stdin if not provided via env
    if not content and not sys.stdin.isatty():
        content = sys.stdin.read()

    # Fetch existing document to merge fields
    if _is_cloud_mode():
        client, config = _get_api_client()
        existing = client.get_document(config["project_id"], doc_id)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        existing = _read_local_doc(fpath)

    # Use existing values as defaults
    if not title:
        title = existing.get("title", "")
    if not scope:
        scope = existing.get("scope", DEFAULT_SCOPE)
    if not milestone:
        milestone = existing.get("milestone", "")
    if not content:
        content = existing.get("content", "")

    # Rebuild with frontmatter
    content = _add_frontmatter(content, scope, milestone)

    if _is_cloud_mode():
        client.update_document(config["project_id"], doc_id, title, content)
    else:
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    import datetime
    data = load_project()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core.save_entry(data, ms_id=milestone, description=f"doc update: {title} ({scope})",
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


def cmd_doc_restore():
    """Restore a document to a previous revision (creates new save, keeps history)."""
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    rev = os.environ.get("BEACON_REV", "")
    if not doc_id or not rev:
        print("Error: doc ID and --rev required")
        sys.exit(1)
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
    doc_id = os.environ.get("BEACON_DOC_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not doc_id:
        print("Error: doc_id required")
        sys.exit(1)

    if _is_cloud_mode():
        client, config = _get_api_client()
        try:
            client.delete_document(config["project_id"], doc_id)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        docs_dir = _get_docs_dir()
        fpath = os.path.join(docs_dir, f"{doc_id}.md")
        if not os.path.exists(fpath):
            print(f"Document not found: {doc_id}")
            sys.exit(1)
        os.remove(fpath)

    if json_mode:
        print(json.dumps({"doc_id": doc_id, "deleted": True}, ensure_ascii=False))
    else:
        print(f"Deleted: {doc_id}")


# ---------------------------------------------------------------------------
# Cloud commands
# ---------------------------------------------------------------------------

DEFAULT_API_URL = "https://beacon-ai.dev"


def _get_cloud_config_path():
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    return os.path.join(beacon_dir, "cloud.json")


def _ensure_cloud_config():
    config_path = _get_cloud_config_path()
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    data = load_project()
    name = data.get("name", "project")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    import hashlib
    h = hashlib.md5(os.path.abspath(get_project_file()).encode()).hexdigest()[:6]
    project_id = f"{slug}-{h}"

    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
    config = {"project_id": project_id, "api_url": api_url}
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Created {config_path} (project_id: {project_id})")
    return config



def _extract_token(creds) -> str:
    """Extract bearer token from credentials (handles both object and dict forms)."""
    if isinstance(creds, dict):
        return creds.get("token", "") or creds.get("id_token", "")
    return (creds.id_token or creds.token) if creds else ""

def _get_api_client():
    """Create an ApiClient from cloud.json config and auth credentials."""
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

    api_url = config.get("api_url", DEFAULT_API_URL)
    token = _extract_token(creds)

    from api_client import ApiClient
    return ApiClient(api_url, token), config


def cmd_cloud_list():
    """List cloud projects via API."""
    from auth import load_credentials
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        sys.exit(1)

    # cloud list may be called before cloud.json exists, so read api_url from env
    api_url = os.environ.get("BEACON_API_URL", DEFAULT_API_URL)
    config_path = _get_cloud_config_path()
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_url = config.get("api_url", api_url)

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
    api_url = config.get("api_url", DEFAULT_API_URL)

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

    # Auto-switch to cloud mode
    beacon_dir = os.path.dirname(get_project_file()) or ".beacon"
    config_path = os.path.join(beacon_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"mode": "cloud"}, f, indent=2)
        f.write("\n")
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
    print(f"API: {config.get('api_url', DEFAULT_API_URL)}")
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
    try:
        eid = core.pr_add(data, ms_id=ms_id, url=url, author=author,
                          intent=intent, date=date, title=title, commits=commits)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    save_project(data)

    if json_mode:
        print(json.dumps({"entry_id": eid, "url": url, "title": title, "intent": intent,
                          "commits": len(commits)}, ensure_ascii=False))
    else:
        print(f"Added PR [{eid}]: {title or url}")
        if commits:
            print(f"  Commits: {len(commits)} linked")
        if intent:
            print(f"  Intent: {intent}")


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


def cmd_pr_create():
    """Wrapper for gh pr create that auto-records the PR in beacon."""
    ms_id = os.environ.get("BEACON_MS_ID", "")
    intent = os.environ.get("BEACON_INTENT", "")
    gh_args = os.environ.get("BEACON_GH_ARGS", "")

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
                          title=title, commits=commits)
    except ValueError as e:
        print(f"Warning: beacon pr record failed: {e}", file=sys.stderr)
        return
    save_project(data)
    print(f"Beacon: PR recorded [{eid}]: {title or pr_url}")
    if commits:
        print(f"  Commits: {len(commits)} linked")


# ---------------------------------------------------------------------------
# Skill install
# ---------------------------------------------------------------------------

def cmd_skill_install():
    """Install beacon Claude Code Skills into ~/.claude/skills/, update CLAUDE.md, and configure hooks."""
    import shutil
    _append_claude_md()

    # Find skills source relative to this file: <beacon_root>/skills/
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skills_src = os.path.join(beacon_root, "skills")

    if not os.path.isdir(skills_src):
        print(f"Error: skills directory not found at {skills_src}")
        sys.exit(1)

    # Destination: ~/.claude/skills/
    home = os.path.expanduser("~")
    claude_skills = os.path.join(home, ".claude", "skills")
    os.makedirs(claude_skills, exist_ok=True)

    installed = []
    for src_file in sorted(os.listdir(skills_src)):
        if not src_file.endswith(".md"):
            continue
        skill_name = src_file[:-3]  # strip .md
        dest_dir = os.path.join(claude_skills, skill_name)
        os.makedirs(dest_dir, exist_ok=True)
        dest_file = os.path.join(dest_dir, "skill.md")
        shutil.copy2(os.path.join(skills_src, src_file), dest_file)
        installed.append(skill_name)

    if installed:
        print(f"Installed {len(installed)} Skills to {claude_skills}:")
        for name in installed:
            print(f"  /{name}")
    else:
        print("No skills found to install.")

    # Configure Claude Code PostToolUse hooks
    hook_script = os.path.join(beacon_root, "bin", "beacon-post-commit-hook.sh")
    settings_path = os.path.join(home, ".claude", "settings.json")
    _install_claude_hooks(hook_script, settings_path)


def _install_claude_hooks(hook_script: str, settings_path: str) -> None:
    """Add beacon PostToolUse hooks to Claude Code settings.json if not already present."""
    if not os.path.exists(hook_script):
        print(f"Warning: hook script not found at {hook_script}")
        return

    # Load existing settings
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    hooks = settings.setdefault("hooks", {})
    post_tool_use = hooks.setdefault("PostToolUse", [])

    # Check if beacon hook is already configured
    beacon_hook_cmd = hook_script
    for entry in post_tool_use:
        for h in entry.get("hooks", []):
            if h.get("command", "") == beacon_hook_cmd:
                print("Hooks: already configured in ~/.claude/settings.json")
                return

    # Add beacon PostToolUse hook (commit + deploy detection)
    post_tool_use.append({
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": beacon_hook_cmd,
            "timeout": 10,
            "statusMessage": "Beacon: checking for commit or deploy..."
        }]
    })

    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print(f"Hooks: PostToolUse hook configured in {settings_path}")


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
    description = os.environ.get("BEACON_DESCRIPTION", "")
    deploy_hash = os.environ.get("BEACON_HASH", "")   # override: specify deployed commit
    deploy_date = os.environ.get("BEACON_DATE", "")   # override: specify deploy datetime
    insert_before = os.environ.get("BEACON_INSERT_BEFORE", "")  # insert before this deploy-id
    type_override = os.environ.get("BEACON_TYPE", "")  # override: "major" or "minor"
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
        "environment": "prod",
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
        print(f"{icon} {deploy_id} [{deploy_type}] {ms_str}")
        print(f"  {description}")
        if semver:
            print(f"  Release: {release_entry['id']} ({semver})")
        if links_to:
            print(f"  Patches: {', '.join(links_to)}")


def cmd_deploy_list():
    """List deployment records."""
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()
    deployments = data.get("deployments", [])
    releases = {r["id"]: r for r in data.get("releases", [])}

    if json_mode:
        print(json.dumps({"deployments": deployments, "releases": list(releases.values())},
                         ensure_ascii=False))
        return

    if not deployments:
        print("No deployments recorded yet.")
        print("Run 'beacon deploy record' after each deploy.")
        return

    for d in reversed(deployments):
        icon = "◉" if d.get("type") == "major" else "○"
        rel = releases.get(d.get("linked_release", ""))
        semver_str = f" {rel['semver']}" if rel and rel.get("semver") else ""
        ms_str = " ".join(d.get("milestones", [])) or "-"
        print(f"{icon} {d['id']}{semver_str}  {d['date'][:10]}  [{d.get('type','')}]  {ms_str}")
        print(f"   {d.get('description', '')}")
        if d.get("links_to"):
            print(f"   patches: {', '.join(d['links_to'])}")


def cmd_deploy_delete():
    """Deprecated: physical deletion of deploy records is not allowed."""
    print("Error: 'beacon deploy delete' is deprecated.")
    print("  Deploy records are immutable facts — they cannot be physically deleted.")
    print("  To mark a record as invalid: beacon deploy void <id> --reason \"...\"")
    sys.exit(1)


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


def cmd_search():
    """Full-text search across milestones, tasks, commits, PRs, and saves."""
    query = os.environ.get("BEACON_QUERY", "").lower().strip()
    ms_filter = os.environ.get("BEACON_MS_ID", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not query:
        print("Usage: beacon search <query>", file=sys.stderr)
        sys.exit(1)

    data = load_project()
    results = []

    def _search_entries(entries, ms_id, ms_title):
        for e in entries:
            desc = e.get("description", "").lower()
            detail = e.get("detail", "").lower()
            if query in desc or query in detail:
                results.append({
                    "ms_id": ms_id,
                    "ms_title": ms_title,
                    "entry_id": e.get("id", ""),
                    "type": e.get("type", ""),
                    "status": e.get("status", ""),
                    "description": e.get("description", ""),
                    "date": e.get("date", "") or e.get("created_at", ""),
                })
            _search_entries(e.get("entries", []), ms_id, ms_title)

    for ms in data.get("milestones", []):
        if ms_filter and ms["id"] != ms_filter:
            continue
        ms_title = ms.get("title", "")
        if query in ms_title.lower():
            results.append({
                "ms_id": ms["id"],
                "ms_title": ms_title,
                "entry_id": ms["id"],
                "type": "milestone",
                "status": ms.get("status", ""),
                "description": ms_title,
                "date": "",
            })
        _search_entries(ms.get("entries", []), ms["id"], ms_title)

    if json_mode:
        print(json.dumps(results, ensure_ascii=False))
        return

    if not results:
        print(f"No results for: {query}")
        return

    type_icons = {"task": "□", "commit": "○", "pr": "PR", "milestone": "MS", "save": "→"}
    print(f"{len(results)} result(s) for: {query}")
    for r in results:
        icon = type_icons.get(r["type"], "?")
        status_note = f" [{r['status']}]" if r["status"] not in ("done", "cancelled", "") else ""
        print(f"  {icon} [{r['entry_id']}] {r['description'][:80]}{status_note}")
        print(f"       └─ {r['ms_id']}: {r['ms_title'][:50]}")


def cmd_help_json():
    """Output beacon CLI command reference as machine-readable JSON."""
    commands = [
        {"command": "beacon init", "flags": [], "description": "Initialize .beacon/ in current directory"},
        {"command": "beacon setup", "flags": [], "description": "First-time setup wizard (auth + hooks + project)"},
        {"command": "beacon status", "flags": ["--json", "--ms <id>"], "description": "Show current status"},
        {"command": "beacon milestone add", "flags": [], "description": "Add a new milestone (interactive)"},
        {"command": "beacon milestone list", "flags": ["--json"], "description": "List milestones"},
        {"command": "beacon milestone start <id>", "flags": [], "description": "Set milestone as active (in_progress)"},
        {"command": "beacon milestone close <id>", "flags": [], "description": "Close milestone"},
        {"command": "beacon milestone observe <id>", "flags": [], "description": "Set milestone to observing"},
        {"command": "beacon milestone rename <id> <title>", "flags": [], "description": "Rename a milestone"},
        {"command": "beacon milestone depends <id> --on <id>", "flags": [], "description": "Declare milestone dependency"},
        {"command": "beacon milestone graph", "flags": ["--json"], "description": "Show dependency graph"},
        {"command": "beacon task add <desc>", "flags": ["-m <ms-id>"], "description": "Add a task to a milestone"},
        {"command": "beacon task done <entry-id>", "flags": [], "description": "Mark task as done"},
        {"command": "beacon task list", "flags": ["--json", "--ms <id>"], "description": "List tasks"},
        {"command": "beacon task update <entry-id>", "flags": ["--ms <ms-id>", "--desc <text>"], "description": "Update task description or move to another milestone"},
        {"command": "beacon log [message]", "flags": ["--prepare", "--finalize", "-m <ms-id>", "--progress <n>", "--summary <text>"], "description": "Record HEAD commit to active milestone"},
        {"command": "beacon save <desc>", "flags": ["-m <ms-id>", "--hash <hash>", "--source manual", "--json"], "description": "Save a freeform entry to a milestone"},
        {"command": "beacon sync", "flags": [], "description": "Auto-sync recent git commits to active milestone"},
        {"command": "beacon summary <text>", "flags": [], "description": "Update project summary"},
        {"command": "beacon doc add", "flags": ["--scope <core|spec|memo>", "--ms <id>", "--title <title>", "--content <text>", "--stdin"], "description": "Add a document"},
        {"command": "beacon doc list", "flags": ["--json", "--scope <scope>", "--ms <id>"], "description": "List documents"},
        {"command": "beacon doc show <doc-id>", "flags": [], "description": "Show document content"},
        {"command": "beacon doc update <doc-id>", "flags": ["--content <text>", "--stdin"], "description": "Update document content"},
        {"command": "beacon pr add", "flags": ["-m <ms-id>", "--url <url>", "--intent <text>"], "description": "Record a PR entry"},
        {"command": "beacon pr approve <entry-id>", "flags": [], "description": "Approve a PR"},
        {"command": "beacon pr reject <entry-id>", "flags": [], "description": "Reject a PR"},
        {"command": "beacon pr merge <entry-id>", "flags": [], "description": "Mark PR as merged"},
        {"command": "beacon retro", "flags": [], "description": "Start weekly retrospective (interactive)"},
        {"command": "beacon trigger check", "flags": [], "description": "Check pending triggers (JSON array)"},
        {"command": "beacon cloud list", "flags": [], "description": "List cloud projects"},
        {"command": "beacon cloud push", "flags": [], "description": "Push local project to cloud"},
        {"command": "beacon cloud pull", "flags": [], "description": "Pull project from cloud"},
        {"command": "beacon cloud join <id>", "flags": [], "description": "Join an existing cloud project"},
        {"command": "beacon auth login", "flags": [], "description": "Sign in with Google"},
        {"command": "beacon auth logout", "flags": [], "description": "Remove cached credentials"},
        {"command": "beacon auth status", "flags": [], "description": "Show login status"},
        {"command": "beacon skill install", "flags": [], "description": "Install Claude Code Skills to ~/.claude/skills/"},
        {"command": "beacon help", "flags": ["--json"], "description": "Show help (--json for machine-readable output)"},
    ]
    print(json.dumps({"version": __version__, "commands": commands}, ensure_ascii=False, indent=2))


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
        "milestone_show": cmd_milestone_show,
        "milestone_update": cmd_milestone_update,
        "milestone_delete": cmd_milestone_delete,
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
        "milestone_graph": cmd_milestone_graph,
        "retro_prepare": cmd_retro_prepare,
        "retro_done": cmd_retro_done,
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
        "cloud_list": cmd_cloud_list,
        "cloud_push": cmd_cloud_push,
        "cloud_pull": cmd_cloud_pull,
        "cloud_status": cmd_cloud_status,
        "cloud_join": cmd_cloud_join,
        "common_setup": cmd_common_setup,
        "auth_check": cmd_auth_check,
        "auth_login": lambda: __import__("auth").login(),
        "auth_logout": lambda: __import__("auth").logout(),
        "auth_status": lambda: __import__("auth").status(),
        "skill_install": cmd_skill_install,
        "search": cmd_search,
        "project_archive": cmd_project_archive,
        "deploy_record": cmd_deploy_record,
        "deploy_list": cmd_deploy_list,
        "deploy_delete": cmd_deploy_delete,
        "deploy_void": cmd_deploy_void,
        "project_unarchive": cmd_project_unarchive,
        "pr_add": cmd_pr_add,
        "pr_close": cmd_pr_close,
        "pr_approve": cmd_pr_approve,
        "pr_reject": cmd_pr_reject,
        "pr_create": cmd_pr_create,
        "pr_request_review": cmd_pr_request_review,
        "pr_request_changes": cmd_pr_request_changes,
        "pr_merge": cmd_pr_merge,
        "version": lambda: print(f"beacon {__version__}"),
        "help_json": cmd_help_json,
    }
    fn = commands.get(cmd)
    if fn:
        fn()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
