"""Back-office occupation seed — the first data-defined occupation (ms-122
e-3958). Proves the descriptor path (target_descriptor + target_engine +
occupation registry) end-to-end and supersedes the ms-121 milestone-流用 stub:
where ms-121 loaded back-office by REUSING the development milestone container
(a throwaway bridge), this defines back-office purely as descriptor DATA, so its
target-classes carry their own fields and phases with no code container.

Shipped as a starter set for ``beacon init --profession backoffice``. It is a
DEFAULT, not a fixed schema: a project owner edits ``target_classes`` in
project.json to add fields / phases / whole classes without touching Beacon
code (that is the entire point of ms-122). The four classes below cover the
common back-office work shapes:

  - 契約 (contract): a finite deal that moves 起草 → 法務レビュー → 締結. The
    法務レビュー phase surfaces fields that only matter there (レビュー依頼先 /
    想定リスク) — the per-phase field extension of SPEC §4.
  - 評価 (evaluation): a finite review cycle 目標設定 → 中間 → 期末.
  - 月次決算 (monthly_close): an ongoing (persistent) monthly task that recurs,
    moving 集計 → レビュー → 確定 each month.
  - 勤怠ウォッチ (attendance_watch): an ongoing monitor with no phases (the
    operation-like shape — a thing you keep an eye on, not a thing you finish).

This module holds only data; the mechanics live in ``target_engine`` and the
registry wiring in ``occupation``.
"""

from __future__ import annotations


BACKOFFICE_PROFESSION = "backoffice"


BACKOFFICE_TARGET_CLASSES = [
    {
        "kind": "contract",
        "label": "契約",
        "profession": BACKOFFICE_PROFESSION,
        "type": "single-shot",
        "id_prefix": "ctr-",
        "collection": "contracts",
        "decomposition": {"id_field": "id", "arms": []},
        "fields": [
            {"key": "counterparty", "label": "相手方", "type": "string",
             "required": True},
            {"key": "amount", "label": "契約金額", "type": "money"},
        ],
        "phases": [
            {"key": "drafting", "label": "起草"},
            {"key": "legal_review", "label": "法務レビュー",
             "fields": [
                 {"key": "reviewer", "label": "レビュー依頼先", "type": "string"},
                 {"key": "risk", "label": "想定リスク", "type": "text"},
             ]},
            {"key": "signed", "label": "締結", "terminal": True},
        ],
    },
    {
        "kind": "evaluation",
        "label": "評価",
        "profession": BACKOFFICE_PROFESSION,
        "type": "single-shot",
        "id_prefix": "ev-",
        "collection": "evaluations",
        "decomposition": {"id_field": "id", "arms": []},
        "fields": [
            {"key": "subject", "label": "対象者", "type": "string",
             "required": True},
        ],
        "phases": [
            {"key": "goal_setting", "label": "目標設定"},
            {"key": "mid_review", "label": "中間レビュー"},
            {"key": "final", "label": "期末評価", "terminal": True},
        ],
    },
    {
        "kind": "monthly_close",
        "label": "月次決算",
        "profession": BACKOFFICE_PROFESSION,
        "type": "persistent",
        "id_prefix": "mc-",
        "collection": "monthly_closes",
        "decomposition": {"id_field": "id", "arms": []},
        "fields": [
            {"key": "period", "label": "対象月", "type": "string",
             "required": True},
        ],
        "phases": [
            {"key": "aggregating", "label": "集計"},
            {"key": "review", "label": "レビュー"},
            {"key": "closed", "label": "確定", "terminal": True},
        ],
    },
    {
        "kind": "attendance_watch",
        "label": "勤怠ウォッチ",
        "profession": BACKOFFICE_PROFESSION,
        "type": "persistent",
        "id_prefix": "aw-",
        "collection": "attendance_watches",
        "decomposition": {"id_field": "id", "arms": []},
        "fields": [
            {"key": "department", "label": "対象部署", "type": "string"},
        ],
        "phases": [],
    },
]


def build_backoffice_project(name: str, objective: str = "", *,
                             retro_day: str = "monday",
                             disclosure_policy: dict | None = None) -> dict:
    """Build a new back-office project dict: the shared top-level fields plus the
    seeded ``target_classes``. Carries ``milestones: []`` so the shared
    ``core.validate_project`` passes unchanged, and ``profession: "backoffice"``
    so the occupation registry projects its descriptor targets. Descriptors are
    deep-copied so callers editing one project don't mutate the module seed."""
    import copy
    project = {
        "name": name,
        "objective": objective,
        "profession": BACKOFFICE_PROFESSION,
        "milestones": [],
        "retro_day": retro_day,
        "target_classes": copy.deepcopy(BACKOFFICE_TARGET_CLASSES),
    }
    if disclosure_policy is not None:
        project["disclosure_policy"] = disclosure_policy
    return project
