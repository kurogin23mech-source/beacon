"""Report / Request primitives — the two client-facing write primitives the
ms-114 SPEC (設計方針 5/6) folds every client write verb into.

Why this exists (SPEC 背景): today a CLI agent is both the *actor* (it does the
work) and the *author* (it writes the record of what it did) — it grades its own
homework, which is the root of "機構を書いた≠達成した" (ms-109 fable review). The
final shape separates the two: the client becomes a *reporter*, the Beacon server
becomes the *author*.

Two primitives:

  - ``report`` (事後記録): an after-the-fact record of "what happened / what was
    decided". The client emits a report carrying enough context; the SERVER
    authors the canonical mutations from it (report → fan-out to log entry +
    progress derivation + task resolve, exactly what ``beacon log`` already does
    for a commit). Because the author is no longer the actor, an executor can no
    longer author its own completion — the self-grading loop is closed
    structurally.
  - ``request`` (事前認可): a before-the-fact authorization ask. The client
    describes an action it wants to take; the server decides grant/deny and
    records the decision.

Scope of THIS module (e-3741): it *defines* the primitives, the report schema
(what a report must carry so the server can author faithfully), validation, and
a pure authoring-*dispatch* skeleton — a rule-registry INTERFACE. The concrete
authoring rules (commit → log entry etc.) land server-side in e-3742, not here.

Design contract (matches ``target_descriptor`` / ``work_base``): pure transforms
only — no I/O, no CLI wiring, tolerant reads, ``ValueError`` on malformed input.
An existing write verb can build a report and route it through ``apply_report``
without changing its external stdout contract (the expand step of the SPEC's
expand/contract migration, 方針7); wiring live verbs is a later task (e-3743).
"""

from __future__ import annotations

import json
from typing import Callable, Optional


# --- primitive modes -------------------------------------------------------

REPORT = "report"    # 事後記録 (after-the-fact)
REQUEST = "request"  # 事前認可 (before-the-fact)


# --- report schema ---------------------------------------------------------
# The envelope a report carries. ``kind`` selects the server authoring rule;
# ``payload`` holds the kind-specific facts that rule consumes (for a commit:
# hash / message / resolves). Everything else is provenance the ledger needs to
# stay replayable (data-immutability-principle). Kept as data so the schema is a
# first-class, queryable artifact rather than prose.

REPORT_SCHEMA = {
    "kind":       {"required": True,  "type": "string",
                   "desc": "報告の種別。サーバ authoring rule の選択キー (例 commit / task_done / meeting_ended)"},
    "actor":      {"required": True,  "type": "string",
                   "desc": "その事実を起こした/決めた主体 (session actor)。記録の author ではない"},
    "payload":    {"required": True,  "type": "object",
                   "desc": "種別固有の生事実。authoring rule が消費する (commit なら hash/message/resolves)"},
    "subject":    {"required": False, "type": "string",
                   "desc": "報告が関わる target ref (ms-id / task-id / opportunity-id 等)。空なら rule が解決 (beacon log の auto-pick と同義)"},
    "evidence":   {"required": False, "type": "array",
                   "desc": "裏取りの参照 (commit hash / URL 等)。append-only 監査の証跡"},
    "occurred_at":{"required": False, "type": "string",
                   "desc": "事実が起きた時刻 (ISO8601)。空なら author 時刻で補完"},
    "origin_verb":{"required": False, "type": "string",
                   "desc": "この報告を emit した CLI verb (監査/routing 用、例 task_done)"},
    "session_id": {"required": False, "type": "string",
                   "desc": "報告元セッション id (provenance)"},
    "source":     {"required": False, "type": "string",
                   "desc": "報告経路のラベル (human / auto-op 等、beacon log の meta.source と同義)"},
    "idempotency_key": {"required": False, "type": "string",
                   "desc": "重複 author を弾く鍵。空なら kind+subject+payload から導出"},
}

REQUEST_SCHEMA = {
    "action":     {"required": True,  "type": "string",
                   "desc": "認可を求めるアクション名 (例 pr_merge / operation_approve)"},
    "actor":      {"required": True,  "type": "string",
                   "desc": "認可を求める主体 (session actor)"},
    "subject":    {"required": False, "type": "string",
                   "desc": "アクションの対象 target ref"},
    "payload":    {"required": False, "type": "object",
                   "desc": "アクション固有の文脈"},
    "origin_verb":{"required": False, "type": "string", "desc": "emit した CLI verb"},
    "session_id": {"required": False, "type": "string", "desc": "要求元セッション id"},
    "requested_at":{"required": False, "type": "string", "desc": "要求時刻 (ISO8601)"},
}


def describe_report_schema() -> dict:
    """Return the report schema (field → {required, type, desc}). A deep-ish copy
    so a caller inspecting the schema can't mutate the module constant."""
    return {k: dict(v) for k, v in REPORT_SCHEMA.items()}


def describe_request_schema() -> dict:
    """Return the request schema (field → {required, type, desc})."""
    return {k: dict(v) for k, v in REQUEST_SCHEMA.items()}


# --- constructors ----------------------------------------------------------

def make_report(*, kind: str, actor: str, payload: Optional[dict] = None,
                subject: str = "", evidence: Optional[list] = None,
                occurred_at: str = "", origin_verb: str = "",
                session_id: str = "", source: str = "",
                idempotency_key: str = "") -> dict:
    """Build a report envelope. ``kind`` / ``actor`` are required and non-empty;
    ``payload`` defaults to an empty dict. Fields are stored verbatim — this is a
    pure builder, it does not author anything. Validate with ``validate_report``
    before handing to ``apply_report``."""
    if not (kind or "").strip():
        raise ValueError("report.kind must be a non-empty string")
    if not (actor or "").strip():
        raise ValueError("report.actor must be a non-empty string")
    # Every optional field is present-but-empty (never absent) so the envelope
    # has one stable shape — a consumer can read report["idempotency_key"] etc.
    # without a KeyError/.get() split (report_key already treats "" as "derive").
    return {
        "mode": REPORT,
        "kind": kind.strip(),
        "actor": actor.strip(),
        "payload": dict(payload or {}),
        "subject": subject or "",
        "evidence": list(evidence or []),
        "occurred_at": occurred_at or "",
        "origin_verb": origin_verb or "",
        "session_id": session_id or "",
        "source": source or "",
        "idempotency_key": idempotency_key or "",
    }


def make_request(*, action: str, actor: str, subject: str = "",
                 payload: Optional[dict] = None, origin_verb: str = "",
                 session_id: str = "", requested_at: str = "") -> dict:
    """Build a request envelope (before-the-fact authorization ask). ``action`` /
    ``actor`` required. Pure builder; the server decides grant/deny elsewhere."""
    if not (action or "").strip():
        raise ValueError("request.action must be a non-empty string")
    if not (actor or "").strip():
        raise ValueError("request.actor must be a non-empty string")
    return {
        "mode": REQUEST,
        "action": action.strip(),
        "actor": actor.strip(),
        "subject": subject or "",
        "payload": dict(payload or {}),
        "origin_verb": origin_verb or "",
        "session_id": session_id or "",
        "requested_at": requested_at or "",
    }


# --- validation ------------------------------------------------------------

def _validate_against(obj: dict, schema: dict, label: str) -> list:
    """Return a list of human-readable problem strings for ``obj`` against
    ``schema`` (empty = valid). Tolerant: unknown extra keys are allowed
    (forward-compat, append-only), only required/type are enforced. A field that
    is present but the wrong type reports a *type* error (distinct from an
    *absent* required field), so a caller can tell "you forgot X" from "X is
    malformed"."""
    problems: list = []
    if not isinstance(obj, dict):
        return [f"{label} は dict である必要があります"]
    for key, spec in schema.items():
        typ = spec.get("type")
        required = spec.get("required")
        present = key in obj and obj[key] is not None
        if not present:
            if required:
                problems.append(f"{label}.{key} は必須です ({spec.get('desc', '')})")
            continue
        if not _type_ok(obj[key], typ):
            problems.append(
                f"{label}.{key} は type={typ} である必要があります")
            continue
        # a required string present but blank counts as missing (not malformed)
        if required and typ == "string" and not obj[key].strip():
            problems.append(f"{label}.{key} は必須です ({spec.get('desc', '')})")
    return problems


def _type_ok(val, typ: str) -> bool:
    if typ == "string":
        return isinstance(val, str)
    if typ == "object":
        return isinstance(val, dict)
    if typ == "array":
        return isinstance(val, list)
    return True


def validate_report(report: dict) -> list:
    """Return problem strings for a report (empty list = valid)."""
    return _validate_against(report, REPORT_SCHEMA, "report")


def validate_request(request: dict) -> list:
    """Return problem strings for a request (empty list = valid)."""
    return _validate_against(request, REQUEST_SCHEMA, "request")


# --- idempotency -----------------------------------------------------------

def report_key(report: dict) -> str:
    """The dedup key for a report: the explicit ``idempotency_key`` when set,
    else a deterministic derivation from kind + subject + a signature over the
    WHOLE payload. Lets the server author a report exactly once even if the
    client re-emits it (network retry / re-run), mirroring ``beacon log``'s
    "Already logged: <hash>" dedup.

    The signature is canonical JSON (``sort_keys``) of the full payload, so two
    reports that differ only in a nested value (e.g. ``resolves: ["e-1"]`` vs
    ``["e-2"]``) get distinct keys — an earlier scalar-only signature silently
    collapsed those into one and dropped the second as a false duplicate. A
    payload value that is not JSON-serialisable falls back to its ``repr`` so the
    derivation never raises (``default=str``)."""
    if report.get("idempotency_key"):
        return str(report["idempotency_key"])
    kind = report.get("kind", "")
    subject = report.get("subject", "")
    payload = report.get("payload") or {}
    sig = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"{kind}|{subject}|{sig}"


# --- authoring dispatch skeleton (interface only; rules land in e-3742) -----

# An authoring rule is ``(project_data, report) -> result`` — it applies a
# report's facts to the project data (the parsed project.json the CLI/server
# already passes around) as canonical mutations and returns a result dict. The
# concrete rules (commit → log entry + progress + task resolve, etc.) are NOT
# defined here; this module only provides the registry + dispatcher so a report
# can be routed to the right author. e-3742 registers the first real rule by
# re-homing ``beacon log``'s internal logic behind ``kind="commit"``.
#
# There is deliberately NO module-level default registry: a mutable module global
# would let an import-time ``register_authoring_rule`` change an unrelated
# ``apply_report`` call's behaviour (remote action). Every caller owns its
# registry (build one with ``new_authoring_registry``) and passes it explicitly.

AuthoringRule = Callable[[dict, dict], dict]


def new_authoring_registry() -> dict:
    """Return a fresh, empty authoring-rule registry (``kind`` → rule). The
    server / a verb builds one, registers its rules into it, and threads it
    through ``apply_report`` — there is no shared global to collide on."""
    return {}


def register_authoring_rule(rules: dict, kind: str, fn: AuthoringRule, *,
                            replace: bool = False) -> None:
    """Register an authoring rule for a report ``kind`` into the caller-owned
    ``rules`` registry. Re-registering an existing ``kind`` raises unless
    ``replace=True`` — a silent overwrite would swap out a live authoring rule
    (every later report of that kind then produces a different mutation) with no
    signal at registration time, so the overwrite must be made explicit."""
    if not isinstance(rules, dict):
        raise ValueError("rules must be a registry dict (see new_authoring_registry)")
    if not (kind or "").strip():
        raise ValueError("authoring rule kind must be non-empty")
    if not callable(fn):
        raise ValueError("authoring rule must be callable")
    k = kind.strip()
    if k in rules and not replace:
        raise ValueError(
            f"authoring rule for kind '{k}' is already registered "
            f"(pass replace=True to intentionally swap it)")
    rules[k] = fn


def authoring_rule_for(rules: dict, kind: str) -> Optional[AuthoringRule]:
    """Return the authoring rule registered for ``kind`` in ``rules`` (or None)."""
    return rules.get((kind or "").strip())


def apply_report(project_data: dict, report: dict, *, rules: dict,
                 seen: dict) -> dict:
    """Route a report to its authoring rule — the server-side entry the client's
    ``report`` primitive lands on. Returns a result dict with a ``status``:

    - ``"invalid"``   — report failed schema validation (``problems`` listed);
    - ``"duplicate"`` — a report with the same ``report_key`` was already
      authored (recorded in ``seen``); no rule is run (idempotent replay);
    - ``"no_rule"``   — no authoring rule registered for ``kind`` in ``rules``
      (until e-3742 registers rules); the report is well-formed but unhandled;
    - ``"authored"``  — the rule ran; its return is under ``result``.

    ``rules`` and ``seen`` are BOTH required (no defaults): the exactly-once
    guarantee this module advertises is real only when a caller-owned dedup
    ledger is threaded through, so the dedup path cannot be silently skipped.
    ``seen`` is a dict (key → key), validated up front — passing a set would only
    fail on the post-mutation write, leaving a mutation applied but unrecorded
    (a retry would then double-author). Pure dispatch: validate → dedup → delegate
    the mutation to the rule; never does I/O.
    """
    if not isinstance(seen, dict):
        raise ValueError(
            "seen must be a dict (key→key dedup ledger); a set fails only after "
            "the mutation is applied. Build one with an empty {}.")
    problems = validate_report(report)
    if problems:
        return {"status": "invalid", "problems": problems}
    key = report_key(report)
    if key in seen:
        return {"status": "duplicate", "report_key": key}
    rule = authoring_rule_for(rules, report.get("kind", ""))
    if rule is None:
        return {"status": "no_rule", "kind": report.get("kind", ""), "report_key": key}
    result = rule(project_data, report)
    seen[key] = key
    return {"status": "authored", "report_key": key, "result": result}
