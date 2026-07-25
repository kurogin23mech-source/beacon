"""CLI-layer tests for the #496 review fix — empty env must not silently clear a
send-account signature.

- BEACON_SEND_SIGNATURE empty/unset (no clear token) → exit 1, existing signature kept.
- BEACON_SEND_SIGNATURE_CLEAR=1 → clears.
- BEACON_SEND_SIGNATURE non-empty → sets.
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
    data = se.build_sales_project("Sig Sales", "close deals")
    se.add_send_account(data, "会社", "sales@corp.example",
                        routes={"gmail": {"namespace": "mcp__gmail"}})
    se.set_send_identity(data, "会社")
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


def _sig(cwd):
    data = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return se.get_send_account(data, "会社").get("signature")


def _clear_env(monkeypatch):
    for k in ("BEACON_SEND_LABEL", "BEACON_SEND_SIGNATURE", "BEACON_SEND_SIGNATURE_CLEAR"):
        monkeypatch.delenv(k, raising=False)


def test_set_signature(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_LABEL", "会社")
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "── 太郎")
    commands.cmd_sales_account_signature()
    assert _sig(cwd) == "── 太郎"


def test_unset_signature_env_errors_and_keeps_existing(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_LABEL", "会社")
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "既存")
    commands.cmd_sales_account_signature()
    capsys.readouterr()
    # now call again WITHOUT the signature env (the forgotten-env footgun)
    monkeypatch.delenv("BEACON_SEND_SIGNATURE", raising=False)
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_signature()
    assert ei.value.code == 1
    assert "既存署名を消しません" in capsys.readouterr().err
    assert _sig(cwd) == "既存"          # untouched — no silent clear


def test_blank_signature_env_errors_and_keeps_existing(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_LABEL", "会社")
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "既存")
    commands.cmd_sales_account_signature()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "   ")  # blank / quoting mistake
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_signature()
    assert ei.value.code == 1
    assert _sig(cwd) == "既存"


def test_explicit_clear_token_removes_signature(tmp_path, monkeypatch, capsys):
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_LABEL", "会社")
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "消す前")
    commands.cmd_sales_account_signature()
    capsys.readouterr()
    monkeypatch.delenv("BEACON_SEND_SIGNATURE", raising=False)
    monkeypatch.setenv("BEACON_SEND_SIGNATURE_CLEAR", "1")
    commands.cmd_sales_account_signature()
    assert _sig(cwd) is None


def test_clear_plus_value_stale_env_rejected(tmp_path, monkeypatch, capsys):
    # #496 再レビュー (AX high): 前回 export した BEACON_SEND_SIGNATURE が残ったまま CLEAR=1 を
    # 立てると『set のつもりが残留 clear で delete』に化ける。共存は exit1 で拒否、既存保持。
    cwd = _project(tmp_path, monkeypatch)
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_SEND_LABEL", "会社")
    monkeypatch.setenv("BEACON_SEND_SIGNATURE", "既存署名")
    commands.cmd_sales_account_signature()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_SEND_SIGNATURE_CLEAR", "1")  # SIGNATURE はまだ残っている (stale)
    with pytest.raises(SystemExit) as ei:
        commands.cmd_sales_account_signature()
    assert ei.value.code == 1
    assert "clear と署名の値が同時" in capsys.readouterr().err
    assert _sig(cwd) == "既存署名"   # 既存は消えず保持
