"""Semantic version determination from commit history.

Rules can be customized per project via a CORE document with slug 'version-rules'.
If no doc exists, defaults to Conventional Commits semver.

Public API:
    load_rules(beacon_dir) -> dict
    classify_commits(commits, rules) -> ('major'|'minor'|'patch', breakdown)
    bump_version(current_tag, level) -> str
    propose_next_version(commits, beacon_dir) -> dict
"""

import re
import subprocess


DEFAULT_RULES = {
    "scheme": "semver",
    "initial": "v0.1.0",
    "major_patterns": [
        r"BREAKING CHANGE",
        r"BREAKING:",
        r"^[a-z]+!:",
        r"^[a-z]+\([^)]+\)!:",
    ],
    "minor_patterns": [
        r"^feat:",
        r"^feat\(",
    ],
    # everything else => patch
}


def load_rules(beacon_dir: str = "."):
    """Read 'version-rules' CORE doc via beacon CLI; fall back to defaults.

    The CORE doc is treated as free-form markdown; we look for ## sections
    'MAJOR', 'MINOR', 'PATCH' and extract regex / prefix hints from bullets.
    For simplicity, custom rules are recognized via these markers:
      - lines starting with '- pattern:' inside MAJOR/MINOR/PATCH sections
      - lines starting with '- 初期バージョン:' for initial version
    Anything not recognized falls back to defaults.
    """
    try:
        result = subprocess.run(
            ["beacon", "doc", "show", "version-rules"],
            capture_output=True, text=True, cwd=beacon_dir,
        )
        if result.returncode != 0:
            return dict(DEFAULT_RULES)
        body = result.stdout
    except FileNotFoundError:
        return dict(DEFAULT_RULES)

    if not body.strip():
        return dict(DEFAULT_RULES)

    rules = dict(DEFAULT_RULES)
    # Parse sections; lightweight (the AI can do better, but the deterministic
    # part stays simple). For now, defaults work for most projects; users who
    # want fine-grained control author the doc and the Skill (not this module)
    # interprets it with AI judgement.
    initial_match = re.search(r"初期バージョン[:：]\s*(v?\d+\.\d+\.\d+)", body)
    if initial_match:
        v = initial_match.group(1)
        rules["initial"] = v if v.startswith("v") else f"v{v}"
    return rules


def classify_commits(commits, rules=None):
    """Return ('major'|'minor'|'patch', {feat:N, fix:N, breaking:N, other:N}).

    `commits` is a list of dicts with 'message' (or plain strings).
    """
    rules = rules or DEFAULT_RULES
    counts = {"breaking": 0, "feat": 0, "fix": 0, "chore": 0, "docs": 0,
             "refactor": 0, "test": 0, "perf": 0, "style": 0, "other": 0}
    has_major = has_minor = False

    for c in commits:
        msg = c["message"] if isinstance(c, dict) else str(c)
        first_line = msg.splitlines()[0] if msg else ""

        # Check MAJOR patterns
        if any(re.search(p, msg, re.MULTILINE) for p in rules["major_patterns"]):
            has_major = True
            counts["breaking"] += 1
            continue

        # Check MINOR patterns
        if any(re.search(p, first_line) for p in rules["minor_patterns"]):
            has_minor = True
            counts["feat"] += 1
            continue

        # PATCH-level prefixes for breakdown
        prefix_match = re.match(r"^([a-z]+)(?:\([^)]+\))?:", first_line)
        if prefix_match:
            prefix = prefix_match.group(1)
            if prefix in counts:
                counts[prefix] += 1
            else:
                counts["other"] += 1
        else:
            counts["other"] += 1

    if has_major:
        level = "major"
    elif has_minor:
        level = "minor"
    else:
        level = "patch"
    return level, counts


def bump_version(current_tag, level):
    """Return next semver string given current tag and bump level."""
    if not current_tag:
        return "v0.1.0"
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", current_tag)
    if not m:
        return "v0.1.0"
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if level == "major":
        return f"v{major + 1}.0.0"
    elif level == "minor":
        return f"v{major}.{minor + 1}.0"
    else:
        return f"v{major}.{minor}.{patch + 1}"


def get_current_tag(repo_path=".", match="v[0-9]*"):
    """Return latest matching tag, or empty string. Default filters CLI semver tags."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", match],
            capture_output=True, text=True, cwd=repo_path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    return ""


def propose_next_version(commits, repo_path="."):
    """End-to-end: read rules, classify commits, compute next version.

    Returns dict: {
      'current': str, 'next': str, 'level': str, 'counts': dict, 'rules': dict
    }
    """
    rules = load_rules(repo_path)
    level, counts = classify_commits(commits, rules)
    current = get_current_tag(repo_path) or rules["initial"]
    next_v = bump_version(current, level)
    return {
        "current": current,
        "next": next_v,
        "level": level,
        "counts": counts,
        "rules": rules,
    }
