# devtime

A Claude Code plugin that shows where your Claude Code time and tokens go, warns you when sessions burn too much, and
helps you cut usage.

- **Local dashboard** (`http://127.0.0.1:8765`): time and estimated cost per project, session, subagent and kind of work
  (reading code, editing, builds, tests, waiting on subagents, idle ...), built from the transcripts Claude Code already
  writes to `~/.claude/projects/`.
- **Weekly-limit chart**: your plan's usage against the weekly limit, with exact readings harvested from the
  `get_usage` calls your sessions make, and projections to the next reset.
- **Readings from every session**: a hooks module reports each session's 5-hour and weekly usage to the dashboard
  (no `get_usage` call needed), and costs are shown as a share of the weekly limit ("% of week") once the allowance is
  fitted, with its uncertainty; dollars stay in the tooltips. (Needs a Claude Code build with function hooks.)
- **Usage alerts**: subagents that cost too much, re-write their prompt cache after long waits, run with huge
  contexts, or burn money fast. Alerts reach the dashboard and the session concerned.
- **Slim agent types**: `coder`, `reader` and `runner` agents with a small fixed context, plus hooks that stop subagents
  waiting on long jobs (each wake-up re-writes their whole cache).
- **Optimization recommendations**: your last 7 days ranked against a catalogue of changes, estimated in $/week.
- **A/B experiments**: try a change (for example Sonnet instead of Opus for coding stages) with arms assigned by
  hash, and read the result per arm.

## Privacy

Everything stays on your machine. The dashboard shows labels, timings and token counts only (shell command
descriptions, file names, subagent descriptions, model names). Prompts and tool outputs are never stored or served.
The server binds to `127.0.0.1`. No account, token or network access is needed.

## Requirements

- Python 3.11 or newer (standard library only).
- Claude Code. The Claude desktop app provides the `get_usage` tool used for plan readings; CLI-only sessions still get
  full cost and time tracking, just without exact weekly-limit readings.

## Install

    claude plugin marketplace add billw2012/claude-devtime
    claude plugin install devtime@devtime

Then restart Claude Code (or run `/reload-plugins`) and run `/devtime:start`. Inside a session the equivalent is
`/plugin marketplace add billw2012/claude-devtime` and `/plugin install devtime@devtime`.

Or ask Claude Code to do it: open a session in a clone of this repo and say "set this up"; it follows
[INSTALL.md](INSTALL.md).

## Organisations

If you are signed into several Claude organisations, each gets its own devtime: separate data, server, port and
dashboard (the page shows the org's name), and nothing of one org is visible to another. Run `/devtime:start` in a
session of each org you want to track; `python -m devtime org` shows which org and port a shell uses. Settings in
`~/.claude/devtime/config` are shared.

## Skills

**`/devtime:start`** sets everything up the first time and keeps it running. It checks Python and, with your confirmation, adds a
marked block to `~/.claude/CLAUDE.md` that asks sessions to read `get_usage` before launching agent stages and to stop
starting new ones at a share of the weekly limit you choose (100 / 95 / 85 / 70 % or your own), and to pause new
agents at a share of the current 5-hour window until it resets. It offers to remove duplicate hook entries, starts the
dashboard and opens it, then runs a 30-minute loop in the session: record a plan reading, label new work for the
weekly work lane, and every two hours let a small reviewer agent flag sessions that waste money. Keep that session
open; a cheap model is fine for it.

**`/devtime:optimize`** measures your last 7 days, ranks the catalogue of optimizations by estimated $/week
(slim agent types, long jobs on a Haiku runner, hand-off past ~250k context, auto-compact window, scripts first,
limited parallelism, background jobs instead of poll loops, ...; the items that only matter for parallel work are
offered once parallel support is on), asks which to apply, shows a dry-run diff and applies
them to `~/.claude/CLAUDE.md` or `settings.json`. Each application is recorded as a marker on the weekly chart so the
effect is visible, and `python -m devtime optimize remove <id>` undoes it.

**`/devtime:ab`** turns an optimization (or any idea) into an experiment: `/devtime:ab <id>` creates it,
`/devtime:ab report <id>` compares the arms (median cost and time, commits, rework rate, cost per commit, with a warning
when there are too few stages to call it), `/devtime:ab stop <id>` ends it and offers to apply the winner. Arms are
assigned by hash, not by the orchestrating session, and a hook enforces them.

**`/devtime:parallel`** is the optional add-on for running several sessions (or autonomous pipelines) on one PC, and it
is **off by default**. devtime has a base set that is always on: the dashboard, usage alerts, the optimizations and A/B
experiments. Parallel support only adds to it, and never turns anything off: the machine-wide slot queue (below) and the
agent budget rules (subagents are refused sleeps, poll loops and long blocking waits, and are asked to hand off past
~250k tokens of context). Note that both also act in your interactive sessions on this PC: heavy commands there get
queued too (interactive editors are exempt). `/devtime:start` asks once at first setup; later run `/devtime:parallel` or
ask Claude to turn devtime parallel support on or off. It also adopts the slot queue in a project: several sessions
(say one per project, running autonomously) share the GPU, headless editor / test runs and builds through one FIFO queue per resource
(default `gpu`=2, `test`=3, `build`=2 at once; `quiet` = test+gpu exclusively). The plugin's hook queues any command
that matches a rule in `~/.claude/devtime/config/slots.toml` by rewriting it to
`python ~/.claude/devtime/devtime.py slot run <resource> -- <command>`. The rules ship covering UnrealEditor(-Cmd),
Build.bat / RunUAT / ue-parallel-build and background Blender, and rules for a project's own scripts are **learned
automatically**: the devtime server watches for heavy programs (UnrealEditor, `blender -b`, UBT) started by a Claude
Code shell outside the queue, finds the project script that started them and writes a rule for it
(`slot rules --learned` lists them; timing runs, `perf` / `measure` / `benchmark` ..., are queued `quiet`). The skill
is optional: review or adjust learned rules and add ones learning can't see. A
slot frees itself however its job ends (crash, kill, closed session, reboot), and the hook refuses loops that wait for
another process or a lock file. `slot status` shows who holds what and who waits; the dashboard shows queue waits and
alerts a session whose ticket waits over 20 min. `DEVTIME_SLOTS=0` turns the wrapping off.
Without parallel support none of this is active (the slot queue and the agent budget hook rules stay off).

## Data and configuration

Runtime data (caches, alerts, plan readings, server log) lives in `~/.claude/devtime/`; your settings and project
profiles in `~/.claude/devtime/config/`. Set `DEVTIME_HOME` to use another folder, `CLAUDE_CONFIG_DIR` if your Claude
config is not `~/.claude`. See [docs/reference.md](docs/reference.md) for settings, pricing, classification rules,
project profiles ([examples/projects/example.toml](examples/projects/example.toml)) and alert thresholds.

## Running from a checkout

    git clone https://github.com/billw2012/claude-devtime
    cd claude-devtime
    python -m devtime serve          # dashboard on http://127.0.0.1:8765
    python -m devtime report         # text summary
    python -m unittest discover -s tests

## Uninstall

    claude plugin uninstall devtime@devtime
    claude plugin marketplace remove devtime

Then delete the blocks between `<!-- devtime:start -->` and `<!-- devtime:end -->` (and any
`<!-- devtime:opt:... -->` / `<!-- devtime:ab:... -->` blocks; `python -m devtime optimize remove <id>` does the same) from
`~/.claude/CLAUDE.md`, and `~/.claude/devtime/` if you want the data gone.

## License

MIT, see [LICENSE](LICENSE).
