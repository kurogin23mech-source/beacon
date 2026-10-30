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
  4. 読み書きの口が**全 backend に揃っている**こと、そして呼び出し側が「在れば使う」
     で経路を選ばないこと。最初は MySQL にだけ置いて getattr で分岐していたが、
     store_router は 1 プロセスに 1 つの可変 module なので、別のテストが backend を
     切り替えて読み込み直すと意図しない実装が選ばれる (実際に踏んだ)。
  5. 分割された文書なのに Target が付いてこない読み方をした時、黙って 0 件にせず
     error を出す。
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


def _fire(monkeypatch, split_store, *, blind=False):
    """app の締切 source を 1 回走らせ、送られた DM と report を返す。

    ``blind=True`` は「分割された文書を v1 読み取りで読む」= 本番で起きていた形。
    """
    sent, report = [], []
    db = app_module.db
    monkeypatch.setattr(db, "append_bus_event",
                        lambda pid, data: sent.append(data) or "ev", raising=False)
    if blind:
        monkeypatch.setattr(db, "get_project_deadline_scope",
                            lambda pid, *a, **k: mc._get("projects", pid),
                            raising=False)
        monkeypatch.setattr(db, "save_deadline_reminder_marks",
                            lambda *a, **k: 0, raising=False)
    else:
        monkeypatch.setattr(db, "get_project_deadline_scope",
                            mc.get_project_deadline_scope, raising=False)
        monkeypatch.setattr(db, "save_deadline_reminder_marks",
                            mc.save_deadline_reminder_marks, raising=False)
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


# --- 4. 口が全 backend に揃っていること ---------------------------------

def test_every_backend_exposes_the_deadline_pair():
    """締切リマインドの 2 つの口は、どの保存層にも同じ名前で在る。

    最初は MySQL にだけ置いて、呼び出し側が「在れば使う」で経路を切り替えていた。
    store_router は 1 プロセスに 1 つしか無い可変 module なので、別のテストが
    backend を切り替えて読み込み直すと**呼び出し側が意図しない実装が選ばれる**
    (実際それで、保存層を差し替えたはずの試験が本物の MySQL を見に行って落ちた)。
    揃っていれば分岐そのものが要らない。1 つでも欠けたらここが赤くなる。
    """
    import ast
    import pathlib as _pl
    router = _pl.Path(__file__).resolve().parents[1] / "server" / "store_router.py"
    tree = ast.parse(router.read_text(encoding="utf-8"))
    per_backend = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and \
                node.module.endswith("_client"):
            per_backend.setdefault(node.module, set()).update(
                a.name for a in node.names)
    assert per_backend, "store_router から backend の import を読み取れなかった"
    for name in ("get_project_deadline_scope", "save_deadline_reminder_marks"):
        missing = sorted(b for b, names in per_backend.items() if name not in names)
        assert not missing, (
            f"{name} が {missing} に無い。呼び出し側 (server/app.py) は分岐せずに "
            f"db.{name} を呼ぶので、欠けている backend では AttributeError になる。")


def test_app_calls_the_pair_without_feature_detection():
    """app は「在れば使う」をしない (= getattr で経路を選ばない)。

    分岐を戻すと、backend を切り替えて読み込み直した後に意図しない実装が選ばれる
    形に逆戻りする。
    """
    import ast
    import pathlib as _pl
    src = (_pl.Path(__file__).resolve().parents[1] / "server" / "app.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    for fname in ("_load_deadline_scope", "_persist_deadline_reminder_marks"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        bad = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "getattr"]
        assert not bad, (
            f"{fname} が getattr で保存層の口を探している。全 backend が口を持つので "
            f"分岐は不要 — 分岐があると別 backend で読み込み直された時に意図しない "
            f"実装が選ばれる。")


# --- 5. 読めないときは鳴る -------------------------------------------------

def test_schema_blind_read_is_logged_not_silent(monkeypatch, split_store, caplog):
    """v1 読み取りで分割スキーマを読んだら error を出す (= 黙って 0 件にしない)。

    修正前の本番はここが無音だった。「発火 0 件」と「見えていないので 0 件」は
    外から区別できず、事故になるまで誰も気付けない。
    """
    with caplog.at_level("ERROR"):
        sent, _ = _fire(monkeypatch, split_store, blind=True)
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


# --- 読んだ後に期日が動いた場合 --------------------------------------------

def test_mark_is_not_stamped_when_the_deadline_moved_after_the_read(
        monkeypatch, split_store):
    """知らせた後・印を刻む前に期日が動いたら、その項目には刻まない。

    刻んでしまうと「**新しい期日で知らせ済み**」という嘘の印が残り、その期日が
    過ぎても二度と知らせない (= 通知が 1 回まるごと消える)。印は「どの期日値に
    ついて知らせたか」であって「この項目に触った」ではない。

    反転ハーネスで見つかった穴: 値の一致確認を外しても他の試験は全部緑だった。
    """
    marks = [{"target_id": "ms-1", "item_id": "ms-1", "deadline": PAST}]
    # 読んだ後に期日が動いた状態を作る
    moved = json.loads(split_store["store"][("beacon_milestones", PID, "ms-1")])
    moved["target_date"] = "2001-02-03"
    split_store["store"][("beacon_milestones", PID, "ms-1")] = json.dumps(moved)

    stamped = mc.save_deadline_reminder_marks(PID, marks, "2026-10-29T00:00:00Z")

    row = json.loads(split_store["store"][("beacon_milestones", PID, "ms-1")])
    assert stamped == 0, "期日が動いた項目に刻んでしまっている"
    assert deadline_mod.REMINDED_FOR_KEY not in row, (
        f"新しい期日 2001-02-03 を『知らせ済み』にしてしまった (row={row})。"
        f"この印が残ると、その期日が過ぎても二度と知らせない。")


def test_stamp_marks_matches_on_the_deadline_value_not_just_the_key():
    """規則そのもの (L2) を直に測る — 鍵が一致しても値が違えば刻まない。"""
    item = {"id": "e-9", "deadline": "2030-01-01"}
    marks = [{"target_id": "ms-1", "item_id": "e-9", "deadline": "2020-01-01"}]
    n = deadline_mod.stamp_marks([(("ms-1", "e-9"), item)], marks, "now")
    assert n == 0 and deadline_mod.REMINDED_FOR_KEY not in item
    # 値が一致していれば刻む (= 上の 0 が「常に 0」でないことの裏取り)
    marks[0]["deadline"] = "2030-01-01"
    n = deadline_mod.stamp_marks([(("ms-1", "e-9"), item)], marks, "now")
    assert n == 1 and item[deadline_mod.REMINDED_FOR_KEY] == "2030-01-01"


# --- 保存層 × スキーマ の全組み合わせ ---------------------------------------
#
# 独立レビューがここで本物のバグを見つけた: mysql_client の whole-doc 分岐が
# import を欠いて NameError で即死していた。既存の試験は MySQL を必ず
# schema_version=3 で呼んでいたので、同じ関数のもう半分を一度も通っていなかった。
# 「3 つの保存層 × {1 文書, 行分割}」を machine で一覧して回す。

def _whole_doc_project() -> dict:
    return {
        "project_id": PID, "name": "whole",
        "milestones": [{
            "id": "ms-1", "title": "期日を過ぎたターゲット", "status": "in_progress",
            "target_date": PAST, "occupation": {"session_id": CLAIMER},
            "entries": [{"id": "e-2", "type": "task", "status": "todo",
                         "description": "締切あり", "deadline": PAST}],
        }],
    }


def _load_backend_module(name: str):
    """backend module を **ファイルから直接**読む。

    ``importlib.import_module`` を使わないのは、別のテストファイルが
    ``sys.modules["firestore_client"] = <別の module>`` と差し替えたまま残すため
    (tests/test_trek_scheduler_api.py:35)。名前で引くと別物が返り、こちらが
    差し替えたつもりの関数が使われずに本物の接続へ行ってしまう。ファイルから
    読めば、他のテストが何をしていてもこの試験の対象は 1 つに定まる。
    """
    import importlib.util
    import pathlib as _pl
    path = _pl.Path(__file__).resolve().parents[1] / "server" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_isolated_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("backend", ["mysql", "firestore", "dynamodb"])
def test_whole_doc_backends_stamp_marks_without_crashing(backend, monkeypatch,
                                                         split_store):
    """1 文書で持つ経路でも印が刻める (= import 漏れ等で即死しない)。

    MySQL は schema_version を持たない project を渡して **whole-doc 分岐**に入れる
    (行分割の分岐は別の試験が見ている)。Firestore / DynamoDB は常にこの経路。
    """
    doc = _whole_doc_project()
    marks = [{"target_id": "ms-1", "item_id": "ms-1", "deadline": PAST},
             {"target_id": "ms-1", "item_id": "e-2", "deadline": PAST}]

    if backend == "mysql":
        # schema_version 無し = whole-doc 分岐
        split_store["store"][("beacon_projects", PID, "")] = json.dumps(doc)
        saved = {}
        monkeypatch.setattr(mc, "_put",
                            lambda e, pk, data, sk="": saved.update({e: data}))
        n = mc.save_deadline_reminder_marks(PID, marks, "2026-10-29T00:00:00Z")
        assert "projects" in saved, "印を刻んだのに保存していない"
        written = saved["projects"]
    else:
        mod = _load_backend_module(f"{backend}_client")
        saved = {}
        monkeypatch.setattr(mod, "get_project", lambda pid: doc, raising=False)
        monkeypatch.setattr(mod, "save_project",
                            lambda pid, d: saved.update({"doc": d}), raising=False)
        n = mod.save_deadline_reminder_marks(PID, marks, "2026-10-29T00:00:00Z")
        assert saved, "印を刻んだのに保存していない"
        written = saved["doc"]

    assert n == 2, f"{backend}: 2 件刻めるはずが {n} 件"
    ms = written["milestones"][0]
    assert ms[deadline_mod.REMINDED_FOR_KEY] == PAST
    assert ms["entries"][0][deadline_mod.REMINDED_FOR_KEY] == PAST


@pytest.mark.parametrize("backend", ["mysql", "firestore", "dynamodb"])
def test_whole_doc_backends_expose_the_read_side_too(backend, monkeypatch,
                                                     split_store):
    """読み側の口も全保存層で呼べる (= 書き側だけ在る半実装を作らせない)。"""
    if backend == "mysql":
        assert mc.get_project_deadline_scope(PID) is not None
    else:
        mod = _load_backend_module(f"{backend}_client")
        monkeypatch.setattr(mod, "get_project",
                            lambda pid: _whole_doc_project(), raising=False)
        assert mod.get_project_deadline_scope(PID)["milestones"]


# --- 鍵の決め方が 1 箇所であること -------------------------------------------

def test_mark_key_is_defined_once_and_used_by_both_sides():
    """印を宛てる鍵の作り方は 1 つ。付ける側と刻む側でずれると印が永久に付かない。"""
    import ast
    import pathlib as _pl
    import deadline_marks
    assert deadline_marks.mark_key("ms-1", {"id": "e-2"}) == ("ms-1", "e-2")
    # id が無い時の落とし所も 1 つ (両側で別々に決めない)
    assert deadline_marks.mark_key("ms-1", {}) == ("ms-1", "")

    # 刻む側 (mysql の行ループ) が自前で鍵を組み立てていないこと
    src = (_pl.Path(__file__).resolve().parents[1]
           / "server" / "mysql_client.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "save_deadline_reminder_marks")
    tuples = [n for n in ast.walk(fn)
              if isinstance(n, ast.Tuple) and len(n.elts) == 2
              and any(isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute)
                      and e.func.attr == "get" for e in n.elts)]
    assert not tuples, (
        "鍵を手で組み立てている箇所がある。deadline_marks.mark_key を使うこと "
        "(付ける側と刻む側でずれると印が 1 つも刻まれず、毎分再送になる)。")


# --- 盲目/刻み漏れが「外から読める面」に出ること -----------------------------

def test_blind_read_is_reported_to_the_tick_report_not_only_the_log(
        monkeypatch, split_store):
    """分割された文書を盲目に読んだら、tick の応答に異常として載る。

    ログにだけ書いても、箱の外の見張りが読む面
    (scheduled_fires / tick-health の scheduled_by_source) からは
    「0 件 = 何も期日が来ていない」と区別が付かない = 直したことにならない。
    """
    import tick_health
    _sent, report = _fire(monkeypatch, split_store, blind=True)
    assert any(e.get("error") for e in report), (
        f"盲目読みが tick の応答に出ていない (report={report})")
    for e in report:
        e.setdefault("source", "deadline")
    summary = tick_health.summarize_scheduled_fires(report, ("deadline",))
    assert summary["deadline"]["error"] >= 1, summary
    assert tick_health.failing_scheduled_sources(summary, ("deadline",)) \
        == ["deadline"], "見張りが異常として拾えていない"


def test_unstamped_marks_are_reported_as_error_so_the_watchdog_sees_them(
        monkeypatch, split_store):
    """印を刻めなかったら見張りが読む面に異常として出る (= 毎分の再送に気付ける)。

    "warning" のような別名で積むと summarize_scheduled_fires は skipped に
    丸めるので、誰にも届かない。
    """
    import tick_health
    db = app_module.db
    monkeypatch.setattr(db, "append_bus_event",
                        lambda pid, data: "ev", raising=False)
    monkeypatch.setattr(db, "get_project_deadline_scope",
                        mc.get_project_deadline_scope, raising=False)
    monkeypatch.setattr(db, "save_deadline_reminder_marks",
                        lambda *a, **k: 0, raising=False)   # 1 件も刻めなかった
    report = []
    app_module._fire_due_deadlines(PID, "2026-10-29T00:00:00.000000Z", report)
    for e in report:
        e.setdefault("source", "deadline")
    summary = tick_health.summarize_scheduled_fires(report, ("deadline",))
    assert summary["deadline"]["error"] >= 1, (
        f"刻み漏れが見張りの集計に出ていない (report={report}, summary={summary})")
