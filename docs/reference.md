# devtime reference

Technical reference for the devtime plugin: commands, what is measured, configuration, rules and alerts. For what it
is and how to install it, see the [README](../README.md).

Commands (from a checkout, or `python <plugin dir>/devtime/...` via `python -m devtime`):

    python -m devtime serve          # http://127.0.0.1:8765, rescans every 10 s, page refreshes itself
    python -m devtime report --project MyProject [--since 2026-10-07] [--top 15]
    python -m devtime projects       # every project found, with totals
    python -m devtime json out.json --project MyProject
    python -m devtime findings --latest   # the last Unreal run: phases, slowest steps, what wastes time

## Plugin contents

The plugin registers its hooks (`hooks/hooks.json`: the SessionStart hook starts the dashboard server on
http://127.0.0.1:8765 if it is not running; `agent_budget.py` guards subagents and delivers usage alerts) and the slim
`coder`, `reader` and `runner` agents plus the `labeller` and `reviewer` agents. Nothing runtime-generated lives in the plugin: caches, alerts, plan samples,
the server log and archived Unreal logs go to `~/.claude/devtime/` (`cache/`, `logs/`), your settings and project
profiles to `~/.claude/devtime/config/`. Set `DEVTIME_HOME` to use another folder. No account or token is needed; everything
is read from the local transcripts.

### Start and keep it updated: `/devtime:start`

Run `/devtime:start` in Claude Code after installing. The first time it

- checks Python, asks for a weekly usage cap (85 % suggested) and adds a short "Usage tracking (devtime)" block to
  `~/.claude/CLAUDE.md` (between `<!-- devtime:start -->` and `<!-- devtime:end -->`; the rest of the file is untouched,
  re-running replaces the block). The block asks sessions to call `get_usage` before launching agent stages, which is
  where the dashboard's plan readings come from, and to stop starting agents at the cap;
- offers to remove duplicate hook entries from `settings.json` (e.g. from an earlier manual install; a `.bak` is kept);
- starts the dashboard server and opens it in the browser;
- does a catch-up pass, then starts `/loop 30m /devtime:start tick` in the session.

The loop lives in the session (keep it open; a cheap model such as Sonnet or Haiku is fine for it). Each pass adds
very little context: one `get_usage` call (records the plan reading via the transcript), one
`python -m devtime tick` call (server check plus a compact JSON: reading, unlabelled work items, whether a review is
due, active alerts, cap), and, only when there is something to judge, a small Haiku subagent:

- `devtime:labeller` names a work section for each unlabelled agent / session (`work-items` -> `work-assign -`), which
  drives the dashboard's weekly work lane;
- `devtime:reviewer` (every 2 h) reads `python -m devtime review-context` (the `/api/watch` summary plus the tail of
  `review-log.md`), leaves at most 3 notes with `review-note` for sessions that waste money (wrong agent types, waiting
  sessions, no hand-off) and records the review with `review-done`. Deliberate `AB:` experiment arms are never flagged.

The mechanical parts are `python -m devtime setup check | claude-md --cap N [--dry-run] | open`, `tick`,
`review-context [--hours 2]` (under 5 KB: the 25 most expensive subagents, the last 12 review lines), `review-done --text ...` and `work-assign -` (JSON on stdin); `CLAUDE_CONFIG_DIR`
overrides `~/.claude`, `DEVTIME_NO_SERVER_START=1` stops `tick` from starting a server.

### Cut usage: `/devtime:optimize`

`/devtime:optimize` measures your last 7 days, ranks the catalogue in `optimizations/catalogue.toml` by estimated
$/week, adds up to three unproven ideas of its own from the metrics, asks which to apply (globally, or as an A/B with
`/devtime:ab <id>`), shows a dry-run diff and applies. Items: slim agent types, long jobs on a Haiku runner,
read-only work on Haiku, hand-off past ~250k context, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000`, scripts first,
2-3 agents at once, Sonnet for well-specified coding (suggest A/B), the weekly cap, background jobs instead of poll
loops. Each item has the summary, the metric that drives it and what was measured on the author's own usage.

- `python -m devtime analyze [--days 7]`: compact JSON (~2.5 KB): cost per week by model and role, base context by
  agent type, the share of cost in requests above 250k / 400k context, subagent wait hours and cache re-writes after
  idle gaps over 5 min, read-only subagents on big models (and their cost at Haiku prices), Opus coding agents (at
  Sonnet prices), concurrent agents, recurring failures, and the catalogue ranked by `estWeeklyUsd`
  (`{id, applied, by, estWeeklyUsd, why}`). Estimates are upper bounds ("if every such request were fixed") scaled
  to a week and overlap (hand-off and auto-compact, runner and polling); applied items show what is still being lost.
- `python -m devtime optimize list | apply <id> [--dry-run] | remove <id>|all [--dry-run] | adopt <id> --heading H [--dry-run]`: `claude_md` actions write a block between
  `<!-- devtime:opt:<id> -->` and `<!-- devtime:opt-end:<id> -->` in `~/.claude/CLAUDE.md` (the rest is untouched),
  `settings_env` actions set a key of `env` in `settings.json` (other keys and the indent are kept, `settings.json.bak`
  is written first), `note` actions are manual steps. Every `settings.json` change is recorded in the install ledger
  `<data dir>/config/installed.json` as `{key, before, after}`: `remove` puts `before` back (deletes the key if it was
  absent) when the value is still `after`, leaves a value you changed since, and leaves settings alone when there is no
  record. `remove all` removes every opt block, every recorded settings change and the `devtime:start` block.
  `adopt <id> --heading H` wraps your own CLAUDE.md section (heading to the next heading of the same or higher level)
  in the tags so it becomes removable. `list` shows each block's state (devtime / adopted / manual / partly) and what
  remove would do. Applying or removing records a change marker ("Applied <title>")
  that the dashboard's weekly chart draws, so the effect is visible. A catalogue item counts as applied when its block
  or env entry exists, or (`by: manual`) when CLAUDE.md already says it, matched by the item's `detect` regex.
  `CLAUDE_CONFIG_DIR` overrides `~/.claude`.

### Test a change first: `/devtime:ab`

`/devtime:ab <catalogue id or description>` sets up an A/B experiment (for example Sonnet vs Opus for coding stages),
`/devtime:ab report <id>` reads it, `/devtime:ab stop <id>` ends it (and offers to apply the optimization if its arm won).
Built to avoid the mistakes of a hand-run A/B, where the orchestrating session chose the model per stage (the hard
ones went to one arm), fix-ups were named differently (rework missed) and cost ignored output:

- Experiments are in `~/.claude/devtime/config/experiments.toml` (`id`, `title`, `active`, `started`, `arms` with
  `name` / `model` / `agent_type` / `instructions`, optional `opt`). `python -m devtime ab new <id> --title ... --arm
  name[:model[:agent_type]] --arm ... [--instructions name=text] [--opt <catalogue id>] [--dry-run]` writes it and a block
  between `<!-- devtime:ab:<id> -->` and `<!-- devtime:ab-end:<id> -->` in `~/.claude/CLAUDE.md`.
- The block tells sessions to run `python ~/.claude/devtime/devtime.py ab arm <id> <stage>` before each coding stage and
  name the agent `AB:<id>:<stage> <description>` (fix-ups reuse the tag). `devtime.py` is a launcher that the SessionStart
  hook (re)writes beside `plugin_root.txt`, so the text keeps working when the plugin's install path changes on update.
- The arm is `sha1(id:stage) mod number of arms`: assigned by hash, not by the session. `agent_budget.py` denies an
  Agent / Task launch tagged `AB:<id>:<stage>` of an active experiment whose model or agent type differs from the arm's,
  naming the right arm. Unknown or stopped experiments pass.
- `ab report <id> [--json]`: per arm the stages, median cost and minutes, commits, edit calls, output tokens, tool errors,
  rework rate and cost per commit. A stage is reworked if a later agent (within 24 h) has the same tag, or is in the same
  work section with a fix / repair / rework / again / retry / follow-up / "...2" description, or has the tag plus a number
  (`view-toggle2` counts as a continuation of `view-toggle`). Arms under 10 stages are flagged "too few stages to call", arm
  imbalance and stages that ran off-arm are noted. `ab stop <id>`, `ab list`.
- The dashboard's experiments table shows new-style experiments next to the legacy `AB:<stage>` rows (Experiment column).

Python 3.11+, standard library only. The first scan reads every transcript (a few GB of transcripts take about 10 s with a process pool);
after that only the bytes appended since the last scan are read (byte-offset cache in `~/.claude/devtime/cache/`), and only
transcripts that changed are re-profiled.

## What it measures

Each transcript's wall-clock is split into intervals:

- **model** - one per API request, from the record that triggered it to its last block; carries token usage,
  thinking time (`thinkingDurationMs`) and estimated cost;
- **tool calls by kind** - tool_use to tool_result. Background shell runs last until their task-notification is
  queued (main sessions: `queue-operation`; subagents: `queued_command` attachments), a `TaskStop`, or - if neither
  was recorded - the last write of the task's output file (metadata only). A foreground call that never got a result
  (crash, interrupt) ends at the next message, capped at its shell timeout;
- **compaction** - from the compact boundary's own duration;
- **gaps** - under 30 s are harness *overhead*; longer ones are *waiting on subagent* (a background agent or
  workflow is running), *waiting on user* (a human prompt ends it), *waiting on wakeup* or *idle*.

Concurrent intervals in one transcript share their overlap equally, so a transcript's shares add up to its
wall-clock. **Agent time** is the sum over every transcript of the non-waiting shares (two subagents busy for an hour
= 2 h); **busy wall-clock** is the union of those intervals across transcripts, and their ratio is the average
parallelism.

Cost is estimated per request from `message.usage` (input, output, cache reads, 5-minute and 1-hour cache writes)
and the prices in `config/devtime.toml`. It runs 3-10 % below the `cost-state` totals Claude Code writes on resume,
because some requests (permission classifiers, summaries) never appear in the transcripts; the dashboard shows both.

## Engine logs: inside Unreal runs

Optional. A project with an `[unreal]` section (`log_globs`) has its Unreal Editor logs harvested: `Saved/Logs`, sibling
checkouts and worktrees, `Saved/Crashes/*/` (logs of crashed runs) and logs runs write into Claude Code session
scratchpads. Every gap between two log lines is attributed to

- a **phase**: startup (until the engine is initialised), map load, editor idle, script (from the first Python line),
  shutdown (from the exit request);
- an **activity**: the category of the line that ends the gap (Zen/DDC server, SDK detection, shader compile, PCG
  generation, python, saving, ... - rules in `config/devtime.toml [unreal]`, project rules first);
- for script lines a script logs itself, a **step**: the time since its previous line, labelled with the message
  normalised (numbers -> `#`, paths -> `<file.png>`) and grouped into step kinds (screenshot, check, load / generate,
  ...).

Each run is matched to the agent's shell call that launched it (the shortest call whose command looks like an editor
launch and whose window holds the run's start), so "unreal run" time splits into time in each editor phase,
*outside the editor* (GPU-lock waits inside scripts, launch, waits after exit), time *polling a running editor*, and
calls with *no editor log*. Unreal purges old logs and Claude Code cleans scratchpads, so each log is summarised as
soon as it appears and archived gzipped to `~/.claude/devtime/logs/<project>/<day>/` (local); the runs are rebuilt
from the archive when the rules change or the cache is lost. Only phase / activity names, script names and the
normalised step labels reach the dashboard.

### Findings

Every harvested run is checked for patterns that waste time (`devtime/unreal.py` `detect_issues`): the editor
throttled to ~3 fps (a background editor without `-unattended`; frame rate from the log's frame counter), repeated
steps of the same length (a fixed-time wait), Python blocking the editor (a gap the frame counter did not move
through), checks that fail only at their timeout, slow startups. The dashboard's Findings table groups them by
script and pattern with the fix; `python -m devtime findings [--latest] [--script X] [--since 1d] [--json]` prints
the same from the Unreal logs alone (0.4 s), and `/api/findings` serves it.

## Hooks

The plugin registers `ensure_server.py` (SessionStart) and `agent_budget.py` (below) through `hooks/run.sh`, which
finds `python3` / `python`.

- `hooks/register.tsx` is a hooks module (`"modules"` in `hooks/hooks.json`, beside the command hooks; needs a Claude
  Code build with function hooks, older builds ignore it). `session.measure` runs in **every** session (CLI and desktop),
  observes only and draws nothing: when the session's `rateLimits` hold `five_hour` and/or `seven_day` it POSTs
  `{session, at, fiveHour, fiveHourReset, weekly, weeklyReset}` (percent used, ISO reset times; the session id is
  `$.session.id()`) to `http://127.0.0.1:<port>/api/reading`, silently, failures ignored. The port is read from
  `<data dir>/cache/port.txt` (data dir = `DEVTIME_HOME`, else `~/.claude/devtime`), else 8765. The same module draws the
  one-line band above the prompt and the `devtime` pane (`/devtime-panel`), only in the session that ran
  `/devtime:start`: pace, rate, cost per commit, waste, live burners, alerts, polled every minute. Costs there are shares of
  the week once an allowance is known. Tests: `claude plugin test .` (`hooks/panel.test.ts`; the engine CLI, not the old `claude`).
- `POST /api/reading` (`devtime/readings.py`): accepts only loopback peers with a loopback `Host` and
  `Content-Type: application/json`. It resolves the session's org (scanned transcripts, the desktop index, else the
  transcript file's own `credential_org` tag; a session not found yet is queued and recorded when it is known, or under
  `unknown` after 15 min) and appends a sample to `cache/plan_samples.jsonl` in the format `plan-sample` writes, with the
  org and exact reset times. A reading identical to the org's last one is not stored again, and a stale one (the same weekly
  window with a lower share, or an earlier window: an idle session repeating an old figure) is dropped. Answers
  `{"result": "recorded"|"duplicate"|"stale"|"queued"}`. `/devtime:start`'s `get_usage` call is only a fallback.
- `hooks/unreal_findings.py` (optional, not registered by the plugin; add it to
  `~/.claude/settings.json` yourself): after a call that ran or waited on an Unreal run of a
  project with `[unreal] log_globs`, tells the agent what that run wasted and how to fix it.
- `hooks/ensure_server.py` also writes `~/.claude/devtime/plugin_root.txt` and the launcher `devtime.py` (see `/devtime:ab`).
- `hooks/agent_budget.py` (PreToolUse `*` + PostToolUse Bash|PowerShell|TaskOutput|Monitor): subagents only (hook
  input carries `agent_id` only there). Denies sleeps, poll loops and blocking waits that may run over 4 min (a
  subagent's prompt cache lives 5 min, so waking up re-writes the whole context), lets a plain long job (a build)
  through until one of the agent's calls has actually run over 4 min, then refuses further ones; the reason tells the
  agent to hand the job back to the main session and end. Past 250k tokens of context (then every 150k) it tells the
  agent to finish its step and hand off. ~0.1 s per call. Limits: `DEVTIME_AGENT_WAIT_LIMIT`, `_WAIT_BUDGET`,
  `_CONTEXT_NUDGE`. Also (main sessions too) enforces the arms of active `/devtime:ab` experiments on Agent / Task launches,
  and queues heavy commands for machine-wide slots (see "Slots" below).

## Slots: a machine-wide queue for heavy jobs

`devtime/slots.py` (runtime), `devtime/slotrules.py` (configuration, rules, the hook's rewrite), `/devtime:slots`
(adopt in a project). One FIFO queue per resource, shared by every Claude Code session on the PC, whatever its
project or organisation (on purpose: they share the machine). Nothing in the code is project-specific; projects only
add rules.

    python ~/.claude/devtime/devtime.py slot run RES[+RES...] [--exclusive] [--max-hold MIN] [--label TEXT] -- CMD ...
    python ~/.claude/devtime/devtime.py slot status [--json]        # per resource: used/limit, holders, queue
    python ~/.claude/devtime/devtime.py slot kill TICKET             # stop a holder (or drop a waiting ticket)
    python ~/.claude/devtime/devtime.py slot log [--since 2h] [--json]
    python ~/.claude/devtime/devtime.py slot rules [--project P]     # rules in force (user ones first)
    python ~/.claude/devtime/devtime.py slot test "COMMAND" [--tool Bash|PowerShell]   # what a command would queue for
    python ~/.claude/devtime/devtime.py slot add-rule --project P --match RE --resource R [--cwd RE] [--exclusive] [--max-hold MIN]
    python ~/.claude/devtime/devtime.py slot remove-rules --project P

(`python -m devtime slot ...` from a checkout is the same; the launcher runs in the caller's folder.) `RES` is a
resource, several joined with `+` (all taken together), or a group. `--shell bash|powershell` runs a shell command line
(from env `DEVTIME_SLOT_CMD`, which is how the hook passes it, or the words after `--`) in a fresh shell of that kind.

**Configuration**: `<plugin>/config/slots.toml` holds the defaults (read it for every key); override or extend them in
`~/.claude/devtime/config/slots.toml` (machine-wide, not per org): `[settings]`, `[resources]` and `[groups]` merge key
by key, your `[[rule]]`s are checked before the shipped ones.

- `[resources]`: `name = N` (jobs at once) or `name = { limit = N, max_hold = MIN }`. Defaults gpu 2, test 3, build 2;
  an unlisted resource has `default_limit` (1) slots.
- `[groups]`: `quiet = ["test", "gpu"]`: requesting a group takes every slot of each of its resources.
- `[settings]`: `dir` (lock directory; default `%LOCALAPPDATA%/devtime-slots` on Windows, `~/.claude/devtime/slots`
  elsewhere; env `DEVTIME_SLOTS_DIR` wins), `max_hold` (45 min), `poll_s` (1 s), `log_kb` (512), `alert_wait_min`
  (20), `default_limit`.
- `[[rule]]`: `match` (case-insensitive regex), `resource` (a resource, `a+b`, a group, or `none` to exempt), optional
  `exclusive`, `max_hold`, `tool` (regex on Bash / PowerShell), `cwd` (regex on the session's folder, `d:/...` form),
  `project` (a tag; `add-rule` writes its rules between `# >>> devtime-slots project=P` / `# <<< devtime-slots`
  markers so `remove-rules` can take them out again).

**Rules match programs, not text.** A command line is split into simple commands (at `;` `&&` `||` `|` `&`,
newlines, brackets, `$(`), and a rule counts only if its match starts inside the program a command runs: the first
word after `VAR=x` assignments and wrappers (`env`, `nohup`, `timeout N`, `cmd /c`, `Start-Process`, `uv run`, ...),
or for an interpreter (`bash`, `sh`, `python`, `pwsh`, `node`, ...) also the script it gets; `bash -c` / `-Command` /
`eval` strings are analysed like a command line, and a PowerShell `$x = "...exe"` / bash `X=...` assignment is followed
into `& $x` / `"$X"`. So `grep UnrealEditor`, `tasklist | findstr UnrealEditor` or `git commit -m "... Build.bat"`
never queue. Several matching commands in one line queue for all their resources at once.

**The hook** (in `hooks/agent_budget.py`, every session, main and subagents; Bash and PowerShell; ~15 ms on top of
the hook's start-up, rules compiled once per call, no server call): a matching command not already under `slot run`
is rewritten via `hookSpecificOutput.updatedInput` (Claude Code 2.1.295 applies it when the hook gives no
permission decision, so the normal permission checks then see the wrapped command; `run_in_background` and the other
fields are kept). The original command travels unchanged in env `DEVTIME_SLOT_CMD` (bash: a single-quoted string,
with `MSYS2_ENV_CONV_EXCL` so Git Bash doesn't convert paths in it; PowerShell: a literal `@'...'@` here-string) and
runs in a fresh bash / Windows PowerShell in the same folder; the agent is told it was queued. Refused instead:
a matching job started in the background inside the command (`&`, `Start-Process` without `-Wait`: it would outlive its
slot; use `run_in_background`), and loops that wait for another job (a `while` / `until` loop that sleeps and polls
`tasklist`, `pgrep`, `ps`, `Get-Process`, `kill -0` or a lock file), with a pointer to `slot run`. In subagents such a
loop also counts as a wait longer than the wait limit, whatever its timeout. Queue time counts toward the tool's
timeout: long jobs belong in `run_in_background`. Permission rules: an allow rule for the bare command no longer
matches the wrapped one; sessions in auto / bypass mode are unaffected.

**Escape hatch**: env `DEVTIME_SLOTS=0` (for the session, e.g. in settings.json `env`) or `DEVTIME_SLOTS=0` anywhere in
a command leaves commands unwrapped and turns the wait-loop refusal off (subagents' wait limit still applies).

**Ordering**: strict FIFO per resource. A ticket starts only when no older waiting ticket wants any of its resources
and each has room; an exclusive or group request waits until every slot is free, and nothing queued behind it
overtakes, so it can't starve. A nested `slot run` for a resource its ancestor already holds runs at once (no
self-deadlock); child processes get `DEVTIME_SLOT_HELD` and `DEVTIME_SLOT_TICKET`.

**Liveness** (no mkdir or PID files as the source of truth): each ticket is `<dir>/tickets/<id>.json`, locked by the
live `slot` process for its whole life (Windows `msvcrt.locking` on a byte far past the content, so it never blocks
readers; POSIX `fcntl.flock`); holding slots means owning a ticket in state `holding`. A ticket whose lock can be
taken has no live owner (crash, kill, closed session, reboot; PID reuse can't fool it): the next process that looks
at the queue deletes it and logs `swept`. Every queue change happens under `<dir>/queue.lock` (milliseconds). On
Windows the command runs in a Job Object with `KILL_ON_JOB_CLOSE`, created suspended and assigned before it runs: when
the `slot` process dies, its whole tree (editor, test runner, their children) dies with it, so no job runs while its
slot is free; processes the command leaves behind are ended when it exits. On POSIX it runs in its own process group,
which is ended on max hold, kill or exit (a SIGKILLed wrapper can't take the group with it there). While queued, the
`slot` process watches its caller (the tool's shell, by handle on Windows): if it is gone, the ticket is dropped.
After `max_hold` minutes the job's tree is killed, the slot freed and `timeout` logged.

**Exit codes**: the command's own, or 123 stopped by `slot kill`, 124 killed at max hold, 125 the caller went away
while queued, 127 the command could not be started, 2 usage error, 130 interrupted. If the lock directory can't be
used (permissions, a sandbox), `slot run` says so and runs the command unqueued rather than blocking the work.

**Log**: `<dir>/log.jsonl`, one JSON line per `acquire` / `release` / `timeout` / `kill` / `gone` / `swept`:
`t, event, ticket, resource, label, cwd, project` (git root's name), `session` (`CLAUDE_CODE_SESSION_ID`), `org`,
`pid, waited_s, held_s, exit`; rotated past `log_kb` (one old file kept).

**Dashboard**: `/api/slots?hours=12` (limits, holders, queue, minutes waited and held per resource per hour, recent
events; another organisation's tickets are shown without label, folder or session). The overview's "Queue waits" card
shows it, and a "queue wait" usage alert goes to the session of a ticket (of this org) waiting over `alert_wait_min`.

Known limits: only commands the hook sees are queued; a job started inside a script that no rule matches (or by a
tool other than Bash / PowerShell) is not. Make the script call `slot run` itself or add a rule for the script.

## Monitoring cost

Every model request is attributed to what started its **turn chain**: the requests from a trigger (human prompt, peer
message, task-notification) until the next one. A message queued during a turn counts from when the turn picked it up,
not from when it was queued. The trigger is the model interval's label: `human`, `peer`, `agent`,
`background-command`, `monitor:<name>`, `start`. For a Monitor event the name is the `MON:<name>` tag in the monitor's
description or its event lines (`MON:queue-watch queue27: ...`, `[MON:queue-watch] ...`); monitors without one are
`monitor:untagged`. The first request of a chain carries the `wake` flag, so a *wake* is one chain, and the cost of a
wake is every request in it. The overview's **Monitoring cost** card, `python -m devtime report` and `/api/overview`
(`monitoring`) give wakes, cost, average cost per wake and input / cache read / cache write / output tokens by monitor
and by main session; `/api/watch` has a short version (`monitoring`: totals, top tags and sessions). To tag a monitor,
start its description with `MON:<name>` and prefix its output lines with `[MON:<name>] ` (`| sed -u 's/^/[MON:name] /'`).
Tests: `python -m unittest discover -s tests`.

## Weekly limit and throughput

The overview's **Weekly limit** chart shows the account's usage since the plan's last weekly reset as a share of the
weekly allowance, the limit itself, and two projections to the next reset: at the pace since the weekly reset and at
the pace since the current 5-hour session window opened (a dot marks where each would hit the limit). The share is
calibrated against the Claude desktop app's own usage samples (`plan_usage_history`, which holds only the 5-hour and
weekly percentages): since the last weekly drop, reported share = a + cost / allowance is fitted, giving the allowance
in API-equivalent dollars; the samples are drawn as dots. The next reset comes from the readings that carry one, else from `weekly_reset`; The allowance is fitted from the readings: this week's, else the last five weeks' (one intercept per week), else spend over the latest reading's share; `weekly_budget_usd` (not set by default) is used only when there is nothing to fit, and bounds a fit to 0.3-3x when set. 5-hour windows are
chained from request times and re-anchored where the reported 5-hour share drops, so they are approximate.
Exact readings also come from the `get_usage` calls that sessions already make: the dashboard picks them out of the Claude
Code transcripts (main and subagent, last 9 days; only the percentages and reset times are kept, cached in
`~/.claude/devtime/cache/usage_harvest.json`), so no scheduled polling is required.
**Throughput** shows commits and finished subagents per bucket for the selected range.

**Shares instead of dollars.** `limits.allowance` (also `limits.budget`) in `/api/overview` is the fitted API-equivalent
dollars per 100 % of the week, `limits.allowanceError` its uncertainty (one standard error, dollars). Wherever an allowance
exists the dashboard, the band and the pane show costs as "% of week" (cost / allowance): per session, agent, work section,
spender, live burner, cost per hour (%/h), cost per commit (% per commit), the cost chart; dollars stay in tooltips
(`title`). Without an allowance (no readings and no `weekly_budget_usd`) everything is in dollars.

**Fit quality.** `limits.fit` holds `samples`, `allowance`, `residual` (largest miss, this-week fits), `sigma` (the residual
standard error of the reported share around the fitted line, 0-1) and `allowanceError` (the standard error of the fitted
slope turned into dollars: `se(k) / k^2` for `allowance = 1 / k`); a one-reading estimate has neither. The weekly-limit
card prints "allowance ~$2,600 ± 200". `limits.fit.byFamily` (with `byFamilySamples`) is only present when the data
separates the model families: the same samples are refitted with opus / sonnet / haiku / other cost as separate regressors
(each week its own intercept), `{family: {allowance, error, share of the window's cost}}`. Required: at least 10 samples,
at least two families with 10 % of the window's cost each (smaller families' cost counts with the largest), regressors that
are not collinear, and every coefficient positive. It is for reading only: it shows whether the API prices match the plan's
weighting (equal allowances = they do), and the displayed shares still use the single allowance. The math is in
`devtime/fit.py`.

The lane under the chart lists the week's large pieces of work. Workstreams (the rules in your project profiles) are
coarse, so when work **sections** exist they are used instead: the `devtime:labeller` agent (started by the `/devtime:start` loop) runs
`python -m devtime work-items` (JSON: the existing section names with their stats, and unlabelled subagents with
their descriptions, plus main sessions with no subagents that cost $3 or more; oldest first, `--days 8 --max 250`),
names a section for each, and passes `{"<transcript id>": "<section name>"}` ("-" = not a piece of work, don't ask
again) to `python -m devtime work-assign FILE` (`-` reads stdin), which merges it into `~/.claude/devtime/cache/work_sections.json`. Only ids and section
names are stored. A section is drawn from the start of its first agent to 95 % of its cost (to now while an agent is
running) if it cost at least 0.4 % of the allowance or has 4 agents; at most 12 are shown. With nothing labelled in
the week the lane falls back to the workstreams.

## Usage alerts

After every rebuild the server checks live transcripts (`devtime/alerts.py`, thresholds in `[settings.alerts]`): a
subagent past $40 (then each doubling), re-writing its cache after waits over 5 min, working with over 300k context,
or any transcript spending over its 30-minute budget; plus all sessions together per hour. Alerts go to
`~/.claude/devtime/cache/alerts.json`, the dashboard's Usage alerts card and `/api/alerts`; `hooks/agent_budget.py` passes each
new one once to the subagent it is about and to its main session (on their next tool call), so the owner can stop,
re-brief or take over the work. The dashboard server has to be running for alerts to be raised. A user-wide
SessionStart hook (`hooks/ensure_server.py`) starts it, detached and windowless, whenever a Claude session opens and
nothing of yours answers on port 8765 (see Organisations and ports; log in `~/.claude/devtime/cache/server.log`).

Three more rules go to the main session only, since only it can act on them: a non-Haiku subagent doing read-only
work (many calls, no edits, not a planner, reviewer or implementer), a subagent writing code without an
`AB:<stage>` tag in a session running the model A/B, and a big-context main session checking on running jobs again
and again (hand the wait to a runner).

For judgement the rules can't encode, the `devtime:reviewer` agent (Haiku, every 2 h from the `/devtime:start` loop) reads
`python -m devtime review-context` (the `/api/watch` summary of the last hours - sessions, subagents with model, work
kinds, cost and commits, active alerts, the experiments; no prompts or labels - and the log tail), messages a session
when something is worth changing, and logs what it raised in `~/.claude/devtime/cache/review-log.md`.

A forked or copied session file repeats the history it was copied from; each API request is counted once, in the
copy whose session went on longest.

## Organisations and ports

One account can be signed into several organisations in the Claude desktop app; they all write to `~/.claude/projects` and
share `CLAUDE.md`, settings and plugins, and each has its own plan limits. devtime therefore runs one fully segregated
instance per organisation.

**Which org is "current".** `devtime/paths.py` picks it when the process starts: env `CLAUDE_CODE_ORGANIZATION_UUID` if set
(Claude Code sets it for sessions and hooks), else `oauthAccount.organizationUuid` in `~/.claude.json`. `python -m devtime org`
prints the org, where it came from, its name, the instance folder, the port and whether its server answers. With no org
(neither source) commands that need data print `{"refused": true, "error": ...}` and exit 2; `/devtime:start` stops then.

**Per org** (`<data dir>/orgs/<org uuid>/`): the cache (`cache_dir`: transcript caches, alerts, plan readings, work sections,
review notes and log, `port.txt`, `server.log`), its own server and port, its own dashboard, alerts, plan readings and pace.
**Shared** (in the data dir, `~/.claude/devtime`): `config/` (settings, project profiles, `experiments.toml`, `changes.toml`
and the optimization ledger `installed.json`), `logs/`, and the stable launcher `devtime.py` with `plugin_root.txt`. A transcript's org is, in this
order: its own `credential_org` record (`{"type": "attachment", "attachment": {"type": "credential_org", "organizationUuid":
...}}`), the desktop app's session index (`<desktop dir>/claude-code-sessions/<account>/<org>/local_*.json` and
`local-agent-mode-sessions/...`, matched by `cliSessionId`), its parent session's org (subagents), else `unknown`
(`devtime/orgs.py`). The desktop dir is `desktop_dir` in `[settings]` or env `DEVTIME_DESKTOP_DIR`, else the app's folder for the
OS (`%APPDATA%\Claude`, `~/Library/Application Support/Claude`, `~/.config/Claude`). An instance only reads transcripts of its
own org; `include_unknown_org = true` in `[settings]` also counts the `unknown` ones (default false, so they are left out).

**The server refuses other orgs.** An API request that names a session or org that is not this instance's gets 403
(`/api/version` is always answered and returns `home`, `org` and `orgName`; the dashboard shows `orgName` as a pill). There is
no org selector, `org=` parameter or org listing endpoint, and the CLI has no `--org` / `--session` options. The hooks
post to and read only their own org's verified server, and alerts reach only sessions of that org. To track another org, run
`/devtime:start` in a session of that org: it starts that org's instance on its own port.

**Migration.** The pre-0.4.0 cache (`<data dir>/cache`, shared by all orgs) is split once by `devtime/migrate.py`, run by
`devtime tick` and `devtime serve`: what cannot be rebuilt (plan readings, review notes, work sections, review log and state)
moves to the instance of the org it belongs to, by each row's org or its transcript's org; rows with no org are only moved on a
single-org machine. Everything rebuildable is dropped and the old folder is removed.

**Ports.** `/api/version` returns `home`, the first 12 hex characters of the SHA-1 of the instance folder, and a server only
counts as yours if that matches (other orgs, and other Windows users on one PC, never see each other's dashboard).
`settings.port` (default 8765) is kept when it is yours or free; if another instance holds it, the next free port of
port+1..port+20 is used and recorded in `<instance>/cache/port.txt`, which setup, tick, the hooks and the server read
(`devtime/ports.py`).

## Projects, rules and workstreams

The plugin's `config/devtime.toml` holds settings, model pricing, built-in tool kinds and the shared classification
rules. To change a setting, add it to `~/.claude/devtime/config/devtime.toml` (tables are merged key by key; your
`[[rules]]` come first). `default_project` there sets the project the dashboard opens on; without it, the project
with the most cost in the last 7 days. Each `~/.claude/devtime/config/projects/<name>.toml` is a project profile
(`config/projects/` of the plugin is read too, if present). `examples/projects/example.toml` is an example of a profile
with custom rules (a game-engine build / run, workstreams, optional `[unreal]` log harvesting); copy it there and adapt it.
Change marks (`python -m devtime mark "what changed"`) are kept in `~/.claude/devtime/config/changes.toml` and drawn on the weekly chart.

- `paths` - globs on the session's working directory (`cwd` of its records, or the desktop app's origin directory).
  This is how sessions are found, wherever Claude Code filed them: a profile can pick up a main checkout, its
  `.claude/worktrees/*` and sibling checkouts. `~` is not expanded in `paths`; start a glob with `*` instead. New sessions are
  picked up on the next scan. Sessions no profile claims are named after their repository folder (the last component of their directory, with a
  trailing `.claude/worktrees/<name>` removed), so every repo gets a project without any configuration.
- `rules` - regexes on a shell call's description + command (or a tool name) giving its kind: game run,
  game build, blender, ... First match wins; project rules run before the shared ones.
- `workstreams` - regexes on the subagent description, session title, cwd or git branch (in that priority); work
  no rule claims falls back to its branch name, then its session title.

Config edits are picked up live, without restarting the server.

## Privacy

Only timings, tool names and short labels (shell descriptions, file basenames, subagent descriptions) reach the
dashboard. Prompts, tool outputs and file contents are never stored or served; the scan cache keeps shell commands
locally (gitignored) only so the rules can be re-applied. The server binds to 127.0.0.1.

## Layout

    devtime/scan.py      incremental transcript reader -> compact events, discovery, desktop-app titles
    devtime/analyze.py   events -> intervals, overlap sweep, gaps, tokens/cost, per-project slices
    devtime/store.py     rescans + rebuilds when transcripts or config change; per-org slices
    devtime/orgs.py      which organisation a transcript belongs to; desktop session index
    devtime/ports.py     the port of this data folder's server (port.txt)
    devtime/server.py    local HTTP server (/api/projects, /api/data, /api/version, POST /api/reading, static/)
    devtime/readings.py  plan readings posted by sessions: validation, dedupe, queue; devtime/fit.py allowance regressions
    devtime/unreal.py    optional Unreal Editor log harvester: phases, activities, script steps, archive
    devtime/report.py    text summary
    devtime/ab.py        A/B experiments: arms by hash, CLAUDE.md block, report (hooks/agent_budget.py enforces)
    devtime/slots.py     machine-wide slot queue: tickets, OS locks, Job Objects, status / kill / log, /api/slots
    devtime/slotrules.py slots.toml, command rules, wait-loop detection, the hook's rewrite (light: the hook imports it)
    static/              the dashboard (vanilla JS, no dependencies)
    config/              default settings, pricing, rules
    examples/            example project profile
    hooks/ agents/ skills/ .claude-plugin/   the plugin: hooks, slim agents, skills, manifest + marketplace
