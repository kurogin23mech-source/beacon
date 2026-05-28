#!/usr/bin/env python3
# =============================================================================
#  THIS SCRIPT IS FOR BEACON MAINTAINERS ONLY.
#  Regular users should never need to run this — to upgrade beacon use:
#      beacon update
#  (or the manual equivalent: brew update && brew upgrade beacon && beacon skill install)
# =============================================================================
"""Beacon release pipeline (maintainer-only).

Audience: Beacon maintainers cutting a new release of the CLI itself.
NOT for end users. If you're a user of beacon and you ended up here looking
for "how do I update", you want `beacon update` instead. See e-577.

Usage:
    python3 scripts/release.py                  # auto-detect version from commits
    python3 scripts/release.py --version vX.Y.Z # explicit version
    python3 scripts/release.py --dry-run        # show every step without doing it
    python3 scripts/release.py --yes            # skip confirmation
    python3 scripts/release.py --no-readme      # skip README badge / CHANGELOG update

Auto-version uses lib/version_rules.py, which reads the 'version-rules' CORE
doc if present, otherwise falls back to Conventional Commits semver.

This file is NOT shipped via brew. It lives only in the source repo.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request


def run(cmd, cwd=None, *, check=True, capture=False, dry_run=False):
    if dry_run:
        print(f"  [dry-run] {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
        return ""
    result = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        err = result.stderr or result.stdout or ""
        raise SystemExit(f"Command failed: {' '.join(cmd)}\n{err}")
    return result.stdout.strip() if capture else ""


def _update_readme_version(beacon_root, version_str, *, dry_run=False):
    """Update the README version badge marker, if present.

    Looks for one of these patterns in README.md and rewrites the version:
        <!-- BEACON_VERSION -->v0.4.0<!-- /BEACON_VERSION -->
        ![version](https://img.shields.io/badge/version-vX.Y.Z-blue)

    Returns True iff README.md was changed. Returns False (silently) if no
    matching pattern is found — that way release.py does not force a README
    layout on the maintainer.
    """
    readme_path = os.path.join(beacon_root, "README.md")
    if not os.path.exists(readme_path):
        return False
    src = open(readme_path, encoding="utf-8").read()
    new = src

    # Pattern 1: explicit comment-marker pair
    marker_re = re.compile(
        r"(<!--\s*BEACON_VERSION\s*-->)[^<]*(<!--\s*/BEACON_VERSION\s*-->)"
    )
    if marker_re.search(new):
        new = marker_re.sub(r"\1v" + version_str + r"\2", new)

    # Pattern 2: shields.io version badge
    badge_re = re.compile(r"(version-)v?\d+\.\d+\.\d+(-\w+)")
    if badge_re.search(new):
        new = badge_re.sub(r"\g<1>v" + version_str + r"\g<2>", new)

    if new == src:
        return False
    if not dry_run:
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(new)
    return True


def _update_changelog(beacon_root, version_tag, info, *, dry_run=False):
    """Prepend a new entry to CHANGELOG.md. Creates the file if missing.

    The entry shape (Keep-a-Changelog flavoured) is:
        ## [vX.Y.Z] - YYYY-MM-DD
        - <subject line of commit 1>
        - <subject line of commit 2>

    Returns True iff CHANGELOG.md was created or modified.
    """
    changelog_path = os.path.join(beacon_root, "CHANGELOG.md")
    today = time.strftime("%Y-%m-%d")
    lines = [f"## [{version_tag}] - {today}", ""]
    if info is not None and info.get("commits"):
        for c in info["commits"]:
            subj = c["message"].splitlines()[0] if c["message"] else c["hash"]
            lines.append(f"- {subj}")
        lines.append("")

    new_entry = "\n".join(lines)

    if not os.path.exists(changelog_path):
        content = (
            "# Changelog\n\n"
            "All notable changes to Beacon are documented here. See "
            "[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) for format.\n\n"
        ) + new_entry + "\n"
        if not dry_run:
            with open(changelog_path, "w", encoding="utf-8") as f:
                f.write(content)
        return True

    existing = open(changelog_path, encoding="utf-8").read()
    # If the version line already exists, do nothing (idempotent re-run).
    if f"## [{version_tag}]" in existing:
        return False

    # Insert after the leading top-level heading + intro paragraph.
    # Heuristic: find the first `## ` heading and insert before it. If there
    # is no `## ` heading yet, append at end.
    next_section = re.search(r"\n## ", existing)
    if next_section:
        i = next_section.start() + 1  # keep the preceding newline
        new_content = existing[:i] + new_entry + "\n" + existing[i:]
    else:
        new_content = existing.rstrip() + "\n\n" + new_entry + "\n"

    if not dry_run:
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    return True


def determine_version(beacon_root):
    """Auto-detect next version from commits since last tag.

    Returns (version_str_with_v, info_dict).
    """
    sys.path.insert(0, os.path.join(beacon_root, "lib"))
    from version_rules import propose_next_version, get_current_tag

    current_tag = get_current_tag(prefix="v", repo_path=beacon_root)
    if current_tag:
        rev_range = f"{current_tag}..HEAD"
    else:
        rev_range = "HEAD"
    log_output = subprocess.run(
        ["git", "log", rev_range, "--pretty=format:%H%x00%s%x00%b%x1e"],
        capture_output=True, text=True, cwd=beacon_root,
    ).stdout

    commits = []
    for entry in log_output.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00")
        if len(parts) >= 2:
            subject, body = parts[1], (parts[2] if len(parts) > 2 else "")
            full_message = subject + ("\n\n" + body if body else "")
            commits.append({"hash": parts[0][:7], "message": full_message})

    if not commits:
        sys.exit("Error: no new commits since last release.")

    info = propose_next_version(commits, axis="push", repo_path=beacon_root)
    info["commits"] = commits
    return info["next"], info


def main():
    parser = argparse.ArgumentParser(description="Beacon release pipeline")
    parser.add_argument("--version", default="", help="Override auto-detected version (e.g. v0.2.0)")
    parser.add_argument("--tap-path", default="", help="Path to homebrew-beacon tap. Auto-detected via brew if omitted.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument(
        "--no-readme", action="store_true",
        help="Skip README badge and CHANGELOG.md auto-update (e-582). "
             "Use when the release isn't user-visible (e.g. tooling-only)."
    )
    args = parser.parse_args()

    dry = args.dry_run
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ---- Pre-flight ----
    print("=> Pre-flight checks")
    status = run(["git", "status", "--porcelain", "--untracked-files=no"],
                 cwd=beacon_root, capture=True, dry_run=False)
    if status:
        print("Error: tracked files have uncommitted changes. Commit or stash first.", file=sys.stderr)
        print(status, file=sys.stderr)
        sys.exit(1)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=beacon_root, capture=True, dry_run=False)
    if branch != "main":
        sys.exit(f"Error: must be on 'main' branch (current: {branch})")

    # ---- Version determination ----
    if args.version:
        v = args.version if args.version.startswith("v") else f"v{args.version}"
        print(f"=> Using explicit version: {v}")
        info = None
    else:
        print("=> Determining next version from commits...")
        v, info = determine_version(beacon_root)
        nonzero = {k: n for k, n in info["counts"].items() if n}
        print(f"   Current tag:  {info['current']}")
        print(f"   Next version: {v}  (bump: {info['level']})")
        print(f"   Commits:      {len(info['commits'])} (breakdown: {nonzero})")
        if info["counts"]["breaking"] > 0:
            print(f"   ⚠️  BREAKING change detected → MAJOR bump")

    # ---- Explicit pre-release preview (e-579) ----
    print()
    print("─" * 72)
    print("Release preview")
    print("─" * 72)
    print(f"  Version:     {v}")
    if info is not None:
        print(f"  From:        {info['current']}")
        print(f"  Bump level:  {info['level']}")
    print(f"  Repo:        {beacon_root}")
    print(f"  Branch:      {branch}")
    print(f"  HEAD:        " + (run(['git', 'rev-parse', '--short', 'HEAD'],
                                    cwd=beacon_root, capture=True, dry_run=False) or '?'))
    if info is not None and info.get("commits"):
        print(f"  Commits to release ({len(info['commits'])}):")
        for c in info["commits"][:10]:
            first_line = c["message"].splitlines()[0] if c["message"] else ""
            print(f"    {c['hash']}  {first_line}")
        if len(info["commits"]) > 10:
            print(f"    ... and {len(info['commits']) - 10} more")
    print("  Will:")
    print(f"    1. bump __version__ in lib/commands.py to {v.lstrip('v')}")
    print(f"    2. git push origin {branch}")
    print(f"    3. git tag {v} && git push origin {v}")
    print(f"    4. gh release create {v}")
    print(f"    5. update homebrew formula (sha256 of tarball)")
    print(f"    6. mirror formula to homebrew-beacon tap repo")
    if not args.no_readme:
        print(f"    7. update README version badge and CHANGELOG.md  (--no-readme to skip)")
    print("─" * 72)

    if not args.yes and not dry:
        confirm = input(f"Proceed with releasing {v}? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            sys.exit(0)

    version_str = v.lstrip("v")

    # ---- Bump __version__ in lib/commands.py ----
    print("=> Updating __version__ in lib/commands.py")
    commands_path = os.path.join(beacon_root, "lib", "commands.py")
    cmds_src = open(commands_path, encoding="utf-8").read()
    new_cmds = re.sub(r'(__version__\s*=\s*)"[^"]+"', f'\\1"{version_str}"', cmds_src, count=1)
    if new_cmds != cmds_src and not dry:
        with open(commands_path, "w", encoding="utf-8") as f:
            f.write(new_cmds)
        run(["git", "add", "lib/commands.py"], cwd=beacon_root, dry_run=dry)
        run(["git", "commit", "-m", f"chore(release): bump __version__ to {version_str}"],
            cwd=beacon_root, dry_run=dry)

    # ---- Push beacon repo (if any unpushed) ----
    print("=> Pushing beacon repo to origin/main")
    run(["git", "push", "origin", "main"], cwd=beacon_root, dry_run=dry)

    # ---- Create + push tag ----
    print(f"=> Tagging {v}")
    run(["git", "tag", v], cwd=beacon_root, dry_run=dry)
    run(["git", "push", "origin", v], cwd=beacon_root, dry_run=dry)

    # ---- GitHub release (best-effort) ----
    print(f"=> Creating GitHub release {v}")
    try:
        run(["gh", "release", "create", v, "--title", v, "--notes", f"Release {v}"],
            cwd=beacon_root, dry_run=dry)
    except SystemExit as e:
        print(f"  (gh release create skipped: {e})", file=sys.stderr)

    # ---- Parse formula for repo URL ----
    formula_path = os.path.join(beacon_root, "packaging", "homebrew", "beacon.rb")
    formula_src = open(formula_path, encoding="utf-8").read()
    homepage_match = re.search(r'homepage\s+"([^"]+)"', formula_src)
    if not homepage_match:
        sys.exit("Error: cannot parse homepage from formula")
    repo_url = homepage_match.group(1).rstrip("/")
    tarball_url = f"{repo_url}/archive/refs/tags/{v}.tar.gz"

    # ---- Fetch tarball + SHA-256 ----
    print(f"=> Fetching {tarball_url}")
    if dry:
        sha256 = "0" * 64
        print(f"   [dry-run] sha256 = {sha256}")
    else:
        sha256 = ""
        for attempt in range(5):
            try:
                with urllib.request.urlopen(tarball_url, timeout=30) as resp:
                    data = resp.read()
                sha256 = hashlib.sha256(data).hexdigest()
                break
            except Exception as e:
                if attempt == 4:
                    raise
                print(f"  retry {attempt+1}/5 ({e})")
                time.sleep(3)
        print(f"   sha256 = {sha256}")

    # ---- Patch repo formula ----
    print("=> Updating repo formula")
    new_src = formula_src
    new_src = re.sub(r'(\n\s*url\s+)"[^"]+"', f'\\1"{tarball_url}"', new_src, count=1)
    new_src = re.sub(r'(\n\s*version\s+)"[^"]+"', f'\\1"{version_str}"', new_src, count=1)
    if re.search(r'\n\s*sha256\s+"', new_src):
        new_src = re.sub(r'(\n\s*sha256\s+)"[^"]+"', f'\\1"{sha256}"', new_src, count=1)
    else:
        new_src = re.sub(r'(\n(\s*)url\s+"[^"]+")',
                         f'\\1\n\\2sha256 "{sha256}"', new_src, count=1)
    if not dry:
        with open(formula_path, "w", encoding="utf-8") as f:
            f.write(new_src)

    run(["git", "add", "packaging/homebrew/beacon.rb"], cwd=beacon_root, dry_run=dry)
    run(["git", "commit", "-m", f"chore(release): bump formula to {version_str}"],
        cwd=beacon_root, dry_run=dry)

    # ---- README badge + CHANGELOG update (e-582) ----
    if not args.no_readme:
        readme_changed = _update_readme_version(beacon_root, version_str, dry_run=dry)
        changelog_changed = _update_changelog(beacon_root, v, info, dry_run=dry)
        if readme_changed or changelog_changed:
            staged = []
            if readme_changed:
                staged.append("README.md")
                print("=> README version badge updated")
            if changelog_changed:
                staged.append("CHANGELOG.md")
                print(f"=> CHANGELOG.md updated with {v} entry")
            for f in staged:
                run(["git", "add", f], cwd=beacon_root, dry_run=dry)
            run(["git", "commit", "-m",
                 f"docs(release): update README/CHANGELOG for {v}"],
                cwd=beacon_root, dry_run=dry)

    run(["git", "push", "origin", "main"], cwd=beacon_root, dry_run=dry)

    # ---- Mirror to tap ----
    print("=> Updating tap repo")
    tap_path = args.tap_path
    if not tap_path:
        try:
            brew_prefix = subprocess.run(["brew", "--prefix"], capture_output=True, text=True).stdout.strip()
        except FileNotFoundError:
            brew_prefix = "/opt/homebrew"
        candidate = os.path.join(brew_prefix, "Library", "Taps", "kurogin23mech-source", "homebrew-beacon")
        if os.path.isdir(candidate):
            tap_path = candidate
        else:
            sys.exit("Error: tap repo not found. Pass --tap-path or clone homebrew-beacon first.")

    tap_formula = os.path.join(tap_path, "Formula", "beacon.rb")
    if not os.path.exists(tap_formula):
        sys.exit(f"Error: {tap_formula} not found")

    tap_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=tap_path, capture=True, dry_run=False)
    print(f"   tap branch: {tap_branch}")
    run(["git", "pull", "--ff-only"], cwd=tap_path, check=False, dry_run=dry)

    if not dry:
        shutil.copy2(formula_path, tap_formula)

    tap_diff = run(["git", "diff", "--stat"], cwd=tap_path, capture=True, dry_run=False)
    if tap_diff or dry:
        run(["git", "add", "Formula/beacon.rb"], cwd=tap_path, dry_run=dry)
        run(["git", "commit", "-m", f"chore: bump beacon formula to {version_str}"],
            cwd=tap_path, dry_run=dry)
        run(["git", "push", "origin", tap_branch], cwd=tap_path, dry_run=dry)
    else:
        print("   (tap formula already up-to-date)")

    # ---- Release notification trigger (e-580) ----
    # We do not post directly to Discord/Slack from this script — those
    # integrations live in user-installed Skills (`discord-post`) so the
    # message is composed by Claude with full release context rather than
    # by a templated curl call. Here we fire a beacon trigger; the user's
    # next /beacon-session-start (or any beacon trigger check) surfaces it
    # and prompts the user to invoke `discord-post`.
    print()
    print("=> Firing release-notify trigger")
    try:
        beacon_bin = shutil.which("beacon") or "beacon"
        commit_count = len(info["commits"]) if info and info.get("commits") else 0
        msg = (
            f"beacon {v} released "
            f"({commit_count} commits). "
            f"Use /discord-post to share."
        )
        if not dry:
            subprocess.run(
                [beacon_bin, "trigger", "fire", f"release-{version_str}", msg],
                cwd=beacon_root,
            )
        else:
            print(f"   [dry-run] beacon trigger fire release-{version_str} '{msg}'")
    except Exception as e:
        print(f"   (trigger fire skipped: {e})")

    print()
    print(f"Release complete: {v}")
    print(f"   Tarball: {tarball_url}")
    print(f"   SHA256:  {sha256}")
    print()
    print("Users can update with:")
    print("   beacon update    # or: brew update && brew upgrade beacon && beacon skill install")


if __name__ == "__main__":
    main()
