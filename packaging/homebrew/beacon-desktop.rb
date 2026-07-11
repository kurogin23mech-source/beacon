cask "beacon-desktop" do
  # DRAFT — ms-44 e-696
  #
  # Homebrew cask for Beacon Desktop. Installs the Tauri-built Beacon.app
  # bundle into /Applications/. Users get auto-upgrade via `brew upgrade --cask`.
  #
  # Until the first `desktop-v*` release publishes signed .dmg artifacts, the
  # `sha256` fields below are placeholders. The release pipeline
  # (.github/workflows/release-build.yml) generates `.sha256` sidecar files
  # alongside each .dmg — paste the values here per release.

  version "0.1.0"

  on_arm do
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    url "https://github.com/kurogin23mech-source/beacon/releases/download/desktop-v#{version}/Beacon-#{version}-arm64.dmg"
  end

  on_intel do
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
    url "https://github.com/kurogin23mech-source/beacon/releases/download/desktop-v#{version}/Beacon-#{version}-x64.dmg"
  end

  name "Beacon Desktop"
  desc "AI-driven milestone tracker for Claude Code sessions"
  homepage "https://github.com/kurogin23mech-source/beacon"

  livecheck do
    url :url
    strategy :github_latest
    regex(/^desktop-v?(\d+(?:\.\d+)+)$/i)
  end

  # No special install rules — the .dmg drops Beacon.app directly.
  app "Beacon.app"

  # Optional CLI shim — symlinks the bundled `beacon` command into Homebrew's
  # bin/ so users who installed Desktop also get the CLI on $PATH.
  # NOTE: enable once the bundled CLI path is finalised (Tauri sidecar layout
  # currently in flux — tracked under e-695). Until then keep this as a
  # commented hint so maintainers don't forget to wire it up.
  # binary "#{appdir}/Beacon.app/Contents/Resources/bin/beacon", target: "beacon"

  # Clean up Beacon's local data directory only if the user explicitly opts in
  # (`brew uninstall --cask --zap beacon-desktop`). Default uninstall leaves
  # ~/.beacon/ and per-project .beacon/ intact so users don't lose data.
  zap trash: [
    "~/Library/Application Support/dev.beacon-ai.desktop",
    "~/Library/Preferences/dev.beacon-ai.desktop.plist",
    "~/Library/Caches/dev.beacon-ai.desktop",
  ]

  caveats <<~EOS
    Beacon Desktop is currently distributed UNSIGNED (we do not yet hold an
    Apple Developer ID — tracked under ms-44 task e-730). On first launch:

      1. Right-click (or Control-click) Beacon.app in /Applications
      2. Choose "Open" from the menu
      3. Click "Open" in the Gatekeeper confirmation dialog

    After this one-time approval Beacon launches normally.

    For the terminal CLI, install separately:
        brew install kurogin23mech-source/beacon/beacon

    Full install guide:
        https://github.com/kurogin23mech-source/beacon/blob/main/INSTALL.md
  EOS
end
