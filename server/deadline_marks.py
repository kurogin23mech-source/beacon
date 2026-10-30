"""締切リマインドの「読む / 印を刻む」の、保存層に依らない共通部分 (ms-185 e-7066)。

保存層は 3 つ (MySQL / Firestore / DynamoDB) あり、**どれも同じ 2 つの口を提供する**:

    get_project_deadline_scope(project_id) -> dict | None
    save_deadline_reminder_marks(project_id, marks, now_iso) -> int

MySQL だけは v3 (行分割) のため専用の実装を持つ (Target 行は全部 / 子行は締切を
持つものだけ読み、触れた行だけ書く)。1 文書で持つ他の 2 つは、この module の
whole-doc 実装をそのまま使う。

**なぜ「ある backend にだけ足す」をやめたか**: 最初は MySQL にだけ置いて、呼び出し側が
``getattr`` で有無を見て経路を切り替えていた。ところが store_router は 1 プロセスに
1 つしか無い可変 module で、別のテストが backend を切り替えて読み込み直すと、
**呼び出し側が意図しない経路が選ばれる**。実際それで、保存層を差し替えたはずの試験が
本物の MySQL を見に行って落ちた。口が全 backend に揃っていれば分岐そのものが無くなり、
差し替えた口が必ず使われる。保存層の非対称を禁じる既存のガード
(tests/test_store_backend_parity.py) が最初にそう言っていた通り。
"""

from __future__ import annotations


def mark_key(target_id: str, item: dict) -> tuple:
    """印を宛てる鍵 ``(target_id, item_id)`` の **唯一の決め方** (ms-185 e-7066)。

    印を付ける側 (発火時に「この項目に知らせた」と控える) と、刻む側 (保存層が
    行を読み直して印を書く) が**別のファイルに居る**。両者の鍵の作り方がずれると、
    常に不一致になって印が 1 つも刻まれず、同じ締切で毎分知らせ続ける。ずれても
    どちらのファイルも単体では正しく見えるので、決め方をここ 1 つに置く。
    """
    return (target_id or "", (item or {}).get("id", ""))


def iter_mark_pairs(doc: dict):
    """``((target_id, item_id), item)`` を、締切を持ちうる全項目について列挙する。

    列挙そのものは職種非依存の ``occupation.iter_deadline_candidates`` に委ねる
    (= ``milestones`` / ``entries`` / ``activities`` といった具体名をここに書かない)。
    """
    import occupation  # noqa: PLC0415
    for cand in occupation.iter_deadline_candidates(doc):
        item = cand.get("item") or {}
        yield mark_key(cand.get("target_id", ""), item), item


def stamp_whole_doc(doc: dict, marks: list, now_iso: str) -> int:
    """1 文書で持つ保存層向け: ``doc`` を その場で書き換えて、刻めた件数を返す。

    呼び出し側は戻り値が 0 でなければ ``doc`` を保存する (0 なら書かない =
    変わっていないものを書き戻さない)。
    """
    import deadline  # noqa: PLC0415
    return deadline.stamp_marks(iter_mark_pairs(doc), marks, now_iso)


def save_marks_whole_doc(doc: dict, marks: list, now_iso: str, save) -> int:
    """1 文書で持つ保存層向けの書き手の本体。刻めた件数を返す。

    各 backend は「自分の ``get_project`` で読んだ doc」と「自分の ``save_project``」を
    渡す。ここで closure に関数を束縛して返す形 (= 工場) にはしない —— import 時に
    関数オブジェクトを捕まえてしまい、その module の関数を差し替えた試験から
    見えなくなるため。各 backend 側で module 大域として呼べば遅延解決される。
    """
    if doc is None:
        return 0
    stamped = stamp_whole_doc(doc, marks, now_iso)
    if stamped:
        save(doc)
    return stamped
