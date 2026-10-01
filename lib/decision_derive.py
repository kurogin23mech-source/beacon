"""ms-166 e-5972 — derive decision-arm records from artifacts that already carry
the "why".

The 作り替え direction 2 (SPEC): a judgment trail should be a DERIVED product of
what teams already write — PR intent, commit rationale, task done_reason, review
findings — not a separate ``beacon decision record`` the AI must remember to call.
This mirrors the deliverable-changelog precedent (ms-161: a deliverable is a derived
product of the log, not hand-recorded).

Scope of THIS module: **PR intent** — the primary "why" artifact not yet on the
decision arm (task done_reason is already welded via the task-done seam; review
adjudication via e-5971; completion verdict via e-5978). A PR's intent IS a declared
decision: "we decided to make change X, because Y." We normalize it into a
``pr-intent`` decision, both write-through (at ``beacon pr add``) and by backfill
(``beacon decision derive`` over existing PRs).

Boundary (SPEC): a conversation's pure introspective judgment that was never written
into any artifact is out of scope — it does not reach a seam and cannot be derived.
Deriving invents no "why": an intent-less PR yields no decision (empty = honest
"no grounds", not a fabricated one — same principle as decision_event evidence).

Pure module: no I/O. The caller (cmd_pr write-through / cmd_decision derive) owns the
cloud POST via ``commands_shared.best_effort_decision_write``.
"""
from __future__ import annotations

DERIVED_PR_INTENT_KIND = "pr-intent"

# Cap on the existing-decision scan the backfill reads to build its dedup set.
# Idempotency holds only when this read covers ALL existing pr-intent decisions;
# past this many, the backfill must warn / abort rather than silently re-derive
# (AX/maintainability review of e-5972). Named so the limit is visible, not magic.
DEDUP_SCAN_LIMIT = 2000


def normalize_pr_number(pr_number) -> str:
    """PR number → canonical str dedup key (ms-166 e-5972, maintainability M4).

    The two callers feed different types — write-through passes the ``str`` from
    ``_pr_number_from_url``, backfill passes the ``int`` from ``meta.pr_number`` —
    so the dedup key (``covered_pr_numbers``) and the derivation must agree on ONE
    type or the same PR looks uncovered and re-derives. Normalize to ``str`` here,
    the single place both paths pass through. Empty / None → ``""``."""
    if pr_number is None:
        return ""
    return str(pr_number).strip()


def build_pr_intent_decision(pr_number, title: str, intent: str, *,
                             decided_by: str):
    """Build the ``pr-intent`` decision payload from a PR's declared intent, or
    ``None`` when it cannot be derived idempotently.

    ``decision`` (what) = the change (PR title); ``rationale`` (why) = the stated
    intent; ``evidence`` links the PR (``pr:<n>``) so "which decision for PR N" is
    queryable AND so the derivation is idempotent (``covered_pr_numbers`` reads it
    back). Returns ``None`` when either the intent is empty (we never fabricate a
    "why") OR the PR number is missing (without a dedup key, re-running would
    duplicate — an un-numbered PR is not derivable, AX/maintainability review)."""
    intent = (intent or "").strip()
    pr = normalize_pr_number(pr_number)
    if not intent or not pr:
        return None
    return {
        "kind": DERIVED_PR_INTENT_KIND,
        "decision": (title or "").strip() or f"PR#{pr}",
        "rationale": intent,
        "decided_by": decided_by,
        "evidence": [f"pr:{pr}"],
    }


def iter_pr_intent_artifacts(data: dict):
    """Yield ``(pr_number, title, intent)`` for every PR entry that carries a
    non-empty intent — the backfill source.

    PRs are recorded under milestones (``core.pr_add`` → ``find_target_milestone``),
    so we walk ``data['milestones'][*]['entries']`` for ``type == 'pr'``. Pure read;
    the caller decides which are already on the arm (dedup) and posts the rest."""
    for ms in (data.get("milestones") or []):
        for entry in (ms.get("entries") or []):
            if entry.get("type") != "pr":
                continue
            meta = entry.get("meta") or {}
            intent = (meta.get("intent") or "").strip()
            if not intent:
                continue
            yield (meta.get("pr_number"), entry.get("description") or "", intent)


def covered_pr_numbers(existing_decisions) -> set:
    """The set of PR numbers already on the arm as ``pr-intent`` decisions,
    read from each decision's ``evidence`` (``pr:<n>``). Used by backfill to skip
    PRs already derived (idempotency), so re-running ``derive`` never duplicates."""
    covered: set = set()
    for d in (existing_decisions or []):
        if (d.get("kind") or "") != DERIVED_PR_INTENT_KIND:
            continue
        for ev in (d.get("evidence") or []):
            if isinstance(ev, str) and ev.startswith("pr:"):
                covered.add(ev[len("pr:"):])
    return covered


# ── 本文から作業対象 (target) を解決する (ms-166 e-6603) ──────────────────────
#
# log-backstop (= commit 時に AI が自己申告する判断記録) は実データで 16/16 すべて
# ``related.target_id`` が空だった。判断記録が「どの対象の話か」を持たないと、
# session-start や session-end の「この対象の判断」という引き方 (``decision list
# --target``) に一件も載らず、記録はあるのに辿れない。
#
# 本文 (``--what`` / ``--rationale``) には実際には対象 id が書かれていることが多い
# (例「opp-3 の成約を…」「ms-166 の掃討で…」)。それを機械で拾って target に解決する。
#
# 曖昧なときは **推測しない**: 複数の異なる対象 id が出てきたら空を返す。1 件に絞れた
# ときだけ解決する。間違った対象に判断を帰属させるのは、帰属が無いより悪い (監査で
# 「この対象はこう判断された」と誤読される)。
#
# 対象 prefix は :func:`work_model.known_target_prefixes` から引く (= ハードコードしない)。
# 新しい target クラスが台帳に載った瞬間にこの解決も効くようにするため。``e-`` (タスク /
# エントリ) は target prefix ではないので拾われない。
_TARGET_REF_RE = None


def _target_ref_pattern():
    """本文中の対象 id を拾う正規表現 (遅延生成 + キャッシュ)。

    ``work_model.known_target_prefixes()`` から組むので、prefix 表に新クラスが増えれば
    自動で対象になる。``op-`` が ``opp-`` の接頭辞だが、リテラルに ``-`` を含むので
    ``opp-3`` が ``op-`` として誤match することはない (長い方を先に並べて明示的に優先)。
    """
    global _TARGET_REF_RE
    if _TARGET_REF_RE is None:
        import re
        import work_model as _wm
        prefixes = sorted(_wm.known_target_prefixes(), key=len, reverse=True)
        alt = "|".join(re.escape(p) for p in prefixes)
        # 対象 id = prefix + 英数字 1 文字以上。直前が英数字 / ハイフンなら拾わない
        # (= 別語の一部を切り出さない)。
        _TARGET_REF_RE = re.compile(r"(?<![0-9A-Za-z-])(" + alt + r")([0-9A-Za-z]+)")
    return _TARGET_REF_RE


def target_ids_in_text(*texts) -> list:
    """``texts`` に現れる対象 id を重複なし・出現順で返す (純関数)。"""
    found = []
    pat = _target_ref_pattern()
    for text in texts:
        for m in pat.finditer(str(text or "")):
            tid = m.group(1) + m.group(2)
            if tid not in found:
                found.append(tid)
    return found


def resolve_target_from_text(*texts) -> str:
    """本文から対象 id を 1 件に解決する。曖昧 (= 0 件 or 2 件以上) なら ``""``。

    「1 件に絞れたときだけ解決する」が肝。複数の対象に触れた判断を片方に帰属させると、
    監査で「この対象はこう判断された」と誤読される。空で返して、呼び出し側が明示指定を
    促せるようにする (= 黙って一方に寄せない)。
    """
    found = target_ids_in_text(*texts)
    return found[0] if len(found) == 1 else ""
