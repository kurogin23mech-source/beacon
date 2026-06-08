# Getting Started: Multi-session DM

Beacon's DM channel lets parallel Claude Code sessions exchange messages
in real time — across machines, worktrees, or projects. This is most
useful when you have:

- A long-running session on one machine that you want to consult from
  another
- Two worktrees of the same repo (e.g. `main/work` and `feature/work`)
  that need to hand off context
- An AI dispatched as a sub-agent that has to report back to a parent

The wire underneath is the `beacon-bus` MCP server. You don't normally
interact with it directly — `bclaude` and the `beacon bus` CLI cover
99% of the workflow.

## Install

If you ran `beacon setup`, it's already installed (Step 4/5). To do it
by hand:

```bash
beacon channel install
```

This drops a `beacon-bus` entry into `./.mcp.json` so Claude Code knows
how to start the bus. Node.js must be on PATH (the formula declares the
dependency; for non-Homebrew installs use `brew install node`,
`apt install nodejs`, or `winget install OpenJS.NodeJS.LTS`).

## Launch Claude Code with DM enabled

The simplest way:

```bash
bclaude
```

`bclaude` is a tiny wrapper bundled with Beacon. It forwards all your
arguments to `claude` and adds the channel flag. If you've opted out
(see below), it falls back to plain `claude` and prints a one-line
audit message to stderr.

Old way (still works):

```bash
claude --dangerously-load-development-channels server:beacon-bus
```

## Typical flows

### See who's around

```bash
beacon bus directory --live
```

Lists every Beacon session currently active in the project. Each entry
shows the session ID, the machine name, and the last heartbeat time.

### Send a DM

```bash
beacon bus send --channel dm --to <session_id> \
  --payload '{"text": "hey, can you check the build?"}'
```

The recipient sees the message inside their Claude Code session as an
incoming `<channel>` event. They can reply via the bus reply tool.

### Receive (one-shot)

```bash
beacon bus receive --channel dm --timeout 30
```

Blocks up to 30 seconds, prints the first new event, exits.

### Receive (long-poll, ack-as-you-go)

```bash
beacon bus listen --channel dm --auto-ack
```

Streams events as they arrive. Auto-advances the cursor so you don't
re-read the same event on the next call.

## Opt out (any time)

Two scopes. Same shape on disk so the user can grep for either.

```bash
beacon channel opt-out --project   # this project only
beacon channel opt-out --global    # everywhere, future projects too
```

There's also a transient escape hatch — `BEACON_NO_BUS=1` in the shell
disables DM for that shell only.

To check the current state:

```bash
beacon channel status
```

The output tells you (1) whether the MCP entry is installed, (2)
whether the JS dependencies are on disk, (3) which opt-out sources are
active, and (4) what the next auto-install attempt would do.

To re-enable:

```bash
beacon channel opt-in --project    # or --global
```

## Uninstall

```bash
beacon channel uninstall                  # remove MCP entry, keep deps
beacon channel uninstall --purge-files    # also move channel/node_modules
                                          # to .trash/
```

`--purge-files` moves the JS dependencies into the project's `.trash/`
directory rather than deleting them, so a mistake is easy to undo.

## Why is this preview?

The MCP "Channels" feature is research preview in Claude Code itself.
`bclaude` uses the `--dangerously-load-development-channels` flag,
which will be replaced by a stable `--channels plugin:beacon-bus`
syntax once Channels GA ships. The wrapper handles the migration
internally; you won't need to change your invocation.

See [`docs/SPEC.md`](SPEC.md) for the broader Beacon architecture.
