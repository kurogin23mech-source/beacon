"""Silent quiesce regression pin — test-first for ms-99 / e-2840.

Layer A gap #2 — memo doc ``JipCZ4lk3s6eBFxbTb4x`` §gap#2 の実装。

## 何を pin するか

2026-07-03 の dogfood で Trek ``tk-29a11d2f`` (cross-project 缶詰作業部屋、
scope=Beacon ms-97 + LPS ms-23 + PE ms-25 + PE ms-5) が **silent quiesce**
する現象を実測した (= scheduler が false-terminal 判定で fanout 停止、
executor sessions に progress-check DM が届かなくなる)。

真因は ``lib/trek_scheduler.py`` の tick 判定関数群が **``trek.task_states``
cache (= stamped 済 slot のみ) を slot inventory の真値源として扱う** こと。
現実の slot inventory は ``trek.scope[] × project pool`` の materialize
だが、 これを scheduler が読まないため、 「1 件だけ stamped で terminal、
scope pool にはまだ todo 多数」 の状態を **完遂と誤判定** して quiesce する。

## test-first サイクル

本 file の test 群は **Phase 2 実装 (e-2833 / ``materialize_slots`` 経由書き
換え) 完了後に pass するべき挙動** を pin する。 現行 code (task_states
cache だけを見る) では **fail する** ため、 各 test に
``pytest.mark.xfail(strict=True, raises=(AssertionError, TypeError))`` を
付与している。

- ``strict=True``: Phase 2 land 後、 test が pass (= xpass) すると失敗判定
  になり、 「もう xfail marker を外していい」 と test 更新者に signal を送る
- ``raises=(AssertionError, TypeError)``: 現行 code は AssertionError で fail
  するが、 Phase 2 で関数 signature が ``(trek_doc, get_project)`` に変わる
  過渡期に ``TypeError: got unexpected keyword argument 'get_project'`` の
  可能性も許容する

## 参照

- SPEC ``lgdevK2ZMDVNiR5LwgaF`` (ms-99 SPEC Trek slot schema v2) の AC 7 / 8 /
  9 / 10 — 「tk-29a11d2f 相当データで False を返す」 の受入条件
- memo doc ``JipCZ4lk3s6eBFxbTb4x`` §gap#2 — 本 test の設計根拠
- CORE doc ``5nfTSmCDVUzD4SLzIhI5`` (Trek autonomous loop の真の強制経路) —
  「narrative + observable ack + TTL 罰則」 3 層構造との整合
- 関連 task: e-2833 (scheduler tick 判定 6 関数書き換え、 本 test の green
  化条件)、 e-2832 (``materialize_slots`` primitive、 本 test が呼び出す
  経路の元)
"""

from __future__ import annotations

import os
import sys

import pytest

# lib/ を import path に載せる (既存 test files と同じ pattern)。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek_scheduler as trek_scheduler_mod  # noqa: E402


# ---------------------------------------------------------------------------
# tk-29a11d2f 相当の trek_doc + project pool fixture
# ---------------------------------------------------------------------------


def _make_tk_29a11d2f_like_doc() -> dict:
    """Reproduce the 2026-07-03 dogfood silent quiesce input shape.

    - scope: MS-narrowed entries (task-level narrowing 無し、 legacy
      「MS 全 task を implicit include」 モデル)
    - task_states: 1 件のみ stamp 済 (= leader が user_review に forward
      した特定 task)

    実 tk-29a11d2f では 4 project 4 MS だったが、 test では最小構成
    (1 project + 1 MS) で silent quiesce の本質を再現する (= 「stamped
    entry が全部 terminal、 但し pool には unclaim todo が残っている」)。
    """
    return {
        "trek_id": "tk-29a11d2f-repro",
        "scope": [
            {"project": "p-beacon", "milestone": "ms-active"},
        ],
        "task_states": {
            "e-forwarded": {
                "state": "user_review",
                "updated_by_session_id": "sv-leader",
                "updated_at": "2026-07-03T05:15:18Z",
                "note": "",
            },
        },
        "meta": {},
        "members": [
            {"session_id": "sv-leader", "role": "leader"},
            {"session_id": "sv-executor-1", "role": "member"},
            {"session_id": "sv-executor-2", "role": "member"},
        ],
    }


def _make_project_pool_with_unclaim_todo() -> dict:
    """Project pool that has unclaim todo tasks under ms-active.

    Phase 2 の materialize_slots は scope の MS narrowing を expand する時、
    この pool の task 一覧を舐めて slot 化する。 現行 code はこの pool を
    見ないため silent quiesce する。
    """
    return {
        "milestones": [
            {
                "id": "ms-active",
                "title": "Active MS with residual todo work",
                "entries": [
                    # 1 件は user_review (= trek.task_states に stamp 済と対応)
                    {
                        "id": "e-forwarded",
                        "type": "task",
                        "status": "user_review",
                        "description": "leader が forward した task",
                    },
                    # 残 3 件は unclaim todo (= 本来 fanout で wake すべき対象)
                    {
                        "id": "e-todo-a",
                        "type": "task",
                        "status": "todo",
                        "description": "phase 2 pending work A",
                    },
                    {
                        "id": "e-todo-b",
                        "type": "task",
                        "status": "todo",
                        "description": "phase 2 pending work B",
                    },
                    {
                        "id": "e-todo-c",
                        "type": "task",
                        "status": "todo",
                        "description": "phase 2 pending work C",
                    },
                ],
            }
        ],
        "operations": [],
    }


# ---------------------------------------------------------------------------
# Test 1 (最重要): tk-29a11d2f silent quiesce — is_trek_task_aggregate_terminal
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason=(
        "e-2833 Phase 2 で materialize_slots 経由の tick 判定へ書き換え、 "
        "現行は task_states cache のみ見て false-terminal 判定 = "
        "tk-29a11d2f silent quiesce bug の直接 pin"
    ),
)
def test_aggregate_terminal_false_when_stamped_all_terminal_but_pool_has_unclaim_todo():
    """tk-29a11d2f silent quiesce の直接再現。

    SPEC AC 8 相当: task_states={e-forwarded: user_review} + scope pool に
    e-todo-a/b/c todo が残っている状態で、 aggregate_terminal は False を
    返すべき (= Trek 完遂ではない、 scheduler は fanout を継続すべき)。

    現行 code は task_states cache だけを見て 「1 件 stamped、 全て terminal」
    → True を返す (= 誤 quiesce)。 Phase 2 実装で materialize_slots
    (scope × project pool) を真値源にすれば pool 側の todo 3 件を検出して
    False を返す。
    """
    trek_doc = _make_tk_29a11d2f_like_doc()
    project_pool = _make_project_pool_with_unclaim_todo()

    def _get_project(pid: str) -> dict:
        assert pid == "p-beacon"
        return project_pool

    # Phase 2 実装後、 is_trek_task_aggregate_terminal は
    # (trek_doc, get_project) の 2 引数化される予定 (SPEC 方針 3)。
    # 現行は 1 引数、 keyword 追加は TypeError で捕捉される。
    result = trek_scheduler_mod.is_trek_task_aggregate_terminal(
        trek_doc, get_project=_get_project,
    )
    assert result is False, (
        "aggregate_terminal が True を返した = silent quiesce bug。 "
        "task_states={e-forwarded: user_review} は 1 件しか stamp されていないが、 "
        "scope pool の ms-active には unclaim todo が 3 件残っている。 "
        "materialize_slots で 4 slots (1 user_review + 3 todo) が見えるべき、 "
        "aggregate は False (todo が残っている) が正解。"
    )


# ---------------------------------------------------------------------------
# Test 2: sanity — 本当に完遂した Trek は True を返す (regression 防御)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason="e-2833 Phase 2 で signature が (trek_doc, get_project) 化される予定",
)
def test_aggregate_terminal_true_when_stamped_and_pool_both_all_done():
    """完遂 Trek の positive path pin。

    Trek scope pool の task が全て terminal (done / user_review) の時、
    aggregate_terminal は True を返すべき。 Phase 2 実装で正しく quiesce
    される条件を明文化する (= 「silent 誤判定を消す」 = 「正常判定は保つ」
    の対称 pin)。
    """
    trek_doc = {
        "trek_id": "tk-done",
        "scope": [{"project": "p-x", "milestone": "ms-completed"}],
        "task_states": {
            "e-1": {"state": "done", "updated_by_session_id": "sv-a"},
            "e-2": {"state": "user_review", "updated_by_session_id": "sv-b"},
        },
        "meta": {},
        "members": [],
    }
    pool = {
        "milestones": [{
            "id": "ms-completed",
            "entries": [
                {"id": "e-1", "type": "task", "status": "done"},
                {"id": "e-2", "type": "task", "status": "user_review"},
            ],
        }],
        "operations": [],
    }

    def _gp(pid: str) -> dict:
        return pool

    result = trek_scheduler_mod.is_trek_task_aggregate_terminal(
        trek_doc, get_project=_gp,
    )
    assert result is True


# ---------------------------------------------------------------------------
# Test 3: has_unclaim_todo — pool の todo を認識する
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason="e-2833 Phase 2 で has_unclaim_todo も materialize_slots ベースへ",
)
def test_has_unclaim_todo_true_when_pool_has_todo_but_task_states_empty():
    """has_unclaim_todo が pool の todo を認識する pin。

    scope が MS narrowing で task_states が空 (= まだ誰も stamp してない
    状態) の時、 has_unclaim_todo は True を返すべき (= 「scope pool に
    誰かが claim すべき todo が残っている」 と scheduler に伝える)。

    現行 code は task_states の中の owner="" を数えるだけなので、 pool を
    見ずに False を返す (= fresh executor が silent skip される真因)。
    """
    trek_doc = {
        "trek_id": "tk-fresh",
        "scope": [{"project": "p-y", "milestone": "ms-just-started"}],
        "task_states": {},  # まだ誰も stamp していない
        "meta": {},
        "members": [{"session_id": "sv-new-exec", "role": "member"}],
    }
    pool = {
        "milestones": [{
            "id": "ms-just-started",
            "entries": [
                {"id": "e-a", "type": "task", "status": "todo"},
                {"id": "e-b", "type": "task", "status": "todo"},
            ],
        }],
        "operations": [],
    }

    def _gp(pid: str) -> dict:
        return pool

    result = trek_scheduler_mod.has_unclaim_todo(trek_doc, get_project=_gp)
    assert result is True, (
        "has_unclaim_todo が False を返した = fresh executor silent skip の直接原因。 "
        "task_states 空でも scope pool に todo が残っていれば True を返すべき。"
    )


# ---------------------------------------------------------------------------
# Test 4: should_fire_executor_tick — fresh executor を wake できる
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason="e-2833 Phase 2: should_fire_executor_tick も pool 参照へ",
)
def test_should_fire_executor_tick_true_for_fresh_executor_with_pool_todo():
    """新規 executor session が scope 内 pool の todo を pick up する経路を pin。

    session_id が claim を持たない (= fresh) 状態で、 scope pool に todo
    が残っている時、 should_fire_executor_tick は True を返すべき。
    これは has_unclaim_todo が pool を見ることに依存する。

    現行 code は task_states 経由でしか判断できないため fresh executor を
    silent skip する (= LPS phase 2 未着手 executor が wake しない原因)。
    """
    trek_doc = _make_tk_29a11d2f_like_doc()
    pool = _make_project_pool_with_unclaim_todo()

    def _gp(pid: str) -> dict:
        return pool

    result = trek_scheduler_mod.should_fire_executor_tick(
        trek_doc,
        session_id="sv-executor-1",
        get_project=_gp,
    )
    assert result is True, (
        "should_fire_executor_tick が False を返した = 新規 executor session が "
        "scope pool の todo に気付けず silent skip される。 tk-29a11d2f dogfood の "
        "『LPS phase 2 未着手 session が wake しない』 経路を直接再現。"
    )


# ---------------------------------------------------------------------------
# Test 5: is_completion_ready — pool 参照後は完遂通知が誤発火しない
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason="e-2833 Phase 2: is_completion_ready も aggregate_terminal 経由で pool 参照",
)
def test_is_completion_ready_false_when_pool_has_unclaim_todo():
    """完遂通知の誤発火防御 pin。

    is_completion_ready は aggregate_terminal を経由するので、 aggregate 判定
    が pool を見るように直せば連鎖的に正しくなる。 tk-29a11d2f 状態で
    completion_ready が True を返すと、 「Trek 完遂しました」 の 1 発通知が
    誤爆する (= UX 事故、 leader が困惑する)。

    Phase 2 land 後は False を返すべき。
    """
    trek_doc = _make_tk_29a11d2f_like_doc()
    pool = _make_project_pool_with_unclaim_todo()

    def _gp(pid: str) -> dict:
        return pool

    result = trek_scheduler_mod.is_completion_ready(trek_doc, get_project=_gp)
    assert result is False


# ---------------------------------------------------------------------------
# Test 6: cross-project sanity — 複数 scope project で pool を舐める
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=(AssertionError, TypeError),
    reason="e-2833 Phase 2: cross-project pool 参照経路 pin",
)
def test_aggregate_terminal_false_when_one_of_multi_scope_projects_has_pool_todo():
    """cross-project Trek (実 tk-29a11d2f と同 shape) の silent quiesce pin。

    scope が複数 project にまたがっている時、 いずれかの project pool に
    unclaim todo が残っていれば aggregate_terminal は False を返すべき。
    実 tk-29a11d2f は 4 project scope だったが、 test では 2 project で
    十分再現できる。
    """
    trek_doc = {
        "trek_id": "tk-cross-project",
        "scope": [
            {"project": "p-alpha", "milestone": "ms-alpha-done"},
            {"project": "p-beta", "milestone": "ms-beta-active"},
        ],
        "task_states": {
            "e-alpha-1": {"state": "done"},
        },
        "meta": {},
        "members": [],
    }

    def _gp(pid: str) -> dict:
        if pid == "p-alpha":
            return {
                "milestones": [{
                    "id": "ms-alpha-done",
                    "entries": [{"id": "e-alpha-1", "type": "task", "status": "done"}],
                }],
                "operations": [],
            }
        if pid == "p-beta":
            return {
                "milestones": [{
                    "id": "ms-beta-active",
                    "entries": [
                        {"id": "e-beta-1", "type": "task", "status": "todo"},
                        {"id": "e-beta-2", "type": "task", "status": "todo"},
                    ],
                }],
                "operations": [],
            }
        raise AssertionError(f"Unexpected project id: {pid}")

    result = trek_scheduler_mod.is_trek_task_aggregate_terminal(
        trek_doc, get_project=_gp,
    )
    assert result is False, (
        "cross-project Trek で片方の pool に todo が残っているのに aggregate=True。 "
        "silent quiesce が cross-project 経路でも起きる証拠。"
    )
