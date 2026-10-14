---
name: ab
description: Run an A/B experiment on how Claude Code works (model, agent type, instructions) with arms assigned by hash, enforced by a hook, and measured on cost, output, failures and rework. Use when the user runs /devtime:ab, wants to test an optimization before adopting it, or asks which arm of an experiment is better.
---

# devtime ab

Run every command as `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime ab ...` (`python` 3.11+, or `python3`). If the
literal text `${CLAUDE_PLUGIN_ROOT}` was not substituted, the plugin root is two levels above this skill's base
directory. Keep replies short; do not read transcripts. Arguments: `<catalogue id or description>` to create,
`report <id>`, `stop <id>`, or nothing to list.

## Why it works this way
The author's own hand-run A/B was biased: the orchestrating session picked the model per stage (Opus got the hard
ones), fix-ups got new names so rework was missed, and cost ignored how much a stage produced. So here the arm is
`sha1(id:stage) mod arms` (never chosen by a session), a `PreToolUse` hook refuses an agent launch whose model / agent
type differs from its arm, fix-ups reuse the stage tag, and the report counts commits, edits, failures and rework.

## No argument: list
`python -m devtime ab list`; for each active one offer `report` or `stop`. For a new one, ask what to test.

## Create (`<catalogue id or description>`)
1. If it is a catalogue id, read its title with `python -m devtime optimize list`. Propose 2 arms (3 at most) and a
   one-line id. Example for `sonnet-default-coding`: `sonnet:sonnet:devtime:coder` vs `opus:opus:devtime:coder`
   (control = the current practice). Arms are `name[:model[:agent_type]]`; optional `--instructions name=short text`.
2. AskUserQuestion to confirm id, arms and what counts as a "coding stage of this kind" (the CLAUDE.md block says so).
3. `python -m devtime ab new <id> --title "..." --arm ... [--opt <catalogue id>] --dry-run`; show the diff briefly
   (a block `<!-- devtime:ab:<id> -->` in `~/.claude/CLAUDE.md`), then on yes run it without `--dry-run`.
4. Explain: from the next session on, before each coding stage a session runs `python <data dir>/devtime.py ab arm <id>
   <stage>` (a stable launcher written at every session start; the plugin path changes on update) and gets the arm as
   one line; it names the agent `AB:<id>:<stage> ...`; the hook enforces the arm. Existing sessions need a restart to
   read the new CLAUDE.md block. Run it until each arm has at least 10-15 stages (a few days of normal work); the
   dashboard's Experiments card shows progress.

## Report (`report <id>`)
`python -m devtime ab report <id>`; summarize in a few lines: per arm stages, median cost and minutes, commits,
edits, errors, rework, cost per commit. State the caveats it prints: "too few stages to call" (< 10 per arm) means
no conclusion yet; imbalance; stages that ran off-arm; rework is only what tags, sections and descriptions reveal.
Call a winner only when the gap is clear and both arms have enough stages; cheaper but more rework or fewer commits is
not cheaper. Do not read transcripts to "check".

## Stop (`stop <id>`)
`python -m devtime ab stop <id>` (marks it stopped, removes the CLAUDE.md block, prints the report). Summarize as
above. If the experiment tests a catalogue item (`--opt`) and its arm won on the evidence (not a small sample), offer
`python -m devtime optimize apply <opt> --dry-run`, then apply on yes; otherwise say the current practice stays.
