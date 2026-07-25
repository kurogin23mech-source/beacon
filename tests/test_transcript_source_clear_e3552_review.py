"""CLI-layer tests for the #498 review fix — empty BEACON_TS_TYPE must not silently
clear an Account's transcript_source declaration.

- empty/unset BEACON_TS_TYPE (no clear token) → exit 1, existing declaration kept.
- BEACON_TS_CLEAR=1 → clears.
- drive_folder without folder_id → error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import commands  # noqa: E402
import sales_entities as se  # noqa: E402


def _project(tmp_path, monkeypatch):
    data = se.build_sales_project("TS Sales", "close deals")
    se.account_add(data, "Globex")  # acc-1
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def _ts(cwd):
    data = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return se.get_account_transcript_source(data, "acc-1")


def _clear_env(monkeypatch):
    for k in ("BEACON_ACCOUNT_ID", "BEACON_TS_TYPE", "BEACON_TS_CLEAR",
              "BEACON_TS_FOLDER_ID", "BEACON_TS_NAMING", "BEACON_TS_TOOL"):
        monkeypatch.delenv(k, raising=False)


def test_set_declaration(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_TYPE", "meet_calendar")
    commands.cmd_sales_account_transcript_source_set()
    assert _ts(cwd) == {"type": "meet_calendar"}


def test_unset_type_errors_and_keeps_existing(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_TYPE", "manual")
    commands.cmd_sales_account_transcript_source_set()
    capsys.readouterr()
    monkeypatch.delenv("BEACON_TS_TYPE", raising=False)  # forgot to pass it
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_transcript_source_set()
    assert ei.value.code == 1
    assert "既存宣言を消しません" in capsys.readouterr().err
    assert _ts(cwd) == {"type": "manual"}   # untouched


def test_explicit_clear_token(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_TYPE", "external")
    monkeypatch.setenv("BEACON_TS_TOOL", "tl;dv")
    commands.cmd_sales_account_transcript_source_set()
    capsys.readouterr()
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_CLEAR", "1")
    commands.cmd_sales_account_transcript_source_set()
    assert _ts(cwd) is None


def test_drive_folder_without_folder_id_errors(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_TYPE", "drive_folder")  # no folder_id
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_transcript_source_set()
    assert ei.value.code == 1
    assert "folder_id" in capsys.readouterr().err


def test_clear_plus_type_stale_env_rejected(tmp_path, monkeypatch, capsys):
    # #498 再レビュー (AX high): 前回 export した BEACON_TS_TYPE が残ったまま CLEAR=1 を
    # 立てると『set のつもりが残留 clear で delete』に化ける。共存は exit1 で拒否、既存保持。
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_ID", "acc-1")
    monkeypatch.setenv("BEACON_TS_TYPE", "meet_calendar")
    commands.cmd_sales_account_transcript_source_set()
    capsys.readouterr()
    # BEACON_TS_TYPE を消し忘れたまま CLEAR=1 (stale env)
    monkeypatch.setenv("BEACON_TS_CLEAR", "1")   # BEACON_TS_TYPE はまだ meet_calendar
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_transcript_source_set()
    assert ei.value.code == 1
    assert "clear と設定値が同時" in capsys.readouterr().err
    assert _ts(cwd) == {"type": "meet_calendar"}   # 既存は消えず保持
