"""ms-142 T5 (e-5160): the "絶対漏らすな capability × 全ターゲットクラス" coverage
matrix, CI-enforced — the 完全性の番人 (§5/§10 of the class-engine ideal image).

Rows are every profession_manifest Target-class (milestone / opportunity /
operation / the synthetic descriptor); columns are the five capabilities a new
class must not silently lack (phase advancement / deadline / 業務→証跡 /
completion-gate existence / claim). Every cell is 3-valued:

  * GREEN        — the capability behaviourally lights up for the class.
  * DECLARED N/A — the manifest itself declares the class lacks the arm the
                   capability rides (operation: work_item_arm=None → no deadline;
                   evidence_arms=[] → no 証跡). The test asserts the BEHAVIOUR
                   agrees (nothing lights up), so a declared absence is proven
                   consistent, not merely asserted (the T4 principle).
  * empty        — neither green nor a declared absence = a SILENT GAP → CI fail.

Because the N/A predicate reads the manifest (not a hand-keyed table), a new
class that declares an evidence arm flips its 証跡 cell from N/A to must-be-green
automatically — the guard follows the declaration. RED debt (a shared capability
reaching a profession concrete) stays in the ledger ratchets (leader 裁定 (A));
this matrix is green/na-only, the behavioural positive twin.
"""
from __future__ import annotations

# sys.path (lib / tests) is centralized in tests/conftest.py (ms-142 e-5144).
import pytest  # noqa: E402

import occupation  # noqa: E402
from capability_profession_matrix import (  # noqa: E402
    MUST_HAVE_CAPABILITIES,
    TARGET_CLASSES,
    _manifest_tc,
)


def _cells():
    for kind, row in TARGET_CLASSES.items():
        for cap, spec in MUST_HAVE_CAPABILITIES.items():
            yield kind, row, cap, spec


@pytest.mark.parametrize(
    "kind,row,cap,spec",
    [pytest.param(k, r, c, s, id=f"{c}:{k}") for k, r, c, s in _cells()],
)
def test_matrix_cell_is_green_or_declared_absent(kind, row, cap, spec):
    """Every (Target-class × must-have capability) cell must be GREEN, or a
    DECLARED absence whose behaviour agrees. An empty cell (a silent gap) fails."""
    project = row["project"]()
    tc = _manifest_tc(project, kind)
    assert tc is not None, (
        f"{kind!r} is a matrix row but not a profession_manifest Target-class — "
        f"add it to the manifest or drop the row.")

    is_na = spec["na"](tc)
    # Probe on a FRESH project (phase_advance mutates state).
    lit = spec["probe"](row["project"](), kind, row["target_id"],
                        row["work_item_id"])

    if is_na:
        # A declared absence must AGREE with behaviour: nothing lights up. If the
        # capability DID light up, the manifest declaration is stale (drift).
        assert not lit, (
            f"cell [{cap} × {kind}] is DECLARED N/A (the manifest arm this "
            f"capability rides — work_item_arm for deadline / evidence_arms for "
            f"evidence — is empty on {kind!r}) but the capability lit up anyway. "
            f"The manifest declaration is stale: give {kind!r} the arm, or drop "
            f"the N/A predicate.")
    else:
        # Not a declared absence ⇒ the capability MUST light up. An empty cell is
        # a silent gap: the class lacks a 絶対漏らすな capability.
        assert lit, (
            f"cell [{cap} × {kind}] is EMPTY — the Target-class silently lacks the "
            f"'{cap}' capability. Either wire it to the occupation abstraction so "
            f"it lights up, or declare the absence in the manifest (an empty arm) "
            f"if the class genuinely has no such grain.")


# ---------------------------------------------------------------------------
# Completeness pins — the guard must fail when a class or capability drifts.
# ---------------------------------------------------------------------------

def test_columns_are_exactly_the_five_must_have_capabilities():
    # §5 "絶対漏らすな capability" set. Dropping/renaming a column (a regression in
    # what the guard checks) fails here. "coverage matrix" itself (§5 row 5) IS
    # this harness, so it is not a column.
    assert set(MUST_HAVE_CAPABILITIES) == {
        "phase_advance", "deadline", "evidence", "completion_gate", "claim",
    }


def test_rows_cover_every_manifest_target_class():
    # The 番人's teeth: a NEW Target-class added to the engine (a new built-in
    # collection or descriptor) must appear as a matrix row, else it could ship
    # missing a must-have capability unnoticed. We assert every manifest kind that
    # appears across the fixtures is a row — so adding a class to a fixture's
    # manifest without a row fails here.
    seen_kinds = set()
    for row in TARGET_CLASSES.values():
        for tc in occupation.profession_manifest(row["project"]())["target_classes"]:
            seen_kinds.add(tc["kind"])
    # Engine-scoped floor (not just fixture-scoped): a bare manifest surfaces the
    # data-INDEPENDENT built-in seed collections (milestones + opportunities +
    # operations). Union it in so a NEW built-in Target-class added to the engine
    # is caught even if no fixture happens to declare it (AX review e-5160).
    for tc in occupation.profession_manifest({})["target_classes"]:
        seen_kinds.add(tc["kind"])
    missing = seen_kinds - set(TARGET_CLASSES)
    assert not missing, (
        f"manifest Target-class(es) {sorted(missing)} have no coverage-matrix "
        f"row — add them to TARGET_CLASSES so every must-have capability is "
        f"checked against them.")


def test_operation_is_a_row_with_declared_absences():
    # ms-142 §8: operation must be a first-class Target-class row. Its deadline /
    # 証跡 cells are DECLARED N/A (work_item_arm=None / evidence_arms=[]), while
    # phase-advance, completion-gate and claim must be GREEN. This pins that
    # operation is present AND that its N/A cells come from the manifest.
    assert "operation" in TARGET_CLASSES
    project = TARGET_CLASSES["operation"]["project"]()
    tc = _manifest_tc(project, "operation")
    assert tc is not None
    assert MUST_HAVE_CAPABILITIES["deadline"]["na"](tc) is True
    assert MUST_HAVE_CAPABILITIES["evidence"]["na"](tc) is True
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](tc) is False


def test_release_is_a_row_with_declared_absences():
    # ms-142 §9 / e-5161: release is dev's L3 first-class Target-class, injected as
    # a profession-default descriptor (occupation.effective_descriptors), so it MUST
    # appear in the dev manifest AND as a coverage-matrix row. Its deadline / 証跡
    # cells are DECLARED N/A (work_item_arm=None / evidence_arms=[]); phase-advance
    # (descriptor phases), completion-gate (self-close-ban) and claim must be GREEN.
    assert "release" in TARGET_CLASSES
    project = TARGET_CLASSES["release"]["project"]()
    tc = _manifest_tc(project, "release")
    assert tc is not None, "release is not in the profession_manifest — the " \
        "effective_descriptors injection regressed."
    assert tc["collection"] == "release_targets"
    assert MUST_HAVE_CAPABILITIES["deadline"]["na"](tc) is True
    assert MUST_HAVE_CAPABILITIES["evidence"]["na"](tc) is True
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](tc) is False


def test_release_surfaces_in_the_bare_dev_manifest_floor():
    # The 番人's engine-scoped floor: release must appear in profession_manifest({})
    # (dev is the default profession), NOT only when a fixture declares a release
    # record — that is what makes the guardian catch a NEW built-in Target-class.
    import occupation  # noqa: E402
    kinds = {tc["kind"]
             for tc in occupation.profession_manifest({})["target_classes"]}
    assert "release" in kinds


def test_account_is_a_row_with_a_declared_completion_gate_absence():
    # ms-142 e-5256: account is the 4th built-in Target-class. It is ball-less and
    # never-terminal, so its EXPECTED cells are pinned here (leader caution 2): the
    # completion-gate cell is a DECLARED N/A (never_terminal), while phase-advance
    # (the phase ladder), deadline (nurturing nrt-), evidence (communications) and
    # claim must be GREEN. A regression that dropped the account manifest entry, its
    # nurturing/communications arms, or the never_terminal declaration fails here.
    assert "account" in TARGET_CLASSES
    row = TARGET_CLASSES["account"]
    project = row["project"]()
    tc = _manifest_tc(project, "account")
    assert tc is not None, "account is not in the profession_manifest — the " \
        "TARGET_COLLECTIONS / _COLLECTION_KIND registration regressed."
    assert tc["collection"] == "accounts"
    # completion_gate is the only N/A cell; the other four must NOT be N/A (→ GREEN).
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](tc) is True
    for cap in ("phase_advance", "deadline", "evidence", "claim"):
        assert MUST_HAVE_CAPABILITIES[cap]["na"](tc) is False, cap
    # And behaviour must AGREE: the four GREEN cells actually light up.
    for cap in ("phase_advance", "deadline", "evidence", "claim"):
        lit = MUST_HAVE_CAPABILITIES[cap]["probe"](
            row["project"](), "account", row["target_id"], row["work_item_id"])
        assert lit, f"account {cap} cell did not light up"


def test_completion_gate_cell_is_behavioural_not_label_only(monkeypatch):
    # ms-142 e-5254: the completion-gate cell must verify the terminal ban ACTUALLY
    # FIRES, not merely that a gate LABEL is declared (DECLARATION≠ENFORCEMENT — the
    # drift-checker target_state.py promised itself). Canary: UNWIRE a real class's
    # ban (empty its routed_states so set_target_state no longer refuses a terminal)
    # while KEEPING its completion_gate label. A pre-e-5254 label-only probe stayed
    # green; the behaviour probe must flip to False (an EMPTY cell = CI fail).
    import target_state as ts  # noqa: E402
    row = TARGET_CLASSES["milestone"]
    probe = MUST_HAVE_CAPABILITIES["completion_gate"]["probe"]
    # with the real (ban-wired) model the cell lights up.
    assert probe(row["project"](), "milestone",
                 row["target_id"], row["work_item_id"]) is True
    # unwire the ban: same gate label, but no gated states + 'done' made advanceable
    # → set_target_state no longer refuses it.
    broken = dict(ts.BUILTIN_STATE_MODELS["milestone"], routed_states={},
                  advanceable_states=("todo", "in_progress", "done"))
    monkeypatch.setitem(ts.BUILTIN_STATE_MODELS, "milestone", broken)
    # the LABEL is still present (a pre-e-5254 probe would pass it green)…
    assert ts.completion_gate_for(ts.state_model_for(None, "milestone")) is not None
    # …but the BEHAVIOUR probe now fails to light up (the ban did not fire).
    assert probe(row["project"](), "milestone",
                 row["target_id"], row["work_item_id"]) is False


def test_account_completion_gate_na_is_driven_by_never_terminal_declaration():
    # Canary (leader caution 2): the account completion-gate N/A must come from the
    # DECLARATION (state_model.never_terminal), not an account-name hardcode. Force
    # never_terminal off on a copy of the manifest tc and the N/A predicate must flip
    # to False — which (since the completion_gate probe does NOT light up for account)
    # would make the cell an EMPTY silent gap = CI fail. This proves the declaration
    # is what holds the cell N/A, and that forcing account non-N/A breaks the matrix.
    project = TARGET_CLASSES["account"]["project"]()
    tc = _manifest_tc(project, "account")
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](tc) is True
    forced = dict(tc)
    forced["state_model"] = dict(tc["state_model"], never_terminal=False)
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](forced) is False, (
        "forcing account to never_terminal=False must drop its completion-gate N/A "
        "(the cell would then be a silent gap) — the N/A is declaration-driven.")
    # The predicate is GENERAL, not account-specific: a terminal class (operation)
    # is never_terminal=False, so its completion-gate cell is (correctly) non-N/A.
    op_tc = _manifest_tc(TARGET_CLASSES["operation"]["project"](), "operation")
    assert MUST_HAVE_CAPABILITIES["completion_gate"]["na"](op_tc) is False
