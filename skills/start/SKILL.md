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
2. If `claudeMd.block` is false: ask both usage caps in ONE AskUserQuestion call with two questions, each with the
   options "100 %", "95 %", "85 %", "70 %" (the widget adds a custom answer): "At what share of your WEEKLY limit
   should sessions stop starting new agents?" (recommend 85 %) and "At what share of the current 5-HOUR window should
   sessions pause new agents until it resets?" (recommend 95 %). Run
   `python -m devtime setup claude-md --cap W --five-hour-cap F --dry-run`, show the diff and, on yes, run it without
   `--dry-run`. Re-running this command later changes the caps.
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
7. First run only, last: invoke the `devtime:optimize` skill (Skill tool; `optimize` if the scoped name is not
   found) to measure this machine's usage and offer the optimizations; follow it through to the end.

## The pass

1. If the tool `mcp__ccd_session_mgmt__get_usage` exists, call it (load it with ToolSearch query
   `select:mcp__ccd_session_mgmt__get_usage` if it is deferred; skip silently if unavailable). The dashboard picks the
   reading up from the transcript; ignore its output.
2. Run `python -m devtime tick` (JSON: url, reading, labels, review, alerts, cap, setup).
3. If `labels` > 0, launch the agent `devtime:labeller` in the foreground. If `review.due` is true, launch
   `devtime:reviewer` likewise. If the scoped name is not found, try `labeller` / `reviewer`. Prompt: "Plugin root:
   ${CLAUDE_PLUGIN_ROOT}; run commands from there."
4. Reply with ONE line: weekly % (from `reading`, or "no reading"), labels done, notes left, URL.
