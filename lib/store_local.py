"""Beacon LocalStore - File-based project storage.

Wraps the existing JSON file I/O pattern, implementing the Store protocol.
"""

from __future__ import annotations

import hashlib
import json
import os

from _file_lock import lock_exclusive, lock_shared, unlock


class LocalStore:
    """Store implementation backed by a local JSON file."""

    def __init__(self, project_file: str):
        self._project_file = project_file
        self._last_hash: str | None = None

    @property
    def project_file(self) -> str:
        return self._project_file

    def load_project(self) -> dict:
        with open(self._project_file, "r", encoding="utf-8") as f:
            lock_shared(f)
            data = json.load(f)
            unlock(f)
        self._last_hash = self._file_hash()
        return data

    def save_project(self, data: dict) -> None:
        with open(self._project_file, "r+", encoding="utf-8") as f:
            lock_exclusive(f)
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            unlock(f)
        self._last_hash = self._file_hash()

    def has_changed(self) -> bool:
        """Check if the file has changed since last load/save.

        Returns True on first call (before any load/save) to trigger initial load.
        """
        current = self._file_hash()
        if self._last_hash is None:
            self._last_hash = current
            return True
        if current != self._last_hash:
            self._last_hash = current
            return True
        return False

    def is_cloud(self) -> bool:
        return False

    def list_documents(self) -> list:
        """List documents from local .beacon/documents/."""
        import glob as g
        doc_dir = os.path.join(os.path.dirname(self._project_file), "documents")
        if not os.path.isdir(doc_dir):
            return []
        results = []
        for fpath in sorted(g.glob(os.path.join(doc_dir, "*.md"))):
            fname = os.path.basename(fpath)
            doc_id = fname[:-3]
            scope, title, milestone = "memo", doc_id, ""
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    raw = f.read()
                # Parse frontmatter
                if raw.startswith("---"):
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        for line in parts[1].strip().splitlines():
                            if line.startswith("scope:"):
                                scope = line.split(":", 1)[1].strip()
                            elif line.startswith("milestone:"):
                                milestone = line.split(":", 1)[1].strip()
                        body = parts[2]
                    else:
                        body = raw
                else:
                    body = raw
                for line in body.strip().splitlines():
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
            except (IOError, UnicodeDecodeError):
                pass
            entry = {"doc_id": doc_id, "title": title, "scope": scope}
            if milestone:
                entry["milestone"] = milestone
            results.append(entry)
        return results

    def get_document(self, doc_id: str) -> dict:
        """Get a single document from local .beacon/documents/."""
        doc_dir = os.path.join(os.path.dirname(self._project_file), "documents")
        fpath = os.path.join(doc_dir, f"{doc_id}.md")
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            return {"doc_id": doc_id, "content": content}
        except (FileNotFoundError, IOError):
            return {}

    def start_watching(self) -> None:
        pass

    def stop_watching(self) -> None:
        pass

    def _file_hash(self) -> str | None:
        try:
            with open(self._project_file, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except FileNotFoundError:
            return None
