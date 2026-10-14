"""OPTIONAL, project-specific (not registered by the plugin). Claude Code PostToolUse hook: after a shell call that ran (or waited on) an Unreal Editor run, tell the agent what
that run wasted, from devtime's findings, so slow scripts get fixed while they are being worked on.

Add it yourself to ~/.claude/settings.json (user-wide; it only acts in projects with [unreal] log_globs) or a project's
.claude/settings.json, with the path of your devtime checkout / plugin install:

    {"hooks": {"PostToolUse": [{"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "timeout": 20,
        "command": "python <devtime dir>/hooks/unreal_findings.py"}]}]}}

Silent unless a run of this session's project finished since the session's previous report and something in it was
flagged (throttled to ~3 fps, repeated fixed-length waits, Python blocking the editor, checks failing at their timeout,
slow startup). Reads only the Unreal logs through the dashboard's cache (<data dir>/cache/unreal.pickle); never fails the call.
"""

import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# a session's first report covers runs that ended this long ago
FIRST_LOOK_BACK = float(os.environ.get("DEVTIME_HOOK_LOOKBACK", 1800))
MAX_RUNS = 3  # report at most the latest few runs per call
QUICK = re.compile(r"Unreal|\.uproject|ExecCmds|ExecutePythonScript|"
                   r"sleep|until|while|Wait-Process|Saved[/\\]Logs|\.log\b|\.output\b", re.IGNORECASE)


def relevant(run, session_id, text):
    """A run this call plausibly launched or waited on: its log is in this session's scratchpad, or the command names
    the log file or the checkout it ran in."""
    source = (run.get("source") or run.get("archive") or "").replace("\\", "/")
    if session_id and session_id in source:
        return True
    name = run.get("name") or ""
    if name and name.lower() in text.lower():
        return True
    checkout = run.get("checkout") or ""
    return bool(checkout and checkout != "scratchpad" and
                re.search(r"[/\\]" + re.escape(checkout) + r"(?=[/\\\"' ;]|$)", text, re.IGNORECASE))


def describe(run, findings_mod):
    items = findings_mod.findings([run], top=6)
    if not items:
        return None
    fmt = findings_mod.fmt
    scripts = " + ".join(run.get("scripts") or ["(no script)"])
    fps = f" at {run['fps']:.0f} fps" if run.get("fps") else ""
    head = (f"Unreal run {scripts} (ended {time.strftime('%H:%M', time.localtime(run['end']))}, "
            f"{fmt(run['end'] - run['start'])}{fps}) wasted time:")
    lines = [head]
    for g in items:
        detail = f" in '{g['label'][:90]}'" if g["label"] else ""
        lines.append(f"  - {fmt(g['seconds'])}: {g['title']} ({g['count']}x){detail}")
    return "\n".join(lines), {g["title"]: g["advice"] for g in items}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if data.get("tool_name") not in ("Bash", "PowerShell"):
        return
    inp = data.get("tool_input") or {}
    if inp.get("run_in_background"):
        return  # still running: a later call that waits on it reports it
    text = f"{inp.get('description', '')}\n{inp.get('command', '')}"
    # Installed user-wide, this runs after every shell call in every project: leave at once (before loading any
    # config) unless the call could have run or waited on an editor.
    if not QUICK.search(text):
        return
    cwd = data.get("cwd") or os.getcwd()
    sys.path.insert(0, ROOT)
    from devtime.config import Config
    config = Config()
    project = config.project_for(cwd)
    if project is None or not (project.unreal or {}).get("log_globs"):
        return
    unreal_cfg = config.unreal
    patterns = [unreal_cfg.get("launch"), unreal_cfg.get("wait"), r"Saved[/\\]Logs|\.log\b|tasks[/\\]\w+\.output"]
    if not any(p and re.search(p, text, re.IGNORECASE) for p in patterns):
        return
    from devtime import findings
    from devtime.unreal import Harvester
    harvester = Harvester(config)
    if not harvester.runs:
        return  # no cache yet (the dashboard builds it); never make the agent wait for a first harvest
    harvester.refresh(min_interval=0, rebuild=False)  # never the slow paths inside a hook

    state_path = os.path.join(config.path("cache_dir"), "hook_state.json")
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    session = data.get("session_id") or "?"
    now = time.time()
    since = state.get(session, now - FIRST_LOOK_BACK)
    runs = [r for r in harvester.runs.values() if r.get("project") == project.name and not r.get("live")
            and r["end"] > since and relevant(r, session, text + "\n" + cwd)]
    state[session] = now
    state = {k: v for k, v in state.items() if now - v < 3 * 86400}
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, state_path)

    reports = [d for d in (describe(r, findings) for r in sorted(runs, key=lambda r: r["end"])[-MAX_RUNS:]) if d]
    if not reports:
        return
    fixes = {}
    for _, advice in reports:
        fixes.update(advice)
    message = ("devtime (automatic, from the Unreal logs): " + "\n".join(text for text, _ in reports) +
               "\nHow to fix:\n" + "\n".join(f"  - {title}: {advice}" for title, advice in fixes.items()) +
               f"\nWorth fixing in the script while you are on it. More: "
               f"`python -m devtime findings --latest` (cwd {ROOT}).")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a hook must never break the session's shell calls
        pass
