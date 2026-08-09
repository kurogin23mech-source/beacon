"""Synthetic-profession fixture for the ms-142 completeness harness (e-5011).

A profession built PURELY by a manifest declaration (a target descriptor under
``project.json`` ``target_classes``), with a shape intentionally UNLIKE both dev
and sales so the coverage matrix (e-5012) stresses the abstraction instead of
re-running dev's implicit assumptions ("dev が二重に通るだけ" を避ける):

  * arm names are NOT ``entries`` / ``activities`` — ``duties`` (work items) and
    ``attestations`` (evidence), classified by EXPLICIT arm ROLES on the
    descriptor (not the ``work_items`` / ``evidence`` name convention). This is
    the ms-142 e-5011 stress: an occupation may name its arms anything and still
    light up every arm-walking capability.
  * TWO DISTINCT arms — dev shares one ``entries`` arm for tasks AND commits.
  * its own phase vocabulary (raised → reviewed → discharged) — different from
    sales' funnel and from dev (which has none).
  * its own id prefix (``obl-``).

If this profession flows through ``profession_manifest`` / ``iter_work_items`` /
``iter_deadline_candidates`` identically to dev and sales, the abstraction is
genuinely declaration-driven (the harness's whole point).

NOT prefixed ``test_`` so pytest does not collect it as a test module — it is an
importable fixture shared by e-5011 and the e-5012 coverage matrix.
"""
from __future__ import annotations

SYNTHETIC_PROFESSION = "compliance"

# The manifest that brings the profession into being — nothing else is wired.
SYNTHETIC_DESCRIPTOR = {
    "kind": "obligation",
    "label": "義務",
    "profession": SYNTHETIC_PROFESSION,
    "type": "single-shot",
    "id_prefix": "obl-",
    "collection": "obligations",
    "decomposition": {"id_field": "id", "arms": ["duties", "attestations"]},
    # Explicit arm ROLES — the arms are named nothing like dev/sales, so the
    # manifest CANNOT fall back to the work_items/evidence name convention; it
    # must read these declarations (ms-142 e-5011).
    "work_item_arm": {"arm": "duties", "item_type": None, "kind": "duty"},
    "evidence_arms": [{"arm": "attestations", "item_type": None}],
    "phases": [
        {"key": "raised", "label": "発生"},
        {"key": "reviewed", "label": "審査"},
        {"key": "discharged", "label": "履行", "terminal": True},
    ],
    "fields": [{"key": "authority", "label": "根拠法", "required": False}],
}


def build_synthetic_project(*, duty_overdue: bool = True) -> dict:
    """A compliance project with one obligation Target carrying a duty (work item,
    with a deadline) and an attestation (evidence). ``duty_overdue`` controls
    whether the duty's deadline is in the past (for deadline-enumeration tests)."""
    return {
        "name": "Compliance",
        "profession": SYNTHETIC_PROFESSION,
        "milestones": [],
        "target_classes": [dict(SYNTHETIC_DESCRIPTOR)],
        "obligations": [
            {
                "id": "obl-1",
                "label": "個人情報の年次点検",
                "status": "raised",
                "phase": "raised",
                "occupation": {"session_id": "sv-comp"},
                "duties": [
                    {"id": "duty-1", "description": "点検レポート提出",
                     "deadline": "2026-08-06" if duty_overdue else "2099-01-01",
                     "status": "todo"},
                ],
                "attestations": [
                    {"id": "att-1", "description": "提出済み証跡"},
                ],
            },
        ],
    }
