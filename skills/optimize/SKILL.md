---
name: optimize
description: Measure the user's Claude Code usage, recommend optimizations with estimated weekly savings (catalogue items plus new ideas) and apply the chosen ones to CLAUDE.md / settings.json. Use when the user runs /devtime:optimize or asks how to cut their Claude Code cost.
---

# devtime optimize

Run every command as `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime ...` (`python` 3.11+, or `python3`). If the
literal text `${CLAUDE_PLUGIN_ROOT}` was not substituted, the plugin root is two levels above this skill's base
directory. Keep replies short; do not read transcripts or other files.

## 1. Measure

1. `python -m devtime analyze --days 7` (JSON under 3 KB; the first scan can take a minute). `catalogue` is already
   ranked by `estWeeklyUsd` (an upper bound, "if every such request were fixed", scaled to a week; null = no $ estimate).
   For an applied item the estimate is what is still being lost. Other keys are the metrics behind it: `byModel`,
   `byRole`, `baseCtxByType` (fixed context per agent type), `big` (cost share above 250k / 400k context),
   `waiting` (subagent wait hours, cache re-writes), `readonlyBig`, `opusCoding`, `concurrent`, `recurringFailures`.
2. `python -m devtime optimize list` for each item's title and one-line reason.

## 2. Present

A short table, one row per catalogue item: item (title), applied? (`by`: devtime / manual / no), est. $/week, why.
Applied items last or greyed out. Then up to 3 **new ideas** the metrics suggest that are not in the catalogue, each
one line with its number, labelled "unproven" (examples: recurring failures worth a script or a CLAUDE.md note; one
model carrying most of the cost; a role or agent type with an outsized base context). Say nothing if nothing stands out.
If the plugin's `sonnet-default-coding` is listed, call it a suggestion to A/B, not a fix.

## 3. Choose

AskUserQuestion, multiSelect: the not-yet-applied catalogue items worth doing (largest estimate first, at most 4 per
question; use a second question for more). Then, per chosen item, ask: apply globally now, or test it first as an A/B
(tell the user to run `/devtime:ab <id>` afterwards for those; do not apply them here). Items whose only action is a
`note` (e.g. `weekly-cap`) have nothing to write: tell the user to run `/devtime:start`.

## 4. Apply

For each item chosen for global use: `python -m devtime optimize apply <id> --dry-run`, show the diff briefly (the
CLAUDE.md block or the settings.json `env` line; settings.json gets a `.bak`), and on yes run it without `--dry-run`.
A change marker "Applied <title>" is recorded automatically. `python -m devtime optimize remove <id> [--dry-run]`
undoes one and restores what `settings.json` held before (the install ledger `<data dir>/config/installed.json`
records it; a value the user changed since is left alone). `optimize remove all [--dry-run]` takes out every devtime
block, every recorded settings change and the `/devtime:start` block. If the user already wrote an item by hand
(`by: manual`) or has their own CLAUDE.md section they want removable, `optimize adopt <id> --heading "<heading text>"
[--dry-run]` wraps it in the tags (custom ids: lowercase letters, digits, dashes). `optimize list` shows each block's
state and what remove would do.
CLAUDE.md edits only reach sessions started afterwards; `settings.json` env changes need a restart of Claude Code.

## 5. Finish

One or two lines: what was applied. The dashboard's weekly chart marks each change, so run `/devtime:start` if it is
not open and compare the weeks. Re-run `/devtime:optimize` in a week to see the estimates move.
