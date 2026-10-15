---
name: optimize
description: Measure the user's Claude Code usage, recommend optimizations with estimated weekly savings (catalogue items plus new ideas), apply the chosen ones to CLAUDE.md / settings.json, and show or remove what devtime installed. Use when the user runs /devtime:optimize, asks how to cut their Claude Code cost, or asks what devtime changed or to remove / undo some or all of it.
---

# devtime optimize

Run every command as `cd "${CLAUDE_PLUGIN_ROOT}" && python -m devtime ...` (`python` 3.11+, or `python3`). If the
literal text `${CLAUDE_PLUGIN_ROOT}` was not substituted, the plugin root is two levels above this skill's base
directory. Keep replies short; do not read transcripts or other files.

The user never runs these commands: you run them and talk in plain words (item titles, what changes in which file),
not command lines or ids. Every change is tagged so it can be taken out again: CLAUDE.md blocks sit between
`<!-- devtime:opt:<id> -->` markers, and settings.json changes are recorded with their prior value in the install
ledger (`<data dir>/config/installed.json`), so removing one restores what was there (a value the user changed since
is left alone).

If the user asks what devtime installed, or to remove / undo some or all of it, skip to **Remove**.

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
question; use a second question for more). The options are the items only: no "None", "Skip" or "All" option (ticking
nothing means none; the widget's own custom answer covers anything else). Then, per chosen item, ask: apply globally now, or test it first as an A/B
(tell the user to run `/devtime:ab <id>` afterwards for those; do not apply them here). Items whose only action is a
`note` (e.g. `weekly-cap`) have nothing to write: tell the user to run `/devtime:start`.

## 4. Apply

For each item chosen for global use: `python -m devtime optimize apply <id> --dry-run`, show the diff briefly (the
CLAUDE.md block or the settings.json `env` line; settings.json gets a `.bak`), and on yes run it without `--dry-run`.
A change marker "Applied <title>" is recorded automatically.
CLAUDE.md edits only reach sessions started afterwards; `settings.json` env changes need a restart of Claude Code.

**Make hand-written rules removable.** If `optimize list` shows items as `manual` (the user's CLAUDE.md already says
it in its own words) or the user set a catalogue setting by hand (`env.X ... no install record`), offer once, in one
question, to tag them so they can be removed later like the rest:
`optimize adopt <id> --heading "<the section's heading text>" --dry-run` for a CLAUDE.md section (one id per section;
for a section covering several items use a short custom id such as `token-efficiency`), and
`optimize adopt <id> --setting env.<KEY> --dry-run` for a setting. Show what gets tagged (the text itself is not
changed), then run them without `--dry-run` on yes.

## 5. Finish

One or two lines: what was applied. The dashboard's weekly chart marks each change, so run `/devtime:start` if it is
not open and compare the weeks. Re-run `/devtime:optimize` in a week to see the estimates move.

## Remove

1. `python -m devtime optimize list`: each item's state (`devtime` = installed and tagged, `adopted`, `manual` = the
   user's own text, not removable until adopted) and a `remove:` line saying exactly what removing it would do.
2. Show the installed ones as a short list (title, where: CLAUDE.md / settings.json) and ask which to remove
   (AskUserQuestion, multiSelect, the items only: no "None" or "All" option; if every item is ticked, use
   `remove all`).
3. One item: `optimize remove <id> --dry-run`; all: `optimize remove all --dry-run` (every devtime block, every
   recorded setting and the Usage tracking block from /devtime:start). Show the diff briefly; run without `--dry-run`
   on yes. Report each `report` line in plain words (removed, restored to the earlier value, left alone because
   changed since). A change marker is recorded for the dashboard.

