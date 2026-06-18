# Installing Beacon

Beacon ships in two forms that work independently:

- **Beacon Desktop** — a Tauri-based UI app (`Beacon.app` / `Beacon.msi` / `Beacon.AppImage`) that you double-click. The CLI is bundled inside for users who never touch a terminal.
- **Beacon CLI** — the command-line tool (`beacon`) for terminal users, AI agents, and CI environments.

You can install either one or both depending on how you want to use Beacon.

> **Recommendation by user type**
>
> - Non-developer / vibe-coder → **Beacon Desktop** (one click, no terminal)
> - Developer who lives in the terminal → **Beacon CLI** (pipx or Homebrew)
> - Both → install Desktop first, then add the CLI

---

## Quick install — Pick your OS

| OS | Beacon Desktop | Beacon CLI |
|----|----------------|------------|
| **macOS** | `brew install --cask beacon-desktop`<br>or download `.dmg` from [Releases](https://github.com/r-kida2/beacon/releases) | `brew install r-kida2/beacon/beacon`<br>or `pipx install beacon-ai` |
| **Windows** | `winget install BeaconAI.BeaconDesktop`<br>or download `.msi` from [Releases](https://github.com/r-kida2/beacon/releases) | `pipx install beacon-ai` |
| **Linux** | Download `.AppImage` from [Releases](https://github.com/r-kida2/beacon/releases) and run `chmod +x Beacon-*.AppImage` | `pipx install beacon-ai`<br>or use Homebrew on Linux |

> **Status (2026-06):** Beacon Desktop distribution (brew cask / WinGet / AppImage releases) is being
> set up in **ms-44**. Until the first artifact is published you can still build Desktop from source
> (see [building from source](#building-from-source)) or use the CLI route which is already stable.

---

## macOS

### Option A — Beacon Desktop (recommended)

```bash
# Homebrew cask (preferred — auto-upgrade aware)
brew install --cask beacon-desktop
```

Or download `Beacon-<version>-arm64.dmg` (Apple Silicon) / `Beacon-<version>-x64.dmg` (Intel) from the
[Releases page](https://github.com/r-kida2/beacon/releases), open the disk image, and drag
`Beacon.app` into `/Applications/`.

When you first launch Beacon, macOS may warn "Beacon can't be opened because it is from an
unidentified developer" (we don't yet have an Apple Developer ID — see
[Code signing status](#code-signing-status)). Workaround:

1. **Control-click** `Beacon.app` in Finder → **Open** → **Open** in the confirmation dialog
2. After this one-time approval the app launches normally on every subsequent run

### Option B — Beacon CLI

```bash
# Homebrew (recommended for macOS developers)
brew tap r-kida2/beacon
brew install beacon

# Or pipx (cross-platform, doesn't require Homebrew)
pipx install beacon-ai
```

`brew install` and `pipx install` both put `beacon` on your `$PATH`. Pick whichever fits your
existing workflow.

### Upgrading on macOS

```bash
brew upgrade --cask beacon-desktop   # Desktop
brew upgrade beacon                  # CLI (Homebrew)
pipx upgrade beacon-ai               # CLI (pipx)
```

---

## Windows

### Option A — Beacon Desktop (recommended)

```powershell
# WinGet (Microsoft's official package manager — included in Windows 10/11)
winget install BeaconAI.BeaconDesktop
```

Or download `Beacon-<version>-x64.msi` from the
[Releases page](https://github.com/r-kida2/beacon/releases) and double-click to install.

> #### Important: Windows SmartScreen / antivirus warning
>
> Beacon Desktop is currently **distributed unsigned** (we do not yet hold an Authenticode
> certificate — see [Code signing status](#code-signing-status)). On first launch you will see
> one of the following:
>
> **Case 1 — Windows SmartScreen blue dialog "Windows protected your PC"**
>
> 1. Click **More info**
> 2. Click **Run anyway**
> 3. (No further prompts on subsequent launches)
>
> **Case 2 — Antivirus (Defender, Avast, Norton, etc.) flags `Beacon.exe` as suspicious**
>
> This is a false positive triggered by signature heuristics on unsigned Rust/Tauri binaries.
> Options:
>
> - Add `Beacon.exe` (or the install directory) to your AV's exception list. The Defender
>   recipe: **Windows Security** → **Virus & threat protection** → **Manage settings** →
>   **Add or remove exclusions** → **Add an exclusion** → **Folder** → select the Beacon
>   install directory.
> - If the AV refuses, install via `winget` instead — WinGet runs the installer in a context
>   where Microsoft's reputation is partially inherited (still unsigned, but less aggressive).
>
> **Case 3 — Corporate / managed device blocks installation outright**
>
> Forward this to your IT administrator and request an exception for:
>
> - **Publisher:** Beacon AI (unsigned)
> - **Product:** Beacon Desktop
> - **Source:** `https://github.com/r-kida2/beacon/releases`
> - **SHA-256 hash:** see the `.msi.sha256` file next to the installer on the Releases page
>
> Code signing certificates (OV or EV) are on the roadmap (ms-44 task **e-730**); SmartScreen
> reputation typically takes several weeks to months to build after issuance.

### Option B — Beacon CLI (PowerShell)

```powershell
# pipx (recommended — isolated venv, $PATH handled automatically)
python -m pip install --user pipx
python -m pipx ensurepath
# Restart PowerShell, then:
pipx install beacon-ai
```

The legacy bash entry-point in `bin/beacon` does **not** work in native PowerShell or cmd —
use `pipx install beacon-ai` (Python entry-point) instead. WSL2 users can also clone the repo and
add `bin/` to `$PATH`, but this is not recommended for non-developers.

### Upgrading on Windows

```powershell
winget upgrade BeaconAI.BeaconDesktop  # Desktop
pipx upgrade beacon-ai                 # CLI
```

---

## Linux

### Option A — Beacon Desktop (recommended)

```bash
# 1. Download AppImage from Releases
curl -LO https://github.com/r-kida2/beacon/releases/latest/download/Beacon-x86_64.AppImage

# 2. Make it executable
chmod +x Beacon-x86_64.AppImage

# 3. Run (double-click in file manager also works)
./Beacon-x86_64.AppImage
```

AppImage is self-contained and works on any modern Linux distro (Ubuntu, Fedora, Arch,
Debian, openSUSE, etc.). Move it to `~/Applications/` or `/opt/` to keep your home directory tidy.

> **AppImage prerequisites**
> Most distros have these by default, but minimal containers may need:
>
> ```bash
> sudo apt install libwebkit2gtk-4.1-0 libfuse2   # Debian/Ubuntu
> sudo dnf install webkit2gtk4.1 fuse-libs        # Fedora
> ```

For `.deb` (Debian/Ubuntu) and `.rpm` (Fedora/RHEL) builds, see the
[Releases page](https://github.com/r-kida2/beacon/releases) — they are generated by the
release pipeline but AppImage is the recommended primary distribution channel.

### Option B — Beacon CLI

```bash
# pipx (recommended)
sudo apt install pipx                  # Debian/Ubuntu
# or: sudo dnf install pipx            # Fedora
# or: brew install pipx                # Homebrew on Linux

pipx install beacon-ai
```

Homebrew on Linux is also supported but requires the same tap setup as macOS:

```bash
brew tap r-kida2/beacon
brew install beacon
```

### Upgrading on Linux

```bash
# AppImage: download the new version and replace the old file
curl -LO https://github.com/r-kida2/beacon/releases/latest/download/Beacon-x86_64.AppImage

# CLI
pipx upgrade beacon
```

---

## Common prerequisites

Beacon Desktop bundles everything it needs. The CLI has a few external dependencies:

| Dependency | Minimum version | Required for |
|------------|-----------------|--------------|
| Python | 3.9 (3.11+ recommended) | CLI (pipx / brew formula both install Python automatically) |
| Git | any | Commit tracking (every project should already have this) |
| tmux | any | The legacy `beacon` dashboard command (not required for Beacon Desktop) |

### Optional: cloud features

The cloud sync, team collaboration, and Google sign-in features need two extra Python packages
that are not installed automatically (to keep the base install lightweight):

```bash
pip install google-auth-oauthlib google-auth
```

This applies to both Beacon Desktop and Beacon CLI. Local mode works without these.

---

## First-time setup

After installing by any method, set up your first project:

```bash
cd your-project
beacon setup
```

`beacon setup` walks you through:
1. Google sign-in (optional — skip if you only need local mode)
2. Claude Code Skills and hook installation
3. Project initialisation or joining an existing cloud project
4. Launching the dashboard

For a quick **local-only** start without the wizard:

```bash
cd your-project
beacon init
beacon milestone add "First milestone"
beacon milestone start ms-1
beacon          # launches the dashboard
```

If you installed Beacon Desktop, you can also double-click the app icon and pick the project
folder from the launcher — no terminal required.

---

## Code signing status

Beacon is open source and currently distributed **without code signing certificates**:

| OS | Signing status | User-visible impact |
|----|----------------|---------------------|
| macOS | Unsigned (no Apple Developer ID) | Gatekeeper warning on first launch — workaround: control-click → Open |
| Windows | Unsigned (no Authenticode certificate) | SmartScreen warning + possible AV false positive — see [Windows section](#windows) for full workaround |
| Linux | N/A (AppImage doesn't require signing) | No warning |

Obtaining signing certificates (Apple Developer ID + Windows OV/EV Authenticode) is tracked under
**ms-44 task e-730**. SmartScreen reputation building can take several weeks to months after a new
certificate is issued, so even with signing the first few weeks of releases may still show warnings.

---

## Building from source

If your OS or architecture isn't in the published releases yet, you can build from source.

### CLI from source

```bash
git clone https://github.com/r-kida2/beacon.git
cd beacon

# Option 1: pipx (recommended — isolated venv)
pipx install .

# Option 2: PATH approach (no Python entry-point needed)
export PATH="$PATH:$(pwd)/bin"
echo "export PATH=\"\$PATH:$(pwd)/bin\"" >> ~/.zshrc   # or ~/.bashrc
```

### Beacon Desktop from source

```bash
git clone https://github.com/r-kida2/beacon.git
cd beacon/desktop

# Install Node deps
npm install

# Install Tauri CLI globally (once)
npm install -g @tauri-apps/cli@2

# Build for your host OS
npm run tauri build

# Output:
#   macOS:   src-tauri/target/release/bundle/dmg/Beacon-*.dmg
#   Windows: src-tauri/target/release/bundle/msi/Beacon-*.msi
#   Linux:   src-tauri/target/release/bundle/appimage/Beacon-*.AppImage
```

You will need Rust (`curl https://sh.rustup.rs -sSf | sh`) and Node 20+ installed.

---

## Maintainer reference: Publishing releases

> This section is for Beacon maintainers who cut new releases.

### Cutting a release

Releases use **two parallel tag streams** so CLI and Desktop versions can move independently:

| Tag pattern | Triggers | Affected artifacts |
|-------------|----------|--------------------|
| `v<X.Y.Z>` | release pipeline (CLI focused) | Homebrew formula bump, PyPI publish |
| `desktop-v<X.Y.Z>` | `.github/workflows/desktop-release.yml` | `.dmg` / `.msi` / `.AppImage` |
| `cli-v<X.Y.Z>` | `.github/workflows/release-build.yml` | PyPI + sdist + wheels |

Push the appropriate tag and the corresponding GitHub Actions workflow builds artifacts and
attaches them to a GitHub Release.

### Updating the Homebrew tap

The CLI tap lives at **`github.com/r-kida2/homebrew-beacon`**. After a new `v*` tag:

1. Get the source tarball SHA-256:

   ```bash
   curl -sL https://github.com/r-kida2/beacon/archive/refs/tags/v0.6.0.tar.gz | shasum -a 256
   ```

2. Update `Formula/beacon.rb` in the tap repo with the new `url`, `sha256`, `version`.
3. Commit and push to the tap repo. Users running `brew upgrade beacon` will pick up the new
   version automatically.

### Updating the Homebrew cask (Desktop)

The Desktop cask formula lives at `packaging/homebrew/beacon-desktop.rb` in this repo and is
mirrored to the tap repo on each desktop release. After cutting a `desktop-v*` tag:

1. Get the `.dmg` SHA-256s for both architectures from the GitHub Release page.
2. Update `version`, `sha256` (arm64 + x86_64), and the download URL in `beacon-desktop.rb`.
3. Copy the file into `Casks/beacon-desktop.rb` of `r-kida2/homebrew-beacon` and push.

### Updating the WinGet manifest

Manifest sources live at `packaging/winget/`. To submit a new version to the official
`microsoft/winget-pkgs` repository:

1. Bump `PackageVersion` and `InstallerSha256` in the version-specific YAML files.
2. Validate locally:

   ```powershell
   winget validate --manifest packaging/winget
   ```

3. Fork `microsoft/winget-pkgs`, place updated files under
   `manifests/b/BeaconAI/BeaconDesktop/<version>/`, and open a PR.

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

## Deploying the API server (maintainer only)

The Beacon cloud API runs on **Cloud Run** (`beacon-api-prod`, region `asia-northeast1`).

### Deploy

```bash
gcloud run deploy beacon-api-prod \
  --source . \
  --region asia-northeast1 \
  --set-build-env-vars=CACHE_BUST=$(date +%s)
```

`CACHE_BUST` forces Cloud Build to re-copy `lib/` even when only Python files change (Cloud Build otherwise caches the layer). Use `--set-build-env-vars=KEY=VALUE` — `--build-arg` is a Docker CLI flag and is **not recognized by gcloud**.

Quick smoke test after the deploy completes (~3-5 min):

```bash
curl -s https://beacon-ai.dev/health
```

Expect `{"status":"ok",...}` plus a new revision number (`beacon-api-prod-NNNNN-xxx`).

### Why `--source .`

Cloud Build reads the `Dockerfile` at the repo root, copies `lib/` and `server/` into the image, and deploys to Cloud Run automatically. No manual `docker build` or `docker push` needed.

### After deploying

Record the deploy in Beacon so the Releases tab stays current:

```bash
beacon deploy record --desc "brief description of what changed"
```

Or just commit and let the `/beacon-deploy` Skill handle it automatically via the PostToolUse hook.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `command not found: beacon` | Make sure `bin/` is on your `$PATH` (manual install) or that `brew install` / `pipx install` completed without errors. On Windows, run `pipx ensurepath` and restart your terminal. |
| `python3: command not found` | Install Python 3.9+ from python.org or via Homebrew: `brew install python@3.11`. On Windows install from the Microsoft Store ("Python 3.11"). |
| `tmux: command not found` | Install tmux: `brew install tmux` (macOS) or `apt install tmux` (Debian/Ubuntu). Not required for Beacon Desktop. |
| Cloud auth errors | Run `pip install google-auth-oauthlib google-auth` and retry `beacon auth login` |
| Skill install fails | Make sure Claude Code is installed and `~/.claude/` exists, then run `beacon skill install` |
| macOS "unidentified developer" warning | Control-click `Beacon.app` → **Open** → **Open** (one-time approval) |
| Windows SmartScreen blocks installer | Click **More info** → **Run anyway**, or add the install folder to AV exceptions — see [Windows](#windows) for details |
| Windows AV flags `Beacon.exe` | Whitelist the install folder in Defender / your AV. Report as false positive to your AV vendor. |
| AppImage won't run (Linux) | Install `libfuse2` (`sudo apt install libfuse2`) and ensure the file is `chmod +x` |
