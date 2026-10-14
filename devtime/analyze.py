"""Turns cached transcript events into the dashboard dataset.

Per transcript, wall-clock is split into intervals:

- model: one per API request, from the record that triggered it to the last block it returned (tokens, thinking time
  and estimated cost attached); its label is the trigger of the turn chain it belongs to ("human", "peer", "agent",
  "background-command", "monitor:<name>", ...) and the first request of a chain carries F_WAKE;
- tool calls by kind, tool_use to tool_result; background shell runs from the call to their task-notification (the
  time it was queued, not when the session got round to reading it);
- compaction, from the compact boundary's own duration;
- gaps where nothing ran: under idle_gap_s they are "overhead" (harness, hooks, permission prompts); longer ones are
  "waiting on subagent" while a background agent or workflow is running, "waiting on user" when a human prompt ends
  them, "waiting on wakeup" after a ScheduleWakeup, else "idle".

Concurrent intervals split the time they overlap equally (the "share"), so the shares of one transcript add up to its
wall-clock. Background subagents and workflows are drawn on the parent's row but not counted there: their work is in
their own transcripts. Agent time is the sum of non-waiting shares over all transcripts; busy wall-clock is the union
of those intervals across transcripts.
"""

import bisect
import collections
import os
import re
import time

from . import unreal

HOUR = 3600.0
_RIDS = {}  # path -> (offset, {request id: time}): request ids per transcript, for fork / copy de-duplication
ROWS_VERSION = 5  # bump when _rows changes, so cached rows are rebuilt
F_BACKGROUND, F_ERROR, F_UNFINISHED, F_NOSHARE, F_LAUNCH, F_WAIT, F_WAKE = 1, 2, 4, 8, 16, 32, 64
SCRIPT_IN_CMD = re.compile(r"([\w.-]+\.py)\b")
GENERIC_BRANCHES = re.compile(r"^(main|master|HEAD|develop|claude/.*)$")


class Interner:
    def __init__(self):
        self.items, self.index = [], {}

    def __call__(self, value):
        value = value if value is not None else ""
        i = self.index.get(value)
        if i is None:
            i = self.index[value] = len(self.items)
            self.items.append(value)
        return i


def majority(counter):
    return max(counter.items(), key=lambda kv: kv[1])[0] if counter else ""


def auto_project_name(cwd, folder):
    p = (cwd or "").replace("\\", "/").rstrip("/")
    p = re.sub(r"/\.claude/worktrees/[^/]+$", "", p)
    name = p.rsplit("/", 1)[-1] if p else folder
    return name or folder


class Builder:
    def __init__(self, config, cache, discovered, desktop, blocks=None, runs=None):
        self.blocks = blocks or {}  # path -> (key, rows): per-transcript results reused while the file is unchanged
        self.runs = runs or {}  # Unreal Editor runs from their logs (devtime.unreal)
        self.config = config
        launch = config.unreal.get("launch")
        self.launch_re = re.compile(launch, re.IGNORECASE) if launch else None
        wait = config.unreal.get("wait")
        self.wait_re = re.compile(wait, re.IGNORECASE) if wait else None
        self.launch_scripts = {}  # interval -> the Python script a launching call names (for runs whose log is lost)
        self.cache = cache
        self.discovered = discovered
        self.desktop = desktop
        self.idle_gap = float(config.settings.get("idle_gap_s", 30))
        self.kinds = Interner()
        self.labels = Interner()
        self.branches = Interner()
        self.workstreams = Interner()
        self.tools = Interner()
        self.models = Interner()
        self.iv = {k: [] for k in ("t", "s", "e", "k", "l", "w", "b", "ws", "f", "n")}
        self.tok = {k: [] for k in ("i", "m", "in", "out", "cr", "cw5", "cw1", "th", "ms", "cost")}
        self.hours = []
        self.transcripts = []

    # ---------------------------------------------------------------------------------------------------------------
    def build(self):
        states = self.cache.states
        mains = {}
        entries = []
        for d in self.discovered:
            st = states.get(d["path"])
            if st is None or st.first_ts is None:
                continue
            entries.append((d, st))
            if d["role"] == "main":
                mains[(d["folder"], d["session"])] = (d, st)
        # Main sessions first so subagents can inherit project / title.
        entries.sort(key=lambda e: (e[0]["role"] != "main", e[1].first_ts))
        info = {}
        for d, st in entries:
            info[d["path"]] = self._transcript_info(d, st, mains, info)
        self.agent_end = {}
        for d, st in entries:
            if d["role"] != "main" and d.get("agent"):
                self.agent_end[d["agent"]] = st.last_ts
        session_ends = collections.defaultdict(list)
        for d, st in entries:
            if d["role"] != "main":
                session_ends[(d["folder"], d["session"])].append(st.last_ts)
        cuts = self._fork_cuts(entries, mains)
        blocks = {}
        stamp = self.config.mtime
        for d, st in entries:
            t = info[d["path"]]
            sub_ends = tuple(sorted(session_ends.get((d["folder"], d["session"]), ()))) if d["role"] == "main" else ()
            cut = cuts.get(d["path"])
            key = (ROWS_VERSION, st.offset, st.last_ts, stamp, t["project"], t["title"], t["description"],
                   self.idle_gap, sub_ends, cut)
            if cut is not None:
                t["start"] = min(max(t["start"], cut), t["end"])
                t["copiedUntil"] = cut
            cached = self.blocks.get(d["path"])
            if cached and cached[0] == key:
                rows = cached[1]
            else:
                rows = self._rows(t, st, cut)
            blocks[d["path"]] = (key, rows)
            self._emit(t, rows)
        self.blocks = blocks
        data = self._dataset()
        data["ue"] = self._unreal()
        return data

    @staticmethod
    def _fork_cuts(entries, mains):
        """A forked or copied session file repeats the history it was copied from (same request ids). Each request
        belongs to one transcript: the copy whose session went on longest (a subagent copy goes with its main). Another transcript holding it is cut at the last
        such request, so the shared history is counted once. Returns {path: cut time}."""
        owner = {}
        per = []
        for d, st in entries:
            cached = _RIDS.get(d["path"])
            if not cached or cached[0] != st.offset:
                rids = {}
                for e in st.events:
                    if e[0] == "a" and e[3]:
                        rids.setdefault(e[3], e[1])
                cached = _RIDS[d["path"]] = (st.offset, rids)
            main = mains.get((d["folder"], d["session"]))
            rank = ((main[1].last_ts or 0) if main else 0, st.last_ts or 0, st.offset)
            per.append((d["path"], cached[1]))
            for rid in cached[1]:
                cur = owner.get(rid)
                if cur is None or rank > cur[0]:
                    owner[rid] = (rank, d["path"])
        cuts = {}
        for path, rids in per:
            lost = [ts for rid, ts in rids.items() if owner[rid][1] != path]
            if lost:
                cuts[path] = max(lost)
        return cuts

    def _transcript_info(self, d, st, mains, info):
        cwd = majority(st.cwds)
        branch = majority(st.branches)
        desk = self.desktop.get(d["session"]) or {}
        parent = None
        if d["role"] == "main":
            project = self.config.project_for(cwd) or self.config.project_for(desk.get("originCwd") or "")
            pname = project.name if project else auto_project_name(desk.get("originCwd") or cwd, d["folder"])
            title = desk.get("title") or d.get("title") or st.title or d["session"][:8]
            desc = ""
        else:
            parent_entry = mains.get((d["folder"], d["session"]))
            parent = info.get(parent_entry[0]["path"]) if parent_entry else None
            project = parent["projectObj"] if parent else self.config.project_for(cwd)
            pname = parent["project"] if parent else (project.name if project else auto_project_name(cwd, d["folder"]))
            title = parent["title"] if parent else d["session"][:8]
            meta = d.get("meta") or {}
            desc = meta.get("description") or d.get("agent", "")
        idx = len(self.transcripts)
        meta = d.get("meta") or {}
        t = {
            "idx": idx, "id": d.get("agent") or d["session"], "role": d["role"], "session": d["session"],
            "parent": parent["idx"] if parent else None, "project": pname, "projectObj": project, "title": title,
            "description": desc, "agentType": meta.get("agentType"), "shape": meta.get("requestShape"),
            "toolUseId": meta.get("toolUseId"), "workflow": d.get("workflow"), "phase": meta.get("workflowPhase"),
            "cwd": cwd, "branch": branch, "start": st.first_ts, "end": st.last_ts,
            "entrypoint": st.entrypoint, "models": dict(st.models),
            "costStates": [{k: c.get(k) for k in ("totalCostUSD", "totalAPIDuration", "totalToolDuration",
                                                  "totalDuration", "startTime", "totalLinesAdded",
                                                  "totalLinesRemoved")} for c in st.cost_states],
            "seconds": collections.Counter(), "calls": collections.Counter(), "wsSeconds": collections.Counter(),
            "tokens": [0, 0, 0, 0, 0, 0], "cost": 0.0, "thinkingS": 0.0, "requests": 0,
        }
        self.transcripts.append(t)
        return t

    # ---------------------------------------------------------------------------------------------------------------
    def _workstream(self, t, branch, ws_cache):
        key = branch
        if key in ws_cache:
            return ws_cache[key]
        fields = {"description": t["description"], "session": t["title"], "branch": branch, "cwd": t["cwd"]}
        name = None
        project = t["projectObj"]
        if project:
            for field in ("description", "session", "cwd", "branch"):
                for ws in project.workstreams:
                    if any(f == field and rx.search(fields[field] or "") for f, rx in ws.tests):
                        name = ws.name
                        break
                if name:
                    break
        if not name:
            name = branch if branch and not GENERIC_BRANCHES.match(branch) else t["title"]
        ws_cache[key] = name
        return name

    def _rows(self, t, st, cut=None):
        cfg = self.config
        project = t["projectObj"]
        events = sorted(st.events, key=lambda e: e[1])
        first, last = st.first_ts, st.last_ts
        if cut is not None:  # history copied from another transcript (a fork): counted there
            events = [e for e in events if e[1] > cut]
            first = min(max(first, cut), last)
        requests = {}
        usage_by_msg = {}
        uses = []
        seen_uses = set()
        results = {}
        notif_task, notif_tool = {}, {}
        humans = []
        prompts = []  # every non-tool-result user message: where an interrupted call is known to be over
        wakeups = []
        compactions = []
        triggers = []  # (time, what woke the session): human prompts, peer messages, task notifications
        stops = {}  # background task id -> TaskStop time
        for e in events:
            tag = e[0]
            if tag == "a":
                _, ts, prev, rid, mid, model, usage, think_ms, branch = e
                r = requests.get(rid)
                if r is None:
                    r = requests[rid] = [prev, ts, model, 0, branch, set(), ts]
                r[1] = max(r[1], ts)
                r[3] += think_ms
                if model and not r[2]:
                    r[2] = model
                if usage and mid:
                    usage_by_msg[mid] = usage
                    r[5].add(mid)
            elif tag == "tu":
                if e[2] in seen_uses:  # the same call logged twice (resume, compaction)
                    continue
                seen_uses.add(e[2])
                uses.append(e)
                if e[3] == "TaskStop" and e[7]:
                    stops.setdefault(e[7], e[1])
                if e[3] == "ScheduleWakeup":
                    wakeups.append(e[1])
            elif tag == "tr":
                if e[2] not in results:
                    results[e[2]] = e
            elif tag == "tn":
                _, ts, task, tool, status = e
                if task not in notif_task or ts < notif_task[task]:
                    notif_task[task] = ts
                if tool and (tool not in notif_tool or ts < notif_tool[tool]):
                    notif_tool[tool] = ts
            elif tag == "u":
                prompts.append(e[1])
                if e[2] == "human":
                    humans.append(e[1])
            elif tag == "cb":
                compactions.append(e)
            elif tag == "tg":
                triggers.append((e[1], e[2]))

        prompts.sort()

        def cut_off(ts):
            """A call that never got its result ended (interrupt, crash, resume) by the next message, at the latest."""
            k = bisect.bisect_right(prompts, ts)
            return prompts[k] if k < len(prompts) else last

        ivs = []  # [start, end, kind, label, flags, branch, tool, tokinfo]
        background = []  # (start, end) of background agents / workflows, for gap classification
        for e in uses:
            _, ts, tid, name, text, label, bg, extra, branch = e
            kind = cfg.classify(project, name, text)
            res = results.get(tid)
            flags = 0
            if (self.launch_re is not None and name in SHELL_TOOLS and kind not in ("read/search code", "git")
                    and self.launch_re.search(text)):
                flags |= F_LAUNCH  # may have started an Unreal Editor run
            elif self.wait_re is not None and name in SHELL_TOOLS and self.wait_re.search(text.split("\n", 1)[-1]):
                flags |= F_WAIT  # polls / sleeps on something else
            if res and res[4]:
                flags |= F_ERROR
            async_agent = name in ("Agent", "Workflow") and res is not None and (
                res[5] is not None and (bg or (extra and extra[1]) or name == "Workflow" or tid in notif_tool))
            if async_agent:
                end = notif_tool.get(tid)
                if end is None:  # no notification: the agent's own transcript says when it stopped
                    end = self.agent_end.get(res[5], last)
                    flags |= F_UNFINISHED
                kind = "subagent (background)" if name == "Agent" else "workflow (background)"
                ivs.append([ts, max(end, ts), kind, label, flags | F_BACKGROUND | F_NOSHARE, branch, name, None])
                background.append((ts, end))
                continue
            if bg and res is not None and res[3]:
                end = notif_task.get(res[3]) or notif_tool.get(tid) or stops.get(res[3])
                flags |= F_BACKGROUND
                if end is None:  # never reported: the task's output file was last written when it finished
                    end = output_mtime(res[6] if len(res) > 6 else None, ts) or cut_off(ts)
                    flags |= F_UNFINISHED
            elif res is not None:
                end = res[1]
            else:
                end = cut_off(ts)
                if name in SHELL_TOOLS:  # a foreground shell call cannot outlive its timeout
                    end = min(end, ts + min(float(extra or 120000), 600000.0) / 1000.0 + 5.0)
                flags |= F_UNFINISHED
            hint = None
            if flags & F_LAUNCH:
                found = [s for s in SCRIPT_IN_CMD.findall(text.split("\n", 1)[-1]) if not s.startswith("tmp")]
                hint = found[0] if found else "(no script)"
            ivs.append([ts, max(end, ts), kind, label, flags, branch, name, hint])
        trigger_ts = [x[0] for x in triggers]  # events are sorted by time
        woken = set()
        for rid, (start, end, model, think_ms, branch, mids, first_ts) in sorted(requests.items(),
                                                                                  key=lambda kv: kv[1][6]):
            # A turn chain: the requests from one trigger until the next one.
            k = bisect.bisect_right(trigger_ts, first_ts) - 1
            trigger = triggers[k][1] if k >= 0 else "start"
            wake = 0
            if k >= 0 and k not in woken and model != "<synthetic>":
                woken.add(k)
                wake = F_WAKE
            tok = [0, 0, 0, 0, 0, 0]
            for mid in mids:
                u = usage_by_msg.get(mid)
                if u:
                    for i in range(6):
                        tok[i] += u[i]
            info = (model, tok, think_ms) if model != "<synthetic>" else None  # harness-made placeholder messages
            ivs.append([min(start, end), end, "model", trigger, wake, branch, "", info])
        for _, ts, dur, trigger in compactions:
            ivs.append([max(first, ts - dur), ts, "compaction", trigger or "", 0, "", "", None])

        shares = self._sweep(ivs, first, last)
        # Gaps: uncovered stretches inside the transcript.
        gaps = self._gaps(ivs, first, last, background, humans, wakeups)

        ws_cache = {}
        default_branch = t["branch"]
        rows = []
        for i, iv in enumerate(ivs + gaps):
            start, end, kind, label, flags, branch, tool, tokinfo = iv
            branch = branch or default_branch
            if i < len(ivs):
                share, hours = shares[i]
            else:
                share = end - start
                hours = split_hours(start, end, 1.0) if int(start // HOUR) != int(end // HOUR) else None
            if tokinfo and not isinstance(tokinfo, str):
                tokinfo = tokinfo + (self.cost(tokinfo[0], tokinfo[1]),)
            rows.append((start, end, kind, label, share, branch, self._workstream(t, branch, ws_cache), flags, tool,
                         hours, tokinfo))
        return rows

    def _emit(self, t, rows):
        tidx = t["idx"]
        iv, tokc = self.iv, self.tok
        for start, end, kind, label, share, branch, ws, flags, tool, hours, tokinfo in rows:
            n = len(iv["t"])
            iv["t"].append(tidx)
            iv["s"].append(round(start, 3))
            iv["e"].append(round(end, 3))
            iv["k"].append(self.kinds(kind))
            iv["l"].append(self.labels(label))
            iv["w"].append(round(share, 3))
            iv["b"].append(self.branches(branch))
            iv["ws"].append(self.workstreams(ws))
            iv["f"].append(flags)
            iv["n"].append(self.tools(tool))
            if hours:
                for h, secs in hours.items():
                    self.hours.append([n, int(h * HOUR), round(secs, 3)])
            if not flags & F_NOSHARE:
                t["seconds"][kind] += share
                t["wsSeconds"][ws] += share
            if tool:
                t["calls"][kind] += 1
            if isinstance(tokinfo, str):
                self.launch_scripts[n] = tokinfo
            elif tokinfo:
                model, tok, think_ms, cost = tokinfo
                tokc["i"].append(n)
                tokc["m"].append(self.models(model))
                for key, value in zip(("in", "out", "cr", "cw5", "cw1", "th"), tok):
                    tokc[key].append(value)
                tokc["ms"].append(think_ms)
                tokc["cost"].append(round(cost, 6))
                for j in range(6):
                    t["tokens"][j] += tok[j]
                t["cost"] += cost
                t["thinkingS"] += think_ms / 1000.0
                t["requests"] += 1

    def _unreal(self):
        """Unreal runs as columns, each matched to the shell call that launched it (the shortest call flagged as a
        launcher whose window holds the run's start, in the same project); unmatched runs keep iv -1."""
        iv = self.iv
        project_of = [t["project"] for t in self.transcripts]
        launchers = collections.defaultdict(list)
        fallback = collections.defaultdict(list)  # calls of an Unreal kind whose command did not name a launcher
        ue_kinds = {i for i, k in enumerate(self.kinds.items) if k.startswith("unreal") or k == "gpu lock wait"}
        for n, f in enumerate(iv["f"]):
            if f & F_LAUNCH:
                launchers[project_of[iv["t"][n]]].append(n)
            elif iv["k"][n] in ue_kinds and not f & F_NOSHARE:
                fallback[project_of[iv["t"][n]]].append(n)
        names = {k: Interner() for k in ("phases", "activities", "labels", "scripts", "stepKinds", "checkouts", "tags",
                                          "logs", "exits")}
        runs = {k: [] for k in ("iv", "t", "s", "e", "script", "checkout", "log", "exit", "project", "live", "fps",
                                "flags")}
        ph = {k: [] for k in ("r", "s", "e", "p", "a")}
        st = {k: [] for k in ("r", "s", "e", "l", "k", "g")}
        issues = {k: [] for k in ("r", "k", "s", "l")}
        tests = {k: [] for k in ("r", "s", "e", "p", "res")}
        names["testPaths"] = Interner()
        names["testResults"] = Interner()
        names["issueKinds"] = Interner()
        for run in sorted(self.runs.values(), key=lambda r: r["start"]):
            best, best_len = -1, None
            for pool in (launchers, fallback):
                for n in pool.get(run.get("project"), ()):
                    s, e = iv["s"][n], iv["e"][n]
                    if s - 5 <= run["start"] <= e + 5 and (best_len is None or e - s < best_len):
                        best, best_len = n, e - s
                if best >= 0:
                    break
            r = len(runs["s"])
            runs["iv"].append(best)
            runs["t"].append(iv["t"][best] if best >= 0 else -1)
            runs["s"].append(round(run["start"], 3))
            runs["e"].append(round(run["end"], 3))
            runs["script"].append(names["scripts"](" + ".join(run["scripts"][:2]) or "(no script)"))
            runs["checkout"].append(names["checkouts"](run.get("checkout") or ""))
            runs["log"].append(names["logs"](run.get("name") or ""))
            runs["exit"].append(names["exits"](run["exit"]))
            runs["project"].append(run.get("project") or "")
            runs["live"].append(bool(run.get("live")))
            runs["fps"].append(run.get("fps"))
            runs["flags"].append(" ".join(run.get("flags") or []))
            for s, e, phase, act in run["phases"]:
                ph["r"].append(r)
                ph["s"].append(round(s, 3))
                ph["e"].append(round(e, 3))
                ph["p"].append(names["phases"](phase))
                ph["a"].append(names["activities"](act))
            for s, e, path, result in run.get("tests", []):
                tests["r"].append(r)
                tests["s"].append(round(s, 3))
                tests["e"].append(round(e, 3))
                tests["p"].append(names["testPaths"](path))
                tests["res"].append(names["testResults"](result))
            for kind, secs, label in run.get("issues", []):
                issues["r"].append(r)
                issues["k"].append(names["issueKinds"](kind))
                issues["s"].append(secs)
                issues["l"].append(names["labels"](label))
            for step in run["steps"]:
                s, e, tag, label = step[:4]
                st["r"].append(r)
                st["s"].append(round(s, 3))
                st["e"].append(round(e, 3))
                st["l"].append(names["labels"](label))
                st["k"].append(names["stepKinds"](step[4] if len(step) > 4 else "other output"))
                st["g"].append(names["tags"](tag))
        out = {k: v.items for k, v in names.items()}
        from .findings import ADVICE
        out.update(runs=runs, ph=ph, st=st, issues=issues, tests=tests, issueTitles=dict(unreal.ISSUE_KINDS), issueAdvice=dict(ADVICE))
        return out

    def cost(self, model, tok):
        rates = self.config.price(model or "")
        if not rates:
            return 0.0
        scale = 1.0
        if rates.get("long_prompt_over") and tok[0] + tok[2] + tok[3] + tok[4] > rates["long_prompt_over"]:
            scale = rates.get("long_prompt_scale", 1.0)  # e.g. Haiku 5.5 prompts over 100k cost 5x
        return scale * (tok[0] * rates["input"] + tok[1] * rates["output"] + tok[2] * rates["cache_read"]
                        + tok[3] * rates["cache_write_5m"] + tok[4] * rates["cache_write_1h"]) / 1e6

    def _sweep(self, ivs, first, last):
        """Equal split of overlapping intervals; returns [(share, {hour: secs} or None)] per interval."""
        shares = [[0.0, None] for _ in ivs]
        points = []
        for i, iv in enumerate(ivs):
            if iv[4] & F_NOSHARE or iv[1] <= iv[0]:
                continue
            points.append((iv[0], 1, i))
            points.append((iv[1], 0, i))
        points.sort()
        active = set()
        prev = None
        for t, opening, i in points:
            if prev is not None and t > prev and active:
                width = (t - prev) / len(active)
                crosses = int(prev // HOUR) != int(t // HOUR)
                for j in active:
                    shares[j][0] += width
                    if crosses or shares[j][1] is not None or int(ivs[j][0] // HOUR) != int(ivs[j][1] // HOUR):
                        hours = shares[j][1]
                        if hours is None:
                            hours = shares[j][1] = {}
                        for h, secs in split_hours(prev, t, 1.0 / len(active)).items():
                            hours[h] = hours.get(h, 0.0) + secs
            if opening:
                active.add(i)
            else:
                active.discard(i)
            prev = t
        for s in shares:  # an interval within one hour needs no split
            if s[1] is not None and len(s[1]) <= 1:
                s[1] = None
        return shares

    def _gaps(self, ivs, first, last, background, humans, wakeups):
        spans = sorted((iv[0], iv[1]) for iv in ivs if not iv[4] & F_NOSHARE and iv[1] > iv[0])
        gaps = []
        cursor = first
        for s, e in spans + [(last, last)]:
            if s > cursor:
                gaps.append((cursor, s))
            cursor = max(cursor, e)
        humans.sort()
        wakeups.sort()
        out = []
        for a, b in gaps:
            if b - a <= self.idle_gap:
                kind = "overhead"
            elif any(bs <= a + 1 and be >= b - 1 for bs, be in background) or \
                    any(bs < b and be > a for bs, be in background):
                kind = "waiting on subagent"
            else:
                h = bisect.bisect_left(humans, b - 2)
                if h < len(humans) and humans[h] <= b + 2:
                    kind = "waiting on user"
                else:
                    w = bisect.bisect_right(wakeups, a + 1) - 1
                    kind = "waiting on wakeup" if w >= 0 and a - wakeups[w] < 600 else "idle"
            out.append([a, b, kind, "", 0, "", "", None])
        return out

    # ---------------------------------------------------------------------------------------------------------------
    def _dataset(self):
        waiting = self.config.waiting
        now = time.time()
        transcripts = []
        for t in self.transcripts:
            secs = t["seconds"]
            busy = sum(v for k, v in secs.items() if k not in waiting and k != "overhead")
            out = {k: v for k, v in t.items() if k not in ("projectObj", "seconds", "calls", "wsSeconds")}
            out["workstream"] = majority(t["wsSeconds"]) if t["wsSeconds"] else ""
            out["busy"] = round(busy, 1)
            out["wall"] = round(t["end"] - t["start"], 1)
            out["live"] = now - t["end"] < 300
            out["cost"] = round(t["cost"], 4)
            out["thinkingS"] = round(t["thinkingS"], 1)
            out["seconds"] = {k: round(v, 1) for k, v in secs.items()}
            out["calls"] = dict(t["calls"])
            transcripts.append(out)
        return {
            "generated": now,
            "kinds": self.kinds.items, "waiting": sorted(waiting), "labels": self.labels.items,
            "branches": self.branches.items, "workstreams": self.workstreams.items, "tools": self.tools.items,
            "models": self.models.items, "transcripts": transcripts, "iv": self.iv, "tok": self.tok,
            "hours": self.hours, "flags": {"background": F_BACKGROUND, "error": F_ERROR, "unfinished": F_UNFINISHED,
                                           "noshare": F_NOSHARE, "launch": F_LAUNCH, "wait": F_WAIT,
                                           "wake": F_WAKE},
            "launchScripts": {str(n): s for n, s in self.launch_scripts.items()},
        }


SHELL_TOOLS = ("Bash", "PowerShell")


def output_mtime(path, start):
    """Last write of a background task's output file (metadata only), if it is still on disk."""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return mtime if mtime >= start else None


def split_hours(a, b, weight):
    out = {}
    t = a
    while t < b:
        h = int(t // HOUR)
        nxt = min(b, (h + 1) * HOUR)
        out[h] = out.get(h, 0.0) + (nxt - t) * weight
        t = nxt
    return out


def subset(data, project):
    """The dataset restricted to one project (transcripts, their intervals and token rows), re-indexed."""
    if not project or project == "*":
        return data
    keep = [t for t in data["transcripts"] if t["project"] == project]
    remap = {t["idx"]: i for i, t in enumerate(keep)}
    transcripts = []
    for i, t in enumerate(keep):
        t = dict(t)
        t["idx"] = i
        t["parent"] = remap.get(t["parent"]) if t["parent"] is not None else None
        transcripts.append(t)
    iv = data["iv"]
    rows = [n for n, ti in enumerate(iv["t"]) if ti in remap]
    rowmap = {n: i for i, n in enumerate(rows)}
    new_iv = {k: [col[n] for n in rows] for k, col in iv.items()}
    new_iv["t"] = [remap[x] for x in new_iv["t"]]
    tok = data["tok"]
    trows = [j for j, n in enumerate(tok["i"]) if n in rowmap]
    new_tok = {k: [col[j] for j in trows] for k, col in tok.items()}
    new_tok["i"] = [rowmap[n] for n in new_tok["i"]]
    hours = [[rowmap[n], h, s] for n, h, s in data["hours"] if n in rowmap]
    out = dict(data)
    out.update(transcripts=transcripts, iv=new_iv, tok=new_tok, hours=hours, project=project)
    out["launchScripts"] = {str(rowmap[int(n)]): s for n, s in data.get("launchScripts", {}).items()
                            if int(n) in rowmap}
    ue = data.get("ue")
    if ue:
        keep_r = [r for r, p in enumerate(ue["runs"]["project"]) if p == project]
        rmap = {r: i for i, r in enumerate(keep_r)}
        runs = {k: [col[r] for r in keep_r] for k, col in ue["runs"].items()}
        runs["iv"] = [rowmap.get(n, -1) if n >= 0 else -1 for n in runs["iv"]]
        runs["t"] = [remap.get(t, -1) if t >= 0 else -1 for t in runs["t"]]
        sub = dict(ue, runs=runs)
        for key in ("ph", "st", "issues", "tests"):
            rows_k = [j for j, r in enumerate(ue[key]["r"]) if r in rmap]
            cols = {k: [col[j] for j in rows_k] for k, col in ue[key].items()}
            cols["r"] = [rmap[r] for r in cols["r"]]
            sub[key] = cols
        out["ue"] = sub
    # Keep only the strings this project uses.
    for table, col, cols in (("kinds", "k", new_iv), ("labels", "l", new_iv), ("branches", "b", new_iv),
                             ("workstreams", "ws", new_iv), ("tools", "n", new_iv), ("models", "m", new_tok)):
        used = sorted(set(cols[col]))
        index = {old: i for i, old in enumerate(used)}
        out[table] = [data[table][i] for i in used]
        cols[col] = [index[x] for x in cols[col]]
    return out


def default_project(data, configured=None, days=7):
    """The configured default project if it exists, else the project with the most cost in the last `days` days
    (else the most cost overall, else None)."""
    names = {t["project"] for t in data["transcripts"]}
    if configured and configured in names:
        return configured
    recent, total = collections.Counter(), collections.Counter()
    cutoff = time.time() - days * 86400
    for t in data["transcripts"]:
        total[t["project"]] += t["cost"]
        if t["end"] >= cutoff:
            recent[t["project"]] += t["cost"]
    for counter in (recent, total):
        if counter and max(counter.values()) > 0:
            return counter.most_common(1)[0][0]
    return None


def projects_summary(data):
    agg = collections.OrderedDict()
    for t in sorted(data["transcripts"], key=lambda t: -t["end"]):
        p = agg.setdefault(t["project"], {"name": t["project"], "sessions": 0, "subagents": 0, "busy": 0.0,
                                          "cost": 0.0, "last": 0.0})
        p["sessions" if t["role"] == "main" else "subagents"] += 1
        p["busy"] += t["busy"]
        p["cost"] += t["cost"]
        p["last"] = max(p["last"], t["end"])
    return sorted(agg.values(), key=lambda p: -p["busy"])
