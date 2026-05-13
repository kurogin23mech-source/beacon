"""Beacon LocalStore - File-based project storage.

Wraps the existing JSON file I/O pattern, implementing the Store protocol.
"""

from __future__ import annotations

import hashlib
import json
import os


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
            data = json.load(f)
        self._last_hash = self._file_hash()
        return data

    def save_project(self, data: dict) -> None:
        with open(self._project_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
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
