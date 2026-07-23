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


if __name__ == "__main__":
    unittest.main()
