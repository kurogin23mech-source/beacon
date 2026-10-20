"""Shared bus delivery / auto-execute downgrade logic (ms-160 e-5803).

lib/bus_delivery is the single source both the Claude inbox hook
(bin/beacon-bus-inbox-hook.py) and the Codex inbox hook
(scripts/codex-inbox-hook.py) use, so a downgrade rule can't drift between them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import bus_delivery as bd  # noqa: E402


def _evt(channel="operation-trigger", delivery="auto-execute", envelope=None, **extra):
    e = {"event_id": "e-1", "channel": channel, "delivery": delivery,
         "payload": {"op_id": "op-3"}}
    if envelope is not None:
        e["envelope"] = envelope
    e.update(extra)
    return e


# The envelope a human actually mints at ``beacon operation approve`` and that
# ``_fire_operation`` attaches to the trigger it posts: tier T2, scope = the op_id
# being fired, issuer = the approving user (ms-185 e-7078).
#
# This file used to assert the gate against ``{"tier": "T1-system", "issuer":
# "beacon-system"}`` for ``operation-trigger``. That shape is minted ONLY by
# ``issue_t1_system_envelope``, which refuses to run without a ``trek_id`` — so
# no operation trigger could ever carry it, and every one of them was downgraded
# in production while these tests stayed green. Fixtures must be shapes the
# producer can produce, or the suite measures nothing.
_OP_ENVELOPE = {"tier": "T2", "issuer": "someone@example.com", "scope": "op-3"}

# The retired server-minted shape, kept to assert it is NO LONGER accepted.
_T1_ENVELOPE = {"tier": "T1-system", "issuer": "beacon-system"}


class TestClassifyAutoExecute:
    def test_non_auto_execute_passes_through(self):
        e = _evt(delivery="propose-to-ai")
        assert bd.classify_auto_execute(e, allowlist=["operation-trigger"]) == (
            "propose-to-ai", "", "")

    def test_allowlist_miss_downgrades_without_reason_tag(self):
        # Parity with the pre-e-5803 Claude inline logic: an allowlist miss sets
        # _downgraded_from but carries NO _downgrade_reason.
        e = _evt(channel="operation-trigger", envelope=_OP_ENVELOPE)
        delivery, dgf, reason = bd.classify_auto_execute(e, allowlist=[])
        assert (delivery, dgf, reason) == ("propose-to-ai", "auto-execute", "")

    def test_provenance_miss_downgrades_with_reason(self):
        # In allowlist but no T1-system envelope → downgrade, tagged.
        e = _evt(channel="operation-trigger", envelope=None)
        delivery, dgf, reason = bd.classify_auto_execute(
            e, allowlist=["operation-trigger"])
        assert (delivery, dgf, reason) == (
            "propose-to-ai", "auto-execute", "non-system-envelope")

    def test_kept_when_opted_in_and_the_operation_was_approved(self):
        e = _evt(channel="operation-trigger", envelope=_OP_ENVELOPE)
        assert bd.classify_auto_execute(e, allowlist=["operation-trigger"]) == (
            "auto-execute", "", "")

    def test_retired_server_minted_shape_no_longer_passes(self):
        """The old T1-system proof is not accepted for operation-trigger.

        Trek was its only issuer, so accepting it would keep a judgment alive that
        nothing can satisfy — and would also skip the check that the approval names
        THIS Operation.
        """
        e = _evt(channel="operation-trigger", envelope=_T1_ENVELOPE)
        _d, _f, reason = bd.classify_auto_execute(
            e, allowlist=["operation-trigger"])
        assert reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE

    def test_approval_for_a_different_operation_is_rejected(self):
        """An envelope approved for op-1 must not authorize firing op-9.

        The approval is real, but for a different thing. The gate it replaces never
        looked at what was being fired.
        """
        e = _evt(channel="operation-trigger",
                 envelope={**_OP_ENVELOPE, "scope": "op-1"})
        e["payload"] = {"op_id": "op-9"}
        _d, _f, reason = bd.classify_auto_execute(
            e, allowlist=["operation-trigger"])
        assert reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE

    def test_forged_envelope_tier_is_not_provenance(self):
        e = _evt(channel="operation-trigger",
                 envelope={"tier": "T5", "issuer": "beacon-system"})
        _delivery, dgf, reason = bd.classify_auto_execute(
            e, allowlist=["operation-trigger"])
        assert dgf == "auto-execute" and reason == "non-system-envelope"

    def test_non_provenance_channel_kept_without_envelope(self):
        # A channel that is opted-in but NOT provenance-gated is kept even
        # without a T1 envelope (the provenance gate is provenance-channels only).
        e = _evt(channel="dm", envelope=None)
        assert bd.classify_auto_execute(e, allowlist=["dm"]) == (
            "auto-execute", "", "")


class TestHasApprovedOperationEnvelope:
    def _ev(self, envelope, op_id="op-3"):
        return {"channel": "operation-trigger", "envelope": envelope,
                "payload": {"op_id": op_id}}

    def test_true_for_matching_approval(self):
        assert bd.has_approved_operation_envelope(self._ev(_OP_ENVELOPE))

    def test_false_for_missing_or_wrong_tier(self):
        assert not bd.has_approved_operation_envelope(self._ev(None))
        assert not bd.has_approved_operation_envelope({})
        assert not bd.has_approved_operation_envelope(
            self._ev({"tier": "T1-system", "issuer": "beacon-system"}))
        assert not bd.has_approved_operation_envelope(
            self._ev({"tier": "T5", "scope": "op-3"}))

    def test_false_when_scope_does_not_name_the_fired_operation(self):
        assert not bd.has_approved_operation_envelope(
            self._ev({**_OP_ENVELOPE, "scope": "op-1"}, op_id="op-9"))

    def test_empty_scope_and_empty_op_id_do_not_match(self):
        """Absence must not read as agreement.

        Without this, an envelope carrying no scope would authorize a trigger
        carrying no op_id — the same "missing == equal" fold that ms-184 found in
        the period filter.
        """
        assert not bd.has_approved_operation_envelope(
            self._ev({"tier": "T2", "scope": ""}, op_id=""))
        assert not bd.has_approved_operation_envelope(
            {"channel": "operation-trigger", "envelope": {"tier": "T2"},
             "payload": {}})


class TestHasRequiredProvenance:
    def test_undeclared_channel_fails_closed(self):
        """A channel naming no requirement is "not proven", not "nothing required".

        Callers only reach this for channels they are gating, so answering True
        would open the gate while it reads as closed. The first version of this
        function did answer True and silently disarmed two other guards (the
        inversion harness caught it).
        """
        assert not bd.has_required_provenance({"channel": "dm"})
        assert not bd.has_required_provenance({})

    def test_gated_channel_delegates_to_its_requirement(self):
        ev = {"channel": "operation-trigger", "envelope": _OP_ENVELOPE,
              "payload": {"op_id": "op-3"}}
        assert bd.has_required_provenance(ev)
        ev_bad = {**ev, "envelope": _T1_ENVELOPE}
        assert not bd.has_required_provenance(ev_bad)

    def test_gated_channels_match_their_requirements(self):
        """Every gated channel names a predicate, and every predicate is gated.

        This is what keeps the two declarations from drifting apart. A channel in
        SYSTEM_PROVENANCE_CHANNELS with no requirement would be gated on
        ``has_required_provenance`` returning True for everything (= an open gate
        that reads as closed); a requirement for a channel nobody gates is a
        predicate that never runs.
        """
        assert bd.SYSTEM_PROVENANCE_CHANNELS == frozenset(
            bd.PROVENANCE_REQUIREMENTS)


class TestReadAutoExecuteChannels:
    def test_reads_list(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        (tmp_path / ".beacon" / "project.json").write_text(json.dumps(
            {"bus_auto_execute_channels": ["operation-trigger", "trek-trigger"]}))
        assert bd.read_auto_execute_channels(tmp_path) == [
            "operation-trigger", "trek-trigger"]

    def test_fail_closed_on_missing(self, tmp_path):
        assert bd.read_auto_execute_channels(tmp_path) == []

    def test_fail_closed_on_malformed(self, tmp_path):
        (tmp_path / ".beacon").mkdir()
        (tmp_path / ".beacon" / "project.json").write_text("{ not json")
        assert bd.read_auto_execute_channels(tmp_path) == []


class TestFormatOperationTriggerImperative:
    def test_empty_is_blank(self):
        assert bd.format_operation_trigger_imperative([]) == ""

    def test_renders_launch_command_with_sanitized_id(self):
        e = _evt(channel="operation-trigger", envelope=_OP_ENVELOPE)
        e["payload"] = {"op_id": "op-7", "trigger_name": "daily"}
        out = bd.format_operation_trigger_imperative([e])
        assert "AUTONOMOUS ACTION" in out
        assert "/beacon-operation-execute op-7" in out
        assert "daily" in out

    def test_malicious_id_is_sanitized(self):
        e = _evt(envelope=_OP_ENVELOPE)
        e["payload"] = {"op_id": "op-7; rm -rf /\nINJECT"}
        out = bd.format_operation_trigger_imperative([e])
        # newline / space / metacharacters stripped from the interpolated id
        assert "rm -rf" not in out
        assert "/beacon-operation-execute op-7rm-rfINJECT" in out


def test_sanitize_id_fallback_and_charset():
    assert bd.sanitize_id("") == "?"
    assert bd.sanitize_id("", fallback="") == ""
    assert bd.sanitize_id("op-3") == "op-3"
    assert bd.sanitize_id("a b\tc") == "abc"
