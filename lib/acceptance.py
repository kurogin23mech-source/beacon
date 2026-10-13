"""受入条件 (acceptance criteria) を節に切り出し、節ごとの判定を扱う (ms-166 e-6898).

なぜ在るか
----------
タスクを done にするとき、そのタスク自身が書いた「Done when」の各節が満たされた
かを、誰も機械で突き合わせていなかった。判定は AI の自己申告だけで通り、未達でも
done にできた。

実例 (2026-10-08 に確定): e-6600 の受入条件は 3 節あり、3 つ目は「規則が work-item
class 共通 (sales/dev で分岐しない)」だった。実際には cancel について開発と営業で
規則が分岐しており、しかも営業の方が厳しい逆向きの非対称が残っていた。実装者自身が
最後のコミットで「全 work-item class で同一の規則」という主張を撤回しており、
撤回したうえで done のままだった。

機械は「節が意味的に満たされたか」を判定できない
--------------------------------------------------
だからこの module は **判定しない**。やるのは次の 2 つだけ:

1. 受入条件の文章を**節に切り出す** (= 何個の主張があるかを数える)
2. 呼び出し側が出した節ごとの判定が、**節を網羅しているか**を検査する

意味の判定は人 / AI が行い、その主張と根拠が記録に残る。残るから後から
「どの節をどう満たしたと主張したか」を引けるし、嘘なら嘘だったと分かる。
これは「正しさの保証」ではなく「主張の記録義務」で、機械で閉じられるのは後者だけ。

節の切り出し方
--------------
実データ 2 形式を測って決めた (``tests/test_acceptance_e6898.py``):

* 番号付き行 (``1. ...`` / ``2) ...``) — e-6918 / e-6937 / e-6923 がこの形
* ``" / "`` 区切りの 1 段落 — e-6911 / e-6898 / e-6899 がこの形

番号付き行が 2 つ以上あればそれを採り、無ければ ``" / "`` で割る。どちらでも
割れなければ全文を 1 節として扱う (= 0 節にはしない。0 節は「検査すべきものが無い」
と読めてしまい、関門の抜け道になる)。
"""

import re
from typing import Dict, List, Optional, Tuple

#: 節ごとに出せる判定。``met`` 以外は「未達の節」として記録に残る。
VERDICTS = ("met", "partial", "not-met")

#: 受入条件そのものが無いタスクを done にしたときに記録する状態。
#:
#: 受入条件が空だからといって関門を素通りさせると、「条件を書かない / 消す」が
#: そのまま抜け道になる (AC 3 項目目)。数えられる状態として必ず残し、
#: ``census_missing_criteria`` が母集団を出せるようにする。
NO_CRITERIA = "no-criteria"

#: 節ごとの判定を出さずに意図的に通したときに記録する状態 (= 債務)。
WAIVED = "waived"

_NUMBERED_LINE = re.compile(r"^\s*(\d+)\s*[.)、]\s*(.+)$")
_BULLET_LINE = re.compile(r"^\s*[-*・]\s+(.+)$")
_INLINE_NUMBER = re.compile(r"\((\d+)\)\s*")


def _normalize(clause: str) -> str:
    """節の中の折り返し (改行 / 連続空白) を 1 スペースに畳む。

    節の切り出し自体は正しくても、原文の折り返しが残ると表示とエラー文が
    読みにくい。意味は変えず見た目だけ整える。
    """
    return re.sub(r"\s+", " ", clause).strip()


def _collect_prefixed_lines(text: str, pattern) -> List[str]:
    """``pattern`` に一致する行を節として集め、続きの行は直前の節に足す。"""
    found: List[str] = []
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            found.append(m.group(m.lastindex).strip())
        elif found and line.strip():
            found[-1] = found[-1] + " " + line.strip()
    return found


def _split_inline_numbers(text: str) -> List[str]:
    """``(1) … (2) …`` 形の行内番号で割る。"""
    marks = list(_INLINE_NUMBER.finditer(text))
    if len(marks) < 2:
        return []
    # 1 番目が先頭近くに無い場合、番号は本文中の参照なので節区切りではない。
    if marks[0].start() > 8:
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        piece = text[m.end():end].strip()
        if piece:
            out.append(piece)
    return out


def _split_outside_parens(text: str, sep: str) -> List[str]:
    """括弧の外に在る ``sep`` だけで割る。

    受入条件の文章は括弧内に補足を持つことが多く (例 ``(どの種別が存在するか /
    綴りの近い候補を添える)``)、素の ``str.split`` はその中の区切りでも割って
    しまう。節を文の途中で切ると、関門は意味を成さない断片への判定を要求する
    ことになるので、深さを見て外側だけで割る。
    """
    opens, closes = "([（「『【", ")]）」』】"
    out, buf, depth, i = [], [], 0, 0
    while i < len(text):
        ch = text[i]
        if ch in opens:
            depth += 1
        elif ch in closes:
            depth = max(0, depth - 1)
        if depth == 0 and text.startswith(sep, i):
            out.append("".join(buf).strip())
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return [p for p in out if p]


def split_clauses(text: str) -> List[str]:
    """受入条件の文章を節に切り出す。空文字なら空リスト。

    実データで観測した 4 形式を、曖昧さの少ない順に試す。どれでも割れなければ
    全文を 1 節として扱う (= 0 節にはしない。0 節は「検査すべきものが無い」と
    読めてしまい、関門の抜け道になる)。
    """
    text = (text or "").strip()
    if not text:
        return []

    for pattern in (_BULLET_LINE, _NUMBERED_LINE):
        found = _collect_prefixed_lines(text, pattern)
        if len(found) >= 2:
            return [_normalize(c) for c in found]

    inline = _split_inline_numbers(text)
    if len(inline) >= 2:
        return [_normalize(c) for c in inline]

    slashed = _split_outside_parens(text, " / ")
    if len(slashed) >= 2:
        return [_normalize(c) for c in slashed]

    return [_normalize(text)]


def clause_count(text: str) -> int:
    return len(split_clauses(text))


def parse_verdict_arg(raw: str, total: int) -> Tuple[int, str, str]:
    """``"2:partial:根拠"`` 形の 1 件を ``(index, verdict, evidence)`` に解く。

    index は 1 始まり (人が読む受入条件の番号と揃える)。

    Raises:
        ValueError: 形が違う / 番号が範囲外 / 未知の判定語。回復できる文面にする。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(
            "受入条件の判定が空です。形は --ac \"<節番号>:<met|partial|not-met>[:<根拠>]\" "
            "(例: --ac \"1:met:commit abc1234 の lib/foo.py で満たした\")")
    bits = raw.split(":", 2)
    if len(bits) < 2:
        raise ValueError(
            f"受入条件の判定 '{raw}' が解けません。形は "
            f"\"<節番号>:<met|partial|not-met>[:<根拠>]\" です "
            f"(節番号は 1 から {total})")
    index_raw, verdict = bits[0].strip(), bits[1].strip()
    evidence = bits[2].strip() if len(bits) > 2 else ""
    if not index_raw.isdigit():
        raise ValueError(
            f"節番号 '{index_raw}' が数字ではありません。1 から {total} の番号で "
            f"どの節への判定かを指してください")
    index = int(index_raw)
    if not 1 <= index <= total:
        raise ValueError(
            f"節番号 {index} は範囲外です (この受入条件は {total} 節)。"
            f"節の数が合わないなら、受入条件の文面を読み直してください")
    if verdict not in VERDICTS:
        raise ValueError(
            f"判定 '{verdict}' は未知です。{' / '.join(VERDICTS)} のいずれかを "
            f"使ってください (partial = 一部達成、not-met = 未達。"
            f"未達でも記録に残せば done にできます — 隠さないことが目的です)")
    return index, verdict, evidence


def build_adjudication(criteria: str, raw_verdicts: List[str]) -> Dict:
    """節ごとの判定をまとめて、記録に入れる形にする。

    Raises:
        ValueError: 判定が節を網羅していない / 同じ節に 2 回出されている /
            個々の判定の形が違う。
    """
    clauses = split_clauses(criteria)
    total = len(clauses)
    if total == 0:
        raise ValueError(
            "受入条件が空のタスクに節ごとの判定は出せません "
            f"(この場合は {NO_CRITERIA} として記録されます)")

    seen: Dict[int, Tuple[str, str]] = {}
    for raw in raw_verdicts:
        index, verdict, evidence = parse_verdict_arg(raw, total)
        if index in seen:
            raise ValueError(
                f"節 {index} に判定が 2 回出されています "
                f"('{seen[index][0]}' と '{verdict}')。1 節につき 1 つにしてください")
        seen[index] = (verdict, evidence)

    missing = [i for i in range(1, total + 1) if i not in seen]
    if missing:
        lines = "\n".join(
            f"  {i}. {clauses[i - 1]}" for i in missing)
        raise ValueError(
            f"受入条件 {total} 節のうち {len(missing)} 節に判定がありません。"
            f"未達でよいので、各節に met / partial / not-met を出してください "
            f"(隠さないことが目的で、未達は done を止めません):\n{lines}")

    items = []
    for i in range(1, total + 1):
        verdict, evidence = seen[i]
        items.append({"index": i, "clause": clauses[i - 1],
                      "verdict": verdict, "evidence": evidence})
    return {"state": "adjudicated", "clause_count": total, "clauses": items}


def unmet_clauses(adjudication: Optional[Dict]) -> List[Dict]:
    """``met`` でなかった節を返す。判定が無ければ空リスト。"""
    if not isinstance(adjudication, dict):
        return []
    return [c for c in adjudication.get("clauses") or []
            if c.get("verdict") != "met"]


def format_unmet_warning(adjudication: Optional[Dict]) -> str:
    """未達の節を人向けに 1 ブロックで返す。無ければ空文字。

    done を止めないが、黙って通さない。exit 0 で済ませると未達が記録の奥に沈む。
    """
    unmet = unmet_clauses(adjudication)
    if not unmet:
        return ""
    lines = ["⚠ 受入条件のうち満たされていない節があります (記録に残しました):"]
    for c in unmet:
        lines.append(f"  {c['index']}. [{c['verdict']}] {c['clause']}")
        if c.get("evidence"):
            lines.append(f"      根拠: {c['evidence']}")
    lines.append("  → 残作業を follow-up タスクとして起票することを検討してください。")
    return "\n".join(lines)
