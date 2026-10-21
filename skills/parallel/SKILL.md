---
name: parallel
description: Manage devtime's optional parallel support - turn it on or off, see what it adds (the machine-wide slot queue for heavy jobs and the agent budget rules), review and adjust the slot rules for the current project, explain `slot status`, and explain agent budget refusals and hand-off nudges. Use when the user runs /devtime:parallel, plans to run several sessions or autonomous pipelines in parallel, wants sessions to share the GPU / editor / builds, asks what is queued or holding a slot, or asks to turn devtime parallel support on or off.
---

# devtime parallel

Run every command as `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime ...`, or `python "<data dir>/devtime.py" slot ...`
for the `slot` commands, with the launcher path from `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime org` (`dataDir` +
`/devtime.py`; usually `~/.claude/devtime/devtime.py`). If the literal text `${CLAUDE_PLUGIN_ROOT}` was not
substituted, the plugin root is two levels above this skill's base directory. Keep replies short.

## First: is parallel support on?
Run `python -m devtime setup parallel` (JSON: `parallel`, and `features` = what is effectively on).
Parallel support is an optional add-on that only adds to devtime's always-on base set (dashboard, alerts,
optimizations, A/B): the **slot queue** (`slots`) and the **agent budget rules** (`agent_budget`). Both also act in
interactive sessions on this PC: heavy commands there get queued (interactive editors are exempt), and subagents are
refused sleeps / poll loops / long blocking waits (each refusal says to hand the job back to the main session and
end) and are asked to hand off past 250k tokens of context.

- Args `on` / `off`, or the user asks to turn it on or off: say in one sentence what changes, confirm with the user,
  then `python -m devtime setup parallel on` (or `off`). It takes effect from the next tool call; tell the user a
  single feature can be forced with `features.slots` / `features.agent_budget` under `[settings.features]` in
  `~/.claude/devtime/config/devtime.toml`. Then stop (after `on`, offer the project review below).
- If `features.slots` is false: say parallel support (or slots) is off and offer `setup parallel on` (or
  `features.slots = true`) **before anything else**; without it the rules below do nothing. Review the rules only if
  the user wants them prepared anyway.
- Args `agent budget` / the user asks about a refusal or hand-off nudge: explain the hook (limits 4 min per wait,
  10 min of waits per subagent, nudge at 250k context; the `runner` agent type is exempt; main sessions are never
  refused) and what to do instead (background the job, let the main session or a `devtime:runner` wait). Then stop.

## What it is (tell the user in two or three sentences, first run only)
One FIFO queue per resource (default `gpu`=2, `test`=3, `build`=2 at once; group `quiet` = test+gpu exclusively),
shared by every session, project and organisation on the PC. The plugin's hook rewrites a command whose program
matches a rule into `slot run <resource> -- <command>`: it waits its turn, runs, and frees the slot when it exits,
however it exits (locks held by the live process; on Windows the job dies with its wrapper). Rules live in
`~/.claude/devtime/config/slots.toml` (machine-wide); the shipped defaults cover UnrealEditor(-Cmd), Build.bat /
RunUAT / ue-parallel-build and background Blender. Rules for a project's own scripts are **learned automatically**:
the devtime server's watcher sees a heavy program (UnrealEditor, `blender -b`, UBT) started unqueued by a Claude Code
shell, finds the project script that started it and writes a learned rule (`~/.claude/devtime/config/
slots_learned.toml`; the job's first run goes unqueued; timing runs - perf / measure / benchmark ... in the script
name or its arguments - are queued `quiet`). Nobody has to run this skill; it is for reviewing and adjusting.

## Args `status`: explain the queue
Run `slot status`: one line per resource, `used/limit`, `held:` tickets (`#id 'label' (project) for`) and `queue:`
(oldest first, `excl` = wants every slot). `slot log --since 2h` shows recent acquire / release / timeout / kill.
A stuck holder (crashed tool, forgotten run): `slot kill <id>` (it ends within seconds). Then stop.

## Otherwise: review this project
1. `slot rules` (rules in force; `[learned ...]` ones were learned, with how often they were seen) and `slot status`.
   `slot log --since 7d` shows `learned` and `unlearnable` events (unlearnable: the chain of process names, e.g. an
   inline loop or `make` that starts the editor; those need a hand rule or a script).
   A wrong learned rule: add a user rule for the same script (user rules win; `resource = "none"` exempts it) or
   `slot remove-rules --learned --project P`. Learning off: `learn = false` under `[settings]`.
2. Find what launches heavy jobs in this project, quickly (Grep / Glob, no deep reading): scripts under Tools/,
   scripts/, bin/, Makefile / package.json / pyproject targets, *.sh / *.ps1 / *.bat / *.py that start an editor or
   game binary (UnrealEditor, Unity, Godot, blender -b), GPU captures or renders, headless or automation test runs,
   full builds, long test suites. Also look for hand-rolled locks: mkdir/flock/lock files, `tasklist` / `pgrep` wait
   loops, "wait for the other session" notes in CLAUDE.md.
3. For each candidate, decide the resource (`gpu` for captures / renders, `test` for editor or headless test runs,
   `build` for builds; `quiet` only when it must have the machine to itself, e.g. timings) and a regex for how a
   session invokes it (the script's path or name, e.g. `Tools/Dev/capture_.*\.sh`). Check each with
   `slot test "<a typical command line>"` (prints the matched resources, or null). Skip what a default or learned rule
   already covers, and scripts that start a watched program (they are learned on their first run).
4. Ask the user with ONE AskUserQuestion, `multiSelect: true`, one option per proposed rule (label: the script, its
   resource; description: the regex and why). Do not add "None" or "All" options (the widget has its own way out).
5. For each chosen rule: `slot add-rule --project "<project name>" --match '<regex>' --resource <res>` (the regex
   in single quotes: in double quotes bash halves its backslashes; add `--cwd '<regex of this project's folder>'` if the pattern is generic enough to hit other projects; `--max-hold MIN`
   for jobs that legitimately run past 45 min). Rules are tagged with the project: `slot rules --project X` lists
   them, `slot remove-rules --project X` removes them.
6. If the project has hand-rolled locks or wait loops that the new rules now cover, tell the session (and propose
   the CLAUDE.md edit, shown as a diff, applied only on yes) to stop using them: heavy jobs are queued by the hook,
   loops that wait for another process or a lock file are refused, and a script can call
   `python ~/.claude/devtime/devtime.py slot run <res> -- <cmd>` itself for jobs it starts internally.
7. One line: rules added, and that `DEVTIME_SLOTS=0` (in the env or in a command) runs something unqueued.
