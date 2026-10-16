"""Unreal Editor logs: harvested as they appear, split into phases and activities, archived (gzipped, locally).

An editor run's log has UTC-timestamped lines ("[2026.10.07-02.30.01:123][ 12]LogPython: ..."). Each gap between two
lines is attributed to

- a phase, from a small state machine: startup (until the engine is initialised), editor idle, map load, script
  (from the first Python line), shutdown (from the exit request);
- an activity, from the category / message of the line that ends the gap (the work that just finished): Zen DDC,
  SDK detection, shader compile, procedural generation, Python, saving, ... (rules in config/devtime.toml [unreal]).

Lines a script logs itself (LogPython "[Tag] message") also give script steps: the time since the previous Python
line, labelled with the message normalised (numbers -> #), so the slow checks of a capture script stand out.

Unreal and Claude Code both delete these logs (purged backups, overwritten names, cleaned scratchpads), so every run
is summarised as soon as its log is seen and the raw log is archived to <data dir>/logs (shared by every org); the
summaries are cached in the org instance's cache/unreal.pickle and re-derived from the archive when the rules change.
"""

import glob
import gzip
import hashlib
import os
import pickle
import re
import time

from . import paths

LINE = re.compile(r"^\[(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d):(\d{3})\]\[\s*(\d+)\](\w+):\s?(.*)")
CMDLINE = re.compile(r"LogInit: Command Line:\s*(.*)")
SCRIPT_RE = re.compile(r"([\w.-]+\.py)\b")
TEST_STARTED = re.compile(r"Test Started\. Name=\{([^}]*)\} Path=\{([^}]*)\}")
TEST_DONE = re.compile(r"Test Completed\. Result=\{(\w+)\} Name=\{([^}]*)\} Path=\{([^}]*)\}")
PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][\w.-]+){2,}[/\\]?")
CACHE_VERSION = 11


def _epoch(y, mo, d, hh, mi, ss, ms):
    # Unreal log timestamps are UTC.
    import calendar
    return calendar.timegm((int(y), int(mo), int(d), int(hh), int(mi), int(ss), 0, 0, 0)) + int(ms) / 1000.0


class Rules:
    def __init__(self, spec):
        spec = spec or {}
        flags = re.IGNORECASE
        self.startup_end = re.compile(spec.get("startup_end", r"Engine is initialized|Total Editor Startup Time|Starting Game\."), flags)
        self.map_start = re.compile(spec.get("map_start", r"^Cmd: MAP LOAD|LoadMap: |Loading map '|Bringing World .* up for play"), flags)
        self.map_end = re.compile(spec.get("map_end", r"Map check complete|Took .* seconds to LoadMap|LoadMap took|Finished loading map"), flags)
        self.script_start = re.compile(spec.get("script_start", r"^LogPython|^Cmd: py |ExecutePythonScript"), flags)
        self.shutdown = re.compile(spec.get("shutdown", r"^LogExit|RequestExit|Engine exit requested|BeginExitIfRequested|^LogCore: .*[Ee]xit"), flags)
        self.crash = re.compile(spec.get("crash", r"Fatal error|=== Critical error ===|Unhandled Exception|Assertion failed"), flags)
        self.activities = [(a["name"], re.compile(a["match"], flags)) for a in spec.get("activities", [])]
        self.step_category = re.compile(spec.get("step_category", r"^LogPython$"))
        self.step_kinds = [(a["name"], re.compile(a["match"], flags)) for a in spec.get("steps", [])]
        self.stamp = repr(sorted((k, str(v)) for k, v in spec.items()))

    def step_kind(self, label):
        for name, rx in self.step_kinds:
            if rx.search(label):
                return name
        return "other output"

    def activity(self, category, message):
        text = category + ": " + message
        for name, rx in self.activities:
            if rx.search(text):
                return name
        return "other"


def step_label(message):
    msg = re.sub(r"^\s*(Display|Warning|Error):\s*", "", message)
    tag = ""
    m = re.match(r"\[([^\]]{1,60})\]\s*(.*)", msg)
    if m:
        tag, msg = m.group(1), m.group(2)
    msg = PATH_RE.sub(lambda p: "<file" + (os.path.splitext(p.group(0).rstrip("/\\"))[1][:5]) + ">", msg)
    msg = re.sub(r"\[[^\]]{12,}\]", "[…]", msg)
    msg = re.sub(r"\{[^}]{12,}\}", "{…}", msg)
    msg = re.sub(r"\d+(\.\d+)?", "#", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return tag, msg[:90]


def frame_rate(rows):
    """Engine frames per second once started (after "Engine is initialized"), from the log's frame counter (which
    wraps at 1000): only line pairs under 5 s apart count, so a wrap cannot hide (5 s holds < 1000 frames below 200
    fps). An editor in the background is throttled to 3 fps unless it runs -unattended."""
    started = next((i for i, r in enumerate(rows) if "Engine is initialized" in r[3]), None)
    if started is None:
        return None
    frames = seconds = 0.0
    for (t, f, _, _), (t2, f2, _, _) in zip(rows[started:], rows[started + 1:]):
        dt = t2 - t
        if 0 < dt < 5.0:
            frames += (f2 - f) % 1000
            seconds += dt
    return round(frames / seconds, 2) if seconds >= 3.0 else None


STALL_SECONDS = 2.0  # a tick blocked this long, ending in the script's own line (Python computing), is flagged
ENGINE_STALL_SECONDS = 10.0  # ending in an engine line (a package save, an HTTP call) only when this long
FIXED_WAIT_MIN = 3.0  # steps at least this long ...
FIXED_WAIT_SPREAD = 0.6  # ... repeating within this many seconds of each other ...
FIXED_WAIT_REPEATS = 3  # ... at least this often in one run look like a fixed-length wait
THROTTLED_FPS = 4.0
SLOW_STARTUP = 30.0
SLOW_TEST = 60.0  # an automation test this long is worth splitting / filtering out of iteration runs
ISSUE_KINDS = {
    "throttled": "editor throttled to ~3 fps (launched without -unattended)",
    "fixed wait": "repeated fixed-length wait",
    "python stall": "Python blocking the editor (no frames)",
    "check timeout": "check failed only at its timeout",
    "slow startup": "slow editor startup",
    "slow test": "slow automation test",
}


def detect_issues(duration, fps, flags, steps, stalls, startup, tests=()):
    """Patterns that waste time, per run: [kind, seconds, label]. They come from the log alone, so every new run is
    checked as soon as it is harvested."""
    issues = []
    # The background throttle only applies without -unattended; a slower -unattended run is blocked frames, not this.
    throttled = fps is not None and fps < THROTTLED_FPS and "-nullrhi" not in flags and "-unattended" not in flags
    if throttled:
        issues.append(["throttled", round(duration, 1), f"{fps:.1f} fps without -unattended"])
        steps = []  # at 3 fps every frame-counted wait looks fixed-length: the throttle is the finding
    # Steps of (nearly) the same length, again and again: a wait for a fixed time rather than for the world.
    long_steps = sorted((s[1] - s[0], s[3]) for s in steps if s[1] - s[0] >= FIXED_WAIT_MIN and not s[3].startswith("CHECK FAILED"))
    group = []
    for item in long_steps + [(float("inf"), "")]:
        if group and item[0] - group[0][0] > FIXED_WAIT_SPREAD:
            if len(group) >= FIXED_WAIT_REPEATS:
                mean = sum(d for d, _ in group) / len(group)
                common = max(set(l for _, l in group), key=[l for _, l in group].count)
                for d, _ in group:
                    issues.append(["fixed wait", round(d, 1), f"~{mean:.0f} s steps, e.g. {common}"])
            group = []
        group.append(item)
    for s, e, label in stalls:
        issues.append(["python stall", round(e - s, 1), label])
    for s in steps:
        if s[3].startswith("CHECK FAILED") and s[1] - s[0] >= 10.0:
            issues.append(["check timeout", round(s[1] - s[0], 1), s[3]])
    for s, e, path, result in tests:
        if e - s >= SLOW_TEST:
            issues.append(["slow test", round(e - s, 1), path])
    total_startup = sum(d for d, _ in startup)
    if total_startup > SLOW_STARTUP:
        by = {}
        for d, a in startup:
            by[a] = by.get(a, 0.0) + d
        top = max(by.items(), key=lambda kv: kv[1])
        issues.append(["slow startup", round(total_startup, 1), f"mostly {top[0]} ({top[1]:.0f} s)"])
    return issues


def parse_log(lines, rules):
    """lines: iterable of str. Returns a run summary dict or None (not an Unreal log)."""
    rows = []
    cmdline = ""
    for line in lines:
        if not cmdline:
            m = CMDLINE.search(line)
            if m:
                cmdline = m.group(1).strip()
        m = LINE.match(line)
        if m:
            g = m.groups()
            rows.append((_epoch(*g[:7]), int(g[7]), g[8], g[9].rstrip()))
    if len(rows) < 5 or not cmdline:
        return None
    fps = frame_rate(rows)
    phases = []  # [start, end, phase, activity]
    steps = []  # [start, end, tag, label]
    state = "startup"
    before_map = None
    crashed = False
    exited = False
    ensures = 0
    stalls = []  # [start, end, label]: a gap the frame counter did not move through (one blocked tick)
    tests = []  # [start, end, path, result]: automation tests, from the controller's started / completed lines
    test_start = {}
    last_python = None
    for i in range(len(rows) - 1):
        t, _, cat, msg = rows[i]
        text = cat + ": " + msg
        # state changes caused by line i
        if state == "startup" and rules.startup_end.search(text):
            state = "editor idle"
        if state != "map load" and state != "shutdown" and rules.map_start.search(text):
            before_map, state = state, "map load"
        elif state == "map load" and rules.map_end.search(text):
            state = before_map or "editor idle"
        if state in ("editor idle", "startup") and rules.script_start.search(text) and cat != "LogInit":
            state = "script"
        if state == "map load" and before_map in ("editor idle", "startup") and rules.script_start.search(text):
            before_map = "script"
        if rules.shutdown.search(text):
            state, exited = "shutdown", True
        if rules.crash.search(text):
            crashed = True
        if cat == "LogAutomationController" and "Test " in msg:
            m = TEST_STARTED.search(msg)
            if m:
                test_start[m.group(2)] = t
            else:
                m = TEST_DONE.search(msg)
                if m and m.group(3) in test_start:
                    tests.append([test_start.pop(m.group(3)), t, m.group(3), m.group(1)])
        if "=== Handled ensure" in msg:
            ensures += 1
        if rules.step_category.search(cat):
            last_python = t if last_python is None else last_python
        t2, f2, cat2, msg2 = rows[i + 1]
        # one frame at most between the lines: the work ran inside a single tick (the next tick logged the result)
        if ((f2 - rows[i][1]) % 1000 <= 1 and state in ("script", "map load")
                and t2 - t >= (STALL_SECONDS if cat2 == "LogPython" else ENGINE_STALL_SECONDS)):
            stalls.append([t, t2, step_label(msg2)[1] if cat2 == "LogPython" else cat2])
        if t2 > t:
            act = rules.activity(cat2, msg2)
            if phases and phases[-1][1] == t and phases[-1][2] == state and phases[-1][3] == act:
                phases[-1][1] = t2
            else:
                phases.append([t, t2, state, act])
        if rules.step_category.search(cat2) and state in ("script", "map load"):
            start = last_python if last_python is not None else t
            if t2 - start >= 0.05:
                tag, label = step_label(msg2)
                steps.append([start, t2, tag, label, rules.step_kind(label)])
            last_python = t2
    if rules.crash.search(rows[-1][2] + ": " + rows[-1][3]):
        crashed = True
    scripts = []
    for s in SCRIPT_RE.findall(cmdline):
        if s not in scripts:
            scripts.append(s)
    flags = [f for f in ("-nullrhi", "-unattended", "-RenderOffscreen", "-game", "-run=") if f.lower() in cmdline.lower()]
    startup = [(e - s, a) for s, e, p, a in phases if p == "startup"]
    last_t, _, last_cat, last_msg = rows[-1]
    if last_cat == "LogAutomationController":  # the loop above stops one line short
        m = TEST_DONE.search(last_msg)
        if m and m.group(3) in test_start:
            tests.append([test_start.pop(m.group(3)), last_t, m.group(3), m.group(1)])
    for path, s in test_start.items():  # started, never completed: crashed or killed
        tests.append([s, last_t, path, "Unfinished"])
    issues = detect_issues(rows[-1][0] - rows[0][0], fps, flags, steps, stalls, startup, tests)
    return {
        "start": rows[0][0], "end": rows[-1][0], "lines": len(rows), "scripts": scripts, "flags": flags,
        "issues": issues,
        "cmdHash": hashlib.sha1(cmdline.encode("utf-8", "replace")).hexdigest()[:12],
        "phases": phases, "steps": steps, "exit": "crash" if crashed else ("clean" if exited else "unknown"),
        "ensures": ensures, "fps": fps, "tests": tests,
    }


class Harvester:
    """Finds, summarises and archives Unreal logs for every project with an [unreal] section."""

    def __init__(self, config):
        self.config = config
        self.cache_path = os.path.join(config.path("cache_dir"), "unreal.pickle")
        self.archive_dir = paths.LOGS  # shared by every org: the logs are the machine's
        self.files = {}  # path -> (size, mtime, key)
        self.runs = {}  # key -> run dict (with "project", "source", "archive")
        self.rules_stamp = None
        self.last = 0.0
        self.imported = False
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "rb") as handle:
                    version, files, runs, stamp = pickle.load(handle)
                if version == CACHE_VERSION:
                    self.files, self.runs, self.rules_stamp = files, runs, stamp
            except Exception:
                pass

    def save(self):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        tmp = f"{self.cache_path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as handle:
            pickle.dump((CACHE_VERSION, self.files, self.runs, self.rules_stamp), handle, protocol=pickle.HIGHEST_PROTOCOL)
        # On Windows the swap fails while another process (a hook) has the cache open for reading: retry briefly.
        for attempt in range(20):
            try:
                os.replace(tmp, self.cache_path)
                return
            except PermissionError:
                time.sleep(0.25)
        os.remove(tmp)  # still locked: the next refresh saves again

    def rules_for(self, project):
        spec = dict(self.config.unreal)
        extra = project.unreal or {}
        spec.update({k: v for k, v in extra.items() if k not in ("activities", "log_globs")})
        spec["activities"] = list(extra.get("activities", [])) + list(self.config.unreal.get("activities", []))
        spec["steps"] = list(extra.get("steps", [])) + list(self.config.unreal.get("steps", []))
        return Rules(spec)

    def refresh(self, min_interval=20.0, rebuild=True):
        """Returns True when runs changed. rebuild=False (hooks): only pick up new logs; re-parsing the archive after a
        rules change, or importing it into an empty cache, is left to the dashboard server."""
        if time.time() - self.last < min_interval:
            return False
        self.last = time.time()
        changed = False
        if not self.imported and rebuild:  # runs whose logs are gone live on in the archive
            changed |= self._import_archive()
            self.imported = True
        stamp = repr([(p.name, self.rules_for(p).stamp) for p in self.config.projects])
        if stamp != self.rules_stamp and rebuild:
            changed |= self._reparse_archive()
            self.rules_stamp = stamp
        for project in self.config.projects:
            globs = (project.unreal or {}).get("log_globs", [])
            if not globs:
                continue
            rules = self.rules_for(project)
            for pattern in globs:
                for path in glob.glob(os.path.expanduser(pattern), recursive=True):
                    changed |= self._visit(path, project, rules)
        if changed and rebuild:  # hooks only read the cache; the server is its one writer
            self.save()
        return changed

    def _visit(self, path, project, rules):
        try:
            st = os.stat(path)
        except OSError:
            return False
        known = self.files.get(path)
        if known and known[0] == st.st_size and known[1] == st.st_mtime:
            return False
        if st.st_size > 200 * 1024 * 1024:
            return False
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            return False
        text = raw.decode("utf-8", "replace").lstrip("﻿")
        run = parse_log(text.splitlines(), rules)
        if run is None:
            self.files[path] = (st.st_size, st.st_mtime, None)
            return False
        key = f"{int(run['start'])}-{run['cmdHash']}"
        old = self.runs.get(key)
        if old and old.get("source") != path and old["end"] >= run["end"]:
            # a shorter copy of a run already seen (crash reporters snapshot the log at an ensure or a crash)
            if run["exit"] == "crash" and old["exit"] != "crash":
                old["exit"] = "crash"
            self.files[path] = (st.st_size, st.st_mtime, key)
            return run["exit"] == "crash"
        complete = run["exit"] != "unknown" or time.time() - st.st_mtime > 120
        run.update(project=project.name, source=path, name=os.path.basename(path), checkout=checkout_of(path))
        # archived while still being written too: a log name reused by the next run would otherwise lose this one
        run.update(archive=self._archive(key, raw, run), live=not complete)
        if known and known[2] and known[2] != key and known[2] in self.runs:
            self.runs[known[2]]["live"] = False  # the file now holds a newer run
        self.runs[key] = run
        self.files[path] = (st.st_size, st.st_mtime, key)
        return True

    def _archive(self, key, raw, run):
        # <data dir>/logs/<project>/<day>/<key>__<checkout>__<log name>.gz: enough to rebuild the run without the cache
        day = time.strftime("%Y-%m-%d", time.gmtime(run["start"]))
        folder = os.path.join(self.archive_dir, safe(run["project"]), day)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, f"{key}__{safe(run['checkout'])}__{safe(run['name'])}.gz")
        if not os.path.exists(dest) or run.get("live") is not False:
            with gzip.open(dest + ".tmp", "wb", compresslevel=6) as handle:
                handle.write(raw)
            os.replace(dest + ".tmp", dest)
        return dest

    def _import_archive(self):
        changed = False
        projects = {safe(p.name): p for p in self.config.projects}
        for path in glob.glob(os.path.join(self.archive_dir, "*", "*", "*.gz")):
            parts = os.path.basename(path)[:-3].split("__", 2)
            project = projects.get(os.path.basename(os.path.dirname(os.path.dirname(path))))
            if len(parts) != 3 or parts[0] in self.runs or project is None:
                continue
            new = self._parse_archive(path, project)
            if new:
                new.update(project=project.name, source=None, name=parts[2], checkout=parts[1], archive=path, live=False)
                self.runs[parts[0]] = new
                changed = True
        return changed

    def _parse_archive(self, path, project):
        try:
            with gzip.open(path, "rb") as handle:
                text = handle.read().decode("utf-8", "replace").lstrip("﻿")
        except (OSError, EOFError):
            return None
        return parse_log(text.splitlines(), self.rules_for(project))

    def _reparse_archive(self):
        changed = False
        projects = {p.name: p for p in self.config.projects}
        for key, run in list(self.runs.items()):
            project = projects.get(run.get("project"))
            src = run.get("archive")
            if not project or not src or not os.path.exists(src):
                continue
            new = self._parse_archive(src, project)
            if new:
                for k in ("project", "source", "name", "checkout", "archive", "live"):
                    new[k] = run.get(k)
                self.runs[key] = new
                changed = True
        return changed


def safe(name):
    return re.sub(r"[^\w.-]+", "_", name or "")[:80]


def checkout_of(path):
    p = path.replace("\\", "/")
    m = re.search(r"/([\w.-]+)/Saved/(Logs|Crashes)/", p, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.search(r"/Temp/claude/", p, re.IGNORECASE):
        return "scratchpad"
    return os.path.basename(os.path.dirname(p))
