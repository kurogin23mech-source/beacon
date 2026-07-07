"""mysql_client.list_documents が sk を doc_id の真値として返すことのテスト。

Firestore→MySQL 移行 (ms-96) で入った古い doc は payload (data JSON) に doc_id
フィールドを持たず、doc_id は sk 列にしか無い。以前 list_documents は SELECT data
のみの _query を使い data.get("doc_id","") で拾っていたため、移行済み doc の doc_id
が全て空になり `beacon doc show` が効かなくなっていた。sk を doc_id の fallback に
する修正を、_query_rows を monkeypatch して DB 無しで検証する。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

mysql_client = pytest.importorskip("mysql_client")


def test_list_documents_uses_sk_as_doc_id(monkeypatch):
    # (sk, data) 行。移行済み doc は data に doc_id 無し、新規 doc は有り、削除は skip。
    fake_rows = [
        ("sk-migrated-abc", {"title": "Old SPEC", "scope": "spec",
                             "updated_at": "2026-01-01T00:00:00Z"}),
        ("sk-new-xyz", {"doc_id": "sk-new-xyz", "title": "New Doc", "scope": "memo",
                        "updated_at": "2026-02-01T00:00:00Z"}),
        ("sk-deleted", {"deleted": True, "title": "gone"}),
    ]
    monkeypatch.setattr(mysql_client, "_query_rows", lambda entity, pk: fake_rows)

    docs = mysql_client.list_documents("proj")
    by_title = {d["title"]: d for d in docs}

    # 移行済み doc: data に doc_id が無くても sk が doc_id として返る (これが修正点)。
    assert by_title["Old SPEC"]["doc_id"] == "sk-migrated-abc"
    # 新規 doc: payload の doc_id を使う (sk と一致)。
    assert by_title["New Doc"]["doc_id"] == "sk-new-xyz"
    # 削除済みは一覧から除外。
    assert "gone" not in by_title
    # 全 doc の doc_id が非空 = beacon doc show で addressable。
    assert all(d["doc_id"] for d in docs)


def test_list_documents_prefers_payload_doc_id_when_present(monkeypatch):
    # payload の doc_id と sk が (万一) 食い違っても payload を優先する。
    monkeypatch.setattr(
        mysql_client, "_query_rows",
        lambda entity, pk: [("sk-x", {"doc_id": "payload-id", "title": "T",
                                      "updated_at": "2026-01-01"})],
    )
    docs = mysql_client.list_documents("proj")
    assert docs[0]["doc_id"] == "payload-id"
