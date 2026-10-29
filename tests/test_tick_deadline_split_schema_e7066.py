"""締切リマインドが「行分割されたスキーマ」でも発火することを測る (ms-185 e-7066)。

**なぜこのファイルが要るか**: 本番 (MySQL / schema_version=3) では project 文書は
行分割されており、``projects`` 行は meta だけ、``milestones`` / ``entries`` は別の行に
ある。ところが定期発火の締切リマインドは ``db.get_project`` ——
``store_router`` 自身が「Projects (v1 whole-doc)」と書いている v1 読み取り —— を
呼んでいた。そのため v3 の project では ``milestones`` が 1 件も見えず、
**締切リマインドは構造的に発火不能**だった。本番の Beacon プロジェクトがまさに v3 で、
締切を過ぎた Target を置いても発火が 0 のまま、tick の応答だけは緑だった。

既存の ``test_scheduled_source_registry_e7066.py`` はこれを捕まえられない。あちらの
足場は ``get_project`` が **milestones 入りの完全な dict を返す** (= v1 形) ので、
本番に存在しない形を測っていた。足場が本番と違う形をしていると、振る舞いを測って
いても緑は嘘になる。

測るのは 5 点:
  1. 行分割された store でも締切リマインドが発火する (= 退行したら赤)。
  2. 発火は 1 回きり (dedup 印が分割された行に刻まれて、毎分 DM が飛ばない)。
  3. narrow read が本当に narrow (締切を持たない子行を読んでいない)。
     = 丸ごと hydrate に戻したら赤。毎分 13.7 MB を読む形は 2026-08-20 の本番
     停止 (e-5367) と同じ読み方なので、安いことも固定する。
  4. 読み手と書き手は**組でしか選ばれない**。片方だけだと部分 hydrate した doc を
     whole-doc save して、読まなかった行を消す。
  5. 分割スキーマを v1 読み取りで読んでしまった時、黙って 0 件にせず error を出す。
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
os.environ.setdefault("BEACON_SCHEDULER_INTERNAL_KEY", "test-scheduler-key")

import app as app_module  # noqa: E402
import deadline as deadline_mod  # noqa: E402
import mysql_client as mc  # noqa: E402

PID = "p-split-e7066"
CLAIMER = "sv-claimer-split"
PAST = "2000-01-01"


# ---------------------------------------------------------------------------
# 行分割された store の最小模型 (= MySQL v3 のレイアウト)
#
# projects 行 = meta のみ / milestones 行 = Target / entries 行 = work item。
# mysql_client._conn を差し替えて実コードを端から端まで走らせるので、これは
# 「読み手が分割を理解しているか」を本当に測る (stub を測っているのではない)。
# ---------------------------------------------------------------------------

def _rows() -> dict:
    meta = {"project_id": PID, "name": "split", "schema_version": 3,
            "operations": []}
    ms = {"id": "ms-1", "title": "期日を過ぎたターゲット", "status": "in_progress",
          "target_date": PAST, "occupation": {"session_id": CLAIMER}}
    # 締切を持たない子行。narrow read はこれを読んではいけない (本番では 4,502 行)。
    plain = {"id": "e-1", "type": "task", "status": "todo",
             "description": "締切なし", "bulk": "x" * 64}
    # 締切を持つ子行。こちらは読まれ、発火する。
    dated = {"id": "e-2", "type": "task", "status": "todo",
             "description": "締切あり", "deadline": PAST}
    return {
        ("beacon_projects", PID, ""): json.dumps(meta),
        ("beacon_milestones", PID, "ms-1"): json.dumps(ms),
        ("beacon_entries", PID, "ms-1#e-1"): json.dumps(plain),
        ("beacon_entries", PID, "ms-1#e-2"): json.dumps(dated),
    }


class _FakeCursor:
    """mysql_client が出す SQL のうち、この経路で使う形だけを解釈する。

    ``JSON_CONTAINS_PATH`` を本当に効かせるのが肝 (= 素通りさせると 3 番目の
    「narrow であること」の試験が意味を失い、偽の緑になる)。
    """

    def __init__(self, store, reads):
        self.store = store
        self.reads = reads          # 読んだ (table, sk) の記録
        self._result = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SHOW TABLES LIKE"):
            like = params[0] if params else ""
            self._result = ([{"t": like}]
                            if any(k[0] == like for k in self.store) else [])
            return len(self._result)
        table = re.search(r"`([^`]+)`", s).group(1)
        if s.startswith("SELECT data FROM"):
            pk = params[0]
            sk = params[1] if "sk=%s" in s else ""
            v = self.store.get((table, pk, sk))
            if v is not None:
                self.reads.append((table, sk))
            self._result = [{"data": v}] if v is not None else []
        elif s.startswith("SELECT sk, data FROM"):
            pk = params[0]
            paths = [p for p in params[1:]] if "JSON_CONTAINS_PATH" in s else None
            out = []
            for (t, p, sk), v in self.store.items():
                if t != table or p != pk:
                    continue
                if paths is not None:
                    doc = json.loads(v)
                    if not any(p_.startswith("$.") and p_[2:] in doc
                               for p_ in paths):
                        continue
                self.reads.append((table, sk))
                out.append({"sk": sk, "data": v})
            self._result = out
        elif s.startswith("SELECT sk FROM"):
            pk = params[0]
            self._result = [{"sk": k[2]} for k in self.store
                            if k[0] == table and k[1] == pk]
        elif s.startswith("INSERT INTO"):
            pk, sk, data = params
            self.store[(table, pk, sk)] = data
            self._result = []
        elif s.startswith("DELETE FROM"):
            self._result = []
        else:  # pragma: no cover - 想定外の SQL は黙らせず落とす
            raise AssertionError(f"unexpected SQL: {s}")
        return len(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store, reads):
        self.store, self.reads = store, reads

    def cursor(self):
        return _FakeCursor(self.store, self.reads)

    def autocommit(self, _v):
        pass

    def commit(self):
        pass

    def rollback(self):  # pragma: no cover
        pass


@pytest.fixture
def split_store(monkeypatch):
    store, reads = _rows(), []
    monkeypatch.setattr(mc, "TABLE_PREFIX", "beacon", raising=False)
    monkeypatch.setattr(mc, "TABLES",
                        {e: f"beacon_{e}" for e in mc.ENTITIES}, raising=False)
    monkeypatch.setattr(mc, "_conn", lambda: _FakeConn(store, reads))
    return {"store": store, "reads": reads}


def _fire(monkeypatch, split_store, *, pair=True):
    """app の締切 source を 1 回走らせ、送られた DM と report を返す。"""
    sent, report = [], []
    db = app_module.db
    monkeypatch.setattr(db, "append_bus_event",
                        lambda pid, data: sent.append(data) or "ev", raising=False)
    if pair:
        monkeypatch.setattr(db, "get_project_deadline_scope",
                            mc.get_project_deadline_scope, raising=False)
        monkeypatch.setattr(db, "save_deadline_reminder_marks",
                            mc.save_deadline_reminder_marks, raising=False)
    else:
        for name in ("get_project_deadline_scope", "save_deadline_reminder_marks"):
            monkeypatch.delattr(db, name, raising=False)
        # 分割スキーマを v1 読み取りで読む = 本番で起きていた形
        monkeypatch.setattr(db, "get_project",
                            lambda pid, *a, **k: mc._get("projects", pid),
                            raising=False)
        monkeypatch.setattr(db, "save_project",
                            lambda pid, p: None, raising=False)
    app_module._fire_due_deadlines(PID, "2026-10-29T00:00:00.000000Z", report)
    return sent, report


# --- 1. 発火する -----------------------------------------------------------

def test_deadline_reminder_fires_on_a_row_split_project(monkeypatch, split_store):
    """行分割された project でも、期日超過の Target / work item が知らされる。

    これが e-7066 の本体。修正前はここが 0 件だった (本番で発火 0 / tick は緑)。
    """
    sent, report = _fire(monkeypatch, split_store)
    labels = sorted((d["payload"] or {}).get("label", "") for d in sent)
    assert labels == ["期日を過ぎたターゲット", "締切あり"], (
        f"分割された行の締切が発火していない (sent={sent}, report={report})")
    assert all((d["payload"] or {}).get("recipient_session_id") == CLAIMER
               for d in sent), "通知先 (Target の claim 者) が解決できていない"


# --- 2. 1 回きり -----------------------------------------------------------

def test_reminder_is_not_repeated_on_the_next_tick(monkeypatch, split_store):
    """dedup 印が分割された行に刻まれる = 同じ締切で毎分 DM が飛ばない。

    印の書き戻しを落とすと、本番では毎分 DM が飛ぶ (直して悪化する形) ので
    ここで固定する。
    """
    first, _ = _fire(monkeypatch, split_store)
    second, _ = _fire(monkeypatch, split_store)
    assert len(first) == 2 and second == [], (
        f"同じ締切で再送されている (2 回目={second})")
    ms = json.loads(split_store["store"][("beacon_milestones", PID, "ms-1")])
    assert ms[deadline_mod.REMINDED_FOR_KEY] == PAST, \
        "Target 行に dedup 印が刻まれていない"
    entry = json.loads(split_store["store"][("beacon_entries", PID, "ms-1#e-2")])
    assert entry[deadline_mod.REMINDED_FOR_KEY] == PAST, \
        "子行に dedup 印が刻まれていない"


# --- 3. 安いこと -----------------------------------------------------------

def test_read_skips_child_rows_without_a_deadline(monkeypatch, split_store):
    """締切を持たない子行は読まない。

    丸ごと hydrate に戻すとここが赤くなる。Beacon 本番では子行が 4,502 件
    (11.3 MB) あり、毎分それを読んで捨てる形は 2026-08-20 に本番を落とした
    読み方そのもの (e-5367)。正しさだけでなく「安いこと」も退行させない。
    """
    _fire(monkeypatch, split_store)
    read_entries = {sk for table, sk in split_store["reads"]
                    if table == "beacon_entries"}
    assert "ms-1#e-2" in read_entries, "締切を持つ子行は読まれるべき"
    assert "ms-1#e-1" not in read_entries, (
        f"締切を持たない子行まで読んでいる (read={sorted(read_entries)})。"
        f"丸ごと hydrate に戻っていないか確認すること。")


# --- 4. 読み手と書き手は組 -------------------------------------------------

def test_store_pair_is_all_or_nothing(monkeypatch):
    """narrow read だけ在る backend では、narrow の組を採らない。

    部分 hydrate した doc を whole-doc save すると、読まなかった行が消える。
    片方ずつ ``getattr`` で拾う実装に戻すとここが赤くなる。
    """
    db = app_module.db
    monkeypatch.setattr(db, "get_project_deadline_scope",
                        lambda pid: {"milestones": []}, raising=False)
    monkeypatch.delattr(db, "save_deadline_reminder_marks", raising=False)
    read, write = app_module._deadline_store_pair()
    assert write is None, "書き手が無いのに narrow write を採っている"
    assert read is db.get_project, (
        "書き手が無いのに narrow read を採っている (部分 hydrate を whole-doc "
        "save する危険な組み合わせ)")


# --- 5. 読めないときは鳴る -------------------------------------------------

def test_schema_blind_read_is_logged_not_silent(monkeypatch, split_store, caplog):
    """v1 読み取りで分割スキーマを読んだら error を出す (= 黙って 0 件にしない)。

    修正前の本番はここが無音だった。「発火 0 件」と「見えていないので 0 件」は
    外から区別できず、事故になるまで誰も気付けない。
    """
    with caplog.at_level("ERROR"):
        sent, _ = _fire(monkeypatch, split_store, pair=False)
    assert sent == [], "v1 読み取りでは締切が見えない (この試験の前提)"
    assert any("schema-blind" in r.getMessage() for r in caplog.records), (
        f"分割スキーマを盲目に読んだのに error が出ていない "
        f"(records={[r.getMessage() for r in caplog.records]})")


# --- 綴りのズレ防止 ---------------------------------------------------------

def test_sql_deadline_paths_are_derived_from_the_deadline_engine():
    """SQL の JSON path は L2 締切エンジンの綴りから導出される。

    書き写しにすると、職種が締切フィールドを足したとき SQL 側だけ古くなり
    「その締切は永久に発火しない」が静かに起きる。
    """
    assert mc._deadline_json_paths() == tuple(
        f"$.{k}" for k in deadline_mod.DEADLINE_KEYS)
