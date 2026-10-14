"""`python -m devtime analyze` and `optimize list | apply | remove`: measure usage, rank the catalogue, apply items.

    analyze [--days 7]               compact JSON: metrics (weekly $ estimates) and the catalogue ranked by estimate
    optimize list                    the catalogue, briefly
    optimize apply <id> [--dry-run]  write the item's CLAUDE.md block / settings.json env entry (diffs only with --dry-run)
    optimize remove <id>             take them out again

The catalogue is optimizations/catalogue.toml. CLAUDE.md blocks sit between <!-- devtime:opt:<id> --> and
<!-- devtime:opt-end:<id> -->; settings.json is copied to settings.json.bak before it is written. Applying or removing
records a change marker for the dashboard's weekly chart. Estimates are upper bounds ("if every such request were
fixed"), scaled to a week.
"""

import collections
import difflib
import json
import os
import re
import shutil
import statistics
import time
import tomllib

from . import setup

ROOT = setup.ROOT
CATALOGUE = os.path.join(ROOT, "optimizations", "catalogue.toml")
BASE_TARGET = 15_000  # the fixed context of a slim agent
BIG_250, BIG_400 = 250_000, 400_000
GAP_S = 300  # a subagent's prompt cache lives 5 minutes
BUCKET_S = 300
MIN_REWRITE = 50_000
JUDGEMENT = re.compile(r"\b(plan|design|spec|review|audit|architect)", re.IGNORECASE)
AB = re.compile(r"^AB[:\s]\s*[\w.\-]+", re.IGNORECASE)
GENERAL = (None, "", "general-purpose")


# ---------------------------------------------------------------------------------------------------------------------
# Catalogue and what is already applied


def load_catalogue(path=CATALOGUE):
    with open(path, "rb") as f:
        items = tomllib.load(f).get("opt") or []
    for item in items:
        for key in ("id", "title", "summary", "signal", "evidence"):
            if not item.get(key):
                raise ValueError("catalogue item %r lacks %s" % (item.get("id"), key))
        for a in item.get("apply") or []:
            if a.get("kind") not in ("claude_md", "settings_env", "note"):
                raise ValueError("catalogue item %s: unknown action kind %r" % (item["id"], a.get("kind")))
    return items


def find_item(items, item_id):
    for item in items:
        if item["id"] == item_id:
            return item
    raise SystemExit("unknown optimization %r; known: %s" % (item_id, ", ".join(i["id"] for i in items)))


def block_re(item_id):
    start, end = "<!-- devtime:opt:%s -->" % item_id, "<!-- devtime:opt-end:%s -->" % item_id
    return re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)


def make_opt_block(item_id, text, eol="\n"):
    body = "\n".join(text.strip("\n").splitlines())
    return ("<!-- devtime:opt:%s -->\n%s\n<!-- devtime:opt-end:%s -->" % (item_id, body, item_id)).replace("\n", eol)


def read_settings():
    """(settings dict, raw text or None, error or None)."""
    raw = setup.read_text(setup.settings_path())
    if raw is None:
        return {}, None, None
    try:
        data = json.loads(raw)
    except ValueError as error:
        return {}, raw, str(error)
    return (data, raw, None) if isinstance(data, dict) else ({}, raw, "settings.json is not an object")


def applied_state(item, claude_md, settings):
    """'devtime' (our block / env entry), 'manual' (CLAUDE.md says it in its own words), 'partly', or None."""
    parts = []
    for a in item.get("apply") or []:
        if a["kind"] == "claude_md":
            parts.append(bool(claude_md and block_re(item["id"]).search(claude_md)))
        elif a["kind"] == "settings_env":
            parts.append(str((settings.get("env") or {}).get(a["key"])) == str(a["value"]))
    if item["id"] == "weekly-cap":
        parts = [bool(claude_md and setup.BLOCK_RE.search(claude_md))]
    if parts and all(parts):
        return "devtime"
    if any(parts):
        return "partly"
    if item.get("detect") and claude_md and re.search(item["detect"], claude_md):
        return "manual"
    return None


# ---------------------------------------------------------------------------------------------------------------------
# apply / remove


def unified(old, new, name):
    return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True),
                                        "a/" + name if old is not None else "/dev/null", "b/" + name))


def json_text(data, raw):
    """settings.json text keeping the file's indent, key order and (non-ASCII) characters."""
    m = re.search(r"^([ \t]+)\S", raw or "", re.M)
    indent = m.group(1) if m else "  "
    eol = "\r\n" if raw and "\r\n" in raw else "\n"
    return json.dumps(data, indent=indent, ensure_ascii=False).replace("\n", eol) + eol


def plan(item, remove=False):
    """The changes an apply / remove makes: [{"file", "old", "new", "what"}] and the manual notes."""
    changes, notes = [], []
    claude_md, md_path = setup.read_text(setup.claude_md_path()), setup.claude_md_path()
    settings, raw, error = read_settings()
    new_md, new_settings = claude_md, json.loads(json.dumps(settings))
    for a in item.get("apply") or []:
        if a["kind"] == "claude_md":
            if remove:
                new_md, _ = setup.remove_block(new_md, block_re(item["id"]))
            else:
                new_md, _ = setup.upsert_block(new_md, block_re(item["id"]),
                                               lambda eol, a=a: make_opt_block(item["id"], a["text"], eol))
        elif a["kind"] == "settings_env":
            if error:
                raise SystemExit("settings.json cannot be edited: %s" % error)
            env = new_settings.setdefault("env", {})
            if remove:
                if str(env.get(a["key"])) == str(a["value"]):
                    del env[a["key"]]
                if not env:
                    del new_settings["env"]
            else:
                env[a["key"]] = str(a["value"])
        elif not remove:
            notes.append(a["text"])
    if new_md != claude_md:
        changes.append({"file": md_path, "old": claude_md, "new": new_md, "what": "CLAUDE.md"})
    if new_settings != settings:
        changes.append({"file": setup.settings_path(), "old": raw, "new": json_text(new_settings, raw),
                        "what": "settings.json", "backup": raw is not None})
    return changes, notes


def write_changes(changes):
    for c in changes:
        os.makedirs(os.path.dirname(c["file"]), exist_ok=True)
        if c.get("backup"):
            shutil.copyfile(c["file"], c["file"] + ".bak")
        with open(c["file"], "wb") as f:
            f.write(c["new"].encode("utf-8"))


def apply_cmd(item_id, dry_run=False, remove=False):
    item = find_item(load_catalogue(), item_id)
    changes, notes = plan(item, remove)
    out = {"id": item_id, "action": "remove" if remove else "apply", "dryRun": dry_run,
           "changed": [c["what"] for c in changes], "notes": notes}
    if dry_run:
        print(json.dumps(out, separators=(",", ":")))
        for c in changes:
            print(unified(c["old"], c["new"], c["what"]) or "(no change)")
        if not changes:
            print("(no change)")
        return 0
    write_changes(changes)
    if changes:
        from . import overview
        out["marker"] = overview.record_change(("Removed " if remove else "Applied ") + item["title"], None,
                                               overview.CHANGES)
    else:
        out["marker"] = None
    print(json.dumps(out, separators=(",", ":")))
    return 0


def list_cmd():
    claude_md = setup.read_text(setup.claude_md_path())
    settings = read_settings()[0]
    for item in load_catalogue():
        state = applied_state(item, claude_md, settings)
        print("%-24s %-8s %s" % (item["id"], state or "-", item["title"]))
        print("    " + item["summary"])
    return 0


def optimize_main(args):
    if args.opt_cmd == "list":
        return list_cmd()
    return apply_cmd(args.id, getattr(args, "dry_run", False), args.opt_cmd == "remove")


# ---------------------------------------------------------------------------------------------------------------------
# analyze


def normalize_label(text):
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"']*|(?:/[\w.\-]+){2,}", "<path>", str(text))
    text = re.sub(r"\b[0-9a-f]{7,}\b", "<id>", text)
    text = re.sub(r"^cd <path>\s*(&&|;)\s*", "", " ".join(text.split()))
    return re.sub(r"\d+", "#", text)[:70]


def family(model):
    parts = model.replace("claude-", "").split("-")
    return parts[0] if parts else model


def rates(config, model):
    return config.price(model) or {"input": 0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0}


def req_cost(r, rt):
    return (r["in"] * rt["input"] + r["out"] * rt["output"] + r["cr"] * rt["cache_read"]
            + r["cw5"] * rt["cache_write_5m"] + r["cw1"] * rt["cache_write_1h"]) / 1e6


def collect(data, config, lo, hi):
    """Per-transcript request rows in [lo, hi): dicts with start, model, in/out/cr/cw5/cw1, cost, ctx."""
    iv, tok, models = data["iv"], data["tok"], data["models"]
    rows = collections.defaultdict(list)
    for j, i in enumerate(tok["i"]):
        s = iv["s"][i]
        if not lo <= s < hi:
            continue
        rows[iv["t"][i]].append({"s": s, "model": models[tok["m"][j]], "in": tok["in"][j], "out": tok["out"][j],
                                 "cr": tok["cr"][j], "cw5": tok["cw5"][j], "cw1": tok["cw1"][j],
                                 "cost": tok["cost"][j],
                                 "ctx": tok["in"][j] + tok["cr"][j] + tok["cw5"][j] + tok["cw1"][j]})
    for r in rows.values():
        r.sort(key=lambda x: x["s"])
    return rows


def metrics(data, config, now, days, claude_md, settings):
    """The measurements and the per-item weekly $ estimates. Returns (metrics dict, {item id: (estimate, why)})."""
    lo, week = now - days * 86400, 7.0 / days
    trs, iv = data["transcripts"], data["iv"]
    kinds, flags = data["kinds"], data["flags"]
    waiting = set(data["waiting"])
    rows = collect(data, config, lo, now + 1)
    # a forked session copies its subagents' transcripts (same agent id): count the one with a description
    seen, keep = set(), []
    for t in sorted(rows, key=lambda t: (not trs[t].get("description"), -trs[t]["end"])):
        if trs[t]["id"] in seen:
            continue
        seen.add(trs[t]["id"])
        keep.append(t)
    rows = {t: rows[t] for t in keep}
    calls = collections.defaultdict(list)  # transcript -> (start, end, kind, flags, label index)
    for i, t in enumerate(iv["t"]):
        if t in rows and iv["e"][i] >= lo:
            calls[t].append((iv["s"][i], iv["e"][i], kinds[iv["k"][i]], iv["f"][i], iv["l"][i]))
    labels = data["labels"]

    total = sum(r["cost"] for rs in rows.values() for r in rs)
    by_model, by_role = collections.Counter(), collections.Counter()
    big250 = big400 = 0.0
    save250 = save400 = 0.0  # cost of the context above the threshold (cache reads at the model's price)
    over250_agents = set()
    base_by_type = collections.defaultdict(list)  # agent type -> [(first context, requests)]
    slim_save = 0.0
    general_requests = 0
    general_bases = []
    churn_job = churn_poll = 0.0
    churn_gaps = 0
    wait_s = busy_s = 0.0
    readonly_cost = readonly_haiku = 0.0
    readonly_n = 0
    coding_opus = coding_sonnet = 0.0
    coding_n = 0
    haiku = rates(config, "claude-haiku-5")
    sonnet = rates(config, "claude-sonnet-5")
    failing = collections.Counter()
    main_shell = collections.Counter()
    buckets = collections.defaultdict(set)
    for t, rs in rows.items():
        tr = trs[t]
        sub = tr["role"] != "main"
        atype = tr.get("agentType") if sub else "main"
        by_role["subagent" if sub else "main"] += sum(r["cost"] for r in rs)
        for r in rs:
            by_model[family(r["model"])] += r["cost"]
            rt = rates(config, r["model"])
            if r["ctx"] > BIG_250:
                big250 += r["cost"]
                if sub:
                    save250 += (r["ctx"] - BIG_250) * rt["cache_read"] / 1e6
            if r["ctx"] > BIG_400:
                big400 += r["cost"]
                save400 += (r["ctx"] - BIG_400) * rt["cache_read"] / 1e6
        for b in {int(r["s"] // BUCKET_S) for r in rs}:
            buckets[b].add(tr["id"])
        if sub and max(r["ctx"] for r in rs) > BIG_250:
            over250_agents.add(t)
        if sub:
            base = rs[0]["ctx"]
            base_by_type[atype or "?"].append((base, len(rs)))
            if atype in GENERAL and base > BASE_TARGET:
                general_requests += len(rs)
                general_bases.append(base)
                slim_save += sum(max(0, min(base, r["ctx"]) - BASE_TARGET) * rates(config, r["model"])["cache_read"]
                                 for r in rs[1:]) / 1e6
        cl = calls.get(t, [])
        if sub:
            for c in cl:
                if c[3] & flags["wait"] or c[2] == "loop / wakeup":
                    wait_s += c[1] - c[0]
            busy_s += sum(c[1] - c[0] for c in cl if c[2] not in waiting and c[2] != "overhead")
            polls = [c for c in cl if c[3] & flags["wait"] or c[2] == "loop / wakeup"]
            if atype != "runner":
                for a, b in zip(rs, rs[1:]):
                    if b["s"] - a["s"] > GAP_S and b["cw5"] + b["cw1"] >= MIN_REWRITE:
                        rt = rates(config, b["model"])
                        cost = (b["cw5"] * rt["cache_write_5m"] + b["cw1"] * rt["cache_write_1h"]) / 1e6
                        churn_gaps += 1
                        spent = sum(min(c[1], b["s"]) - max(c[0], a["s"]) for c in polls
                                    if c[0] < b["s"] and c[1] > a["s"])
                        if spent > 0.5 * (b["s"] - a["s"]):
                            churn_poll += cost
                        else:
                            churn_job += cost
            desc = tr.get("description") or ""
            has_edit = any(c[2] == "edit code" for c in cl)
            real = [c for c in cl if c[2] not in waiting and c[2] != "overhead"]
            cost = sum(r["cost"] for r in rs)
            fam = family(rs[-1]["model"])
            if not has_edit and fam != "haiku" and atype not in ("runner", "reader") and len(real) >= 12 \
                    and cost >= 0.5 and not JUDGEMENT.search(desc) and not AB.match(desc):
                readonly_n += 1
                readonly_cost += cost
                readonly_haiku += sum(req_cost(r, haiku) for r in rs)
            if has_edit and fam == "opus":
                coding_n += 1
                coding_opus += cost
                coding_sonnet += sum(req_cost(r, sonnet) for r in rs)
        else:
            for c in cl:
                if c[2] in ("build", "tests"):
                    main_shell[c[2]] += 1
        for c in cl:
            label = normalize_label(labels[c[4]]) if c[3] & flags["error"] and c[2] != "model" else ""
            if label:
                failing[(atype or "?", label)] += 1
    recurring = [(k, n) for k, n in failing.most_common() if n >= 3]
    counts = sorted(len(v) for v in buckets.values())
    peak = counts[-1] if counts else 0
    p90 = counts[int(0.9 * (len(counts) - 1))] if counts else 0
    typical = int(statistics.median(counts)) if counts else 0
    r2 = lambda x: round(x, 2)
    w = lambda x: round(x * week)  # weekly dollars
    env = settings.get("env") or {}
    types = {k: {"agents": len(v), "base": int(statistics.median(x[0] for x in v)), "req": sum(x[1] for x in v)}
             for k, v in sorted(base_by_type.items(), key=lambda kv: -sum(x[1] for x in kv[1]))[:6]}
    m = {"days": days, "weeklyUsd": w(total), "byModel": {k: w(v) for k, v in by_model.most_common(5)},
         "byRole": {k: w(v) for k, v in by_role.items()},
         "baseCtxByType": types,
         "big": {"over250kShare": r2(big250 / total) if total else 0, "over400kShare": r2(big400 / total) if total else 0,
                 "subagentsOver250k": len(over250_agents)},
         "waiting": {"subagentWaitH": round(wait_s * week / 3600), "subagentBusyH": round(busy_s * week / 3600),
                     "rewriteGaps": round(churn_gaps * week), "rewriteUsd": w(churn_job + churn_poll),
                     "pollingRewriteUsd": w(churn_poll)},
         "readonlyBig": {"agents": readonly_n, "usd": w(readonly_cost), "atHaikuUsd": w(readonly_haiku)},
         "opusCoding": {"agents": coding_n, "usd": w(coding_opus), "atSonnetUsd": w(coding_sonnet)},
         "concurrent": {"peak": peak, "p90": p90, "typical": typical},
         "autoCompactWindow": env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"),
         "recurringFailures": {"count": len(recurring),
                               "top": [{"agent": k[0], "label": k[1][:50], "n": n} for k, n in recurring[:3]]}}
    est = {
        "slim-agents": (w(slim_save), "%d general-purpose requests a week carry ~%dk base context vs 15k"
                        % (round(general_requests * week), (statistics.median(general_bases) if general_bases else 0) // 1000)),
        "runner-for-long-jobs": (w(churn_job + churn_poll), "%d cache re-writes a week after waits over 5 min"
                                 % round(churn_gaps * week)),
        "readonly-on-haiku": (w(readonly_cost - readonly_haiku), "%d read-only subagents on bigger models cost $%d (Haiku: $%d)"
                              % (round(readonly_n * week), w(readonly_cost), w(readonly_haiku))),
        "handoff-250k": (w(save250), "%.0f%% of cost is requests above 250k; %d subagents went past it"
                         % (100 * big250 / total if total else 0, len(over250_agents))),
        "autocompact-400k": (w(save400), "%.0f%% of cost is requests above 400k context" % (100 * big400 / total if total else 0)),
        "scripts-first": (None, "%d build / test calls in main sessions" % round(sum(main_shell.values()) * week)),
        "parallelism": (None, "peak %d agents in 5 min, typical %d, p90 %d" % (peak, typical, p90)),
        "sonnet-default-coding": (w(coding_opus - coding_sonnet) if coding_n else None,
                                  "%d Opus coding agents cost $%d (Sonnet: $%d); A/B first"
                                  % (round(coding_n * week), w(coding_opus), w(coding_sonnet))),
        "weekly-cap": (None, "cap block %s" % ("present" if claude_md and setup.BLOCK_RE.search(claude_md) else "missing")),
        "background-not-polling": (w(churn_poll), "%d agent hours a week in poll / sleep calls" % round(wait_s * week / 3600)),
    }
    return m, est


def analyze(days=7.0, now=None, store=None):
    from . import overview
    if store is None:
        from .store import Store
        store = Store(verbose=False)
        for attempt in range(5):
            try:
                store.refresh()
                break
            except PermissionError:  # the server saving its cache at the same moment
                time.sleep(1)
        else:
            store.refresh()
    now = now or time.time()
    data = store.slice("*")
    claude_md = setup.read_text(setup.claude_md_path())
    settings, _, _ = read_settings()
    m, est = metrics(data, store.config, now, days, claude_md, settings)
    items = []
    for item in load_catalogue():
        state = applied_state(item, claude_md, settings)
        usd, why = est.get(item["id"], (None, ""))
        items.append({"id": item["id"], "applied": state is not None and state != "partly", "by": state,
                      "estWeeklyUsd": usd, "why": why})
    items.sort(key=lambda x: (x["estWeeklyUsd"] is None, -(x["estWeeklyUsd"] or 0)))
    m["catalogue"] = items
    return m


def analyze_cmd(days):
    print(json.dumps(analyze(days), separators=(",", ":")))
    return 0
