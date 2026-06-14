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

Why T4 is absent (ms-63 / e-1433):

  Earlier iterations of the tier model reserved T4 for "AI suggested,
  awaiting human ack" — a state between T2 (scope-auto) and T5 (autonomous
  send). During the e-1155 design we collapsed that into ``decide_delivery``
  outcomes ("propose-to-ai") because the "awaiting ack" is a *delivery*
  property, not an authorization tier — the same envelope can yield
  propose-to-ai under one receiver context and auto-execute under another.
  Keeping the slot empty rather than renumbering preserves backward-compat
  with any audit record that referenced T1/T2/T3/T5 by name, and the gap
  is a recurring reminder that delivery and tier are orthogonal axes.

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
# Disclosure contract (ms-63 / e-1443) — project disclosure_policy snapshot
# burned into the envelope at mint time.
#
# Why this lives on the envelope (= signed) rather than as advisory metadata:
#   The disclosure gate (e-1430) on the receiver side fires when the receiver
#   AI is about to emit text in *reply* to a bus message. The contract that
#   bounds what the receiver may say is decided by the *sender's* project
#   sensitivity at the moment they joined the conversation. An attacker who
#   could strip a separate ``disclosure_contract`` field post-signature would
#   bypass the gate; folding the snapshot into the signed canonical bytes
#   shuts that off — any tamper invalidates the HMAC.
#
# Why "snapshot" not "live lookup":
#   See SPEC § 設計方針 4 — dev/prod 不可分問題は project sensitivity 固定で
#   扱う (人間組織の NDA メタファー). The contract is what the project
#   *agreed to at join time*, not what it might be re-configured to mid-flight.
#
# Sensitivity values:
#   * "high" — project handles confidential context (PII, credentials,
#     business secrets). T5 (= AI-autonomous) replies are capped to the
#     T5_RESPONSE_SCHEMA_HIGH allowlist (= status pings only); no free-text
#     answers can leave the boundary, even from a well-meaning AI.
#   * "low"  — project is openly shared (OSS repos, public docs). T5 replies
#     can use the full short-ping schema; no extra cap beyond Phase 1
#     ping-shape rules.
#
# Future axes (reserved, NOT validated in this Phase):
#   * t5_response_mode — explicit override of the high/low default mapping.
#   * t5_free_text     — bool, opt-in to allow T5 free text on low projects.
#   * data_classes     — declared inventory of confidential context kinds.
# ---------------------------------------------------------------------------

SENSITIVITY_HIGH = "high"
SENSITIVITY_LOW = "low"
VALID_SENSITIVITIES = {SENSITIVITY_HIGH, SENSITIVITY_LOW}

# Default sensitivity for a project mint when none is configured. The SPEC
# § 設計方針 2 picks "high" so that "forgetting to set it" fails in the safe
# direction (= the AI clams up). The flip side — overly chatty defaults —
# would let the very first DM after a new project is created leak context.
DEFAULT_SENSITIVITY = SENSITIVITY_HIGH

# T5 response mode keywords. ``schema-only`` is the high-sensitivity cap.
# ``free`` is the low-sensitivity (= default Phase-1 behaviour, only bounded
# by the ping-shape rules already in validate_t5_payload).
T5_RESPONSE_MODE_SCHEMA_ONLY = "schema-only"
T5_RESPONSE_MODE_FREE = "free"
VALID_T5_RESPONSE_MODES = {T5_RESPONSE_MODE_SCHEMA_ONLY, T5_RESPONSE_MODE_FREE}

# Keys allowed in a T5 reply on a high-sensitivity project (= the "定型
# schema only" enforcement from SPEC § 設計方針 5 and acceptance criterion 5).
# This is intentionally narrower than T5_PING_KEYS — it excludes ``ts`` and
# ``kind`` (those are sender-emitted markers, not response shapes).
T5_RESPONSE_SCHEMA_HIGH_KEYS = frozenset({"busy", "available", "ack", "task_id"})


def default_disclosure_contract() -> dict:
    """Return the default disclosure_contract used when a project has not yet
    declared a disclosure_policy.

    Defaulting to ``sensitivity=high`` matches SPEC § 設計方針 2: the failure
    mode "forgot to configure → AI clams up" is preferred over "forgot to
    configure → AI leaks". A caller that wants the open-OSS posture must
    declare it explicitly via ``beacon init --sensitivity low`` (cmd_init).
    """
    return {
        "sensitivity": DEFAULT_SENSITIVITY,
        "t5_response_mode": T5_RESPONSE_MODE_SCHEMA_ONLY,
        "t5_free_text": False,
    }


def normalize_disclosure_contract(raw: Optional[dict]) -> dict:
    """Coerce a raw disclosure_contract dict into the canonical schema shape.

    Unknown keys are dropped (forward-compat with Phase-2 axes like
    declared data_classes). Missing keys are filled from the default
    contract. The returned dict is fresh (caller-owned).

    Validation is conservative:
      * sensitivity must be in ``VALID_SENSITIVITIES`` else default applied
      * t5_response_mode must be in ``VALID_T5_RESPONSE_MODES`` else default
      * t5_free_text coerces to bool
    """
    contract = default_disclosure_contract()
    if not isinstance(raw, dict):
        return contract
    sensitivity = raw.get("sensitivity")
    if sensitivity in VALID_SENSITIVITIES:
        contract["sensitivity"] = sensitivity
    mode = raw.get("t5_response_mode")
    if mode in VALID_T5_RESPONSE_MODES:
        contract["t5_response_mode"] = mode
    elif sensitivity == SENSITIVITY_LOW and "t5_response_mode" not in raw:
        # low-sensitivity default flips the mode to "free" when the caller
        # didn't override it — see SPEC § 設計方針 2 dual-default.
        contract["t5_response_mode"] = T5_RESPONSE_MODE_FREE
    if "t5_free_text" in raw:
        contract["t5_free_text"] = bool(raw.get("t5_free_text"))
    elif contract["sensitivity"] == SENSITIVITY_LOW:
        # low projects default to allowing free text. Explicit override above
        # still wins.
        contract["t5_free_text"] = True
    return contract


def disclosure_contract_from_policy(policy: Optional[dict]) -> dict:
    """Build the envelope-burned contract from a project's disclosure_policy.

    ``policy`` is the dict that lives in project.json under the
    ``disclosure_policy`` key. Missing or malformed inputs degrade to the
    safe default (= high sensitivity, schema-only T5).

    The output of this function is what gets *signed into* the envelope at
    issuance time; we keep it small so the canonical bytes stay compact.
    """
    return normalize_disclosure_contract(policy)


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
    # ms-63 / e-1443: disclosure_contract sits in parallel with
    # actions_authorized as the read-side counterpart. Defaulting to None
    # is what older (pre-ms-63) envelopes look like on the wire; verify()
    # treats absent contract as the safe default (= high sensitivity) for
    # forward-compat with envelopes minted before this field existed.
    disclosure_contract: Optional[dict] = None
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
    disclosure_contract: Optional[dict] = None,
    disclosure_policy: Optional[dict] = None,
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
    # Enforce enumeration discipline. T1/T3/T5 require strict enumeration
    # (no wildcards). T2 (Operation scope) allows last-segment wildcards per
    # ms-60 SPEC § 設計方針 4 — the SPEC author has documented the subscope
    # contract in the approve flow, so "extract:profile:*" is meaningful.
    # See server/approved_actions.py for the syntax grammar.
    from approved_actions import ApprovedActionsError, validate_actions
    try:
        validate_actions(
            actions_authorized,
            allow_last_segment_wildcard=(tier == TIER_T2),
        )
    except ApprovedActionsError as exc:
        raise ValueError(str(exc)) from exc

    issued_at = _now_iso()
    expires_at = _plus_seconds_iso(ttl_seconds)
    nonce = secrets.token_urlsafe(16)
    convo = conversation_id or secrets.token_urlsafe(8)

    # ms-63 / e-1429: bake the disclosure_contract in at mint time so the
    # receive-side gate (e-1430) consults the contract that the *sender's*
    # project agreed to at join time, not a live (mutable) project lookup.
    # Precedence: explicit disclosure_contract > disclosure_policy → derived
    # > default-safe. Either way the result is normalized to the canonical
    # schema shape so out-of-band edits don't get carried into the signed
    # bytes.
    if disclosure_contract is not None:
        contract = normalize_disclosure_contract(disclosure_contract)
    elif disclosure_policy is not None:
        contract = disclosure_contract_from_policy(disclosure_policy)
    else:
        contract = default_disclosure_contract()

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
        "disclosure_contract": contract,  # ms-63 / e-1443 — signed-in
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
# Disclosure gate (ms-63 / e-1430) — receive-side response gate
#
# Symmetric counterpart to e-1293 (persistence gate, "書込口"). Where the
# write gate refuses an *inbound* bus DM from being persisted, the disclosure
# gate refuses an *outbound* AI reply from leaving the boundary when the
# inbound envelope's disclosure_contract forbids it.
#
# Two related responsibilities:
#   1. ``disclosure_gate_check(envelope, reply_kind, reply_payload)`` — verdict
#      function the handler layer calls before sending a reply. Returns either
#      a "permit" or "refuse" result with a reason.
#   2. ``T5 自発的問い合わせ救済`` (SPEC § 設計方針 5) — the two-direction
#      relaxation for genuine query traffic: in_reply_to chain → T3 treatment
#      (handled at verify time via the parent lookup), and ``query_type``
#      schema → permitted even from T5 on low-sensitivity projects.
# ---------------------------------------------------------------------------

# Reply kinds the disclosure gate knows about. ``schema`` is a structured
# response (ack / busy / etc.); ``query`` is a question (no answer leakage
# risk); ``free`` is open prose (the prompt-injection / exfil risk surface).
REPLY_KIND_SCHEMA = "schema"
REPLY_KIND_QUERY = "query"
REPLY_KIND_FREE = "free"
VALID_REPLY_KINDS = {REPLY_KIND_SCHEMA, REPLY_KIND_QUERY, REPLY_KIND_FREE}

# Keys allowed in a ``query`` reply (SPEC § 設計方針 5 方向 B). A query is a
# question, not an answer — so it can carry the question text but not data.
# ``question`` holds the question prose; ``ref`` may carry a task/event id
# the question is about. Length-capped to avoid free-text exfil through the
# query schema.
T5_QUERY_KEYS = frozenset({"question", "ref", "kind"})
T5_QUERY_VALUE_MAX_LEN = 200  # generous enough for a real question, capped


@dataclasses.dataclass
class DisclosureVerdict:
    """Outcome of disclosure_gate_check.

    ``permit`` is True iff the reply may be sent as-is. ``rewrite_to`` is the
    schema-conformant fallback the caller should send instead when the gate
    refuses but a degraded reply is still appropriate (e.g. ack-only). The
    handler layer can choose to either send ``rewrite_to`` automatically or
    escalate via the t3-escalation channel (e-1442 wiring).
    """
    permit: bool
    reason: str
    rewrite_to: Optional[dict] = None

    def to_audit_dict(self) -> dict:
        return {
            "permit": self.permit,
            "reason": self.reason,
            "rewrite_to": dict(self.rewrite_to) if self.rewrite_to else None,
        }


def _validate_query_payload(payload: dict) -> Optional[str]:
    """Return None if ``payload`` fits the T5 query schema, else a reason.

    The query schema is the second of the two SPEC § 設計方針 5 救済
    paths — it lets a genuine question travel even on T5 while the answer
    surface stays bounded by the receiver's own disclosure gate.
    """
    if not isinstance(payload, dict):
        return "query payload must be a dict"
    extra = set(payload.keys()) - T5_QUERY_KEYS
    if extra:
        return f"query payload keys not in allowlist: {sorted(extra)}"
    for key, value in payload.items():
        if isinstance(value, str):
            if len(value) > T5_QUERY_VALUE_MAX_LEN:
                return (f"query payload field {key!r} exceeds "
                        f"{T5_QUERY_VALUE_MAX_LEN} chars")
        elif isinstance(value, (int, float, bool)) or value is None:
            continue
        else:
            return f"query payload field {key!r} must be primitive"
    return None


def _validate_high_sensitivity_schema(payload: dict) -> Optional[str]:
    """Return None if ``payload`` fits the high-sensitivity reply schema.

    Implements SPEC acceptance § 5: "busy / available / ack / task_id 参照
    以外の自由テキスト応答が gate で reject されること". Used by the
    disclosure_gate when the inbound envelope's contract says
    ``t5_response_mode == schema-only`` (= high-sensitivity project).
    """
    if not isinstance(payload, dict):
        return "high-sensitivity reply payload must be a dict"
    keys = set(payload.keys())
    extra = keys - T5_RESPONSE_SCHEMA_HIGH_KEYS
    if extra:
        return (f"high-sensitivity reply keys not in allowlist "
                f"{sorted(T5_RESPONSE_SCHEMA_HIGH_KEYS)}: {sorted(extra)}")
    for key, value in payload.items():
        if isinstance(value, str):
            if len(value) > T5_PING_VALUE_MAX_LEN:
                return (f"high-sensitivity reply field {key!r} exceeds "
                        f"{T5_PING_VALUE_MAX_LEN} chars")
        elif isinstance(value, (int, float, bool)) or value is None:
            continue
        else:
            return f"high-sensitivity reply field {key!r} must be primitive"
    return None


def effective_tier_for_disclosure(envelope: Optional[dict]) -> str:
    """Return the effective tier for disclosure-gate purposes.

    SPEC § 設計方針 5 方向 A: a T5 envelope that carries a non-empty
    ``in_reply_to`` is treated as T3 (= reply chain). This lets the
    TrailNode → Beacon "PR#66 どうなった?" use case survive — the original
    spontaneous question is T5 (the requester's AI woke up by itself),
    but the conversation chain is alive, so the recipient can answer in
    the relaxed-but-bounded T3 regime instead of the T5-ping-only cap.

    This is **separate** from the verify-time tier (= the wire tier). The
    wire tier governs delivery decisions (decide_delivery), the
    disclosure-effective tier governs reply content (disclosure_gate).
    Keeping the two helpers separate preserves backwards-compat with the
    existing ms-60 / e-1340 wiring that reads ``effective_tier`` from
    VerifyResult.

    Other tiers pass through unchanged. The in_reply_to chain's integrity
    is still verified at receive-time (Step 6 of the verify pipeline), so
    by the time the disclosure gate runs, an in_reply_to-carrying T5
    envelope is already known to be replying to a real parent.
    """
    if envelope is None:
        return TIER_T5
    tier = envelope.get("tier")
    in_reply_to = envelope.get("in_reply_to")
    if tier == TIER_T5 and in_reply_to:
        return TIER_T3
    return tier or TIER_T5


def _ack_only_rewrite() -> dict:
    """Return the minimal ack-only reply used as a refuse-but-degrade fallback.

    When the gate refuses a free-text reply, the handler layer can substitute
    this instead of dropping the reply on the floor. ``ack=true`` is the
    standard "I received your message but can't share details" signal.
    """
    return {"ack": True}


def disclosure_gate_check(
    envelope: Optional[dict],
    *,
    reply_kind: str,
    reply_payload: dict,
) -> DisclosureVerdict:
    """Check whether an outbound AI reply may be sent under this envelope.

    Symmetric to the receive-side verify (e-1155) and the persistence gate
    (e-1293): the handler layer calls this BEFORE emitting any reply text.
    A None envelope means the inbound message was legacy/unsigned — same
    fail-closed default the rest of the pipeline applies.

    Decision matrix (SPEC § 設計方針):
      * reply_kind=schema with payload conforming to T5 ping → always permit
        (= the safest reply shape).
      * reply_kind=query → permitted on low-sensitivity projects (= valuable
        TrailNode→Beacon "PR#66 どうなった?" pattern, SPEC § 5 方向 B);
        refused on high-sensitivity projects with a query→schema rewrite
        suggestion (the question itself can leak context about what the
        sender is interested in).
      * reply_kind=free → permitted on low-sensitivity projects iff the
        contract allows ``t5_free_text``; refused on high-sensitivity with
        an ack-only rewrite suggestion.

    Phase 2 (NOT implemented): tier-aware relaxation for T1 inbound (= human
    explicit) where free-text replies may be permitted unconditionally. The
    current gate treats *all* tiers conservatively because the inbound
    envelope's tier affects what the sender is authorized to *send*, not
    what the receiver may *disclose*. Those are orthogonal axes.
    """
    if reply_kind not in VALID_REPLY_KINDS:
        return DisclosureVerdict(
            permit=False,
            reason=f"unknown reply_kind {reply_kind!r}",
            rewrite_to=_ack_only_rewrite(),
        )

    # Pull contract from envelope; fall back to the safe default when the
    # inbound is legacy or didn't ship a contract. Fail-closed is required
    # here — an attacker who can suppress the contract field on the wire
    # must NOT thereby gain free-text disclosure.
    if envelope is None:
        contract = default_disclosure_contract()
    else:
        contract = normalize_disclosure_contract(
            envelope.get("disclosure_contract")
        )

    sensitivity = contract.get("sensitivity", SENSITIVITY_HIGH)
    response_mode = contract.get("t5_response_mode", T5_RESPONSE_MODE_SCHEMA_ONLY)
    allow_free_text = bool(contract.get("t5_free_text", False))

    # SPEC § 設計方針 5 方向 A: T5 + in_reply_to → T3 promotion for the
    # disclosure side. The reply-chain promotion is *additive*: high
    # sensitivity still caps free-text outright (the NDA metaphor doesn't
    # bend just because we're in a reply), but the **query** surface
    # opens on high projects when the conversation is alive — the
    # original TrailNode → Beacon question pattern survives even on
    # high-sensitivity recipients.
    effective_tier = effective_tier_for_disclosure(envelope)
    in_reply_chain = (effective_tier == TIER_T3)

    # --- schema reply ---
    # Schema replies (= structured short-ping shape) are the lowest-risk
    # surface; they're always permitted as long as the payload itself fits
    # the high-sensitivity allowlist (which is stricter than ping-shape, so
    # passing the strict one means passing both).
    if reply_kind == REPLY_KIND_SCHEMA:
        fail = _validate_high_sensitivity_schema(reply_payload)
        if fail:
            return DisclosureVerdict(
                permit=False,
                reason=fail,
                rewrite_to=_ack_only_rewrite(),
            )
        return DisclosureVerdict(permit=True, reason="schema reply permitted")

    # --- query reply (T5 救済方向 B + 方向 A combined) ---
    if reply_kind == REPLY_KIND_QUERY:
        if sensitivity == SENSITIVITY_HIGH and not in_reply_chain:
            # 自発的 T5 (no in_reply_to) on a high project: even the
            # question itself can carry context about what we care about;
            # refuse and suggest a schema rewrite. This is the conservative
            # default. An alive reply chain (in_reply_to → T3 promotion)
            # opens this surface so the TrailNode → Beacon pattern works
            # even on high projects (SPEC § 設計方針 5 方向 A).
            return DisclosureVerdict(
                permit=False,
                reason=("query replies refused on high-sensitivity project "
                        "(send a schema reply with task_id reference instead)"),
                rewrite_to=_ack_only_rewrite(),
            )
        fail = _validate_query_payload(reply_payload)
        if fail:
            return DisclosureVerdict(
                permit=False,
                reason=fail,
                rewrite_to=_ack_only_rewrite(),
            )
        return DisclosureVerdict(permit=True, reason="query reply permitted")

    # --- free-text reply ---
    # This is the prompt-injection / exfil risk surface. Refuse unless:
    #   * project is low-sensitivity, AND
    #   * contract explicitly allows t5_free_text, AND
    #   * the policy isn't in schema-only mode (an override on a low project).
    if sensitivity == SENSITIVITY_HIGH:
        return DisclosureVerdict(
            permit=False,
            reason=("free-text replies refused on high-sensitivity project "
                    "(use a schema reply: ack / busy / available / task_id)"),
            rewrite_to=_ack_only_rewrite(),
        )
    if response_mode == T5_RESPONSE_MODE_SCHEMA_ONLY:
        return DisclosureVerdict(
            permit=False,
            reason="response_mode=schema-only forbids free-text replies",
            rewrite_to=_ack_only_rewrite(),
        )
    if not allow_free_text:
        return DisclosureVerdict(
            permit=False,
            reason="t5_free_text disabled on this project",
            rewrite_to=_ack_only_rewrite(),
        )
    return DisclosureVerdict(permit=True, reason="free reply permitted")


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
