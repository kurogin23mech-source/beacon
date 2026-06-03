class Beacon < Formula
  desc "AI-driven milestone tracker for Claude Code sessions"
  homepage "https://github.com/kurogin23mech-source/beacon"
  url "https://github.com/kurogin23mech-source/beacon/archive/refs/tags/v0.7.1.tar.gz"
  sha256 "b0618124d395e1caf8eb3961dd6a6c51314ec768dc552bc22a80c0104c5fedde"
  version "0.7.1"
  license "MIT"

  # Python 3.11 is recommended; 3.9+ is supported
  depends_on "python@3.11"

  # Optional cloud features require these at runtime (not installed automatically)
  # Users who want cloud sync / auth should run:
  #   pip install google-auth-oauthlib google-auth
  # after installation.

  def install
    # Copy the shell entry-point
    bin.install "bin/beacon"

    # Copy the Python library into a versioned libexec directory so that
    # the shell script can locate it regardless of the Homebrew prefix.
    libexec.install Dir["lib/*.py"]
    libexec.install "lib/firebase_config.json.example"

    # Hook scripts (used by `beacon skill install` to configure Claude Code).
    # Placed alongside the Python library so commands.py can find them.
    libexec.install Dir["bin/*.sh"]

    # Skills source files (distributed to ~/.claude/skills/ via `beacon skill install`).
    prefix.install "skills"

    # Rewrite BEACON_DIR inside bin/beacon so it points to libexec.
    # The original script resolves lib/ relative to itself; after install the
    # layout changes, so we patch the two path variables accordingly.
    inreplace bin/"beacon" do |s|
      s.gsub! 'BEACON_DIR="$(cd "$(dirname "$0")/.." && pwd)"',
              "BEACON_DIR=\"#{libexec}\""
      s.gsub! 'DASHBOARD_PY="$BEACON_DIR/lib/dashboard.py"',
              "DASHBOARD_PY=\"#{libexec}/dashboard.py\""
      s.gsub! 'COMMANDS_PY="$BEACON_DIR/lib/commands.py"',
              "COMMANDS_PY=\"#{libexec}/commands.py\""
    end
  end

  # Post-install note shown to users
  def caveats
    <<~EOS
      Beacon requires tmux and Python 3.9+ (installed as python@3.11 via Homebrew).

      For cloud features (auth, team collaboration), install the optional dependencies:
        pip install google-auth-oauthlib google-auth

      To get started, run the one-time setup wizard from inside a project directory:
        cd your-project
        beacon setup

      Or initialise a project without cloud features:
        beacon init
        beacon milestone add "First milestone"
        beacon milestone start ms-1

      For Claude Code integration, Skills are installed automatically by:
        beacon skill install

      Full documentation: https://github.com/kurogin23mech-source/beacon
    EOS
  end

  test do
    # Verify the binary is executable and returns a known exit code
    assert_match "Beacon - Milestone-driven project management", shell_output("#{bin}/beacon help")

    # Verify the library files are accessible from the patched paths
    assert_predicate libexec/"commands.py", :exist?
    assert_predicate libexec/"dashboard.py", :exist?
  end
end
