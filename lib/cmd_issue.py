#!/usr/bin/env python3
"""cmd_issue.py — the `beacon issue *` command family (ms-127 e-4320).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports these names for dispatch + `commands.X`.
"""

import json
import os
import sys

import core
from commands_shared import load_project, save_project


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
