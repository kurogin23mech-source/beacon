"""code_graph_curate.py — curated 層 (契約 / 意図) の書き込み口 (ms-156 e-5542)

SPEC 方針3/4: 機械が導出できる構造 (import/call/route) は自動導出に任せ、人が書くのは
「隣人への契約・この module の意図・なぜこの継ぎ目か」だけ。ここはその **curated セル
(role / contract / guard_test) を人手で node に書き込む** 純粋関数群。

格納は Beacon table-doc (node 表) なので、書き込みは該当 module の行の cell を
``table_doc.set_cell`` で更新する (append-only 履歴が残る = data-immutability-principle)。
機械層 (seam / depends-on) は触らない — curated と機械層の分界を書き込み口でも守る。
取得 / 保存 (cloud) は ``scripts/graph-curate.py`` が担い、ここは model 変換に徹する。
"""

from __future__ import annotations

import table_doc

# 人手で書ける curated cell (機械層の seam / governs は含めない)。
CURATED_KEYS = ("role", "contract", "guard_test")


class CurateError(ValueError):
    """対象 module が node 表に無い等、curate できないときに送出。"""


def _find_row_id(node_table: dict, module_id: str) -> str | None:
    for row in table_doc.active_rows(node_table):
        if (row.get("cells", {}).get("id") or "") == module_id:
            return row.get("id")
    return None


def set_curated(node_table: dict, module_id: str, updates: dict, *,
                actor: str, at: str) -> list[str]:
    """``module_id`` の行の curated cell を ``updates`` で更新する (in-place)。

    ``updates`` は ``{key: value}`` で key は ``CURATED_KEYS`` のみ許す (機械層セルを
    curate 経路から触らせない)。実際に値が変わった key の一覧を返す (履歴を汚さない
    ため、現状と同じ値は set しない)。対象 module が無ければ ``CurateError``。
    """
    unknown = set(updates) - set(CURATED_KEYS)
    if unknown:
        raise CurateError(
            f"curated 層で書けないセルです: {', '.join(sorted(unknown))} "
            f"(書けるのは {', '.join(CURATED_KEYS)} のみ。seam/depends-on は機械層)")

    row_id = _find_row_id(node_table, module_id)
    if row_id is None:
        raise CurateError(f"node 表に module がありません: {module_id}")

    row = table_doc.get_row(node_table, row_id)
    current = row.get("cells", {})
    changed: list[str] = []
    for key, value in updates.items():
        value = value or ""
        if (current.get(key) or "") == value:
            continue  # 同値は履歴を汚さない
        table_doc.set_cell(node_table, row_id, key, value, actor=actor, at=at)
        changed.append(key)
    return changed


def curated_view(node_table: dict, module_id: str) -> dict:
    """``module_id`` の現在の curated セル (＋機械層の参照) を読み出す。"""
    row_id = _find_row_id(node_table, module_id)
    if row_id is None:
        raise CurateError(f"node 表に module がありません: {module_id}")
    cells = table_doc.get_row(node_table, row_id).get("cells", {})
    return {
        "id": module_id,
        "role": cells.get("role", ""),
        "contract": cells.get("contract", ""),
        "guard_test": cells.get("guard_test", ""),
        # 参考 (機械層、read-only):
        "seam": cells.get("seam", ""),
        "governs": cells.get("governs", ""),
    }
