"""Every downgrade reason has its own advice, enumerated — not listed by hand.

ms-181 e-7112, 受入条件 3
========================
The ms-185 fork (PR #809) fixed the defect itself: the inbox hook used to print
one fixed line — "channel not in bus_auto_execute_channels" — for every reason
an auto-execute event was downgraded. Two of the three reasons only happen when
the channel IS on the allowlist, so that line was the opposite of the truth and
sent a reading AI off to add an allowlist entry, away from the real cause (a
missing signature, or a retired channel).

What that change did NOT leave behind is 受入条件 3: a test that goes red when
a reason is added and its advice branch is forgotten. Measured on 2026-10-20,
every existing test names the reasons by hand —
``(ALLOWLIST_MISS, PROVENANCE, RETIRED, UNKNOWN)`` and
``[PROVENANCE, RETIRED, ALLOWLIST_MISS]`` — so a fourth real reason added to
``bus_delivery`` would fall into the "I don't know this tag" fallback and no
test would notice. The fallback is safe (it refuses to mislabel), but silence
is not the same as coverage: the operator still gets no advice, and nothing
tells the person who added the reason that they left a gap.

So this module **discovers** the reason set from the module that declares it and
compares it against the branches that consume it. That is the "理由の集合と案内の
分岐の集合を機械で突き合わせる" the acceptance criterion asks for, and the reason
it explicitly forbids measuring by source-string presence: the fork's own test
checked that an assignment existed in the source and therefore certified as
"safe" a renderer that ignored the value (受入条件 4).
"""

import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The fallback wording ``_downgrade_advice`` returns for a tag it does not
#: know. A reason that lands here has NO advice of its own — which is exactly
#: the gap this module exists to detect.
_UNKNOWN_MARKER = "この hook が知りません"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bd():
    import bus_delivery
    return bus_delivery


@pytest.fixture(scope="module")
def claude_hook():
    return _load(os.path.join("bin", "beacon-bus-inbox-hook.py"), "_claude_hook")


@pytest.fixture(scope="module")
def codex_hook():
    return _load(os.path.join("scripts", "codex-inbox-hook.py"), "_codex_hook")


def _declared_reasons(bd) -> dict:
    """Every downgrade reason ``bus_delivery`` declares, discovered by name.

    Includes the private allowlist-miss sentinel (the empty string): it is a
    real reason with a real advice branch, and leaving it out would let the
    set-comparison pass while the most common case regressed.
    """
    found = {name: getattr(bd, name) for name in dir(bd)
             if name.startswith("DOWNGRADE_")
             and isinstance(getattr(bd, name), str)}
    found["_DOWNGRADE_ALLOWLIST_MISS"] = getattr(
        bd, "_DOWNGRADE_ALLOWLIST_MISS", "")
    return found


def test_the_reason_set_is_discovered_not_assumed(bd):
    """The enumeration has to actually find something.

    A discovery helper that silently returns {} would make every test below
    vacuously true — the shape of false green this milestone is about.
    """
    reasons = _declared_reasons(bd)
    assert len(reasons) >= 3, reasons
    assert "DOWNGRADE_NON_SYSTEM_ENVELOPE" in reasons
    assert "DOWNGRADE_RETIRED_CHANNEL" in reasons
    assert "_DOWNGRADE_ALLOWLIST_MISS" in reasons


def test_every_declared_reason_has_advice_of_its_own(bd, claude_hook):
    """受入条件 3: add a reason, forget the advice branch, go red.

    This is the test the fix was missing. It reads the declaration rather than
    a hand-written list, so a reason added tomorrow is covered without anyone
    remembering to extend a tuple here.
    """
    missing = []
    for name, value in sorted(_declared_reasons(bd).items()):
        advice = claude_hook._downgrade_advice(value)
        if _UNKNOWN_MARKER in advice:
            missing.append(name)
    assert not missing, (
        "これらの降格理由に専用の案内がありません (未知タグの fallback に落ちて "
        f"います): {missing}。bus_delivery に理由を足したら "
        "_downgrade_advice にも分岐を足してください。"
    )


def test_no_two_declared_reasons_share_one_advice(bd, claude_hook):
    """Distinct causes must read as distinct, or the advice cannot be acted on.

    Two reasons collapsing onto one wording is how the original defect looked
    from the operator's side, even though each branch "existed".
    """
    seen = {}
    for name, value in sorted(_declared_reasons(bd).items()):
        advice = claude_hook._downgrade_advice(value)
        if advice in seen:
            pytest.fail(f"{name} と {seen[advice]} の案内が同一です: {advice!r}")
        seen[advice] = name


def test_the_advice_never_blames_the_allowlist_when_the_channel_is_on_it(
        bd, claude_hook):
    """受入条件 2, as a property rather than a case list.

    ``non-system-envelope`` and ``retired-provenance-channel`` can only happen
    while the channel IS allowlisted. Advice for those must not point at
    allowlisting — that is the "opposite of the truth" the task names, and the
    direction an AI actually went.
    """
    on_allowlist = [v for k, v in _declared_reasons(bd).items()
                    if k != "_DOWNGRADE_ALLOWLIST_MISS" and v]
    assert on_allowlist, "allowlist 済で起きる理由が 1 つも見つかりません"
    for value in on_allowlist:
        advice = claude_hook._downgrade_advice(value)
        assert "not in bus_auto_execute_channels" not in advice, \
            f"{value!r}: channel は許可リストに在るのに、許可リスト漏れと案内しています"
        assert "auto-execute add" not in advice, \
            f"{value!r}: 許可を足す方向 (= 真因と逆) に誘導しています"


def test_every_declared_reason_lands_in_its_own_counter(bd, codex_hook):
    """The Codex side counts by reason too, and must not fold a new one in.

    Same enumeration, different consumer: the digest's per-reason counters are
    a second place the reason set is read, and a reason added without a counter
    would be reported under ``other`` with no notice block of its own.
    """
    # Named, not probed with getattr-or-skip: a skip reads exactly like a pass
    # in the summary line, so "the function was renamed" would quietly become
    # "this consumer is covered". If the name changes, this test must break.
    tally_fn = codex_hook.tally_downgrade_reasons

    folded = []
    for name, value in sorted(_declared_reasons(bd).items()):
        counts = tally_fn([value], bd=bd)
        if counts.get("other"):
            folded.append(name)
    assert not folded, (
        f"これらの理由が 'other' に畳まれています: {folded} — "
        "digest に専用の件数と案内が出ません。"
    )
