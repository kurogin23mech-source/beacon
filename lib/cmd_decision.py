#!/usr/bin/env python3
"""cmd_decision.py — the `beacon decision *` command family (ms-154 e-5594).

The CLI path for recording a decision-arm event to the server's unified
``decision_events`` stream. Used by the /beacon-log log-time backstop (= AI
adversarially self-reports the non-trivial decisions a commit embodied) and, in
general, by any caller that wants to leave an auditable "誰が/なぜ/何を根拠に"
record without going through a dedicated mutating route.

The decision stream is server-side (cloud), so these verbs are cloud-only: in
local mode there is no stream to append to, and the command says so and exits
cleanly (= it never hard-fails a caller like the log Skill). Depends only on
commands_shared (upward) + leaf modules — acyclic (SPEC 方針4).
"""

import json
import os
import sys

from commands_shared import _is_cloud_mode, _get_api_client

# ms-154 e-5652: decided_by 語彙は decision_vocab.DECIDED_BY が単一ソース。CLI 側でも
# 語彙外を弾いて server 400 を待たず早期に気付けるようにするが、語彙の定義は server と
# 共有する (旧: 二重定義していた = 片方だけ増やすと silent に割れる §2 SSoT 違反)。
from decision_vocab import DECIDED_BY as _DECIDED_BY  # noqa: F401
# ms-166 e-6633: 種別の既知集合も同じ単一ソースから引く (server/ は CLI の import
# 経路に無いので、e-6633 で lib/decision_vocab.py へ移設した)。
from decision_vocab import KNOWN_DECISION_KINDS as _KNOWN_KINDS


def _split_evidence(raw: str) -> list:
    """改行区切りの evidence link を list に。空行は落とす。"""
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _parse_limit(raw: str) -> int:
    """--limit を正の整数に。未指定は 100。不正値は exit 1 (ms-154 e-5649).

    旧実装は ``int(limit) if limit.isdigit() else 100`` で、``--limit abc`` が
    silently 100 に fallback していた (= 要求とは別の page 数を返しながら成功に
    見える silent 破壊、audit する側は検知不能)。非整数 / 非正は明示エラーで
    落とす (= argparse の type=int が返す拒否と対称)。
    """
    if not raw:
        return 100
    try:
        value = int(raw)
    except ValueError:
        print(f"Error: --limit must be an integer (got {raw!r})", file=sys.stderr)
        sys.exit(1)
    if value < 1:
        print(f"Error: --limit must be a positive integer (got {value})",
              file=sys.stderr)
        sys.exit(1)
    return value


def cmd_decision_record():
    kind = os.environ.get("BEACON_DECISION_KIND", "").strip() or "log-backstop"
    what = os.environ.get("BEACON_DECISION_WHAT", "").strip()
    rationale = os.environ.get("BEACON_DECISION_RATIONALE", "").strip()
    # ms-166 e-6603 (2): 帰属を **session-kind から機械決定** する。旧実装は既定が
    # "autonomous-AI" の固定文字列で、人間端末から打った判断まで「人間未確認の AI 単独
    # 決定」として残っていた (実データで帰属が逆)。導出は commands_shared の
    # decided_by_for_review (= 人間端末なら human-delegated、そうでなければ
    # autonomous-AI) を再利用する — 同じ写像を 2 つ目のコピーとして書かない。
    # --decided-by の明示指定は従来どおり勝つ (= 呼び出し側が判断主体を知っている場合)。
    #
    # 「明示指定だったか」は **1 回だけ** 読んで保持する (独立レビュー 保守性 M-2)。
    # 後段の開示表示で同じ env を再読みすると、読み方を変えたとき表示だけ食い違う。
    from commands_shared import decided_by_for_review
    explicit_decided_by = os.environ.get("BEACON_DECISION_DECIDED_BY", "").strip()
    decided_by = explicit_decided_by or decided_by_for_review()
    evidence = _split_evidence(os.environ.get("BEACON_DECISION_EVIDENCE", ""))
    related_task = os.environ.get("BEACON_DECISION_RELATED_TASK", "").strip()
    # 対象 (related.target_id) を **明示指定** する口。読み側の
    # `beacon decision list --target` と対になる書き側で、2 つの機能が独立に同じ旗を
    # 必要とした (ms-166 e-6602 + e-6603):
    #   - e-6602 (完遂の冪等): 冪等判定が related.target_id を鍵に含むので、この経路から
    #     completion-verdict を記録するには対象を立てられる必要がある。無いと下の
    #     「既に記録済み」表示が構造的に到達不能だった。
    #   - e-6603 (帰属・対象の機械決定): 本文からの導出に対して明示指定を優先させるため。
    #     無いと「本文の言い回しを変える」以外に曖昧を解消する手段が無かった。
    related_target = os.environ.get("BEACON_DECISION_RELATED_TARGET", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not what:
        print("Error: --what (the decision made) is required", file=sys.stderr)
        sys.exit(1)
    # ms-166 e-5986 独立レビュー AX-2: 明示指定の対象も本文解決と同じ prefix 表で検証する。
    # 検証しないと綴り違いや別種の id が「成功」表示のまま書き込まれ、`decision list
    # --target <正しい id>` で二度と見つからない行が残る (= 記録はあるのに辿れない)。
    if related_target:
        import decision_derive as _dd_check
        if not _dd_check.is_known_target_id(related_target):
            import work_model as _wm_check
            print(f"Error: --related-target {related_target!r} は対象 id に見えません "
                  f"(対象 id は {', '.join(_wm_check.known_target_prefixes())} "
                  f"のいずれかで始まります。例: ms-166 / opp-3)。"
                  f"タスクに紐づけたいなら --related-task を使ってください",
                  file=sys.stderr)
            sys.exit(1)
    if decided_by not in _DECIDED_BY:
        print(f"Error: --decided-by must be one of {sorted(_DECIDED_BY)}",
              file=sys.stderr)
        sys.exit(1)
    # decided_by を立てるなら evidence 必須 (= server の schema 不変条件を CLI で先取り)。
    if not evidence:
        print("Error: --evidence is required (a first-class decision must link "
              "its grounds; give a commit hash / file:line / url)", file=sys.stderr)
        sys.exit(1)

    if not _is_cloud_mode():
        # local mode には決定ストリームが無い。呼び出し側 (log Skill 等) を壊さない
        # よう、明示メッセージを出して正常終了する。
        print("decision stream は cloud プロジェクトのみ (local mode では記録しません)")
        return

    payload = {
        "kind": kind,
        "decision": what,
        "decided_by": decided_by,
        "evidence": evidence,
    }
    if rationale:
        payload["rationale"] = rationale
    related = {}
    if related_task:
        related["task_id"] = related_task
    # ms-166 e-6603 (1): 本文に書かれている対象 id を target に解決する。これが無いと
    # 判断記録が「どの対象の話か」を持たず、`decision list --target` に一件も載らない
    # (記録はあるのに辿れない)。曖昧なときは推測せず空のまま残す。
    import decision_derive as _dd
    derived_target = _dd.resolve_target_from_text(what, rationale)
    # ms-166 e-6852: **構造的な手がかりが自由文のスクレイピングに勝つ**。
    # 旧実装は「明示指定 (--related-target) > 本文からの導出」の 2 段しかなく、
    # ``--related-task`` で作業項目を明示して渡しても対象の決定に一切使われていなかった。
    # 本文に別の対象 id が 1 件だけ出ているとそちらが確実に勝つので、ms-173 の fork が
    # ms-173 の判断を「ms-140 についての判断」として記録してしまった。
    # 最も確実な手がかり (明示的に渡された作業項目の親) が最も弱い手がかりに負けていた。
    task_parent_target, _task_lookup_error = _work_item_parent_target(
        related_task, skip=bool(related_target))
    resolved_target = related_target or task_parent_target or derived_target
    if resolved_target:
        related["target_id"] = resolved_target
    if related:
        payload["related"] = related

    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        result = client.record_decision(project_id, payload)
    except SystemExit:
        raise
    except Exception as exc:
        # ms-166 e-6637 (独立 AX レビュー PR#785 AX-1 系の指摘): 同じ出来事が経路に
        # よって別の診断になってはならない。record_decision の絞り所には
        # prod-test-write ガードが居て、テスト文脈から本番に書こうとすると
        # ProdWriteBlocked を投げる。共有の受け口 (best_effort_decision_write) は
        # これを「ガードが働いた」と報告するのに、この前景コマンドは同じ例外を
        # 「Error: failed to record decision」で包んでいた — 読み手は endpoint の
        # 障害だと受け取って再試行や調査に向かう。分類は例外の型で 1 回決める。
        #
        # ここでは **飲まない**: このコマンドは記録することが目的なので、記録できな
        # かったなら非ゼロで落ちるのが正しい (best-effort の副作用経路とは責務が違う)。
        # 変えるのは「何が起きたか」の説明と次の一手だけ。
        import cloud_write_guard as _cwg
        if isinstance(exc, _cwg.ProdWriteBlocked):
            print(f"Refused: {exc}", file=sys.stderr)
            print("Hint: これは失敗ではなくガードです。テストから本番の判断記録に"
                  "書こうとしています (追記専用なので消せません)。テストを隔離する"
                  "か、本当に本番へ書くなら抜け道を明示してください。",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Error: failed to record decision: {exc}", file=sys.stderr)
        sys.exit(1)

    # 機械が決めた帰属と対象を開示する。**text と json で同じ情報を出す**
    # (独立レビュー AX-2): --json は自動化経路が「何が記録されたか」を確認する正規手段
    # なので、人間向け print にだけ開示を実装すると、この修正が足した情報そのものが
    # 機械の読み手 から消える。
    _attr_source = "明示指定" if explicit_decided_by else "session 種別から導出"
    _target_source = ("明示指定" if related_target
                      else "作業項目の親から解決" if task_parent_target
                      else "本文から解決" if derived_target else "")
    _ambiguous = ([] if resolved_target
                  else [t for t in _dd.target_ids_in_text(what, rationale)])
    if json_mode:
        out = dict(result) if isinstance(result, dict) else {"result": result}
        out["decided_by"] = decided_by
        out["decided_by_source"] = "explicit" if explicit_decided_by else "session-kind"
        out["target_id"] = resolved_target or None
        out["target_id_source"] = ("explicit" if related_target
                                   else "related-task-parent" if task_parent_target
                                   else "text" if derived_target else None)
        if _task_lookup_error:
            # 解けなかったことを黙って本文導出に落とさない (e-6757 と同じ方針):
            # 「なぜ本文が勝ったか」が読み手に見えないと誤記録を疑えない。
            out["related_task_lookup_error"] = _task_lookup_error
        if len(_ambiguous) > 1:
            out["target_candidates"] = _ambiguous
        print(json.dumps(out, ensure_ascii=False))
    else:
        did = result.get("decision_id", "?") if isinstance(result, dict) else "?"
        # 2 つの開示は **排他ではない** (ms-166 e-6602 + e-6603)。「明示または導出で対象が
        # 決まった上で、同じ対象×結論が既に在るので既存行に畳まれた」という状態があり得る
        # ので、追記されたかどうか (e-6602) と、帰属・対象がどう決まったか (e-6603) を
        # 両方出す。片方だけにすると、畳まれた時に対象が見えない / 新規記録の時に
        # 追記の有無が見えない、のどちらかが欠ける。
        if isinstance(result, dict) and result.get("deduplicated"):
            print(f"Decision already recorded [{did}]: {kind} — {what[:60]}")
            print("  この target の同じ判定は既に記録済みのため、追記しませんでした "
                  "(完遂の記録は 1 度だけ残ります)")
        else:
            print(f"Decision recorded [{did}]: {kind} — {what[:60]}")
        print(f"  帰属: {decided_by} ({_attr_source})")
        if resolved_target:
            print(f"  対象: {resolved_target} ({_target_source})")
        if _task_lookup_error:
            # text と --json で同じ情報を出す (独立 AX レビュー PR#785 AX-2)。
            # この開示だけが --json 側にしか無く、既定のテキスト経路で叩いた人は
            # 「作業項目の親を解こうとして失敗し、本文に落ちた」事実に気づけなかった。
            # **自分がすぐ上のコメントに書いた規約を、自分が足した新フィールドが破っていた。**
            print(f"  ⚠ 作業項目の親を解けませんでした ({_task_lookup_error}) — "
                  f"対象は本文からの導出になっています")
        elif len(_ambiguous) > 1:
            print(f"  ⚠ 対象を解決できません — 本文に {', '.join(_ambiguous)} が在り"
                  f"どれの判断か決められません (取り違えを避けて空のまま記録しました)。"
                  f"--related-target <id> で直接指定できます")


def _work_item_parent_target(related_task: str, *, skip: bool = False):
    """``--related-task`` で渡された作業項目の親 target を解く (ms-166 e-6852)。

    返り値は ``(target_id, 読めなかった理由)``。理由を返すのは、解けなかったことを
    黙って本文からの導出に落とすと **「なぜ本文が勝ったか」が読み手に見えない** ため
    (失敗を無言にしないという e-6757 と同じ方針)。呼び出し側が開示に載せる。

    ``skip=True`` (= ``--related-target`` が明示されていて既に勝ちが決まっている) の
    ときはプロジェクトを読まない — 結果に影響しない I/O をしないため。

    規則そのもの (どの target がその作業項目を持つか) は純関数
    ``decision_derive.resolve_target_from_work_item`` が持つ。ここが持つのは
    プロジェクトの読み込み (I/O) だけ。
    """
    if skip or not (related_task or "").strip():
        return "", ""
    try:
        from commands_shared import load_project
        import decision_derive as _dd
        data = load_project()
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    resolved = _dd.resolve_target_from_work_item(data, related_task)
    if resolved:
        return resolved, ""
    # ms-166 e-6819 独立 AX レビュー AX-3: 「存在しない id」と「実在するが作業項目で
    # ない id」が どちらも空文字で、呼び出し側が区別できなかった。PR / commit の id を
    # 誤って渡した人は何の開示もなく本文スクレイピングへ静かに縮退する。型を問わない
    # 存在チェックで切り分けて、理由を開示経路に乗せる。
    if _dd.work_item_id_exists(data, related_task):
        return "", (f"{related_task} は存在しますが作業項目 (task / 活動) では "
                    f"ありません — 作業項目の id を渡すか、対象を --related-target で "
                    f"明示してください")
    return "", f"{related_task} に一致する作業項目がありません"


def _recognized_decision_kinds() -> frozenset:
    """「見覚えのある種別」の集合 (ms-166 e-6633)。

    種別の語彙は **意図的に開いている** (任意の文字列を kind として書ける /
    server/decision_event.py 冒頭の設計方針) ので、これは拒否のための許可リストでは
    なく **綴り違いに気づかせるための既知集合**。3 つを合わせる:

    * ``decision_vocab.KNOWN_DECISION_KINDS`` — seam (コード上の判断地点) に
      溶接された種別。
    * ``capability_ledger.DECISION_CAPTURE_DERIVED_KINDS`` — 既存の成果物から
      導出される種別 (pr-intent = PR の意図から導出)。
    * ``capability_ledger.DECISION_CAPTURE_ADHOC_KINDS`` — 実行時に名付けられた
      種別 (triage = ``beacon decision record`` で人や AI が名付けて書いたもの)。

    後ろ 2 つを足すのが要点。忘れると、本番に実在する pr-intent / triage を
    「知らない種別」と言ってしまい、**警告そのものが嘘になる**。e-6756 で台帳に
    載せた事実をここで消費する形。
    """
    from capability_ledger import (DECISION_CAPTURE_DERIVED_KINDS,
                                   DECISION_CAPTURE_ADHOC_KINDS)
    return frozenset(set(_KNOWN_KINDS)
                     | set(DECISION_CAPTURE_DERIVED_KINDS)
                     | set(DECISION_CAPTURE_ADHOC_KINDS))


def cmd_decision_list():
    """List decisions from the unified stream (ms-154 e-5595).

    The read side for the independent-verification path (別 AI が宣言 rationale を
    実コードに照合する) and for auditing. cloud-only.
    """
    kind = os.environ.get("BEACON_DECISION_KIND", "").strip()
    limit = _parse_limit(os.environ.get("BEACON_DECISION_LIMIT", "").strip())
    # ms-164 e-6030: filter to one session's / one worked-Target's decisions so
    # session-end can reconcile "did THIS session record the judgments it made on
    # THIS target". Applied server-side before the limit window.
    session = os.environ.get("BEACON_DECISION_SESSION", "").strip()
    target = os.environ.get("BEACON_DECISION_TARGET", "").strip()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"

    if not _is_cloud_mode():
        # AX review PR#708: when a --session / --target filter was given, an empty
        # result in local mode must NOT read as "this session recorded no
        # decisions" — the stream simply does not exist locally and the filter was
        # never evaluated. Signal ``filter_applied: false`` (JSON) / a note (text)
        # so a caller can tell "filter ran, 0 matches" from "filter not evaluated".
        filtered = bool(session or target)
        if json_mode:
            out = {"decisions": [], "count": 0}
            if filtered:
                out["filter_applied"] = False
            print(json.dumps(out, ensure_ascii=False))
        else:
            msg = "decision stream は cloud プロジェクトのみ (local mode では記録なし)"
            if filtered:
                msg += " — --session / --target フィルタは適用されていません"
            print(msg)
        return

    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        result = client.list_decisions(project_id, kind=kind, limit=limit,
                                        session=session, target=target)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: failed to list decisions: {exc}", file=sys.stderr)
        sys.exit(1)

    rows = result.get("decisions", []) if isinstance(result, dict) else []
    # ms-166 e-6633: 存在しない種別名を渡しても黙って 0 件が返っていた。綴りを
    # 間違えた人は「この種別の判断は 1 件も無い」と読み、実際には **問い合わせ自体が
    # 的を外していた** ことに気づけない (e-6603 で直した「全部除外されて 0 件」と
    # 同型の、0 件の意味が潰れる病理)。語彙は開いているので **拒否はしない** —
    # 既知集合に無いことを開示するだけ。
    # 2 つの別の事実を混ぜない (独立 AX レビュー PR#783 の AX-1):
    #   _in_vocab — 語彙 / 台帳に宣言済か。**生の事実**。
    #   _suspect  — この応答を疑うべきか = 宣言に無く かつ 0 件。**行動につながる信号**。
    # 初版は生の所属を kind_recognized という 1 つの可否に見える名前で --json に常時
    # 載せ、人間向けには 0 件のときだけ警告していた。結果、宣言に無いが実データがある
    # 種別で「kind_recognized: false なのに decisions が非空」という食い違いが起き、
    # 自動化経路の読み手は **正しいデータを疑って** 綴りを直そうと再試行しうる
    # (まさにこの修正が防ごうとした誤診の裏返し)。信号は 1 箇所で作り、両方の出力面に
    # 同じものを流す。
    _known = _recognized_decision_kinds() if kind else frozenset()
    _in_vocab = (kind in _known) if kind else None
    _suspect = bool(kind) and not _in_vocab and not rows
    if json_mode:
        # text と json で同じ情報を出す (e-6603 独立レビュー AX-2): --json は自動化
        # 経路の正規手段なので、人間向け文言にだけ開示を書くと機械の読み手から消える。
        out = dict(result) if isinstance(result, dict) else {"result": result}
        if kind:
            out["kind_filter"] = kind
            # 生の事実と、行動につながる信号を別の名前で出す。
            out["kind_in_known_vocabulary"] = _in_vocab
            out["kind_filter_suspect"] = _suspect
            out["recognized_kinds"] = sorted(_known)
        print(json.dumps(out, ensure_ascii=False))
        return
    # 既定で外した kind (独立レビュー AX-1): この開示は **0 件のときこそ要る**。
    # 旧実装は `if not rows: print("(決定なし)"); return` が開示より手前にあり、
    # 「全部が除外されて 0 件」と「そもそも判断記録が無い」が同じ文言に潰れていた。
    # 前者を後者と読むと「このプロジェクトには判断記録が無い」と誤って結論する。
    _ex = result.get("excluded_kinds") or [] if isinstance(result, dict) else []
    _ex_note = (f"  (既定では {', '.join(_ex)} を除いています — 通信ログであって判断では"
                f"ないため。見るときは --kind {_ex[0]})") if _ex else ""
    if not rows:
        print("(決定なし)")
        if _suspect:
            # 0 件の理由が「その種別の判断が無い」ではなく「種別名が的を外している」
            # 可能性を示す。断定はしない (語彙は開いているので、本当に新しい種別を
            # 誰かが書き始めた直後という場合もある)。--json の kind_filter_suspect と
            # 同じ信号から出している (= 2 つの面が食い違えない)。
            print(f"  ⚠ 種別 '{kind}' は見覚えのある種別に含まれていません "
                  f"(綴り違いの可能性)。0 件は「この種別の判断が無い」ではなく "
                  f"「問い合わせが的を外している」かもしれません。")
            print(f"  見覚えのある種別: {', '.join(sorted(_recognized_decision_kinds()))}")
        if _ex_note:
            print(_ex_note)
        return
    for r in rows:
        did = r.get("decision_id", "?")
        k = r.get("kind", "?")
        what = r.get("decision", "")
        by = r.get("decided_by") or "?"
        ev = r.get("evidence") or []
        print(f"  [{did}] {k} / {by}: {what}")
        if r.get("rationale"):
            print(f"      なぜ: {r['rationale']}")
        if ev:
            print(f"      根拠: {', '.join(ev)}")
    if _ex_note:
        print(_ex_note)


def cmd_decision_derive():
    """Derive decisions from existing artifacts that already carry the "why"
    (ms-166 e-5972). Currently the ONLY source is **PR intent** — every recorded PR
    that declares an intent AND has a PR number becomes a ``pr-intent`` decision, so
    a change's "why" is a DERIVED product of what was already written (no separate
    ``beacon decision record``). dry-run 既定 / ``--apply`` で書込 / ``--json``。cloud-only.

    Idempotent — PRs already on the arm are skipped by reading existing ``pr-intent``
    decisions (dedup by ``pr:<n>`` in evidence). This holds ONLY when that read
    succeeds and is not truncated at ``DEDUP_SCAN_LIMIT``; if the dedup basis cannot
    be established, ``--apply`` aborts rather than risk duplicates (dry-run degrades
    with a warning). PRs without a number are skipped (no dedup key).

    (task done_reason は task-done seam、採否は review-adjudication、完遂 verdict は
    completion-verdict で既に捕獲済み。commit rationale の導出は別 source = 粒度と
    正規化規則が別物なので未対応。) 会話の純粋内省判断は artifact に書かれていない
    ので導出対象外。"""
    import decision_derive
    from commands_shared import (load_project, decided_by_for_review,
                                 best_effort_decision_write)

    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    # dry-run 既定 (deliverable backfill と同じ安全側): 既存 PR は数百件ありうるので、
    # --apply を明示しない限り「何件導出するか」を報告するだけで書き込まない。
    apply = os.environ.get("BEACON_APPLY", "") == "1"
    if not _is_cloud_mode():
        # 前提未充足も --json では機械可読に返す (AX review: exit 0 + 平文は自動化を誤らせる)。
        if json_mode:
            print(json.dumps({"error": "cloud-only",
                              "message": "decision derive は cloud プロジェクトのみ"},
                             ensure_ascii=False))
        else:
            print("decision derive は cloud プロジェクトのみ "
                  "(local mode では決定ストリームが無い)")
        return
    try:
        client, config = _get_api_client()
        project_id = config.get("project_id", "")
        if not project_id:
            print("Error: no project_id in cloud.json", file=sys.stderr)
            sys.exit(1)
        data = load_project()
        # pr_number を持つ artifact だけが dedup key を持てる = 導出可能。番号無しは
        # 冪等に扱えないので除外して可視化する (AX/保守性 review)。
        derivable = []
        skipped_no_number = 0
        for pr_number, title, intent in decision_derive.iter_pr_intent_artifacts(data):
            if decision_derive.normalize_pr_number(pr_number):
                derivable.append((pr_number, title, intent))
            else:
                skipped_no_number += 1

        # 既存 pr-intent decision を読んで covered set を作る (dedup = 冪等)。冪等が
        # この command の宣言契約なので、その土台の read が壊れたら握りつぶさない:
        # --apply は中止 (重複を撒くくらいなら止める)、dry-run は縮退して警告のみ。
        DEDUP_SCAN_LIMIT = decision_derive.DEDUP_SCAN_LIMIT
        dedup_reliable = True
        try:
            res = client.list_decisions(
                project_id, kind=decision_derive.DERIVED_PR_INTENT_KIND,
                limit=DEDUP_SCAN_LIMIT)
            existing = res.get("decisions", []) if isinstance(res, dict) else []
            if len(existing) >= DEDUP_SCAN_LIMIT:
                dedup_reliable = False  # truncation 疑い → 上限外は covered から漏れる
                print(f"  ⚠ 既存 pr-intent decision が上限 {DEDUP_SCAN_LIMIT} 件に達し "
                      "dedup が不完全な可能性があります。", file=sys.stderr)
        except Exception as exc:
            dedup_reliable = False
            existing = []
            print(f"  ⚠ 既存 decision の読み出しに失敗 (dedup 不能): {exc}", file=sys.stderr)
        if apply and not dedup_reliable:
            print("Error: dedup の前提 (既存 decision の確認) が満たせないため --apply を "
                  "中止しました。冪等性が壊れ重複を撒くのを防ぐためです。時間をおいて "
                  "再試行するか、`beacon decision derive --json` で状態を確認してください。",
                  file=sys.stderr)
            sys.exit(1)

        covered = decision_derive.covered_pr_numbers(existing)
        decided_by = decided_by_for_review()
        to_derive = [
            (pr_number, title, intent)
            for (pr_number, title, intent) in derivable
            if decision_derive.normalize_pr_number(pr_number) not in covered
        ]
        derived = 0
        failed = 0
        if apply:
            for pr_number, title, intent in to_derive:
                payload = decision_derive.build_pr_intent_decision(
                    pr_number, title, intent, decided_by=decided_by)
                if payload is None:
                    continue
                # 失敗契約は正典 seam に一本化 (保守性 M2)。成功/失敗は ok フラグで数える。
                ok = False
                with best_effort_decision_write(f"pr-intent for PR #{pr_number}"):
                    client.record_decision(project_id, payload)
                    ok = True
                if ok:
                    derived += 1
                else:
                    failed += 1
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: failed to derive decisions: {exc}", file=sys.stderr)
        sys.exit(1)

    attempted = len(to_derive)
    already_covered = len(derivable) - attempted
    if json_mode:
        out = {"apply": apply, "already_covered": already_covered,
               "skipped_no_number": skipped_no_number,
               "pr_intent_artifacts": len(derivable) + skipped_no_number}
        if apply:
            out.update({"derived": derived, "failed": failed})
        else:
            out["pending"] = attempted
        print(json.dumps(out, ensure_ascii=False))
    elif apply:
        msg = (f"decision derive: {derived} 件を導出 / {failed} 件失敗 / "
               f"{already_covered} 件は既存済み")
        if skipped_no_number:
            msg += f" / {skipped_no_number} 件は PR 番号欠落で対象外"
        print(msg)
    else:
        msg = (f"decision derive (dry-run): {attempted} 件が導出対象 / "
               f"{already_covered} 件は既存済み")
        if skipped_no_number:
            msg += f" / {skipped_no_number} 件は PR 番号欠落で対象外"
        print(msg + "。実行するには --apply を付けてください。")
