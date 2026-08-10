"""ms-142 の芯を閉じる E2E: 新職種を manifest 宣言するだけで締切が両経路に載る (e-5014).

ms-142 の約束は「新しい職種の manifest を宣言するだけで、その職種の Target/WorkItem を
歩く共有 (L2) capability が配線ゼロで点灯する」。deadline は 2 経路で消費される:

  - サーバの締切リマインダ (``app._deadline_reminder_candidates`` → tick で overdue を
    claim 者へ DM、ms-139)、
  - session-start の締切表示 (``commands.cmd_deadline_due`` = ``beacon deadline due``、
    ``scripts/session-start-deadlines.py`` が呼ぶ)。

既存の parity テスト (test_deadline_parity_e5010) は dev/sales で「旧経路=新経路」を
証明するが、**合成職種 (compliance)** は使わない — つまり「known な職種で回帰なし」までしか
言えない。本テストはその先を pin する: arm 名が dev/sales と全く違う (duties/attestations)、
コードのどこにも名前が現れない職種を、``project.json`` の ``target_classes`` 記述子 1 個だけで
宣言し、その duty の締切が **両サイトで** surface することを実行証明する。どちらのサイトも
``obligations`` / ``duties`` を名指ししないので、これが通れば「宣言 ⇒ 点灯」は本物。

決定的な負の対照 (``test_..._only_because_of_the_manifest_declaration``): 同じデータから
記述子だけを外すと duty は surface しなくなる — 点灯させているのが「宣言」であって、コードに
埋め込まれた職種知識ではないことを直接示す。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
SERVER = Path(__file__).parent.parent / "server"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).parent))

import deadline  # noqa: E402
import commands  # noqa: E402
import app  # noqa: E402
from synth_profession import build_synthetic_project  # noqa: E402

# duty-1's deadline is 2026-08-06 (build_synthetic_project duty_overdue=True), so
# a judgment date of 2026-08-10 makes it overdue → it must fire at both sites.
TODAY = "2026-08-10"


def _fires(item) -> bool:
    return deadline.work_item_temporal_status(item, TODAY) in (
        deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE)


def _server_firing_set(project) -> set:
    """The (id, kind, deadline, recipient) tuples the SERVER reminder would fire —
    the shipped ``app._deadline_reminder_candidates`` gated by the L2 temporal
    rule, exactly as ``_fire_due_deadlines`` gates it."""
    return {
        (item.get("id"), kind, deadline.deadline_of(item), recipient)
        for item, kind, _label, recipient in app._deadline_reminder_candidates(project)
        if _fires(item)
    }


def _sessionstart_items(project, tmp_path, monkeypatch) -> list:
    """The rows ``beacon deadline due --json`` emits — the exact command
    ``scripts/session-start-deadlines.py`` invokes. Runs the real handler end to
    end against a temp project file."""
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(project, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(commands, "_today_iso", lambda: TODAY)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_deadline_due()
    return json.loads(buf.getvalue())["items"]


# --- SERVER site: the synthetic profession's duty fires the reminder -----------

def test_synthetic_profession_deadline_fires_server_reminder():
    project = build_synthetic_project(duty_overdue=True)
    firing = _server_firing_set(project)
    # The duty surfaces with its DECLARED work-item kind ("duty", from the
    # descriptor's work_item_arm), under its claiming session — with the server
    # never naming "obligations"/"duties".
    assert ("duty-1", "duty", "2026-08-06", "sv-comp") in firing, firing
    # And it genuinely fires (overdue), not merely enumerated.
    duty = project["obligations"][0]["duties"][0]
    assert deadline.work_item_temporal_status(duty, TODAY) == deadline.TRANSITION_OVERDUE


# --- SESSION-START site: the same duty shows in `beacon deadline due` -----------

def test_synthetic_profession_deadline_shows_in_deadline_due(tmp_path, monkeypatch):
    project = build_synthetic_project(duty_overdue=True)
    items = _sessionstart_items(project, tmp_path, monkeypatch)
    hit = [r for r in items if r["kind"] == "duty" and r["deadline"] == "2026-08-06"]
    assert len(hit) == 1, items
    assert hit[0]["temporal"] == deadline.TRANSITION_OVERDUE
    # context is the occupation-agnostic breadcrumb "<target_id> / <item_id>".
    assert "obl-1" in hit[0]["context"] and "duty-1" in hit[0]["context"]


# --- ZERO WIRING is real: only the manifest declaration lights it up -----------

def test_deadline_lights_up_only_because_of_the_manifest_declaration():
    """The decisive proof. Strip the ONE thing that declares the profession — the
    ``target_classes`` descriptor — and the very same obligation data goes dark at
    the server site. So it is the DECLARATION that lights the deadline up, not
    profession knowledge baked into either enumeration path (had ``obligations`` /
    ``duties`` been hardcoded, the duty would fire with or without the descriptor)."""
    with_manifest = build_synthetic_project(duty_overdue=True)
    assert ("duty-1", "duty", "2026-08-06", "sv-comp") in _server_firing_set(with_manifest)

    without = build_synthetic_project(duty_overdue=True)
    without.pop("target_classes")   # remove the sole declaration
    firing = _server_firing_set(without)
    assert not any(t[0] == "duty-1" for t in firing), (
        "duty-1 fired WITHOUT its manifest declaration — a path hardcodes the "
        "obligations/duties collection instead of reading the descriptor: " + str(firing))


# --- temporal gate applies to the synthetic profession too (not everything shown) -

def test_future_synthetic_deadline_does_not_fire_either_site(tmp_path, monkeypatch):
    """A NON-overdue duty must NOT fire — proving the L2 temporal rule is applied to
    the synthetic profession identically, not that the profession dumps everything."""
    project = build_synthetic_project(duty_overdue=False)   # deadline 2099-01-01
    assert not any(t[0] == "duty-1" for t in _server_firing_set(project))
    items = _sessionstart_items(project, tmp_path, monkeypatch)
    assert not any(r["kind"] == "duty" for r in items), items
