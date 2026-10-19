---
name: slots
description: Review and adjust the machine-wide slot queue (devtime slots) for the current project - rules for its scripts are learned automatically; this optionally reviews the learned ones, adds rules learning can't see (jobs that start no watched program, long test suites), and retires hand-rolled locks. Also explains `slot status`. Use when the user runs /devtime:slots, asks how sessions should share the GPU / editor / builds, or asks what is queued or holding a slot.
---

# devtime slots

Run every command as `python "<data dir>/devtime.py" slot ...` with the launcher path from
`cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime org` (`dataDir` + `/devtime.py`; usually `~/.claude/devtime/devtime.py`).
If the literal text `${CLAUDE_PLUGIN_ROOT}` was not substituted, the plugin root is two levels above this skill's base
directory. Keep replies short.

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
