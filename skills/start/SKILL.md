---
name: start
description: Start or resume the devtime usage dashboard - first-run setup, open the dashboard, then keep it updated every 30 minutes. Use when the user runs /devtime:start or asks to start, set up or resume devtime.
---

# devtime start

Run every command as `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime ...` (`python` 3.11+, or `python3`). If the
literal text `${CLAUDE_PLUGIN_ROOT}` was not substituted, the plugin root is two levels above this skill's base
directory (shown when the skill loads). Keep replies short; do not read other files or explain.

If the args are exactly `tick`, do only the **pass** below (no setup, no browser, no new loop).

## Otherwise: setup, a first pass, then the loop

1. Run `python -m devtime setup check` (JSON; the first scan can take a minute). If `pythonOk` is false, say Python
   3.11+ is needed and stop.
2. If `claudeMd.block` is false: ask the weekly reserve and the 5-hour cap in ONE AskUserQuestion call with two
   questions. First (header "Reserve"): "How much of your weekly limit should Claude keep in reserve?", description "A
   margin so the week never runs dry by accident; it shrinks as the reset gets closer.", options "5% per day until the
   reset (Recommended)" (65% usable with a week left, 85% with 3 days, 95% on the last day), "10% per day" (30%, 70%,
   90%) and "2% per day" (86%, 94%, 98%); a fixed cap comes through the automatic Other (a number W, in percent).
   Second: "At what share of the current 5-HOUR window should sessions pause new agents until it resets?" with options
   "100 %", "95 %", "85 %", "70 %" (recommend 95 %). Run
   `python -m devtime setup claude-md --reserve-per-day N --five-hour-cap F --dry-run` (`--cap W` instead of
   `--reserve-per-day N` for a fixed cap), show the diff and, on yes, run it without `--dry-run`. Re-running this
   command later changes the reserve or caps. (`claudeMd.weekly` of `setup check` says which form an existing block
   has: `reserve` or `cap`; `legacy: true` is the pre-0.8 wording, which still works and is refreshed by re-running.)
3. If `settings.duplicateHooks` is not empty (hook entries in settings.json that repeat the plugin's own), show them
   (event, command) and offer to remove them. Only on yes: copy settings.json to settings.json.bak, remove exactly
   those entries (and any group or event left empty), keep everything else, check it still parses. If
   `settings.error` is set, do not edit the file.
4. Run `python -m devtime setup open` (starts the server, opens the browser). If it says the server is not
   answering, show the log path it names and stop.
5. Do the **pass** once (the catch-up).
6. First run only (this session has no devtime loop yet): start the loop by invoking the `loop` skill with args
   `30m /devtime:start tick`. Tell the user the loop lives in this session (keep it open; a cheap model such as
   Sonnet or Haiku is fine for it).
7. First run only, before the optimize step: one AskUserQuestion, "Also set up support for running several sessions in
   parallel?", options "No, just the basics (Recommended)" and "Yes, add parallel support" (description: for
   autonomous pipelines or several sessions sharing the GPU, editor and builds; adds the machine-wide slot queue and
   agent budget rules, which also apply to your interactive sessions on this PC: heavy commands get queued,
   interactive editors are exempt; subagents are stopped from waiting on slow jobs and asked to hand off past 250k
   context). Run `python -m devtime setup parallel on` for yes, `... off` for no, and for yes mention that
   `/devtime:parallel` adopts the queue in a project and turns the support off again. (`setup check` shows the current
   `parallel` state; if it is already on, say so instead of asking.)
8. First run only, last: invoke the `devtime:optimize` skill (Skill tool; `optimize` if the scoped name is not
   found) to measure this machine's usage and offer the optimizations; follow it through to the end.

## The pass

1. Plan readings normally arrive by themselves: the plugin's `session.measure` hook posts every session's 5-hour and
   weekly usage to the dashboard (`/api/reading`). Calling `get_usage` is only a fallback for when that hook is
   unavailable (an older Claude Code build, or no readings in the last hour in the previous pass's `reading`): then,
   if the tool `mcp__ccd_session_mgmt__get_usage` exists, call it (load it with ToolSearch query
   `select:mcp__ccd_session_mgmt__get_usage` if it is deferred; skip silently if unavailable). The dashboard picks the
   reading up from the transcript; ignore its output. Otherwise skip this step.
2. Run `python -m devtime tick` (JSON: url, org, reading, labels, review, alerts, cap, weekly, weeklyUsable, setup). Each organisation on
   this PC has its own devtime (own data, server and port), and the pass works on the organisation this session is
   signed into. If the JSON says `"refused": true` (no organisation could be determined), tell the user why (its
   `error`), do not start or continue the loop, and stop. Mention once that every other organisation on the PC needs
   `/devtime:start` run in a session of that organisation to be tracked.
3. If `labels` > 0, launch the agent `devtime:labeller` in the foreground. If `review.due` is true, launch
   `devtime:reviewer` likewise. If the scoped name is not found, try `labeller` / `reviewer`. Prompt: "Plugin root:
   ${CLAUDE_PLUGIN_ROOT}; run commands from there."
4. Reply with ONE line: weekly % (from `reading`, or "no reading"; add "usable N %" from `weeklyUsable` when set), labels done, notes left, URL.
