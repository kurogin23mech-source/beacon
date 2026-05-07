#!/usr/bin/env python3
"""Beacon Dashboard - Curses-based interactive project milestone viewer."""

import curses
import json
import hashlib
import sys
import time
import unicodedata


def load_project(path):
    """Load project.json, return None on error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def char_width(ch):
    """Return display width of a character (2 for fullwidth/wide, 1 otherwise)."""
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ('F', 'W') else 1


def display_width(text):
    """Return the display width of a string, accounting for wide characters."""
    return sum(char_width(ch) for ch in text)


def wrap_line(text, max_width, cont_indent=None):
    """Wrap a line to fit within max_width, returning a list of strings.

    cont_indent: number of spaces for continuation lines.
                 If None, auto-detects from leading whitespace + 2.
    """
    if not text or display_width(text) <= max_width:
        return [text]

    if cont_indent is None:
        stripped = text.lstrip()
        cont_indent = len(text) - len(stripped) + 2

    result = []
    current = ""
    current_width = 0

    for ch in text:
        cw = char_width(ch)
        if current_width + cw > max_width:
            result.append(current)
            current = " " * cont_indent + ch
            current_width = cont_indent + cw
        else:
            current += ch
            current_width += cw

    if current:
        result.append(current)

    return result if result else [""]


class Dashboard:
    def __init__(self, project_path):
        self.project_path = project_path
        self.project = None
        self.last_hash = None
        self.scroll_offset = 0
        self.cursor_pos = 0  # Which milestone is selected
        self.expanded = set()  # Set of milestone indices that are expanded
        self.lines = []  # Rendered lines buffer

    def reload_if_changed(self):
        """Reload project data if file changed."""
        current_hash = file_hash(self.project_path)
        if current_hash != self.last_hash:
            self.project = load_project(self.project_path)
            self.last_hash = current_hash
            # Auto-expand active milestone
            if self.project:
                for i, ms in enumerate(self.project.get("milestones", [])):
                    if ms.get("status") == "in_progress":
                        self.expanded.add(i)
            return True
        return False

    def build_lines(self, width):
        """Build the display lines from project data."""
        lines = []
        if not self.project:
            lines.append(("  Waiting for project...", curses.A_DIM))
            lines.append(("  Run: beacon init", curses.A_DIM))
            self.lines = lines
            return

        # Header
        lines.append((" BEACON ", "header"))
        lines.append(("", 0))

        # Objective
        obj = self.project.get("objective", "(未設定)")
        lines.append(("  ◆ 大目的", "section"))
        lines.append(("    " + obj, curses.A_NORMAL))
        lines.append(("", 0))

        # Summary
        summary = self.project.get("summary", "")
        if summary:
            lines.append(("  ▶ 現状", "section_yellow"))
            lines.append(("    " + summary, curses.A_NORMAL))
            lines.append(("", 0))

        # Overall progress
        milestones = self.project.get("milestones", [])
        done_count = sum(1 for m in milestones if m.get("status") == "done")
        total_count = len(milestones)
        bar_w = min(width - 20, 30)
        filled = int(bar_w * done_count / total_count) if total_count > 0 else 0
        bar_str = f"  {'█' * filled}{'░' * (bar_w - filled)}  {done_count}/{total_count} MS"
        lines.append((bar_str, "progress"))
        lines.append(("  " + "─" * (width - 4), curses.A_DIM))
        lines.append(("", 0))

        # Milestones
        for i, ms in enumerate(milestones):
            is_last = i == len(milestones) - 1
            status = ms.get("status", "todo")
            title = ms.get("title", "(無題)")
            ms_id = ms.get("id", "?")
            progress = ms.get("progress", 0)
            target = ms.get("target_date", "")
            is_active = status == "in_progress"
            is_selected = i == self.cursor_pos
            is_expanded = i in self.expanded

            # Status icon
            icon_map = {"done": "●", "in_progress": "◐",
                        "todo": "○", "waiting": "◌", "in_review": "◑"}
            icon = icon_map.get(status, "?")

            # Tree connector
            connector = "└─" if is_last else "├─"
            child_prefix = "   " if is_last else "│  "

            # Milestone title line
            marker = " ◀ ACTIVE" if is_active else ""
            expand_hint = " [−]" if is_expanded else " [+]" if ms.get("entries") else ""

            ms_line = f"  {connector} {icon} {ms_id} {title}{marker}{expand_hint}"

            if is_selected:
                style = "selected_active" if is_active else "selected"
            elif is_active:
                style = "active"
            elif status == "done":
                style = "done"
            else:
                style = "todo"
            lines.append((ms_line, style))

            # Progress bar
            p_bar_w = min(15, width - 20)
            p_filled = int(p_bar_w * progress / 100)
            date_info = f"  目標: {target}" if target else ""
            p_line = f"  {child_prefix}  {'█' * p_filled}{'░' * (p_bar_w - p_filled)} {progress}%{date_info}"
            lines.append((p_line, "progress" if progress > 0 else curses.A_DIM))

            # Entries (if expanded)
            entries = ms.get("entries", [])
            if is_expanded and entries:
                for j, entry in enumerate(entries):
                    is_last_e = j == len(entries) - 1
                    e_connector = "└─" if is_last_e else "├─"
                    e_type = entry.get("type", "?")
                    e_desc = entry.get("description", "")
                    e_status = entry.get("status", "todo")

                    if e_type == "commit":
                        meta = entry.get("meta", {})
                        e_hash = meta.get("hash", "")[:7]
                        e_line = f"  {child_prefix}  {e_connector} {e_hash} {e_desc}"
                        lines.append((e_line, "commit"))
                    else:
                        s_mark = "●" if e_status == "done" else "○"
                        e_line = f"  {child_prefix}  {e_connector} {s_mark} [{e_type}] {e_desc}"
                        lines.append((e_line, "task" if e_status == "done" else curses.A_DIM))
            elif entries and not is_expanded:
                lines.append((f"  {child_prefix}  {len(entries)} entries", curses.A_DIM))

            # Spacing
            if not is_last:
                lines.append((f"  {child_prefix}", 0))

        # Footer hint
        lines.append(("", 0))
        lines.append(("  " + "─" * (width - 4), curses.A_DIM))
        now = time.strftime("%H:%M:%S")
        lines.append((f"  {now}  |  ↑↓:move  Enter:expand  q:quit", curses.A_DIM))

        # Wrap all lines to fit terminal width
        wrapped = []
        for text, style in lines:
            for wl in wrap_line(text, width - 1):
                wrapped.append((wl, style))

        self.lines = wrapped

    def handle_key(self, key):
        """Handle keyboard input. Returns False to quit."""
        milestones = self.project.get("milestones", []) if self.project else []
        ms_count = len(milestones)

        if key in (ord('q'), ord('Q')):
            return False
        elif key in (curses.KEY_UP, ord('k')):
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
        elif key in (curses.KEY_DOWN, ord('j')):
            if self.cursor_pos < ms_count - 1:
                self.cursor_pos += 1
        elif key in (curses.KEY_ENTER, 10, 13, ord(' ')):
            if self.cursor_pos in self.expanded:
                self.expanded.discard(self.cursor_pos)
            else:
                self.expanded.add(self.cursor_pos)
        return True

    def draw(self, stdscr):
        """Main draw loop."""
        curses.curs_set(0)
        stdscr.timeout(500)  # 500ms non-blocking getch for auto-refresh

        # Init color pairs
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)      # section
        curses.init_pair(2, curses.COLOR_YELLOW, -1)    # section_yellow / active
        curses.init_pair(3, curses.COLOR_GREEN, -1)     # done / progress
        curses.init_pair(4, curses.COLOR_WHITE, -1)     # normal
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)   # commit
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW) # selected_active
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)   # header
        curses.init_pair(9, curses.COLOR_BLUE, -1)      # task

        style_map = {
            "header": curses.color_pair(8) | curses.A_BOLD,
            "section": curses.color_pair(1) | curses.A_BOLD,
            "section_yellow": curses.color_pair(2) | curses.A_BOLD,
            "active": curses.color_pair(2) | curses.A_BOLD,
            "done": curses.color_pair(3),
            "todo": curses.A_DIM,
            "progress": curses.color_pair(3),
            "commit": curses.color_pair(5),
            "task": curses.color_pair(9),
            "selected": curses.color_pair(6),
            "selected_active": curses.color_pair(7) | curses.A_BOLD,
        }

        while True:
            height, width = stdscr.getmaxyx()

            # Reload project data
            self.reload_if_changed()

            # Build display
            self.build_lines(width)

            # Adjust scroll to keep cursor visible
            # Find which line the cursor milestone starts at
            self._adjust_scroll(height)

            # Render
            stdscr.erase()
            visible_lines = self.lines[self.scroll_offset:self.scroll_offset + height]
            for row, (text, style) in enumerate(visible_lines):
                if row >= height:
                    break
                try:
                    if isinstance(style, str):
                        attr = style_map.get(style, curses.A_NORMAL)
                    else:
                        attr = style
                    stdscr.addstr(row, 0, text if text else "", attr)
                except curses.error:
                    pass

            stdscr.refresh()

            # Handle input
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                stdscr.clear()
                continue
            elif key != -1:
                if not self.handle_key(key):
                    break

    def _adjust_scroll(self, height):
        """Ensure the selected milestone is visible."""
        # Find approximate line of current cursor milestone in the lines buffer
        # Simple heuristic: scan for the milestone line
        milestones = self.project.get("milestones", []) if self.project else []
        if not milestones:
            return

        target_id = milestones[self.cursor_pos].get("id", "")
        target_line = 0
        for i, (text, _) in enumerate(self.lines):
            if target_id and target_id in text:
                target_line = i
                break

        # Adjust scroll so target is visible (with some margin)
        margin = 3
        if target_line < self.scroll_offset + margin:
            self.scroll_offset = max(0, target_line - margin)
        elif target_line > self.scroll_offset + height - margin - 1:
            self.scroll_offset = target_line - height + margin + 1

        # Clamp
        max_scroll = max(0, len(self.lines) - height)
        self.scroll_offset = min(self.scroll_offset, max_scroll)


def main():
    if len(sys.argv) < 2:
        print("Usage: dashboard.py <path-to-project.json>")
        sys.exit(1)

    project_path = sys.argv[1]
    dashboard = Dashboard(project_path)

    curses.wrapper(dashboard.draw)


if __name__ == "__main__":
    main()
