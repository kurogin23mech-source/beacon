"""Orphan / throwaway project detection signals (ms-123 / e-4030).

The ``beacon project orphans`` command is a read-only pre-step for the
destructive cleanup (e-4028): before a human archives 48 throwaway test
projects, they need to *see* the candidates and why each was flagged, with
zero risk of a real project being swept up. The CLI wrapper does I/O; this
suite pins the pure classifier in ``lib/project_cleanup`` so the signal
logic can't silently drift.

Grounding (2026-07-23 live scan): the real residue is 45× ``phase4-test``
plus ``test_beacon`` / ``beacon-test`` / ``Idle Wake Test``, all owned by
the current user, and the project listing carries NO ``created_at`` — so
the discriminating signal in practice is the name pattern. The genuine
project (``beacon-b95643``) must never be flagged.
"""

import os
import sys
import unittest

# Make lib/ importable without packaging gymnastics — same pattern as
# tests/test_session_start_operation_activation.py.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lib")
)

import project_cleanup as pc  # noqa: E402


def _proj(pid, name, owner="u-1", archived=False, **extra):
    row = {"project_id": pid, "name": name, "owner": owner, "archived": archived}
    row.update(extra)
    return row


class TestNameSignal(unittest.TestCase):
    def test_phase_test_variants_match(self):
        for name in ("phase4-test", "phase-test", "phase12-test"):
            self.assertIn("test_named", pc.project_signals(_proj("p", name)))

    def test_known_fixtures_match(self):
        cases = {
            "test-beacon": "test_beacon",
            "beacon-test-7f54cb": "beacon-test",
            "idle-wake-test-352f78": "Idle Wake Test",
        }
        for pid, name in cases.items():
            self.assertIn(
                "test_named", pc.project_signals(_proj(pid, name)),
                f"{pid!r} / {name!r} should be flagged test_named",
            )

    def test_real_projects_not_flagged(self):
        # The genuine project and other real names must carry NO signals.
        for pid, name in (
            ("beacon-b95643", "Beacon"),
            ("sales-crm", "Sales CRM"),
            ("trailnode", "TrailNode"),
            ("greatest-hits", "Greatest Hits"),  # 'test' as a substring only
        ):
            self.assertEqual(
                [], pc.project_signals(_proj(pid, name)),
                f"{name!r} must not be flagged as throwaway",
            )

    def test_test_matches_on_id_when_name_blank(self):
        self.assertIn(
            "test_named", pc.project_signals(_proj("phase4-test-abc", "")),
        )


class TestOtherSignals(unittest.TestCase):
    def test_ownerless_flagged(self):
        sig = pc.project_signals(_proj("p", "Something", owner=""))
        self.assertIn("ownerless", sig)

    def test_empty_created_at_only_when_present(self):
        # Field absent → no signal (the common listing shape).
        self.assertNotIn(
            "empty_created_at", pc.project_signals(_proj("p", "Real")),
        )
        # Field present but blank → signal.
        self.assertIn(
            "empty_created_at",
            pc.project_signals(_proj("p", "Real", created_at="")),
        )
        # Field present and populated → no signal.
        self.assertNotIn(
            "empty_created_at",
            pc.project_signals(_proj("p", "Real", created_at="2026-01-01")),
        )


class TestDetectCandidates(unittest.TestCase):
    def _batch(self):
        return [
            _proj("beacon-b95643", "Beacon"),                  # real, keep
            _proj("phase4-test-01", "phase4-test"),            # candidate
            _proj("phase4-test-02", "phase4-test", archived=True),  # already done
            _proj("test-beacon", "test_beacon"),               # candidate
            _proj("ghost", "Ghost", owner=""),                 # ownerless candidate
        ]

    def test_excludes_real_archived_and_current(self):
        cands = pc.detect_orphan_candidates(
            self._batch(), current_project_id="beacon-b95643",
        )
        ids = {c["project_id"] for c in cands}
        self.assertEqual(ids, {"phase4-test-01", "test-beacon", "ghost"})
        # archived candidate is excluded (nothing left to do)
        self.assertNotIn("phase4-test-02", ids)
        # the current/real project is never proposed
        self.assertNotIn("beacon-b95643", ids)

    def test_current_project_excluded_even_if_test_named(self):
        batch = [_proj("phase4-test-self", "phase4-test")]
        cands = pc.detect_orphan_candidates(
            batch, current_project_id="phase4-test-self",
        )
        self.assertEqual([], cands)

    def test_confidence_high_when_two_signals(self):
        batch = [_proj("phase4-test-x", "phase4-test", owner="")]  # test + ownerless
        cands = pc.detect_orphan_candidates(batch)
        self.assertEqual(1, len(cands))
        self.assertEqual("high", cands[0]["confidence"])

    def test_confidence_medium_when_single_signal(self):
        batch = [_proj("phase4-test-y", "phase4-test", owner="u-1")]
        cands = pc.detect_orphan_candidates(batch)
        self.assertEqual("medium", cands[0]["confidence"])

    def test_no_side_effects_on_input(self):
        batch = self._batch()
        before = [dict(p) for p in batch]
        pc.detect_orphan_candidates(batch)
        self.assertEqual(before, batch)  # input rows untouched


class TestBuildArchivePlan(unittest.TestCase):
    def _cands(self, n):
        return [
            {"project_id": f"phase4-test-{i}", "name": "phase4-test",
             "owner": "u", "signals": ["test_named"], "confidence": "medium"}
            for i in range(n)
        ]

    def test_no_limit_returns_all(self):
        cands = self._cands(48)
        self.assertEqual(48, len(pc.build_archive_plan(cands)))
        self.assertEqual(48, len(pc.build_archive_plan(cands, limit=None)))
        self.assertEqual(48, len(pc.build_archive_plan(cands, limit=0)))

    def test_positive_limit_caps_batch(self):
        cands = self._cands(48)
        plan = pc.build_archive_plan(cands, limit=5)
        self.assertEqual(5, len(plan))
        self.assertEqual(cands[:5], plan)

    def test_limit_larger_than_batch_is_safe(self):
        cands = self._cands(3)
        self.assertEqual(3, len(pc.build_archive_plan(cands, limit=99)))

    def test_pure_no_mutation(self):
        cands = self._cands(4)
        before = [dict(c) for c in cands]
        pc.build_archive_plan(cands, limit=2)
        self.assertEqual(before, cands)


class TestFormatReport(unittest.TestCase):
    def test_empty_states_no_change(self):
        out = pc.format_orphan_report([], total_scanned=54)
        self.assertIn("54", out)
        self.assertIn("変更なし", out)

    def test_report_lists_candidates_and_signals(self):
        cands = pc.detect_orphan_candidates(
            [_proj("phase4-test-01", "phase4-test")],
        )
        out = pc.format_orphan_report(cands, total_scanned=54)
        self.assertIn("phase4-test-01", out)
        self.assertIn("test_named", out)
        # states plainly that nothing was archived
        self.assertIn("archive", out.lower())


class TestSignalCoverage(unittest.TestCase):
    """ms-125 e-4095: name which signals are degraded so the human doesn't
    trust a phantom redundancy (in prod only test_named effectively fires)."""

    def test_name_pattern_always_effective(self):
        cov = pc.assess_signal_coverage([_proj("p", "phase4-test")])
        self.assertEqual("effective", cov["test_named"]["status"])

    def test_ownerless_degraded_when_none_visible(self):
        # A non-admin scan never receives ownerless rows → can't fire.
        cov = pc.assess_signal_coverage([_proj("p", "Real", owner="u-1")])
        self.assertEqual("degraded", cov["ownerless"]["status"])

    def test_ownerless_effective_when_present(self):
        cov = pc.assess_signal_coverage([_proj("p", "Ghost", owner="")])
        self.assertEqual("effective", cov["ownerless"]["status"])

    def test_created_at_degraded_when_field_absent(self):
        # The standard listing carries no created_at at all.
        cov = pc.assess_signal_coverage([_proj("p", "Real")])
        self.assertEqual("degraded", cov["empty_created_at"]["status"])

    def test_created_at_effective_when_field_present(self):
        cov = pc.assess_signal_coverage(
            [_proj("p", "Real", created_at="2026-01-01")])
        self.assertEqual("effective", cov["empty_created_at"]["status"])

    def test_note_flags_degraded_signals(self):
        cov = pc.assess_signal_coverage([_proj("p", "Real", owner="u-1")])
        note = pc.format_coverage_note(cov)
        self.assertIn("縮退", note)
        self.assertIn("ownerless", note)
        self.assertIn("empty_created_at", note)

    def test_note_reassuring_when_all_effective(self):
        # Ownerless present + created_at present → nothing degraded.
        cov = pc.assess_signal_coverage(
            [_proj("p", "Ghost", owner="", created_at="")])
        note = pc.format_coverage_note(cov)
        self.assertNotIn("縮退", note)
        self.assertIn("有効", note)

    def test_note_empty_on_empty_coverage(self):
        self.assertEqual("", pc.format_coverage_note({}))


class TestBindConfirmedPlan(unittest.TestCase):
    """ms-125 e-4095: a confirmed run archives EXACTLY the reviewed ids — never
    a freshly recomputed candidate set."""

    def _cands(self, *ids):
        return [
            {"project_id": i, "name": i, "owner": "u",
             "signals": ["test_named"], "confidence": "medium"}
            for i in ids
        ]

    def test_plan_is_intersection_in_order(self):
        cands = self._cands("a", "b", "c")
        out = pc.bind_confirmed_plan(cands, ["c", "a"])
        self.assertEqual(["a", "c"], [c["project_id"] for c in out["plan"]])

    def test_new_candidate_refused(self):
        # 'z' became a candidate after the dry-run (human confirmed only a,b).
        cands = self._cands("a", "b", "z")
        out = pc.bind_confirmed_plan(cands, ["a", "b"])
        self.assertEqual(["a", "b"], [c["project_id"] for c in out["plan"]])
        self.assertEqual(["z"],
                         [c["project_id"] for c in out["unreviewed_new"]])

    def test_confirmed_but_gone_is_skipped(self):
        # Human confirmed 'x' but it's no longer a candidate.
        cands = self._cands("a")
        out = pc.bind_confirmed_plan(cands, ["a", "x"])
        self.assertEqual(["a"], [c["project_id"] for c in out["plan"]])
        self.assertEqual(["x"], out["skipped_missing"])

    def test_limit_caps_plan_only(self):
        cands = self._cands("a", "b", "c", "d")
        out = pc.bind_confirmed_plan(cands, ["a", "b", "c", "d"], limit=2)
        self.assertEqual(["a", "b"], [c["project_id"] for c in out["plan"]])

    def test_empty_confirmed_archives_nothing(self):
        cands = self._cands("a", "b")
        out = pc.bind_confirmed_plan(cands, [])
        self.assertEqual([], out["plan"])
        # everything is 'unreviewed' → nothing gets archived
        self.assertEqual(["a", "b"],
                         [c["project_id"] for c in out["unreviewed_new"]])

    def test_pure_no_mutation(self):
        cands = self._cands("a", "b")
        before = [dict(c) for c in cands]
        pc.bind_confirmed_plan(cands, ["a"])
        self.assertEqual(before, cands)


if __name__ == "__main__":
    unittest.main()
