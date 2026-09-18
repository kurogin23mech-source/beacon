"""ms-173 (e-6564): occupation claim とセッション行の working_target 宣言の溶接。

営業プロジェクト (profession=sales) の運用室で、メインセッションの「作業中の
ターゲット (担当商談)」が出なかった。担当の導出 fallback (branch / cwd の
ms-番号 / focus.milestone) は開発前提で、main branch で商談を進める営業
セッションではどの経路もヒットしない (2026-09-18 実測: Cairn Sales の live 行が
working_target.target=null / source=none)。

fix: work-start の occupation claim (`_claim_occupation_for_work`) が stamp と
同時にセッション行へ working_target を宣言する — derive の第 1 優先
(declaration) に載り、職種に依らず運用室に担当が出る。release の counterpart は
自分の claim を手放したときだけ、同じ target を指す宣言をクリアする。

すべて best-effort: local mode / offline / テスト文脈からの本番書き込みは
黙って skip し、claim 本体を止めない。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands_shared as cs  # noqa: E402


def _project():
    return {
        "profession": "sales",
        "milestones": [{"id": "ms-1", "status": "in_progress"}],
        "operations": [{"id": "op-1", "status": "open"}],
        "opportunities": [
            {"id": "opp-4", "label": "株式会社abs：AI/業務自動化の提案検討",
             "status": "open", "phase": "初回打診"}],
    }


class _FakeClient:
    """intent 書き込みと session 読みを記録するだけの偽 API client。"""

    def __init__(self, session_doc=None):
        self.intent_calls = []
        self._session_doc = session_doc or {}

    def upsert_session_intent(self, project_id, session_id, **kw):
        self.intent_calls.append((project_id, session_id, kw))
        return {"status": "ok"}

    def get_session(self, project_id, session_id):
        return self._session_doc


def _wire(monkeypatch, client, *, sid="sv-me", api_url="https://api.test"):
    monkeypatch.setattr(cs, "_resolve_session_id", lambda: sid)
    monkeypatch.setattr(cs, "_get_api_client",
                        lambda: (client, {"api_url": api_url,
                                          "project_id": "proj-1"}))
    monkeypatch.setattr(cs, "_resolve_bus_project_id", lambda config: "proj-1")
    import agent
    monkeypatch.setattr(agent, "get_actor", lambda: {"machine": "m", "agent": "a"})


def test_claim_declares_working_target_for_opportunity(monkeypatch):
    """商談への work-start claim がセッション行に opp を宣言すること — 営業の
    運用室に担当が出る唯一の経路 (branch/cwd/focus は営業で全 miss)。"""
    client = _FakeClient()
    _wire(monkeypatch, client)
    data = _project()

    assert cs._claim_occupation_for_work(data, "opp-4") is True

    assert len(client.intent_calls) == 1
    pid, sid, kw = client.intent_calls[0]
    assert (pid, sid) == ("proj-1", "sv-me")
    tgt = kw["working_target"]["target"]
    assert tgt["kind"] == "opportunity"
    assert tgt["id"] == "opp-4"
    assert tgt["title"] == "株式会社abs：AI/業務自動化の提案検討"


def test_release_of_own_claim_clears_matching_declaration(monkeypatch):
    """自分の claim を手放したら、同じ target を指す宣言だけがクリアされること。"""
    session_doc = {"intent": {"working_target": {
        "target": {"kind": "opportunity", "id": "opp-4"}}}}
    client = _FakeClient(session_doc=session_doc)
    _wire(monkeypatch, client)
    data = _project()
    cs._claim_occupation_for_work(data, "opp-4")
    client.intent_calls.clear()

    cs._release_occupation_for_transition(data, "opp-4", reason="phase-advance")

    assert client.intent_calls == [("proj-1", "sv-me", {"working_target": {}})]


def test_release_leaves_declaration_pointing_elsewhere(monkeypatch):
    """claim A → claim B の後に A を release しても、B を指す宣言は消えないこと
    (巻き添えクリアの防止)。"""
    session_doc = {"intent": {"working_target": {
        "target": {"kind": "operation", "id": "op-1"}}}}
    client = _FakeClient(session_doc=session_doc)
    _wire(monkeypatch, client)
    data = _project()
    cs._claim_occupation_for_work(data, "opp-4")
    client.intent_calls.clear()

    cs._release_occupation_for_transition(data, "opp-4", reason="done")

    assert client.intent_calls == []  # 宣言は op-1 を指しており opp-4 ではない


def test_release_of_another_sessions_claim_does_not_touch_declaration(monkeypatch):
    """他セッションの claim の takeover 解放では、こちらの宣言経路を一切呼ばない
    こと (宣言はその session 自身のもの)。"""
    client = _FakeClient()
    _wire(monkeypatch, client)
    data = _project()
    import core
    core.claim_occupation(data, "opp-4", session_id="sv-other")

    cs._release_occupation_for_transition(data, "opp-4", reason="takeover")

    assert client.intent_calls == []


def test_prod_write_is_refused_from_test_context(monkeypatch):
    """テスト文脈から本番クラウド (beacon-ai.dev) への intent 書き込みは構造で
    拒むこと (ms-123 e-4029 の tap 閉じと同型)。claim 本体は成功する。"""
    client = _FakeClient()
    _wire(monkeypatch, client, api_url="https://beacon-ai.dev")
    data = _project()

    assert cs._claim_occupation_for_work(data, "opp-4") is True

    assert client.intent_calls == []


def test_local_mode_is_silent_and_claim_still_lands(monkeypatch):
    """cloud 未設定 (_get_api_client が SystemExit) でも claim 本体は成立し、
    例外が漏れないこと。"""
    monkeypatch.setattr(cs, "_resolve_session_id", lambda: "sv-me")

    def _exit():
        raise SystemExit(1)

    monkeypatch.setattr(cs, "_get_api_client", _exit)
    import agent
    monkeypatch.setattr(agent, "get_actor", lambda: {})
    data = _project()

    assert cs._claim_occupation_for_work(data, "opp-4") is True
    assert data["opportunities"][0]["occupation"]["session_id"] == "sv-me"
