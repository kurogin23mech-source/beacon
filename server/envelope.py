"""Bus message envelope (ms-54 / e-1155 — Phase 1).

Implements the AI-to-AI authorization envelope from CORE doc
``1UGomhHqCQo0iYSRtCdB``. Every bus event MAY carry an ``envelope`` field
that declares **who authorized this specific action** (= capability-based
trust). The server signs the envelope with HMAC-SHA256 (the public-key
asymmetric variant is a Phase 2 concern; HMAC is enough as long as the
issuer and verifier are the same server process, which is true here).

The 4-tier model:

  * **T1** — human explicit signature. ``actions_authorized`` is auto-permitted.
  * **T2** — Operation scope envelope. ``actions_authorized`` enumerates
    scope-bound actions; non-listed actions degrade to ``propose-to-ai``.
  * **T3** — reply to T1/T2. Action requests always degrade to ``propose-to-ai``.
  * **T5** — AI autonomous send. Info disclosure forbidden (short-ping shape
    only); actions forbidden.

Phase 1 scope (this module):

  * envelope dataclass + canonical serialization
  * HMAC sign / verify with project-scoped server secret
  * 9-step receive-time verify (signature → tier consistency → project
    → time window → nonce → reply chain → chain_depth → action vs. tier
    → data class vs. tier)
  * automatic T5 degradation on verify failure

Phase 2 (NOT implemented — fields reserved):

  * ``tokens`` / ``refill_policy`` — schema slots reserved, values always None.
  * asymmetric public-key signatures
  * cross-issuer trust (only server self-signed envelopes for now).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Tier definitions (mirror CORE doc § "4 Tier 定義")
# ---------------------------------------------------------------------------

TIER_T1 = "T1"  # human explicit signature
TIER_T2 = "T2"  # Operation scope envelope
TIER_T3 = "T3"  # reply chain to T1/T2
TIER_T5 = "T5"  # AI autonomous send (info-forbidden, action-forbidden)

VALID_TIERS = {TIER_T1, TIER_T2, TIER_T3, TIER_T5}

# Permission matrix flags. Both are independent (orthogonal) axes per CORE doc.
TIER_ACTION_PERMISSION = {
    TIER_T1: "auto",         # actions_authorized run without further consent
    TIER_T2: "scope-auto",   # in-scope actions auto, out-of-scope → propose
    TIER_T3: "propose-only", # any action → propose-to-ai (never auto)
    TIER_T5: "forbidden",    # action requests rejected outright
}

TIER_INFO_DISCLOSURE = {
    TIER_T1: "free",
    TIER_T2: "scope-bound",  # only data_class declared in scope contract
    TIER_T3: "inherit",       # match parent message tier
    TIER_T5: "ping-only",     # short-ping schema, no free text
}

# chain_depth limits per use case (CORE doc § "tier 移行のルール")
CHAIN_DEPTH_LIMIT_DEFAULT = 5         # user ↔ AI DM (T1 / T3) default
CHAIN_DEPTH_LIMIT_OPERATION = 50      # Operation notify chain (T2 reply)
CHAIN_DEPTH_LIMIT_CROSS_PROJECT = 3   # cross-project T1

# High-risk actions: T1-only AND must be explicitly enumerated in
# actions_authorized. The agent layer has no codepath to call these from
# bus-origin payload (defense in depth — CORE doc § "高リスク action の
# server-side gate").
HIGH_RISK_ACTIONS = frozenset({
    "deploy",
    "project.delete",
    "project.archive",
    "milestone.delete",
    "member.bulk_change",
    "member.remove",
})

# Short-ping schema (T5 disclosure cap). The payload must be a dict whose
# keys are a subset of this allowlist, and whose values are all primitive
# (no free text strings — only enum-like short tokens or numbers/bools).
T5_PING_KEYS = frozenset({"ping", "ack", "status", "kind", "ts"})
T5_PING_VALUE_MAX_LEN = 32  # tokens like "ok"/"busy"/"awake" — not free prose

# Nonce TTL (cleanup horizon). The verify step requires nonce uniqueness
# within this window; older nonces are GC'd. Generous bound — replay
# protection only needs the window to exceed practical clock skew + envelope
# expires_at, which is ~1h.
NONCE_TTL_SECONDS = 3600

# Backward-compat: bus events without an envelope are treated as T5-equivalent
# (no auto-execute, no info disclosure beyond ping). This keeps e-1136
# dogfood running without instant breakage while the senders adopt envelopes.
LEGACY_NO_ENVELOPE_TIER = TIER_T5


# ---------------------------------------------------------------------------
# Envelope schema
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Envelope:
    """Bus message envelope v1 (Phase 1).

    Field order matches the CORE doc schema for ease of cross-referencing.
    Mutable defaults are avoided per dataclass best practice.
    """
    tier: str
    issuer: str
    scope: Optional[str]  # T2-only; must be None for T1/T3/T5
    actions_authorized: list[str]
    data_class: str
    issued_at: str
    expires_at: str
    project_id: str
    nonce: str
    conversation_id: str
    in_reply_to: Optional[str]
    chain_depth: int
    # Phase 2 reservation slots — always None in Phase 1.
    tokens: Optional[dict] = None
    refill_policy: Optional[dict] = None
    signature: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Envelope":
        """Build an Envelope from a raw dict (e.g. JSON payload).

        Unknown extra keys are dropped (forward-compat with Phase 2 fields).
        Missing required keys raise KeyError so the verifier can map that to
        a structured fail-and-degrade rather than crashing later.
        """
        kwargs = {}
        for f in dataclasses.fields(cls):
            if f.name in data:
                kwargs[f.name] = data[f.name]
            elif f.default is dataclasses.MISSING:
                # Required field absent.
                raise KeyError(f.name)
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Canonical serialization (signature-stable byte form)
# ---------------------------------------------------------------------------

# Fields excluded from the canonical signed bytes. ``signature`` is excluded
# because it's the output, not the input. Phase 2 reserved fields are
# included as ``null`` so a Phase 2 server that starts filling them in will
# automatically invalidate Phase 1 signatures (= correct behavior).
_SIGNATURE_EXCLUDED = {"signature"}


def canonical_bytes(envelope: dict) -> bytes:
    """Return the canonical byte form of ``envelope`` for signature input.

    Canonicalization rules:
      * fields are sorted alphabetically by key
      * ``signature`` is excluded (it's the output)
      * JSON output uses ``sort_keys`` + no whitespace + ensure_ascii
      * lists preserve order (action ordering is semantically meaningful)
    """
    canon = {k: v for k, v in envelope.items() if k not in _SIGNATURE_EXCLUDED}
    return json.dumps(canon, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


# Dev fallback for the envelope signing secret. Visible in the public repo, so
# *production* must never run with this value — production startup checks
# ``is_using_dev_fallback()`` and refuses to boot when the env var is unset
# (see ``server/app.py`` startup handler / e-1291).
_DEV_FALLBACK_SECRET = "dev-envelope-secret-CHANGE-ME"


def _server_secret() -> bytes:
    """Return the server HMAC secret for envelope signing.

    Reads ``BEACON_ENVELOPE_SECRET`` env. Falls back to a dev placeholder so
    the test suite can run without configuration; the dev placeholder is
    intentionally distinct from ``BEACON_CLI_TOKEN_SECRET`` so an attacker
    who recovers one secret can't forge the other.
    """
    return os.environ.get(
        "BEACON_ENVELOPE_SECRET", _DEV_FALLBACK_SECRET
    ).encode("utf-8")


def is_using_dev_fallback() -> bool:
    """Return True iff envelope signing would use the hard-coded dev secret.

    The dev fallback is fine for unit tests and local dev (``BEACON_API_AUTH=0``
    posture), but in production it would let anyone with read access to the
    GitHub repo forge T1 envelopes — i.e. authorize high-risk actions on the
    bus. Production startup checks this and refuses to boot.

    "Using the dev fallback" means ``BEACON_ENVELOPE_SECRET`` is **unset or
    empty** in the process env (in which case ``_server_secret()`` returns
    the hard-coded placeholder). An explicit set of the env var to the same
    literal placeholder string is still treated as a misconfiguration —
    forgetting to rotate from the public placeholder is exactly the failure
    mode this guard exists to catch.
    """
    configured = os.environ.get("BEACON_ENVELOPE_SECRET", "")
    if not configured:
        return True
    return configured == _DEV_FALLBACK_SECRET


def sign(envelope: dict) -> str:
    """Compute HMAC-SHA256 signature of ``envelope`` (base64-encoded).

    The caller is expected to embed this back into the envelope's
    ``signature`` field. Canonicalization (`canonical_bytes`) ensures field
    order doesn't change the signature.
    """
    mac = hmac.new(_server_secret(), canonical_bytes(envelope),
                   hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def verify_signature(envelope: dict) -> bool:
    """Return True iff ``envelope.signature`` matches the canonical HMAC.

    Constant-time compare prevents timing leaks.
    """
    sig = envelope.get("signature", "")
    if not sig:
        return False
    expected = sign(envelope)
    return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# Issuance (server endpoints call into these)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _plus_seconds_iso(seconds: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def issue_envelope(
    *,
    tier: str,
    issuer: str,
    project_id: str,
    actions_authorized: list[str],
    data_class: str = "free",
    scope: Optional[str] = None,
    conversation_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    chain_depth: int = 0,
    ttl_seconds: int = 3600,
) -> dict:
    """Issue a server-signed envelope.

    Caller responsibilities:
      * pick ``tier`` based on origin (T1 = explicit user signature in
        session, T2 = Operation activation, T3 = reply, T5 = AI self-send)
      * pass scope for T2 (None for others)
      * enumerate actions_authorized — **wildcards / regex / natural language
        forbidden** per CORE doc § "scope 自然言語の曖昧性"

    The signature is computed last over the canonical form.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    if not isinstance(chain_depth, int) or isinstance(chain_depth, bool) \
            or chain_depth < 0:
        # Tighten the invariant at issuance so the server itself can never
        # mint an envelope with a malformed chain_depth. The verify path
        # enforces the upper bound (use-case limit); the lower bound is 0.
        raise ValueError("chain_depth must be >= 0")
    if tier == TIER_T1 and scope is not None:
        raise ValueError("T1 envelope must have scope=None")
    if tier == TIER_T2 and not scope:
        raise ValueError("T2 envelope requires non-empty scope")
    if tier in (TIER_T3, TIER_T5) and scope is not None:
        raise ValueError(f"{tier} envelope must have scope=None")
    # Enforce enumeration discipline: every action must be a non-empty
    # string with no wildcard characters. Empty list is allowed (e.g. T5
    # ping-only).
    for action in actions_authorized:
        if not isinstance(action, str) or not action:
            raise ValueError(f"action must be a non-empty string: {action!r}")
        if "*" in action or "?" in action or "/" in action[:1]:
            raise ValueError(f"action wildcards forbidden (enumerate): {action!r}")

    issued_at = _now_iso()
    expires_at = _plus_seconds_iso(ttl_seconds)
    nonce = secrets.token_urlsafe(16)
    convo = conversation_id or secrets.token_urlsafe(8)

    body: dict[str, Any] = {
        "tier": tier,
        "issuer": issuer,
        "scope": scope,
        "actions_authorized": list(actions_authorized),
        "data_class": data_class,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "project_id": project_id,
        "nonce": nonce,
        "conversation_id": convo,
        "in_reply_to": in_reply_to,
        "chain_depth": chain_depth,
        "tokens": None,        # Phase 2 reservation
        "refill_policy": None, # Phase 2 reservation
    }
    body["signature"] = sign(body)
    return body


# ---------------------------------------------------------------------------
# Receive-time verify (9-step pipeline per CORE doc)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class VerifyResult:
    """Result of running an envelope through the 9-step verify pipeline.

    ``passed`` is True only if every applicable step succeeds. ``steps``
    records each step's outcome ("ok" / failure reason) so the audit log
    can answer "which check failed exactly?" — this is what makes audit
    logs forensically useful (CORE doc § "監査ログとの統合").

    On failure: caller degrades to T5 (info forbidden + action forbidden)
    and writes the audit record. If T5 itself is forbidden for the payload
    (e.g. it contains an action request or non-ping disclosure), the
    receive is rejected entirely.
    """
    passed: bool
    effective_tier: str          # original tier if passed, T5 if degraded
    steps: dict[str, str]
    rejection_reason: Optional[str] = None

    def to_audit_dict(self) -> dict:
        return {
            "passed": self.passed,
            "effective_tier": self.effective_tier,
            "steps": dict(self.steps),
            "rejection_reason": self.rejection_reason,
        }


def _tier_internal_consistent(envelope: dict) -> Optional[str]:
    """Return None if consistent, else failure reason string.

    Rules from CORE doc:
      * T1: scope must be None
      * T2: scope required (non-empty)
      * T3/T5: scope must be None
      * Phase 1: tokens/refill_policy must be None (reserved fields)
    """
    tier = envelope.get("tier")
    scope = envelope.get("scope")
    if tier == TIER_T1 and scope is not None:
        return "T1 must have scope=None"
    if tier == TIER_T2 and not scope:
        return "T2 requires non-empty scope"
    if tier in (TIER_T3, TIER_T5) and scope is not None:
        return f"{tier} must have scope=None"
    if envelope.get("tokens") is not None:
        return "tokens field reserved for Phase 2 (must be null)"
    if envelope.get("refill_policy") is not None:
        return "refill_policy field reserved for Phase 2 (must be null)"
    return None


def _within_time_window(envelope: dict, now: Optional[datetime.datetime] = None
                        ) -> Optional[str]:
    """Return None if ``issued_at <= now < expires_at``, else reason."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        issued = _parse_iso(envelope.get("issued_at", ""))
        expires = _parse_iso(envelope.get("expires_at", ""))
    except ValueError as e:
        return f"unparseable timestamp: {e}"
    if not issued or not expires:
        return "missing issued_at/expires_at"
    if issued > now:
        return "issued_at is in the future"
    if expires <= now:
        return "envelope expired"
    return None


def _parse_iso(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None
    # Accept both "Z" suffix (Beacon convention) and standard +00:00.
    cleaned = value.replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(cleaned)


def _chain_depth_limit(envelope: dict, *, cross_project: bool = False) -> int:
    """Return the chain_depth limit applicable to this envelope.

    Choosing the limit by tier+context (vs. a single global cap) lets long
    Operation chains coexist with short DM chains in the same project.
    """
    if cross_project:
        return CHAIN_DEPTH_LIMIT_CROSS_PROJECT
    tier = envelope.get("tier")
    if tier == TIER_T2:
        return CHAIN_DEPTH_LIMIT_OPERATION
    return CHAIN_DEPTH_LIMIT_DEFAULT


def _action_permitted_by_tier(envelope: dict, action: Optional[str]) -> Optional[str]:
    """Return None if ``action`` is permitted under the envelope's tier+scope.

    "permitted" here means "the receive-time verify lets the request
    through". Whether the action then runs auto vs. propose-to-ai is a
    downstream concern handled by ``decide_delivery``.

    Returns failure reason if forbidden outright.
    """
    if action is None:
        return None  # no action requested → tier action permission n/a
    tier = envelope.get("tier")
    if tier == TIER_T5:
        return "T5 cannot request actions"
    # T1/T2 must enumerate the action.
    authorized = envelope.get("actions_authorized") or []
    if action not in authorized and tier in (TIER_T1, TIER_T2):
        return f"action {action!r} not in actions_authorized"
    # T3 actions are always allowed at verify (action will degrade to
    # propose-to-ai downstream — that's a delivery concern not a verify
    # one).
    # High-risk actions: T1 only (defense in depth).
    if action in HIGH_RISK_ACTIONS and tier != TIER_T1:
        return f"high-risk action {action!r} requires T1"
    return None


def _payload_disclosure_ok(envelope: dict, payload: dict) -> Optional[str]:
    """Return None if payload disclosure matches tier permission.

    T5 cap: payload must conform to the short-ping schema (no free text).
    T1/T2/T3: no schema cap at this step (data_class field is advisory).
    """
    if envelope.get("tier") != TIER_T5:
        return None
    if not isinstance(payload, dict):
        return "T5 payload must be a dict"
    return validate_t5_payload(payload)


def validate_t5_payload(payload: dict) -> Optional[str]:
    """Verify ``payload`` fits the T5 short-ping schema.

    The short-ping schema is the only payload shape an AI-autonomous (T5)
    message may carry. This is the structural realization of CORE doc's
    "T5 → 情報提供禁止 (= 短い ping schema のみ、自由テキスト不可)".

    Returns None on pass, else a string explaining the violation.
    """
    extra = set(payload.keys()) - T5_PING_KEYS
    if extra:
        return f"T5 payload keys not in allowlist: {sorted(extra)}"
    for key, value in payload.items():
        if isinstance(value, str):
            if len(value) > T5_PING_VALUE_MAX_LEN:
                return f"T5 payload field {key!r} exceeds {T5_PING_VALUE_MAX_LEN} chars"
        elif isinstance(value, (int, float, bool)) or value is None:
            continue
        else:
            return f"T5 payload field {key!r} must be primitive"
    return None


def verify(
    envelope: Optional[dict],
    *,
    project_id: str,
    payload: dict,
    requested_action: Optional[str],
    nonce_store: "NonceStore",
    parent_lookup: "ParentLookup",
    sender_session_id: str,
    cross_project: bool = False,
    now: Optional[datetime.datetime] = None,
) -> VerifyResult:
    """Run the 9-step receive verify pipeline from CORE doc § "受信時の verify ロジック".

    Steps (any failure → T5 degrade + audit; T5 also forbidden for payload
    that requires action / info disclosure → reject):

      1. signature verify
      2. tier internal consistency
      3. project_id match
      4. time window
      5. nonce unique
      6. in_reply_to: parent exists + sender was recipient of parent
      7. chain_depth within limit
      8. action tier permission (if action requested)
      9. payload data_class vs. disclosure permission

    ``cross_project``: when True the chain_depth limit shrinks to the
    cross-project cap. Callers determine cross-project by comparing
    envelope.project_id with the receiving project.
    """
    steps: dict[str, str] = {}

    # Backward compat: missing envelope → degrade to T5-equivalent, validate
    # T5 schema, and return. e-1136 dogfood is mid-migration so we cannot
    # block on missing envelope (the dogfood senders haven't adopted envelopes
    # yet). Instead we treat the message as if it carried a T5 envelope:
    #
    #   * action requested → hard reject (T5 can't carry actions, period)
    #   * free-text payload (not ping-shape) → soft degrade: the event
    #     flows so existing readers still see it, but ``passed=False`` so
    #     the caller caps delivery at notify-user-only and the audit log
    #     marks it. The CORE doc disclosure rule ("T5 = 情報提供禁止 / 短い
    #     ping schema only") is enforced by the delivery cap, not by
    #     dropping the event.
    #   * ping-shape payload → pass.
    if envelope is None:
        steps["envelope_present"] = "missing → legacy T5"
        if requested_action is not None:
            return VerifyResult(
                passed=False,
                effective_tier=TIER_T5,
                steps=steps,
                rejection_reason=("legacy/missing envelope cannot request "
                                  "actions (T5-equivalent)"),
            )
        t5_check = validate_t5_payload(payload if isinstance(payload, dict) else {})
        if t5_check:
            # Soft degrade. The caller (app.py) treats T5 + payload that
            # doesn't conform to ping-shape as "deliver as notify-user-only
            # rather than reject". This keeps legacy DM traffic flowing
            # during the rollout — see e-1136 / CORE doc § backward compat.
            steps["legacy_t5_payload"] = t5_check
            return VerifyResult(
                passed=False,
                effective_tier=TIER_T5,
                steps=steps,
                rejection_reason=None,
            )
        steps["legacy_t5_payload"] = "ok"
        return VerifyResult(passed=True, effective_tier=TIER_T5, steps=steps)

    # Step 1: signature
    if not verify_signature(envelope):
        steps["signature"] = "fail"
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              "signature verify failed")
    steps["signature"] = "ok"

    # Step 2: tier internal consistency
    tier = envelope.get("tier")
    if tier not in VALID_TIERS:
        steps["tier"] = f"unknown: {tier!r}"
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              f"unknown tier {tier!r}")
    consistency = _tier_internal_consistent(envelope)
    if consistency:
        steps["tier_consistency"] = consistency
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              consistency)
    steps["tier_consistency"] = "ok"

    # Step 3: project_id match
    if envelope.get("project_id") != project_id:
        steps["project_match"] = (
            f"envelope.project_id={envelope.get('project_id')!r} "
            f"!= receiving project_id={project_id!r}")
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              "project_id mismatch")
    steps["project_match"] = "ok"

    # Step 4: time window
    time_fail = _within_time_window(envelope, now=now)
    if time_fail:
        steps["time_window"] = time_fail
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              time_fail)
    steps["time_window"] = "ok"

    # Step 5: nonce uniqueness (replay protection)
    nonce = envelope.get("nonce")
    if not nonce or not nonce_store.check_and_record(project_id, nonce):
        steps["nonce"] = "replay or missing"
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              "nonce replay or missing")
    steps["nonce"] = "ok"

    # Step 6: in_reply_to chain integrity
    parent_id = envelope.get("in_reply_to")
    if parent_id:
        parent = parent_lookup.find_parent(project_id, parent_id)
        if not parent:
            steps["in_reply_to"] = "parent not found"
            return _degrade_to_t5(envelope, payload, requested_action, steps,
                                  "in_reply_to parent missing")
        # sender of this message must have been the recipient of the parent.
        parent_recipient = (parent.get("payload") or {}).get("recipient_session_id")
        if parent_recipient and sender_session_id and parent_recipient != sender_session_id:
            steps["in_reply_to"] = (
                f"sender={sender_session_id!r} was not parent recipient="
                f"{parent_recipient!r}")
            return _degrade_to_t5(envelope, payload, requested_action, steps,
                                  "in_reply_to sender/recipient mismatch")
        steps["in_reply_to"] = "ok"
    else:
        steps["in_reply_to"] = "n/a"

    # Step 7: chain_depth within use-case limit
    # Invariant: 0 <= depth <= limit. Both ends matter — a negative depth
    # would silently pass a `depth > limit` check and become risky in Phase 2
    # (token grant) where chain_depth interacts with token-count accounting.
    depth = envelope.get("chain_depth", 0)
    limit = _chain_depth_limit(envelope, cross_project=cross_project)
    if (not isinstance(depth, int) or isinstance(depth, bool)
            or not (0 <= depth <= limit)):
        steps["chain_depth"] = f"depth={depth} out of [0, {limit}]"
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              f"chain_depth {depth} out of [0, {limit}]")
    steps["chain_depth"] = f"ok ({depth}/{limit})"

    # Step 8: action vs. tier permission matrix
    action_fail = _action_permitted_by_tier(envelope, requested_action)
    if action_fail:
        steps["action_permission"] = action_fail
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              action_fail)
    steps["action_permission"] = "ok"

    # Step 9: payload disclosure vs. tier
    disclosure_fail = _payload_disclosure_ok(envelope, payload)
    if disclosure_fail:
        steps["disclosure"] = disclosure_fail
        return _degrade_to_t5(envelope, payload, requested_action, steps,
                              disclosure_fail)
    steps["disclosure"] = "ok"

    return VerifyResult(passed=True, effective_tier=tier, steps=steps)


def _degrade_to_t5(envelope: dict, payload: dict,
                   requested_action: Optional[str], steps: dict[str, str],
                   reason: str) -> VerifyResult:
    """Apply T5 degradation rule.

    Per CORE doc: any verify failure → degrade to T5. Outcomes:

      * action requested → **hard reject** (T5 can't carry actions).
      * payload that doesn't fit T5 short-ping schema → **hard reject**
        only when the envelope was *present* (a malformed signed envelope
        is structurally suspicious, so we don't paper over it). For the
        legacy missing-envelope path the caller handles soft-degrade
        upstream — this helper is only reached when an envelope exists.
      * ping-shape payload with no action → **soft degrade**: event still
        flows but caller caps delivery (e.g. notify-user-only) and the
        audit trail records ``rejection_reason=reason``-style breadcrumb
        via the ``steps`` dict, leaving ``rejection_reason`` itself None so
        the upstream HTTP layer does not 403.
    """
    if requested_action is not None:
        return VerifyResult(
            passed=False,
            effective_tier=TIER_T5,
            steps=steps,
            rejection_reason=(f"{reason} (action {requested_action!r} "
                              "forbidden under T5 degrade)"),
        )
    t5_check = validate_t5_payload(payload if isinstance(payload, dict) else {})
    if t5_check:
        return VerifyResult(
            passed=False,
            effective_tier=TIER_T5,
            steps=steps,
            rejection_reason=f"{reason} (T5 payload check: {t5_check})",
        )
    # Soft degrade: ping-shape payload, no action requested. The audit log
    # still gets the failure breadcrumb via ``steps``; ``rejection_reason``
    # is None so the HTTP layer doesn't 403 (CORE doc backward compat: keep
    # the bus flowing, downgrade delivery via decide_delivery).
    steps["soft_degrade_reason"] = reason
    return VerifyResult(
        passed=False,
        effective_tier=TIER_T5,
        steps=steps,
        rejection_reason=None,
    )


# ---------------------------------------------------------------------------
# Delivery decision (action → auto-execute vs. propose-to-ai vs. forbidden)
# ---------------------------------------------------------------------------

def decide_delivery(
    *,
    envelope: Optional[dict],
    effective_tier: str,
    requested_action: Optional[str],
    requested_delivery: str,
    t5_payload_conforms: bool = True,
) -> str:
    """Determine the effective delivery mode after envelope-aware downgrade.

    Rules (from CORE doc § "4 Tier 定義" combined with §"delivery target"):

      * effective_tier == T5: action requests forbidden (caller should have
        already rejected); otherwise notify-user-only is the cap when the
        payload is *not* in T5 short-ping shape (= free-text disclosure
        risk), else respect the requested_delivery up to the no-auto cap.
      * effective_tier == T3: any action → propose-to-ai (never auto).
      * effective_tier == T2: in-scope action (in actions_authorized) →
        auto-execute; out-of-scope → propose-to-ai.
      * effective_tier == T1: actions in actions_authorized → auto-execute;
        otherwise the requested_delivery prevails.
      * Missing envelope (legacy) is handled via effective_tier=T5 +
        t5_payload_conforms: ping-shape → propose-to-ai cap, free-text
        → notify-user-only cap (because the AI seeing free-text from an
        unsigned source is exactly the prompt-injection risk we are
        defending against).

    ``t5_payload_conforms``: pass True iff the payload satisfies
    :func:`validate_t5_payload` (= ping-shape). Callers compute this once
    during verify; we pass it through so the decision can downgrade
    free-text payloads to notify-user-only without re-running validation.

    The function is monotonic — it can only *lower* trust, never raise it.
    """
    if effective_tier == TIER_T5:
        # Action requests are forbidden under T5; caller should hard-reject
        # before reaching this branch. As a defense-in-depth fallback, cap
        # at notify-user-only when an action is still attached.
        if requested_action is not None:
            return "notify-user-only"
        # Auto-execute never allowed under T5.
        if requested_delivery == "auto-execute":
            return "notify-user-only" if not t5_payload_conforms else "propose-to-ai"
        # Free-text payload from an unsigned/legacy sender: prevent the AI
        # from auto-injecting it (= notify-user-only). Ping-shape payloads
        # are safe to surface as propose-to-ai (the schema bounds the
        # disclosure surface).
        if not t5_payload_conforms:
            return "notify-user-only"
        return requested_delivery

    if effective_tier == TIER_T3:
        if requested_action is not None and requested_delivery == "auto-execute":
            return "propose-to-ai"
        return requested_delivery

    if effective_tier == TIER_T2:
        authorized = set(envelope.get("actions_authorized") or [])
        if requested_action is not None and requested_action not in authorized:
            # Out-of-scope: downgrade to propose-to-ai.
            if requested_delivery == "auto-execute":
                return "propose-to-ai"
        return requested_delivery

    # T1: in actions_authorized → respect requested_delivery; otherwise
    # caller's request stands (T1 issuer endorsed it).
    return requested_delivery


# ---------------------------------------------------------------------------
# Nonce store + parent lookup interfaces (server wires these to Firestore)
# ---------------------------------------------------------------------------

class NonceStore:
    """Replay-protection nonce store.

    A nonce is one-time per (project_id, nonce) pair. Implementations:

      * Firestore-backed (production)
      * in-memory (tests, dev)

    The interface is ``check_and_record(project_id, nonce) -> bool``: returns
    True if the nonce is fresh AND records it; False if already seen. The
    atomicity guarantee is implementation-specific (Firestore transaction in
    the cloud path).
    """

    def check_and_record(self, project_id: str, nonce: str) -> bool:
        raise NotImplementedError


class InMemoryNonceStore(NonceStore):
    """Process-local nonce store. Suitable for unit tests and single-replica
    dev servers. Not safe across replicas; production uses the Firestore
    variant in firestore_client.py."""

    def __init__(self) -> None:
        # nonce → (project_id, expiry_unix)
        self._seen: dict[str, tuple[str, float]] = {}

    def check_and_record(self, project_id: str, nonce: str) -> bool:
        import time
        now = time.time()
        # GC expired entries opportunistically (cheap for a small dict).
        expired = [n for n, (_, exp) in self._seen.items() if exp < now]
        for n in expired:
            self._seen.pop(n, None)
        key = nonce
        if key in self._seen:
            return False
        self._seen[key] = (project_id, now + NONCE_TTL_SECONDS)
        return True


class ParentLookup:
    """Resolve in_reply_to → parent bus event.

    Returns the parent event dict (with ``payload``, ``sender_session_id``,
    etc.) or None if not found. Production implementations query the
    ``bus_events`` subcollection; test implementations use an in-memory list.
    """

    def find_parent(self, project_id: str, event_id: str) -> Optional[dict]:
        raise NotImplementedError


class FunctionParentLookup(ParentLookup):
    """Adapter wrapping a callable ``(project_id, event_id) -> dict | None``."""

    def __init__(self, fn) -> None:
        self._fn = fn

    def find_parent(self, project_id: str, event_id: str) -> Optional[dict]:
        return self._fn(project_id, event_id)
