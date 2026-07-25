"""CLI-layer tests for e-3760 — 営業エンティティの直叩き → skill soft 誘導 (nudge).

The user-facing sales verbs (``opportunity add`` / ``account add`` /
``doc add --account``) print a soft nudge toward the matching ``/beacon-sales-*``
skill when called directly, **but still execute** (hard block はしない, master=
人間). A skill's own call sets ``BEACON_SALES_SKILL_CALL=1`` to suppress the nudge.
Dev / non-account doc adds are unaffected. The nudge goes to stderr so ``--json``
stdout stays clean.
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

NUDGE = "💡"


def _project(tmp_path, monkeypatch, data: dict) -> Path:
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    return cwd


@pytest.fixture
def sales_cwd(tmp_path, monkeypatch):
    data = se.build_sales_project("Nudge Sales", "close deals")
    se.account_add(data, "顧客A", phase="リード")
    return _project(tmp_path, monkeypatch, data)


def _clear(monkeypatch):
    for k in ("BEACON_SALES_SKILL_CALL", "BEACON_OPP_TITLE", "BEACON_OPP_ACCOUNT",
              "BEACON_ACCOUNT_NAME", "BEACON_TITLE", "BEACON_CONTENT",
              "BEACON_SCOPE", "BEACON_ACCOUNT", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def test_account_add_direct_nudges_but_executes(sales_cwd, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_NAME", "Acme")
    commands.cmd_account_add()
    err = capsys.readouterr().err
    assert NUDGE in err and "/beacon-sales-card" in err
    # still executed → the account was persisted
    assert any(a["name"] == "Acme" for a in _read_accounts(sales_cwd))


def test_account_add_with_skill_token_is_silent(sales_cwd, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_ACCOUNT_NAME", "Beta")
    monkeypatch.setenv("BEACON_SALES_SKILL_CALL", "1")
    commands.cmd_account_add()
    err = capsys.readouterr().err
    assert NUDGE not in err
    assert any(a["name"] == "Beta" for a in _read_accounts(sales_cwd))


def test_opportunity_add_direct_nudges(sales_cwd, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_OPP_TITLE", "Deal1")
    monkeypatch.setenv("BEACON_OPP_ACCOUNT", "acc-1")
    commands.cmd_opportunity_add()
    err = capsys.readouterr().err
    assert NUDGE in err and "/beacon-sales-opportunity" in err


def test_opportunity_add_with_skill_token_is_silent(sales_cwd, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_OPP_TITLE", "Deal2")
    monkeypatch.setenv("BEACON_OPP_ACCOUNT", "acc-1")
    monkeypatch.setenv("BEACON_SALES_SKILL_CALL", "1")
    commands.cmd_opportunity_add()
    assert NUDGE not in capsys.readouterr().err


def test_doc_add_with_account_nudges_to_dossier(sales_cwd, monkeypatch, capsys):
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TITLE", "顧客メモ")
    monkeypatch.setenv("BEACON_SCOPE", "spec")
    monkeypatch.setenv("BEACON_CONTENT", "x")
    monkeypatch.setenv("BEACON_ACCOUNT", "acc-1")
    commands.cmd_doc_add()
    err = capsys.readouterr().err
    assert NUDGE in err and "/beacon-sales-dossier" in err


def test_doc_add_without_account_does_not_nudge(sales_cwd, monkeypatch, capsys):
    # dev / 一般 doc add (no --account) must stay silent — the nudge is sales-only.
    _clear(monkeypatch)
    monkeypatch.setenv("BEACON_TITLE", "設計メモ")
    monkeypatch.setenv("BEACON_SCOPE", "core")
    monkeypatch.setenv("BEACON_CONTENT", "y")
    commands.cmd_doc_add()
    assert NUDGE not in capsys.readouterr().err


def _read_accounts(cwd: Path) -> list:
    data = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return data.get("accounts", [])
