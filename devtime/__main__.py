"""devtime: where development time goes in Claude Code sessions.

    python -m devtime serve [--port 8765] [--no-open]   live dashboard at http://127.0.0.1:8765
    python -m devtime report [--project MyProject] [--since 2026-10-07] [--top 15]
    python -m devtime json out.json [--project MyProject]  write the dataset
    python -m devtime projects                           list projects found and their totals
    python -m devtime findings [--project MyProject] [--script my_script.py] [--since 1d] [--latest] [--json]
                                                         what wastes time in Unreal runs, and what to change
    python -m devtime plan-sample --five 14 --five-reset 2026-10-11T16:10Z --week 74 --week-reset 2026-10-16T00:00Z
                                                         record the plan's usage as the app reports it (a session
                                                         reads it with get_usage); the weekly-limit chart is
                                                         pinned to these readings (of the current org)
    python -m devtime mark "Runner agent on Haiku" [--at 2026-10-11T10:26Z]
                                                         mark a change on the weekly-limit chart (<data dir>/config/changes.toml)
    python -m devtime review-note --session "Main session" --text "..."
                                                         a usage reviewer's note for a main session (by title);
                                                         delivered to it like a usage alert
    python -m devtime work-items [--days 8] [--max 250]  unlabelled pieces of work and the existing section names
                                                         (JSON), for the scheduled section labeller
    python -m devtime setup check | claude-md --cap N [--dry-run] | open
                                                         what /devtime:start does mechanically (devtime/setup.py)
    python -m devtime analyze [--days 7]                compact JSON of usage metrics and the ranked optimization catalogue
    python -m devtime optimize list | apply ID [--dry-run] | remove ID|all [--dry-run] | adopt ID --heading H | --setting env.KEY [--dry-run]
                                                         what /devtime:optimize does (devtime/optimize.py); remove
                                                         restores prior settings.json values from the install ledger
    python -m devtime ab new ID --title T --arm name[:model[:agent_type]] ... | arm ID STAGE | report ID | stop ID | list
                                                         what /devtime:ab does (devtime/ab.py)
    python -m devtime work-assign FILE|-                merge {"<id>": "<section or ->"} (file, or - for stdin) into
                                                         <data dir>/cache/work_sections.json
    python -m devtime tick                               start the server if needed; compact JSON for one loop pass
    python -m devtime review-context [--hours 2]         summary + review log tail for the usage reviewer (devtime/tick.py)
    python -m devtime review-done --text "..."          record a finished review
    python -m devtime org                                the current organisation, its instance folder and port
    python -m devtime slot run RES [--exclusive] [--max-hold MIN] [--label T] -- CMD ... | status | kill N | log
                                                         machine-wide FIFO queue for heavy jobs (devtime/slots.py),
                                                         shared by every session, project and org on the PC

Each organisation on the PC has its own devtime (devtime/paths.py): every command works on the instance of the org it
runs in (env CLAUDE_CODE_ORGANIZATION_UUID, else ~/.claude.json's login). If that org can't be determined, commands
that use devtime's data print {"refused": true, ...} and exit 2.
"""

import argparse
import datetime
import json
import sys
import time

from . import paths

Store = None  # devtime.store.Store, imported by run()

NO_ORG_OK = {"org", "mark", "setup:claude-md", "ab:new", "ab:arm", "ab:stop", "ab:list", "optimize:list",
             "optimize:apply", "optimize:remove", "optimize:adopt", None}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["slot"]:  # machine-wide, no org needed (devtime/slots.py)
        from . import slots
        return slots.main(argv[1:])
    return run(argv)


def org_cmd():
    from . import ports
    out = {"org": paths.ORG, "source": paths.ORG_SOURCE, "name": paths.org_name(paths.ORG) if paths.ORG else None,
           "instance": paths.INSTANCE if paths.ORG else None, "dataDir": paths.DATA}
    if paths.ORG:
        number = ports.recorded()
        out.update(port=number, serverAnswering=bool(number and ports.ours(number)),
                   url="http://%s:%d/" % (ports.HOST, number) if number else None)
    else:
        out["error"] = paths.NO_ORG_TEXT
    print(json.dumps(out, separators=(",", ":")))


def run(argv=None):
    global Store
    if Store is None:
        from .store import Store  # heavy (scan, analyze ...): not imported for `slot`
    parser = argparse.ArgumentParser(prog="devtime", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--project")
    p.add_argument("--since", help="ISO date/time (UTC)")
    p.add_argument("--top", type=int, default=15)
    p = sub.add_parser("json")
    p.add_argument("out")
    p.add_argument("--project")
    p = sub.add_parser("projects")
    p = sub.add_parser("findings")
    p.add_argument("--project")
    p.add_argument("--script", help="e.g. my_script.py")
    p.add_argument("--since", help="ISO date/time, or a duration like 6h / 2d")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--latest", action="store_true", help="the most recent run only (e.g. from a hook after a run)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("plan-sample")
    p.add_argument("--five", type=float, required=True, help="5-hour limit, percent used")
    p.add_argument("--five-reset", help="when the 5-hour window resets (ISO 8601)")
    p.add_argument("--week", type=float, required=True, help="weekly limit (all models), percent used")
    p.add_argument("--week-reset", help="when the weekly limit resets (ISO 8601)")
    p = sub.add_parser("mark")
    p.add_argument("label")
    p.add_argument("--at", help="ISO 8601 (default: now)")
    p = sub.add_parser("review-note")
    p.add_argument("--session", required=True, help="the main session's title, as /api/watch shows it")
    p.add_argument("--text", required=True)
    p = sub.add_parser("work-items")
    p.add_argument("--days", type=float, default=8)
    p.add_argument("--max", type=int, default=250)
    p = sub.add_parser("work-assign")
    p.add_argument("file", help='JSON {"<transcript id>": "<section name or ->", ...}; - reads stdin')
    p = sub.add_parser("tick")
    sub.add_parser("org")
    p = sub.add_parser("review-context")
    p.add_argument("--hours", type=float, default=2)
    p = sub.add_parser("review-done")
    p.add_argument("--text", required=True, help="one line: the notes left, or nothing to raise")
    p = sub.add_parser("setup")
    ssub = p.add_subparsers(dest="setup_cmd", required=True)
    ssub.add_parser("check")
    q = ssub.add_parser("claude-md")
    q.add_argument("--cap", type=float, required=True, help="weekly cap, percent of the all-models limit")
    q.add_argument("--five-hour-cap", type=float, help="5-hour cap, percent of the 5-hour limit (omit for none)")
    q.add_argument("--dry-run", action="store_true")
    ssub.add_parser("open")
    p = sub.add_parser("analyze")
    p.add_argument("--days", type=float, default=7)
    p = sub.add_parser("optimize")
    osub = p.add_subparsers(dest="opt_cmd", required=True)
    osub.add_parser("list")
    q = osub.add_parser("apply")
    q.add_argument("id")
    q.add_argument("--dry-run", action="store_true")
    q = osub.add_parser("remove")
    q.add_argument("id", help="a catalogue / adopted id, or 'all'")
    q.add_argument("--dry-run", action="store_true")
    q = osub.add_parser("adopt")
    q.add_argument("id", help="catalogue id or a custom id (lowercase letters, digits, dashes)")
    q.add_argument("--heading", help="text of the markdown heading of the CLAUDE.md section")
    q.add_argument("--setting", action="append", help="env.KEY in settings.json set by hand (repeatable): removing ID deletes it")
    q.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("ab")
    asub = p.add_subparsers(dest="ab_cmd", required=True)
    q = asub.add_parser("new")
    q.add_argument("id")
    q.add_argument("--title")
    q.add_argument("--arm", action="append", help="name[:model[:agent_type]], repeatable (at least two)")
    q.add_argument("--instructions", action="append", help="armname=short text, repeatable")
    q.add_argument("--opt", help="id of the catalogue optimization this tests")
    q.add_argument("--dry-run", action="store_true")
    q = asub.add_parser("arm")
    q.add_argument("id")
    q.add_argument("stage")
    q = asub.add_parser("report")
    q.add_argument("id")
    q.add_argument("--json", action="store_true")
    q = asub.add_parser("stop")
    q.add_argument("id")
    asub.add_parser("list")
    args = parser.parse_args(argv)
    key = args.cmd
    for sub_key in ("setup_cmd", "ab_cmd", "opt_cmd"):
        if getattr(args, sub_key, None):
            key = "%s:%s" % (args.cmd, getattr(args, sub_key))
    if args.cmd == "org":
        return org_cmd()
    if not paths.ORG and key not in NO_ORG_OK:
        print(json.dumps({"refused": True, "error": paths.NO_ORG_TEXT}, separators=(",", ":")))
        return 2
    if args.cmd == "ab":
        from . import ab
        return ab.main(args)
    if args.cmd in ("analyze", "optimize"):
        from . import optimize
        return optimize.analyze_cmd(args.days) if args.cmd == "analyze" else optimize.optimize_main(args)
    if args.cmd == "setup":
        from . import setup
        return setup.main(args)
    if args.cmd == "work-assign":
        from .overview import assign_work
        if args.file == "-":
            mapping = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        else:
            with open(args.file, encoding="utf-8") as handle:
                mapping = json.load(handle)
        store = Store(verbose=False)  # keep to this org's own transcripts: another org's work is not ours to label
        store.refresh()
        mine = {t["id"] for t in store.slice("*")["transcripts"]}
        skipped = sum(1 for k in mapping if k not in mine)
        mapping = {k: v for k, v in mapping.items() if k in mine}
        print(f"assigned {assign_work(mapping)}" + (f" (skipped {skipped} not of this organisation)" if skipped else ""))
        return
    if args.cmd in ("tick", "review-context", "review-done"):
        from . import tick
        if args.cmd == "tick":
            return tick.tick()
        if args.cmd == "review-context":
            return tick.review_context(args.hours)
        return tick.review_done(args.text)
    if args.cmd == "mark":
        from .overview import record_change
        print(record_change(args.label, args.at))
        return
    if args.cmd == "review-note":
        from .alerts import record_note
        print(record_note(args.session, args.text))
        return
    if args.cmd == "plan-sample":
        from .overview import record_plan_sample
        print(record_plan_sample(args.five, args.five_reset, args.week, args.week_reset))
        return
    if args.cmd == "findings":
        return findings_cmd(args)

    if args.cmd == "serve":
        from . import migrate
        migrate.run()
        from .server import serve
        serve(Store(verbose=True), args.host, args.port, not args.no_open)
        return
    store = Store(verbose=False)
    store.refresh()
    if args.cmd == "report":
        project = args.project or store.slice("*").get("defaultProject")
        since = None
        if args.since:
            text = args.since + ("T00:00:00+00:00" if len(args.since) == 10 else "")
            since = datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        from .report import report
        report(store.slice(project), args.top, since)
    elif args.cmd == "json":
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(store.slice(args.project), handle)
    elif args.cmd == "work-items":
        from .overview import work_items
        print(json.dumps(work_items(store.slice("*"), time.time(), args.days, args.max), separators=(",", ":")))
    elif args.cmd == "projects":
        for p in store.slice("*").get("projects", []):
            last = datetime.datetime.fromtimestamp(p["last"]).strftime("%Y-%m-%d %H:%M")
            print(f"  {p['name'][:40]:40s} {p['sessions']:4d} sessions {p['subagents']:5d} subagents "
                  f"busy {p['busy'] / 3600:7.1f} h  ${p['cost']:9.2f}  last {last}")
    else:
        parser.print_help()


def parse_since(text):
    if not text:
        return None
    units = {"m": 60, "h": 3600, "d": 86400}
    if text[-1] in units and text[:-1].replace(".", "", 1).isdigit():
        return time.time() - float(text[:-1]) * units[text[-1]]
    text = text + ("T00:00:00" if len(text) == 10 else "")
    return datetime.datetime.fromisoformat(text).timestamp()


def findings_cmd(args):
    """Only the Unreal logs are read (no transcript scan), so this is quick enough to run after every editor run."""
    from . import findings
    from .config import Config
    from .unreal import Harvester
    config = Config()
    harvester = Harvester(config)
    harvester.refresh(min_interval=0)
    runs = list(harvester.runs.values())
    project = args.project or config.settings.get("default_project")
    if args.latest:
        run = findings.latest_run(runs, project, args.script)
        if args.json:
            print(json.dumps({"run": {k: run[k] for k in ("start", "end", "scripts", "fps", "exit", "issues")} if run else None,
                              "findings": findings.findings([run], top=args.top) if run else []}))
        elif run:
            findings.print_run(run)
        return
    items = findings.findings(runs, project, args.script, parse_since(args.since), args.top)
    if args.json:
        print(json.dumps(items))
    else:
        scope = " ".join(x for x in (project, args.script, f"since {args.since}" if args.since else "") if x)
        findings.print_findings(items, f"Time wasted in Unreal runs ({scope}), by pattern:")


if __name__ == "__main__":
    sys.exit(main())
