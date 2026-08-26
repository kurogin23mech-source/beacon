#!/usr/bin/env python3
"""decision_vocab.py — the single source of the decision-arm vocabulary (ms-154 e-5652).

The ``decided_by`` enum used to be defined twice — ``server/decision_event.py``
(``DECIDED_BY``, the schema authority) and ``lib/cmd_decision.py`` (``_DECIDED_BY``,
the CLI's early-reject copy) — kept in sync only by a hand comment. That is a
silent-drift hazard (§2 SSoT / PR#676 maintainability review): adding a fifth
attribution to one side and forgetting the other splits the vocabulary.

This leaf module is the shared source both sides import. It lives in ``lib/``
because ``lib/`` is on the path of every runtime that needs it:
- the CLI runs from ``lib/`` natively;
- the server adds ``lib/`` to ``sys.path`` at startup (``server/app.py``) and already
  imports lib leaves (``core`` / ``work_model``);
- tests get ``lib/`` on the path via ``tests/conftest.py``.

Pure data, stdlib-only — no imports, so it can be a leaf for both the CLI and the
server without introducing a dependency cycle.
"""
from __future__ import annotations


# decided_by (= 誰が決めたか) の一級 enum (ms-154 §設計方針1 / AC1)。
# autonomous-AI が最も audit-critical (= 人間が見ていない判断こそ検分が要る)。
DECIDED_BY: frozenset[str] = frozenset(
    {
        "autonomous-AI",            # 人間未確認の AI 単独決定 (最も audit-critical)
        "AI-proposed-human-chose",  # AI が選択肢を提示し人間が選んだ
        "human-delegated",          # 人間が AI に判断を委譲した
        "programmatic",             # コードが機械的に決めた (= AI 判断ですらない)
    }
)
