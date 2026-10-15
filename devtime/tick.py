"""The mechanical half of one /devtime:start pass, kept small so a loop in a session costs almost no context.

    python -m devtime tick [--session ID] [--org ORG]
                                                  make sure the server is up (no browser), print compact JSON:
        {"url", "org", "reading": {weekly, fiveHour, ageMin} | null, "labels": unlabelled work items,
         "review": {"due", "lastAt"}, "alerts": active alerts, "cap": CLAUDE.md cap | null, "setup": block exists}
        Everything is scoped to one organisation: the one of the session --session (its CLI session id), else --org,
        else the most recently active ("orgNote" says when the session was not found / not given).
    python -m devtime review-context [--hours 2] [--org ORG]
                                                  everything the reviewer needs in one call: the /api/watch summary
                                                  of that org and the last 40 lines of review-log.md
    python -m devtime review-done --text "..."    append a line to review-log.md, stamp review_state.json

The numbers come from the running server (/api/tick, /api/watch); if it does not answer they are computed locally.
Review is due when the last review is >= REVIEW_EVERY_S old or there was none.
"""

import datetime
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import orgs, paths, setup

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


def org_error(error):
    """The server refused the org (unknown, ambiguous, all not allowed): as an orgs.OrgError with its message."""
    try:
        message = json.loads(error.read().decode("utf-8")).get("error") or str(error)
    except Exception:
        message = str(error)
    return orgs.OrgError(error.code, message)


def query(**params):
    """"?a=b&..." of the parameters that are set."""
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return "?" + urllib.parse.urlencode(params) if params else ""


def tick_numbers(url, up, now, session=None, org=None):
    if up:
        try:
            return server_json(url, "/api/tick" + query(session=session, org=org))
        except urllib.error.HTTPError as error:
            if error.code in (400, 403, 404):
                raise org_error(error)
        except Exception:
            pass  # an older server without the endpoint
    from . import overview
    store = local_store()
    note = None
    if org:
        scope = store.resolve_org(org)
    else:
        scope = store.org_of_session(session) if session else None
        if not scope:
            scope = store.default_org()
            note = "session id %s; the most recent org is used" % ("not found" if session else "not given")
    out = overview.tick_summary(store.slice("*", scope), store.config.settings, store.alerts.list(scope), now, scope)
    out["org"] = scope if scope else "all"
    if note:
        out["orgNote"] = note
    return out


def tick(session=None, org=None):
    now = time.time()
    url, up = setup.ensure_server()
    out = {"url": url}
    numbers = tick_numbers(url, up, now, session, org)
    block, cap, five = setup.claude_md_status()
    out.update(org=numbers.get("org"), reading=numbers.get("reading"), labels=numbers.get("labels", 0),
               review=review_state(now), alerts=numbers.get("alerts", 0), cap=cap, fiveHourCap=five, setup=block)
    if numbers.get("orgNote"):
        out["orgNote"] = numbers["orgNote"]
    if not up:
        out["server"] = "not answering"
    print(json.dumps(out, separators=(",", ":")))


def watch_summary(hours, org=None):
    url, up = setup.ensure_server(wait_s=0)
    if up:
        try:
            return server_json(url, "/api/watch" + query(hours="%g" % hours, org=org))
        except urllib.error.HTTPError as error:
            if error.code in (400, 403, 404):
                raise org_error(error)
        except Exception:
            pass
    from . import overview
    store = local_store()
    scope = store.resolve_org(org)
    data = store.slice("*", scope)
    return overview.watch(data, overview.Index(data), store.alerts.list(scope), time.time(), hours)


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


def review_context(hours=2.0, org=None):
    try:
        with open(log_path(), encoding="utf-8") as f:
            lines = [line[:LOG_LINE] for line in f.read().splitlines()[-LOG_TAIL:]]
    except OSError:
        lines = []
    print(json.dumps({"watch": trim_watch(watch_summary(hours, org)), "reviewLog": lines}, separators=(",", ":")))


def review_done(text):
    now = time.time()
    stamp = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
    line = " ".join(str(text).split())
    with open(paths.cache_file("review-log.md"), "a", encoding="utf-8") as f:
        f.write("%s | %s\n" % (stamp, line))
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump({"lastAt": round(now, 1)}, f)
    print("review recorded at " + stamp)
