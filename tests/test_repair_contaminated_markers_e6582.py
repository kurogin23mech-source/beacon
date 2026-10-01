"""ms-173 / e-6582 — 残留した「確認待ち」宣言を一度だけ洗う。

運用室で橙のまま何日も消えない行の正体は、producer 修正 (62766932) より前の beacon が
書いた汚染マーカー (`declared_state=awaiting_human` なのに `state_detail` がアイドル通知の
文言)。宣言を書き換えられるのはセッション自身だけなので、二度と hook が発火しない放置
セッションでは永久に残る (2026-09-19 実測 3 行)。

**なぜ恒久的な実行時ルールでなく修復なのか** (e-6717 をこの判断で閉じた): 汚染を作る
経路は producer 側で閉じており汚染はもう増えない有限の集合。増えない集合のために
「読む側で降格する」恒久ルールを足すと、修正前 beacon では汚染と『本物の待ちが上書き
された行』が完全同形になるため、本物の確認待ちを隠す恐れを永久に抱え込む。

固定する契約:

  * 汚染の判定は producer と同じ 1 箇所 (lib/session_state_hook) から引く。別実装を
    持つと drift する。
  * ``state_detail`` を持たない ``awaiting_human`` は修復しない (message 無しの分類
    不能な Notification を awaiting_human に倒す fail-safe を壊さない)。
  * ``declared_at`` / ``state_since`` は書き換えない。now にすると死んだセッションが
    「たった今宣言した」ように見え、derive_state の not-live 猶予窓をすり抜けて状態が
    復活する。修復は「何を宣言していたか」を正すだけで「いつ」を偽らない。
  * 既定は dry-run。書くのは ``--apply`` のときだけで、書く前に必ず退避を作る。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import bus_liveness
import session_state_hook

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "repair-contaminated-state-markers.py"

IDLE_MSG = "Claude is waiting for your input"
PERM_MSG = "Claude needs your permission to use Bash"


def _contaminated():
    return {
        "declared_state": "awaiting_human",
        "declared_at": "2026-09-10T04:51:05.006Z",
        "state_since": "2026-09-10T04:50:05.006Z",
        "source_event": "Notification",
        "state_detail": IDLE_MSG,
    }


# --- 純粋な判定 / 変換 --------------------------------------------------------

class TestDetectAndRepair:
    def test_contaminated_is_detected(self):
        assert session_state_hook.is_contaminated_awaiting_human(_contaminated())

    def test_genuine_wait_is_not_contaminated(self):
        m = dict(_contaminated(), state_detail=PERM_MSG)
        assert not session_state_hook.is_contaminated_awaiting_human(m)
        assert session_state_hook.repair_contaminated_marker(m) is None

    def test_detail_less_awaiting_human_is_left_alone(self):
        """detail の不在を汚染の証拠にしない (fail-safe の向きを壊さない)。"""
        m = _contaminated()
        del m["state_detail"]
        assert not session_state_hook.is_contaminated_awaiting_human(m)
        assert session_state_hook.repair_contaminated_marker(m) is None

    def test_other_states_untouched(self):
        for state in ("running", "idle", "blocked", "terminated"):
            m = dict(_contaminated(), declared_state=state)
            assert session_state_hook.repair_contaminated_marker(m) is None

    def test_repair_demotes_to_idle_and_drops_detail(self):
        out = session_state_hook.repair_contaminated_marker(_contaminated())
        assert out["declared_state"] == bus_liveness.STATE_IDLE
        assert "state_detail" not in out

    def test_repair_preserves_the_timestamps(self):
        """『いつ宣言したか』を偽らない。now にすると死んだセッションが
        derive_state の not-live 猶予窓をすり抜けて状態が復活する。"""
        src = _contaminated()
        out = session_state_hook.repair_contaminated_marker(src)
        assert out["declared_at"] == src["declared_at"]
        assert out["state_since"] == src["state_since"]

    def test_repair_leaves_a_trace(self):
        out = session_state_hook.repair_contaminated_marker(_contaminated())
        assert "repair" in out["source_event"].lower()

    def test_repair_does_not_mutate_the_input(self):
        src = _contaminated()
        session_state_hook.repair_contaminated_marker(src)
        assert src["declared_state"] == "awaiting_human"
        assert src["state_detail"] == IDLE_MSG

    @pytest.mark.parametrize("junk", [None, "", 0, [], "awaiting_human"])
    def test_non_dict_is_safe(self, junk):
        assert session_state_hook.is_contaminated_awaiting_human(junk) is False
        assert session_state_hook.repair_contaminated_marker(junk) is None


# --- スクリプト経路 (探索と書き込み) -----------------------------------------

def _seed(root: Path, marker: dict) -> Path:
    (root / ".beacon").mkdir(parents=True, exist_ok=True)
    p = root / ".beacon" / "session-state.json"
    p.write_text(json.dumps(marker), encoding="utf-8")
    return p


def _run(root: Path, *extra):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json", *extra],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestScript:
    def test_script_exists(self):
        assert SCRIPT.is_file()

    def test_dry_run_reports_but_does_not_write(self, tmp_path):
        p = _seed(tmp_path, _contaminated())
        out = _run(tmp_path)
        assert out["applied"] is False
        assert [r["status"] for r in out["results"]] == ["contaminated"]
        # ファイルは手付かず
        assert json.loads(p.read_text(encoding="utf-8"))["declared_state"] \
            == "awaiting_human"

    def test_apply_repairs_and_keeps_a_backup(self, tmp_path):
        p = _seed(tmp_path, _contaminated())
        out = _run(tmp_path, "--apply")
        row = out["results"][0]
        assert row["status"] == "repaired"
        after = json.loads(p.read_text(encoding="utf-8"))
        assert after["declared_state"] == "idle"
        assert "state_detail" not in after
        # 退避が残っており、元の内容が戻せる (削除は一切しない)
        backup = Path(row["backup"])
        assert backup.is_file()
        assert json.loads(backup.read_text(encoding="utf-8"))["state_detail"] == IDLE_MSG

    def test_apply_is_idempotent(self, tmp_path):
        _seed(tmp_path, _contaminated())
        _run(tmp_path, "--apply")
        out = _run(tmp_path, "--apply")
        assert [r["status"] for r in out["results"]] == ["clean"]

    def test_genuine_wait_is_not_touched_by_the_script(self, tmp_path):
        p = _seed(tmp_path, dict(_contaminated(), state_detail=PERM_MSG))
        out = _run(tmp_path, "--apply")
        assert [r["status"] for r in out["results"]] == ["clean"]
        assert json.loads(p.read_text(encoding="utf-8"))["state_detail"] == PERM_MSG

    def test_unreadable_marker_is_reported_not_crashed(self, tmp_path):
        (tmp_path / ".beacon").mkdir(parents=True)
        (tmp_path / ".beacon" / "session-state.json").write_text("{nope", encoding="utf-8")
        out = _run(tmp_path, "--apply")
        assert [r["status"] for r in out["results"]] == ["unreadable"]

    def test_missing_marker_is_not_an_error(self, tmp_path):
        out = _run(tmp_path)
        assert out["results"] == []

    def test_script_reuses_the_producer_predicate(self):
        """判定を script 側に書き写していないこと — 書き写すと producer と drift する。"""
        src = SCRIPT.read_text(encoding="utf-8")
        assert "session_state_hook.repair_contaminated_marker(" in src
        assert "waiting for your input" not in src.split('"""', 2)[-1], (
            "アイドル文言を script 側に再定義している (判定が 2 箇所になる)")
