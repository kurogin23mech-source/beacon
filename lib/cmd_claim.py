#!/usr/bin/env python3
"""cmd_claim.py — the `beacon claim *` command family (ms-127 e-4321).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports the PUBLIC handlers for dispatch + `commands.X`;
family-private helpers are NOT re-exported (patch them at cmd_claim.<name>).
"""

import json
import os
import sys

import occupation
import work_model
from commands_shared import (
    load_project,
    _extract_token,
    _is_cloud_mode,
    _resolve_active_api_url,
    _get_cloud_config_path,
    _get_api_client,
    _resolve_bus_project_id,
    _resolve_session_id,
)


def _claims_register_provider() -> None:
    """Wire lib/claims.py's cloud provider hook to this CLI's transport
    (ms-55 e-1730).

    Called lazily by each claim command so import-time of lib/claims.py
    stays side-effect-free. Re-registers on every call (cheap, idempotent)
    — handy if the user switches cloud mode mid-session.

    The provider closure returns ``(client, project_id)`` when cloud
    mode is active, or ``None`` to fall back to local. Errors during
    resolution map to ``None`` so the worker keeps working.
    """
    import claims as _claims

    def _provider():
        try:
            if not _is_cloud_mode():
                return None
            client, config = _get_api_client()
        except Exception:
            return None
        project_id = (config or {}).get("project_id") or ""
        if not project_id:
            return None
        return client, project_id

    _claims.register_cloud_provider(_provider)


def _claim_parse_target_env() -> tuple[str, str]:
    """Pull --target kind:id from env vars set by the bash dispatcher."""
    tk = os.environ.get("BEACON_CLAIM_TARGET_KIND", "").strip()
    ti = os.environ.get("BEACON_CLAIM_TARGET_ID", "").strip()
    if not tk or not ti:
        print(
            "Error: --target <kind>:<id> is required "
            "(kind = ms|task|operation|trek|free)",
            file=sys.stderr,
        )
        sys.exit(1)
    return tk, ti


def _claim_post_event(payload: dict) -> dict:
    """Common transport for claim / response / release events.

    Same shape as _stop_post_event: bypass the envelope-issue path
    because claim signals are coordination, not tier-gated capability
    grants — broadcasting "I'm picking up this task" should not require
    the auxiliary endpoint to be up.
    """
    import claims as _claims
    client, config = _get_api_client()
    project_id = _resolve_bus_project_id(config)
    sender = payload.get("from_session_id", "")
    event = client.post_bus_event(
        project_id, _claims.CLAIM_CHANNEL,
        sender_session_id=sender,
        payload=payload,
        delivery="propose-to-ai",
        envelope=None,
        requested_action=None,
    )
    return event


def _claim_issue(claim_kind: str):
    """Shared body for `beacon claim request|handoff|post`."""
    import claims as _claims

    tk, ti = _claim_parse_target_env()
    intent = os.environ.get("BEACON_CLAIM_INTENT", "")
    to_sid = os.environ.get("BEACON_CLAIM_TO", "").strip()
    expires_at = os.environ.get("BEACON_CLAIM_EXPIRES_AT", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_claim_payload(
            claim_kind=claim_kind,
            from_session_id=sender,
            target_kind=tk,
            target_id=ti,
            intent=intent,
            to_session_id=to_sid,
            expires_at=expires_at,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    # Persist the issuer's view of the claim. Cloud-mode rounds through
    # the server subcollection (= multi-machine view) + the local cache
    # mirror; local-mode just writes the cache (ms-55 e-1730).
    _claims_register_provider()
    try:
        _claims.record_claim(payload)
    except (OSError, ValueError) as e:
        # Don't fail the send if persistence is broken — the bus event
        # is the canonical record, this store is a derived view.
        print(f"Note: could not persist claim: {e}",
              file=sys.stderr)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return

    kind_label = {
        "request": "REQUEST (recipient must accept)",
        "handoff": "HANDOFF (recipient must accept)",
        "claim": "CLAIM (broadcast, first-publisher-wins)",
    }.get(claim_kind, claim_kind.upper())
    print(f"{kind_label} on {tk}:{ti} by {sender}")
    print(f"  claim_id: {payload['claim_id']}")
    print(f"  event_id: {event.get('event_id', '?')}")
    if to_sid:
        print(f"  recipient: {to_sid}")
    if intent:
        print(f"  intent: {intent}")
    print(f"  Release with: beacon claim release {payload['claim_id']}")


def cmd_claim_request():
    """beacon claim request --target <k>:<id> --to <sid> [--intent ...]"""
    _claim_issue("request")


def cmd_claim_handoff():
    """beacon claim handoff --target <k>:<id> --to <sid> [--intent ...]"""
    _claim_issue("handoff")


def cmd_claim_post():
    """beacon claim post --target <k>:<id> [--intent ...]

    Broadcast a claim (= "I'm taking X"). First-publisher-wins; no
    recipient consent needed.
    """
    _claim_issue("claim")


def cmd_claim_respond():
    """beacon claim respond <claim_id> --accept|--decline [--reason ...]"""
    import claims as _claims

    claim_id = os.environ.get("BEACON_CLAIM_ID", "").strip()
    decision = os.environ.get("BEACON_CLAIM_DECISION", "").strip()
    reason = os.environ.get("BEACON_CLAIM_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not claim_id:
        print("Error: claim_id is required", file=sys.stderr)
        sys.exit(1)
    if decision not in ("accept", "decline"):
        print("Error: pass --accept or --decline", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_response_payload(
            claim_id=claim_id,
            decision=decision,
            from_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    label = "ACCEPTED" if decision == "accept" else "DECLINED"
    print(f"{label} claim {claim_id} by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


def cmd_claim_release():
    """beacon claim release <claim_id> [--outcome completed|abandoned] [--reason ...]"""
    import claims as _claims

    claim_id = os.environ.get("BEACON_CLAIM_ID", "").strip()
    outcome = os.environ.get("BEACON_CLAIM_OUTCOME", "completed").strip() or "completed"
    reason = os.environ.get("BEACON_CLAIM_REASON", "")
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not claim_id:
        print("Error: claim_id is required", file=sys.stderr)
        sys.exit(1)

    sender = _resolve_session_id()
    if not sender:
        print("Error: cannot resolve current session_id", file=sys.stderr)
        sys.exit(1)

    try:
        payload = _claims.build_release_payload(
            claim_id=claim_id,
            outcome=outcome,
            from_session_id=sender,
            reason=reason,
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    event = _claim_post_event(payload)

    # Drop the local cache entry + cloud-mode subcollection record
    # (ms-55 e-1730). Same fall-back rule as the issue path.
    _claims_register_provider()
    try:
        _claims.release_claim(claim_id)
    except OSError as e:
        print(f"Note: could not update claim cache: {e}",
              file=sys.stderr)

    if json_mode:
        print(json.dumps(event, ensure_ascii=False))
        return
    print(f"RELEASED claim {claim_id} ({outcome}) by {sender}")
    print(f"  event_id: {event.get('event_id', '?')}")


def cmd_claim_list():
    """beacon claim list [--mine] [--target <k>:<id>] [--json]

    Lists claims this session has issued + still has cached locally.

    For a project-wide view ("what's everyone holding right now?"),
    use `beacon bus receive --channel claim-signal` (= live stream) or
    the future `beacon claim status` command that reduces the channel
    history server-side. The local list is the right surface for
    "what was I in the middle of when this session restarted?".
    """
    import claims as _claims

    mine_flag = os.environ.get("BEACON_CLAIM_MINE", "") == "1"
    tk = os.environ.get("BEACON_CLAIM_TARGET_KIND", "").strip() or None
    ti = os.environ.get("BEACON_CLAIM_TARGET_ID", "").strip() or None
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    mine = _resolve_session_id() if mine_flag else None
    if mine_flag and not mine:
        print("Error: --mine requires a resolvable session_id",
              file=sys.stderr)
        sys.exit(1)

    # ms-55 e-1730: cloud-mode pulls from the server subcollection (=
    # multi-machine view), local-mode falls back to the cached file.
    _claims_register_provider()
    out = _claims.list_claims(
        mine=mine, target_kind=tk, target_id=ti,
    )

    if json_mode:
        print(json.dumps(out, ensure_ascii=False))
        return

    if not out:
        print("No active claims in the local cache.")
        return

    print(f"Local active claims ({len(out)}):")
    for rec in out:
        target = rec.get("target") or {}
        tag = f"{target.get('kind', '?')}:{target.get('id', '?')}"
        intent = rec.get("intent") or ""
        intent_suffix = f"  intent: {intent}" if intent else ""
        print(
            f"  [{rec.get('claim_kind', '?')}] {tag}  "
            f"id={rec.get('claim_id', '?')}  "
            f"by={rec.get('from_session_id', '?')}  "
            f"at={rec.get('issued_at', '?')}"
        )
        if intent_suffix:
            print(f"   {intent_suffix.strip()}")


def _resolve_healthy_session_ids():
    """Return the set of currently LIVE + healthy session ids for the current
    project, or ``None`` when liveness cannot be verified (local mode / the
    directory is unreachable). ``None`` is meaningful to the claim view: it
    means "assume occupation claims are live" (conservative, non-blocking),
    whereas an empty set means "verified — nobody is live".

    Best-effort: any failure (no cloud, auth error, network) collapses to
    ``None`` so ``beacon claim view`` still works offline off the raw
    occupation claims on project.json.
    """
    # ms-125 e-4092 (ms-112 目的達成レビュー指摘): in local mode (no cloud.json)
    # liveness is unverifiable — return None (assume occupation claims are live)
    # WITHOUT calling _get_api_client(), which prints a diagnostic to stdout and
    # sys.exit(1)s. The precondition check keeps `--json` output clean; the
    # SystemExit in the except is the belt-and-suspenders for the not-logged-in
    # case (sys.exit is a BaseException, so a bare `except Exception` misses it
    # and would abort the whole command instead of degrading).
    try:
        if not os.path.exists(_get_cloud_config_path()):
            return None
        client, config = _get_api_client()
        project_id = _resolve_bus_project_id(config)
        if not project_id:
            return None
        sessions = client.list_sessions(
            project_id, live_only=True, healthy_only=True, since_minutes=5)
    except (Exception, SystemExit):
        return None
    ids = {s.get("session_id") for s in (sessions or []) if s.get("session_id")}
    return ids


def _invert_focus_directory(sessions):
    """Pure: invert directory session records into ``target_id -> [ {session_id,
    machine, agent, focused_at}, … ]`` keyed by each session's focus milestone
    (ms-125 e-4094). No I/O — split out from ``_resolve_focus_directory`` so the
    inversion (focus.milestone dig-out, actor shaping) is directly unit-testable
    and its bugs surface as exceptions instead of being swallowed by the
    cloud-call ``except`` (ms-125 review: a blanket except over both the call and
    the transform hid transform bugs behind a silent occupation-only fallback).
    """
    directory: dict = {}
    for s in sessions or ():
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id")
        if not sid:
            continue
        focus = s.get("focus") or {}
        milestone = (focus.get("milestone") or {}) if isinstance(focus, dict) else {}
        target_id = (milestone.get("id") or "").strip() if isinstance(milestone, dict) else ""
        if not target_id:
            continue
        actor = s.get("actor") or {}
        directory.setdefault(target_id, []).append({
            "session_id": sid,
            "machine": actor.get("machine") or "",
            "agent": actor.get("agent") or "",
            "focused_at": s.get("last_heartbeat_at") or s.get("last_active") or "",
        })
    return directory


def _resolve_focus_directory(live_ids):
    """Return ``target_id -> [ {session_id, machine, agent, focused_at}, … ]``
    for the live sessions the bus directory reports as focused on each target,
    or ``None`` when the directory cannot be consulted (ms-125 e-4094).

    This is the second, independent LIVE source the claim view consumes (the
    first being the occupation claim on the target record). Each live session
    heartbeats its ``focus.milestone`` into the directory; we invert that into a
    per-target map so ``claim_view`` can flag "someone is focused on this target
    now" even when the one-time occupation claim has gone stale.

    ``live_ids is None`` means liveness could not be verified (local mode /
    directory unreachable) — there is no directory to read a focus from, so we
    return ``None`` and the claim view falls back to occupation-only. A failure
    of the cloud CALL (no cloud, auth error, network) also collapses to ``None``
    so the command keeps working; the focus source is a bonus, never a hard
    dependency. The ``except`` wraps ONLY the cloud call — the pure inversion is
    outside it so a transform bug is not masked as "focus unavailable".
    """
    if live_ids is None:
        return None
    try:
        client, config = _get_api_client()
        project_id = _resolve_bus_project_id(config)
        if not project_id:
            return None
        sessions = client.list_sessions(
            project_id, live_only=True, healthy_only=True, since_minutes=5)
    except Exception:
        return None
    return _invert_focus_directory(sessions)


def cmd_claim_view():
    """beacon claim view [--target <k>:<id>] [--json]

    Read the 2-layer claim state (ms-112 e-3674): for a target, WHO is LIVE
    working on it (occupation claim, liveness-checked against the bus
    directory) + WHO is the persistent assignee — bundled into one view,
    across target-classes (milestone / opportunity / account).

    Non-exclusive (SPEC 設計方針 3): this is a READ. It never blocks work; it
    surfaces flags/warnings so consumers (session-start「次の一手」/ dispatch /
    cockpit) can組み立てる claim-aware without stopping anyone. With ``--target``
    it emits a single view; without, the view for every target keyed by id.
    """
    import claim_view as _claim_view

    tk = os.environ.get("BEACON_CLAIM_TARGET_KIND", "").strip()
    ti = os.environ.get("BEACON_CLAIM_TARGET_ID", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    data = load_project()

    # ms-112 AX+maintainability consensus: the <kind> in --target <kind>:<id>
    # was read then never used by `view` — `--target opp:ms-1` passed silently.
    # Validate it against the id's derived kind (fail-fast, before the cloud call
    # in build_claim_views below) so a mismatch is rejected instead of ignored.
    # ms-109 e-5525 (C9): both sides are descriptor-driven now — the accepted
    # <kind> tokens come from occupation.narrowing_id_prefixes and the id→kind
    # derivation from narrowing_kind_for_ref (built-in fallback work_model.target_
    # kind keeps acquisition/release covered), so a data-defined occupation's
    # claimable kind validates too instead of the built-in ms/opp/acc hardcode.
    if ti and tk:
        _declared = occupation.canonical_claim_kind(tk, data)
        _actual = occupation.narrowing_kind_for_ref(ti, data) \
            or work_model.target_kind(ti)
        if _declared and _actual and _declared != _actual:
            print(f"Error: --target の kind '{tk}' が id '{ti}' (= {_actual}) と "
                  f"一致しません。<kind>:<id> は同じ対象を指してください。",
                  file=sys.stderr)
            sys.exit(1)
    my_session_id = _resolve_session_id()
    # "me" for the assignee layer: the assignee auto-add on `milestone start`
    # writes ``agent.get_actor()["agent"]`` (e.g. "MACHINE-claude"), so match
    # against that first, plus machine / email / the git-actor handle, so a
    # target assigned to any of my aliases reads as assigned_to_me.
    my_identities: set = set()
    try:
        import agent as _agent
        actor = _agent.get_actor()
        for key in ("agent", "machine", "email"):
            v = (actor.get(key) or "").strip()
            if v:
                my_identities.add(v)
    except Exception:
        pass
    try:
        import work_base
        my_identities.add(work_base.current_actor())
    except Exception:
        pass

    live_ids = _resolve_healthy_session_ids()
    # Second LIVE source (ms-125 e-4094): the bus-directory focus. Best-effort —
    # None (local mode / unreachable) collapses the claim view to occupation-only.
    focus_directory = _resolve_focus_directory(live_ids)

    views = _claim_view.build_claim_views(
        data,
        live_session_ids=live_ids,
        focus_directory=focus_directory,
        my_session_id=my_session_id,
        my_identities=my_identities,
    )

    # ms-125 review (AX): disclose whether the focus source was actually
    # consulted, so a transient focus outage reads as "focus unavailable" instead
    # of silently collapsing to "nobody live" (the exact mis-read e-4094 set out
    # to fix). Mirrors cleanup's signal_coverage disclosure. Additive per-view
    # key, so existing consumers of the view map are unaffected.
    if live_ids is None:
        focus_status = "unverified"   # local mode: no directory to read focus
    elif focus_directory is None:
        focus_status = "unavailable"  # liveness verified but focus fetch failed
    else:
        focus_status = "checked"
    for _v in views.values():
        _v["focus_source"] = focus_status
    _focus_note = ("  ※ focus source 取得不可 — 別セッションの focus 作業を取りこぼす"
                   "可能性 (occupation のみで判定)") if focus_status == "unavailable" else ""

    if ti:
        view = views.get(ti)
        if view is None:
            # The target isn't in project.json — still return a well-formed
            # (unclaimed) view so callers don't special-case a miss.
            # ms-112 AX finding: a miss (unknown id, or a kind this view does
            # not walk — task/operation/trek) is NOT "unclaimed=free to grab".
            # Mark exists=False so a typo can't read as a safe claim target.
            view = _claim_view.build_claim_view(
                {"id": ti},
                live_session_ids=live_ids,
                focus_sessions=(focus_directory or {}).get(ti),
                my_session_id=my_session_id,
                my_identities=my_identities,
                exists=False,
            )
            view["focus_source"] = focus_status
        if json_mode:
            print(json.dumps(view, ensure_ascii=False))
            return
        line = _claim_view.format_claim_line(view)
        label = view.get("label") or view.get("target_id")
        print(f"{view.get('target_id')} {label}")
        if not view.get("exists", True):
            # ms-109 e-5525 (C9): the claimable-kind list is derived from the
            # manifest (occupation.claim_target_kinds), not hardcoded — the old
            # "milestone / opportunity / account" text had already drifted (it
            # omitted operation, which build_claim_views DOES walk). task / trek /
            # free are the true out-of-scope kinds (not walked by this view).
            _claimable = " / ".join(occupation.claim_target_kinds(data))
            print(f"  ⚠ この id の target は見つかりません (claim 対象は {_claimable}。"
                  "task / trek / free はこの view の対象外)。unclaimed とは扱いません。")
        else:
            print(f"  {line}" if line else "  (未 claim — 誰も作業中でなく担当も未設定)")
        if _focus_note:
            print(_focus_note)
        if live_ids is None:
            print("  ※ liveness 未確認 (local mode / directory 不通) — LIVE claim は"
                  "健全性未検証で表示")
        return

    if json_mode:
        print(json.dumps(views, ensure_ascii=False))
        return

    if not views:
        print("(no targets)")
        return
    shown = 0
    for tid, view in views.items():
        line = _claim_view.format_claim_line(view)
        if not line:
            continue  # unclaimed targets are noise in the human view
        label = view.get("label") or tid
        print(f"{tid} {label}: {line}")
        shown += 1
    if shown == 0:
        print("(claim 済みの target はありません — 全 target が未 claim)")
    if _focus_note:
        print(_focus_note.lstrip())
    if live_ids is None:
        print("※ liveness 未確認 (local mode / directory 不通)")
