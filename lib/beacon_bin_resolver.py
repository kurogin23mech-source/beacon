"""BEACON_BIN resolution + path health evaluation for install Skills.

ms-93 / e-2276 phase 0: provides a single source of truth for the Codex /
Claude Code install Skills to answer two questions:

1. **Which `beacon` binary should I use?** Resolution order (= e-2276 AC #1):
   1. ``$BEACON_BIN`` env var (if executable + accepts ``--version``)
   2. ``<install_root>/bin/beacon`` (= repo bin, absolute path)
   3. bare ``beacon`` on PATH (= last resort)

2. **Is the chosen binary usable?** Doctor JSON probe (= e-2276 AC #3):
   - Runs ``<bin> doctor --json`` and parses the output
   - Applies the Codex-proposed hard/soft policy:
     - ``hard_fail``: JSON parse fails (= old CLI doesn't understand ``--json``)
       OR ``selected-version-too-old`` OR ``bus-sessions-unavailable``
     - ``soft_warn``: ``multiple-binaries`` (= primary is fine, others present)
     - ``ok``: no PATH signals fire

The 2026-06-25 Codex dogfood confirmed a critical silent-failure path: the
stale homebrew 0.2.1 ``beacon doctor --json`` prints ``OK: all checks passed.``
with exit 0 because the old CLI ignores ``--json`` entirely. Without the
JSON-parse-required gate, an install Skill would call bare ``beacon`` and
declare the environment healthy when it is in fact broken.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path


# Codes that the Skill / wrapper treats as hard fail (= refuse to continue
# install / surface a 1-line WARN with the recommended BEACON_BIN export).
# Codex's 2026-06-25 dogfood proposed this split: PATH problems that block
# DM / sessions usage are hard fails; cosmetic shadowing alone is a warn.
_HARD_FAIL_PATH_CODES = (
    "selected-version-too-old",
    "bus-sessions-unavailable",
)
# Codes that the Skill should mention but not block on.
_SOFT_WARN_PATH_CODES = (
    "multiple-binaries",
)
# Internal codes synthesised by this resolver (= not emitted by the server
# doctor; meaningful only on the wrapper side).
_INTERNAL_CODES = (
    "doctor-json-unavailable",  # candidate accepts the run but stdout is not JSON
    "candidate-not-executable",  # candidate exists but won't run
)


@dataclass
class ProbeResult:
    """Outcome of probing a single candidate ``beacon`` binary."""

    bin_path: str
    source: str  # "env" | "install_root" | "path"
    json_ok: bool
    version_raw: str = ""
    path_signals: list = field(default_factory=list)
    missing_subcommands: tuple = ()
    error: str = ""


@dataclass
class ResolutionResult:
    """End-to-end resolution for the install Skill / wrapper to act on."""

    verdict: str  # "ok" | "soft_warn" | "hard_fail" | "no-candidate"
    selected_bin: str  # absolute path of the chosen beacon (or "" if none)
    selected_source: str  # "env" | "install_root" | "path" | ""
    hard_fail_codes: list = field(default_factory=list)
    soft_warn_codes: list = field(default_factory=list)
    advice: str = ""  # 1-line user-facing recommendation
    candidates_probed: list = field(default_factory=list)  # list of ProbeResult dicts

    def to_dict(self) -> dict:
        d = asdict(self)
        d["candidates_probed"] = [asdict(p) if not isinstance(p, dict) else p
                                  for p in self.candidates_probed]
        return d


def _resolve_candidates(env_bin: str, install_root: str, allow_path_fallback: bool) -> list:
    """Build the ordered candidate list per the resolution policy."""
    candidates = []
    if env_bin:
        candidates.append((str(Path(env_bin)), "env"))
    if install_root:
        repo_bin = str(Path(install_root) / "bin" / "beacon")
        if repo_bin not in [c[0] for c in candidates]:
            candidates.append((repo_bin, "install_root"))
    if allow_path_fallback:
        bare = shutil.which("beacon")
        if bare and bare not in [c[0] for c in candidates]:
            candidates.append((bare, "path"))
    return candidates


def _parse_version_tuple(raw: str):
    """Return (major, minor, patch) from a `beacon <ver>` string, or None."""
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _probe_candidate(bin_path: str, source: str, timeout: float = 5.0) -> ProbeResult:
    """Probe a single candidate directly: version, ``doctor --json`` shape,
    and a direct ``bus``/``sessions`` subcommand probe.

    The direct subcommand probe is the authoritative usability check —
    doctor's PATH signals describe the env's PATH primary, not the
    candidate we selected. The Codex 2026-06-25 dogfood reframed this:
    only the selected binary's own capability matters for "can I use it";
    PATH signals are advisory.
    """
    result = ProbeResult(bin_path=bin_path, source=source, json_ok=False)
    if not Path(bin_path).is_file() or not os.access(bin_path, os.X_OK):
        result.error = "candidate-not-executable"
        return result
    try:
        version_proc = subprocess.run(
            [bin_path, "--version"],
            capture_output=True, text=True, timeout=timeout,
        )
        result.version_raw = (version_proc.stdout or "").strip()
    except Exception as exc:
        result.error = f"version-probe-failed: {exc}"
        return result

    # Direct subcommand probe: does THIS binary know ``bus``/``sessions``?
    # We pin on the exact ``Unknown command`` phrase from the dispatcher.
    missing_subs = []
    for sub in ("bus", "sessions"):
        try:
            r = subprocess.run(
                [bin_path, sub, "--help"],
                capture_output=True, text=True, timeout=timeout,
            )
            combined = (r.stdout or "") + (r.stderr or "")
            if "Unknown command" in combined:
                missing_subs.append(sub)
        except Exception:
            # Transport error → can't say, don't add to missing.
            continue
    result.missing_subcommands = tuple(missing_subs)

    # Doctor JSON probe: the Codex 2026-06-25 dogfood proved this is also
    # the only way to tell ``--json`` is honoured at all — old CLIs ignore
    # the flag and print human text, exiting 0.
    try:
        doctor_proc = subprocess.run(
            [bin_path, "doctor", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:
        result.error = f"doctor-probe-failed: {exc}"
        return result
    stdout = doctor_proc.stdout or ""
    try:
        parsed = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        # Old CLI doesn't understand --json: not necessarily a hard
        # failure on its own (the binary may still have bus/sessions),
        # but ``json_ok`` stays False so the caller knows.
        result.error = "doctor-json-unavailable"
        return result
    if not isinstance(parsed, dict) or "signals" not in parsed:
        result.error = "doctor-json-unavailable"
        return result
    result.json_ok = True
    result.path_signals = [s for s in parsed.get("signals", []) if isinstance(s, dict)]
    return result


_DEFAULT_MIN_VERSION = (0, 30, 0)


def _categorise_signals(probe: ProbeResult) -> tuple:
    """Return (hard_fail_codes, soft_warn_codes) for a probed candidate.

    Hard-fail comes from THIS candidate's own capability — version below
    the floor, or missing ``bus``/``sessions``. Doctor's PATH signals are
    advisory (they describe the env's PATH primary, which may or may not
    be the same binary we selected) and surface as soft_warn.
    """
    hard, soft = [], []
    # Direct subcommand probe = authoritative on whether THIS bin works.
    if probe.missing_subcommands:
        hard.append("bus-sessions-unavailable")
    # Version floor.
    parsed = _parse_version_tuple(probe.version_raw)
    if parsed is not None and parsed < _DEFAULT_MIN_VERSION:
        hard.append("selected-version-too-old")
    # doctor --json unsupported is borderline: if bus/sessions are
    # present and version is OK, the candidate is usable but we lose
    # observability. Soft warn.
    if probe.error == "doctor-json-unavailable":
        soft.append("doctor-json-unavailable")
    # Surface advisory PATH signals from doctor (only when JSON was
    # parseable, so we actually have them).
    for sig in probe.path_signals:
        code = sig.get("code")
        if code in _SOFT_WARN_PATH_CODES:
            soft.append(code)
        # We intentionally do NOT treat ``selected-version-too-old`` /
        # ``bus-sessions-unavailable`` from doctor as hard fail — they
        # describe PATH primary, not our selected. The direct probe
        # above covers the selected candidate.
    return hard, soft


# Per-code one-line descriptions used to compose ``advice`` when the
# verdict is ``soft_warn``. Each entry stays short enough for a Skill
# UX 1-liner. Adding a new soft code = add an entry here.
_SOFT_CODE_DESCRIPTIONS = {
    "multiple-binaries": (
        "other beacon binaries exist on PATH (cosmetic; primary is fine)"
    ),
    "doctor-json-unavailable": (
        "selected beacon does not understand `doctor --json`"
        " (usable, but PATH-health observability is degraded)"
    ),
}


def _describe_soft_code(code: str) -> str:
    """Return a 1-line description for a soft_warn code (or the code itself)."""
    return _SOFT_CODE_DESCRIPTIONS.get(code, code)


def _format_advice(verdict: str, selected_bin: str, hard_codes: list, soft_codes: list) -> str:
    """Compose a 1-line user-facing recommendation.

    Codex 2026-06-25 polish: soft_warn advice must stay accurate for any
    combination of soft codes (the previous wording was hard-coded to the
    ``multiple-binaries`` case and went stale when other codes joined).
    We render per-code descriptions, joined by ``; `` so the advice
    always reads like "Selected is usable. Note: <code1>: <desc1>;
    <code2>: <desc2>".
    """
    if verdict == "no-candidate":
        return ("No usable `beacon` binary found. Set BEACON_BIN to an absolute"
                " path of a recent install (v0.30.0+), or add bin/ to PATH.")
    if verdict == "hard_fail":
        joined = ", ".join(hard_codes) if hard_codes else "doctor-json-unavailable"
        return (f"Selected beacon at {selected_bin} cannot be used ({joined})."
                f" Set BEACON_BIN to an absolute path of a recent install"
                f" (v0.30.0+) and re-run.")
    if verdict == "soft_warn":
        if soft_codes:
            notes = "; ".join(f"{c}: {_describe_soft_code(c)}" for c in soft_codes)
            return (f"Selected beacon at {selected_bin} is usable. Note: {notes}.")
        return f"Selected beacon at {selected_bin} is usable."
    return f"Selected beacon at {selected_bin} is healthy."


def resolve_beacon_bin(
    env_bin: str = "",
    install_root: str = "",
    allow_path_fallback: bool = True,
) -> ResolutionResult:
    """Resolve a usable ``beacon`` binary and report its health.

    Walks the candidate list (env → install_root/bin → PATH) and returns the
    first candidate whose ``doctor --json`` returns parseable JSON without
    hard-fail signals. If no candidate works, ``verdict`` is ``no-candidate``
    (= caller should refuse to proceed).

    The function never raises on transport errors — it captures them as
    ``error`` fields on the per-candidate probe and moves on.
    """
    env_bin = env_bin or os.environ.get("BEACON_BIN", "")
    candidates = _resolve_candidates(env_bin, install_root, allow_path_fallback)
    probes = []
    selected = None
    selected_source = ""

    for bin_path, source in candidates:
        probe = _probe_candidate(bin_path, source)
        probes.append(probe)
        if probe.error == "candidate-not-executable":
            continue
        hard, _soft = _categorise_signals(probe)
        if hard:
            # Hard fail on this candidate's own capability — try next.
            continue
        selected = probe
        selected_source = source
        break

    if selected is None:
        # No candidate passed. Pick the "best partial" candidate (first
        # that probed without transport error) so the user knows where
        # things broke. ``env`` is preferred over ``install_root`` over
        # ``path`` by construction of the candidate list.
        best_partial = next(
            (p for p in probes if p.error != "candidate-not-executable"),
            None,
        )
        if best_partial is None:
            verdict = "no-candidate"
            advice = _format_advice(verdict, "", [], [])
            return ResolutionResult(
                verdict=verdict, selected_bin="", selected_source="",
                advice=advice,
                candidates_probed=[asdict(p) for p in probes],
            )
        hard, soft = _categorise_signals(best_partial)
        verdict = "hard_fail"
        advice = _format_advice(verdict, best_partial.bin_path, hard, soft)
        return ResolutionResult(
            verdict=verdict,
            selected_bin=best_partial.bin_path,
            selected_source=best_partial.source,
            hard_fail_codes=hard,
            soft_warn_codes=soft,
            advice=advice,
            candidates_probed=[asdict(p) for p in probes],
        )

    hard, soft = _categorise_signals(selected)
    verdict = "soft_warn" if soft else "ok"
    advice = _format_advice(verdict, selected.bin_path, hard, soft)
    return ResolutionResult(
        verdict=verdict,
        selected_bin=selected.bin_path,
        selected_source=selected_source,
        hard_fail_codes=hard,
        soft_warn_codes=soft,
        advice=advice,
        candidates_probed=[asdict(p) for p in probes],
    )


__all__ = ["resolve_beacon_bin", "ResolutionResult", "ProbeResult"]
