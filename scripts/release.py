#!/usr/bin/env python3
"""Beacon release pipeline (maintainer-only).

Usage:
    python3 scripts/release.py                  # auto-detect version from commits
    python3 scripts/release.py --version vX.Y.Z # explicit version
    python3 scripts/release.py --dry-run
    python3 scripts/release.py --yes            # skip confirmation

Auto-version uses lib/version_rules.py, which reads the 'version-rules' CORE
doc if present, otherwise falls back to Conventional Commits semver.

NOT shipped via brew. Lives in repo only.
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


def determine_version(beacon_root):
    """Auto-detect next version from commits since last tag.

    Returns (version_str_with_v, info_dict).
    """
    sys.path.insert(0, os.path.join(beacon_root, "lib"))
    from version_rules import propose_next_version, get_current_tag

    current_tag = get_current_tag(beacon_root)
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

    info = propose_next_version(commits, repo_path=beacon_root)
    info["commits"] = commits
    return info["next"], info


def main():
    parser = argparse.ArgumentParser(description="Beacon release pipeline")
    parser.add_argument("--version", default="", help="Override auto-detected version (e.g. v0.2.0)")
    parser.add_argument("--tap-path", default="", help="Path to homebrew-beacon tap. Auto-detected via brew if omitted.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
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

    if not args.yes and not dry:
        print()
        confirm = input(f"Release {v}? [y/N]: ").strip().lower()
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

    print()
    print(f"Release complete: {v}")
    print(f"   Tarball: {tarball_url}")
    print(f"   SHA256:  {sha256}")
    print()
    print("Users can update with:")
    print("   brew update && brew upgrade beacon && beacon skill install")


if __name__ == "__main__":
    main()
