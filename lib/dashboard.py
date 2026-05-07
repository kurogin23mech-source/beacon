#!/usr/bin/env python3
"""Beacon Dashboard - Tree view of project milestones and commits."""

import json
import os
import sys
import time
import shutil
import hashlib

# ANSI color codes
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    # Colors
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RED = "\033[31m"
    # Backgrounds
    BG_BLUE = "\033[44m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"


def load_project(path):
    """Load project.json, return None on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def status_icon(status):
    icons = {
        "todo": f"{C.GRAY}○{C.RESET}",
        "waiting": f"{C.GRAY}◌{C.RESET}",
        "in_progress": f"{C.YELLOW}◐{C.RESET}",
        "in_review": f"{C.CYAN}◑{C.RESET}",
        "done": f"{C.GREEN}●{C.RESET}",
    }
    return icons.get(status, f"{C.GRAY}?{C.RESET}")


def progress_bar(done, total, width=20):
    if total == 0:
        return f"{C.GRAY}{'░' * width}{C.RESET}"
    filled = int(width * done / total)
    bar = f"{C.GREEN}{'█' * filled}{C.RESET}{C.GRAY}{'░' * (width - filled)}{C.RESET}"
    return bar


def truncate(text, max_len):
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def render(project, term_width, term_height):
    """Render the dashboard to a list of lines."""
    lines = []
    w = term_width

    # Header
    header = f" BEACON "
    lines.append(f"{C.BOLD}{C.BG_BLUE}{C.WHITE}{header:^{w}}{C.RESET}")
    lines.append("")

    # Project objective
    obj = project.get("objective", "(未設定)")
    lines.append(f"  {C.BOLD}{C.CYAN}◆ 大目的{C.RESET}")
    # Word wrap objective
    max_obj_w = w - 6
    while obj:
        lines.append(f"    {C.WHITE}{truncate(obj, max_obj_w)}{C.RESET}")
        obj = obj[max_obj_w:]
    lines.append("")

    # Milestones summary
    milestones = project.get("milestones", [])
    done_count = sum(1 for m in milestones if m.get("status") == "done")
    total_count = len(milestones)
    bar = progress_bar(done_count, total_count, min(w - 20, 30))
    lines.append(f"  {bar}  {done_count}/{total_count} MS")
    lines.append(f"  {C.DIM}{'─' * (w - 4)}{C.RESET}")
    lines.append("")

    # Milestone tree
    for i, ms in enumerate(milestones):
        is_last_ms = i == len(milestones) - 1
        ms_status = ms.get("status", "planned")
        ms_icon = status_icon(ms_status)
        ms_title = ms.get("title", "(無題)")
        target = ms.get("target_date", "")
        is_active = ms_status == "in_progress"

        # Tree connector
        connector = "└─" if is_last_ms else "├─"
        child_prefix = "   " if is_last_ms else "│  "

        # Active milestone highlight
        if is_active:
            marker = f" {C.BOLD}{C.YELLOW}◀ ACTIVE{C.RESET}"
        else:
            marker = ""

        # Milestone line
        title_display = truncate(ms_title, w - 20)
        if is_active:
            lines.append(
                f"  {connector} {ms_icon} {C.BOLD}{C.WHITE}{title_display}{C.RESET}{marker}"
            )
        else:
            title_color = C.WHITE if ms_status == "done" else C.GRAY
            lines.append(
                f"  {connector} {ms_icon} {title_color}{title_display}{C.RESET}"
            )

        # Target date
        if target:
            date_color = C.YELLOW if is_active else C.DIM
            lines.append(
                f"  {child_prefix}  {date_color}目標: {target}{C.RESET}"
            )

        # Entries under this milestone
        entries = ms.get("entries", [])
        if entries and (is_active or ms_status == "done"):
            for j, entry in enumerate(entries):
                is_last_e = j == len(entries) - 1
                e_connector = "└─" if is_last_e else "├─"
                e_type = entry.get("type", "?")
                e_desc = truncate(entry.get("description", ""), w - 28)
                e_status = entry.get("status", "todo")

                # Type-specific icon and color
                if e_type == "commit":
                    meta = entry.get("meta", {})
                    e_hash = meta.get("hash", "")[:7]
                    lines.append(
                        f"  {child_prefix}  {e_connector} {C.MAGENTA}{e_hash}{C.RESET} {C.DIM}{e_desc}{C.RESET}"
                    )
                else:
                    status_mark = f"{C.GREEN}●{C.RESET}" if e_status == "done" else f"{C.GRAY}○{C.RESET}"
                    type_color = C.CYAN if e_type == "decision" else C.BLUE
                    lines.append(
                        f"  {child_prefix}  {e_connector} {status_mark} {type_color}[{e_type}]{C.RESET} {C.DIM}{e_desc}{C.RESET}"
                    )
        elif entries:
            lines.append(
                f"  {child_prefix}  {C.DIM}{len(entries)} entries{C.RESET}"
            )

        # Spacing between milestones
        if not is_last_ms:
            lines.append(f"  {child_prefix}")

    # Footer
    remaining_lines = term_height - len(lines) - 2
    if remaining_lines > 0:
        lines.extend([""] * remaining_lines)

    now = time.strftime("%H:%M:%S")
    lines.append(f"  {C.DIM}{'─' * (w - 4)}{C.RESET}")
    lines.append(f"  {C.DIM}Updated: {now}  |  beacon help{C.RESET}")

    return lines


def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: dashboard.py <path-to-project.json>")
        sys.exit(1)

    project_path = sys.argv[1]
    last_hash = None

    # Initial render
    clear_screen()

    while True:
        try:
            current_hash = file_hash(project_path)
            term_size = shutil.get_terminal_size((40, 24))
            w = term_size.columns
            h = term_size.lines

            if current_hash != last_hash or current_hash is None:
                project = load_project(project_path)
                if project is None:
                    clear_screen()
                    print(f"\n  {C.YELLOW}Waiting for project...{C.RESET}")
                    print(f"  {C.DIM}Run: beacon init{C.RESET}")
                else:
                    lines = render(project, w, h)
                    clear_screen()
                    for line in lines[:h]:
                        print(line)

                last_hash = current_hash

            time.sleep(2)

        except KeyboardInterrupt:
            clear_screen()
            sys.exit(0)


if __name__ == "__main__":
    main()
