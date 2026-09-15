"""The beacon-view name is consistent across all four layers (ms-170 e-6476).

Guards scripts/check-viewer-name-drift.py, which pins the cross-layer contract
that build.sh / cmd_view.py / release-build.yml / beacon.rb all name the bundled
Go viewer identically. Includes a test-the-test: an injected drift MUST be
detected, so the guard can't silently pass on a real rename.
"""
import importlib.util
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "check_viewer_name_drift",
    os.path.join(_ROOT, "scripts", "check-viewer-name-drift.py"))
_CVND = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CVND)


def test_all_four_layers_name_the_viewer_identically():
    problems = _CVND.check()
    assert problems == [], "beacon-view name drift:\n" + "\n".join(problems)


def test_every_layer_actually_contributes_a_name():
    """Extractors must each find the name — otherwise 'consistent' is vacuous."""
    found = _CVND.extract_names()
    for layer, names in found.items():
        assert names, f"{layer}: extractor matched nothing (would silently pass)"
        assert names == {"beacon-view"}, f"{layer}: {sorted(names)}"


def test_guard_fails_on_injected_drift(monkeypatch):
    """test-the-test: rename the viewer in one layer -> check() must complain."""
    orig = _CVND._read

    def drifted(rel):
        s = orig(rel)
        if rel == "packaging/homebrew/beacon.rb":
            s = s.replace('bin/"beacon-view"', 'bin/"beacon-viewer"')
        return s

    monkeypatch.setattr(_CVND, "_read", drifted)
    problems = _CVND.check()
    assert problems, "guard did not detect an injected rename (useless guard)"
    assert any("beacon-viewer" in p for p in problems)
