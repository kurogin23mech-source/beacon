# Installing Beacon

This document covers all supported installation methods.

---

## Option 1: Homebrew (macOS — recommended)

Beacon is distributed via a [Homebrew tap](https://docs.brew.sh/Taps).

```bash
brew tap r-kida2/beacon
brew install beacon
```

That's it. Homebrew handles Python dependencies and puts `beacon` on your `$PATH`.

> **Note** — Cloud features (Google auth, team collaboration) need two extra Python packages
> that are not installed automatically to keep the base install lightweight:
>
> ```bash
> pip install google-auth-oauthlib google-auth
> ```

### Upgrading

```bash
brew upgrade beacon
```

### Uninstalling

```bash
brew uninstall beacon
brew untap r-kida2/beacon
```

---

## Option 2: Manual (any OS)

### Prerequisites

| Dependency | Minimum version | Notes |
|------------|-----------------|-------|
| Python | 3.9 | 3.11+ recommended |
| tmux | any | Required for the live dashboard |
| Git | any | Required for commit tracking |

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/r-kida2/beacon.git

# 2. Add the bin/ directory to your PATH
export PATH="$PATH:$(pwd)/beacon/bin"
```

Add the `export` line to your shell profile to make it permanent:

```bash
# For zsh
echo 'export PATH="$PATH:/path/to/beacon/bin"' >> ~/.zshrc

# For bash
echo 'export PATH="$PATH:/path/to/beacon/bin"' >> ~/.bashrc
```

### Optional: cloud features

```bash
pip install google-auth-oauthlib google-auth
```

---

## First-time setup

After installing by either method, run the setup wizard from inside your project directory:

```bash
cd your-project
beacon setup
```

`beacon setup` walks you through:
1. Google sign-in (optional — skip if you only need local mode)
2. Claude Code Skills and hook installation
3. Project initialisation or joining an existing cloud project
4. Launching the dashboard

For a quick local-only start without the wizard:

```bash
cd your-project
beacon init
beacon milestone add "First milestone"
beacon milestone start ms-1
beacon          # launches the dashboard
```

---

## Creating the Homebrew tap repository

> This section is for Beacon maintainers who need to publish a new release.

The tap lives at **`github.com/r-kida2/homebrew-beacon`** (Homebrew convention: `homebrew-<tap-name>`).

### One-time setup

```bash
# 1. Create the GitHub repository named "homebrew-beacon" under r-kida2
#    (do this via the GitHub web UI or gh CLI)
gh repo create r-kida2/homebrew-beacon --public --description "Homebrew tap for Beacon"

# 2. Clone it locally
git clone https://github.com/r-kida2/homebrew-beacon.git
cd homebrew-beacon

# 3. Create the Formula directory
mkdir Formula

# 4. Copy the formula from the main repo
cp /path/to/beacon/packaging/homebrew/beacon.rb Formula/beacon.rb

# 5. Commit and push
git add Formula/beacon.rb
git commit -m "feat: add beacon formula"
git push origin main
```

### Releasing a new version

When a new version of Beacon is tagged (e.g. `v0.2.0`):

1. Download the source tarball SHA-256:

   ```bash
   curl -sL https://github.com/r-kida2/beacon/archive/refs/tags/v0.2.0.tar.gz | shasum -a 256
   ```

2. Update `Formula/beacon.rb` in the tap repo:

   ```ruby
   url "https://github.com/r-kida2/beacon/archive/refs/tags/v0.2.0.tar.gz"
   sha256 "<paste-sha256-here>"
   version "0.2.0"
   ```

3. Commit and push to `homebrew-beacon`. Users running `brew upgrade beacon` will
   automatically pick up the new version.

### Testing the formula locally

```bash
# Audit for common issues
brew audit --strict Formula/beacon.rb

# Install from the local file directly
brew install --build-from-source Formula/beacon.rb

# Run the built-in tests
brew test beacon
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `command not found: beacon` | Make sure `bin/` is on your `$PATH` (manual install) or that `brew install` completed without errors |
| `python3: command not found` | Install Python 3.9+ from python.org or via Homebrew: `brew install python@3.11` |
| `tmux: command not found` | Install tmux: `brew install tmux` (macOS) or `apt install tmux` (Debian/Ubuntu) |
| Cloud auth errors | Run `pip install google-auth-oauthlib google-auth` and retry `beacon auth login` |
| Skill install fails | Make sure Claude Code is installed and `~/.claude/` exists, then run `beacon skill install` |
