#!/usr/bin/env python3
"""Beacon Dashboard - Curses-based interactive project milestone viewer."""

from commands import __version__ as BEACON_VERSION
from store import get_store

import curses
import json
import os
import sys
import time
import unicodedata


def char_width(ch):
    """Return display width of a character.

    Ambiguous-width chars (box drawing, bullets, block elements etc.)
    render as width 2 on ja_JP terminals, matching F/W.
    """
    eaw = unicodedata.east_asian_width(ch)
    return 2 if eaw in ('F', 'W', 'A') else 1


def display_width(text):
    """Return the display width of a string, accounting for wide characters."""
    return sum(char_width(ch) for ch in text)


def truncate_to_width(text, max_width):
    """Truncate text to fit within max_width display columns."""
    if not text:
        return ""
    width = 0
    result = []
    for ch in text:
        cw = char_width(ch)
        if width + cw > max_width:
            break
        result.append(ch)
        width += cw
    return "".join(result)


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
        self.store = get_store(project_path)
        self.store.start_watching()
        self.project = None
        self.scroll_offset = 0
        self.cursor_pos = 0  # Index into self.selectable
        self.expanded = set()  # Set of milestone IDs that are expanded
        self.expanded_entries = set()  # Set of entry IDs whose detail is shown
        self.hide_done = False  # Toggle to hide done entries
        self.summary_expanded = False  # Toggle to expand full summary
        self.view_mode = "project"  # "project", "retro", or "documents"
        self.retro_scroll = 0  # Scroll offset for retro view
        self.doc_scroll = 0  # Scroll offset for documents view
        self.doc_files = []  # List of document filenames
        self.doc_entries = []  # List of (filename, scope, title)
        self.doc_selected = 0  # Selected document index
        self.doc_content = None  # Content of currently viewed document
        self.header_lines = []  # Fixed header lines
        self.lines = []  # Scrollable body lines
        self.selectable = []  # List of (line_index, kind, key) for navigable rows
        # kind: "milestone" (key=ms_index) or "entry" (key=entry_id)

    def reload_if_changed(self):
        """Reload project data if store detects changes."""
        if not self.store.has_changed():
            return False
        try:
            self.project = self.store.load_project()
        except (FileNotFoundError, json.JSONDecodeError,
                RuntimeError, ConnectionError):
            self.project = None
        # Auto-expand active milestone
        if self.project:
            for ms in self.project.get("milestones", []):
                if ms.get("status") == "in_progress":
                    self.expanded.add(ms.get("id", ""))
        return True

    def _get_next_retro_date(self):
        """Calculate the next retro date from project's retro_day setting."""
        import datetime
        DAY_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                     "friday": 4, "saturday": 5, "sunday": 6}
        day_str = (self.project or {}).get("retro_day", "friday").lower()
        target_weekday = DAY_NAMES.get(day_str, 4)
        today = datetime.date.today()
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0:
            return f"{today} (今日)"
        next_date = today + datetime.timedelta(days=days_ahead)
        return str(next_date)

    def _auto_fire_retro_trigger(self):
        """Create retro trigger file if today is retro day and not yet reviewed."""
        import json as j
        import datetime
        project_dir = os.path.dirname(self.project_path)
        try:
            with open(self.project_path, "r") as f:
                data = j.load(f)
        except (IOError, j.JSONDecodeError):
            return
        day_names = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                     "friday": 4, "saturday": 5, "sunday": 6}
        retro_day = day_names.get(data.get("retro_day", "friday").lower(), 4)
        today = datetime.date.today()
        if today.weekday() != retro_day:
            return
        year, week, _ = today.isocalendar()
        current_week = f"{year}-W{week:02d}"
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
            j.dump(trigger_data, f, ensure_ascii=False)
            f.write("\n")

    def _get_pending_triggers(self):
        """Read pending triggers from .beacon/triggers/. Returns list of warning messages."""
        import json as j
        self._auto_fire_retro_trigger()
        project_dir = os.path.dirname(self.project_path)
        triggers_dir = os.path.join(project_dir, "triggers")
        if not os.path.isdir(triggers_dir):
            return []

        warnings = []
        for fname in sorted(os.listdir(triggers_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(triggers_dir, fname), "r") as f:
                    data = j.load(f)
                msg = data.get("message", "")
                if msg:
                    warnings.append(f"  \u26a0 {msg}")
            except (j.JSONDecodeError, IOError):
                pass
        return warnings

    @staticmethod
    def _parse_doc_frontmatter(text):
        """Parse frontmatter from document text. Returns (scope, body)."""
        if not text.startswith("---"):
            return "memo", text
        end = text.find("\n---", 3)
        if end == -1:
            return "memo", text
        header = text[4:end]
        body = text[end + 4:].lstrip("\n")
        scope = "memo"
        for line in header.split("\n"):
            line = line.strip()
            if line.startswith("scope:"):
                scope = line.split(":", 1)[1].strip()
        return scope, body

    def _load_doc_files(self):
        """Load document list from store (cloud API or local files)."""
        try:
            docs = self.store.list_documents()
        except Exception:
            docs = []
        self.doc_entries = []  # list of (doc_id, scope, title)
        for d in docs:
            self.doc_entries.append((d.get("doc_id", ""), d.get("scope", "memo"), d.get("title", "")))
        self.doc_files = [e[0] for e in self.doc_entries]
        self.doc_selected = min(self.doc_selected, max(0, len(self.doc_files) - 1))

    def _open_doc(self, index):
        """Load a document by index, stripping frontmatter for display."""
        if index < 0 or index >= len(self.doc_files):
            return
        doc_id = self.doc_files[index]
        try:
            result = self.store.get_document(doc_id)
            raw = result.get("content", "")
            _, body = self._parse_doc_frontmatter(raw)
            self.doc_content = (doc_id, body)
            self.doc_scroll = 0
        except Exception:
            self.doc_content = None

    def build_documents_lines(self, width):
        """Build display lines for documents view."""
        self.header_lines = []
        self.selectable = []

        if self.doc_content is not None:
            # Viewing a single document
            name, content = self.doc_content
            self.header_lines = [
                (f" BEACON DOCUMENTS | {name}  (h/ESC: back, D: project) ", "header"),
                ("", 0),
            ]
            lines = []
            for raw_line in content.splitlines():
                stripped = raw_line.rstrip()
                if stripped.startswith("# "):
                    style = "section"
                elif stripped.startswith("## "):
                    style = "section_yellow"
                elif stripped.startswith("### "):
                    style = "active"
                else:
                    style = curses.A_NORMAL
                for wl in wrap_line("  " + stripped, width - 1):
                    lines.append((wl, style))
            self.lines = lines
            return

        # Document list grouped by scope
        self.header_lines = [
            (f" BEACON DOCUMENTS | {len(self.doc_files)} files  (D: back to project) ", "header"),
            ("", 0),
        ]

        if not self.doc_files:
            self.lines = [("  No documents found. Add .md files to .beacon/documents/", curses.A_NORMAL)]
            return

        scope_order = ["core", "spec", "memo"]
        scope_labels = {"core": "\u2605 CORE", "spec": "\u25cb SPEC", "memo": "\u25cc MEMO"}
        # Group entries by scope
        grouped = {}
        for i, (fname, scope, title) in enumerate(self.doc_entries):
            grouped.setdefault(scope, []).append((i, title))

        # Build display order: indices into doc_entries, grouped by scope
        self._doc_display_order = []
        for scope in scope_order:
            for idx, _title in grouped.get(scope, []):
                self._doc_display_order.append(idx)

        lines = []
        for scope in scope_order:
            entries = grouped.get(scope, [])
            if not entries:
                continue
            label = scope_labels.get(scope, scope.upper())
            lines.append((f"  {label}", "section_yellow"))
            for idx, title in entries:
                display_pos = self._doc_display_order.index(idx)
                marker = " \u25b6 " if display_pos == self.doc_selected else "   "
                style = "active" if display_pos == self.doc_selected else curses.A_NORMAL
                lines.append((f"  {marker}{title}", style))
            lines.append(("", 0))
        self.lines = lines

    def _get_latest_retro(self):
        """Find the latest retro file in .beacon/retro/."""
        import glob as g
        project_dir = os.path.dirname(self.project_path)
        retro_dir = os.path.join(project_dir, "retro")
        files = sorted(g.glob(os.path.join(retro_dir, "*.md")))
        if not files:
            return None, None
        path = files[-1]
        try:
            with open(path, "r", encoding="utf-8") as f:
                return os.path.basename(path), f.read()
        except (FileNotFoundError, IOError):
            return None, None

    def build_retro_lines(self, width):
        """Build display lines for retro view."""
        self.header_lines = []
        self.selectable = []

        name, content = self._get_latest_retro()
        if not content:
            self.header_lines = [(" BEACON RETRO | No retro files found ", "header")]
            self.lines = [("  Run /beacon-retro to generate one.", curses.A_NORMAL)]
            return

        self.header_lines = [
            (f" BEACON RETRO | {name}  (r: back to project) ", "header"),
            ("", 0),
        ]

        lines = []
        for raw_line in content.splitlines():
            # Simple markdown styling
            stripped = raw_line.rstrip()
            if stripped.startswith("# "):
                style = "section"
            elif stripped.startswith("## "):
                style = "section_yellow"
            elif stripped.startswith("### "):
                style = "active"
            elif stripped.startswith("- **"):
                style = curses.A_NORMAL
            else:
                style = curses.A_NORMAL

            for wl in wrap_line("  " + stripped, width - 1):
                lines.append((wl, style))

        self.lines = lines

    def _add_line(self, lines, text, style, selectable_kind=None, selectable_key=None):
        """Append a line and optionally register it as selectable."""
        line_index = len(lines)
        lines.append((text, style))
        if selectable_kind is not None:
            self.selectable.append((line_index, selectable_kind, selectable_key))

    def build_lines(self, width):
        """Build the display lines from project data."""
        header_lines = []
        body_lines = []
        self.selectable = []
        if not self.project:
            header_lines.append(("  Waiting for project...", curses.A_NORMAL))
            header_lines.append(("  Run: beacon init", curses.A_NORMAL))
            self.header_lines = header_lines
            self.lines = body_lines
            return

        # Header (fixed at top)
        name = self.project.get("name", "")
        header_title = f" BEACON v{BEACON_VERSION}: Where humans and AI are bound together. "
        header_lines.append((header_title, "header"))
        if name:
            header_lines.append((f"  {name}", curses.A_BOLD))
        header_lines.append(("", 0))

        # Objective
        obj = self.project.get("objective", "(未設定)")
        header_lines.append(("  ◆ 大目的", "section"))
        for wl in wrap_line("    " + obj, width - 1):
            header_lines.append((wl, curses.A_NORMAL))
        header_lines.append(("", 0))

        # Summary (collapsible: 's' to toggle)
        summary = self.project.get("summary", "")
        if summary:
            max_preview_lines = 2
            all_lines = wrap_line("    " + summary, width - 1)
            needs_toggle = len(all_lines) > max_preview_lines
            toggle_hint = " [s: 展開]" if needs_toggle and not self.summary_expanded else ""
            toggle_hint = " [s: 折りたたみ]" if self.summary_expanded and needs_toggle else toggle_hint
            header_lines.append(("  ▶ 現状" + toggle_hint, "section_yellow"))
            if self.summary_expanded or not needs_toggle:
                for wl in all_lines:
                    header_lines.append((wl, curses.A_NORMAL))
            else:
                for wl in all_lines[:max_preview_lines]:
                    header_lines.append((wl, curses.A_NORMAL))
                header_lines.append(("    ...", curses.A_DIM))
            header_lines.append(("", 0))

        # Next retro date
        next_retro = self._get_next_retro_date()
        if next_retro:
            header_lines.append((f"  ◇ Next Review: {next_retro}", curses.A_DIM))
            header_lines.append(("", 0))

        # Pending triggers (from .beacon/triggers/)
        triggers = self._get_pending_triggers()
        for warning in triggers:
            header_lines.append((warning, "warning"))
        if triggers:
            header_lines.append(("", 0))

        # Overall progress
        milestones = self.project.get("milestones", [])
        done_count = sum(1 for m in milestones if m.get("status") in ("done", "observing"))
        total_count = len(milestones)
        bar_suffix = f"  {done_count}/{total_count} MS"
        bar_cols = max(4, width - 2 - display_width(bar_suffix) - 1)
        bar_count = bar_cols // 2  # █/░ are 2 display-cols each
        filled = int(bar_count * done_count / total_count) if total_count > 0 else 0
        bar_str = f"  {'█' * filled}{'░' * (bar_count - filled)}{bar_suffix}"
        header_lines.append((bar_str, "progress"))
        header_lines.append(("  " + "─" * ((width - 4) // 2), curses.A_NORMAL))
        header_lines.append(("", 0))

        self.header_lines = header_lines
        lines = body_lines

        # Milestones - filter done when hide_done is active
        if self.hide_done:
            milestones = [m for m in milestones if m.get("status") != "done"]

        for i, ms in enumerate(milestones):
            is_last = i == len(milestones) - 1
            status = ms.get("status", "todo")
            title = ms.get("title", "(無題)")
            ms_id = ms.get("id", "?")
            progress = ms.get("progress", 0)
            target = ms.get("target_date", "")
            is_active = status == "in_progress"
            is_expanded = ms_id in self.expanded

            # Status icon
            icon_map = {"done": "●", "in_progress": "◑",
                        "todo": "○", "waiting": "◌", "in_review": "◕",
                        "observing": "◕"}
            icon = icon_map.get(status, "?")

            # Tree connector
            connector = "└─" if is_last else "├─"
            child_prefix = "   " if is_last else "│  "

            # Milestone title line - fixed-width columns
            marker = " ◀ ACTIVE" if is_active else ""
            expand_hint = " [−]" if is_expanded else " [+]" if ms.get("entries") else ""

            # Fixed column: ID padded to 6 chars
            id_col = f"{ms_id:<6}"
            ms_line = f"  {connector} {icon} {id_col} {title}{marker}{expand_hint}"

            # Style - determined during draw based on cursor position
            if is_active:
                style = "active"
            elif status in ("done", "observing"):
                style = "done"
            else:
                style = "todo"
            self._add_line(lines, ms_line, style, "milestone", ms_id)

            # Progress bar - scales with terminal width
            date_info = f"  目標: {target}" if target else ""
            pct_col = f"{progress:>3}%"
            p_prefix = f"  {child_prefix}  "
            p_suffix = f" {pct_col}{date_info}"
            p_bar_cols = max(4, width - display_width(p_prefix) - display_width(p_suffix) - 1)
            p_bar_count = p_bar_cols // 2  # █/░ are 2 display-cols each
            p_filled = int(p_bar_count * progress / 100)
            p_line = f"{p_prefix}{'█' * p_filled}{'░' * (p_bar_count - p_filled)}{p_suffix}"
            p_style = "progress_active" if is_active else ("progress" if progress > 0 else curses.A_NORMAL)
            lines.append((p_line, p_style))

            # Entries (if expanded)
            entries = ms.get("entries", [])
            if is_expanded and entries:
                self._render_entries(lines, entries, f"  {child_prefix}  ", width)
            elif entries and not is_expanded:
                total = self._count_entries(entries)
                lines.append((f"  {child_prefix}  {total} entries", curses.A_NORMAL))

            # Spacing
            if not is_last:
                lines.append((f"  {child_prefix}", 0))

        # Footer (fixed at bottom, built during draw)
        self.footer_lines = []

        # Clamp cursor_pos to valid range
        if self.selectable:
            self.cursor_pos = max(0, min(self.cursor_pos, len(self.selectable) - 1))

        # Apply cursor highlight to the selected line
        if self.selectable:
            sel_line_idx, sel_kind, sel_key = self.selectable[self.cursor_pos]
            text, base_style = lines[sel_line_idx]
            if sel_kind == "milestone":
                ms_obj = next((m for m in milestones if m.get("id") == sel_key), {})
                ms_status = ms_obj.get("status", "todo")
                is_active = ms_status == "in_progress"
                lines[sel_line_idx] = (text, "selected_active" if is_active else "selected")
            else:
                lines[sel_line_idx] = (text, "selected")

        # Wrap body lines to fit terminal width
        # Track which pre-wrap line index maps to which wrapped line index
        wrapped = []
        self._line_index_map = {}  # pre-wrap index -> first wrapped index
        for orig_idx, (text, style) in enumerate(lines):
            self._line_index_map[orig_idx] = len(wrapped)
            for wl in wrap_line(text, width - 1):
                wrapped.append((wl, style))

        self.lines = wrapped

    def _render_entries(self, lines, entries, prefix, width, depth=0):
        """Render entries recursively, supporting nested task→commit grouping."""
        visible = [e for e in entries if e.get("status") != "cancelled"]
        for j, entry in enumerate(visible):
            is_last_e = j == len(visible) - 1
            e_connector = "└─" if is_last_e else "├─"
            e_type = entry.get("type", "?")
            e_desc = entry.get("description", "")
            e_status = entry.get("status", "todo")
            e_id = entry.get("id", "")
            has_detail = bool(entry.get("detail", ""))
            children = entry.get("entries", []) if e_type != "commit" else []
            is_expandable = has_detail or bool(children)
            entry_expanded = e_id in self.expanded_entries

            created = entry.get("created_at", entry.get("date", ""))
            done = entry.get("done_at", "")
            date_str = f" ({created}" if created else ""
            if done and done != created:
                date_str += f"→{done})"
            elif date_str:
                date_str += ")"

            expand_hint = ""
            if is_expandable:
                expand_hint = " [−]" if entry_expanded else " [+]"

            if e_type == "commit":
                meta = entry.get("meta", {})
                e_hash = meta.get("hash", "")[:7]
                id_col = f"[{e_id}]" if e_id else "[commit]"
                e_line = f"{prefix}{e_connector} {id_col} {e_desc} ({e_hash}){expand_hint}{date_str}"
                if is_expandable:
                    self._add_line(lines, e_line, "commit", "entry", e_id)
                else:
                    lines.append((e_line, "commit"))
            else:
                s_mark = "●" if e_status == "done" else "○"
                child_count = f" ({len(children)})" if children else ""
                id_col = f"[{e_id}]" if e_id else f"[{e_type:<6}]"
                e_line = f"{prefix}{e_connector} {s_mark} {id_col} {e_desc}{child_count}{expand_hint}{date_str}"
                style = "task" if e_status == "done" else curses.A_NORMAL
                # All task entries are selectable (expandable ones toggle, others just highlight)
                self._add_line(lines, e_line, style, "entry", e_id)

                # Render nested entries under this task (only when expanded)
                if children and entry_expanded:
                    nest_prefix = prefix + ("   " if is_last_e else "│  ")
                    self._render_entries(lines, children, nest_prefix, width, depth + 1)

            # Render detail block if expanded
            if entry_expanded and has_detail:
                detail_text = entry.get("detail", "")
                detail_prefix = prefix + ("   " if is_last_e else "│  ")
                for detail_line in detail_text.split("\n"):
                    for wl in wrap_line(f"{detail_prefix}  {detail_line}", width - 1):
                        lines.append((wl, "detail"))

    @staticmethod
    def _count_entries(entries):
        """Count total entries including nested."""
        total = 0
        for e in entries:
            total += 1
            total += Dashboard._count_entries(e.get("entries", []))
        return total

    def handle_key(self, key):
        """Handle keyboard input. Returns False to quit."""
        if key in (ord('q'), ord('Q')):
            return False
        if key == ord('r'):
            if self.view_mode == "project":
                self.view_mode = "retro"
                self.retro_scroll = 0
            else:
                self.view_mode = "project"
            return True
        if key == ord('D'):
            if self.view_mode == "project":
                self.view_mode = "documents"
                self.doc_scroll = 0
                self.doc_content = None
                self._load_doc_files()
            else:
                self.view_mode = "project"
            return True
        if self.view_mode == "retro":
            if key in (curses.KEY_UP, ord('k')):
                self.retro_scroll = max(0, self.retro_scroll - 1)
            elif key in (curses.KEY_DOWN, ord('j')):
                self.retro_scroll += 1
            return True
        if self.view_mode == "documents":
            if self.doc_content is not None:
                # Viewing a document: scroll or go back
                if key in (curses.KEY_UP, ord('k')):
                    self.doc_scroll = max(0, self.doc_scroll - 1)
                elif key in (curses.KEY_DOWN, ord('j')):
                    self.doc_scroll += 1
                elif key in (27, ord('h'), curses.KEY_LEFT):  # ESC or h or left
                    self.doc_content = None
                    self.doc_scroll = 0
            else:
                # Document list: navigate and select (in display order)
                display_count = len(getattr(self, '_doc_display_order', []))
                if key in (curses.KEY_UP, ord('k')):
                    self.doc_selected = max(0, self.doc_selected - 1)
                elif key in (curses.KEY_DOWN, ord('j')):
                    if display_count:
                        self.doc_selected = min(display_count - 1, self.doc_selected + 1)
                elif key in (curses.KEY_ENTER, 10, 13, ord(' ')):
                    if display_count:
                        actual_idx = self._doc_display_order[self.doc_selected]
                        self._open_doc(actual_idx)
            return True
        if not self.selectable:
            return True

        sel_count = len(self.selectable)

        if key in (ord('q'), ord('Q')):
            return False
        elif key in (curses.KEY_UP, ord('k')):
            if self.cursor_pos > 0:
                self.cursor_pos -= 1
        elif key in (curses.KEY_DOWN, ord('j')):
            if self.cursor_pos < sel_count - 1:
                self.cursor_pos += 1
        elif key in (curses.KEY_ENTER, 10, 13, ord(' ')):
            _, kind, sel_key = self.selectable[self.cursor_pos]
            if kind == "milestone":
                if sel_key in self.expanded:
                    self.expanded.discard(sel_key)
                else:
                    self.expanded.add(sel_key)
            elif kind == "entry":
                if sel_key in self.expanded_entries:
                    self.expanded_entries.discard(sel_key)
                else:
                    self.expanded_entries.add(sel_key)
        elif key == ord('d'):
            self.hide_done = not self.hide_done
        elif key == ord('s'):
            self.summary_expanded = not self.summary_expanded
        return True

    def draw(self, stdscr):
        """Main draw loop."""
        curses.curs_set(0)
        stdscr.timeout(500)  # 500ms non-blocking getch for auto-refresh

        # Init color pairs
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)      # section
        curses.init_pair(2, curses.COLOR_YELLOW, -1)    # section_yellow
        curses.init_pair(12, curses.COLOR_BLUE, -1)    # active (in_progress)
        curses.init_pair(3, curses.COLOR_GREEN, -1)     # done / progress
        curses.init_pair(4, curses.COLOR_WHITE, -1)     # normal
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)   # commit
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_BLUE) # selected_active
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)   # header
        curses.init_pair(9, curses.COLOR_BLUE, -1)      # task
        curses.init_pair(10, curses.COLOR_CYAN, -1)    # detail
        curses.init_pair(11, curses.COLOR_WHITE, curses.COLOR_RED)  # warning

        style_map = {
            "header": curses.color_pair(8) | curses.A_BOLD,
            "section": curses.color_pair(1) | curses.A_BOLD,
            "section_yellow": curses.color_pair(2) | curses.A_BOLD,
            "active": curses.color_pair(12) | curses.A_BOLD,
            "done": curses.color_pair(3),
            "todo": curses.color_pair(4),
            "progress": curses.color_pair(3),
            "progress_active": curses.color_pair(12),
            "commit": curses.color_pair(5),
            "task": curses.color_pair(9),
            "detail": curses.color_pair(10),
            "warning": curses.color_pair(11) | curses.A_BOLD,
            "selected": curses.color_pair(6),
            "selected_active": curses.color_pair(7) | curses.A_BOLD,
        }

        _last_size = (0, 0)  # Force size mismatch on first iteration
        _needs_redraw = True
        _last_trigger_check = 0  # epoch seconds of last trigger check

        while True:
            height, width = stdscr.getmaxyx()

            # Size changed: endwin/refresh to fully re-initialize stdscr.
            # resizeterm() alone doesn't rebuild the internal buffer,
            # so addstr beyond the original dimensions silently fails.
            # This only fires on actual size changes (not every loop).
            if (height, width) != _last_size:
                curses.endwin()
                stdscr.refresh()
                height, width = stdscr.getmaxyx()
                _last_size = (height, width)
                _needs_redraw = True

            # Periodic trigger check (every 5 minutes, independent of data changes)
            _now = time.time()
            if _now - _last_trigger_check > 1800:
                _last_trigger_check = _now
                _needs_redraw = True

            # Check for data changes only when idle (skip if already need redraw)
            if not _needs_redraw and self.reload_if_changed():
                _needs_redraw = True

            if not _needs_redraw:
                key = stdscr.getch()
                if key == curses.KEY_RESIZE:
                    _needs_redraw = True
                elif key != -1:
                    if not self.handle_key(key):
                        break
                    _needs_redraw = True
                continue

            _needs_redraw = False

            # Build display
            if self.view_mode == "retro":
                self.build_retro_lines(width)
            elif self.view_mode == "documents":
                self.build_documents_lines(width)
            else:
                self.build_lines(width)

            # Build footer lines
            done_hint = "d:show done" if self.hide_done else "d:hide done"
            mode_label = "☁ Cloud" if self.store.is_cloud() else "💾 Local"
            if self.view_mode == "documents":
                if self.doc_content:
                    footer_hint = f"  {mode_label}  ↑↓:scroll  h:back  D:project  q:quit"
                else:
                    footer_hint = f"  {mode_label}  ↑↓:select  Enter:open  D:project  q:quit"
            elif self.view_mode == "retro":
                footer_hint = f"  {mode_label}  ↑↓:scroll  r:project  q:quit"
            else:
                footer_hint = f"  {mode_label}  ↑↓:move  Enter:expand  {done_hint}  r:review  D:docs  q:quit"
            footer_lines = [
                ("  " + "─" * ((width - 4) // 2), curses.A_NORMAL),
                (footer_hint, curses.A_NORMAL),
            ]

            # Calculate layout: fixed header + scrollable body + fixed footer
            header_count = len(getattr(self, 'header_lines', []))
            footer_count = len(footer_lines)
            body_height = height - header_count - footer_count
            if body_height < 1:
                body_height = 1

            # Adjust scroll to keep cursor visible within body area
            if self.view_mode == "retro":
                max_scroll = max(0, len(self.lines) - body_height)
                self.retro_scroll = min(self.retro_scroll, max_scroll)
                self.scroll_offset = self.retro_scroll
            elif self.view_mode == "documents" and self.doc_content:
                max_scroll = max(0, len(self.lines) - body_height)
                self.doc_scroll = min(self.doc_scroll, max_scroll)
                self.scroll_offset = self.doc_scroll
            elif self.view_mode == "documents":
                self.scroll_offset = 0
            else:
                self._adjust_scroll(body_height)

            # Render
            stdscr.clear()

            # Draw fixed header
            for row, (text, style) in enumerate(self.header_lines):
                if row >= height:
                    break
                try:
                    if isinstance(style, str):
                        attr = style_map.get(style, curses.A_NORMAL)
                    else:
                        attr = style
                    safe = truncate_to_width(text, width - 1) if text else ""
                    stdscr.addstr(row, 0, safe, attr)
                except curses.error:
                    pass

            # Draw scrollable body
            visible_lines = self.lines[self.scroll_offset:self.scroll_offset + body_height]
            for i, (text, style) in enumerate(visible_lines):
                row = header_count + i
                if row >= height:
                    break
                try:
                    if isinstance(style, str):
                        attr = style_map.get(style, curses.A_NORMAL)
                    else:
                        attr = style
                    safe = truncate_to_width(text, width - 1) if text else ""
                    stdscr.addstr(row, 0, safe, attr)
                except curses.error:
                    pass

            # Draw fixed footer
            for i, (text, style) in enumerate(footer_lines):
                row = height - footer_count + i
                if row < header_count:
                    continue  # Skip if terminal too small
                try:
                    if isinstance(style, str):
                        attr = style_map.get(style, curses.A_NORMAL)
                    else:
                        attr = style
                    safe = truncate_to_width(text, width - 1) if text else ""
                    stdscr.addstr(row, 0, safe, attr)
                except curses.error:
                    pass

            stdscr.refresh()

            # Handle input after draw
            key = stdscr.getch()
            if key == curses.KEY_RESIZE:
                _needs_redraw = True
            elif key != -1:
                if not self.handle_key(key):
                    break
                _needs_redraw = True

    def _adjust_scroll(self, height):
        """Ensure the selected item is visible."""
        if not self.selectable:
            return

        # Find the wrapped line index of the selected item
        sel_idx = self.cursor_pos
        sel_line_idx, _, _ = self.selectable[sel_idx]
        target_line = self._line_index_map.get(sel_line_idx, 0)

        # Find the end of this entry (start of next selectable, or end of lines)
        if sel_idx + 1 < len(self.selectable):
            next_line_idx, _, _ = self.selectable[sel_idx + 1]
            target_end = self._line_index_map.get(next_line_idx, len(self.lines)) - 1
        else:
            target_end = len(self.lines) - 1

        # Adjust scroll so both the start and end of the entry are visible
        margin = 2
        if target_end > self.scroll_offset + height - 1:
            self.scroll_offset = target_end - height + 1
        if target_line < self.scroll_offset + margin:
            self.scroll_offset = max(0, target_line - margin)

        # Clamp
        max_scroll = max(0, len(self.lines) - height)
        self.scroll_offset = min(self.scroll_offset, max_scroll)


def main():
    if len(sys.argv) < 2:
        print("Usage: dashboard.py <path-to-project.json>")
        sys.exit(1)

    project_path = sys.argv[1]
    dashboard = Dashboard(project_path)

    try:
        curses.wrapper(dashboard.draw)
    finally:
        dashboard.store.stop_watching()


if __name__ == "__main__":
    main()
