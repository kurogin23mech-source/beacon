"""判断記録の対象は、構造的な手がかりを自由文のスクレイピングより優先する (ms-166 e-6852)。

## なぜ要るか

``beacon decision record --related-task <id>`` で **作業項目を明示して渡しても**、記録
される対象 (``related.target_id``) はその親から決まっていなかった。``--related-task`` は
``related["task_id"]`` を立てるだけで、対象の決定には一切使われていなかった。対象は
「``--related-target`` の明示 > 本文からの導出」の 2 段しかなく、本文に別の対象 id が
**1 件だけ** 出ているとそれが確実に勝つ。

実害 (ms-173 の fork が 2026-10-02 に踏んだ): ``--related-task e-6799`` (= ms-173 の
タスク) を渡したのに、本文の「ms-140」が拾われて対象が ms-140 になった。判断は ms-173 の
話なので、監査では「ms-140 についてこう判断された」と誤読される。

つまり **最も確実な構造的手がかり (明示的に渡された作業項目の親) が、最も弱い手がかり
(自由文のスクレイピング) に負けていた**。e-6603 (対象を機械決定する) の射程の穴。

## 直した順位

  1. ``--related-target`` の明示   — 人が対象そのものを指定した
  2. ``--related-task`` の親       — 人が作業項目を指定し、親は構造から一意に決まる
  3. 本文からの導出                — 手がかりが文章しか無いときの最後の手段

3 が 2 に勝たないことがこのファイルの要点。2 が解けないときだけ 3 に落ちる。

## 職種非依存

規則は ``decision_derive.resolve_target_from_work_item`` が持ち、
``occupation.iter_target_records`` を歩くので milestone / operation / 商談
(opportunity) のどれでも同じ形で解ける。作業項目がどの target class の下に居るかで
分岐しない。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import cmd_decision      # noqa: E402
import commands_shared   # noqa: E402
import decision_derive   # noqa: E402


# --- 純関数の規則 (どの target がその作業項目を持つか) -----------------------

def _dev_project():
    return {"milestones": [
        {"id": "ms-173", "entries": [{"id": "e-6799", "type": "task"}]},
        {"id": "ms-140", "entries": []},
    ]}


def _sales_project():
    # 商談の作業項目は ``activities`` arm に入る (``entries`` ではない)。arm 名は
    # occupation.profession_manifest の宣言から来るので、テストも実際の arm で書く
    # — ここを entries にすると「通るが実データでは効かない」偽のテストになる。
    return {"profession": "sales", "opportunities": [
        {"id": "opp-4", "activities": [{"id": "a-1", "activities": [{"id": "a-2"}]}]},
        {"id": "opp-9", "activities": []},
    ]}


def _sales_account_project():
    # 顧客の arm は ``nurturings``。宣言されている arm であれば同じ形で解ける。
    return {"profession": "sales", "accounts": [
        {"id": "acc-2", "nurturings": [{"id": "n-7"}]},
    ]}


def test_resolves_a_task_to_its_parent_milestone():
    assert decision_derive.resolve_target_from_work_item(
        _dev_project(), "e-6799") == "ms-173"


def test_resolves_a_nested_work_item_in_a_non_dev_target_class():
    # 職種非依存: 商談の下の入れ子の活動でも親が解ける (target class で分岐しない)。
    assert decision_derive.resolve_target_from_work_item(
        _sales_project(), "a-2") == "opp-4"


def test_resolves_a_work_item_in_another_declared_arm():
    # 商談の活動だけでなく、宣言されている他の arm (顧客のナーチャリング) でも解ける。
    assert decision_derive.resolve_target_from_work_item(
        _sales_account_project(), "n-7") == "acc-2"


def test_an_undeclared_key_is_not_walked():
    # 宣言に無い鍵は見ない (= arm 名を推測しない)。これが効いていないと、将来 arm が
    # 増えたときに宣言を通さない経路が静かに生まれる。
    data = {"profession": "dev",
            "milestones": [{"id": "ms-1", "made_up_arm": [{"id": "e-1"}]}]}
    assert decision_derive.resolve_target_from_work_item(data, "e-1") == ""


@pytest.mark.parametrize("wid", ["", "   ", "e-9999", "ms-173"])
def test_unresolvable_work_items_return_empty(wid):
    # 推測しない。空で返して呼び出し側が本文導出に落ちる (= 片方に黙って寄せない)。
    assert decision_derive.resolve_target_from_work_item(_dev_project(), wid) == ""


def test_a_non_dict_project_does_not_raise():
    for data in (None, [], "x", {}):
        assert decision_derive.resolve_target_from_work_item(data, "e-1") == ""


def test_the_rule_is_pure():
    # このモジュールの契約 (I/O なし) を保つ: data を渡される側で、読み込みはしない。
    import inspect
    src = inspect.getsource(decision_derive.resolve_target_from_work_item)
    # occupation の宣言 (manifest) を読むのは I/O ではない — 純データの引き当て。
    for forbidden in ("load_project", "open(", "_get_api_client", "requests"):  # noqa
        assert forbidden not in src, forbidden


# --- CLI の優先順位 ---------------------------------------------------------

class _Client:
    def __init__(self):
        self.written = []

    def record_decision(self, project_id, decision):
        self.written.append(decision)
        return {"decision_id": "dec-x", "kind": decision.get("kind")}


_ENV = ("BEACON_DECISION_WHAT", "BEACON_DECISION_RATIONALE", "BEACON_DECISION_EVIDENCE",
        "BEACON_DECISION_RELATED_TASK", "BEACON_DECISION_RELATED_TARGET",
        "BEACON_DECISION_KIND", "BEACON_JSON")


def _record(monkeypatch, project, **env):
    """``beacon decision record`` を 1 回走らせて ``(出力 JSON, 書かれた payload)``。"""
    client = _Client()
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "load_project", lambda *a, **k: project)
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    # 本文には **ms-140 が 1 件だけ** 出る = 本文導出が確実に成功する状況を作る。
    monkeypatch.setenv("BEACON_DECISION_WHAT", "ms-140 の件で方針を決めた")
    monkeypatch.setenv("BEACON_DECISION_RATIONALE", "理由")
    monkeypatch.setenv("BEACON_DECISION_EVIDENCE", "commit:abc1234")
    monkeypatch.setenv("BEACON_JSON", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_decision.cmd_decision_record()
    return json.loads(buf.getvalue()), (client.written[-1] if client.written else {})


def test_the_regression_ms173_fork_hit(monkeypatch):
    """実害の回帰ピン: --related-task e-6799 + 本文に ms-140 → 対象は ms-173。

    旧実装ではここが ms-140 になり、ms-173 の判断が別マイルストーンの判断として
    記録された。
    """
    out, payload = _record(monkeypatch, _dev_project(),
                           BEACON_DECISION_RELATED_TASK="e-6799")
    assert out["target_id"] == "ms-173", out
    assert out["target_id_source"] == "related-task-parent", out
    assert (payload.get("related") or {}).get("target_id") == "ms-173", payload
    # 作業項目そのものも従来どおり運ばれる (対象の決定に使うようになっただけ)。
    assert (payload.get("related") or {}).get("task_id") == "e-6799", payload


def test_explicit_target_still_wins_over_the_task_parent(monkeypatch):
    # 人が対象そのものを指定したなら、それが最優先 (構造の推論より人の明示)。
    out, _ = _record(monkeypatch, _dev_project(),
                     BEACON_DECISION_RELATED_TASK="e-6799",
                     BEACON_DECISION_RELATED_TARGET="ms-140")
    assert out["target_id"] == "ms-140", out
    assert out["target_id_source"] == "explicit", out


def test_text_is_used_only_when_the_task_parent_cannot_be_resolved(monkeypatch):
    out, _ = _record(monkeypatch, _dev_project(),
                     BEACON_DECISION_RELATED_TASK="e-9999")
    assert out["target_id"] == "ms-140", out
    assert out["target_id_source"] == "text", out


def test_without_a_related_task_the_old_behaviour_is_unchanged(monkeypatch):
    out, _ = _record(monkeypatch, _dev_project())
    assert out["target_id"] == "ms-140", out
    assert out["target_id_source"] == "text", out


def test_the_task_parent_wins_for_a_non_dev_target_class(monkeypatch):
    # 商談でも同じ順位が効く (職種で分岐しない)。
    out, _ = _record(monkeypatch, _sales_project(),
                     BEACON_DECISION_RELATED_TASK="a-2")
    assert out["target_id"] == "opp-4", out
    assert out["target_id_source"] == "related-task-parent", out


def test_a_project_read_failure_is_disclosed_not_swallowed(monkeypatch):
    # 解けなかったことを黙って本文導出に落とすと「なぜ本文が勝ったか」が読み手に
    # 見えない (e-6757 と同じ方針)。理由を開示したうえで本文に落ちる。
    def _boom(*a, **k):
        raise RuntimeError("store unavailable")
    out, _ = _record(monkeypatch, _dev_project(),
                     BEACON_DECISION_RELATED_TASK="e-6799")
    # 正常系では開示フィールドは出ない (既定の出力を太らせない)。
    assert "related_task_lookup_error" not in out, out

    monkeypatch.setattr(commands_shared, "load_project", _boom)
    client = _Client()
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    monkeypatch.setenv("BEACON_DECISION_RELATED_TASK", "e-6799")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_decision.cmd_decision_record()
    out2 = json.loads(buf.getvalue())
    assert out2["target_id_source"] == "text", out2
    assert "store unavailable" in (out2.get("related_task_lookup_error") or ""), out2


def test_no_project_read_when_the_target_is_already_explicit(monkeypatch):
    # 結果に影響しない I/O をしない。明示指定で勝ちが決まっているなら読まない。
    def _must_not_be_called(*a, **k):
        pytest.fail("--related-target が明示されているのにプロジェクトを読みました")
    monkeypatch.setattr(commands_shared, "load_project", _must_not_be_called)
    client = _Client()
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_DECISION_WHAT", "ms-140 の件")
    monkeypatch.setenv("BEACON_DECISION_EVIDENCE", "commit:abc1234")
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DECISION_RELATED_TASK", "e-6799")
    monkeypatch.setenv("BEACON_DECISION_RELATED_TARGET", "ms-140")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_decision.cmd_decision_record()
    assert json.loads(buf.getvalue())["target_id"] == "ms-140"


def test_human_readable_output_names_the_same_source(monkeypatch):
    # text と --json で同じ情報を出す (e-6603 独立レビュー AX-2 の方針を引き継ぐ)。
    client = _Client()
    monkeypatch.setattr(cmd_decision, "_is_cloud_mode", lambda: True)
    monkeypatch.setattr(cmd_decision, "_get_api_client",
                        lambda: (client, {"project_id": "p1"}))
    monkeypatch.setattr(commands_shared, "load_project", lambda *a, **k: _dev_project())
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_DECISION_WHAT", "ms-140 の件で方針を決めた")
    monkeypatch.setenv("BEACON_DECISION_EVIDENCE", "commit:abc1234")
    monkeypatch.setenv("BEACON_DECISION_RELATED_TASK", "e-6799")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_decision.cmd_decision_record()
    text = buf.getvalue()
    assert "作業項目の親から解決" in text, text
    assert "ms-173" in text, text


# --- test-the-test: 順位を入れ替えると赤くなる ------------------------------

def test_swapping_the_priority_order_breaks_the_regression_pin():
    """本文導出が作業項目の親に勝つ形に戻したら、上の回帰ピンが落ちることを示す。

    実コードを書き換えずに順位だけを合成して確かめる (= guard が順位に依っていて、
    どちらでも緑になる形ではないこと)。
    """
    explicit, task_parent, derived = "", "ms-173", "ms-140"
    fixed = explicit or task_parent or derived
    swapped = explicit or derived or task_parent
    assert fixed == "ms-173"
    assert swapped == "ms-140", "入れ替えても同じ答えになるなら順位を検査できていない"
    assert fixed != swapped
