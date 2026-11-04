"""引き継ぎの節が、本文のまま次のセッションへ届くこと (ms-160 e-7314).

守りたいのは 3 層で、**層ごとに別の試験にしてある** (どの層が守っているのかを
取り違えないため):

1. 抽出器 (`lib/session_handoff.py`) … 節を本文のまま取り、隣を巻き込まない
2. 見出しの単一真実源 … 書き口 (session-end Skill) の文面が、読み口が探す綴りと
   一致している。揺れたらここが赤くなる
3. 読み口の手順 (session-start Skill Step 1j) … 「全件を叩いて AI が抽出せよ」に
   **戻っていない**。戻ると、切り出しの最中に要約へ化ける元の経路に逆戻りする

3 を試験にしてあるのが肝。1 と 2 だけなら「口は在るが誰も使っていない」状態を
通してしまい、それは修正前と同じ結果になる。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import session_handoff as sh  # noqa: E402


# --- 1. 抽出器 -------------------------------------------------------------

SUMMARY = """前置きの散文。ここは節ではない。

## 次セッション最優先

1. 最初にやること。
2. 次にやること。

### 補足の小見出し

小見出しは節の中身として残る (ここで切ると本文が尻切れになる)。

## 次にやること

- 細かい作業
- もう一つ

## 別の節

ここは引き継ぎではない。
"""


def test_required_section_comes_back_verbatim():
    got = sh.extract_handoff(SUMMARY)
    body = got["required"]["body"]
    assert got["required"]["heading"] == sh.REQUIRED_HEADING
    assert "1. 最初にやること。" in body
    assert "2. 次にやること。" in body
    # 下位の小見出しは本文として残る
    assert "### 補足の小見出し" in body
    assert "小見出しは節の中身として残る" in body


def test_the_next_section_is_not_dragged_in():
    """隣の節を巻き込まない (巻き込むと「最優先」が薄まる)。"""
    body = sh.extract_handoff(SUMMARY)["required"]["body"]
    assert "細かい作業" not in body
    assert "ここは引き継ぎではない" not in body
    assert "前置きの散文" not in body


def test_optional_section_is_returned_separately():
    got = sh.extract_handoff(SUMMARY)
    assert [o["heading"] for o in got["optional"]] == list(sh.OPTIONAL_HEADINGS)
    assert "細かい作業" in got["optional"][0]["body"]


def test_a_missing_required_section_is_named_not_silently_empty():
    """無いときは「無い」と言う。空文字で「無かった」と「拾えなかった」を畳まない。"""
    got = sh.extract_handoff("## 別の節\n\n中身\n")
    assert got["required"] is None
    assert got["missing"] == [sh.REQUIRED_HEADING]
    assert got["optional"] == []


@pytest.mark.parametrize("heading_line", [
    f"## {sh.REQUIRED_HEADING}",
    f"# {sh.REQUIRED_HEADING}",
    f"###  {sh.REQUIRED_HEADING}  ",
])
def test_heading_level_and_padding_do_not_break_the_read(heading_line):
    """階層をひとつ変えただけで拾えなくなるのは脆すぎる。"""
    got = sh.extract_handoff(f"{heading_line}\n\n本文\n")
    assert got["required"] is not None, heading_line
    assert got["required"]["body"] == "本文"


def test_empty_summary_is_a_missing_section_not_a_crash():
    for empty in ("", None):
        got = sh.extract_handoff(empty)
        assert got["required"] is None
        assert got["missing"] == [sh.REQUIRED_HEADING]


def test_format_shows_the_body_and_names_a_missing_section():
    entry = {"session_id": "sv-x", "last_aggregated_at": "2026-11-04T00:00:00Z"}
    out = sh.format_handoff(entry, sh.extract_handoff(SUMMARY))
    assert "sv-x" in out and "1. 最初にやること。" in out
    missing = sh.format_handoff(entry, sh.extract_handoff("## 別の節\n本文\n"))
    assert sh.REQUIRED_HEADING in missing and "ありません" in missing


def test_a_heading_with_a_parenthetical_is_still_the_section():
    """注記つきの書式を受けること。**これを厳密一致にしていて 82% を落としていた。**

    独立 AX レビューが本番の引き継ぎ 111 件に通して実測: 拾えたのは 20 件で、
    うち 39 件は旧 Skill が推奨していた書式そのもの (`(top of queue)` 併記) だった。
    しかも拾えなかったものを「書き残していない」と断定して表示していた。
    下の綴りは実コーパスから採ってある。
    """
    for variant in (
        f"{sh.REQUIRED_HEADING} (top of queue)",
        f"{sh.REQUIRED_HEADING} / top of queue",
        f"{sh.REQUIRED_HEADING} (= top of queue)",
        f"{sh.REQUIRED_HEADING} / 次にやること",
        f"{sh.REQUIRED_HEADING} (top of queue) — CLEAN セッションで updated main から",
    ):
        got = sh.extract_handoff(f"## {variant}\n\n本文\n")
        assert got["required"] is not None, f"拾えていない: {variant!r}"
        assert got["required"]["body"] == "本文"


def test_a_different_word_that_merely_starts_the_same_is_not_the_section():
    """前方一致を緩めすぎていないこと (別の語を拾ったら、別の節を最優先と誤読する)。"""
    got = sh.extract_handoff(f"## {sh.REQUIRED_HEADING}事項\n\n本文\n")
    assert got["required"] is None


def test_a_comment_inside_a_code_fence_does_not_cut_the_body():
    """**コードブロックの中は見出しとして読まない。**

    引き継ぎにコマンド例を書くのは日常的で、その中の `# コメント` が見出し
    (level 1) と判定されると、節がそこで打ち切られて以降が黙って消える。
    この module が防ぐはずの「引き継ぎが黙って失われる」を別の扉から
    再導入していた (独立保守性レビューが実測)。
    """
    s = (f"## {sh.REQUIRED_HEADING}\n\n1. 盤を建て直す:\n\n"
         "```bash\n# 動いている盤を閉じてから\nbash viewer/build.sh darwin\n```\n\n"
         "2. そのあと PR を出す。\n\n## 次にやること\n- 別の作業\n")
    got = sh.extract_handoff(s)
    body = got["required"]["body"]
    assert "2. そのあと PR を出す。" in body, f"フェンスで本文が切れている: {body!r}"
    assert "# 動いている盤を閉じてから" in body, "本文のコメント行が落ちている"
    assert "別の作業" not in body, "隣の節を巻き込んでいる"


def test_a_heading_appearing_twice_keeps_both_bodies():
    """後勝ちで前を捨てない (捨てると引き継ぎが黙って失われる)。"""
    got = sh.extract_handoff(
        f"## {sh.REQUIRED_HEADING}\n\n1 回目\n\n"
        f"## {sh.REQUIRED_HEADING}\n\n2 回目\n")
    body = got["required"]["body"]
    assert "1 回目" in body and "2 回目" in body, body
    assert got["required"]["occurrences"] == 2


def test_an_unreadable_heading_is_not_called_absent():
    """語は在るのに読めなかったときは「無い」と断定しない。

    「書き残していない」と「読めなかった」を同じ文面に畳むと、読み手は前セッションが
    手を抜いたと誤解して読むのをやめる。
    """
    got = sh.extract_handoff(f"## {sh.REQUIRED_HEADING}事項 (別の語)\n\n本文\n")
    assert got["required"] is None
    assert got["near_miss"], "near_miss を立てていない"
    out = sh.format_handoff({"session_id": "sv-x"}, got)
    assert "読めませんでした" in out
    assert "書き残していない" not in out


# --- 2. 見出しの単一真実源 (書き口が読み口と同じ綴りを指しているか) ----------

_END_SKILLS = (
    "skills/beacon-session-end.md",
    "shared/skills/beacon-session-end/SKILL.md",
    "plugins/beacon/skills/beacon-session-end/SKILL.md",
)


@pytest.mark.parametrize("rel", _END_SKILLS)
def test_the_write_side_instructs_the_exact_heading(rel):
    """session-end が、読み口が探す綴りそのものを指示していること。

    以前は「次セッション最優先 / top of queue / 次にやること等」と**例示**していた。
    書き口と読み口が同じ語に縛られていないと、揺れた瞬間に黙って拾えなくなる。
    """
    text = (REPO / rel).read_text(encoding="utf-8")
    assert f"## {sh.REQUIRED_HEADING}" in text, (
        f"{rel}: 読み口が探す見出し `## {sh.REQUIRED_HEADING}` を指示していない")
    assert "lib/session_handoff.py" in text, (
        f"{rel}: 見出しの真実源 (lib/session_handoff.py) を指していない — "
        "どこを直せば揺れが閉じるのか読み手に分からない")


# --- 3. 読み口の手順が、元の「抽出せよ」に戻っていないこと -------------------

_START_SKILLS = (
    "skills/beacon-session-start.md",
    "shared/skills/beacon-session-start/SKILL.md",
    "plugins/beacon/skills/beacon-session-start/SKILL.md",
)


@pytest.mark.parametrize("rel", _START_SKILLS)
def test_the_read_side_uses_the_narrow_command(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert "beacon session log handoff" in text, (
        f"{rel}: Step 1j が狭い口 (`beacon session log handoff`) を指していない")


@pytest.mark.parametrize("rel", _START_SKILLS)
def test_the_read_side_does_not_ask_the_ai_to_extract_again(rel):
    """**これが肝**: 「全件を叩いて AI が抽出せよ」に戻ったら赤くなる。

    口を足しただけでは、手順が元に戻った瞬間に修正前と同じ結果になる
    (= 切り出しの最中に要約へ化ける)。手順そのものを固定する。
    """
    text = (REPO / rel).read_text(encoding="utf-8")
    step = text[text.index("## Step 1j:"):]
    step = step[:step.index("\n## ", 10)] if "\n## " in step[10:] else step
    # **走査するのは指示行だけ** — 引用 (`>`) は経緯で、指示ではない。
    # 最初はこの Step 全体を走査したので、「以前は『AI が文脈で抽出』せよと
    # 書いていた」という**経緯の引用**に誤反応して赤くなった。言い回しの除外
    # 規則を足すより、見る範囲を絞るほうが確か (同じ判断を
    # scripts/check-idle-default-claims.py でも採った)。引用の中に指示を書いても
    # 捕まらないが、引用の中の文は指示として読まれないので、それは正しい。
    instructions = "\n".join(
        ln for ln in step.splitlines() if not ln.lstrip().startswith(">"))
    assert "AI が文脈で抽出" not in instructions, (
        f"{rel}: Step 1j が再び「AI が文脈で抽出」を指示している")
    # 経緯として list に言及するのは良いが、**叩く手順** として残っていてはならない。
    assert "beacon session log list --json\n```" not in instructions, (
        f"{rel}: Step 1j が再び `session log list --json` を叩く手順になっている")
    assert "要約" in instructions and "してはならない" in instructions, (
        f"{rel}: Step 1j が「そのまま貼る / 要約してはならない」と言っていない")


# --- 4. 口が両フロントで **実際に動く** こと -------------------------------
#
# 最初はここを `"session_log_handoff" in dispatch.py` の部分一致で書いた。反転で
# 「Python フロントの配線だけ落とす」を当てたら **緑のまま通った** —— 名前は
# parser 定義の行に残るので、呼び出しを消しても部分一致は満たされる。名前の存在は
# 配線の証拠にならないので、両フロントを**実際に走らせて**出力を突き合わせる。

import subprocess  # noqa: E402

_KNOWN_SHAPES = ("前セッションの引き継ぎ (", "引き継ぎがまだありません")


def _run_frontend(argv):
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["BEACON_SKIP_AUTO_UPDATE"] = "1"
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                          env=env, timeout=180)


def test_both_frontends_actually_serve_the_verb():
    """bash と Python のどちらから叩いても、同じ既知の形が返ること。

    引き継ぎが在る環境では本文つきの案内、無い環境 (= まっさらな取り込み検査) では
    「まだありません」。**どちらの形でもよいが、両フロントで一致していること**を
    見る —— 片方だけ壊れていれば形が食い違うか、非ゼロで落ちる。

    **Python 側は `beacon_cli.dispatch.dispatch` を直に叩く。** `beacon_cli.main`
    経由にすると、bash が在る環境 (macOS / Linux) では **bash に委譲される**ので、
    Python 側の配線を落としても緑のまま通ってしまう —— 反転で実測した
    (「両フロントを測っている」つもりで bash を 2 回測っていた)。
    """
    bash = _run_frontend(["bash", str(REPO / "bin" / "beacon"),
                          "session", "log", "handoff"])
    py = _run_frontend([sys.executable, "-c",
                        "import sys, pathlib; "
                        "from beacon_cli.dispatch import dispatch; "
                        "sys.exit(dispatch(pathlib.Path('.'), "
                        "['session', 'log', 'handoff']))"])
    for label, r in (("bin/beacon", bash), ("beacon_cli", py)):
        out = r.stdout + r.stderr
        assert r.returncode == 0, f"{label}: 非ゼロで終了\n{out}"
        assert any(k in out for k in _KNOWN_SHAPES), (
            f"{label}: 既知の形を返していない (配線が落ちている疑い)\n{out}")
    bash_shape = next(k for k in _KNOWN_SHAPES if k in bash.stdout + bash.stderr)
    py_shape = next(k for k in _KNOWN_SHAPES if k in py.stdout + py.stderr)
    assert bash_shape == py_shape, (
        "両フロントで形が食い違っている (片方だけ配線が落ちている)\n"
        f"bash={bash_shape!r} python={py_shape!r}")


def test_both_frontends_refuse_a_stray_argument_the_same_way():
    """**異常入力でも一致すること。**

    docstring が「両フロントで一致」と言うのに、測っていたのは正常系だけだった
    (独立 AX レビューが実測して指摘)。当時 bash 側は余分な位置引数を黙って捨てて
    exit 0、Python 側 (argparse) は exit 2 で、同じ入力で結果が割れていた。
    緑のガードが「一致している」と誤認させる形だったので、検査範囲を主張に合わせる。
    """
    bash = _run_frontend(["bash", str(REPO / "bin" / "beacon"),
                          "session", "log", "handoff", "garbage123"])
    py = _run_frontend([sys.executable, "-c",
                        "import sys, pathlib; "
                        "from beacon_cli.dispatch import dispatch; "
                        "sys.exit(dispatch(pathlib.Path('.'), "
                        "['session', 'log', 'handoff', 'garbage123']))"])
    assert bash.returncode != 0, f"bash が余分な引数を黙って受けた\n{bash.stdout}"
    assert py.returncode != 0, f"python が余分な引数を黙って受けた\n{py.stdout}"
    assert bash.returncode == py.returncode, (
        f"終了コードが割れている: bash={bash.returncode} python={py.returncode}")


def test_the_verb_is_registered_in_the_ledger():
    """台帳にも載っていること (載せ忘れると drift 検査の対象から漏れる)。"""
    sys.path.insert(0, str(REPO / "lib"))
    import verb_ledger_data  # noqa: PLC0415
    assert "session_log_handoff" in verb_ledger_data.VERB_LEDGER, (
        "verb 台帳に session_log_handoff が無い")
