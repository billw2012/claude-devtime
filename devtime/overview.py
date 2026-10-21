"""The overview page's numbers: performance and efficiency for a time range, what is live now, the biggest spenders.

Everything is derived from the built dataset (one project's slice or all); /api/overview?project=&range= serves it.

Efficiency is about cost: estimated API-equivalent cost, cost per commit, the avoidable part (subagents re-writing
their whole cache after waits over 5 minutes, requests carrying over 300k of context), cache hit rate, the 7-day
total against an estimated weekly budget. Performance is about throughput: commits per hour, how much of the time
something was working, how parallel it was, how long sessions sat waiting on the user, Unreal time and its flagged
waste. Each metric comes with the same numbers for the previous period of equal length, for a trend.
"""

import bisect
import collections
import datetime
import itertools
import json
import math
import os
import re
import statistics
import time
import tomllib

from . import fit as fitting, orgs, own, paths

RANGES = {"1h": 3600, "6h": 6 * 3600, "12h": 12 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
COMMIT = re.compile(r"^\s*(commit|git commit)\b|\bgit\b(\s+-C\s+\S+)?\s+(add\b[^\n]*&&\s*git\b[^\n]*\s)?commit\b"
                    r"|\bp4(?:\.exe)?\s+(?:-\w+(?:\s+[^-\s]\S*)?\s+)*submit\b|\bsvn\s+(?:commit|ci)\b|\bhg\s+commit\b",
                    re.IGNORECASE)
SOURCE_CONTROL = ("source control", "git")  # the kind of version-control calls ("git" in caches from before 0.7.0)
NOT_COMMIT = re.compile(r"\b(verify|check|inspect|show|log|status|diff|stats?)\b", re.IGNORECASE)
CHURN_GAP = 300  # a subagent's prompt cache lives 5 minutes
CHURN_MIN_TOKENS = 50_000
BIG_CONTEXT = 300_000
LIVE_S = 600
TICK_DAYS = 7  # the window of tick_summary's devtime usage
# editor-run findings that are plain waste; python stalls / slow tests are often the work itself (headless builds)
AVOIDABLE_EDITOR = {"throttled", "fixed wait", "check timeout", "slow startup"}


def bounds(range_key, data, now):
    if range_key in RANGES:
        return now - RANGES[range_key], now
    starts = [t["start"] for t in data["transcripts"]]
    return (min(starts) if starts else now - 86400), now


def union(spans):
    total, cs, ce = 0.0, None, None
    for s, e in sorted(spans):
        if cs is None or s > ce:
            if cs is not None:
                total += ce - cs
            cs, ce = s, e
        else:
            ce = max(ce, e)
    return total + ((ce - cs) if cs is not None else 0.0)


class Index:
    """Per-transcript request lists, built once per dataset version and shared by every range."""

    def __init__(self, data):
        iv, tok, trs = data["iv"], data["tok"], data["transcripts"]
        self.reqs = collections.defaultdict(list)  # t -> [(start, cost, cache write, context, cache read, model)]
        for j, i in enumerate(tok["i"]):
            self.reqs[iv["t"][i]].append((iv["s"][i], tok["cost"][j], tok["cw5"][j] + tok["cw1"][j],
                                          tok["in"][j] + tok["cr"][j] + tok["cw5"][j] + tok["cw1"][j],
                                          tok["cr"][j], tok["m"][j], tok["in"][j]))
        for rows in self.reqs.values():
            rows.sort()
        self.starts = {t: [r[0] for r in rows] for t, rows in self.reqs.items()}
        self.by_t = collections.defaultdict(list)  # t -> interval indexes, by start
        for i in range(len(iv["t"])):
            self.by_t[iv["t"][i]].append(i)
        for ids in self.by_t.values():
            ids.sort(key=lambda i: iv["s"][i])
        labels = data["labels"]
        scm = {data["kinds"].index(k) for k in SOURCE_CONTROL if k in data["kinds"]}
        hits = [i for i in range(len(iv["t"])) if iv["k"][i] in scm
                and COMMIT.search(labels[iv["l"][i]]) and not NOT_COMMIT.search(labels[iv["l"][i]])]
        self.commits = sorted(iv["s"][i] for i in hits)
        self.commits_by_t = collections.Counter(iv["t"][i] for i in hits)
        self.main_of = {}
        for t in trs:
            self.main_of[t["idx"]] = t["idx"] if t["role"] == "main" else (t["parent"] if t["parent"] is not None
                                                                           else t["idx"])
        # Model requests whose turn chain a Monitor event started: (start, main session, tag, cost, in, cache read,
        # cache write, out, wake).
        wake_flag = data["flags"].get("wake", 0)
        labels = data["labels"]
        mon = []
        for j, i in enumerate(tok["i"]):
            label = labels[iv["l"][i]]
            if label.startswith("monitor:"):
                mon.append((iv["s"][i], self.main_of[iv["t"][i]], label[8:], tok["cost"][j], tok["in"][j],
                            tok["cr"][j], tok["cw5"][j] + tok["cw1"][j], tok["out"][j],
                            1 if iv["f"][i] & wake_flag else 0))
        mon.sort(key=lambda r: r[0])
        self.monitor = mon
        self.monitor_starts = [r[0] for r in mon]
        # devtime's own model requests (analyze.F_OWN flags): (start, category index, cost)
        base = data["flags"].get("own", 0)
        mine = []
        for j, i in enumerate(tok["i"]):
            bits = (iv["f"][i] // base) & 15 if base else 0
            if bits:
                mine.append((iv["s"][i], bits.bit_length() - 1, tok["cost"][j]))
        mine.sort()
        self.own = mine
        self.own_starts = [r[0] for r in mine]


def period(data, idx, lo, hi):
    """Totals for one time window."""
    iv, trs, kinds = data["iv"], data["transcripts"], data["kinds"]
    noshare = data["flags"]["noshare"]
    waits = set(data["waiting"]) | {"overhead"}
    models = data["models"]
    cost = churn = big = sub_cost = opus_cost = 0.0
    cr = cw = inp = 0
    nreq = 0
    per_t = collections.defaultdict(lambda: [0.0, 0.0, [], 0])  # t -> [cost, churn, contexts, requests]
    for t, rows in idx.reqs.items():
        st = idx.starts[t]
        a, b = bisect.bisect_left(st, lo), bisect.bisect_right(st, hi)
        if a == b:
            continue
        sub = trs[t]["role"] != "main"
        acc = per_t[t]
        for k in range(a, b):
            s, c, w, ctx, r, m, n = rows[k]
            cost += c
            nreq += 1
            cr += r
            cw += w
            inp += n
            acc[0] += c
            acc[2].append(ctx)
            acc[3] += 1
            if ctx >= BIG_CONTEXT:
                big += c
            if sub:
                sub_cost += c
                if k and s - rows[k - 1][0] > CHURN_GAP and w >= CHURN_MIN_TOKENS:
                    churn += c
                    acc[1] += c
            name = models[m] if isinstance(m, int) else str(m)
            if "opus" in name:
                opus_cost += c
    # time: agent time by kind (shared overlap split), busy wall-clock, wall-clock waiting on the user
    agent = collections.Counter()
    busy, user_wait = [], []
    for i in range(len(iv["t"])):
        s, e = iv["s"][i], iv["e"][i]
        if e <= lo or s >= hi:
            continue
        a, b = max(s, lo), min(e, hi)
        kind = kinds[iv["k"][i]]
        if kind in waits:
            if kind in ("waiting on user", "editor open for user") and trs[iv["t"][i]]["role"] == "main":
                user_wait.append((a, b))
            continue
        if iv["f"][i] & noshare:
            continue
        agent[kind] += iv["w"][i] * (b - a) / max(e - s, 1e-6)
        busy.append((a, b))
    busy_wall = union(busy)
    # waiting on the user only counts while nothing else was running
    blocked = 0.0
    if user_wait:
        ev = sorted([(s, 1, 0) for s, e in busy] + [(e, -1, 0) for s, e in busy] +
                    [(s, 1, 1) for s, e in user_wait] + [(e, -1, 1) for s, e in user_wait])
        n_busy = n_wait = 0
        prev = None
        for t, d, which in ev:
            if prev is not None and t > prev and n_wait > 0 and n_busy == 0:
                blocked += t - prev
            if which:
                n_wait += d
            else:
                n_busy += d
            prev = t
    commits = bisect.bisect_right(idx.commits, hi) - bisect.bisect_left(idx.commits, lo)
    agent_total = sum(agent.values())
    ue = data.get("ue") or {}
    runs = ue.get("runs") or {}
    editor = waste = avoidable = 0.0
    waste_kinds = collections.Counter()
    in_range = set()
    for r in range(len(runs.get("s", []))):
        if runs["e"][r] > lo and runs["s"][r] < hi:
            in_range.add(r)
            editor += min(runs["e"][r], hi) - max(runs["s"][r], lo)
    issues = ue.get("issues") or {}
    issue_kinds = (ue.get("names") or {}).get("issueKinds") or ue.get("issueKinds") or []
    for j in range(len(issues.get("r", []))):
        if issues["r"][j] in in_range:
            waste += issues["s"][j]
            k = issues["k"][j]
            name = issue_kinds[k] if isinstance(k, int) and k < len(issue_kinds) else str(k)
            waste_kinds[name] += issues["s"][j]
            if name in AVOIDABLE_EDITOR:
                avoidable += issues["s"][j]
    hours = max(hi - lo, 1) / 3600
    return {
        "lo": lo, "hi": hi, "hours": hours, "cost": cost, "requests": nreq, "costPerHour": cost / hours,
        "churn": churn, "big": big, "subCost": sub_cost, "opusCost": opus_cost,
        "cacheHit": cr / max(cr + cw + inp, 1), "commits": commits,
        "costPerCommit": cost / commits if commits else None, "commitsPerHour": commits / hours,
        "agentTime": agent_total, "busyWall": busy_wall, "busyShare": busy_wall / max(hi - lo, 1),
        "parallel": agent_total / busy_wall if busy_wall else 0.0, "blockedOnUser": blocked,
        "kindTime": dict(agent.most_common()), "editor": editor, "editorFlagged": waste, "editorAvoidable": avoidable,
        "wasteKinds": dict(waste_kinds.most_common(4)), "perT": per_t,
    }


def monitoring(data, idx, lo, hi, cost=None, sessions=8):
    """What session monitoring cost in a window: the model requests of turn chains that a Monitor event started
    (trigger "monitor:<tag>", tag from the MON:<tag> label; "untagged" without one), by tag and by main session. A
    wake is one chain: the first request after the event until the next human prompt or notification."""
    rows = idx.monitor[bisect.bisect_left(idx.monitor_starts, lo):bisect.bisect_right(idx.monitor_starts, hi)]
    trs = data["transcripts"]

    def new():
        return {"wakes": 0, "requests": 0, "cost": 0.0, "in": 0, "cacheRead": 0, "cacheWrite": 0, "out": 0}

    total, by_tag, by_session = new(), collections.defaultdict(new), collections.defaultdict(new)
    session_tags = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0]))
    for _, main, tag, cost_, inp, cr, cw, out, wake in rows:
        for acc in (total, by_tag[tag], by_session[main]):
            acc["wakes"] += wake
            acc["requests"] += 1
            acc["cost"] += cost_
            acc["in"] += inp
            acc["cacheRead"] += cr
            acc["cacheWrite"] += cw
            acc["out"] += out
        st = session_tags[main][tag]
        st[0] += wake
        st[1] += cost_

    def finish(acc, **extra):
        acc = dict(acc, cost=round(acc["cost"], 4), avgPerWake=round(acc["cost"] / acc["wakes"], 4)
                   if acc["wakes"] else None)
        acc.update(extra)
        return acc
    total = finish(total, share=round(total["cost"] / cost, 4) if cost else None)
    tags = sorted((finish(a, tag=k) for k, a in by_tag.items()), key=lambda r: -r["cost"])
    sess = []
    for main, acc in sorted(by_session.items(), key=lambda kv: -kv[1]["cost"])[:sessions]:
        sess.append(finish(acc, session=label_of(trs[main]), tags={
            k: {"wakes": v[0], "cost": round(v[1], 4)} for k, v in sorted(session_tags[main].items(), key=lambda kv: -kv[1][1])}))
    return {"total": total, "byTag": tags, "bySession": sess}


def window_cost(idx, lo, hi):
    """Total cost of the requests that started in a window (what period() reports as cost, without the time sweep)."""
    total = 0.0
    for t, rows in idx.reqs.items():
        st = idx.starts[t]
        total += sum(r[1] for r in rows[bisect.bisect_left(st, lo):bisect.bisect_right(st, hi)])
    return total


def own_usage(data, idx, lo, hi, cost=None):
    """devtime's own Claude usage in a window (see devtime.own): {cost, share of `cost`, requests, byCategory}. A
    breakdown of the totals, not a part taken out of them."""
    cats = data.get("ownCategories") or list(own.CATEGORIES)
    rows = idx.own[bisect.bisect_left(idx.own_starts, lo):bisect.bisect_right(idx.own_starts, hi)]
    by = [0.0] * len(cats)
    for _, c, cost_ in rows:
        by[c] += cost_
    total = sum(by)
    return {"cost": round(total, 4), "share": round(total / cost, 4) if cost else None, "requests": len(rows),
            "byCategory": {name: round(by[i], 4) for i, name in enumerate(cats)}}


def label_of(t):
    if t["role"] == "main":
        return t.get("title") or t["id"][:8]
    return t.get("description") or t["id"][:8]


def model_of(t):
    models = t.get("models") or {}
    return max(models, key=models.get).replace("claude-", "") if models else ""


def current(data, idx, t, now):
    """What a live transcript is doing: its latest interval (a call still running, or the model)."""
    iv, kinds, labels = data["iv"], data["kinds"], data["labels"]
    ids = idx.by_t.get(t["idx"], [])
    for i in reversed(ids[-40:]):
        kind = kinds[iv["k"][i]]
        if kind in ("idle", "overhead"):
            continue
        running = bool(iv["f"][i] & data["flags"]["unfinished"]) and iv["e"][i] >= t["end"] - 1
        return {"kind": kind, "label": labels[iv["l"][i]][:90], "since": iv["s"][i],
                "running": running or now - iv["e"][i] < 30}
    return None


def live(data, idx, now):
    trs = data["transcripts"]
    out = []
    for t in trs:
        if now - t["end"] > LIVE_S:
            continue
        rows = idx.reqs.get(t["idx"], [])
        recent = [r for r in rows if r[0] >= now - 1800]
        ctx = rows[-1][3] if rows else 0
        out.append({"idx": t["idx"], "id": t["id"], "main": idx.main_of[t["idx"]], "role": t["role"], "name": label_of(t),
                    "project": t["project"], "model": model_of(t), "start": t["start"], "end": t["end"],
                    "cost": sum(r[1] for r in rows), "burn30": sum(r[1] for r in recent), "context": ctx,
                    "now": current(data, idx, t, now)})
    mains = {o["main"] for o in out}
    for m in mains:  # a live subagent's main session is shown even when it is only waiting
        if not any(o["idx"] == m for o in out):
            t = trs[m]
            rows = idx.reqs.get(m, [])
            out.append({"idx": m, "id": t["id"], "main": m, "role": "main", "name": label_of(t), "project": t["project"],
                        "model": model_of(t), "start": t["start"], "end": t["end"],
                        "cost": sum(r[1] for r in rows), "burn30": 0.0, "context": rows[-1][3] if rows else 0,
                        "now": {"kind": "waiting on subagents", "label": "", "since": t["end"], "running": True}})
    out.sort(key=lambda o: (o["main"], o["role"] != "main", -o["end"]))
    return out


def spenders(data, cur, top=10):
    trs = data["transcripts"]
    rows = []
    for t, (cost, churn, ctxs, n) in cur["perT"].items():
        tr = trs[t]
        rows.append({"idx": t, "name": label_of(tr), "role": tr["role"], "project": tr["project"],
                     "session": label_of(trs[tr["parent"]]) if tr.get("parent") is not None else "",
                     "model": model_of(tr), "cost": cost, "churn": churn, "requests": n,
                     "context": statistics.median(ctxs) if ctxs else 0})
    rows.sort(key=lambda r: -r["cost"])
    return rows[:top]


def trend(data, idx, lo, hi):
    """Cost per bucket: main sessions, subagents, and the avoidable part (cache churn)."""
    span = hi - lo
    step = 300 if span <= 2.5 * 3600 else 900 if span <= 13 * 3600 else 3600 if span <= 4 * 86400 else 86400
    start = lo - lo % step
    n = int((hi - start) // step) + 1
    main, sub, churn = [0.0] * n, [0.0] * n, [0.0] * n
    trs = data["transcripts"]
    for t, rows in idx.reqs.items():
        st = idx.starts[t]
        is_sub = trs[t]["role"] != "main"
        for k in range(bisect.bisect_left(st, lo), bisect.bisect_right(st, hi)):
            s, c, w = rows[k][0], rows[k][1], rows[k][2]
            b = int((s - start) // step)
            if is_sub:
                sub[b] += c
                if k and s - rows[k - 1][0] > CHURN_GAP and w >= CHURN_MIN_TOKENS:
                    churn[b] += c
            else:
                main[b] += c
    commits, agents = [0] * n, [0] * n  # throughput: commits made, subagents finished
    for s in idx.commits[bisect.bisect_left(idx.commits, lo):bisect.bisect_right(idx.commits, hi)]:
        commits[int((s - start) // step)] += 1
    for t, st in idx.starts.items():
        if trs[t]["role"] != "main" and lo <= st[-1] <= hi and hi - st[-1] > LIVE_S:
            agents[int((st[-1] - start) // step)] += 1
    return {"start": start, "step": step, "main": [round(x, 2) for x in main], "sub": [round(x, 2) for x in sub],
            "churn": [round(x, 2) for x in churn], "commits": commits, "agents": agents}


WEEK = 7 * 86400
SESSION = 5 * 3600


PLAN_SAMPLES = os.path.join(paths.CACHE, "plan_samples.jsonl")


def iso_ts(text):
    return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() if text else None


CHANGES = os.path.join(paths.USER_CONFIG, "changes.toml")


def changes(path=CHANGES):
    """[(time, label)] from <data dir>/config/changes.toml, oldest first."""
    try:
        with open(path, "rb") as f:
            items = tomllib.load(f).get("change") or []
    except (OSError, ValueError):
        return []
    out = []
    for c in items:
        at = c.get("at")
        t = at.timestamp() if isinstance(at, datetime.datetime) else iso_ts(at) if at else None
        if t and c.get("label"):
            out.append((t, str(c["label"])))
    return sorted(out)


def record_change(label, at=None, path=CHANGES):
    t = iso_ts(at) if at else time.time()
    stamp = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f'\n[[change]]\nat = "{stamp}"\nlabel = {json.dumps(label)}\n')
    return f"marked {stamp}: {label}"


def record_plan_sample(five, five_reset, week, week_reset, path=PLAN_SAMPLES, org=None):
    """Append one reading of the plan's usage (percent used and reset times, as the app's get_usage reports them).
    The reading belongs to `org` (default: this instance's): plans differ per organisation."""
    row = {"t": round(time.time(), 1), "fh": five, "sd": week, "fhReset": iso_ts(five_reset),
           "sdReset": iso_ts(week_reset), "org": org or paths.ORG or orgs.UNKNOWN}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return f"recorded: 5-hour {five:g} %, weekly {week:g} %"


USAGE_HARVEST = os.path.join(paths.CACHE, "usage_harvest.json")
CLAUDE_PROJECTS = os.path.join("~", ".claude", "projects")
HARVEST_DAYS = 9
HARVEST_VERSION = 3  # 2: per-file org; 3: other orgs' transcripts skipped
_WEEKLY_ALL = re.compile(r"^Weekly\b.*all models", re.I)
_harvest_mem = {"cache": None}


def _transcripts(root, since):
    """[(path, size, mtime)] of main and subagent transcripts under root modified since `since`."""
    out = []

    def add(entry):
        try:
            st = entry.stat()
        except OSError:
            return
        if st.st_mtime >= since:
            out.append((entry.path, st.st_size, st.st_mtime))

    try:
        with os.scandir(root) as projects:
            for proj in projects:
                if not proj.is_dir():
                    continue
                with os.scandir(proj.path) as items:
                    for e in items:
                        if e.name.endswith(".jsonl") and e.is_file():
                            add(e)
                        elif e.is_dir():
                            sub = os.path.join(e.path, "subagents")
                            if os.path.isdir(sub):
                                with os.scandir(sub) as subs:
                                    for x in subs:
                                        if x.name.endswith(".jsonl") and x.is_file():
                                            add(x)
    except OSError:
        pass
    return out


def _usage_reading(text, t):
    """[t, 5-hour %, weekly %, 5-hour reset, weekly reset] from a get_usage result text, or None."""
    try:
        plan = json.loads(text).get("plan") or {}
    except (ValueError, AttributeError):
        return None
    if plan.get("status") != "ok":
        return None
    fh = sd = None
    for w in plan.get("windows") or []:
        label, pct = str(w.get("label") or ""), w.get("percentUsed")
        if not isinstance(pct, (int, float)):
            continue
        try:
            reset = iso_ts(w.get("resetsAt"))
        except ValueError:
            reset = None
        if label.startswith("5-hour") and fh is None:
            fh = (pct, reset)
        elif _WEEKLY_ALL.match(label) and sd is None:
            sd = (pct, reset)
    if fh is None or sd is None:
        return None
    return [round(t, 1), fh[0], sd[0], fh[1], sd[1]]


def _scan_transcript(path, start=0, pending=(), org=None):
    """Scan a transcript from byte `start`: (get_usage readings, ids still waiting for a result, end offset, the
    transcript's own org tag). Only the numbers of get_usage results are kept."""
    ids, readings, pos = set(pending), [], start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written; pick it up next time
                pos += len(raw)
                if b"credential_org" in raw:
                    try:
                        org = orgs.tag_of(json.loads(raw)) or org
                    except ValueError:
                        pass
                    continue
                if b"get_usage" not in raw and b"percentUsed" not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                content = (row.get("message") or {}).get("content") if isinstance(row, dict) else None
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and str(c.get("name") or "").endswith("get_usage"):
                        ids.add(c.get("id"))
                    elif c.get("type") == "tool_result" and c.get("tool_use_id") in ids:
                        ids.discard(c.get("tool_use_id"))
                        body = c.get("content")
                        text = body if isinstance(body, str) else "".join(
                            x.get("text", "") for x in body or [] if isinstance(x, dict))
                        try:
                            t = iso_ts(row.get("timestamp"))
                        except ValueError:
                            t = None
                        r = _usage_reading(text, t) if t else None
                        if r:
                            readings.append(r)
    except OSError:
        pass
    return readings, sorted(i for i in ids if i), pos, org


def harvest_usage(root=None, cache=USAGE_HARVEST, days=HARVEST_DAYS, desktop_orgs=None, allowed=()):
    """Exact plan-usage readings that sessions already made with the get_usage tool, read from the Claude Code
    transcripts (main and subagent) under root, last `days` days: [(time, 5-hour %, weekly %, 5-hour reset,
    weekly reset, org)], oldest first, one per second and org. The org is the transcript's (orgs.py: its tag, its
    parent session's, the desktop index `desktop_orgs`, else "unknown"); a transcript whose org is not in `allowed`
    is not read beyond its org tag. Only those numbers are kept. Per-file results are cached by (size, mtime); a
    transcript that only grew is scanned from where the last scan stopped."""
    root = os.path.expanduser(root or CLAUDE_PROJECTS)
    desktop_orgs = desktop_orgs or {}
    allowed = frozenset(allowed or ())
    files = _transcripts(root, time.time() - days * 86400)
    sig = (sorted(files), allowed, sorted(desktop_orgs.items()))
    mem = _harvest_mem.get("cache")
    if mem and mem["root"] == root and mem["sig"] == sig:
        return _harvest_rows(mem["entries"], desktop_orgs)
    entries = {}
    if mem and mem["root"] == root:
        entries = mem["entries"]
    else:
        try:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("root") == root and data.get("v") == HARVEST_VERSION:
                entries = data.get("files") or {}
        except (OSError, ValueError, AttributeError):
            pass
    tags = {}
    for path, size, mtime in files:
        old = entries.get(path)
        tags[path] = orgs.scan_tag(path, (old.get("tagOff", 0), old.get("org")) if old else None)
    fresh, changed = {}, False
    for path, size, mtime in files:
        old = entries.get(path)
        off, tag = tags[path]
        if _org_of(path, tag, tags, desktop_orgs) not in allowed:  # another org's: nothing but its tag is read
            fresh[path] = {"size": size, "mtime": mtime, "tagOff": off, "org": tag, "skip": True, "readings": []}
            changed |= fresh[path] != old
            continue
        if old and not old.get("skip") and old.get("size") == size and old.get("mtime") == mtime:
            fresh[path] = dict(old, tagOff=off, org=tag)
            continue
        changed = True
        if old and not old.get("skip") and old.get("size", 0) <= size and old.get("offset", 0) > 0:
            readings, pending, pos, _ = _scan_transcript(path, old["offset"], old.get("pending") or ())
            readings = old.get("readings", []) + readings
        else:
            readings, pending, pos, _ = _scan_transcript(path)
        fresh[path] = {"size": size, "mtime": mtime, "offset": pos, "pending": pending, "readings": readings,
                       "tagOff": off, "org": tag}
    if set(fresh) != set(entries):
        changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = f"{cache}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": HARVEST_VERSION, "root": root, "files": fresh}, f, separators=(",", ":"))
            os.replace(tmp, cache)
        except OSError:
            pass
    _harvest_mem["cache"] = {"root": root, "sig": sig, "entries": fresh}
    return _harvest_rows(fresh, desktop_orgs)


def _org_of(path, tag, tags, desktop_orgs):
    """A harvested transcript's org: its tag, else (a subagent) its parent session's, else the desktop index's."""
    if tag:
        return tag
    folder, name = os.path.split(path)
    if os.path.basename(folder) == "subagents":  # <project>/<session>/subagents/agent-x.jsonl
        session_dir = os.path.dirname(folder)
        session = os.path.basename(session_dir)
        parent = os.path.join(os.path.dirname(session_dir), session + ".jsonl")
        parent_tag = (tags.get(parent) or orgs.scan_tag(parent))[1]
        return orgs.resolve(None, session, desktop_orgs, parent_tag)
    return orgs.resolve(None, os.path.splitext(name)[0], desktop_orgs)


def _harvest_rows(entries, desktop_orgs):
    """The readings of all scanned transcripts, each with its transcript's org."""
    tags = {path: (e.get("tagOff", 0), e.get("org")) for path, e in entries.items()}
    seen, out = set(), []
    for path, e in entries.items():
        if not e.get("readings"):
            continue
        org = _org_of(path, e.get("org"), tags, desktop_orgs)
        for r in e["readings"]:
            if (round(r[0]), org) not in seen:
                seen.add((round(r[0]), org))
                out.append(tuple(r) + (org,))
    out.sort()
    return out


def same_org(sample_org, org):
    """True if a reading made in `sample_org` (an id, a prefix of one, or "unknown") belongs to the org id `org`."""
    return orgs.matches(org, sample_org or orgs.UNKNOWN)


def plan_samples(settings, org, path=PLAN_SAMPLES, single_org=False):
    """Readings of the plan's usage of the org `org` (this instance's), oldest first: [(time, 5-hour share, weekly
    share, 5-hour reset, weekly reset, org)]. Each org has its own plan, so only that org's readings count:
    - the desktop app's plan-usage history (only percentages, sampled while the app polls): entries tagged with the
      org; entries without an org only when `single_org` (every org known on this machine is this one);
    - readings sessions posted (the session.measure hook) or recorded with `devtime plan-sample` (exact, with reset
      times), stored with their org; rows of "unknown" never count;
    - get_usage calls found in the org's own Claude Code transcripts (exact, numbers only).
    Missing sources are skipped. No org: no readings."""
    if not org:
        return []
    out = []
    try:
        with open(os.path.expanduser(str(settings.get("plan_usage_history") or "")), encoding="utf-8") as f:
            raw = json.load(f).get("samples") or []
        for s in raw:
            u = s.get("u") or {}
            who = s.get("org")
            if (who == org or (not who and single_org)) and isinstance(u.get("sd"), (int, float)):
                out.append((s["t"] / 1000, (u.get("fh") or 0) / 100, u["sd"] / 100, None, None, org))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                    if same_org(s.get("org"), org):
                        out.append((s["t"], s["fh"] / 100, s["sd"] / 100, s.get("fhReset"), s.get("sdReset"), org))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    have = sorted(r[0] for r in out)
    allowed = {org} | ({orgs.UNKNOWN} if settings.get("include_unknown_org") else set())
    for t, fh, sd, fh_reset, sd_reset, who in harvest_usage(desktop_orgs=orgs.desktop_index(settings),
                                                             allowed=allowed):
        if who not in allowed:
            continue
        i = bisect.bisect_left(have, t - 5)
        if i < len(have) and have[i] <= t + 5:
            continue
        out.append((t, fh / 100, sd / 100, fh_reset, sd_reset, who))
    return sorted(out)


WORK_SHARE = 0.015  # a workstream is a large piece of work if it cost this share of the weekly allowance
WORK_MIN_SHARE = 0.004  # a labelled section is one if it cost this share of the allowance (or has WORK_MIN_AGENTS agents)
WORK_MIN_AGENTS = 4
WORK_MAX = 16
WORK_RECENT_S = 48 * 3600
WORK_SECTIONS = os.path.join(paths.CACHE, "work_sections.json")
# Scheduled bookkeeping sessions of this project are not work to label.
BOOKKEEPING = ("Plan usage reading", "Usage review", "Work sections")


def work_sections(path=WORK_SECTIONS):
    """Section names assigned to transcripts: {transcript id: name, or "-" for not a piece of work}. Only names and
    ids are stored. Missing or corrupt files give {}."""
    try:
        with open(path, encoding="utf-8") as f:
            assign = json.load(f).get("assign")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(assign, dict):
        return {}
    return {str(k): v.strip() for k, v in assign.items() if isinstance(v, str) and v.strip()}


def assign_work(mapping, path=WORK_SECTIONS):
    """Merge {transcript id: section name or "-"} into the sections file (atomically). Returns how many were set."""
    mapping = {str(k): v.strip() for k, v in (mapping or {}).items() if isinstance(v, str) and v.strip()}
    assign = work_sections(path)
    assign.update(mapping)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"assign": assign}, f, indent=0)
    os.replace(tmp, path)
    return len(mapping)


def work_items(data, now, days=8, limit=250, assign=None):
    """What the section labeller needs: {sections: existing names with their stats over the window, items: unassigned
    pieces of work (subagents with a description, and main sessions that have no subagents in the window and cost at
    least $3), oldest first, at most `limit`}. Descriptions, titles, times and costs only."""
    assign = work_sections() if assign is None else assign
    lo = now - days * 86400
    trs = [t for t in data["transcripts"] if t["end"] >= lo and t["start"] <= now]
    parents = {t["parent"] for t in trs if t["role"] != "main" and t["parent"] is not None}
    clock = lambda x: datetime.datetime.fromtimestamp(x).strftime("%a %H:%M")
    secs, items = {}, []
    for t in trs:
        name = assign.get(t["id"])
        if name and name != "-":
            e = secs.setdefault(name, {"name": name, "projects": collections.Counter(), "agents": 0, "cost": 0.0,
                                       "first": t["start"], "last": t["end"]})
            e["projects"][t["project"]] += t["cost"]
            e["agents"] += 1
            e["cost"] += t["cost"]
            e["first"], e["last"] = min(e["first"], t["start"]), max(e["last"], t["end"])
            continue
        if name or str(t["title"]).startswith(BOOKKEEPING):
            continue
        if t["role"] == "main":
            if t["idx"] in parents or t["cost"] < 3:
                continue
            text = t["title"]
        else:
            text = t["description"]
        if not str(text or "").strip():
            continue
        items.append({"id": t["id"], "project": t["project"], "session": t["title"], "text": text,
                      "start": clock(t["start"]), "cost": round(t["cost"], 2), "_s": t["start"]})
    items.sort(key=lambda x: x["_s"])
    for x in items:
        del x["_s"]
    sections = [{"name": e["name"], "project": e["projects"].most_common(1)[0][0], "agents": e["agents"],
                 "cost": round(e["cost"], 2), "first": clock(e["first"]), "last": clock(e["last"])}
                for e in sorted(secs.values(), key=lambda e: e["first"])]
    return {"sections": sections, "items": items[:limit]}


def section_work(data, lo, hi, budget, assign):
    """Pieces of work from labelled sections, or None if nothing in [lo, hi] is assigned to one."""
    iv, tok, trs = data["iv"], data["tok"], data["transcripts"]
    per = {}
    for j, i in enumerate(tok["i"]):
        s = iv["s"][i]
        if not lo <= s <= hi:
            continue
        t = trs[iv["t"][i]]
        name = assign.get(t["id"])
        if not name or name == "-":
            continue
        e = per.setdefault(name, {"rows": [], "agents": {}, "projects": collections.Counter()})
        e["rows"].append((s, tok["cost"][j]))
        e["agents"][t["idx"]] = t
        e["projects"][t["project"]] += tok["cost"][j]
    if not per:
        return None
    out = []
    for name, e in per.items():
        rows = sorted(e["rows"])
        total = sum(c for _, c in rows)
        if total < WORK_MIN_SHARE * budget and len(e["agents"]) < WORK_MIN_AGENTS:
            continue
        first = min(min(t["start"] for t in e["agents"].values()), rows[0][0])
        a = max(first, lo)
        acc, b = 0.0, rows[-1][0]
        for s, c in rows:
            acc += c
            if acc >= 0.95 * total:
                b = s
                break
        if any(hi - t["end"] < 3600 for t in e["agents"].values()):  # still going
            b = hi
        out.append({"name": name, "from": a, "to": max(b, a), "cost": round(total, 2), "share": total / budget,
                    "ongoing": first - lo < 3600, "agents": len(e["agents"]),
                    "project": e["projects"].most_common(1)[0][0]})
    return out


def large_work(data, lo, hi, budget, assign=None):
    """The week's large pieces of work. With labelled sections (<data dir>/cache/work_sections.json, see work_items), those:
    each spans from the start of its first agent to when it had spent 95 % of its cost (to `hi` while an agent is
    still running). Otherwise workstreams that cost at least WORK_SHARE of the allowance, each spanning from when it
    had spent 5 % of its cost to when it had spent 95 % (so a stray early or late touch doesn't stretch it).
    [{name, from, to, cost, share, ongoing}] (sections add agents, project), at most WORK_MAX by cost, by start."""
    assign = work_sections() if assign is None else assign
    out = section_work(data, lo, hi, budget, assign) if assign else None
    if out is not None:  # sections started in the last WORK_RECENT_S always show (new work is cheap now); then by cost
        out = sorted(out, key=lambda x: (x["from"] < hi - WORK_RECENT_S, -x["cost"]))[:WORK_MAX]
        return sorted(out, key=lambda x: x["from"])
    iv, tok, names = data["iv"], data["tok"], data.get("workstreams") or []
    per = collections.defaultdict(list)
    for j, i in enumerate(tok["i"]):
        s = iv["s"][i]
        if lo <= s <= hi and iv["ws"][i] is not None:
            per[iv["ws"][i]].append((s, tok["cost"][j]))
    out = []
    for w, rows in per.items():
        total = sum(c for _, c in rows)
        name = names[w] if isinstance(w, int) and w < len(names) else str(w)
        if total < WORK_SHARE * budget or name.lower().startswith("untitled"):
            continue
        rows.sort()
        acc, a, b = 0.0, None, rows[-1][0]
        for s, c in rows:
            acc += c
            if a is None and acc >= 0.05 * total:
                a = s
            if acc >= 0.95 * total:
                b = s
                break
        if hi - rows[-1][0] < 3600:  # still going
            b = hi
        out.append({"name": name, "from": a, "to": max(b, a), "cost": round(total, 2), "share": total / budget,
                    "ongoing": rows[0][0] - lo < 3600})
    out = sorted(out, key=lambda x: -x["cost"])[:WORK_MAX]
    return sorted(out, key=lambda x: x["from"])


def guess_reset(samples):
    """A weekly reset instant (any week of it) from the weekly drops in readings without reset times, or None. Each
    drop puts the reset between the sample before it and the one after (mod 7 days); the end of the drop interval that
    the most drops agree with wins (the narrowest on a tie), so a one-off early reset does not move it."""
    spans = [(samples[i - 1][0], samples[i][0]) for i in range(1, len(samples))
             if samples[i][2] < samples[i - 1][2] - 0.03 and samples[i][2] < 0.2 and samples[i][0] - samples[i - 1][0] < WEEK]
    if not spans:
        return None
    inside = lambda c, a, b: (c - a) % WEEK <= b - a
    score = lambda c: (sum(inside(c, a, b) for a, b in spans), -min(b - a for a, b in spans if inside(c, a, b)))
    return max((b for _, b in spans), key=score)


def limits(data, idx, settings, now, samples=None):
    """Plan-limit view for the whole account: weekly usage as a share of the weekly allowance, the plan's own samples,
    the 5-hour session windows, and where the week ends at the pace since the weekly reset and since the session one.

    The next weekly reset comes from the plan's own readings when they carry one, else from settings.weekly_reset
    (any reset instant; they repeat every 7 days). The app's
    usage history (settings.plan_usage_history) calibrates it: since its last weekly drop, the reported share is fitted
    as a + cost / allowance, which gives the allowance in API-equivalent dollars and when the week really started.
    Without it, weekly_budget_usd and the 7 days before the reset are used. A 5-hour window starts where the
    reported 5-hour share drops (when sampled), else at the first request after the previous window closed."""
    configured = float(settings.get("weekly_budget_usd") or 0)
    budget = configured
    anchor = settings.get("weekly_reset")
    if samples is None:
        samples = plan_samples(settings, data.get("org"), single_org=bool(data.get("singleOrg")))
    exact = [s for s in samples if s[4]]
    guessed = None
    if not anchor and not exact:  # no reset instant configured and none reported by the plan: take the app history's
        guessed = guess_reset(samples)
        if guessed is None:
            return None
    a0 = exact[-1][4] if exact else guessed if guessed else iso_ts(anchor)
    end = a0 + math.ceil((now - a0) / WEEK) * WEEK
    if end <= now:
        end += WEEK
    # Samples since the last weekly drop.
    week, drop = [], None
    for i, s in enumerate(samples):
        if i and s[2] < samples[i - 1][2] - 0.03:
            week, drop = [], samples[i - 1][0]
        week.append(s)
    week = [s for s in week if s[0] <= now]
    week = [s for s in week if end - 9 * 86400 <= s[0] <= now]
    lo = min([end - 5 * WEEK] + [s[0] for s in week])  # four earlier weeks too, for the pooled allowance fit
    models = data["models"]
    fam_of = [fitting.family(m) for m in models]
    reqs = sorted((r[0], r[1], fam_of[r[5]]) for t, rows in idx.reqs.items()
                  for r in rows[bisect.bisect_left(idx.starts[t], lo - SESSION):])
    ts = [r[0] for r in reqs]
    cum = list(itertools.accumulate((r[1] for r in reqs), initial=0.0))
    spent_to = lambda t: cum[bisect.bisect_right(ts, t)]  # cost of requests from lo - 5 h up to t
    fam_cum = {f: list(itertools.accumulate((r[1] if r[2] == f else 0.0 for r in reqs), initial=0.0))
               for f in fitting.FAMILIES}
    fam_spent = lambda t: {f: fam_cum[f][bisect.bisect_right(ts, t)] for f in fitting.FAMILIES}

    # Fit reported share = a + spent_to(t) / allowance over this week's samples.
    fit = None
    groups = []  # the groups of (time, share) the fit used, for its uncertainty and the per-family fit
    pts = [(spent_to(s[0]), s[2]) for s in week]
    sane = lambda k: k > 0 and (not configured or 0.3 <= 1 / k / configured <= 3)
    if (len(pts) >= 3 and max(p[0] for p in pts) - min(p[0] for p in pts) >= 1
            and max(p[1] for p in pts) - min(p[1] for p in pts) >= 0.03):
        mx = statistics.fmean(p[0] for p in pts)
        my = statistics.fmean(p[1] for p in pts)
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        k = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx else 0
        if sane(k):
            budget = 1 / k
            fit = {"samples": len(pts), "allowance": budget, "residual": max(abs(my + k * (x - mx) - y) for x, y in pts)}
            groups = [[(s[0], s[2]) for s in week]]
    if fit is None:  # too little this week: the same fit over the last five weeks, each week its own intercept
        sets, cur = [], []
        for i, x in enumerate(samples):
            if i and x[2] < samples[i - 1][2] - 0.03 and x[2] < 0.2:  # a weekly reset starts a new group
                sets.append(cur)
                cur = []
            if lo <= x[0] <= now:
                cur.append((x[0], x[2]))
        sets.append(cur)
        sxy = sxx = 0.0
        n = 0
        for g in sets:
            if len(g) < 3:
                continue
            g = [(spent_to(t), y) for t, y in g]
            gx, gy = statistics.fmean(p[0] for p in g), statistics.fmean(p[1] for p in g)
            sxy += sum((p[0] - gx) * (p[1] - gy) for p in g)
            sxx += sum((p[0] - gx) ** 2 for p in g)
            n += len(g)
        k = sxy / sxx if sxx else 0
        if n >= 6 and sane(k):
            budget = 1 / k
            fit = {"samples": n, "allowance": budget, "pooled": True}
            groups = [[(t, y) for t, y in g] for g in sets if len(g) >= 3]
    if fit is not None and groups:  # how well the line explains the reported share, and what each model family weighs
        reg = fitting.regress([[(y, (spent_to(t),)) for t, y in g] for g in groups], 1)
        if reg and reg["coef"][0] > 0:
            fit["sigma"] = reg["sigma"]  # residual standard error of the reported share (0..1)
            fit["allowanceError"] = fitting.allowance(reg["coef"][0], reg["se"][0])[1]
        fam = fitting.by_family([[(y, fam_spent(t)) for t, y in g] for g in groups])
        if fam:
            fit["byFamily"] = fam["families"]
            fit["byFamilySamples"] = fam["samples"]
    if not budget and pts and pts[-1][1] >= 0.02:  # one reading: spend since the week's first reading over its share
        dc = pts[-1][0] - (spent_to(drop) if drop else spent_to(end - WEEK))
        if dc >= 1:
            budget = dc / pts[-1][1]
            fit = {"samples": 1, "allowance": budget, "single": True}
    if not budget:
        return None
    if week:
        offset = pts[-1][1] - pts[-1][0] / budget  # through the latest reading
    else:
        offset = -spent_to(end - WEEK) / budget
    share_at = lambda t: offset + spent_to(t) / budget
    # The week started where the fitted share was 0 (not before the last drop, not more than 8 days before the end).
    start = end - WEEK
    if week:
        zero = -offset * budget
        k = bisect.bisect_left(cum, zero)
        start = ts[k - 1] if 0 < k <= len(ts) else (drop or week[0][0])
        if drop and start < drop:
            start = drop
        start = min(max(start, end - 8 * 86400), now)
    share = max(0.0, share_at(now))

    step = 3600
    n = int((now - start) // step) + 1
    used = [round(max(0.0, share_at(min(start + (i + 1) * step, now))), 4) for i in range(n)]

    # 5-hour windows: chained from requests, re-anchored at each drop of the reported 5-hour share.
    drops = [samples[i][0] for i in range(1, len(samples))
             if samples[i][1] < samples[i - 1][1] - 0.02 and samples[i][0] >= start - SESSION]
    windows, open_until = [], None
    di = 0
    for s in ts:
        if s > now:
            break
        while di < len(drops) and drops[di] <= s:  # a sampled reset: the window then started at most 5 h earlier
            if open_until is None or drops[di] > open_until - SESSION + 60:
                open_until = drops[di]
            di += 1
        if open_until is None or s >= open_until:
            open_until = s + SESSION
            windows.append(s)
    sessions = [w for w in windows if w + SESSION > start]
    cur = sessions[-1] if sessions and now < sessions[-1] + SESSION else None
    if exact and exact[-1][3] and exact[-1][3] > now:  # the app said when this window resets
        cur = exact[-1][3] - SESSION
        sessions = [w for w in sessions if w + SESSION <= cur] + [cur]
    # A window only minutes old says little about pace: take the previous one in with it until it's 30 minutes old.
    base = cur if cur is not None and now - cur >= 1800 else (sessions[-2] if len(sessions) > 1 else cur)

    def pace(t0, label):
        hours = (now - t0) / 3600
        if hours <= 0:
            return None
        rate = (share - (0.0 if t0 <= start else share_at(t0))) / hours  # share of the week per hour
        hit = now + (1 - share) / rate * 3600 if rate > 0 and share < 1 else (now if share >= 1 else None)
        return {"label": label, "from": t0, "perHour": rate, "atReset": share + rate * (end - now) / 3600,
                "limitAt": hit if hit is not None and hit < end else None}
    return {"start": start, "end": end, "now": now, "step": step, "used": used, "share": share, "budget": budget,
            "allowance": budget, "allowanceError": fit.get("allowanceError") if fit else None,
            "resetGuessed": bool(guessed),
            "changes": [{"t": t, "label": label} for t, label in changes() if start <= t <= end],
            "work": large_work(data, start, now, budget),
            "fit": fit, "samples": [(round(s[0]), round(s[2], 3)) for s in week if s[0] >= start],
            "reading": {"t": week[-1][0], "share": week[-1][2], "five": week[-1][1],
                        "exact": bool(week[-1][4])} if week else None,
            "sessions": sessions,
            "session": {"start": cur, "end": cur + SESSION if cur else None,
                        "share": share - share_at(cur) if cur else 0},
            "pace": [p for p in (pace(start, "since weekly reset"),
                                 pace(base, "since session reset") if base is not None else None) if p]}


AB = re.compile(r"^AB[:\s]\s*([\w.\-]+)", re.IGNORECASE)  # "AB:R12 kerb heights", "AB:R12 fix tests"
RUNNER = re.compile(r"^\s*runner\b", re.IGNORECASE)  # general-purpose agents spawned as runners: "Runner (haiku): ..."
PLANNER = re.compile(r"^AB[:\s]\s*[\w.\-]+\s+plan\b", re.IGNORECASE)  # "AB:R12 plan", the stage's planning agent
NEW_AB = re.compile(r"^AB:[\w-]+:[\w.-]+", re.IGNORECASE)  # "AB:<experiment id>:<stage> ...", see devtime/ab.py
EXPERIMENT_DAYS = 30


def experiments(data, idx, now):
    """Model comparisons on live work. A/B stages: subagents whose description starts "AB:<stage>". A stage's arm is
    its first implementing agent's model, prefixed "<model> plan →" when a planning agent ("AB:<stage> plan ...")
    came first; its cost / time / agents include every agent of the stage (planner and fix-ups too), and fix-ups are
    implementing agents after the first. New-style experiments (devtime/ab.py, "AB:<id>:<stage>") are added to the same
    rows with an "experiment" field (their arm names are in "model"). Runner jobs: subagents of type "runner", by model."""
    trs = data["transcripts"]
    lo = now - EXPERIMENT_DAYS * 86400
    stages = {}
    runners = collections.defaultdict(list)
    for t in trs:
        if t["role"] == "main" or t["start"] < lo:
            continue
        rows = idx.reqs.get(t["idx"], [])
        if not rows:
            continue
        cost = sum(r[1] for r in rows)
        if t.get("agentType") == "runner" or RUNNER.match(t.get("description") or ""):
            runners[model_of(t)].append({"cost": cost, "minutes": (t["end"] - t["start"]) / 60,
                                         "context": statistics.median(r[3] for r in rows)})
            continue
        if NEW_AB.match(t.get("description") or ""):
            continue  # AB:<experiment>:<stage>: devtime/ab.py (below)
        m = AB.match(t.get("description") or "")
        if not m:
            continue
        key = (t.get("session"), m.group(1).lower())
        st = stages.setdefault(key, {"agents": [], "cost": 0.0, "start": t["start"], "end": t["end"], "commits": 0})
        st["agents"].append((t["start"], model_of(t), bool(PLANNER.search(t.get("description") or ""))))
        st["cost"] += cost
        st["start"], st["end"] = min(st["start"], t["start"]), max(st["end"], t["end"])
        st["commits"] += idx.commits_by_t.get(t["idx"], 0)
    by_model = collections.defaultdict(list)
    for st in stages.values():
        st["agents"].sort()
        impl = [a for a in st["agents"] if not a[2]]
        if not impl:
            continue  # only planned so far
        plan = next((a for a in st["agents"] if a[2] and a[0] <= impl[0][0]), None)
        st["fixups"] = len(impl) - 1
        by_model[(f"{plan[1]} plan → " if plan else "") + impl[0][1]].append(st)

    def summary(items, key):
        vals = [x[key] for x in items]
        return statistics.median(vals) if vals else None
    ab = [{"experiment": None, "model": m, "stages": len(v), "medianCost": summary(v, "cost"), "totalCost": sum(x["cost"] for x in v),
           "medianMinutes": statistics.median([(x["end"] - x["start"]) / 60 for x in v]),
           "rework": sum(1 for x in v if x["fixups"]) / len(v),
           "agentsPerStage": sum(len(x["agents"]) for x in v) / len(v),
           "costPerCommit": (sum(x["cost"] for x in v) / max(1, sum(x["commits"] for x in v)))
           if any(x["commits"] for x in v) else None}
          for m, v in sorted(by_model.items())]
    from . import ab as ab_module  # new-style experiments (experiments.toml), arm by hash; next to the legacy rows
    if ab_module.load_experiments():
        ab += ab_module.dashboard_rows(ab_module.agent_rows(data, idx))
    run = [{"model": m, "runs": len(v), "medianCost": summary(v, "cost"), "totalCost": sum(x["cost"] for x in v),
            "medianMinutes": summary(v, "minutes"), "medianContext": summary(v, "context")}
           for m, v in sorted(runners.items())]
    # Fixed context by agent type (last 7 days): the first request's context is the type's tool definitions, system
    # prompt, memory files and the brief; every later request re-reads it.
    types = collections.defaultdict(list)
    for t in trs:
        rows = idx.reqs.get(t["idx"])
        if t["role"] == "main" or not rows or rows[0][0] < now - 7 * 86400:
            continue
        types[t.get("agentType") or "?"].append((rows[0][3], len(rows), sum(r[1] for r in rows)))
    kinds = [{"type": k, "agents": len(v), "baseContext": statistics.median(x[0] for x in v),
              "requests": sum(x[1] for x in v), "medianCost": statistics.median(x[2] for x in v)}
             for k, v in sorted(types.items(), key=lambda kv: -sum(x[1] for x in kv[1])) if k != "?"]
    return {"days": EXPERIMENT_DAYS, "ab": ab, "runner": run, "types": kinds}


def overview(data, idx, range_key, settings, week_cost=None, now=None, plan=None):
    now = now or time.time()
    lo, hi = bounds(range_key, data, now)
    cur = period(data, idx, lo, hi)
    prev = period(data, idx, lo - (hi - lo), lo) if range_key in RANGES else None
    budget = float(settings.get("weekly_budget_usd") or 0)
    if plan:  # the plan's own week, since the last weekly reset
        week_cost, budget = plan["share"] * plan["budget"], plan["budget"]
    elif week_cost is None:
        week_cost = period(data, idx, now - 7 * 86400, now)["cost"]
    strip = lambda p: {k: v for k, v in p.items() if k != "perT"} if p else None
    return {"range": range_key, "now": now, "current": strip(cur), "previous": strip(prev),
            "week": {"cost": week_cost, "budget": budget, "share": week_cost / budget if budget else None},
            "live": live(data, idx, now), "spenders": spenders(data, cur), "trend": trend(data, idx, lo, hi),
            "experiments": experiments(data, idx, now), "limits": plan,
            "monitoring": monitoring(data, idx, lo, hi, cur["cost"]),
            "devtime": own_usage(data, idx, lo, hi, cur["cost"])}


def watch(data, idx, alerts, now, hours=3):
    """A compact, judgement-ready summary of recent work for a periodic reviewer (a small model): live sessions,
    subagents of the last few hours with their model and what kind of work they did, active alerts, the experiments.
    Descriptions, tool kinds and numbers only: no prompts, outputs or tool labels."""
    trs, iv = data["transcripts"], data["iv"]
    kinds = data["kinds"]
    lo = now - hours * 3600
    work = collections.defaultdict(collections.Counter)
    for i, t in enumerate(iv["t"]):
        if iv["s"][i] >= lo:
            work[t][kinds[iv["k"][i]]] += 1
    titles = {t["session"]: t.get("title") for t in trs if t["role"] == "main"}
    mains, subs = [], []
    for t in trs:
        rows = idx.reqs.get(t["idx"], [])
        recent = [r for r in rows if r[0] >= lo]
        if not recent:
            continue
        row = {"session": titles.get(t.get("session")) or (t.get("session") or "")[:8], "model": model_of(t),
               "live": now - rows[-1][0] < LIVE_S, "costRecent": round(sum(r[1] for r in recent), 2),
               "context": recent[-1][3], "calls": dict(work[t["idx"]].most_common(6))}
        if t["role"] == "main":
            mains.append(dict(row, project=t.get("project")))
        else:
            subs.append(dict(row, description=(t.get("description") or "")[:80], agentType=t.get("agentType"),
                             cost=round(sum(r[1] for r in rows), 2), minutes=round((rows[-1][0] - rows[0][0]) / 60),
                             edits=work[t["idx"]].get("edit code", 0), commits=idx.commits_by_t.get(t["idx"], 0)))
    subs.sort(key=lambda r: -r["cost"])
    active = [{k: a.get(k) for k in ("kind", "owner", "model", "role", "text", "advice", "first", "last")}
              for a in alerts if a.get("active")]
    mon = monitoring(data, idx, lo, now, sessions=5)
    brief = lambda r: {k: r[k] for k in ("wakes", "cost", "avgPerWake")}
    monitoring_brief = dict(brief(mon["total"]), requests=mon["total"]["requests"],
                            byTag={r["tag"]: brief(r) for r in mon["byTag"][:6]},
                            bySession=[dict(brief(r), session=r["session"]) for r in mon["bySession"]])
    return {"now": now, "hours": hours, "mains": mains, "subagents": subs[:40], "alerts": active,
            "monitoring": monitoring_brief, "experiments": experiments(data, idx, now)}


def tick_summary(data, settings, alerts, now):
    """What a periodic dashboard pass needs: the latest plan-usage reading ({weekly, fiveHour (percent), ageMin} or
    None), the number of unlabelled work items and of active alerts."""
    samples = plan_samples(settings, data.get("org"), single_org=bool(data.get("singleOrg")))
    reading = None
    if samples:
        t, fh, sd = samples[-1][:3]
        reading = {"weekly": round(sd * 100, 1), "fiveHour": round(fh * 100, 1), "ageMin": round(max(0, now - t) / 60)}
    idx = Index(data)
    lo = now - TICK_DAYS * 86400
    return {"reading": reading, "labels": len(work_items(data, now, 8, 10 ** 6)["items"]),
            "alerts": sum(1 for a in alerts if a.get("active")), "devtimeDays": TICK_DAYS,
            "devtime": own_usage(data, idx, lo, now, window_cost(idx, lo, now))}
