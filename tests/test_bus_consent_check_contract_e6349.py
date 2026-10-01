"""`beacon bus consent-check` の出力契約の試験 (ms-160 e-6349, 独立レビュー A-2/A-4/M-2).

なぜ規則の単体試験だけでは足りないか — この verb を足した最初の版で、まさにこの PR が
閉じようとしている「手順書と実装の食い違い」を私自身が作っていた。手順書は
「`--json` で問い合わせ、返ってきた但し書きを draft に転記せよ」と書いているのに、
実装は但し書きを人間可読モードでしか出しておらず、**手順書どおり動く呼び手には
永久に届かなかった** (A-2)。

規則側 (`classify_send_consent`) の試験は緑のままだった。壊れていたのは規則ではなく
**規則と読み手のあいだの薄いグルー層**で、そこを誰も実行していなかった (M-2)。

そこでこれらは verb 本体を実行し、手順書が読者に約束している field が実在することを
固定する。`commands.py` は差し替えず、`cmd_bus_consent_check` を env とキャプチャで
直接呼ぶ (外部送信は一切起きない — この verb は読み取り専用)。
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import cmd_bus  # noqa: E402
import dm_consent  # noqa: E402

# 手順書 (skills/beacon-dm-send.md Step 3.1) が読者に説明している field。
# ここを変えるなら手順書も変える、が守られているかを固定する。
PROMISED_JSON_FIELDS = {
    "recipient_confirmation_required",
    "reason",
    "explanation",
}
# 但し書き系 — A-2 / A-4 で「JSON からも落とすな」と決めた field。
CAVEAT_JSON_FIELDS = {
    "identity_uncertain",
    "channel_recognized",
    "carve_outs_not_checked",
    "caveats",
}


def _run(monkeypatch, *, sender="me@example.com", recipient="you@example.com",
         env=None):
    """verb を直接叩き、(exit_code, stdout) を返す。身元解決はスタブする。"""
    monkeypatch.setattr(cmd_bus, "_resolve_creator_identity",
                        lambda: ("uid", sender, "sid"), raising=False)
    monkeypatch.setattr(cmd_bus, "_resolve_recipient_live",
                        lambda r, c, advise=None: (r, None, recipient),
                        raising=False)
    for k in ("BEACON_BUS_RECIPIENT", "BEACON_BUS_CHANNEL",
              "BEACON_BUS_IN_REPLY_TO", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            cmd_bus.cmd_bus_consent_check()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def _json_run(monkeypatch, **kw):
    env = dict(kw.pop("env", {}))
    env.setdefault("BEACON_BUS_RECIPIENT", "sv-target")
    env["BEACON_JSON"] = "1"
    code, out = _run(monkeypatch, env=env, **kw)
    assert code == 0, out
    return json.loads(out.strip().splitlines()[-1])


def test_json_carries_every_field_the_skill_promises(monkeypatch):
    got = _json_run(monkeypatch)
    missing = PROMISED_JSON_FIELDS - set(got)
    assert not missing, f"手順書が説明している field が出力に無い: {sorted(missing)}"


def test_json_carries_the_caveats_too(monkeypatch):
    """但し書きは JSON にも出す (A-2: 人間可読モードだけに出していた退行の固定)。"""
    got = _json_run(monkeypatch)
    missing = CAVEAT_JSON_FIELDS - set(got)
    assert not missing, f"但し書き系 field が JSON から落ちている: {sorted(missing)}"


def test_cross_user_new_send_requires_confirmation(monkeypatch):
    got = _json_run(monkeypatch)
    assert got["recipient_confirmation_required"] is True
    assert got["reason"] == dm_consent.CONSENT_REQUIRED_CROSS_USER
    assert got["explanation"]


def test_same_user_does_not(monkeypatch):
    got = _json_run(monkeypatch, sender="me@example.com", recipient="me@example.com")
    assert got["recipient_confirmation_required"] is False
    assert got["reason"] == dm_consent.CONSENT_SKIP_SAME_USER


def test_reply_lane_does_not(monkeypatch):
    got = _json_run(monkeypatch, env={"BEACON_BUS_IN_REPLY_TO": "evt-1"})
    assert got["recipient_confirmation_required"] is False
    assert got["reason"] == dm_consent.CONSENT_SKIP_REPLY
    assert got["is_reply"] is True


def test_unresolved_identity_is_flagged_in_json(monkeypatch):
    """身元が解けないとき、JSON だけ見ている呼び手にもそれが伝わること (A-2)。"""
    got = _json_run(monkeypatch, recipient="")
    assert got["identity_uncertain"] is True
    assert got["caveats"], "但し書きが空"
    assert any("身元" in c for c in got["caveats"])


def test_a_misspelled_channel_is_flagged_instead_of_silently_answering(monkeypatch):
    """`--channel DM` は規則上 'dm 以外' に落ちる。黙って「不要」と答えない (A-1)。

    規則は完全一致で見るので綴り違いは構造的に別 channel になる。規則側は変えず
    (変えると送信経路の意味が動く)、問い合わせ面が「見覚えが無い」と言う。
    """
    got = _json_run(monkeypatch, env={"BEACON_BUS_CHANNEL": "DM"})
    assert got["recipient_confirmation_required"] is False   # 規則どおり
    assert got["channel_recognized"] is False                # だが黙らない
    assert any("見覚え" in c for c in got["caveats"])


@pytest.mark.parametrize("channel", ["dm", "trek-trigger", "operation-trigger"])
def test_known_channels_are_not_flagged(monkeypatch, channel):
    got = _json_run(monkeypatch, env={"BEACON_BUS_CHANNEL": channel})
    assert got["channel_recognized"] is True
    assert not any("見覚え" in c for c in got["caveats"])


def test_unchecked_carve_outs_are_disclosed(monkeypatch):
    """判定に使わなかった 2 軸を出力で明かす (A-4)。

    安全側 (過剰確認) に倒すこと自体は正しいが、倒したと黙っていると、Trek /
    Operation 文脈で「必要」と出た呼び手が理由を誤診して無関係な所をデバッグする。
    """
    got = _json_run(monkeypatch)
    assert set(got["carve_outs_not_checked"]) == {"operation_envelope", "shared_trek"}


def test_missing_recipient_is_a_usage_error(monkeypatch):
    code, _ = _run(monkeypatch, env={"BEACON_JSON": "1"})
    assert code == 1


def test_the_answer_never_rides_on_the_exit_code(monkeypatch):
    """required=True でも exit 0。`-check` を `test` 慣習で分岐させない契約 (A-3)。

    exit code に答えを載せると `if beacon bus consent-check ...; then` が常に
    「不要」側へ倒れる。答えは JSON から読む、を固定する。
    """
    env = {"BEACON_BUS_RECIPIENT": "sv-target", "BEACON_JSON": "1"}
    code, out = _run(monkeypatch, env=env)
    assert code == 0
    assert json.loads(out.strip().splitlines()[-1])["recipient_confirmation_required"] is True


def test_human_readable_mode_shows_the_same_caveats(monkeypatch):
    """人間可読モードでも但し書きが落ちないこと (JSON だけ直して片肺にしない)。"""
    code, out = _run(monkeypatch, recipient="",
                     env={"BEACON_BUS_RECIPIENT": "sv-target",
                          "BEACON_BUS_CHANNEL": "DM"})
    assert code == 0
    assert "身元" in out
    assert "見覚え" in out
