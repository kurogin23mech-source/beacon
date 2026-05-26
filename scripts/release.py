#!/usr/bin/env python3
"""Beacon release pipeline (maintainer-only).

Run from the beacon repo root:
    python3 scripts/release.py [--version v0.2.0] [--tap-path PATH] [--dry-run]

Steps:
  1. Pre-flight: git working tree clean, on main branch
  2. git push origin main
  3. If --version: git tag + push tag + gh release create
  4. Fetch tarball + compute SHA-256
  5. Update packaging/homebrew/beacon.rb (sha256 / version / url)
  6. Commit + push the formula bump
  7. Mirror Formula/beacon.rb to the homebrew-beacon tap repo, commit + push

NOT shipped via brew. This is a dev tool that lives in the repo.
"""

import argparse
import hashlib
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


def main():
    parser = argparse.ArgumentParser(description="Beacon release pipeline")
    parser.add_argument("--version", default="", help="Semantic version (e.g. v0.2.0). Omit to keep current version & main tarball.")
    parser.add_argument("--tap-path", default="", help="Path to homebrew-beacon tap repo. Auto-detected via brew if omitted.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    dry = args.dry_run
    beacon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ---- Step 1: Pre-flight ----
    print("=> Pre-flight checks")
    status = run(["git", "status", "--porcelain"], cwd=beacon_root, capture=True, dry_run=False)
    if status:
        print("Error: git working tree is not clean. Commit or stash changes first.", file=sys.stderr)
        print(status, file=sys.stderr)
        sys.exit(1)
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=beacon_root, capture=True, dry_run=False)
    if branch != "main":
        print(f"Error: must be on 'main' branch (current: {branch})", file=sys.stderr)
        sys.exit(1)

    # ---- Step 2: Parse formula for repo URL ----
    formula_path = os.path.join(beacon_root, "packaging", "homebrew", "beacon.rb")
    if not os.path.exists(formula_path):
        sys.exit(f"Error: formula not found at {formula_path}")
    formula_src = open(formula_path, encoding="utf-8").read()
    homepage_match = re.search(r'homepage\s+"([^"]+)"', formula_src)
    if not homepage_match:
        sys.exit("Error: cannot parse homepage from formula")
    repo_url = homepage_match.group(1).rstrip("/")

    if args.version:
        v = args.version if args.version.startswith("v") else f"v{args.version}"
        tarball_url = f"{repo_url}/archive/refs/tags/{v}.tar.gz"
        version_str = v.lstrip("v")
    else:
        tarball_url = f"{repo_url}/archive/refs/heads/main.tar.gz"
        version_match = re.search(r'version\s+"([^"]+)"', formula_src)
        version_str = version_match.group(1) if version_match else "0.1.0"
        v = None

    # ---- Step 3: Push beacon repo ----
    print("=> Pushing beacon repo to origin/main")
    run(["git", "push", "origin", "main"], cwd=beacon_root, dry_run=dry)

    if v:
        print(f"=> Tagging {v}")
        run(["git", "tag", v], cwd=beacon_root, dry_run=dry)
        run(["git", "push", "origin", v], cwd=beacon_root, dry_run=dry)
        try:
            run(["gh", "release", "create", v, "--title", v, "--notes", f"Release {v}"],
                cwd=beacon_root, dry_run=dry)
        except SystemExit as e:
            print(f"  (gh release create skipped: {e})", file=sys.stderr)

    # ---- Step 4: Fetch tarball + SHA-256 ----
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

    # ---- Step 5: Patch formula ----
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

    # ---- Step 6: Mirror to tap ----
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
    print(f"Release complete: {version_str}")
    print(f"   Tarball: {tarball_url}")
    print(f"   SHA256:  {sha256}")
    print()
    print("Users can update with:")
    print("   brew upgrade beacon && beacon skill install")


if __name__ == "__main__":
    main()
