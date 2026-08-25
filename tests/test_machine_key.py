"""Unit tests for server/machine_key.py (ms-151 / e-5474).

Pure module: no store, no network. Exercises the crypto/format primitives that
are the single source of truth for machine key 発行・失効・検証:

- issue() が raw token と hash-only record を返す (平文 secret を保存しない)。
- token は形式検査で parse でき、往復する。
- verify_token() が正しい key を通し、失効 / すり替え / 改竄 / 別 project を弾く。
- 規模の契約: 大量発行しても key_id / secret が衝突しない。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import machine_key as mk  # noqa: E402

NOW = "2026-08-24T12:00:00Z"


def test_issue_returns_raw_and_hash_only_record():
    raw, record = mk.issue("beacon-b95643", label="PE Lambda",
                           created_by="u-1", now=NOW)
    # raw token は machine key の形。
    assert mk.looks_like_machine_key(raw)
    # record は平文 secret を持たず、hash だけを保存する。
    assert "secret" not in record
    assert record["secret_hash"] and len(record["secret_hash"]) == 64
    assert record["project_id"] == "beacon-b95643"
    assert record["label"] == "PE Lambda"
    assert record["created_by"] == "u-1"
    assert record["created_at"] == NOW
    assert record["revoked_at"] is None
    # raw token の secret 部は record の hash と対応する。
    _, _, secret = mk.parse_token(raw)
    assert mk.hash_secret(secret) == record["secret_hash"]


def test_token_roundtrip():
    raw, record = mk.issue("beacon-b95643", now=NOW,
                           key_id="kid123", secret="s3cr3t")
    assert raw == "bmk.beacon-b95643.kid123.s3cr3t"
    assert mk.parse_token(raw) == ("beacon-b95643", "kid123", "s3cr3t")


def test_parse_rejects_malformed():
    assert mk.parse_token("") is None
    assert mk.parse_token("bcli.abc.def") is None          # 別 prefix
    assert mk.parse_token("bmk.only-two.parts") is None     # 部品不足
    assert mk.parse_token("bmk.proj..secret") is None       # 空 key_id
    assert mk.parse_token("bmk.proj.kid.") is None          # 空 secret


def test_parse_project_id_with_dots_is_safe():
    # project_id が将来 dot を含んでも末尾剥がしで壊れない。
    raw = mk.format_token("a.b.c", "kid", "sec")
    assert mk.parse_token(raw) == ("a.b.c", "kid", "sec")


def test_verify_accepts_valid_key():
    raw, record = mk.issue("beacon-b95643", now=NOW)
    assert mk.verify_token(raw, record) is record


def test_verify_rejects_revoked_key():
    raw, record = mk.issue("beacon-b95643", now=NOW)
    record["revoked_at"] = "2026-08-24T13:00:00Z"
    assert mk.verify_token(raw, record) is None


def test_verify_rejects_missing_record():
    raw, _ = mk.issue("beacon-b95643", now=NOW)
    assert mk.verify_token(raw, None) is None


def test_verify_rejects_tampered_secret():
    raw, record = mk.issue("beacon-b95643", now=NOW,
                           key_id="kid", secret="right")
    forged = mk.format_token("beacon-b95643", "kid", "wrong")
    assert mk.verify_token(forged, record) is None


def test_verify_rejects_cross_project_substitution():
    # 別 project の token を、ある project の record にすり替えても通らない。
    raw_other, _ = mk.issue("beacon-OTHER", now=NOW,
                            key_id="kid", secret="sec")
    _, record_here = mk.issue("beacon-b95643", now=NOW,
                              key_id="kid", secret="sec")
    assert mk.verify_token(raw_other, record_here) is None


def test_verify_rejects_key_id_mismatch():
    raw, _ = mk.issue("beacon-b95643", now=NOW, key_id="kidA", secret="sec")
    _, other = mk.issue("beacon-b95643", now=NOW, key_id="kidB", secret="sec")
    assert mk.verify_token(raw, other) is None


def test_redacted_hides_hash():
    _, record = mk.issue("beacon-b95643", label="x", now=NOW)
    view = mk.redacted(record)
    assert "secret_hash" not in view
    assert view["revoked"] is False
    record["revoked_at"] = NOW
    assert mk.redacted(record)["revoked"] is True


def test_scale_issue_no_collisions():
    # 規模の契約 (CORE doc scale-contract-principle): 大量発行で key_id / secret /
    # raw token が衝突しない (乱数生成の一意性)。
    raws, kids, secrets_ = set(), set(), set()
    for _ in range(500):
        raw, record = mk.issue("beacon-b95643", now=NOW)
        _, kid, secret = mk.parse_token(raw)
        raws.add(raw)
        kids.add(kid)
        secrets_.add(secret)
    assert len(raws) == 500
    assert len(kids) == 500
    assert len(secrets_) == 500
