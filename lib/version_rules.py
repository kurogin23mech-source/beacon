"""Multi-axis semantic version determination from commit history.

Rules can be customized per project via a CORE document with slug 'version-rules'.
Supports two axes: 'push' (CLI/brew distribution) and 'deploy' (server releases).
Each axis is independently configurable.

Public API:
    load_rules(axis='push', repo_path='.') -> dict
    classify_commits(commits, rules) -> ('major'|'minor'|'patch', breakdown)
    bump_version(current_tag, level, prefix='v') -> str
    propose_next_version(commits, axis='push', repo_path='.') -> dict
    get_current_tag(prefix='v', repo_path='.') -> str
    filter_commits_by_scope(commits, scope_paths, repo_path='.') -> list
"""

import re
import subprocess


# Default Conventional Commits patterns (used unless overridden)
_DEFAULT_MAJOR_PATTERNS = [
    r"BREAKING CHANGE",
    r"BREAKING:",
    r"^[a-z]+!:",
    r"^[a-z]+\([^)]+\)!:",
]
_DEFAULT_MINOR_PATTERNS = [
    r"^feat:",
    r"^feat\(",
]

DEFAULT_AXES = {
    "push": {
        "enabled": True,
        "prefix": "v",
        "scheme": "semver",
        "initial": "v0.1.0",
        "scope_paths": None,
        "major_patterns": list(_DEFAULT_MAJOR_PATTERNS),
        "minor_patterns": list(_DEFAULT_MINOR_PATTERNS),
    },
    "deploy": {
        "enabled": False,  # opt-in only
        "prefix": "deploy-v",
        "scheme": "semver",
        "initial": "deploy-v0.1.0",
        "scope_paths": None,
        "major_patterns": list(_DEFAULT_MAJOR_PATTERNS),
        "minor_patterns": list(_DEFAULT_MINOR_PATTERNS),
    },
}


def _parse_core_doc(body: str) -> dict:
    """Parse the version-rules CORE doc into per-axis overrides.

    Recognized headers (case-insensitive, allow trailing parens / spaces):
      ## push軸 / ## push / ## Push
      ## deploy軸 / ## deploy / ## Deploy

    Recognized keys within a section (case-insensitive):
      有効         / enabled            -> bool ('yes'/'no'/'true'/'false')
      タグprefix   / prefix             -> str
      初期バージョン / initial            -> str
      スコープ制限 / scope_paths         -> comma- or whitespace-separated list
    """
    sections = {}
    current = None
    header_re = re.compile(r"^\s*##\s+(?:(push|deploy)軸|(push|deploy))\b", re.IGNORECASE)
    next_section_re = re.compile(r"^\s*##\s+\S")
    key_re = re.compile(r"^\s*[-•]?\s*([^:：]+)[:：]\s*(.+?)\s*$")

    for line in body.splitlines():
        m = header_re.match(line)
        if m:
            current = (m.group(1) or m.group(2)).lower()
            sections[current] = {}
            continue
        if current is None:
            continue
        if next_section_re.match(line):
            current = None
            continue
        km = key_re.match(line)
        if not km:
            continue
        key = km.group(1).strip().lower()
        val = km.group(2).strip()
        sections[current][key] = val
    return sections


def _apply_overrides(axis_default: dict, overrides: dict) -> dict:
    rules = dict(axis_default)
    if not overrides:
        return rules

    def get(*keys):
        for k in keys:
            if k in overrides:
                return overrides[k]
        return None

    enabled = get("有効", "enabled", "active")
    if enabled is not None:
        rules["enabled"] = enabled.lower() in ("yes", "true", "on", "1", "有効")
    prefix = get("タグprefix", "タグprefix", "prefix", "tag prefix", "tag_prefix")
    if prefix:
        rules["prefix"] = prefix.strip()
        if not rules["initial"].startswith(prefix):
            # Reset initial to use the new prefix at 0.1.0
            rules["initial"] = f"{prefix.rstrip('-')}0.1.0" if prefix.endswith(("v", "v-")) else f"{prefix}0.1.0"
    initial = get("初期バージョン", "initial", "initial_version")
    if initial:
        rules["initial"] = initial.strip()
    scopes = get("スコープ制限", "scope_paths", "scope")
    if scopes:
        parts = [p.strip().rstrip("/") for p in re.split(r"[,\s]+", scopes) if p.strip()]
        rules["scope_paths"] = parts or None
    return rules


def load_rules(axis: str = "push", repo_path: str = "."):
    """Read 'version-rules' CORE doc for the given axis. Falls back to defaults."""
    axis = axis.lower()
    if axis not in DEFAULT_AXES:
        raise ValueError(f"Unknown axis: {axis}")

    try:
        result = subprocess.run(
            ["beacon", "doc", "show", "version-rules"],
            capture_output=True, text=True, cwd=repo_path,
        )
        body = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        body = ""

    parsed = _parse_core_doc(body) if body.strip() else {}
    return _apply_overrides(DEFAULT_AXES[axis], parsed.get(axis, {}))


def classify_commits(commits, rules=None):
    """Return ('major'|'minor'|'patch', counts_dict)."""
    rules = rules or DEFAULT_AXES["push"]
    counts = {"breaking": 0, "feat": 0, "fix": 0, "chore": 0, "docs": 0,
             "refactor": 0, "test": 0, "perf": 0, "style": 0, "other": 0}
    has_major = has_minor = False

    for c in commits:
        msg = c["message"] if isinstance(c, dict) else str(c)
        first_line = msg.splitlines()[0] if msg else ""
        if any(re.search(p, msg, re.MULTILINE) for p in rules["major_patterns"]):
            has_major = True
            counts["breaking"] += 1
            continue
        if any(re.search(p, first_line) for p in rules["minor_patterns"]):
            has_minor = True
            counts["feat"] += 1
            continue
        prefix_match = re.match(r"^([a-z]+)(?:\([^)]+\))?:", first_line)
        if prefix_match:
            p = prefix_match.group(1)
            counts[p if p in counts else "other"] += 1
        else:
            counts["other"] += 1

    if has_major:
        return "major", counts
    if has_minor:
        return "minor", counts
    return "patch", counts


def bump_version(current_tag, level, prefix="v"):
    """Return next semver tag string given current tag, level, and prefix."""
    pattern = re.escape(prefix) + r"(\d+)\.(\d+)\.(\d+)"
    m = re.search(pattern, current_tag or "")
    if not m:
        return f"{prefix}0.1.0"
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if level == "major":
        return f"{prefix}{major + 1}.0.0"
    if level == "minor":
        return f"{prefix}{major}.{minor + 1}.0"
    return f"{prefix}{major}.{minor}.{patch + 1}"


def get_current_tag(prefix: str = "v", repo_path: str = "."):
    """Return latest matching tag, or empty string."""
    match = f"{prefix}[0-9]*"
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


def describe_from_tag(prefix: str = "v", repo_path: str = "."):
    """Return tag+delta like 'v0.2.1+3' or 'v0.2.1' (exact) or '' (no tag)."""
    match = f"{prefix}[0-9]*"
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--match", match],
            capture_output=True, text=True, cwd=repo_path,
        )
        if result.returncode != 0:
            return ""
        # git describe yields e.g. 'v0.2.1-3-gabc123' for non-exact
        d = result.stdout.strip()
        m = re.match(r"^(.+?)-(\d+)-g([0-9a-f]+)$", d)
        if m:
            return f"{m.group(1)}+{m.group(2)}"
        return d
    except FileNotFoundError:
        return ""


def filter_commits_by_scope(commits, scope_paths, repo_path: str = "."):
    """Keep only commits that touch at least one path under scope_paths.

    `commits` items must have 'hash' field. If 'hash' is missing, the commit is
    kept (best-effort). If scope_paths is None/empty, returns commits unchanged.
    """
    if not scope_paths:
        return commits
    filtered = []
    for c in commits:
        h = c.get("hash") if isinstance(c, dict) else None
        if not h:
            filtered.append(c)
            continue
        try:
            files = subprocess.run(
                ["git", "show", "--name-only", "--format=", h],
                capture_output=True, text=True, cwd=repo_path,
            ).stdout.splitlines()
        except FileNotFoundError:
            filtered.append(c)
            continue
        for f in files:
            f = f.strip()
            if any(f.startswith(s.rstrip("/") + "/") or f == s for s in scope_paths):
                filtered.append(c)
                break
    return filtered


def propose_next_version(commits, axis: str = "push", repo_path: str = "."):
    """End-to-end: read rules, optionally filter by scope, classify, bump.

    Returns:
      {
        'axis': str,
        'enabled': bool,
        'current': str,
        'next': str,
        'level': str,
        'counts': dict,
        'rules': dict,
        'filtered_count': int,  # commits after scope filter
        'total_count': int,     # commits before filter
      }
    """
    rules = load_rules(axis, repo_path)
    total = len(commits)
    if rules.get("scope_paths"):
        commits = filter_commits_by_scope(commits, rules["scope_paths"], repo_path)
    filtered = len(commits)
    level, counts = classify_commits(commits, rules)
    current = get_current_tag(rules["prefix"], repo_path) or rules["initial"]
    next_v = bump_version(current, level, rules["prefix"])
    return {
        "axis": axis,
        "enabled": rules["enabled"],
        "current": current,
        "next": next_v,
        "level": level,
        "counts": counts,
        "rules": rules,
        "filtered_count": filtered,
        "total_count": total,
    }
