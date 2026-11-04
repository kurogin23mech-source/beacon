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
_HEADING_RE = re.compile(r"^(#{1,3})\s*(.+?)\s*$", re.M)


def _sections(text: str) -> "list[tuple[int, str, str]]":
    """``(level, 見出し, 本文)`` を文書順に返す。本文は **そのまま** (整形しない)。"""
    out: list[tuple[int, str, str]] = []
    marks = list(_HEADING_RE.finditer(text or ""))
    for i, m in enumerate(marks):
        level = len(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        # 節の終わりは「同じか上の階層」の次の見出し。下位の見出し (### 等) は
        # 本文の一部として残す — 書き手が節の中で小見出しを使うのは普通で、
        # そこで切ると本文が尻切れになる。
        end = len(text)
        for nxt in marks[i + 1:]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        out.append((level, title, text[start:end].strip("\n")))
    return out


def extract_handoff(summary: str) -> dict:
    """引き継ぎ本文から節を取り出す (純関数)。

    返り値::

        {
          "required": {"heading": str, "body": str} | None,
          "optional": [{"heading": str, "body": str}, ...],
          "missing":  [str, ...],          # 見つからなかった必須の見出し
        }

    ``required`` が ``None`` のときは ``missing`` に見出し名が入る。**空文字列を
    返して「無い」と「拾えなかった」を同じ形に畳まない** — 呼ぶ側がその区別を
    利用者に見せられるようにする。
    """
    found = {title: body for _lvl, title, body in _sections(summary)}
    req = None
    missing: list[str] = []
    if REQUIRED_HEADING in found:
        req = {"heading": REQUIRED_HEADING, "body": found[REQUIRED_HEADING]}
    else:
        missing.append(REQUIRED_HEADING)
    optional = [
        {"heading": h, "body": found[h]}
        for h in OPTIONAL_HEADINGS
        if h in found
    ]
    return {"required": req, "optional": optional, "missing": missing}


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
        lines.append(req["body"])
    else:
        for h in extracted.get("missing", []):
            lines.append(f"⚠ この引き継ぎに「{h}」節がありません "
                         f"(前セッションが書き残していない — 空だったのではなく、"
                         f"節そのものが無い)。")
    for sec in extracted.get("optional", []):
        lines.append("")
        lines.append(f"## {sec['heading']}")
        lines.append(sec["body"])
    return "\n".join(lines)
