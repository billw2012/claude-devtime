"""`python -m devtime analyze` and `optimize list | apply | remove | adopt`: measure usage, rank the catalogue, apply items.

    analyze [--days 7]                 compact JSON: metrics (weekly $ estimates) and the catalogue ranked by estimate
    optimize list                      each catalogue item and adopted / custom block: state and what remove would do
    optimize apply <id> [--dry-run]    write the item's CLAUDE.md block / settings.json env entry (diffs only with --dry-run)
    optimize remove <id> [--dry-run]   take them out again; settings.json values go back to what they were before
    optimize remove all [--dry-run]    every opt block, every recorded settings change and the devtime:start block
    optimize adopt <id> --heading H [--dry-run]
                                       wrap the hand-written CLAUDE.md section under heading H in the tags for <id>
                                       (a catalogue id or a custom one: lowercase letters, digits, dashes)

The catalogue is optimizations/catalogue.toml. CLAUDE.md blocks sit between <!-- devtime:opt:<id> --> and
<!-- devtime:opt-end:<id> -->; settings.json is copied to settings.json.bak before it is written. The install ledger
<data dir>/config/installed.json records each settings change as {key, before, after} (before null = it was absent) and
each adopted section: remove restores `before` if the value is still `after`, leaves a value you changed since, and
leaves settings alone when there is no record. Applying or removing records a change marker for the dashboard's weekly
chart. Estimates are upper bounds ("if every such request were fixed"), scaled to a week.
"""

import collections
import datetime
import difflib
import json
import os
import re
import shutil
import statistics
import time
import tomllib

from . import config, paths, setup

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


def offered(item):
    """Catalogue items with `requires = "parallel"` only make sense with parallel support on (settings.parallel)."""
    return item.get("requires") != "parallel" or config.parallel()


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
# Install ledger: what settings.json held before an apply, and which CLAUDE.md sections were adopted


ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ANY_OPT_RE = re.compile(r"<!-- devtime:opt:([a-z0-9][a-z0-9-]*) -->.*?<!-- devtime:opt-end:\1 -->", re.S)
MISSING = object()


def ledger_path():
    return os.path.join(paths.DATA, "config", "installed.json")


def read_ledger():
    """(ledger dict {"items": [...]}, raw text or None)."""
    raw = setup.read_text(ledger_path())
    if raw is None:
        return {"items": []}, None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("not a ledger")
    except ValueError as error:
        raise SystemExit("%s cannot be read (%s); fix or delete it" % (ledger_path(), error))
    data.setdefault("items", [])
    return data, raw


def ledger_entry(ledger, item_id):
    return next((e for e in ledger["items"] if e.get("id") == item_id), None)


def with_entry(ledger, entry):
    items = [e for e in ledger["items"] if e.get("id") != entry["id"]]
    items.append(entry)
    return dict(ledger, items=items)


def without_entry(ledger, item_id):
    return dict(ledger, items=[e for e in ledger["items"] if e.get("id") != item_id])


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_change(old_ledger, new_ledger, raw):
    if new_ledger == old_ledger or (raw is None and not new_ledger["items"]):
        return None
    return {"file": ledger_path(), "old": raw, "new": json.dumps(new_ledger, indent=2, ensure_ascii=False) + "\n",
            "what": "installed.json", "internal": True}


def restore_settings(env, entry, report):
    """Undo a ledger entry's settings changes in the env dict (in place), listing what happened in report."""
    for s in entry.get("settings") or []:
        key = s.get("key", "")
        if not key.startswith("env."):
            report.append("%s: unknown setting, left alone" % key)
            continue
        name, before, after = key[4:], s.get("before"), s.get("after")
        if name not in env:
            report.append("%s: already gone" % key)
        elif str(env[name]) != str(after):
            report.append("%s: changed since install, left alone" % key)
        elif before is None:
            del env[name]
            report.append("%s: deleted (was not set before)" % key)
        elif str(before) == str(after):
            report.append("%s: left in place (already set by you before install)" % key)
        else:
            env[name] = before
            report.append("%s: restored to %s" % (key, json.dumps(before)))


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


def apply_settings(a, env, entry, recs, report):
    """Set one settings_env action in env, recording {key, before, after} in recs."""
    key, value = a["key"], str(a["value"])
    old = next((s for s in (entry or {}).get("settings") or [] if s.get("key") == "env." + key), None)
    cur = env.get(key, MISSING)
    if cur is not MISSING and str(cur) == value:
        rec = old if old and str(old.get("after")) == value else {"key": "env." + key, "before": cur, "after": cur}
    else:
        if old and cur is not MISSING and str(cur) == str(old.get("after")):
            before = old["before"]  # applied before with another value: keep the original prior value
        else:
            before = None if cur is MISSING else cur
        env[key] = value
        rec = {"key": "env." + key, "before": before, "after": value}
    if rec["before"] is not None and str(rec["before"]) == str(rec["after"]):
        report.append("env.%s: already set by you (stays on remove)" % key)
    recs.append(rec)


def tidy_env(settings, new_settings):
    """Drop an env object that is empty now unless it was empty already."""
    if new_settings.get("env") == {} and settings.get("env") != {}:
        del new_settings["env"]


def plan(item, remove=False):
    """The changes an apply / remove makes: [{"file", "old", "new", "what"}], the manual notes and a report of what
    happens to settings.json (the ledger's own change has "internal": True)."""
    changes, notes, report = [], [], []
    item_id = item["id"]
    claude_md, md_path = setup.read_text(setup.claude_md_path()), setup.claude_md_path()
    settings, raw, error = read_settings()
    ledger, ledger_raw = read_ledger()
    entry = ledger_entry(ledger, item_id)
    new_md, new_settings, new_ledger = claude_md, json.loads(json.dumps(settings)), ledger
    recs = []
    for a in item.get("apply") or []:
        if a["kind"] == "claude_md":
            if remove:
                new_md, found = setup.remove_block(new_md, block_re(item_id))
                if found:
                    report.append("CLAUDE.md: block removed")
            else:
                new_md, _ = setup.upsert_block(new_md, block_re(item_id),
                                               lambda eol, a=a: make_opt_block(item_id, a["text"], eol))
        elif a["kind"] == "settings_env":
            if error:
                raise SystemExit("settings.json cannot be edited: %s" % error)
            env = new_settings.setdefault("env", {})
            if remove:
                if not entry and a["key"] in env:
                    report.append("env.%s: no install record, left alone" % a["key"])
            else:
                apply_settings(a, env, entry, recs, report)
        elif not remove:
            notes.append(a["text"])
    if remove:
        if entry and entry.get("settings"):
            if error:
                raise SystemExit("settings.json cannot be edited: %s" % error)
            restore_settings(new_settings.setdefault("env", {}), entry, report)
        new_ledger = without_entry(ledger, item_id)
    elif recs:
        new_entry = dict(entry or {}, id=item_id, settings=recs)
        new_entry.setdefault("at", now_iso())
        new_ledger = with_entry(ledger, new_entry)
    tidy_env(settings, new_settings)
    if new_md != claude_md:
        changes.append({"file": md_path, "old": claude_md, "new": new_md, "what": "CLAUDE.md"})
    if new_settings != settings:
        changes.append({"file": setup.settings_path(), "old": raw, "new": json_text(new_settings, raw),
                        "what": "settings.json", "backup": raw is not None})
    lc = ledger_change(ledger, new_ledger, ledger_raw)
    if lc:
        changes.append(lc)
    return changes, notes, report


def plan_all():
    """Everything devtime installed, taken out: every opt block, the setup block, every ledger settings change."""
    claude_md, md_path = setup.read_text(setup.claude_md_path()), setup.claude_md_path()
    settings, raw, error = read_settings()
    ledger, ledger_raw = read_ledger()
    changes, report = [], []
    new_md = claude_md
    for item_id in [m.group(1) for m in ANY_OPT_RE.finditer(claude_md or "")]:
        new_md, _ = setup.remove_block(new_md, block_re(item_id))
        report.append("CLAUDE.md: block %s removed" % item_id)
    new_md, found = setup.remove_block(new_md, setup.BLOCK_RE)
    if found:
        report.append("CLAUDE.md: usage tracking block removed")
    new_settings = json.loads(json.dumps(settings))
    if any(e.get("settings") for e in ledger["items"]):
        if error:
            raise SystemExit("settings.json cannot be edited: %s" % error)
        env = new_settings.setdefault("env", {})
        for entry in ledger["items"]:
            restore_settings(env, entry, report)
        tidy_env(settings, new_settings)
    if new_md != claude_md:
        changes.append({"file": md_path, "old": claude_md, "new": new_md, "what": "CLAUDE.md"})
    if new_settings != settings:
        changes.append({"file": setup.settings_path(), "old": raw, "new": json_text(new_settings, raw),
                        "what": "settings.json", "backup": raw is not None})
    lc = ledger_change(ledger, dict(ledger, items=[]), ledger_raw)
    if lc:
        changes.append(lc)
    return changes, report


def write_changes(changes):
    for c in changes:
        os.makedirs(os.path.dirname(c["file"]), exist_ok=True)
        if c.get("backup"):
            shutil.copyfile(c["file"], c["file"] + ".bak")
        with open(c["file"], "wb") as f:
            f.write(c["new"].encode("utf-8"))


def finish(out, changes, dry_run, label):
    """Print the result (and, for a dry run, the diffs); otherwise write the changes and mark them for the dashboard."""
    out["changed"] = [c["what"] for c in changes if not c.get("internal")]
    if dry_run:
        print(json.dumps(out, separators=(",", ":")))
        for c in changes:
            print(unified(c["old"], c["new"], c["what"]) or "(no change)")
        if not changes:
            print("(no change)")
        return 0
    write_changes(changes)
    if out["changed"] and label:
        from . import overview
        out["marker"] = overview.record_change(label, None, overview.CHANGES)
    else:
        out["marker"] = None
    print(json.dumps(out, separators=(",", ":")))
    return 0


def custom_item(item_id):
    return {"id": item_id, "title": item_id, "apply": [{"kind": "claude_md", "text": ""}]}


def find_removable(item_id):
    """A catalogue item, or a synthetic one for an adopted / custom block or ledger entry."""
    items = load_catalogue()
    if any(i["id"] == item_id for i in items):
        return find_item(items, item_id)
    if block_re(item_id).search(setup.read_text(setup.claude_md_path()) or "") \
            or ledger_entry(read_ledger()[0], item_id):
        return custom_item(item_id)
    return find_item(items, item_id)


def apply_cmd(item_id, dry_run=False, remove=False):
    item = find_removable(item_id) if remove else find_item(load_catalogue(), item_id)
    changes, notes, report = plan(item, remove)
    out = {"id": item_id, "action": "remove" if remove else "apply", "dryRun": dry_run, "notes": notes,
           "report": report}
    return finish(out, changes, dry_run, ("Removed " if remove else "Applied ") + item["title"])


def remove_all_cmd(dry_run=False):
    changes, report = plan_all()
    out = {"id": "all", "action": "remove", "dryRun": dry_run, "notes": [], "report": report}
    return finish(out, changes, dry_run, "Removed all devtime optimizations")


# ---------------------------------------------------------------------------------------------------------------------
# adopt: make a hand-written CLAUDE.md section removable


def heading_of(line):
    """(level, text) of a markdown heading line, else None."""
    m = re.match(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", line.rstrip("\r\n"))
    return (len(m.group(1)), m.group(2).strip()) if m else None


def adopt_section(text, item_id, heading):
    """text with the section under `heading` wrapped in the devtime:opt tags for item_id (SystemExit if it cannot)."""
    want = heading.strip().lstrip("#").strip()
    lines = text.splitlines(True)
    eol = "\r\n" if "\r\n" in text else "\n"
    heads, fence = [], False
    for i, line in enumerate(lines):
        if re.match(r"^\s{0,3}(```|~~~)", line):
            fence = not fence
        elif not fence:
            h = heading_of(line)
            if h:
                heads.append((i, h[0], h[1]))
    found = [h for h in heads if h[2] == want]
    if not found:
        raise SystemExit("heading %r not found in CLAUDE.md" % heading)
    if len(found) > 1:
        raise SystemExit("heading %r appears %d times in CLAUDE.md; cannot tell which to adopt" % (heading, len(found)))
    first, level = found[0][0], found[0][1]
    stop = next((h[0] for h in heads if h[0] > first and h[1] <= level), len(lines))
    last = stop
    while last > first + 1 and not lines[last - 1].strip():
        last -= 1  # the blank lines before the next heading stay outside the block
    start_pos, end_pos = sum(map(len, lines[:first])), sum(map(len, lines[:last]))
    spans = [m.span() for m in ANY_OPT_RE.finditer(text)] + [m.span() for m in setup.BLOCK_RE.finditer(text)]
    if any(s < end_pos and start_pos < e for s, e in spans):
        raise SystemExit("heading %r is inside (or contains) a devtime block; nothing to adopt" % heading)
    body = lines[first:last]
    if not body[-1].endswith(("\n", "\r")):
        body[-1] += eol
    return "".join(lines[:first] + ["<!-- devtime:opt:%s -->%s" % (item_id, eol)] + body
                   + ["<!-- devtime:opt-end:%s -->%s" % (item_id, eol)] + lines[last:])


def adopt_setting_cmd(item_id, keys, dry_run=False):
    """Records settings the user set by hand (env.KEY) as installed under item_id, as if they were absent before, so
    removing item_id deletes them (unless changed since)."""
    settings, _, error = read_settings()
    if error:
        raise SystemExit("settings.json cannot be read: %s" % error)
    env = settings.get("env") or {}
    recs = []
    for key in keys:
        name = key[4:] if key.startswith("env.") else key
        if name not in env:
            raise SystemExit("env.%s is not set in settings.json; nothing to adopt" % name)
        recs.append({"key": "env." + name, "before": None, "after": str(env[name])})
    ledger, ledger_raw = read_ledger()
    old = ledger_entry(ledger, item_id) or {}
    have = {r["key"] for r in recs}
    recs = [r for r in old.get("settings") or [] if r.get("key") not in have] + recs
    entry = dict(old, id=item_id, adopted=True, settings=recs)
    entry.setdefault("at", now_iso())
    changes = []
    lc = ledger_change(ledger, with_entry(ledger, entry), ledger_raw)
    if lc:
        changes.append(lc)
    out = {"id": item_id, "action": "adopt", "dryRun": dry_run, "settings": sorted(have), "notes": [],
           "report": ["%s: recorded; remove deletes it" % k for k in sorted(have)]}
    return finish(out, changes, dry_run, None)


def adopt_cmd(item_id, heading=None, dry_run=False, settings=None):
    if not ID_RE.match(item_id or "") or item_id == "all":
        raise SystemExit("id %r: use lowercase letters, digits and dashes ('all' is reserved)" % item_id)
    if settings:
        return adopt_setting_cmd(item_id, settings, dry_run)
    if not heading:
        raise SystemExit("adopt needs --heading or --setting")
    md = setup.read_text(setup.claude_md_path())
    if md is None:
        raise SystemExit("%s does not exist" % setup.claude_md_path())
    if block_re(item_id).search(md):
        raise SystemExit("CLAUDE.md already has a devtime block for %s" % item_id)
    new_md = adopt_section(md, item_id, heading)
    ledger, ledger_raw = read_ledger()
    entry = {"id": item_id, "at": now_iso(), "adopted": True, "heading": heading.strip()}
    changes = [{"file": setup.claude_md_path(), "old": md, "new": new_md, "what": "CLAUDE.md"}]
    lc = ledger_change(ledger, with_entry(ledger, entry), ledger_raw)
    if lc:
        changes.append(lc)
    out = {"id": item_id, "action": "adopt", "dryRun": dry_run, "heading": heading.strip(), "notes": [], "report": []}
    return finish(out, changes, dry_run, None)


# ---------------------------------------------------------------------------------------------------------------------
# list


def list_cmd():
    claude_md = setup.read_text(setup.claude_md_path())
    settings = read_settings()[0]
    ledger = read_ledger()[0]
    items = load_catalogue()
    rows = []
    for item in items:
        state = applied_state(item, claude_md, settings)
        entry = ledger_entry(ledger, item["id"])
        if state == "devtime" and entry and entry.get("adopted"):
            state = "adopted"
        if not offered(item) and not state:  # needs parallel support; still listed while installed, so it can be removed
            continue
        rows.append((item, state, item["title"], item["summary"]))
    known = {i["id"] for i in items}
    for m in ANY_OPT_RE.finditer(claude_md or ""):
        if m.group(1) not in known:
            entry = ledger_entry(ledger, m.group(1)) or {}
            rows.append((custom_item(m.group(1)), "adopted", "(custom block)",
                         "Heading: %s" % entry["heading"] if entry.get("heading") else "Not from the catalogue."))
    for item, state, title, summary in rows:
        print("%-24s %-8s %s" % (item["id"], state or "-", title))
        print("    " + summary)
        try:
            report = plan(item, True)[2]
        except SystemExit:
            report = ["settings.json cannot be read"]
        if state == "manual":
            report = report + ["CLAUDE.md text is yours, not devtime's: left alone (adopt makes it removable)"]
        print("    remove: " + ("; ".join(report) if report else "nothing to do"))
    return 0


def optimize_main(args):
    if args.opt_cmd == "list":
        return list_cmd()
    if args.opt_cmd == "adopt":
        return adopt_cmd(args.id, args.heading, args.dry_run, getattr(args, "setting", None))
    if args.opt_cmd == "remove" and args.id == "all":
        return remove_all_cmd(args.dry_run)
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
        "weekly-cap": (None, "weekly reserve block %s" % ("present" if claude_md and setup.BLOCK_RE.search(claude_md) else "missing")),
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
        if not offered(item) and not state:
            continue
        usd, why = est.get(item["id"], (None, ""))
        items.append({"id": item["id"], "applied": state is not None and state != "partly", "by": state,
                      "estWeeklyUsd": usd, "why": why, "guard": bool(item.get("guard"))})
    items.sort(key=lambda x: (x["estWeeklyUsd"] is None, -(x["estWeeklyUsd"] or 0)))
    m["catalogue"] = items
    return m


def analyze_cmd(days):
    print(json.dumps(analyze(days), separators=(",", ":")))
    return 0
