# Installing devtime (instructions for a Claude Code session)

You are setting up devtime for the user: a Claude Code plugin with a local usage dashboard, usage alerts and
token-saving tools. Do the steps below yourself, in order, and report briefly at the end. Ask the user only where
a step says so.

1. **Check Python 3.11+.** Run `python --version` (or `python3 --version` on macOS/Linux). If neither gives 3.11 or
   newer, tell the user to install it (Windows: `winget install Python.Python.3.12` or python.org; macOS:
   `brew install python`; Debian/Ubuntu: `sudo apt install python3`), and stop. Do not continue without it.

2. **Install the plugin.** Run via Bash:

       claude plugin marketplace add billw2012/claude-devtime
       claude plugin install devtime@devtime

   If `claude` is not on PATH, or a command fails, ask the user to run these themselves in a Claude Code session:
   `/plugin marketplace add billw2012/claude-devtime`, then `/plugin install devtime@devtime`. (If the plugin is
   already installed, `claude plugin list` shows it; continue.)

3. **Tell the user what `/devtime:start` will change**, before it runs:
   - it adds one marked block ("Usage tracking (devtime)", between `<!-- devtime:start -->` and
     `<!-- devtime:end -->`) to `~/.claude/CLAUDE.md`; it shows a diff and asks first, and the rest of the file is
     untouched. To undo, delete that block.
   - it may offer to remove duplicate devtime hook entries from `~/.claude/settings.json` (a `.bak` is kept);
   - it asks whether to also set up support for running several sessions in parallel (the machine-wide slot queue and
     agent budget rules; off by default, the recommended answer is no). Later, `/devtime:parallel` or asking Claude
     turns it on or off;
   - it starts a dashboard server on `http://127.0.0.1:8765` and opens it, then starts a 30-minute loop in that
     session (keep the session open). Data is kept in `~/.claude/devtime/`; everything is local.

4. **Activate it.** A plugin installed from inside a running session is not loaded yet. Ask the user to run
   `/reload-plugins` (or restart Claude Code), then run `/devtime:start`. If the skill `devtime:start` is already
   in your skill list, invoke it yourself instead.

## Fallback without plugins

If plugins cannot be used, clone and run from the checkout:

    git clone https://github.com/billw2012/claude-devtime ~/.claude/devtime-src
    cd ~/.claude/devtime-src
    python -m devtime setup check     # reports Python, hooks and CLAUDE.md state
    python -m devtime setup open      # starts the dashboard and opens it in the browser

The hooks and agents then have to be added by hand; see the README and `docs/reference.md`.
