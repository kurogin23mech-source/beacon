"""引き継ぎ (session log の summary) の節を、本文のまま取り出す (ms-160 e-7314).

## なぜこの module が在るか

前のセッションが「次セッション最優先」として明記した内容が、次の起動で **要約されて
流れる** 取りこぼしが繰り返し起きていた。利用者から「ちょいちょいこういうことが
起こる」と指摘された。原因は注意不足ではなく、**失われやすい経路に置いてあった**こと:

- 読む口は `beacon session log list --json` しか無く、これは **全セッション分**を返す
  (実測 111 件)。最新 1 件だけを取る旗が無く、`show` は id 必須で、その id を知るには
  list が要る。
- 文脈が溢れるので、読む側は必ず切り出す (先頭 1 要素を取る / grep / 途中打ち切り)。
- 肝心の節は **数百字しかない**のに、その切り出しの最中に言い換えに化ける。
- Skill は「AI が文脈で抽出せよ」と散文で頼むだけで、本文のまま届いたかを見る機構が
  無かった。

**切り出す必要が無い大きさで返せば、言い換えが起きる余地が無い。** だからこの module は
「節だけを本文のまま返す」ことに閉じる。要約も整形もしない。

## 見出しは 1 箇所で決める

以前は Skill の散文が「次セッション最優先 / 次にやること / top of queue …」と**例示**
していただけで、書き口 (session-end) と読み口 (session-start) が同じ語に縛られて
いなかった。狭い口だけ作って揺れを許すと、同じ問題が「拾えない」側へ移る。
``REQUIRED_HEADING`` / ``OPTIONAL_HEADINGS`` が唯一の定義で、両 Skill の文面が
これと食い違っていないことは tests/test_session_handoff.py が機械で突き合わせる。
"""
from __future__ import annotations

import re

#: 引き継ぎに **必ず** 在るべき節。session-end がこの見出しで書き、session-start が
#: この見出しで読む。揺れを許さないのは、揺れた瞬間に「拾えない」= 黙って空になる
#: 経路が生まれるため (空を「無かった」と「拾えなかった」に畳まないため、無いときは
#: 無いと言う)。
REQUIRED_HEADING = "次セッション最優先"

#: 在れば一緒に出す節。無くても欠落として扱わない。
OPTIONAL_HEADINGS = ("次にやること",)

#: 見出し行。`#`〜`###` と前後の空白を許す (書き手が `##` で書くのが既定だが、
#: 階層をひとつ変えただけで拾えなくなるのは脆すぎる)。level を捕獲するのは、
#: 節の終わりを「同じか上の階層の見出し」で決めるため。
_HEADING_RE = re.compile(r"^(#{1,3})\s*(.+?)\s*$")

#: コードブロックの囲い (``` / ~~~)。**囲いの中は見出しとして読まない。**
#:
#: ここを見落として本文を失った (独立保守性レビューが実測): 引き継ぎにコマンド例を
#: 書くのは日常的で、その中の `# コメント` が見出し (level 1) と判定されると、
#: **節がそこで打ち切られて以降が黙って消える**。この module が防ぐために在る
#: 「引き継ぎが黙って失われる」を、別の扉から再導入していた。
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _heading_matches(title: str, wanted: str) -> bool:
    """見出しが `wanted` の節かを判定する。**注記つきの書式を受ける。**

    厳密一致にしていたら、**実データの 82% を取りこぼしていた** (独立 AX レビューが
    本番の引き継ぎ 111 件に通して実測: 拾えたのは 20 件、うち 39 件は
    「次セッション最優先 (top of queue)」「次セッション最優先 / top of queue」の
    ように、旧 Skill が推奨していた書式そのもの)。しかも拾えなかったものを
    「書き残していない」と断定して表示していた —— 取りこぼしを直すための機能が、
    別の形で取りこぼしを再生産していた。

    受けるのは「`wanted` で始まり、続きが語の続きでない」もの。
    `次セッション最優先 (top of queue)` / `… / top of queue` / `… — 補足` は受け、
    `次セッション最優先事項` のような別の語は受けない。
    """
    t = title.strip()
    if t == wanted:
        return True
    if not t.startswith(wanted):
        return False
    rest = t[len(wanted):]
    return bool(rest) and not rest[0].isalnum()


def _sections(text: str) -> "list[tuple[int, str, str]]":
    """``(level, 見出し, 本文)`` を文書順に返す。本文は **そのまま** (整形しない)。

    コードブロックの中は見出しとして読まない (`_FENCE_RE` の理由を参照)。
    """
    lines = (text or "").splitlines()
    fenced = False
    marks: list[tuple[int, int, str]] = []   # (行番号, level, 見出し)
    for i, ln in enumerate(lines):
        if _FENCE_RE.match(ln):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING_RE.match(ln)
        if m:
            marks.append((i, len(m.group(1)), m.group(2).strip()))

    out: list[tuple[int, str, str]] = []
    for idx, (line_no, level, title) in enumerate(marks):
        # 節の終わりは「同じか上の階層」の次の見出し。下位の見出し (### 等) は
        # 本文の一部として残す — 書き手が節の中で小見出しを使うのは普通で、
        # そこで切ると本文が尻切れになる。
        end = len(lines)
        for nxt_line, nxt_level, _t in marks[idx + 1:]:
            if nxt_level <= level:
                end = nxt_line
                break
        body = "\n".join(lines[line_no + 1:end]).strip("\n")
        out.append((level, title, body))
    return out


def extract_handoff(summary: str) -> dict:
    """引き継ぎ本文から節を取り出す (純関数)。

    返り値::

        {
          "required": {"heading": str, "body": str, "occurrences": int} | None,
          "optional": [{"heading": str, "body": str, "occurrences": int}, ...],
          "missing":  [str, ...],          # 見つからなかった必須の見出し
          "near_miss": [str, ...],         # 語は在るが節として読めなかった見出し行
        }

    ``required`` が ``None`` のときは ``missing`` に見出し名が入る。**空文字列を
    返して「無い」と「拾えなかった」を同じ形に畳まない** — 呼ぶ側がその区別を
    利用者に見せられるようにする。``near_miss`` が非空なら「書き残していない」では
    なく「読めなかった」であり、**断定してはならない**。

    同じ見出しが 2 回以上現れたら **全部を文書順に連ねる** (後勝ちで前を捨てない)。
    捨てると、この module が防ぐはずの「引き継ぎが黙って失われる」が起きる。
    ``occurrences`` に回数が入るので、呼ぶ側はそれを言える。
    """
    secs = _sections(summary)

    def _collect(wanted: str):
        bodies = [b for _lvl, t, b in secs if _heading_matches(t, wanted)]
        if not bodies:
            return None
        return {"heading": wanted, "body": "\n\n".join(bodies),
                "occurrences": len(bodies)}

    req = _collect(REQUIRED_HEADING)
    missing: list[str] = [] if req else [REQUIRED_HEADING]
    optional = [s for s in (_collect(h) for h in OPTIONAL_HEADINGS) if s]

    near_miss: list[str] = []
    if not req:
        # 語は在るのに節として読めなかったものを拾う。これが在るなら「無い」と
        # 言ってはならない (読めなかっただけ)。
        near_miss = [t for _lvl, t, _b in secs
                     if REQUIRED_HEADING in t and not _heading_matches(
                         t, REQUIRED_HEADING)]
    return {"required": req, "optional": optional, "missing": missing,
            "near_miss": near_miss}


def format_handoff(entry: dict, extracted: dict) -> str:
    """利用者 (= 次のセッション) に見せる文面。**本文はそのまま貼る。**

    ``entry`` は session log の 1 件 (session_id / last_aggregated_at を読む)。
    """
    sid = entry.get("session_id", "?")
    when = entry.get("last_aggregated_at", "") or "?"
    lines = [f"前セッションの引き継ぎ ({sid} / {when}):", ""]
    req = extracted.get("required")
    if req:
        lines.append(f"## {req['heading']}")
        if req.get("occurrences", 1) > 1:
            lines.append(f"(この見出しは {req['occurrences']} 回現れたので、"
                         f"全部を順に並べています)")
        lines.append(req["body"])
    elif extracted.get("near_miss"):
        # **断定しない。** 書いてあるのに読めなかった側。
        lines.append(f"⚠ 「{REQUIRED_HEADING}」の節を読めませんでした "
                     f"(書かれてはいますが、見出しの書式が想定と違います)。")
        for t in extracted["near_miss"]:
            lines.append(f"    実際の見出し: ## {t}")
        lines.append(f"  → 本文は `beacon session log show {sid}` で読めます。")
    else:
        for h in extracted.get("missing", []):
            lines.append(f"⚠ この引き継ぎに「{h}」節がありません "
                         f"(見出しの語も見当たりません —— 前セッションが"
                         f"書き残していない可能性が高いですが、"
                         f"`beacon session log show {sid}` で本文を確かめられます)。")
    for sec in extracted.get("optional", []):
        lines.append("")
        lines.append(f"## {sec['heading']}")
        lines.append(sec["body"])
    return "\n".join(lines)
