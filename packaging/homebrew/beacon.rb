class Beacon < Formula
  desc "AI-driven milestone tracker for Claude Code sessions"
  homepage "https://github.com/kurogin23mech-source/beacon"
  url "https://github.com/kurogin23mech-source/beacon/archive/refs/tags/v0.60.1.tar.gz"
  sha256 "b152df496a9f1ba31a9f70811e5f855224b5a3ca09942035262780d4d4cad56d"
  version "0.60.1"
  license "MIT"

  # Python 3.11 is recommended; 3.9+ is supported
  depends_on "python@3.11"

  # ms-54 e-1169: channel/bus.mjs needs Node + npm at install time (to run
  # `npm install` for the bus dependencies) and at runtime (to spawn the
  # MCP server when Claude Code starts). Without node, `beacon channel
  # install` fails to wire up beacon-bus DM.
  depends_on "node"

  # Optional cloud features require these at runtime (not installed automatically)
  # Users who want cloud sync / auth should run:
  #   pip install google-auth-oauthlib google-auth
  # after installation.

  def install
    # Copy the shell entry-point
    bin.install "bin/beacon"

    # ms-54 e-1167: install the bclaude wrapper so users can launch
    # Claude Code with the Beacon DM channel pre-wired (or auto-disabled
    # when opt-out is active). The bash version is the canonical one;
    # bclaude.cmd is shipped for Windows users who may install via
    # alternate paths but is not symlinked here.
    bin.install "bin/bclaude"

    # Copy the Python library into a versioned libexec directory so that
    # the shell script can locate it regardless of the Homebrew prefix.
    libexec.install Dir["lib/*.py"]
    libexec.install "lib/firebase_config.json.example"

    # Hook scripts (used by `beacon skill install` to configure Claude Code).
    # Placed alongside the Python library so commands.py can find them.
    libexec.install Dir["bin/*.sh"]

    # Skills source files (distributed to ~/.claude/skills/ via `beacon skill install`).
    prefix.install "skills"

    # ms-54 e-1169: ship channel/ assets (bus.mjs + package.json) and run
    # `npm install` once at install time so the bundled deps live alongside
    # libexec/. cmd_channel_install resolves this layout via candidate #3
    # in _resolve_channel_root() (commands.py at libexec/, channel/ as
    # sibling). node_modules is committed at install — the formula owns
    # the lifecycle, runtime auto-install is the fallback for dev clones.
    (libexec/"channel").install Dir["channel/*.mjs"], "channel/package.json"
    if File.exist?("channel/package-lock.json")
      (libexec/"channel").install "channel/package-lock.json"
    end
    cd(libexec/"channel") do
      system "npm", "install", "--silent", "--no-audit", "--no-fund"
    end

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

      Getting started (Claude Code + Skill driven):

        1. (optional, only if you want cloud sync) Run the setup wizard:
             beacon setup
           This signs you in to Beacon Cloud and creates / joins a project.

        2. Open Claude Code in your project directory and start a session:
             cd your-project
             claude
           Then in Claude Code, talk to /beacon-init:
             > /beacon-init
           This walks you through naming the project and stating its
           high-level objective in a conversational form.

        3. Keep exploring direction with the same chat-driven flow:
             > /beacon-vision     # deepen the project's why / who / done-state
             > /beacon-roadmap    # propose a milestone sequence toward the goal

      Skills are installed automatically the first time you run `beacon setup`,
      or you can install them on demand with:
        beacon skill install

      Multi-session DM (ms-54 e-1167):
        bclaude          # launches `claude` with the Beacon DM channel
        beacon channel status  # check install / opt-out state
      `bclaude` honors the opt-out flag (env BEACON_NO_BUS, project, or global)
      and falls back to plain `claude` when set. Run `beacon channel install`
      in your project to wire up the MCP entry, or `beacon channel opt-out`
      to disable DM entirely.

      Full documentation: https://github.com/kurogin23mech-source/beacon
    EOS
  end

  test do
    # Verify the binary is executable and returns a known exit code
    assert_match "Beacon - Milestone-driven project management", shell_output("#{bin}/beacon help")

    # Verify the library files are accessible from the patched paths
    assert_predicate libexec/"commands.py", :exist?
    assert_predicate libexec/"dashboard.py", :exist?

    # ms-54 e-1167: bclaude wrapper is on PATH and looks like a script.
    assert_predicate bin/"bclaude", :exist?
    assert_predicate bin/"bclaude", :executable?
  end
end
