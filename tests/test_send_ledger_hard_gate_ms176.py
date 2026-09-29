"""ms-176 e-6608 — 送信アカウント台帳は「空のまま運用開始」を許さない (hard gate)。

営業の既定は permissive (警告するが止めない、master=人間)。この 1 点だけは意図的な例外に
する: 送信アカウントの取り違えは「別人格の Google アカウントから顧客にメールが飛ぶ」外部
発行で、取り消せない。可逆な記録の鮮度 (骨格フィールドの未反映など) とは強度を変えるのが筋。

実データで起きていたのは「照合ゲート (``check_send_from``) は在るのに、台帳が空のまま送信
運用を始められた」状態 — ゲートが土台無しで素通りしていた。ここでは 2 つを釘付けにする:

1. **ゲート側 (停止)**: 台帳が空なら、legacy の bare pin と from が一致していても通さない。
   登録すれば通る (止めっぱなしにしない)。CLI ``sales_identity_check`` の終了コードでも同じ。
2. **検知側 (記録の時点)**: 送信自体は MCP tool 経由で Beacon が止められないので、外部への
   outbound 証跡が台帳無しで記録されたら「ゲートが呼ばれなかった」証拠として警告を出す。
   記録そのものは止めない (事実の記録を失う方が害が大きい)。
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

FROM = "sales@corp.example"


def _sales(with_ledger: bool = False):
    data = se.build_sales_project("Gate Sales", "close deals")
    se.set_send_identity(data, FROM)          # legacy な bare pin
    if with_ledger:
        se.add_send_account(data, "会社", FROM)
        se.set_send_identity(data, "会社")
    return data


# ---------------------------------------------------------------------------
# 1. ゲート側
# ---------------------------------------------------------------------------

def test_empty_ledger_blocks_even_when_the_legacy_pin_matches():
    data = _sales()
    ok, msg = se.check_send_from(data, FROM)
    assert ok is False
    assert "台帳が空" in msg
    # 直し方を必ず示す。ms-160 e-5981 で登録は CLI 動詞になったので、内部 verb 名
    # (sales_account_add) ではなく **読み手がそのまま叩ける形** を出すことを固定する
    # — 内部 verb 名を案内すると、行き詰まった瞬間の AI に CLI 境界の飛び越しを
    # 教えることになる (独立 AX レビュー A-1)。
    assert "beacon sales send-account add" in msg
    assert "commands.py" not in msg


def test_registering_the_account_lets_the_send_through():
    data = _sales(with_ledger=True)
    ok, msg = se.check_send_from(data, FROM)
    assert ok is True and "一致" in msg
    # 登録後も取り違えは止まる (hard gate が照合そのものを甘くしていない)
    ok2, msg2 = se.check_send_from(data, "personal@gmail.example")
    assert ok2 is False and "取り違え" in msg2


def test_cli_gate_exits_nonzero_on_an_empty_ledger(tmp_path, monkeypatch, capsys):
    # 送信 Skill は終了コードで gate するので、CLI の exit code まで確かめる。
    for with_ledger, expected_code in ((False, 1), (True, 0)):
        data = _sales(with_ledger=with_ledger)
        cwd = tmp_path / ("ledger" if with_ledger else "empty")
        (cwd / ".beacon").mkdir(parents=True)
        (cwd / ".beacon" / "project.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("BEACON_PROJECT_FILE",
                           str(cwd / ".beacon" / "project.json"))
        monkeypatch.setenv("BEACON_SEND_FROM", FROM)
        monkeypatch.delenv("BEACON_SEND_LABEL", raising=False)
        with pytest.raises(SystemExit) as exc:
            commands.cmd_sales_identity_check()
        assert exc.value.code == expected_code
        out = capsys.readouterr()
        assert ("BLOCK" in out.err) if expected_code else ("OK" in out.out)


# ---------------------------------------------------------------------------
# 2. 検知側 (記録の時点)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction,channel,expected", [
    ("outbound", "email", True),    # 外部へ出た送信 + 台帳空 → 警告
    ("outbound", "slack", True),
    ("inbound", "email", False),    # 受信は送信アカウントの土台を要さない
    ("outbound", "meeting", False),  # 面談の記録は「自分のアカウントからの送信」でない
    ("outbound", "phone", False),
])
def test_gap_warning_only_for_outward_sends(direction, channel, expected):
    data = _sales()
    band = se.send_ledger_gap_warning(data, direction=direction, channel=channel)
    assert bool(band) is expected
    if expected:
        # 上と同じ契約: 直し方は叩ける CLI の形で出し、内部実装の直叩きは案内しない。
        assert "台帳が空" in band
        assert "beacon sales send-account add" in band
        assert "commands.py" not in band


def test_gap_warning_is_silent_once_the_ledger_exists():
    data = _sales(with_ledger=True)
    assert se.send_ledger_gap_warning(data, direction="outbound",
                                      channel="email") == ""


def test_recording_an_outward_send_without_a_ledger_warns_but_records(
        tmp_path, monkeypatch, capsys):
    data = _sales()
    se.account_add(data, "顧客A", phase="リード")
    opp = se.opportunity_add(data, "商談X", account_id="acc-1")
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    for k in ("BEACON_COMM_SOURCE_REF", "BEACON_COMM_SOURCE_URL",
              "BEACON_COMM_BODY", "BEACON_COMM_OCCURRED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_COMM_TARGET", opp)
    monkeypatch.setenv("BEACON_COMM_SUMMARY", "初回連絡を送付")
    monkeypatch.setenv("BEACON_COMM_DIRECTION", "outbound")
    monkeypatch.setenv("BEACON_COMM_CHANNEL", "email")

    commands.cmd_communication_add()
    out = capsys.readouterr().out
    assert "Recorded communication" in out       # 記録は通す
    assert "台帳が空" in out                      # だが土台の欠落は言う
    saved = json.loads((cwd / ".beacon" / "project.json").read_text(encoding="utf-8"))
    assert len(se.communications_of(se.find_opportunity(saved, opp))) == 1
