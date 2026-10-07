"""Structural guard: tests must not create projects on the production cloud.

ms-123 / e-4029. The root cause of the 48 throwaway ``phase4-test`` projects
that piled up in the production directory was simple: a test / one-off check
called the project-creation API against production and never cleaned up. When
the test crashed (or just forgot teardown), the project leaked — and 45 of
them did.

Cleaning up the residue (e-4028) without closing this tap means the same
garbage accumulates again. So this module closes the tap in two layers, per
the user's "構造で防ぐ" principle (close it with a code constraint, not a
prompt request):

  第一層 (structure) — ``guard_prod_project_write``: a hard barrier at the single
    choke point (``ApiClient.create_project``). In a test context, creating a
    project on the production cloud raises instead of writing. A test
    physically cannot leak a prod project. This is the primary defense.

  第二層 (insurance) — ``disposable_project``: for the rare test that must hit
    a real (non-prod) cloud, a context manager that guarantees the created
    project is archived on exit — even if the test body raises. Teardown can't
    be forgotten because it's structural (a ``finally``), so a crash mid-test
    still cleans up.

The escape hatch (``BEACON_ALLOW_PROD_TEST_WRITE=1``) is deliberately noisy:
an author has to set it on purpose, which is the moment to also reach for
``disposable_project``.
"""

from __future__ import annotations

import contextlib
import os
import urllib.parse

class ProdWriteBlocked(RuntimeError):
    """Raised when a test context is refused a write to the production cloud
    (ms-108 e-5300 AX review). A DISTINCT type — not a bare ``RuntimeError`` —
    so a caller that has a broad ``except RuntimeError`` fallback (e.g.
    ``cmd_bus_send``'s legacy-envelope path, which treats a RuntimeError as
    "server rejected the envelope, fall back to the legacy POST") can tell a
    guard refusal apart and RE-RAISE it instead of swallowing it into a
    degraded path. Subclasses ``RuntimeError`` so existing ``except
    RuntimeError`` / ``assertRaises(RuntimeError)`` sites keep matching."""


# The production Beacon cloud. Writing test projects here is the leak.
_PROD_HOSTS = frozenset({"beacon-ai.dev", "www.beacon-ai.dev"})
_PROD_HOST_SUFFIXES = (".beacon-ai.dev",)


def is_test_context() -> bool:
    """True when the current process is a test / CI driver.

    ``PYTEST_CURRENT_TEST`` is set by pytest for the duration of each test;
    ``BEACON_TEST_MODE`` is set explicitly (conftest.py sets it for the whole
    suite, and CI can export it) so subprocess children that invoke the
    ``beacon`` CLI inherit the test context too.
    """
    if os.environ.get("BEACON_TEST_MODE") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def is_prod_api_url(url: str) -> bool:
    """True when ``url`` points at the production Beacon cloud."""
    if not url:
        return False
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in _PROD_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in _PROD_HOST_SUFFIXES)


# The single escape hatch shared by every prod-test-write guard below. It is
# deliberately process-wide and shared (not per-guard): a test that legitimately
# needs to touch prod is rare, and one loud, greppable opt-in is easier to audit
# than a family of near-identical ones. Because it is shared, each guard's error
# message states that scope explicitly (下記 AX finding, ms-108 e-5194) so an
# author enabling it for one write path knows it unlocks all of them.
_PROD_TEST_WRITE_HATCH = "BEACON_ALLOW_PROD_TEST_WRITE"

# 「この抜け道は何を解錠するか」の文言 (ms-166 e-6637 の保守性レビュー M-1)。
# 以前は 3 つの拒否文言に手書きで写していたため、3 つ目のガード (判断記録) を足した
# 瞬間に先の 2 つが「project AND bus」のまま古くなった。守る対象が増えても文言が
# 揃うように 1 箇所から作る — 新しいガードを足す人はここに 1 語足すだけでよい。
# M-1 では 3 つの拒否文言に射程を手書きしていたのを「1 箇所から作る」形に直した。
# ところが **その 1 箇所の中身が手書きの文字列だった** ので、4 つ目のガード (読み取り)
# を足したときに "project, bus AND decision" のまま古くなり、READ を拒否しているのに
# 「この抜け道は書き込み 3 種を解錠する」と出た (独立 AX レビュー PR#785 AX-1)。
# **単一真実源を作っても、その中身が守る対象の集合と照合されていなければ、同じ drift が
# 中身の側で再発する。**
#
# なので射程は **ガードの集合から導く**。新しいガードを足す人はこの表に 1 行足すだけで
# 全文言が揃い、足し忘れたらテストが落ちる
# (tests/test_prod_read_guard_e6819.py::test_every_guard_is_named_in_the_hatch_scope)。
_HATCH_SCOPED_GUARDS = {
    "guard_prod_project_write": "project",
    "guard_prod_bus_write": "bus",
    "guard_prod_decision_write": "decision",
    # ms-166 e-6854: 廊下の受け止め (= 上の 3 つで覆われない全ての書き込み)。
    # ラベルは兄弟と同じく 1 語に揃える (独立 AX レビュー AX-5: 3 語の句が 1 つだけ
    # 混ざると列挙が不自然になり、次にガードを足す人がどちらの書式に倣うか迷う)。
    "guard_prod_write": "writes",
    "guard_prod_read": "read",
}


def _hatch_scope() -> str:
    """抜け道が解錠する対象の列挙 — 表から組み立てる (手書きしない)。"""
    labels = list(dict.fromkeys(_HATCH_SCOPED_GUARDS.values()))
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " AND " + labels[-1]


def _isolation_options_note() -> str:
    """「どう隔離すればよいか」の案内 — 全ガードの拒否文言が共有する 1 文。

    ms-166 e-6819 の保守性レビュー M-6: 推奨する手段 (fixture 2 つと非本番の base_url) を
    bus / decision / read の文言にそれぞれ手で書き直していた。``_hatch_note`` で学んだ
    教訓 (3 つ目のガードを足した瞬間に先の 2 つが古くなる) が、もう一段大きいこの重複には
    適用されていなかった。fixture 名が変わったときに 1 箇所で直るようにする。

    各ガードに固有の事情 (追記専用で消せない / 結果が再現しない 等) は呼び出し側が
    前後に足す。ここが持つのは **どの隔離手段があるか** だけ。
    """
    return ("use the ``fake_cloud_config`` fixture (it points BEACON_PROJECT_FILE at a "
            "tmp cloud.json so _get_cloud_config_path resolves non-prod in EVERY "
            "module), force local mode with the ``isolated_project`` fixture (a tmp "
            ".beacon with no cloud.json, so _is_cloud_mode() is False), or give the "
            "ApiClient a local/sandbox base_url")


def _hatch_note() -> str:
    """全ガードの拒否文言が共有する「抜け道とその射程」の 1 文。"""
    return (f"set {_PROD_TEST_WRITE_HATCH}=1 (this shared hatch unlocks ALL "
            f"prod test access — {_hatch_scope()} — for the process)")


def _prod_test_write_blocked(base_url: str) -> bool:
    """True when a prod write from a test context must be refused — the single
    decision chain (is_test_context → is_prod_api_url → hatch) behind every
    ``guard_prod_*_write`` below. Extracted so the rule has one source of truth:
    a 3rd write guard (DM / envelope …) is a thin shell over this, and changing
    the hatch name or adding staging can't drift half the guards (ms-108 e-5194
    maint finding). False (= allowed) outside a test context, for non-prod
    targets, or when the shared hatch is set."""
    if not is_test_context():
        return False
    if not is_prod_api_url(base_url):
        return False
    if os.environ.get(_PROD_TEST_WRITE_HATCH) == "1":
        return False
    return True


def prod_test_write_blocked(base_url: str) -> bool:
    """Public accessor for the single decision chain (ms-173 PR#758 maint
    finding): a best-effort write path that wants a bool (refuse silently, not
    raise) reads THIS instead of re-composing is_test_context × is_prod_api_url
    by hand — a hand-rolled copy silently drops the ``BEACON_ALLOW_PROD_TEST_
    WRITE`` hatch and forks the rule into a second truth source."""
    return _prod_test_write_blocked(base_url)


def guard_prod_project_write(base_url: str) -> None:
    """Raise if a test context is about to write a project to production.

    Covers every project-creating write path: ``create_project`` (POST) and
    ``put_project`` (PUT upsert) both land on ``/api/projects/{id}`` and both
    can materialize a new project, so both are guarded. Guarding only one
    would leave the other as a leak bypass.

    No-op outside a test context (normal CLI use is unaffected) and no-op for
    non-prod targets (local mode, staging, a sandbox cloud). The escape hatch
    ``BEACON_ALLOW_PROD_TEST_WRITE=1`` lets a test opt in, but such a test must
    wrap creation in :func:`disposable_project` so teardown always archives it.
    """
    if not _prod_test_write_blocked(base_url):
        return
    raise ProdWriteBlocked(
        "cloud_write_guard: refusing to write a project to the production "
        f"cloud ({base_url}) from a test context. Tests must use local mode "
        "or a sandbox/staging cloud. If this test genuinely must hit prod, "
        f"{_hatch_note()} AND wrap "
        "creation in cloud_write_guard.disposable_project(...) so a crash "
        "can't leak it."
    )


def guard_prod_bus_write(base_url: str) -> None:
    """Raise if a test context is about to post a bus event to production.

    ``guard_prod_project_write`` only covers project-*creating* writes, so a
    test that constructs a real ``ApiClient`` and posts to the bus of an
    already-existing production project slipped through (the exact leak that
    let a non-hermetic operation-trigger unit test spray ``op-1`` "test"
    events onto the live bus every time the suite ran — a wrong monkeypatch
    target meant the helper's local-mode early-return never fired). Guard the
    bus **post** at the same choke point so no test can leak a bus *post* to
    the live bus even when its own mock is wired up incorrectly.

    Scope (ms-108 e-5216): this same choke point now guards EVERY bus-mutating
    verb, not just ``post_bus_event``. The sibling writes — ``advance_bus_cursor``
    / ``ack_bus_event_receipt`` / ``issue_bus_envelope`` / ``respond_dm_approval``
    — each call ``guard_prod_bus_write`` before their POST (e-5194 AX finding: a
    guard that covers one write door leaves the others as bypass doors). So a test
    can no longer leak ANY bus write to the live bus even with a mis-wired mock.

    No-op outside a test context (normal CLI / autonomous use is unaffected)
    and no-op for non-prod targets (local mode, staging, a sandbox cloud). The
    escape hatch ``BEACON_ALLOW_PROD_TEST_WRITE=1`` lets a test that genuinely
    must exercise the live bus opt in.
    """
    if not _prod_test_write_blocked(base_url):
        return
    raise ProdWriteBlocked(
        "cloud_write_guard: refusing to post a bus event to the production "
        f"cloud ({base_url}) from a test context. Fake cloud mode the canonical "
        f"way: {_isolation_options_note()}. Unlike a prod project write, a bus "
        "write has no disposable_project teardown counterpart (bus events are "
        "server-side, not leaked directory residue). If this test genuinely must "
        f"hit the prod bus, {_hatch_note()}."
    )


def guard_prod_decision_write(base_url: str) -> None:
    """Raise if a test context is about to append a decision to production.

    ms-166 e-6637. The decision stream (= 誰が何をなぜ決めたかの恒久記録) is
    **append-only**: there is no delete path, so a row a test writes can never be
    taken back. That makes it the worst place for test residue — worse than a
    leaked project (archivable) or a bus event (transient). Measured on the live
    stream: of one session's 100 decisions, roughly 90 were its own test runs
    (ms-9 / opp-3 / ms-T and others), so the audit arm — the thing a human reads
    to check what an AI decided — was mostly fiction.

    Why the guard belongs HERE and not in the callers: ``record_decision`` is the
    single door every writer passes through, so guarding it covers **every** call
    site in ``lib/`` (spread over 7 modules today) plus every future one. A
    caller-layer fix would leave all the others open — the same "one door guarded,
    the others are bypass doors" shape that e-5194 found for the bus and e-5216
    then closed at the choke point. (No count is quoted here on purpose: the
    number drifts, and a stale number offered as the justification is worse than
    none. ``tests/test_decision_write_guard_e6637`` pins the structural facts.)

    No-op outside a test context (normal CLI / autonomous use is unaffected) and
    no-op for non-prod targets (local mode, staging, a sandbox cloud). The escape
    hatch ``BEACON_ALLOW_PROD_TEST_WRITE=1`` lets a test that genuinely must write
    to the live stream opt in — but note there is **no teardown counterpart** for
    an append-only stream (unlike ``disposable_project``), so an opted-in test
    leaves a permanent row.
    """
    if not _prod_test_write_blocked(base_url):
        return
    raise ProdWriteBlocked(
        "cloud_write_guard: refusing to append a decision to the production "
        f"cloud ({base_url}) from a test context. The decision stream is "
        "append-only — a test row cannot be deleted afterwards. Fake cloud mode "
        f"the canonical way: {_isolation_options_note()}. If this test genuinely must write to "
        f"the live stream, {_hatch_note()}; there is no teardown that can "
        "remove the row afterwards."
    )

def guard_prod_write(base_url: str, *, method: str = "", path: str = "") -> None:
    """Raise if a test context is about to WRITE to production — **any** write.

    ms-166 e-6854. The sibling guards above each cover one kind of door
    (project / bus / decision). That axis was the *kind of payload*, and it left
    53 of 61 write methods open: documents, notes, retros, sessions, purge,
    operations, machine keys, organizations, claims, trek — all reachable from a
    test against the live cloud. This repo closed a door one-at-a-time four times
    (e-4029 / e-5194 / e-5216 / e-6637); the fifth time, close the corridor.

    Why guarding every write is the right default rather than a per-door
    judgement: the guard is **inert outside a test context and inert for
    non-prod targets**, so the production cost of covering a door is zero. A
    door only needs an exemption if a *test* genuinely must write to the *live*
    cloud — and that is what the escape hatch is for, per test, in the open.
    There is no door for which "a test writing to production" is the desired
    behaviour, which is why the earlier per-door triage never found a reason to
    leave one open.

    This is a **backstop, not a replacement**: the specific guards fire first
    (they are called inside their own methods, before the transport is reached),
    so their richer diagnosis — "the decision stream is append-only", "a leaked
    project can be archived" — is what the author sees for those doors. This one
    catches everything else, including doors that do not exist yet.

    ``method`` / ``path`` are carried into the message so the author learns which
    write was refused without reading a traceback.
    """
    if not _prod_test_write_blocked(base_url):
        return
    where = f" ({method.upper()} {path})" if (method or path) else ""
    raise ProdWriteBlocked(
        f"cloud_write_guard: refusing a write{where} to the production cloud "
        f"({base_url}) from a test context. Fake cloud mode the canonical way: "
        f"{_isolation_options_note()}. If this test genuinely must write to the "
        f"live cloud, {_hatch_note()} — and prefer `disposable_project` so the "
        "residue is cleaned up even if the test raises."
    )


def guard_prod_read(base_url: str) -> None:
    """Raise if a test context is about to READ from production.

    ms-166 e-6819. A test that reads the live cloud is not destructive, but its
    result depends on **whatever production happens to hold right now** — so it
    passes or fails for reasons that have nothing to do with the change under
    test, and it cannot be reproduced. (Observed while fixing e-6852: two unit
    tests were resolving a work item's parent out of the developer's real
    project, and one of them only passed because that project happened to
    contain the id they used.)

    Why the guard sits on the shared transport rather than on each reader: the
    client exposes ~36 read verbs. Guarding them one at a time is the "one door
    guarded, the others are bypass doors" shape that e-5194 found for the bus and
    e-6637 found for decisions. ``_request`` is the single hallway every read
    passes through, so the GET leg of it is the one place to ask the question.

    No-op outside a test context (normal CLI / autonomous use is unaffected) and
    no-op for non-prod targets. The escape hatch is the SAME shared one as the
    write guards (``BEACON_ALLOW_PROD_TEST_WRITE=1``) — deliberately, because a
    test that opts into touching prod at all should make that one declaration
    rather than collect a flag per verb.
    """
    if not _prod_test_write_blocked(base_url):
        return
    raise ProdWriteBlocked(
        "cloud_write_guard: refusing to READ from the production cloud "
        f"({base_url}) from a test context. A test whose result depends on live "
        "production state is not reproducible — it passes or fails for reasons "
        "unrelated to the change under test. Point the test at fixture data "
        "instead: stub the reader (``monkeypatch.setattr(commands_shared, "
        f"'load_project', ...)``), or {_isolation_options_note()}. "
        "If this test genuinely must read prod, "
        f"{_hatch_note()}."
    )


def _default_cleanup(client, project_id: str) -> None:
    """Best-effort archive of a disposable project via the envelope path.

    Mirrors ``beacon project cleanup`` (e-4028): mint a T1 envelope authorizing
    ``project.archive`` and archive with it. Best-effort — any failure here is
    swallowed so it never masks the test's own result; the worst case degrades
    to the same leak we're guarding against, which the periodic cleanup catches.
    """
    try:
        env = client.issue_bus_envelope(
            project_id, tier="T1", actions_authorized=["project.archive"],
        )
        client.archive_project(project_id, env)
    except Exception as e:
        # ms-123 review (AX + maintainability consensus): swallow so a teardown
        # failure never masks the test's own result, but NEVER silently — a
        # failed cleanup IS the leak this module exists to prevent, so surface
        # the fact + the manual recovery command. Silent-swallow made the leak
        # recur under a "guarded" label.
        import sys as _sys
        print(f"cloud_write_guard: cleanup of {project_id} FAILED ({e}). "
              f"手動で archive してください: beacon project cleanup --confirm",
              file=_sys.stderr)


@contextlib.contextmanager
def disposable_project(
    client,
    project_id: str,
    name: str,
    objective: str = "",
    *,
    cleanup=None,
):
    """Create a cloud project guaranteed to be cleaned up on exit (第二層).

    For a test that must exercise a real (non-prod) cloud. Creates the project,
    yields its id, and on exit — success OR exception — runs ``cleanup`` (an
    optional ``callable(client, project_id)``; defaults to archiving via the
    envelope path). The teardown is a ``finally``, so a crashing test body
    still cleans up: the leak vector is structurally removed.

    Note: ``create_project`` still routes through :func:`guard_prod_project_write`,
    so pointing this at the production cloud from a test remains blocked unless
    the author has explicitly set ``BEACON_ALLOW_PROD_TEST_WRITE=1``.
    """
    client.create_project(project_id, name, objective)
    try:
        yield project_id
    finally:
        (cleanup or _default_cleanup)(client, project_id)
