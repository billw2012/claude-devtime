"""Optimisation findings from Unreal runs: the per-run issues (devtime.unreal.detect_issues) grouped by script and
pattern, ranked by the time they cost, each with what to change. Shared by the CLI (python -m devtime findings), the
server (/api/findings) and so by anything that wants to check a run right after it finished (a hook, an agent).
"""

import collections
import time

from .unreal import ISSUE_KINDS

ADVICE = {
    "throttled": "launch the editor with -unattended (a background editor is capped at 3 fps; every frame-counted "
                 "wait and procedural generation's per-frame budget run ~25x slower)",
    "fixed wait": "wait for the world instead of a fixed time: poll a busy / settled state (or count frames) "
                  "rather than sleeping for N seconds",
    "python stall": "the script computes for this long inside one tick: cache what it re-reads through Unreal, cut "
                    "the candidates by distance / bounding box first, avoid O(n^2) loops in Python",
    "check timeout": "fail the check as soon as the world has settled and it is still false, instead of at its timeout",
    "slow test": "split it into per-scenario tests, keep benchmarks out of the default filter, run only the "
                 "affected tests while iterating (Automation RunTests <Area>) and the full suite once per stage",
    "slow startup": "if it is 'waiting for UnrealBuildTool': editor startup waits for any C++ build on the same engine "
                    "(UBT is one instance per engine), so don't build in one checkout while editors start in others; "
                    "fewer parallel checkouts, builds queued; otherwise keep the DDC / Zen server warm",
}


def run_matches(run, project=None, script=None, since=None):
    if project and run.get("project") != project:
        return False
    if script and script not in (run.get("scripts") or []):
        return False
    return not (since and run["end"] < since)


def findings(runs, project=None, script=None, since=None, top=20):
    """[{kind, title, advice, script, label, runs, seconds, count, last}] sorted by seconds."""
    groups = {}
    for run in runs:
        if not run_matches(run, project, script, since):
            continue
        name = " + ".join((run.get("scripts") or [])[:2]) or "(no script)"
        for kind, seconds, label in run.get("issues", []):
            key = (kind, name, label if kind != "throttled" else "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"kind": kind, "title": ISSUE_KINDS.get(kind, kind), "advice": ADVICE.get(kind, ""),
                                   "script": name, "label": label if kind != "throttled" else "", "runs": set(),
                                   "seconds": 0.0, "count": 0, "last": 0.0}
            g["runs"].add(run["start"])
            g["seconds"] += seconds
            g["count"] += 1
            g["last"] = max(g["last"], run["end"])
    out = []
    for g in groups.values():
        g["runs"] = len(g["runs"])
        g["seconds"] = round(g["seconds"], 1)
        out.append(g)
    out.sort(key=lambda g: -g["seconds"])
    return out[:top] if top else out


def latest_run(runs, project=None, script=None):
    candidates = [r for r in runs if run_matches(r, project, script)]
    return max(candidates, key=lambda r: r["end"]) if candidates else None


def fmt(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def print_findings(items, header):
    print(header)
    if not items:
        print("  nothing flagged")
        return
    for g in items:
        label = f": {g['label']}" if g["label"] else ""
        print(f"  {fmt(g['seconds']):>7s}  {g['script']}  {g['title']} ({g['count']}x in {g['runs']} run"
              f"{'s' if g['runs'] != 1 else ''}){label}")
    print("\nWhat to change:")
    for kind in dict.fromkeys(g["kind"] for g in items):
        print(f"  - {ISSUE_KINDS.get(kind, kind)}: {ADVICE.get(kind, '')}")


def print_run(run):
    """One run, as a hook or an agent would want it right after the run finished."""
    duration = run["end"] - run["start"]
    phases = collections.Counter()
    for s, e, p, _ in run["phases"]:
        phases[p] += e - s
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run["start"]))
    fps = f", {run['fps']:.0f} fps" if run.get("fps") else ""
    print(f"Unreal run {' + '.join(run.get('scripts') or ['(no script)'])} at {when}: {fmt(duration)}{fps}, exit "
          f"{run['exit']}; " + ", ".join(f"{p} {fmt(v)}" for p, v in phases.most_common()))
    slow = sorted(run["steps"], key=lambda s: s[0] - s[1])[:5]
    if slow:
        print("  slowest steps: " + "; ".join(f"{fmt(s[1] - s[0])} {s[3][:60]}" for s in slow))
    items = findings([run], top=10)
    if items:
        print_findings(items, "  flagged:")
