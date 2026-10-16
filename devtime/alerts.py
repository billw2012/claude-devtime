"""Usage alerts: live transcripts using more than they ought to, from the built dataset.

The server runs check() after every rebuild and writes <data dir>/cache/alerts.json; hooks/agent_budget.py (user-wide, every
tool call) delivers each new alert once to the session that owns it: to the subagent itself and to its main session,
which can stop it, re-brief it or take its long jobs over. The dashboard shows them too (/api/alerts).

Defaults are set so that only unusually expensive agents trigger (all configurable in [settings.alerts]):

- agent cost: a subagent past $40, then each doubling.
- cache churn: a subagent re-wrote its whole cache after waits over 5 min (its cache lives 5 min) N times in the last
  hour, costing more than $X.
- big context: a subagent's recent requests carry over N tokens of context (each call re-reads all of it).
- burn: a transcript spent more than $X in the last 30 min.
- machine burn: everything together spent more than $X in the last hour (dashboard only; no single owner).
- read-only on a big model: a non-Haiku subagent with many tool calls and no edits (not a planner or reviewer).
- untagged coding stage: a subagent writing code without an AB:<stage> tag in a session running the model A/B.
- polling from the main session: a big-context main session checking on running jobs again and again.
"""

import hashlib
import json
import os
import re
import statistics
import time

from . import paths

NOTES = os.path.join(paths.CACHE, "review_notes.jsonl")
NOTE_S = 3 * 3600  # a note is delivered if its session makes a tool call within this long


def record_note(session, text, path=NOTES, org=None):
    """A periodic reviewer's note for a main session (by title) of this instance's org (`org`, default paths.ORG);
    check() turns it into an alert for that session."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {"t": round(time.time(), 1), "session": session, "text": text.strip(), "org": org or paths.ORG}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return f"noted for '{session}'"


def notes(now, path=NOTES):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    n = json.loads(line)
                except ValueError:
                    continue
                if now - NOTE_S <= n.get("t", 0) <= now:
                    out.append(n)
    except OSError:
        pass
    return out

DEFAULTS = {
    "agent_cost": 40.0,  # $; alert again at each doubling
    "churn_count": 2, "churn_cost": 5.0,  # re-writes after >5 min gaps in the last hour, and their $
    "churn_gap_s": 300, "churn_min_tokens": 50_000,
    "context": 300_000,  # subagent: median context of its last few requests
    "burn_sub": 15.0, "burn_main": 25.0,  # $ in the last 30 min
    "machine_burn": 80.0,  # $ in the last hour, all sessions
    "live_s": 900,  # only transcripts with a request this recent
    "keep_s": 6 * 3600,  # how long resolved alerts stay listed
    "readonly_calls": 12, "readonly_cost": 0.5,  # a non-Haiku subagent with this many calls and no edit
    "poll_count": 4, "poll_context": 150_000,  # main session: job checks in the last 30 min, at this context
}

AB = re.compile(r"^AB[:\s]\s*[\w.\-]+", re.IGNORECASE)
RUNNER = re.compile(r"^\s*runner\b", re.IGNORECASE)
# Read-only work that still wants a strong model: planning, design, review.
JUDGEMENT = re.compile(r"\b(plan|design|spec|review|audit|architect)", re.IGNORECASE)
# A shell call that checks on a running job rather than doing work.
POLL = re.compile(r"\bcheck(ing)? (on |whether |if )?(the )?([\w./-]+ ){0,2}(progress|status|result)|\bstill running|"
                  r"\bis [\w ]{0,20}(done|finished|running)\b|\bwait(ing)? (for|on|until)\b|\btail -f\b|-Wait\b",
                  re.IGNORECASE)
# Agents that will write code but may not have yet: they explore first.
IMPLEMENT = re.compile(r"\b(implement|fix|add|build|write|port|refactor|wire|make|create|update|stage)", re.IGNORECASE)
# Kinds only the main session is told about: the agent itself can't act on them.
MAIN_ONLY = {"big context", "read-only on a big model", "untagged coding stage"}

ADVICE = {
    "agent cost": "if it is still on the same stage, let it finish the current step and hand off to a fresh agent; "
                  "if it is mostly waiting, stop it and run its long jobs from the main session",
    "cache churn": "it is waiting on slow jobs and re-writing its whole context each time it wakes: hand the jobs to "
                   "the main session (background, 1 h cache) and end the agent",
    "big context": "finish the step, commit, hand off to a fresh agent with a short brief",
    "burn": "check what it is doing: a poll loop, re-reading big files or images, or an Opus agent on routine work",
    "read-only on a big model": "read-only work (searching, reading files or logs, checking progress) goes to the "
                                "\"reader\" agent type (Haiku, slim) from now on",
    "untagged coding stage": "tag comparable coding stages AB:<stage> and follow the arm rotation; leave only "
                             "trivial edits untagged",
    "polling from the main session": "each check re-reads the whole context: hand the wait to a runner "
                                     "(\"Runner (haiku): ...\") that reports back when the job ends, and check less often",
}


def fmt_money(x):
    return f"${x:.0f}" if x >= 10 else f"${x:.1f}"


def check(data, config, now=None):
    """[{key, level, kind, org, project, session, agent, owner, title, text, advice, value, since}] of the dataset
    (one org's instance; alerts carry their transcript's org). Review notes count only if recorded for the dataset's
    org (data["org"])."""
    now = now or time.time()
    th = dict(DEFAULTS, **(config.settings.get("alerts") or {}))
    iv, tok, trs, models = data["iv"], data["tok"], data["transcripts"], data["models"]
    lo = now - 3600
    recent = {}  # transcript -> [(start, cost, cache write, context, model)] in the last hour
    totals = {}
    for j, i in enumerate(tok["i"]):
        t = iv["t"][i]
        s = iv["s"][i]
        if s <= now:
            totals[t] = totals.get(t, 0.0) + tok["cost"][j]
        if lo <= s <= now:
            recent.setdefault(t, []).append((s, tok["cost"][j], tok["cw5"][j] + tok["cw1"][j],
                                             tok["in"][j] + tok["cr"][j] + tok["cw5"][j] + tok["cw1"][j],
                                             tok["m"][j]))
    kinds, labels = data["kinds"], data["labels"]
    by_t = {}  # live-ish transcript -> its tool-call intervals
    for i, t in enumerate(iv["t"]):
        if t in recent:
            by_t.setdefault(t, []).append(i)
    ab_sessions = {tr.get("session") for tr in trs
                   if tr["role"] != "main" and tr["end"] >= now - 86400 and AB.match(tr.get("description") or "")}
    out = []
    machine = 0.0
    seen = set()
    # a forked session copies its subagents' transcripts (same agent id, no description): keep the original
    for t, rows in sorted(recent.items(), key=lambda kv: (not trs[kv[0]].get("description"), -trs[kv[0]]["end"])):
        tr = trs[t]
        if tr["id"] in seen:
            continue
        seen.add(tr["id"])
        rows.sort()
        machine += sum(r[1] for r in rows)
        if now - rows[-1][0] > th["live_s"]:
            continue
        sub = tr["role"] != "main"
        name = (tr.get("description") or tr.get("title") or tr["id"][:8]) if sub else (tr.get("title") or "main")
        model = models[rows[-1][4]].replace("claude-", "") if isinstance(rows[-1][4], int) else str(rows[-1][4])
        base = {"org": tr.get("org"), "project": tr.get("project"), "session": tr.get("session"),
                "agent": tr["id"] if sub else None, "owner": name, "model": model, "role": "subagent" if sub else "main"}

        def add(kind, level, value, text):
            out.append(dict(base, key=f"{tr['id']}:{kind}:{level}", kind=kind, level=level, value=round(value, 2),
                            title=f"{'subagent' if sub else 'session'} '{name}' ({model}): {kind}", text=text,
                            advice=ADVICE.get(kind, ""), since=now))

        total = totals.get(t, 0.0)
        if sub and total >= th["agent_cost"]:
            level = int(total // th["agent_cost"]).bit_length()  # 40 -> 1, 80 -> 2, 160 -> 3 ...
            add("agent cost", level, total, f"has cost {fmt_money(total)} so far")
        if sub:
            gaps, churn = 0, 0.0
            for a, b in zip(rows, rows[1:]):
                if b[0] - a[0] > th["churn_gap_s"] and b[2] >= th["churn_min_tokens"]:
                    gaps += 1
                    churn += b[1]
            if gaps >= th["churn_count"] and churn >= th["churn_cost"]:
                add("cache churn", gaps // th["churn_count"], churn,
                    f"re-wrote its cache after {gaps} waits over {th['churn_gap_s'] // 60} min in the last hour "
                    f"({fmt_money(churn)})")
            ctx = statistics.median(r[3] for r in rows[-5:])
            if ctx >= th["context"]:
                add("big context", int(ctx // th["context"]), ctx, f"is working with ~{ctx / 1000:.0f}k tokens of "
                    f"context per call")
        half = sum(r[1] for r in rows if r[0] >= now - 1800)
        limit = th["burn_sub"] if sub else th["burn_main"]
        if half >= limit:
            add("burn", int(half // limit), half, f"spent {fmt_money(half)} in the last 30 min")

        calls = [(iv["s"][i], kinds[iv["k"][i]], labels[iv["l"][i]]) for i in by_t.get(t, [])]
        desc = tr.get("description") or ""
        runner = tr.get("agentType") == "runner" or RUNNER.match(desc)
        if sub and not runner and "haiku" not in model and not JUDGEMENT.search(desc) and not AB.match(desc) \
                and not IMPLEMENT.search(desc):
            edits = sum(1 for c in calls if c[1] == "edit code")
            if not edits and len(calls) >= th["readonly_calls"] and total >= th["readonly_cost"]:
                add("read-only on a big model", 1, total,
                    f"has made {len(calls)} tool calls, none of them edits, on {model} ({fmt_money(total)})")
        if sub and not runner and not AB.match(desc) and tr.get("session") in ab_sessions \
                and any(c[1] == "edit code" for c in calls):
            add("untagged coding stage", 1, total, "is writing code without an AB:<stage> tag while this session "
                "runs the model A/B")
        if not sub:
            polls = [c for c in calls if now - 1800 <= c[0] <= now and c[1] not in ("git", "edit code")
                     and POLL.search(c[2])]
            ctx = rows[-1][3]
            if len(polls) >= th["poll_count"] and ctx >= th["poll_context"]:
                each = statistics.median(r[1] for r in rows[-10:])
                add("polling from the main session", len(polls) // th["poll_count"], len(polls),
                    f"checked on running jobs {len(polls)} times in the last 30 min with ~{ctx / 1000:.0f}k "
                    f"context (~{fmt_money(each)} a call)")
    # Notes from the periodic usage review: to the most recent main session with that title.
    mains = {}
    for tr in trs:
        if tr["role"] == "main" and tr.get("title") and tr["end"] >= mains.get(tr["title"], {}).get("end", 0):
            mains[tr["title"]] = tr
    mine = data.get("org")
    for n in notes(now):
        if not mine or n.get("org") != mine:  # a note of no org or of another org reaches nobody
            continue
        tr = mains.get(n.get("session"))
        if not tr:
            continue
        digest = hashlib.sha1(f"{n['t']}:{n['text']}".encode()).hexdigest()[:10]
        out.append({"key": f"review:{tr['session']}:{digest}", "kind": "usage review", "level": 1, "value": 0,
                    "org": tr.get("org"), "project": tr.get("project"), "session": tr.get("session"), "agent": None,
                    "owner": n["session"], "model": "", "role": "main",
                    "title": f"session '{n['session']}': usage review", "text": f"has a usage review note: {n['text']}",
                    "advice": "act on it at the next natural break if it holds; the reviewer only sees numbers and "
                              "descriptions, so ignore it if it misreads the work", "since": n["t"]})
    if machine >= th["machine_burn"]:
        level = int(machine // th["machine_burn"])
        out.append({"key": f"machine:burn:{level}", "kind": "machine burn", "level": level,
                    "value": round(machine, 2), "org": mine, "project": None, "session": None, "agent": None,
                    "owner": "all sessions", "model": "", "role": "machine",
                    "title": "all sessions: machine burn", "text": f"spent {fmt_money(machine)} in the last hour",
                    "advice": "too many agents at once; limit to 2-3 across all sessions", "since": now})
    return out


class Alerts:
    """Keeps the alert list across rebuilds (first seen, last seen, resolved) and writes it for the hooks."""

    def __init__(self, path):
        self.path = path
        self.items = {}
        try:
            with open(path, encoding="utf-8") as handle:
                self.items = {a["key"]: a for a in json.load(handle).get("alerts", [])}
        except (OSError, ValueError):
            pass

    def update(self, data, config, now=None):
        now = now or time.time()
        th = dict(DEFAULTS, **(config.settings.get("alerts") or {}))
        current = check(data, config, now)
        for a in current:
            old = self.items.get(a["key"])
            a["first"] = old["first"] if old else now
            a["last"] = now
            a["active"] = True
            self.items[a["key"]] = a
        live = {a["key"] for a in current}
        for key, a in list(self.items.items()):
            if key not in live:
                a["active"] = False
                if now - a["last"] > th["keep_s"]:
                    del self.items[key]
        self.save(now)
        return current

    def list(self):
        """All alerts of this instance, active first."""
        return sorted(self.items.values(), key=lambda a: (not a["active"], -a["last"], -a["value"]))

    def save(self, now):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"generated": now, "alerts": self.list()}, handle)
        for _ in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:  # a hook is reading it
                time.sleep(0.1)
        os.remove(tmp)
