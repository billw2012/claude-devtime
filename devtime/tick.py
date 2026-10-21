"""The mechanical half of one /devtime:start pass, kept small so a loop in a session costs almost no context.

    python -m devtime tick                       make sure this org's server is up (no browser), print compact JSON:
        {"url", "org": {id, name}, "reading": {weekly, fiveHour, ageMin} | null, "labels": unlabelled work items,
         "review": {"due", "lastAt"}, "alerts": active alerts, "cap": CLAUDE.md fixed weekly cap | null, "weekly": {mode: reserve | cap, value} | null (the
         CLAUDE.md rule), "weeklyUsable": percent usable now | null, "setup": block exists}
        Everything is the current org's instance (paths.py). If the org can't be determined it prints
        {"refused": true, "error": ...} and exits 2 without doing anything.
    python -m devtime review-context [--hours 2]  everything the reviewer needs in one call: the /api/watch summary
                                                  and the last lines of review-log.md
    python -m devtime review-done --text "..."    append a line to review-log.md, stamp review_state.json

The numbers come from the running server (/api/tick, /api/watch); if it does not answer they are computed locally.
Review is due when the last review is >= REVIEW_EVERY_S old or there was none.
"""

import datetime
import json
import os
import time
import urllib.parse
import urllib.request

from . import paths, setup

REVIEW_EVERY_S = 2 * 3600
LOG_TAIL = 12
LOG_LINE = 220
MAX_SUBAGENTS = 25


def state_path():
    return os.path.join(paths.CACHE, "review_state.json")


def log_path():
    return os.path.join(paths.CACHE, "review-log.md")


def server_json(url, path, timeout=60):
    with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def review_state(now):
    try:
        with open(state_path(), encoding="utf-8") as f:
            last = float(json.load(f).get("lastAt"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {"due": True, "lastAt": None}
    return {"due": now - last >= REVIEW_EVERY_S, "lastAt": datetime.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")}


def local_store():
    from .store import Store
    store = Store(verbose=False)
    store.refresh()
    return store


def query(**params):
    """"?a=b&..." of the parameters that are set."""
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return "?" + urllib.parse.urlencode(params) if params else ""


def tick_numbers(url, up, now):
    if up:
        try:
            out = server_json(url, "/api/tick" + query(org=paths.ORG))
            if out.get("org") == paths.ORG:  # our own org's server (the port came from this instance's port.txt)
                return out
        except Exception:
            pass  # an older server without the endpoint, or one that refused
    from . import overview
    store = local_store()
    return overview.tick_summary(store.slice("*"), store.config.settings, store.alerts.list(), now)


def refuse_without_org():
    """Prints the refusal and returns 2 if the current org is unknown, else None."""
    if paths.ORG:
        return None
    print(json.dumps({"refused": True, "error": paths.NO_ORG_TEXT}, separators=(",", ":")))
    return 2


def tick():
    refused = refuse_without_org()
    if refused:
        return refused
    from . import migrate
    migrate.run()
    now = time.time()
    url, up = setup.ensure_server()
    out = {"url": url, "org": {"id": paths.ORG, "name": paths.org_name(paths.ORG)}}
    numbers = tick_numbers(url, up, now)
    block, cap, five = setup.claude_md_status()
    out.update(reading=numbers.get("reading"), labels=numbers.get("labels", 0), review=review_state(now),
               alerts=numbers.get("alerts", 0), cap=cap, fiveHourCap=five, weekly=setup.claude_md_weekly(),
               weeklyUsable=numbers.get("weeklyUsable"), setup=block)
    if not up:
        out["server"] = "not answering"
    print(json.dumps(out, separators=(",", ":")))


def watch_summary(hours):
    url, up = setup.ensure_server(wait_s=0)
    if up:
        try:
            return server_json(url, "/api/watch" + query(hours="%g" % hours, org=paths.ORG))
        except Exception:
            pass
    from . import overview
    store = local_store()
    data = store.slice("*")
    return overview.watch(data, overview.Index(data), store.alerts.list(), time.time(), hours)


def rounded(x, nd=1):
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: rounded(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [rounded(v, nd) for v in x]
    return x


def trim_watch(watch):
    """The reviewer's copy of the /api/watch summary, under ~5 KB: the 25 most expensive subagents, fewer fields,
    rounded numbers, no per-agent-type table."""
    top = lambda calls, n: dict(list(calls.items())[:n])
    mains = [{"session": m["session"], "model": m["model"], "live": m["live"], "cost": m["costRecent"],
              "ctxK": m["context"] // 1000, "calls": top(m["calls"], 3)} for m in watch.get("mains", [])]
    subs = sorted(watch.get("subagents", []), key=lambda r: -r["cost"])[:MAX_SUBAGENTS]
    subs = [{"session": r["session"], "desc": (r.get("description") or "")[:50], "type": r.get("agentType"),
             "model": r["model"], "cost": r["cost"], "min": r["minutes"], "ctxK": r["context"] // 1000,
             "edits": r["edits"], "calls": top(r["calls"], 3)} for r in subs]
    exp = watch.get("experiments") or {}
    return rounded({"hours": watch.get("hours"), "mains": mains, "subagents": subs, "alerts": watch.get("alerts", []),
                    "monitoring": watch.get("monitoring"), "ab": exp.get("ab"), "runner": exp.get("runner")})


def review_context(hours=2.0):
    try:
        with open(log_path(), encoding="utf-8") as f:
            lines = [line[:LOG_LINE] for line in f.read().splitlines()[-LOG_TAIL:]]
    except OSError:
        lines = []
    print(json.dumps({"watch": trim_watch(watch_summary(hours)), "reviewLog": lines}, separators=(",", ":")))


def review_done(text):
    now = time.time()
    stamp = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    line = " ".join(str(text).split())
    with open(paths.cache_file("review-log.md"), "a", encoding="utf-8") as f:
        f.write("%s | %s\n" % (stamp, line))
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump({"lastAt": round(now, 1)}, f)
    print("review recorded at " + stamp)
