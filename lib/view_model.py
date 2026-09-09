"""ビュー用スキーマ — 盤 (= 今どうなっているかの全体像) を表示層に渡す唯一の形。

ms-170 (= ローカルでもクラウドでも同じ盤が見えるビューワー) の変換層。SPEC
``KIGmqQTIanqUypbtmrTs`` の設計方針 2 を実装する: **データの取得元 (ローカルの
SQLite / クラウド API) を、この層より先に一切漏らさない**。

なぜこの層が薄いか
------------------
データ層は既に取得元を吸収している。``Store`` プロトコル (lib/store.py) の
``load_project()`` は LocalStore / SqliteStore / StoreApi のどれでも **同じ
プロジェクト辞書** を返す (ms-148 = ローカル書き込み一本化 の副産物)。そして
``occupation.project_targets`` / ``root_target.synthesized_projection`` は
その辞書に対する **純粋関数** である。

したがって「ローカルとクラウドで同じ形が出る」ことは、この層が何かを揃えて
いるからではなく、**入力が既に同じ形だから構造的にそうなる**。本モジュールの
仕事は新しい投影を発明することではなく、既存の投影を「表示層に渡す形」として
明示的に固定し、版を付け、盤に必要だが投影に無いもの (ドキュメント / セッション)
を同じ形に並べることだけである。

この層を厚くしてはいけない。ここで取得元ごとの補正を始めた瞬間に、SPEC が
畳もうとしている二重メンテが変換層の中に再建される。

出所 (source) の扱い
--------------------
``source`` は「どこから読んだか」を人間に見せるためのラベルであり、**他の
どのフィールドの中身も変えない**。表示層が ``source`` を見て分岐したら、それは
この層の失敗である (SPEC 受入条件 2)。

純粋性
------
入力辞書を変更しない。I/O を行わない。時刻を読まない。すべて呼び出し側が渡す。
"""

from __future__ import annotations

import occupation
import root_target
import work_model

# ビュー用スキーマの版。表示層はこの版を見て、自分が解釈できる形かを判断する。
# 互換を壊す変更 (フィールドの削除 / 意味の変更) をしたら上げる。追加は上げない。
SCHEMA_VERSION = 1

# 取得元ラベルの許容値。表示のためだけに存在し、他のフィールドに影響しない。
SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"

# プロジェクト見出しとして表示する物語フィールド (root_target が所有する)。
_HEADER_KEYS = ("name", "objective", "summary", "profession")


def _target_row(row: dict) -> dict:
    """職種非依存の Target 行を、表示層が受け取る形に正規化する。

    ``occupation.project_targets`` の行は ``work_items_total`` /
    ``work_items_done`` を平らに持つ。表示層では「消化数」という 1 つの概念なので
    入れ子に畳む。``detail`` は職種固有の付帯情報 (開発なら進捗率と期日、営業なら
    フェーズとボール) で、中身の解釈は表示層に任せるためそのまま通す。
    """
    total = int(row.get("work_items_total") or 0)
    done = int(row.get("work_items_done") or 0)
    return {
        "id": row.get("id", ""),
        "label": row.get("label", ""),
        "status": row.get("status", ""),
        # kind = この Target が何のクラスか (milestone / opportunity / …)。
        # 表示層はこれで見出しの語 (「マイルストーン」/「商談」) を選ぶ。
        "kind": row.get("kind", ""),
        "work_items": {
            "total": total,
            "done": done,
            "open": max(total - done, 0),
        },
        "is_done": work_model.is_done(row),
        "is_open": work_model.is_open(row),
        "detail": dict(row.get("detail") or {}),
    }


def _document_row(doc: dict) -> dict:
    """ドキュメント 1 件を表示層の形に正規化する。

    ローカルはファイルの前書き (frontmatter) 由来、クラウドは API 由来だが、
    ``Store.list_documents()`` が既に同じ辞書の形で返すのでキーを選ぶだけでよい。
    """
    return {
        "id": doc.get("doc_id") or doc.get("id") or "",
        "title": doc.get("title", ""),
        "scope": doc.get("scope", ""),
        "updated_at": doc.get("updated_at", ""),
        # どの Target に紐づくドキュメントか (無ければ空)。
        "target": doc.get("target") or doc.get("milestone") or "",
    }


def _session_row(sess: dict) -> dict:
    """作業セッション 1 件を表示層の形に正規化する。

    セッションはプロジェクト辞書の中に無く、クラウドでは directory API、ローカル
    では手元の記録から来る。**取得は呼び出し側の仕事** で、本モジュールは渡された
    ものを並べるだけ (この層は I/O をしない)。

    Beacon に参加を宣言していないセッション (素の Claude / Codex) をここに載せる
    構想は ms-171 に分けてある。本スキーマは「名乗っているセッション」だけを扱う。
    """
    actor = sess.get("actor") or {}
    focus = (sess.get("focus") or {}).get("milestone") or {}
    health = sess.get("poll_health") or {}
    return {
        "id": sess.get("session_id", ""),
        # 誰の・どのマシンの・何のセッションか。生の識別子は使い捨ての経路
        # トークンなので、人間には who / machine / kind を見せる。
        "who": actor.get("email", ""),
        "machine": actor.get("machine", ""),
        "agent": (sess.get("agent") or {}).get("kind", ""),
        "cwd": sess.get("cwd", ""),
        # このセッションが今どの Target を見ているか (空なら未宣言)。
        "target": focus.get("id", ""),
        "target_label": focus.get("title", ""),
        "live": bool(sess.get("live")),
        "healthy": bool(health.get("healthy")),
        "last_active": sess.get("last_active", ""),
    }


def build_board_view(
    data: dict,
    *,
    source: str,
    project_id: str = "",
    documents=None,
    sessions=None,
) -> dict:
    """盤 1 面分のビューを組み立てる (純粋関数)。

    Args:
        data: ``Store.load_project()`` が返すプロジェクト辞書。ローカル / クラウド
            のどちらの取得元でも同じ形なので、ここで取得元を意識する必要はない。
        source: どこから読んだかのラベル (``SOURCE_LOCAL`` / ``SOURCE_CLOUD``)。
            **表示のためだけ** に載る。他のフィールドの中身には影響しない。
        project_id: クラウドのプロジェクト識別子 (ローカルでは空)。同じく表示用。
        documents: ``Store.list_documents()`` の結果 (省略可)。
        sessions: 作業セッション一覧 (省略可)。取得は呼び出し側の仕事。

    Returns:
        ビュー用スキーマ 1 件。表示層はこの形だけを知っていればよく、
        ``source`` 以外に取得元の痕跡は含まれない。
    """
    projection = root_target.synthesized_projection(data)
    counts = projection.get("counts") or {}

    header = {k: data.get(k, "") for k in _HEADER_KEYS}
    # 職種が未設定なら開発として扱う (status の既定と揃える)。
    header["profession"] = header.get("profession") or "dev"

    return {
        "schema_version": SCHEMA_VERSION,
        "project": header,
        "progress": {
            "total": int(counts.get("total") or 0),
            "done": int(counts.get("done") or 0),
            "open": int(counts.get("open") or 0),
        },
        "targets": [_target_row(r) for r in projection.get("targets") or []],
        # 生み出した価値 (deliverable) の投影。採用している Target クラスが
        # 増えれば自動で寄与が増えるので、ここで列挙し直さない。
        "deliverables": list(projection.get("deliverables") or []),
        "documents": [_document_row(d) for d in documents or []],
        "sessions": [_session_row(s) for s in sessions or []],
        "source": {"kind": source, "project_id": project_id},
    }


def board_view_from_store(store, *, documents=None, sessions=None) -> dict:
    """``Store`` から盤ビューを 1 つ組み立てる薄い包み。

    取得元の判定を ``store.is_cloud()`` の 1 箇所に閉じ込めるためだけに存在する。
    ここ以外に「ローカルかクラウドか」を尋ねる場所を作ってはいけない。
    """
    is_cloud = bool(store.is_cloud())
    project_id = ""
    if is_cloud:
        # StoreApi は識別子を非公開属性で持つ。``Store`` プロトコルには
        # 「自分がどのプロジェクトか」を尋ねる口が無いので、公開属性 → 非公開属性
        # の順に見る。表示用ラベルなので、取れなくても空で通す (盤は成立する)。
        project_id = (getattr(store, "project_id", "")
                      or getattr(store, "_project_id", "") or "")
    return build_board_view(
        store.load_project(),
        source=SOURCE_CLOUD if is_cloud else SOURCE_LOCAL,
        project_id=project_id,
        documents=documents,
        sessions=sessions,
    )
