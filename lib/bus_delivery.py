"""Shared bus delivery / auto-execute downgrade logic (ms-160 e-5803).

The Claude bus-inbox hook (``bin/beacon-bus-inbox-hook.py``) and the Codex inbox
hook (``scripts/codex-inbox-hook.py``) both receive bus events and must make the
SAME safety decision for ``auto-execute`` events:

  1. An ``auto-execute`` event whose channel is NOT in the project's
     ``bus_auto_execute_channels`` allowlist is downgraded to ``propose-to-ai``
     (a session must not run a Skill it was never opted-in to).
  2. An ``auto-execute`` event on a gated channel (operation-trigger / trek-*)
     that does not carry the proof of human authority ITS channel demands is ALSO
     downgraded — otherwise a project editor could forge a
     ``/beacon-… <attacker-id>`` imperative. The proof differs per channel
     (``PROVENANCE_REQUIREMENTS``): operation-trigger wants the T2 envelope the
     human approved for that op_id, trek-* want the server-minted T1-system one.
  3. A surviving opted-in ``operation-trigger`` event is rendered as a structured
     "AUTONOMOUS ACTION" block that tells the AI to run
     ``/beacon-operation-execute <op-id>`` without asking first.

Before this module, all of the above lived ONLY in the Claude hook; the Codex
hook rendered every event as a generic DM, so a Codex session neither downgraded
un-allowlisted auto-execute events nor surfaced the operation-trigger imperative.
This module is the ONE place that logic lives; both hooks import it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Delivery classes (mirror server-side BusEventCreate + bus.mjs).
AUTO_EXECUTE = "auto-execute"
PROPOSE_TO_AI = "propose-to-ai"
NOTIFY_USER_ONLY = "notify-user-only"

OPERATION_TRIGGER_CHANNEL = "operation-trigger"

# Channels whose auto-execute events are routed into a Level 3 imperative block.
# An auto-execute event here is only honored when it also carries proof that a
# human authorized this autonomy; otherwise it is downgraded.
#
# **What counts as proof differs per channel** — see PROVENANCE_REQUIREMENTS.
# This set is exactly that map's keys, so a channel can never be "gated" without
# naming a predicate that something can actually satisfy (ms-185 e-7078).
#
# History (why the name says "system"): the gate used to demand one single proof,
# a server-minted ``T1-system`` envelope. That envelope is structurally bound to
# Trek — ``issue_t1_system_envelope`` refuses to mint without a ``trek_id`` and
# forces ``scope="trek:<id>"`` — so retiring Trek left the gate with ZERO possible
# issuers. Measured on 2026-10-20: ``operation-trigger`` sat in this set while
# ``_fire_operation`` attaches the envelope the human minted at
# ``beacon operation approve`` (``tier="T2"``, ``issuer=<their email>``), so
# ``classify_auto_execute`` downgraded EVERY operation trigger with
# ``non-system-envelope`` — Operation autonomy had never once fired in
# production. The tests were green because they pinned ``operation-trigger``
# against a synthetic T1-system envelope, a shape production cannot produce.
# So the proof was re-pointed from "the server signed it (for a Trek)" to "the
# human approved it (for this Operation)". The set name is kept because the
# downgrade reason tag ``non-system-envelope`` is a wire value the inbox hooks
# branch on to choose which fix to advise (e-5803 review); renaming the vocabulary
# is a separate change from re-pointing the gate.
#
# Kept as a literal (not derived from ``PROVENANCE_REQUIREMENTS.keys()``) because
# ``classify_auto_execute`` binds it as a default argument at def time, and the
# map cannot be built until its predicates exist. The two are held in agreement
# by ``test_gated_channels_match_their_requirements``, which fails if a name sits
# in one and not the other — a structural check instead of fragile init order.
SYSTEM_PROVENANCE_CHANNELS = frozenset({
    "operation-trigger",
    "trek-trigger",
    "trek-progress-check",
    "trek-task-review",
    "trek-leader-digest",
})

# Channels that USED to be server-minted and no longer are (ms-185 e-7077).
#
# **Why this set has to exist.** The provenance gate below is deliberately
# narrow: a channel the project explicitly opted into (``bus auto-execute add``)
# is honored WITHOUT a server signature, because the opt-in itself is the human
# consent (pinned by ``test_non_provenance_channel_kept_without_envelope``).
# That is sound while "opted in" and "server-minted" are decided independently.
#
# It stops being sound the moment a channel is REMOVED from
# ``SYSTEM_PROVENANCE_CHANNELS`` while stale opt-ins for it still sit in some
# project's ``bus_auto_execute_channels``. The channel then silently changes
# class — from "signature required" to "trusted on the opt-in alone" — and an
# unsigned ``delivery=auto-execute`` event on it is executed. Measured on
# 2026-10-19: with ``trek-*`` still in this repo's allowlist (written there by
# ``_arm_for_trek`` at Trek-join time) and removed from the provenance set,
# ``classify_auto_execute`` returns ``("auto-execute", "", "")`` for an
# unsigned event. Nothing in the old code could notice.
#
# So retiring a provenance channel is a two-sided operation and this set is the
# code side: a retired channel is downgraded even when it is allowlisted, which
# keeps stale data fail-closed forever. The data side (removing the stale
# opt-ins) is still done — this is defence in depth, not a substitute, because
# allowlists live in every project's own record and we cannot reach them all.
#
# **Do not just delete a name from SYSTEM_PROVENANCE_CHANNELS — move it here.**
# ``tests/test_retired_provenance_channels_e7077.py`` fails if a known
# server-minted channel family member is in neither set.
#
# ms-185 e-7078 note: the ``trek-*`` family stays LIVE here for now. Their proof
# (the T1-system envelope the Trek scheduler mints) still has its issuer, so the
# gate is still satisfiable and nothing is left unsatisfiable. They move into this
# set together with the removal of Trek's bus surface (e-7080 / e-7070), which is
# also where their tests are retired — moving them earlier would churn tests that
# are about to be deleted, and would stop Trek autonomy before its code is gone.
RETIRED_PROVENANCE_CHANNELS: frozenset = frozenset()

# The envelope tier a human mints at ``beacon operation approve`` (server mints it
# with ``tier=T2``, ``scope=<op_id>``). This is the proof operation-trigger demands.
OPERATION_ENVELOPE_TIER = "T2"

# The server-minted tier. Still the proof the ``trek-*`` channels demand (see
# PROVENANCE_REQUIREMENTS), and its sole issuer is Trek — so these constants, the
# ``has_server_minted_envelope`` predicate, and the trek-* requirement entries all
# retire together with Trek (e-7070 / e-7080). ``operation-trigger`` no longer
# accepts this proof (ms-185 e-7078).
T1_SYSTEM_TIER = "T1-system"
T1_SYSTEM_ISSUER = "beacon-system"

# Downgrade reason tags (surfaced in the audit frame / inject note).
# e-5803 review (AX-8/AX-9): the allowlist-miss sentinel MUST stay the empty
# string. The Claude hook attaches ``_downgrade_reason`` to an event only when the
# reason is truthy (``if downgrade_reason:``); an allowlist miss historically
# carried NO reason key, so a non-empty value would change that hook's on-wire
# event shape + audit frame (breaking parity with the pre-e-5803 inline logic).
# It is ``_``-prefixed (private) so a caller can't ``import`` it and write
# ``reason == bd._DOWNGRADE_ALLOWLIST_MISS`` — which would alias "unset" and
# misclassify any empty reason. Branch on ``reason == DOWNGRADE_NON_SYSTEM_ENVELOPE``
# and treat the else as allowlist-miss (or use ``if not reason``).
_DOWNGRADE_ALLOWLIST_MISS = ""
DOWNGRADE_NON_SYSTEM_ENVELOPE = "non-system-envelope"
# ms-185 e-7077 — the channel was retired from the server-minted set, so an
# opt-in for it no longer means anything. Distinct from the two reasons above
# because the operator action differs: an allowlist miss is fixed by opting in,
# a provenance miss cannot be fixed by the operator at all, and THIS one is
# fixed by removing the stale opt-in (``bus auto-execute remove``).
DOWNGRADE_RETIRED_CHANNEL = "retired-provenance-channel"

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_id(raw: object, *, fallback: str = "?", maxlen: int = 64) -> str:
    """Strip an id down to ``[A-Za-z0-9_-]`` and cap its length.

    Payload-derived ids (op_id / trek_id / task_id) are interpolated into the
    imperative command strings; this defends the injected instruction against a
    malformed id (newline / markdown / shell metacharacter) leaking in. Returns
    ``fallback`` when nothing survives so the block never emits an empty command.
    """
    cleaned = _SAFE_ID_RE.sub("", str(raw or ""))[:maxlen]
    return cleaned or fallback


def has_approved_operation_envelope(ev: dict) -> bool:
    """True when an ``operation-trigger`` event carries its own approved envelope.

    The proof that a human authorized this Operation's autonomy is the T2 envelope
    minted at ``beacon operation approve`` (``server/routers_projects.py`` issues it
    with ``tier=T2``, ``issuer=<the approving user>``, ``scope=<op_id>``) and stored
    in ``operation_envelopes/``. ``_fire_operation`` attaches that exact record to
    the event it posts, so the authority travels with the trigger.

    **The scope must name the Operation being fired.** Without that, an envelope
    approved for ``op-1`` would authorize an autonomous run of ``op-9`` — the
    approval would be real but for a different thing. This is a stricter check than
    the T1-system gate it replaces, which only asked "did the server sign this for
    some Trek?" and never looked at what was being fired.

    Reading the claimed ``tier`` / ``scope`` is sound for the same reason the old
    T1-system check gave: every receive runs ``envelope_mod.verify`` before persist
    (``server/app.py`` bus-receive), and a forged claim is degraded to T5, which
    strips ``auto-execute`` via ``decide_delivery``. ``classify_auto_execute`` only
    reaches this predicate for events whose delivery is still ``auto-execute``, so a
    forgery cannot get here.
    """
    env = ev.get("envelope")
    if not isinstance(env, dict):
        return False
    if env.get("tier") != OPERATION_ENVELOPE_TIER:
        return False
    scope = env.get("scope")
    payload = ev.get("payload")
    op_id = payload.get("op_id") if isinstance(payload, dict) else None
    # Both sides must be present AND equal. Empty-vs-empty must not read as a
    # match, or an envelope with no scope would authorize a trigger with no op_id.
    return bool(scope) and bool(op_id) and scope == op_id


def has_server_minted_envelope(ev: dict) -> bool:
    """True when the event carries a persisted T1-system envelope.

    The proof the Trek channels demand: the Beacon server signed the envelope on
    behalf of the human who activated the Trek. ``issue_t1_system_envelope`` is its
    only issuer, and it refuses to mint without a ``trek_id`` — so this predicate
    dies with Trek. It is listed per-channel (rather than being THE gate) exactly so
    that removing Trek removes its entries and this function together, instead of
    leaving a demand no issuer can meet (ms-185 e-7078 / e-7080).
    """
    env = ev.get("envelope")
    if not isinstance(env, dict):
        return False
    return (
        env.get("tier") == T1_SYSTEM_TIER
        and env.get("issuer") == T1_SYSTEM_ISSUER
    )


# What each gated channel accepts as proof of human authority. A channel may only
# appear in SYSTEM_PROVENANCE_CHANNELS if it names a predicate here, which is what
# structurally prevents the state ms-185 e-7078 found: a gate whose proof no issuer
# can mint (the judgment stays in the code, always fails, and nobody notices
# because the failure looks like ordinary caution).
#
# The proof differs per channel because the authorizing human differs:
#   operation-trigger → the person who ran ``beacon operation approve`` for THIS op
#   trek-*            → the person who activated the Trek (the server signs for them)
PROVENANCE_REQUIREMENTS = {
    OPERATION_TRIGGER_CHANNEL: has_approved_operation_envelope,
    "trek-trigger": has_server_minted_envelope,
    "trek-progress-check": has_server_minted_envelope,
    "trek-task-review": has_server_minted_envelope,
    "trek-leader-digest": has_server_minted_envelope,
}


def has_required_provenance(ev: dict) -> bool:
    """True when ``ev`` carries the proof of human authority its channel demands.

    **Fail-closed on an undeclared channel.** A channel with no entry in
    ``PROVENANCE_REQUIREMENTS`` has no proof it could present, so this returns
    False. Callers reach this only for channels they are gating (membership in
    ``SYSTEM_PROVENANCE_CHANNELS``), and for a gated channel "we never said what
    counts as proof" must mean "not proven", never "nothing required".

    That is not the unsatisfiable judgment ms-185 e-7078 set out to remove: the
    default sets cannot reach this state because
    ``test_gated_channels_match_their_requirements`` fails if a gated channel names
    no requirement. Fail-closed only bites a caller passing its own gated set, and
    there the safe direction is to downgrade.

    Returning True for an unknown channel instead (the first shape of this
    function) silently removed the teeth from two guards — the inversion harness
    caught both: swapping the retired/provenance check order stopped being
    detectable, and so did making every opted-in custom channel require a
    signature. An open gate that reads as closed is worse than no gate.
    """
    requirement = PROVENANCE_REQUIREMENTS.get(str(ev.get("channel") or ""))
    if requirement is None:
        return False
    return requirement(ev)


def read_auto_execute_channels(root: "str | Path") -> list[str]:
    """Read the project's auto-execute allowlist from ``.beacon/project.json``.

    Fail-closed: any read error (missing file, corrupt JSON, wrong type) is
    treated as "no channels armed". The receiver-side downgrade is the safety
    net — an unreadable allowlist must NOT be confused for "allow everything".
    """
    path = Path(root) / ".beacon" / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("bus_auto_execute_channels")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c]


def classify_auto_execute(
    event: dict,
    *,
    allowlist,
    # 並びは **評価順** に合わせてある (allowlist → retired → provenance)。
    # ms-185 e-7077 の AX レビュー: 並びと評価順が逆だと、文脈ゼロの読み手が
    # 引数の順から優先順位を類推して逆に取る。優先順位が逆になっても降格自体は
    # 変わらないが理由タグが変わり、消費側の案内が反転する。
    retired_channels=RETIRED_PROVENANCE_CHANNELS,
    provenance_channels=SYSTEM_PROVENANCE_CHANNELS,
) -> tuple:
    """Resolve one event's effective delivery + downgrade metadata.

    Returns ``(delivery, downgraded_from, downgrade_reason)``:

      * non-auto-execute event → ``(delivery, "", "")`` unchanged.
      * auto-execute, channel not in ``allowlist`` →
        ``("propose-to-ai", "auto-execute", "")`` (allowlist miss, no reason tag).
      * auto-execute, channel in ``retired_channels`` →
        ``("propose-to-ai", "auto-execute", "retired-provenance-channel")``.
        Checked BEFORE the provenance gate and regardless of the allowlist,
        because a retired channel's opt-in is exactly the stale state we must
        not trust (ms-185 e-7077 — see RETIRED_PROVENANCE_CHANNELS).
      * auto-execute, channel is a provenance channel without a T1-system
        envelope → ``("propose-to-ai", "auto-execute", "non-system-envelope")``.
      * auto-execute, opted-in and (if provenance-gated) system-minted → kept:
        ``("auto-execute", "", "")``. The caller routes a kept event by channel
        (operation-trigger → imperative block, trek-* → their blocks).

    This is the exact decision the Claude hook's main loop used inline; both hooks
    now share it so a downgrade rule can't drift between them.
    """
    delivery = event.get("delivery") or PROPOSE_TO_AI
    if delivery != AUTO_EXECUTE:
        return (delivery, "", "")
    channel = str(event.get("channel") or "")
    if channel not in allowlist:
        return (PROPOSE_TO_AI, AUTO_EXECUTE, _DOWNGRADE_ALLOWLIST_MISS)
    if channel in retired_channels:
        return (PROPOSE_TO_AI, AUTO_EXECUTE, DOWNGRADE_RETIRED_CHANNEL)
    if channel in provenance_channels and not has_required_provenance(event):
        return (PROPOSE_TO_AI, AUTO_EXECUTE, DOWNGRADE_NON_SYSTEM_ENVELOPE)
    return (AUTO_EXECUTE, "", "")


def format_operation_trigger_imperative(events: list) -> str:
    """Render the "AUTONOMOUS ACTION" inject for opted-in operation-trigger events.

    Scope boundary (e-5803 review / Maint-3): this module currently shares ONLY
    the operation-trigger imperative. The trek-channel Level-3 imperatives
    (trek-trigger / trek-progress-check / trek-task-review / trek-leader-digest)
    still live as Claude-hook-only formatters in bin/beacon-bus-inbox-hook.py and
    are NOT rendered by the Codex inbox hook. That is intentional for now (e-5803's
    scope was operation-trigger + the downgrade gate), not an oversight — a Codex
    session gets the generic event list for trek DMs, not the forced-invoke block.
    Extending trek parity to Codex is deliberately deferred to a follow-up so this
    module isn't a half-migrated home that invites duplicate trek formatters.


    Each event is an opted-in auto-execute event on the ``operation-trigger``
    channel (already passed ``classify_auto_execute`` as kept). The block tells
    the AI to run ``/beacon-operation-execute`` autonomously and lifts the
    op_id / spec_doc_id / trigger_name out of the payload. Returns "" for an
    empty list so callers can just check truthiness.
    """
    if not events:
        return ""
    lines: list[str] = []
    lines.append("## AUTONOMOUS ACTION — operation autonomy active")
    lines.append("")
    lines.append(
        "以下の operation-trigger event は `bus_auto_execute_channels` "
        "allowlist に登録済 (= 人間 opt-in 済) のため、"
        "**確認を取らず autonomous に実行する**。"
    )
    lines.append("Run this autonomously without asking the user first.")
    lines.append("")
    for ev in events:
        payload = ev.get("payload") or {}
        op_id = sanitize_id(payload.get("op_id"))
        spec_doc_id = sanitize_id(payload.get("spec_doc_id"), fallback="")
        trigger_name = payload.get("trigger_name", "")
        eid = ev.get("event_id", "?")
        lines.append(f"- event_id: {eid}")
        lines.append(f"  - op_id: {op_id}")
        if spec_doc_id:
            lines.append(f"  - spec_doc_id: {spec_doc_id}")
        if trigger_name:
            lines.append(f"  - trigger_name: {trigger_name}")
        cmd = f"/beacon-operation-execute {op_id}"
        lines.append(f"  - launch: `{cmd}`  (no confirmation prompt)")
    lines.append("")
    lines.append(
        "budget が枯渇していたら Skill 側 (Step 4.5) が自動で停止 → incident "
        "open → note 残しに降格する。Skill の責務なのでこの inject 側で "
        "事前判定はしない。"
    )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "AUTO_EXECUTE",
    "PROPOSE_TO_AI",
    "NOTIFY_USER_ONLY",
    "OPERATION_TRIGGER_CHANNEL",
    "SYSTEM_PROVENANCE_CHANNELS",
    "T1_SYSTEM_TIER",
    "T1_SYSTEM_ISSUER",
    "DOWNGRADE_NON_SYSTEM_ENVELOPE",
    "OPERATION_ENVELOPE_TIER",
    "PROVENANCE_REQUIREMENTS",
    "has_server_minted_envelope",
    "sanitize_id",
    "has_approved_operation_envelope",
    "has_required_provenance",
    "read_auto_execute_channels",
    "classify_auto_execute",
    "RETIRED_PROVENANCE_CHANNELS",
    "DOWNGRADE_RETIRED_CHANNEL",
    "format_operation_trigger_imperative",
]
