"""session-start の締切超過 surface (scripts/session-start-deadlines.py) — ms-139
e-4952, ms-142 e-5010 で列挙を ``beacon deadline due`` に一本化。

列挙・temporal 判定・terminal Target 除外は CLI (``beacon deadline due --json``)
と occupation.iter_deadline_candidates が持つ (test_deadline_due_cmd.py で pin)。
この script に残るのは「CLI の返す items を整形して表示する」glue だけなので、
ここでは subprocess (_beacon_json) を stub して整形・空表示・並び転記を固める。
"""

import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
import deadline  # noqa: E402

# scripts/ はパッケージでないので spec から直接ロードする。
_SPEC = importlib.util.spec_from_file_location(
    "ss_deadlines",
    os.path.join(REPO, "scripts", "session-start-deadlines.py"))
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_format_empty_is_blank():
    assert mod._format([]) == ""


def test_main_calls_deadline_due_and_formats(monkeypatch, capsys):
    calls = []

    def fake_json(args):
        calls.append(args)
        if args[:2] == ["deadline", "due"]:
            return {"items": [
                {"kind": "milestone", "label": "遅れてるMS",
                 "deadline": "2026-08-05", "temporal": deadline.TRANSITION_OVERDUE,
                 "context": "ms-1"},
                {"kind": "task", "label": "期限切れ", "deadline": "2026-08-06",
                 "temporal": deadline.TRANSITION_OVERDUE, "context": "ms-1 / e-1"},
            ]}
        return None

    monkeypatch.setattr(mod, "_beacon_json", fake_json)
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    # session-start は列挙を CLI に委ねる: deadline due だけを叩く (task list /
    # opportunity due の職種分岐 subprocess はもう無い)。
    assert calls == [["deadline", "due"]]
    assert "締切超過" in out
    assert "遅れてるMS" in out and "期限切れ" in out
    assert "[MS]" in out and "[タスク]" in out


def test_main_empty_prints_nothing(monkeypatch, capsys):
    monkeypatch.setattr(mod, "_beacon_json", lambda args: {"items": []})
    rc = mod.main()
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_main_degrades_on_cli_failure(monkeypatch, capsys):
    # CLI が失敗 (None) しても空表示で exit 0 (best-effort 二重化)。
    monkeypatch.setattr(mod, "_beacon_json", lambda args: None)
    rc = mod.main()
    assert rc == 0
    assert capsys.readouterr().out == ""
