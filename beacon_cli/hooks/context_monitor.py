"""Cross-platform Stop hook: context-usage threshold monitor.

Python translation of ``bin/context-usage-monitor.sh`` (ms-44 e-854).
Reads a Claude Code Stop-hook JSON payload on stdin, parses the latest
assistant ``usage`` block from the transcript, and at each of the
20 / 40 / 60 / 80 / 100 % thresholds:

  * Records an auto-enriched ``beacon note`` snapshot of the current
    project state (mirrors the bash script's three-section template:
    現在地 / 直近のコミット / Active MS の未消化タスク).
  * Emits a Claude Code ``hookSpecificOutput.additionalContext`` JSON
    asking the model to add a SEPARATE note with the three
    decision-style sections (決定事項 / 議論の要点 / 次のアクション)
    — the post-e-1195 anti-empty-placeholder design.

Contract is byte-equivalent to the bash version so existing tests
(``tests/test_context_monitor_limit.py`` /
``tests/test_context_monitor_auto_note.py``) keep passing when run
against either implementation.

Why Python (vs bash)
--------------------
``bin/context-usage-monitor.sh`` depends on bash + jq + sed + awk + a
sub-shelled python3. None of those are available on Windows pipx (no
bash, no jq), so Claude Code's Stop hook silently fails on Windows
and the user gets no 20-80 % notification. This module is a
self-contained single file that runs with the same stdlib-only Python
that pipx already installed for ``beacon`` itself. (e-854)

Source-of-truth keys + environment variables
--------------------------------------------
The Python port keeps every public contract identical:

  * Stdin JSON: ``{"session_id": "...", "transcript_path": "..."}``.
  * Env var ``BEACON_CONTEXT_LIMIT`` (integer tokens) wins over
    model-name inference. Same name as bash.
  * State file: ``.claude/context-usage-state.json`` (same path).
  * Auto-note body: identical headings to the bash template so
    downstream consumers (Claude prompt, session_notes.jsonl) don't
    care which implementation produced the note.
  * Hook output: ``hookSpecificOutput.additionalContext`` with
    ``hookEventName=Stop`` — same shape as bash printf JSON.

Dry-run mode
------------
Setting ``BEACON_CONTEXT_MONITOR_DRY_RUN=1`` skips both the
``beacon note`` invocation and the state-file write. The hook still
parses everything and emits the hookSpecificOutput JSON so callers
can inspect what *would* have been written. Used by the unit tests to
verify pure logic (threshold computation, template generation) without
side effects.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THRESHOLDS: Tuple[int, ...] = (20, 40, 60, 80, 100)
"""Context-usage thresholds (percent) at which the hook fires."""

DEFAULT_CONTEXT_LIMIT_200K: int = 200_000
DEFAULT_CONTEXT_LIMIT_1M: int = 1_000_000
"""Model-class denominators. ``[1m]`` suffix in the model id forces 1M."""

# A model is the 1M class if its id matches any of these. Bash uses
# ``grep -qE '\[1m\]|-1m$|1m-'`` — replicated exactly.
_ONE_M_MODEL_RE = re.compile(r"\[1m\]|-1m$|1m-")

STATE_FILE_REL = Path(".claude") / "context-usage-state.json"
"""Per-project state file. Same path as the bash script."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    """Stderr log line. Prefix matches the bash ``[context-monitor]`` tag."""
    sys.stderr.write(f"[context-monitor] {msg}\n")


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _latest_assistant_usage(transcript_path: str) -> Tuple[Optional[dict], str]:
    """Walk the JSONL transcript in reverse and return the most recent
    ``(usage_dict, model_name)`` pair from an assistant message.

    Returns ``(None, "")`` when no usable line is found. Never raises —
    the hook contract is "fail-safe silent exit, don't block the user".
    """
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return (None, "")

    for line in reversed(lines):
        try:
            d = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        msg = d.get("message") or {}
        if msg.get("role") == "assistant" and "usage" in msg:
            return (msg.get("usage") or {}, msg.get("model", "") or "")
    return (None, "")


def _resolve_context_limit(model_name: str) -> int:
    """Apply the three-step priority: env var > model inference > 200K."""
    env_limit = os.environ.get("BEACON_CONTEXT_LIMIT", "").strip()
    if env_limit:
        try:
            return int(env_limit)
        except ValueError:
            pass  # malformed env var: fall through to inference
    if model_name and _ONE_M_MODEL_RE.search(model_name):
        return DEFAULT_CONTEXT_LIMIT_1M
    return DEFAULT_CONTEXT_LIMIT_200K


def _current_context_tokens(usage: dict) -> int:
    """Sum the three token buckets the bash jq pipeline sums.

    ``input_tokens + cache_read_input_tokens + cache_creation_input_tokens``
    — output tokens are intentionally NOT counted (they don't return to the
    context window on the next turn).
    """
    def _i(v) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    return (
        _i(usage.get("input_tokens"))
        + _i(usage.get("cache_read_input_tokens"))
        + _i(usage.get("cache_creation_input_tokens"))
    )


# ---------------------------------------------------------------------------
# Threshold logic
# ---------------------------------------------------------------------------


def _classify_thresholds(
    percent: int, notified: List[int]
) -> Tuple[Optional[int], List[int]]:
    """Return ``(triggered_threshold, all_crossed)``.

    * ``all_crossed`` is every threshold ``<= percent`` (regardless of
      notified state) — these are the ones the state file should mark
      as notified after this fire (so a session that jumps from 0 → 67 %
      doesn't later fire 20 / 40 separately).
    * ``triggered_threshold`` is the highest threshold in ``all_crossed``
      that wasn't already notified, or ``None`` when there's nothing new.
    """
    notified_set = set(notified)
    all_crossed = [t for t in THRESHOLDS if percent >= t]
    new = [t for t in all_crossed if t not in notified_set]
    triggered = max(new) if new else None
    return triggered, all_crossed


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _load_state(state_path: Path, current_session: str) -> Tuple[str, List[int]]:
    """Read previous ``(session_id, notified_thresholds)`` or empty defaults.

    If ``session_id`` mismatches the current one, the bash counterpart
    *resets* notified_thresholds — so a new session starts fresh.
    """
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return ("", [])

    prev_session = data.get("session_id", "") or ""
    if prev_session != current_session:
        _log(f"session changed ({prev_session} -> {current_session}) — resetting state")
        return (prev_session, [])

    raw_thresholds = data.get("notified_thresholds")
    if isinstance(raw_thresholds, list):
        try:
            return (prev_session, sorted({int(t) for t in raw_thresholds}))
        except (TypeError, ValueError):
            return (prev_session, [])
    if isinstance(raw_thresholds, str):
        # bash sometimes serializes the list as a JSON string in the field
        # (jq -r quirk). Be defensive.
        try:
            arr = json.loads(raw_thresholds)
            return (prev_session, sorted({int(t) for t in arr}))
        except (TypeError, ValueError, json.JSONDecodeError):
            return (prev_session, [])
    return (prev_session, [])


def _save_state(state_path: Path, session_id: str, notified: List[int]) -> None:
    """Persist the merged notified list. Never raises."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session_id, "notified_thresholds": sorted(set(notified))}
        state_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Auto-enrichment helpers (mirrors bash blocks that bake `beacon status`
# / `git log` / `beacon task list` into the note body)
# ---------------------------------------------------------------------------


def _which_beacon() -> Optional[str]:
    return shutil.which("beacon")


def _run_capture(args: List[str], timeout: float = 5.0) -> str:
    """Run ``args`` and return stdout (empty on any failure)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _build_location_text(status_raw: str) -> str:
    """Format the "現在地" block. Mirrors the bash inline python."""
    if not status_raw.strip():
        return ""
    try:
        d = json.loads(status_raw)
    except (ValueError, json.JSONDecodeError):
        return ""
    lines: List[str] = []
    for ms in (d.get("milestones") or []):
        if ms.get("status") == "in_progress":
            lines.append(
                f"- Active MS: {ms.get('id', '?')} \"{ms.get('title', '')}\""
                f" ({ms.get('progress', 0)}% / "
                f"{ms.get('done_tasks', 0)}/{ms.get('total_tasks', 0)}タスク完了)"
            )
    for op in (d.get("operations") or []):
        if op.get("status") == "open":
            lines.append(f"- Active Operation: {op.get('id', '?')} \"{op.get('title', '')}\"")
    return "\n".join(lines)


def _build_recent_commits_text(cwd: Path) -> str:
    """Past 6h git commits, max 10 lines. Empty repo => fallback message."""
    fallback = "（直近6時間にコミットなし）"
    try:
        result = subprocess.run(
            ["git", "log", "--since=6 hours ago", "--pretty=format:- %h: %s"],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    if result.returncode != 0 or not result.stdout.strip():
        return fallback
    lines = result.stdout.splitlines()[:10]
    return "\n".join(lines) if lines else fallback


def _build_pending_tasks_text(beacon: Optional[str], cwd: Path) -> str:
    """Active MS の未消化タスク (priority order, max 8)."""
    if not beacon:
        return "（beacon CLI 未検出）"

    status_raw = _run_capture([beacon, "status", "--json"])
    if not status_raw.strip():
        return "（beacon status 取得失敗）"
    try:
        d = json.loads(status_raw)
    except (ValueError, json.JSONDecodeError):
        return "（取得エラー: JSONDecodeError）"

    active = next(
        (ms for ms in (d.get("milestones") or []) if ms.get("status") == "in_progress"),
        None,
    )
    if not active:
        return "（active MS なし）"

    tasks_raw = _run_capture([beacon, "task", "list", "--ms", active["id"], "--json"])
    if not tasks_raw.strip():
        return "（タスク取得失敗）"
    try:
        tasks = json.loads(tasks_raw)
    except (ValueError, json.JSONDecodeError):
        return "（取得エラー: JSONDecodeError）"

    order = {"highest": 0, "high": 1, "middle": 2, "low": 3, "lowest": 4, "-": 5}
    pending: List[Tuple[str, str, str]] = []

    def _walk(entries):
        for e in entries or []:
            if e.get("type") == "task" and e.get("status") != "done":
                p = (e.get("meta") or {}).get("priority") or "-"
                desc = (e.get("description") or "")[:120]
                pending.append((p, e.get("id", ""), desc))
            _walk(e.get("entries") or [])

    _walk(tasks.get("entries") or [])
    if not pending:
        return "（未消化タスクなし）"

    pending.sort(key=lambda t: order.get(t[0], 9))
    lines = []
    for p, eid, desc in pending[:8]:
        lines.append(f"- [{p:7}] {eid}: {desc}")
    return "\n".join(lines)


def _build_note_body(
    *,
    percent: int,
    note_date: str,
    location_text: str,
    recent_commits_text: str,
    pending_tasks_text: str,
) -> str:
    """Compose the note body. Headings match the bash version byte-for-byte
    so ``test_context_monitor_auto_note.py`` assertions stay green."""
    loc = location_text or "（取得失敗）"
    return (
        f"## コンテキストサマリー（自動記録 {percent}% / {note_date}）\n"
        "\n"
        "### 現在地（自動取得）\n"
        f"{loc}\n"
        "\n"
        "### 直近のコミット（過去 6 時間 / 自動取得）\n"
        f"{recent_commits_text}\n"
        "\n"
        "### Active MS の未消化タスク（優先度順 / 自動取得）\n"
        f"{pending_tasks_text}\n"
    )


def _build_template_instruction(
    *,
    percent: int,
    current_context: int,
    context_limit: int,
    triggered_threshold: int,
    note_date: str,
) -> str:
    """The Claude-facing additionalContext. Mirrors the bash ms-57 e-1195
    decision-section template — keep this prose stable so prompts behave
    identically across OSes."""
    context_limit_fmt = f"{context_limit:,}"
    return (
        f"BEACON [コンテキスト {percent}% / {current_context}/{context_limit_fmt} tokens]\n"
        f"閾値 {triggered_threshold}% 到達 — session note の **意思決定追記** を依頼します。\n"
        "\n"
        "script が現在地 / 直近コミット / 未消化タスクは自動取得済 (beacon note list で確認可)。\n"
        "ただし auto-取得は **状態スナップショット** であり、引き継ぎの本質である\n"
        "『なぜそう判断したか・何を議論したか・次セッションで何をやるか』は AI が追記しないと\n"
        "ノートとしての価値が成立しません。空欄で session-end するのは避けてください。\n"
        "\n"
        "次の 3 セクションを **必ず別 note として** 追加してください:\n"
        "\n"
        f"beacon note \"## このセッションで決めたこと ({note_date})\n"
        "\n"
        "### 決定事項\n"
        "- (何を選んで何を捨てたか — 技術選定 / scope / 優先順位)\n"
        "- (設計判断 — アーキテクチャ / API 形 / データ構造)\n"
        "- (運用ルール / ガイドライン)\n"
        "\n"
        "### 議論の要点 (なぜそう判断したか)\n"
        "- (選んだ理由)\n"
        "- (代替案を退けた理由)\n"
        "- (ユーザー / 他 session からの指摘・フィードバック)\n"
        "- (想定外の発見)\n"
        "\n"
        "### 次のアクション / 残された論点\n"
        "- (次セッションで先に着手すべきこと)\n"
        "- (未解決の不確実性)\n"
        "- (post-merge で確認が必要なこと)\n"
        "\"\n"
        "\n"
        "書き方の質問:\n"
        "- このセッションで何を選んで何を捨てたか?\n"
        "- なぜその判断になったか? (代替案を退けた理由含む)\n"
        "- ユーザーから出た指摘・フィードバックは何?\n"
        "- 次セッションで先に着手すべきこと、未解決の不確実性は?\n"
        "- 自動取得の現在地・コミット・タスク一覧と同程度かそれ以上の情報量で書いてください。\n"
        "\n"
        "参考: CORE doc 'beacon-note-writing-principle' (良いノート / 悪いノートの例) を参照してください。"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _is_dry_run() -> bool:
    return os.environ.get("BEACON_CONTEXT_MONITOR_DRY_RUN", "").strip() in ("1", "true", "yes")


def main(argv: Optional[list] = None) -> int:
    """Console-script entry point. Returns 0 always (fail-safe contract)."""
    try:
        return _main_impl()
    except Exception:
        # Defensive: a hook bug must never block the user's Stop event.
        return 0


def _main_impl() -> int:
    # ─── Beacon project root guard ───────────────────────────────────────────
    if not Path(".beacon/project.json").is_file():
        return 0

    # ─── stdin → payload ─────────────────────────────────────────────────────
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw.strip():
        _log("empty stdin — skipping")
        return 0
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        _log("malformed stdin JSON — skipping")
        return 0

    session_id = (payload.get("session_id") or "").strip()
    transcript_path = (payload.get("transcript_path") or "").strip()
    if not session_id or not transcript_path:
        _log("session_id or transcript_path not found in payload — skipping")
        return 0
    if not Path(transcript_path).is_file():
        _log(f"transcript file not found: {transcript_path} — skipping")
        return 0

    # ─── usage + model ───────────────────────────────────────────────────────
    usage, model_name = _latest_assistant_usage(transcript_path)
    if not usage:
        _log("no assistant usage data found in transcript — skipping")
        return 0

    context_limit = _resolve_context_limit(model_name)
    _log(f"context_limit={context_limit} (model={model_name or 'unknown'})")

    current_context = _current_context_tokens(usage)
    if current_context <= 0:
        _log("failed to parse usage tokens — skipping")
        return 0

    percent = int(current_context / context_limit * 100)

    # 100% を超えるのは post-compaction continuation。bash と同じくスキップ。
    if percent > 100:
        _log(f"percent={percent} > 100 (post-compaction continuation) — skipping")
        return 0

    _log(
        f"session={session_id} context={current_context}/{context_limit} "
        f"percent={percent}%"
    )

    # ─── state ───────────────────────────────────────────────────────────────
    state_path = Path(STATE_FILE_REL)
    prev_session, notified = _load_state(state_path, session_id)

    triggered, all_crossed = _classify_thresholds(percent, notified)

    if triggered is None:
        # session change should still bump the state file (matches bash).
        if prev_session != session_id and not _is_dry_run():
            _save_state(state_path, session_id, notified)
        return 0

    # ─── enrichment + note ───────────────────────────────────────────────────
    note_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    beacon = _which_beacon()
    status_raw = _run_capture([beacon, "status", "--json"]) if beacon else ""

    location_text = _build_location_text(status_raw)
    recent_commits_text = _build_recent_commits_text(Path.cwd())
    pending_tasks_text = _build_pending_tasks_text(beacon, Path.cwd())

    note_body = _build_note_body(
        percent=percent,
        note_date=note_date,
        location_text=location_text,
        recent_commits_text=recent_commits_text,
        pending_tasks_text=pending_tasks_text,
    )

    if not _is_dry_run():
        # Persist state BEFORE invoking `beacon note` so a hanging beacon
        # call doesn't cause the same threshold to re-fire next time.
        _save_state(state_path, session_id, sorted(set(notified) | set(all_crossed)))

        if beacon:
            try:
                subprocess.run(
                    [beacon, "note", note_body],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass  # never fail the hook on note error

    _log(f"threshold {triggered}% triggered — sending notification")

    # ─── additionalContext → stdout ──────────────────────────────────────────
    instruction = _build_template_instruction(
        percent=percent,
        current_context=current_context,
        context_limit=context_limit,
        triggered_threshold=triggered,
        note_date=note_date,
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": instruction,
        }
    }
    sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
