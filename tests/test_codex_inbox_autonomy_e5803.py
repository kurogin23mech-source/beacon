"""Codex inbox hook: operation-trigger imperative + auto-execute downgrade parity
with the Claude inbox hook (ms-160 e-5803).

Before this, the Codex inbox hook rendered every non-stop event as a generic DM:
it never surfaced the "run /beacon-operation-execute autonomously" imperative for
opted-in operation-trigger events, and never downgraded an auto-execute event on
a channel the project had not opted into. Both hooks now share
lib/bus_delivery.classify_auto_execute + format_operation_trigger_imperative.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "codex-inbox-hook.py"

CODEX_SID = "codex-1787000000000-abcdef01"
_T1_ENVELOPE = {"tier": "T1-system", "issuer": "beacon-system"}

# ms-185 e-7078: operation-trigger's proof is the T2 envelope the human
# approved for that op_id — not the server-minted T1-system one (Trek is its
# only issuer). Pinning the T1-system shape here asserted a shape production
# cannot produce, which is why these stayed green while every real operation
# trigger was downgraded.
# scope names op-9 because that is the op_id _op_trigger_event fires.
_OP_ENVELOPE = {"tier": "T2", "issuer": "someone@example.com",
                "scope": "op-9"}


def _setup(tmp_path: Path, *, auto_execute_channels) -> Path:
    cwd = tmp_path / "proj"
    beacon = cwd / ".beacon"
    (beacon / "codex" / "inbox").mkdir(parents=True)
    (beacon / "cloud.json").write_text(json.dumps({"project_id": "proj-1"}))
    (beacon / "project.json").write_text(json.dumps(
        {"bus_auto_execute_channels": auto_execute_channels}))
    (beacon / "codex" / "receive-loop.session.json").write_text(
        json.dumps({"session_id": CODEX_SID, "project_id": "proj-1"}))
    return cwd


def _seed(cwd: Path, event: dict) -> None:
    (cwd / ".beacon" / "codex" / "inbox" / f"{event['event_id']}.json").write_text(
        json.dumps(event))


def _run(cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--cwd", str(cwd),
         "--no-archive", "--install-root", str(REPO)],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout or "{}")
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def _op_trigger_event(*, envelope=None) -> dict:
    e = {
        "event_id": "1787000010000-OPtrig",
        "channel": "operation-trigger",
        "delivery": "auto-execute",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:00.000000Z",
        "payload": {"recipient_session_id": CODEX_SID,
                    "op_id": "op-9", "trigger_name": "daily-health"},
    }
    if envelope is not None:
        e["envelope"] = envelope
    return e


def _trek_trigger_event(*, envelope=None) -> dict:
    """``trek-trigger`` の 1 件 (= 引退した channel の実例)。

    **ms-185 で扱いが変わった**: 以前はサーバ署名 (T1-system) を要求する channel
    だったが、署名を要求する集合 (``GATED_PROVENANCE_CHANNELS``) から外れ、
    ``RETIRED_PROVENANCE_CHANNELS`` (= サーバがもう発行しない) に移った。だから
    回復手段は「operator には直せない」ではなく、**許可リストから外す**
    (``beacon bus auto-execute remove``) であり、operator に手段が在る。

    許可リストに **無い** 状態では、引退を知らせる案内になる (e-7395)。許可リストに
    **残っている** 状態では「外してください」になる。混在時に片方が誤案内に
    ならないかを測るのに使う。
    """
    e = {
        "event_id": "1787000020000-TREKtrig",
        "channel": "trek-trigger",
        "delivery": "auto-execute",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:05:00.000000Z",
        "payload": {"recipient_session_id": CODEX_SID, "trek_id": "trek-1"},
    }
    if envelope is not None:
        e["envelope"] = envelope
    return e


def test_opted_in_operation_trigger_emits_imperative(tmp_path):
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, _op_trigger_event(envelope=_OP_ENVELOPE))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" in ctx
    assert "/beacon-operation-execute op-9" in ctx
    assert "安全側降格" not in ctx  # kept, not downgraded


def test_non_opted_in_auto_execute_is_downgraded_with_allowlist_reason(tmp_path):
    # bus_auto_execute_channels is empty → allowlist-miss downgrade; no imperative,
    # and the notice must name the allowlist / opt-in cause (not provenance).
    cwd = _setup(tmp_path, auto_execute_channels=[])
    _seed(cwd, _op_trigger_event(envelope=_T1_ENVELOPE))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" in ctx
    assert "opt-in 前" in ctx
    assert "T1-system" not in ctx  # e-5803 review AX-1: not a provenance failure


def test_retired_channel_without_optin_is_not_told_to_opt_in(tmp_path):
    """受入条件1 (Codex 側, ms-185 e-7395): 引退 + 許可リスト空 の案内。

    以前はここが「channel が allowlist に無い (= 人間 opt-in 前)」の件数バンドに
    畳まれていた。人間がそれに従って ``auto-execute add`` で登録すると、次の通知で
    「外してください」と逆を言われる —— 1 往復の手間と、二度と使わない channel を
    許可させる誤誘導。
    """
    cwd = _setup(tmp_path, auto_execute_channels=[])
    _seed(cwd, _trek_trigger_event())
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" in ctx
    assert "引退した channel" in ctx, (
        "引退である旨を案内していません: " + ctx
    )
    assert "opt-in 前" not in ctx, (
        "引退した channel を「人間 opt-in 前」と案内しています = 登録させる "
        "誤誘導です: " + ctx
    )


def test_retired_channel_with_stale_optin_still_says_remove(tmp_path):
    """受入条件2 (Codex 側): 引退 + 許可リストに在る → 従来どおり「外す」。"""
    cwd = _setup(tmp_path, auto_execute_channels=["trek-trigger"])
    _seed(cwd, _trek_trigger_event())
    ctx = _run(cwd)
    assert "auto-execute remove" in ctx, (
        "許可リストに残った引退 channel の直し方 (外す) が出ていません: " + ctx
    )


def test_live_channel_without_optin_still_says_opt_in(tmp_path):
    """受入条件3 (Codex 側): 現役 + 許可リスト空 → 従来どおり「opt-in 前」。

    引退側を分けたことで、現役側の案内が巻き込まれて変わっていないことを見る。
    """
    cwd = _setup(tmp_path, auto_execute_channels=[])
    _seed(cwd, _op_trigger_event(envelope=_T1_ENVELOPE))
    ctx = _run(cwd)
    assert "opt-in 前" in ctx, ctx
    assert "引退した channel" not in ctx, (
        "現役 channel を引退と案内しています: " + ctx
    )


def test_opted_in_but_no_approved_envelope_names_the_operation_recovery(tmp_path):
    """許可リスト済 + 承認の封筒が無い → 降格し、**その channel の**直し方を出す。

    ms-185 / #812 の独立レビュー 2 体が一致で指摘した欠陥の回帰テスト。

    この試験は以前 ``assert "T1-system" in ctx`` を置いていた。#812 が
    ``operation-trigger`` の証拠を「サーバ署名 (T1-system)」から「人間がその
    op_id を承認した封筒 (T2)」へ張り替えた後は、**本番に存在しない語彙**を
    期待する偽の緑になっていた。しかも実装の文言も trek の世界観のままで、
    ``beacon operation approve <op-id>`` という**実在する回復手段を隠して
    「直りません」と断定**していた。

    なので測るべきは「T1-system と書いてあるか」ではなく:
      1. 回復手段 (``beacon operation approve``) を名指しているか
      2. operator には直せないという趣旨の trek 側の文言を出していないか
      3. 許可リスト漏れ (= 別の理由) と混同していないか
    """
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, _op_trigger_event(envelope=None))
    ctx = _run(cwd)
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" in ctx
    assert "beacon operation approve" in ctx, (
        "operation-trigger の降格で、実在する回復手段 "
        "`beacon operation approve <op-id>` を案内していません。"
    )
    assert "T1-system" not in ctx, (
        "operation-trigger が要求するのはサーバ署名 (T1-system) ではなく "
        "人間が承認した封筒です。T1-system を持ち出すのは #812 以前の語彙。"
    )
    assert "直りません" not in ctx, (
        "回復手段が実在するのに「直りません」と断定しています "
        "(= 真因と逆の案内、e-7077 と同型の退行)。"
    )
    assert "opt-in 前" not in ctx
    # channel を名指していること (= どの channel の話か読み手が分かる)
    assert "channel=operation-trigger" in ctx


def test_unprocessed_stop_surfaces_a_degraded_warning(tmp_path):
    # e-5803 review (AX-1/AX-2): a STOP that can't be turned into a halt-request
    # (here: no receive-loop.session.json → session_id unresolved) must NOT vanish
    # silently on the kill-switch path — it must surface a fail-loud warning.
    cwd = _setup(tmp_path, auto_execute_channels=[])
    (cwd / ".beacon" / "codex" / "receive-loop.session.json").unlink()  # session_id ""
    _seed(cwd, {
        "event_id": "1787000099000-STOPx",
        "channel": "stop-signal",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:09.000000Z",
        "payload": {"kind": "stop", "scope": "global",
                    "issued_by_session_id": "other-sid", "reason": "halt"},
    })
    ctx = _run(cwd)
    assert "STOP signal 受信" in ctx
    assert "停止処理に失敗" in ctx
    # and the stop is NOT rendered as a normal DM
    assert "1787000099000-STOPx" not in ctx


def test_plain_dm_has_no_imperative_or_downgrade(tmp_path):
    cwd = _setup(tmp_path, auto_execute_channels=["operation-trigger"])
    _seed(cwd, {
        "event_id": "1787000011000-DM",
        "channel": "dm",
        "sender_session_id": "other-sid",
        "created_at": "2026-08-29T07:00:01.000000Z",
        "payload": {"recipient_session_id": CODEX_SID, "text": "hello"},
    })
    ctx = _run(cwd)
    assert "hello" in ctx
    assert "AUTONOMOUS ACTION" not in ctx
    assert "安全側降格" not in ctx


def test_mixed_provenance_batch_names_each_channels_own_recovery(tmp_path):
    """降格した 2 つの channel に、**それぞれの**直し方を出す。

    ms-185 / #812 レビュー。Codex 側の通知は理由ごとの **件数** で束ねる形なので、
    以前は「署名欠落 N 件」に対して 1 つの固定文を出すだけだった。回復手段が違う
    channel が同じ受信に混ざると、**片方は必ず誤案内**になる (回復手段が在る側に
    「直りません」と言う)。この状況は以前の実装では表現そのものが不可能だった =
    回帰テストが置けない形だったので、channel を集めて 1 行ずつ案内する形に変えた。

    ms-185 e-7071 で測り方を実態に合わせた。trek-* が引退済みの集合へ移った
    (発行者が e-7070 で消えたため) ので、この 2 件は**同じ理由タグでは無くなった**
    — operation-trigger は署名欠落、trek-trigger は引退済み。理由が違えば塊も
    別々になるので、以前の「2 件を 1 つの塊で数える」は成立しない。

    **守る契約は変わっていない**: 2 つの channel が同じ受信に混ざったとき、
    それぞれが名指しされ、それぞれの回復手段が出ること (= 片方の文言がもう片方に
    適用されないこと)。理由が分かれた今はむしろ誤案内の危険が高い (「古い opt-in を
    外せ」と「承認の封筒を付けろ」は operator の取る行動が正反対) ので、
    1 行ずつ出ていることをここで固定し続ける。
    """
    cwd = _setup(tmp_path,
                 auto_execute_channels=["operation-trigger", "trek-trigger"])
    _seed(cwd, _op_trigger_event(envelope=None))          # 承認の封筒が無い
    _seed(cwd, _trek_trigger_event(envelope=None))        # 引退済み channel
    ctx = _run(cwd)

    # 理由が違うので塊も 2 つに分かれる (= 1 件 + 1 件)。
    blocks = [b for b in ctx.split("⚠ 安全側降格:") if b.strip()][1:] \
        if ctx.count("⚠ 安全側降格:") == 0 else \
        ["⚠ 安全側降格:" + b for b in ctx.split("⚠ 安全側降格:")[1:]]
    assert len(blocks) == 2, (
        f"降格の塊が 2 つありません (理由ごとに分けていない): {len(blocks)}"
    )
    op_block = [b for b in blocks if "operation-trigger" in b]
    retired_block = [b for b in blocks if "auto-execute remove" in b]
    assert len(op_block) == 1, f"署名欠落の塊が 1 つではない: {blocks}"
    assert len(retired_block) == 1, f"引退済みの塊が 1 つではない: {blocks}"

    # それぞれの塊が、その channel の回復手段だけを出している
    # (= 片方の文言がもう片方に適用されていない)。行動が正反対なので、
    # 混ざると operator は必ず間違う。
    assert "beacon operation approve" in op_block[0], (
        "署名欠落の塊に回復手段 (承認の封筒を付ける) が出ていません。"
    )
    assert "auto-execute remove" not in op_block[0], (
        "署名欠落の塊に「古い opt-in を外せ」と案内しています = 誤案内。"
        "この channel は現役なので、opt-in を外すのは直し方ではない。"
    )
    assert "operation approve" not in retired_block[0], (
        "引退済みの塊に「承認すれば直る」と案内しています = 誤案内。"
        "引退は operator の承認では直らない (発行者そのものが居ない)。"
    )
    # 署名欠落の側は channel を名指しする行で出る (回復手段が channel ごとに
    # 違うため)。引退済みの側は全 channel で同じ助言なので固定文で足りる。
    op_lines = [ln for ln in op_block[0].splitlines()
                if ln.strip().startswith("- channel=")]
    assert len(op_lines) == 1 and "operation-trigger" in op_lines[0], (
        f"署名欠落の案内が channel を名指ししていません: {op_lines}"
    )
