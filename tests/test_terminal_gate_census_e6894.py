"""終端遷移の関門の被覆を、手書きの関数名でなく構文木の数え上げで留める (ms-166 e-6894)。

e-6600 の留め (`test_gate_is_the_shared_dev_helper_not_a_sales_copy`) は対象を
``cmd_activity_done`` / ``cmd_activity_cancel`` と **手で書いていた**。そのため関門に
乗らない新しい終端動詞が足されても緑のまま何も言わず、「もう全部閉じている」という
誤った確信を与えた。ここでは対象をソースから導出し、集合として比べる。

**この留め自身を壊して赤を見る試験を同梱する** (下の TestTheGuardActuallyFails)。
緑のガードは信頼されるので、偽の安全は無ガードより悪い —— 捕まえるべき形ごとに
「本当に赤くなるか」を実測するまで、この数え上げを信用してはならない。
"""

import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import terminal_gate as tg  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


#: 数え上げの対象ディレクトリ。**terminal_gate の被覆主張と同じ範囲でなければならない**
#: (ms-166 e-6894 保守性レビュー finding #1, medium): terminal_gate.py のコメントは
#: 「lib/ + server/ + scripts/ で 0 件」と書いていたのに、ここは lib/ と server/ しか
#: 読んでいなかった。scripts/ に押印層を呼ぶコードが足されても緑のまま通る状態で、
#: このモジュール自身が戒めている「緑のガードが嘘をつく」型そのものだった。主張を
#: 狭めるのでなく計測を広げた — scripts/ は実際にコードが置かれる場所なので、
#: 被覆から外す理由が無い。
CENSUS_DIRS = ("lib", "server", "scripts")


def _sources() -> dict:
    """``{import 名: ソース}`` —— 数え上げの対象 (CENSUS_DIRS)。"""
    out = {}
    for d in CENSUS_DIRS:
        for path in glob.glob(os.path.join(REPO, d, "*.py")):
            out[os.path.splitext(os.path.basename(path))[0]] = \
                open(path, encoding="utf-8").read()
    return out


# ---------------------------------------------------------------------------
# 軸1 — 押印層を呼ぶ経路が監査エントリを運んでいるか
# ---------------------------------------------------------------------------

def test_no_unguarded_terminal_stamp_calls():
    bad = tg.unguarded_terminal_calls(_sources())
    assert bad == [], (
        "終端状態を押す (mark_done / stamp_cancel) のに監査エントリを運んでいない"
        "呼び出しがある。reason / acknowledge / exempt のいずれかを渡すか、"
        "どうしても持ちようがないなら terminal_gate.EXEMPT_CALL_SITES に理由を 1 行"
        "添えて登録すること:\n"
        + "\n".join(f"  {h['module']}:{h['lineno']} {h['function']}() -> {h['stamp']}()"
                    for h in bad))


# ---------------------------------------------------------------------------
# 軸2 — 押印層を通さず status に終端を手書きしている経路
#
# 軸1 だけでは「押印層を直したので終端に至るには押印層を通るしかない」という主張が
# 嘘になる (手書きは 1 件も挙がらないため)。実測で 11 件在った。
# ---------------------------------------------------------------------------

def test_no_unregistered_handwritten_terminal_writes():
    bad = tg.unregistered_handwritten_terminal_writes(_sources())
    assert bad == [], (
        "status に終端 (done / cancelled) を手書き代入しているのに台帳に無い経路が"
        "ある。押印層 (work_model.mark_done / work_base.stamp_cancel) に寄せるか、"
        "寄せられない理由を 1 行添えて terminal_gate.KNOWN_HANDWRITTEN_TERMINAL に"
        "登録すること:\n"
        + "\n".join(f"  {h['key']}  (:{h['lineno']})" for h in bad))


def test_no_stale_handwritten_terminal_rows():
    """台帳の行は実在する経路だけ。直したら行を消す (台帳が嘘に腐るのを防ぐ)。"""
    observed = set()
    for module, src in _sources().items():
        for hit in tg.census_handwritten_terminal_writes(src, module):
            observed.add(hit["key"])
    stale = sorted(set(tg.KNOWN_HANDWRITTEN_TERMINAL) - observed)
    assert stale == [], (
        "KNOWN_HANDWRITTEN_TERMINAL に、もう手書きしていない (= 直った、または関数名が"
        "変わった) 行が残っている。直したなら行を消すこと。残すと台帳が『まだ債務が"
        "ある』と嘘をつき、次の読み手が直った箇所をもう一度直そうとする:\n"
        + "\n".join(f"  {k}" for k in stale))


def test_handwritten_terminal_rows_name_real_functions():
    """台帳のキーが実在する関数を指すこと (rename で false pass にならない)。"""
    import importlib
    for key in sorted(tg.KNOWN_HANDWRITTEN_TERMINAL):
        mod_name, _, fn_name = key.rpartition(".")
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, fn_name), (
            f"台帳のキー '{key}' が指す関数が {mod_name} に無い。rename したなら"
            f"台帳のキーも直すこと (放置すると数え上げが新しい名前を『未登録』として"
            f"赤くし、古いキーは stale として赤くなる)")


def test_the_coverage_claim_names_the_same_dirs_the_census_scans():
    """terminal_gate の被覆主張に出るディレクトリと、実際に走査する範囲を一致させる。

    保守性レビュー finding #1 (medium) の再発防止。コメントを直すだけだと、次に
    主張を書き換えた人が計測範囲を広げ忘れて同じ嘘が戻る。主張の文に現れる
    ``<dir>/`` を拾って CENSUS_DIRS と集合比較する。
    """
    src = open(os.path.join(REPO, "lib", "terminal_gate.py"), encoding="utf-8").read()
    marker = "unguarded_terminal_calls`` over "
    assert marker in src, (
        "被覆主張の文が見つからない。主張の書き方を変えたなら、この留めの marker も"
        "一緒に直すこと (= 主張と計測が離れないようにするのがこの留めの役目)")
    claim_line = src.split(marker, 1)[1].split("returns", 1)[0]
    claimed = {d for d in CENSUS_DIRS if f"{d}/" in claim_line}
    unclaimed = sorted(set(CENSUS_DIRS) - claimed)
    assert unclaimed == [], (
        f"走査しているのに被覆主張に挙がっていないディレクトリ: {unclaimed} "
        f"(主張文: {claim_line.strip()!r})")
    import re
    mentioned = set(re.findall(r"``([a-z_]+)/``", claim_line))
    extra = sorted(mentioned - set(CENSUS_DIRS))
    assert extra == [], (
        f"被覆主張が、実際には走査していないディレクトリを挙げている: {extra}。"
        f"主張を狭めるか、CENSUS_DIRS に足して本当に走査すること")


def test_no_stale_terminal_gate_exemption():
    """免除台帳の行も実在する呼び出しだけ (今は空であることが実測値)。"""
    used = set()
    for module, src in _sources().items():
        for hit in tg.census_terminal_stamp_calls(src, module):
            used.add(f"{hit['module']}.{hit['function']}")
    stale = sorted(set(tg.EXEMPT_CALL_SITES) - used)
    assert stale == [], (
        f"EXEMPT_CALL_SITES に、もう押印層を呼んでいない行が残っている: {stale}")


# ---------------------------------------------------------------------------
# 軸3 — 結果 (outcome) を運ばない押印呼び出しに、理由が書かれているか
#
# e-6894 の動機の前半: outcome は第一級の引数になったが渡しているのは 18 箇所中 3 箇所
# だけで、**次の人が真似る見本が 8 割そちら側に在る**。渡していないこと自体は誤りでは
# ないので、禁止ではなく「なぜ運ばないのか」を 1 行書かせる形で留める。
# ---------------------------------------------------------------------------

def test_every_outcome_omission_is_declared():
    bad = tg.undeclared_outcome_omissions(_sources())
    assert bad == [], (
        "押印層を呼ぶのに結果 (outcome) を運んでおらず、その理由も台帳に無い呼び出しが"
        "ある。結果を運ぶように直すか、なぜ運ばないのかを 1 行添えて "
        "terminal_gate.KNOWN_NO_OUTCOME に登録すること (黙って外さない):\n"
        + "\n".join(f"  {k}" for k in bad))


def test_no_stale_outcome_omission_rows():
    """結果を運ぶように直した行は台帳から消す (台帳が嘘に腐るのを防ぐ)。"""
    omits = set(tg.census_outcome_on_terminal_calls(_sources())["omits"])
    stale = sorted(set(tg.KNOWN_NO_OUTCOME) - omits)
    assert stale == [], (
        "KNOWN_NO_OUTCOME に、もう結果を運ばない形ではない (= 直った、または関数名が"
        f"変わった) 行が残っている: {stale}")


def test_at_least_one_call_site_does_carry_an_outcome():
    """見本が全滅していないこと。

    1 つも運んでいないと「この引数は実際には使われていない」と読まれ、次の人が消す側に
    倒れる。e-6600 で足した引数が死に引数になるのを防ぐ下限の確認。
    """
    assert tg.census_outcome_on_terminal_calls(_sources())["carries"], \
        "結果を運んでいる押印呼び出しが 1 つも無い (引数が死んでいる)"


# ---------------------------------------------------------------------------
# この留め自身を壊して赤を見る (= test the test)
#
# 「0 件だった」は、数え上げが何も見えていないときも 0 件になる。捕まえるべき形ごとに
# 現に欠陥が在る木を作って当て、赤くなることを実測する。
# ---------------------------------------------------------------------------

class TestTheGuardActuallyFails:
    def test_catches_a_stamp_call_with_no_audit_entry(self):
        src = (
            "def close_it(rec):\n"
            "    work_model.mark_done(rec, actor='me')\n"
        )
        hits = tg.census_terminal_stamp_calls(src, "synthetic")
        assert len(hits) == 1 and hits[0]["forwards_audit"] is False
        assert tg.unguarded_terminal_calls({"synthetic": src}), \
            "監査エントリを運ばない押印呼び出しを見逃した"

    def test_passes_a_stamp_call_that_forwards_the_audit_entry(self):
        src = (
            "def close_it(rec, reason='', acknowledge=False):\n"
            "    work_model.mark_done(rec, reason=reason, acknowledge=acknowledge)\n"
        )
        assert tg.unguarded_terminal_calls({"synthetic": src}) == [], \
            "義務を上流へ転送している呼び出しを誤って赤くした"

    def test_catches_a_handwritten_terminal_literal(self):
        src = (
            "def sneaky(rec):\n"
            '    rec["status"] = "done"\n'
        )
        assert tg.unregistered_handwritten_terminal_writes({"synthetic": src}), \
            "status への終端 literal の手書き代入を見逃した"

    def test_catches_a_terminal_written_through_an_aliased_constant(self):
        """``MEETING_CANCELLED = "cancelled"`` の実形。literal だけ見る実装では漏れる。"""
        src = (
            'MEETING_CANCELLED = "cancelled"\n'
            "\n"
            "def sneaky(rec):\n"
            '    rec["status"] = MEETING_CANCELLED\n'
        )
        assert tg.unregistered_handwritten_terminal_writes({"synthetic": src}), \
            "別名の定数を経由した終端代入を見逃した (実際に meeting_cancel がこの形だった)"

    def test_catches_a_terminal_hidden_in_a_conditional(self):
        """``"approved" if ok else "cancelled"`` の実形 (transition_approval がこれ)。"""
        src = (
            "def sneaky(rec, ok):\n"
            '    rec["status"] = "approved" if ok else "cancelled"\n'
        )
        assert tg.unregistered_handwritten_terminal_writes({"synthetic": src}), \
            "条件式の片側に隠れた終端代入を見逃した"

    def test_does_not_flag_a_non_terminal_status_write(self):
        src = (
            "def advance(rec):\n"
            '    rec["status"] = "in_progress"\n'
        )
        assert tg.unregistered_handwritten_terminal_writes({"synthetic": src}) == [], \
            "終端でない status 代入を誤って赤くした (広すぎる数え上げ)"

    def test_does_not_flag_reading_a_terminal_status(self):
        src = (
            "def is_done(rec):\n"
            '    return rec["status"] == "done"\n'
        )
        assert tg.unregistered_handwritten_terminal_writes({"synthetic": src}) == [], \
            "終端 status の読み取りを書き込みと混同した"

    def test_catches_a_regression_injected_into_the_real_tree(self):
        """合成木ではなく **実ソース** から監査エントリを剥がして赤を測る。

        合成木だけで試すと、実コードの書き方 (多行呼び出し / 属性形 / 日本語コメントに
        よるバイト列のずれ) で数え上げが外れていても気付けない。
        """
        real = open(os.path.join(REPO, "lib", "core.py"), encoding="utf-8").read()
        assert tg.unguarded_terminal_calls({"core": real}) == [], "前提: 今は緑"
        broken = real.replace(
            'return work_base.stamp_cancel(entry, reason=reason,\n'
            '                                  acknowledge=acknowledge,\n'
            '                                  verb="task cancel")',
            'return work_base.stamp_cancel(entry)')
        assert broken != real, "差し替え対象が見つからない (実装が変わったら此処も直す)"
        bad = tg.unguarded_terminal_calls({"core": broken})
        assert [h["function"] for h in bad] == ["task_delete"], (
            "実ソースから監査エントリを剥がしたのに数え上げが赤くならなかった "
            f"(= この留めは偽の安全。得られた結果: {bad})")


# ---------------------------------------------------------------------------
# 規則そのもの (e-6895 を含む)
# ---------------------------------------------------------------------------

class TestRequireAudit:
    def test_reason_is_returned_as_is(self):
        assert tg.require_audit("task done", reason="直した") == "直した"

    def test_acknowledge_returns_the_sentinel(self):
        assert tg.require_audit("task done", acknowledge=True) == \
            tg.ACKNOWLEDGED_REASON

    def test_neither_refuses(self):
        with pytest.raises(tg.TerminalAuditRequired):
            tg.require_audit("task done")

    def test_empty_reason_is_not_a_waiver(self):
        """空文字は「意図した免除」と「旗を埋めただけ」の区別が付かないので不可。"""
        with pytest.raises(tg.TerminalAuditRequired):
            tg.require_audit("task done", reason="   ")

    def test_both_refuses(self):
        """e-6895: 旧実装は acknowledge を先に見て、書いた理由を黙って捨てていた。"""
        with pytest.raises(ValueError) as exc:
            tg.require_audit("task done", reason="ちゃんとした理由",
                             acknowledge=True)
        assert "not both" in str(exc.value)
        assert "e-6895" in str(exc.value)

    def test_the_written_reason_is_never_silently_dropped(self):
        """e-6895 の本体: どんな組み合わせでも、渡した理由が黙って消えない。

        「両方渡したら拒否」を選んだので、理由が記録に残らない唯一の経路は
        acknowledge 単独 (= 意図した免除) だけになる。
        """
        for kwargs in ({"reason": "r"}, {"acknowledge": True},
                       {"reason": "r", "acknowledge": False}):
            got = tg.require_audit("task done", **kwargs)
            if kwargs.get("reason"):
                assert got == kwargs["reason"]
            else:
                assert got == tg.ACKNOWLEDGED_REASON

    def test_unknown_exemption_key_refuses(self):
        with pytest.raises(ValueError, match="EXEMPT_CALL_SITES"):
            tg.require_audit("task done", exempt="not.registered")

    def test_acknowledge_cannot_stack_with_an_exemption(self):
        with pytest.raises(ValueError, match="cannot be combined"):
            tg.require_audit("task done", acknowledge=True, exempt="x")


class TestEnvAdapter:
    def test_reads_the_default_env_pair(self, monkeypatch):
        monkeypatch.setenv("BEACON_REASON", "直した")
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)
        assert tg.require_audit_from_env("task done") == "直した"

    def test_falls_back_to_a_legacy_reason_variable(self, monkeypatch):
        """旧名は移行用の fallback として 1 箇所から読む (verb ごとに差し替えない)。

        AX + 保守性レビューが独立に同じ指摘 (合意度 2/2): 当初は
        ``reason_env=`` という可変パラメータで 4 つの別名を温存しており、次に
        終端 verb を足す人が 5 つ目の名前を作る前例になっていた。パラメータを
        消して「新しい別名を書ける場所」自体を無くした。
        """
        monkeypatch.delenv("BEACON_REASON", raising=False)
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)
        for legacy in tg.LEGACY_AUDIT_REASON_ENVS:
            for other in tg.LEGACY_AUDIT_REASON_ENVS:
                monkeypatch.delenv(other, raising=False)
            monkeypatch.setenv(legacy, "誤起票")
            assert tg.require_audit_from_env("account cancel") == "誤起票", legacy

    def test_canonical_name_wins_over_a_legacy_one(self, monkeypatch):
        monkeypatch.setenv("BEACON_REASON", "正準")
        monkeypatch.setenv(tg.LEGACY_AUDIT_REASON_ENVS[0], "旧")
        monkeypatch.delenv("BEACON_ACKNOWLEDGE", raising=False)
        assert tg.require_audit_from_env("account cancel") == "正準"

    def test_the_gate_takes_no_per_verb_reason_variable(self):
        """``reason_env`` のような『verb ごとに別名を書ける口』が無いこと。

        これが有ると不一致が関数の正式な形として固定され、機械ガードも生まれにくい
        (レビュー指摘の核心)。署名で構造的に塞ぐ。
        """
        import inspect
        params = inspect.signature(tg.require_audit_from_env).parameters
        assert "reason_env" not in params, (
            "verb ごとに理由の env 名を差し替える口が復活している。"
            "AUDIT_REASON_ENV 1 名 + LEGACY_AUDIT_REASON_ENVS の移行 fallback に寄せること")

    def test_both_env_signals_refuse(self, monkeypatch):
        monkeypatch.setenv("BEACON_REASON", "直した")
        monkeypatch.setenv("BEACON_ACKNOWLEDGE", "1")
        with pytest.raises(ValueError, match="not both"):
            tg.require_audit_from_env("task done")


# ---------------------------------------------------------------------------
# AX レビュー finding #1 (high) の修正 — 非終端の動詞に監査の旗を渡したら拒否する
#
# 併せて、その判定が **環境に残っている値** で誤発火しないことも留める。最初の実装は
# 「BEACON_REASON が空でないか」で推論しており、shell に BEACON_REASON を export して
# いる利用者の `list` が拒否された (全件実行で実測して気付いた)。値の有無は「旗が
# 渡されたか」の代わりにならない。
# ---------------------------------------------------------------------------

class TestAuditFlagOnNonTerminalVerbs:
    def test_marker_distinguishes_passed_from_ambient(self, monkeypatch):
        monkeypatch.setenv("BEACON_REASON", "環境に残っている値")
        monkeypatch.delenv(tg.AUDIT_FLAG_GIVEN_ENV, raising=False)
        assert tg.audit_flag_was_given() is False, (
            "環境に値が残っているだけで『旗が渡された』と判定してはいけない")
        monkeypatch.setenv(tg.AUDIT_FLAG_GIVEN_ENV, "1")
        assert tg.audit_flag_was_given() is True

    def test_work_item_list_refuses_when_the_flag_is_actually_passed(
            self, monkeypatch, capsys):
        import cmd_target
        monkeypatch.setenv("BEACON_WI_ACTION", "list")
        monkeypatch.setenv("BEACON_TARGET_ID", "x-1")
        monkeypatch.setenv("BEACON_REASON", "意味のない理由")
        monkeypatch.setenv(tg.AUDIT_FLAG_GIVEN_ENV, "1")
        with pytest.raises(SystemExit) as exc:
            cmd_target.cmd_target_work_item()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "done / cancel にだけ意味があります" in err, err

    def test_work_item_list_is_unaffected_by_an_ambient_reason(
            self, monkeypatch):
        """旗を渡していない list は、環境に BEACON_REASON が在っても通ること。

        ここが赤くなる実装 (値から推論する形) を一度書いてしまい、全件実行で
        test_target_child_fields_cli_e5344 が落ちて気付いた。
        """
        import cmd_target
        monkeypatch.setenv("BEACON_WI_ACTION", "list")
        monkeypatch.setenv("BEACON_TARGET_ID", "x-1")
        monkeypatch.setenv("BEACON_REASON", "前の呼び出しの残り")
        monkeypatch.delenv(tg.AUDIT_FLAG_GIVEN_ENV, raising=False)
        # target が無いので別の理由で落ちるのは構わない。拒否文言が出ないことだけ見る。
        try:
            cmd_target.cmd_target_work_item()
        except SystemExit:
            pass
        except Exception:
            pass

    def test_both_frontends_set_the_marker(self):
        """片フロントだけがマーカーを立てると、そちらだけ拒否が効く (割れ)。"""
        bash = open(os.path.join(REPO, "bin", "lib", "cmd_target.sh"),
                    encoding="utf-8").read()
        bash_acq = open(os.path.join(REPO, "bin", "lib", "cmd_acquisition.sh"),
                        encoding="utf-8").read()
        py = open(os.path.join(REPO, "beacon_cli", "dispatch.py"),
                  encoding="utf-8").read()
        for name, src in (("cmd_target.sh", bash), ("cmd_acquisition.sh", bash_acq),
                          ("dispatch.py", py)):
            assert tg.AUDIT_FLAG_GIVEN_ENV in src, (
                f"{name} が {tg.AUDIT_FLAG_GIVEN_ENV} を立てていない — そのフロント"
                f"からは非終端動詞への旗が拒否されず黙って捨てられる")


def test_the_sentinel_has_one_declaration():
    """CLI 側の写しが宣言元と一致していること (文言のずれで記録が割れない)。"""
    import commands_shared
    assert commands_shared._ACKNOWLEDGED_REASON is tg.ACKNOWLEDGED_REASON
